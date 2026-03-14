import requests
import json
import argparse
import sys
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    import colorama
    from colorama import Fore, Style
    colorama.init(autoreset=True)
except ImportError:
    class DummyColor:
        def __getattr__(self, name): return ""
    Fore = DummyColor()
    Style = DummyColor()

SESSION_COOKIE = "__Host-session=PUT HERE"
CSRF_TOKEN = "PUT HERE"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-CSRF-Token": CSRF_TOKEN,
    "Cookie": SESSION_COOKIE
}

GRAPHQL_URL = "https://hackerone.com/graphql"


QUERY = """
query OpportunityCategoryElasticQuery($from: Int, $size: Int, $query: OpportunitiesQuery!, $filter: QueryInput!, $sort: [SortInput!], $post_filters: OpportunitiesFilterInput) {
  opportunities_search(
    query: $query
    filter: $filter
    from: $from
    size: $size
    sort: $sort
    post_filters: $post_filters
  ) {
    nodes {
      ... on OpportunityDocument { id, handle, __typename }
      ...OpportunityList
      __typename
    }
    total_count
    __typename
  }
}

fragment OpportunityList on OpportunityDocument {
  id, ...OpportunityCard, __typename
}

fragment OpportunityCard on OpportunityDocument {
  handle, offers_bounties, first_response_time, structured_scope_stats, resolved_report_count, launched_at, __typename
}
"""

SCOPE_QUERY = """
query TeamScopes($handle: String!) {
  team(handle: $handle) {
    handle
    structured_scopes(archived: false) {
      nodes { asset_identifier, asset_type, eligible_for_submission }
    }
  }
}
"""

METRICS_QUERY = """
query TeamMetrics($handle: String!) {
  team(handle: $handle) {
    handle
    reports_received_last_90_days
    formatted_bounties_paid_last_90_days
    formatted_total_bounties_paid_amount
    average_bounty_lower_amount
    average_bounty_upper_amount
    response_efficiency_percentage
  }
}
"""

VARIABLES = {
    "size": 100,
    "from": 0,
    "query": {},
    "filter": {},
    "sort": [{"field": "launched_at", "direction": "DESC"}]
}

def print_banner():
    banner = rf"""{Fore.GREEN}
  _   _                          ____      _ _           _
 | | | |      ___  _ __   ___   / ___|___ | | | ___  ___| |_ ___  _ __
 | |_| | __  / _ \| '_ \ / _ \ | |   / _ \| | |/ _ \/ __| __/ _ \| '__|
 |  _  | __ | (_) | | | |  __/ | |__| (_) | | |  __/ (__| || (_) | |
 |_| |_|     \___/|_| |_|\___|  \____\___/|_|_|\___|\___|\__\___/|_|

                          by: rv_u{Style.RESET_ALL}
    """
    print(banner)
    print(f"{Fore.CYAN}=" * 85)

def extract_scope(target):
    print(f"[+] Fetching scopes for target: {target}...")
    payload = {"operationName": "TeamScopes", "query": SCOPE_QUERY, "variables": {"handle": target}}
    response = requests.post(GRAPHQL_URL, headers=HEADERS, json=payload)

    if response.status_code != 200:
        print(f"[-] Error {response.status_code} while fetching scopes.")
        return

    try:
        scopes = response.json()['data']['team']['structured_scopes']['nodes']
    except:
        print(f"[-] Could not find scopes for '{target}'.")
        return

    wildcards = []
    domains = []

    for s in scopes:
        if not s.get('eligible_for_submission'):
            continue

        asset = s.get('asset_identifier', '')
        if s.get('asset_type') == 'WILDCARD' or '*.' in asset:
            wildcards.append(asset.replace('*.', ''))
        elif s.get('asset_type') == 'URL':
            domains.append(asset)

    if wildcards:
        with open(f"{target}_wildcards.txt", "w") as f: f.write("\n".join(wildcards))
        print(f"[v] Saved {len(wildcards)} Eligible Wildcards to '{target}_wildcards.txt'")
    if domains:
        with open(f"{target}_domains.txt", "w") as f: f.write("\n".join(domains))
        print(f"[v] Saved {len(domains)} Eligible Domains to '{target}_domains.txt'")
    if not wildcards and not domains:
        print("[-] No eligible Wildcards or Domains found.")

def fetch_programs():
    print("[+] Connecting to HackerOne API (Fetching unique programs)...")
    all_nodes, seen_handles = [], set()
    current_from = 0

    while True:
        VARIABLES["from"] = current_from
        payload = {"operationName": "OpportunityCategoryElasticQuery", "query": QUERY, "variables": VARIABLES}
        response = requests.post(GRAPHQL_URL, headers=HEADERS, json=payload)

        if response.status_code != 200: break

        try:
            nodes = response.json()['data']['opportunities_search']['nodes']
            if not nodes: break

            for node in nodes:
                handle = node.get('handle')
                if handle and handle not in seen_handles:
                    seen_handles.add(handle)
                    all_nodes.append(node)

            sys.stdout.write(f"\r[*] Collected {len(all_nodes)} unique programs...")
            sys.stdout.flush()

            if len(nodes) < 100: break
            current_from += 100
        except: break

    print("\n[+] Data collection complete!")
    return all_nodes

ERROR_PRINTED = False

def fetch_90d_metrics(handle):
    global ERROR_PRINTED
    payload = {"operationName": "TeamMetrics", "query": METRICS_QUERY, "variables": {"handle": handle}}
    try:
        resp = requests.post(GRAPHQL_URL, headers=HEADERS, json=payload, timeout=15)
        if resp.status_code == 200:
            json_data = resp.json()

            if 'errors' in json_data:
                if not ERROR_PRINTED:
                    error_message = json_data['errors'][0].get('message', 'Unknown Error')
                    print(f"\n[!] GraphQL Debug: {error_message}")
                    ERROR_PRINTED = True
                return handle, {}

            team = json_data.get('data', {}).get('team', {}) or {}

            return handle, {
                'reports_90d': team.get('reports_received_last_90_days'),
                'bounties_90d': team.get('formatted_bounties_paid_last_90_days'),
                'total_paid': team.get('formatted_total_bounties_paid_amount'),
                'avg_bounty_low': team.get('average_bounty_lower_amount'),
                'avg_bounty_up': team.get('average_bounty_upper_amount'),
                'efficiency': team.get('response_efficiency_percentage')
            }
    except Exception:
        pass
    return handle, {}

def analyze_and_sort(nodes, args):
    print("[+] Applying your initial filters...")
    filtered_programs = []

    for node in nodes:
        handle = node.get('handle')
        scope_stats = node.get('structured_scope_stats') or {}

        total_assets = sum(scope_stats.values()) if isinstance(scope_stats, dict) else 0

        w_count = scope_stats.get('WILDCARD', 0)
        m_count = scope_stats.get('GOOGLE_PLAY_APP_ID', 0) + scope_stats.get('APPLE_STORE_APP_ID', 0)
        d_count = scope_stats.get('URL', 0)

        is_match, filters_applied = False, False

        if args.wildcard: filters_applied = True; is_match = is_match or (w_count > 0)
        if args.mobile: filters_applied = True; is_match = is_match or (m_count > 0)
        if args.domain: filters_applied = True; is_match = is_match or (d_count > 0)

        bounties = node.get('offers_bounties', False)

        if args.bounty_only:
            filters_applied = True
            if (is_match or not any([args.wildcard, args.mobile, args.domain])) and bounties:
                is_match = True
            else:
                is_match = False

        elif args.vdp:
            filters_applied = True
            if (is_match or not any([args.wildcard, args.mobile, args.domain])) and not bounties:
                is_match = True
            else:
                is_match = False

        if filters_applied and not is_match: continue

        resolved = node.get('resolved_report_count') or 0
        first_resp = node.get('first_response_time')
        launched_at = node.get('launched_at') or "1970-01-01"

        filtered_programs.append({
            'handle': handle,
            'offers_bounties': 'Yes' if bounties else 'No',
            'w_count': w_count, 'm_count': m_count, 'd_count': d_count,
            'total_assets': total_assets, # تم حفظ الإجمالي هنا بدلاً من استعلام الـ GraphQL
            'resolved_count': resolved,
            'first_resp': first_resp if first_resp is not None else 9999.0,
            'launched_at': launched_at,
            'metrics': {}
        })

    print(f"[*] Extracting deep financial & asset metrics for {len(filtered_programs)} targets...")
    completed = 0
    total = len(filtered_programs)

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_handle = {executor.submit(fetch_90d_metrics, p['handle']): p for p in filtered_programs}

        for future in as_completed(future_to_handle):
            program = future_to_handle[future]
            handle, metrics_dict = future.result()

            program['metrics'] = metrics_dict

            completed += 1
            sys.stdout.write(f"\r[*] Progress: [{completed}/{total}] targets analyzed...")
            sys.stdout.flush()

    print("\n[+] Deep data collection complete!")

    if args.bounty:
        print(f"[*] Filtering programs with average bounty >= ${args.bounty}...")
        filtered_programs = [
            p for p in filtered_programs
            if p['offers_bounties'] == 'Yes' and (p['metrics'].get('avg_bounty_low') or 0) >= args.bounty
        ]

    if args.compare == 'least':
        sorted_programs = sorted(filtered_programs, key=lambda x: x['metrics'].get('reports_90d') or 99999)
        print("[*] Sorted by: Low Competition (Least Reports in LAST 90 DAYS)")
    elif args.compare == 'eff':
        sorted_programs = sorted(filtered_programs, key=lambda x: x['metrics'].get('efficiency') or 0, reverse=True)
        print("[*] Sorted by: Highest Response Efficiency %")
    else:
        sorted_programs = sorted(filtered_programs, key=lambda x: x['launched_at'], reverse=True)
        print("[*] Sorted by: Launch Date (Newest to Oldest)")

    return sorted_programs

def format_bounty_range(low, up):
    if low and up:
        l_str = f"{int(low/1000)}k" if low >= 1000 else str(low)
        u_str = f"{int(up/1000)}k" if up >= 1000 else str(up)
        return f"${l_str}-${u_str}"
    elif low:
        return f"${low}+"
    return "--"

def export_to_csv(filename, results):
    print(f"[*] Exporting results to {filename}...")
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Program Handle', 'Bounty', 'Wildcards', 'Total Assets', 'Resolved Reps', '90d Reps', '90d Paid', 'Total Paid', 'Avg Bounty', 'First Resp(h)', 'Efficiency %', 'Launched Date'])

        for p in results:
            m = p['metrics']
            w_count = p['w_count']
            assets = p['total_assets']
            resolved = p['resolved_count']
            rep_90d = m.get('reports_90d') or 0
            eff = m.get('efficiency')
            efficiency = f"{eff}%" if eff is not None else "--"
            first_resp = f"{p['first_resp']}h" if p['first_resp'] != 9999.0 else "--"
            date_only = p['launched_at'][:10]

            if p['offers_bounties'] == 'Yes':
                b_90d = m.get('bounties_90d')
                paid_90d = f"${b_90d:,}" if b_90d else "$0"
                t_paid = m.get('total_paid')
                total_paid = f"${t_paid:,}" if t_paid else "$0"
                avg_bnty = format_bounty_range(m.get('avg_bounty_low'), m.get('avg_bounty_up'))
            else:
                paid_90d = "--"
                total_paid = "--"
                avg_bnty = "--"

            writer.writerow([
                p['handle'], p['offers_bounties'], w_count, assets, resolved,
                rep_90d, paid_90d, total_paid, avg_bnty, first_resp, efficiency, date_only
            ])
    print(f"[v] Export completed successfully!")

def main():
    custom_epilog = """
=============================================================================
🌟 H1 Collector Capabilities & Examples:
=============================================================================
1. General Radar:
   > python H-OneCollector.py

2. Scope Extractor:
   > python H-OneCollector.py paypal

3. Target Wildcards:
   > python H-OneCollector.py -w -c least

4. Financial Filter:
   > python H-OneCollector.py -b 1000 -c eff

5. VDP Filter:
   > python H-OneCollector.py -V -w

6. Bounty Filter:
   > python H-OneCollector.py -B -m

7. Export to CSV:
   > python H-OneCollector.py -w -o wildcards_targets.csv
=============================================================================
    """

    parser = argparse.ArgumentParser(
        description="HackerOne Recon Sniper - Ultimate Pro Version",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=custom_epilog,
        add_help=False
    )

    parser.add_argument('-help', '--help', action='help', default=argparse.SUPPRESS, help='Show this detailed help message and exit')
    parser.add_argument('target', nargs='?', help="Target handle to extract scopes (e.g., paypal)")
    parser.add_argument('-w', '--wildcard', action='store_true', help="Filter programs with Wildcards (*.domain)")
    parser.add_argument('-m', '--mobile', action='store_true', help="Filter programs with Mobile apps (Android/iOS)")
    parser.add_argument('-d', '--domain', action='store_true', help="Filter programs with specific Domains/URLs")
    parser.add_argument('-b', '--bounty', type=int, help="Minimum Average Bounty amount (e.g., 500)")
    parser.add_argument('-V', '--vdp', action='store_true', help="Filter programs that DO NOT pay bounties (VDP only)")
    parser.add_argument('-B', '--bounty-only', action='store_true', help="Filter programs that DO pay bounties only")
    parser.add_argument('-o', '--output', type=str, help="Export results to a CSV file (e.g., targets.csv)")
    parser.add_argument('-c', '--compare', choices=['least', 'eff', 'new'], default='new',
                        help="Sort by: 'least' (Least 90d Reports), 'eff' (Best Efficiency %), 'new' (Newest)")

    if '-help' in sys.argv or '--help' in sys.argv:
        print_banner()
        parser.print_help()
        sys.exit(0)

    print_banner()
    args = parser.parse_args()

    if args.target:
        extract_scope(args.target)
    else:
        nodes = fetch_programs()
        if nodes:
            results = analyze_and_sort(nodes, args)
            if not results:
                print("[-] No results found matching your criteria.")
                return

            if args.output:
                export_to_csv(args.output, results)

            print("\n" + f"{Fore.CYAN}="*147)
            print(f"{Fore.CYAN}{'Program Handle':<18} | {'Bounty':<6} | {'W':<3} | {'Assets':<6} | {'Resolved':<8} | {'90d Reps':<8} | {'90d Paid':<9} | {'Total Paid':<10} | {'Avg Bnty':<10} | {'F_Resp':<7} | {'Efficiency':<10} | {'Launched'}{Style.RESET_ALL}")
            print(f"{Fore.CYAN}="*147 + Style.RESET_ALL)

            for p in results:
                handle = p['handle'][:18]
                w_count = p['w_count']
                assets_count = str(p['total_assets'])
                resolved = str(p['resolved_count'])

                m = p['metrics']
                rep_90d = str(m.get('reports_90d') or "0")
                eff = m.get('efficiency')
                efficiency = f"{eff}%" if eff is not None else "--"

                if p['offers_bounties'] == 'Yes':
                    b_90d = m.get('bounties_90d')
                    paid_90d = f"${b_90d:,}" if b_90d else "$0"

                    t_paid = m.get('total_paid')
                    total_paid = f"${t_paid:,}" if t_paid else "$0"

                    avg_bnty = format_bounty_range(m.get('avg_bounty_low'), m.get('avg_bounty_up'))
                else:
                    paid_90d = "--"
                    total_paid = "--"
                    avg_bnty = "--"

                first_resp = f"{p['first_resp']}h" if p['first_resp'] != 9999.0 else "--"
                date_only = p['launched_at'][:10]

                print(f"{handle:<18} | {p['offers_bounties']:<6} | {w_count:<3} | {assets_count:<6} | {resolved:<8} | {rep_90d:<8} | {paid_90d:<9} | {total_paid:<10} | {avg_bnty:<10} | {first_resp:<7} | {efficiency:<10} | {date_only}")

if __name__ == "__main__":
    main()
