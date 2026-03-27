"""
Stephenson Agency — Ricochet360 Call History Scraper
Runs daily via GitHub Actions at 6am
Logs into Ricochet360, downloads call history, saves as JSON for the dashboard
"""

import requests
import json
import csv
import io
import os
import sys
from datetime import datetime, timedelta

# ── CREDENTIALS (set these as GitHub Secrets) ────────────────────────────────
RICOCHET_EMAIL    = os.environ.get('RICOCHET_EMAIL', '')
RICOCHET_PASSWORD = os.environ.get('RICOCHET_PASSWORD', '')
RICOCHET_DOMAIN   = 'stephenson.ricochet.me'
REPORTS_BASE      = 'https://reports.ricochet.me/stephenson'
MAIN_LINES        = {'15094081190', '15095162250', '15095353171'}

# ── DATE RANGE ───────────────────────────────────────────────────────────────
# Pull last 60 days every run so the dashboard always has plenty of history
today    = datetime.now()
from_dt  = today - timedelta(days=60)
FROM_STR = from_dt.strftime('%Y-%m-%d')
TO_STR   = today.strftime('%Y-%m-%d')

REPORT_FIELDS = [
    'lead_id', 'created_at', 'call_type', 'user_name',
    'Duration', 'caller_id', 'to', 'call_campaign', 'lead_name'
]

def login(session):
    """Log into Ricochet360 and return authenticated session"""
    print(f"Logging in as {RICOCHET_EMAIL}...")

    # Get CSRF token first
    r = session.get(f'https://{RICOCHET_DOMAIN}/login')
    r.raise_for_status()

    # Extract CSRF token from HTML
    import re
    csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    csrf_token = csrf.group(1) if csrf else ''

    # Submit login form
    r = session.post(f'https://{RICOCHET_DOMAIN}/login', data={
        '_token': csrf_token,
        'email': RICOCHET_EMAIL,
        'password': RICOCHET_PASSWORD,
    }, headers={
        'Referer': f'https://{RICOCHET_DOMAIN}/login',
        'Content-Type': 'application/x-www-form-urlencoded',
    }, allow_redirects=True)

    if 'dashboard' in r.url or r.status_code == 200:
        print("Login successful")
        return True

    print(f"Login failed — ended up at: {r.url}")
    return False

def fetch_report(session):
    """POST to reports backend to request call history"""
    print(f"Fetching call history {FROM_STR} to {TO_STR}...")

    payload = {
        'report': 'call_history',
        'custom': False,
        'from_existing_report': False,
        'report_label': None,
        'filters': {
            'from_created_at': f'{FROM_STR} 00:00:00',
            'to_created_at':   f'{TO_STR} 23:59:59',
            'report_fields':   REPORT_FIELDS,
        }
    }

    r = session.post(
        f'{REPORTS_BASE}/api/reports/report_request',
        json=payload,
        headers={
            'Accept':          'application/json',
            'Content-Type':    'application/json',
            'Referer':         f'{REPORTS_BASE}/',
            'X-Requested-With': 'XMLHttpRequest',
        }
    )

    print(f"Report request status: {r.status_code}")
    print(f"Response preview: {r.text[:300]}")
    return r

def poll_for_csv(session, response_data):
    """If the API returns a job ID, poll until the CSV is ready"""
    import time

    job_id = (response_data.get('job_id') or
              response_data.get('request_id') or
              response_data.get('id'))

    if not job_id:
        return None

    print(f"Got job ID: {job_id} — polling for result...")

    for attempt in range(20):
        time.sleep(3)
        r = session.get(
            f'{REPORTS_BASE}/api/reports/report_request/{job_id}',
            headers={'Accept': 'application/json'}
        )
        data = r.json()
        status = data.get('status', '')
        print(f"  Poll {attempt+1}: status={status}")

        if status in ('complete', 'done', 'finished') or data.get('download_url') or data.get('file_url'):
            return data
        if data.get('rows') or data.get('data') or data.get('results'):
            return data

    print("Polling timed out")
    return None

def download_csv(session, url):
    """Download CSV from a URL"""
    r = session.get(url)
    r.raise_for_status()
    return r.text

def parse_csv_to_json(csv_text):
    """Parse CSV into list of dicts"""
    reader = csv.DictReader(io.StringIO(csv_text))
    return list(reader)

def normalize_rows(rows):
    """Normalize field names to what the dashboard expects"""
    normalized = []
    for r in rows:
        normalized.append({
            'Lead ID':           r.get('lead_id') or r.get('Lead ID') or '',
            'Call Date':         r.get('created_at') or r.get('Call Date') or '',
            'Call Type':         r.get('call_type') or r.get('Call Type') or '',
            'Agent':             r.get('user_name') or r.get('Agent') or '',
            'Call Duration':     r.get('Duration') or r.get('Call Duration') or '0',
            'From Number':       r.get('caller_id') or r.get('From Number') or '',
            'To Number':         r.get('to') or r.get('To Number') or '',
            'Call Campaign':     r.get('call_campaign') or r.get('Call Campaign') or '',
            'Full Name':         r.get('lead_name') or r.get('Full Name') or '',
        })
    return normalized

def filter_main_lines(rows):
    """Keep only calls to the 3 main lines"""
    def clean(n):
        return ''.join(c for c in str(n) if c.isdigit())

    filtered = [r for r in rows if clean(r.get('To Number', '')) in MAIN_LINES]
    print(f"Total rows: {len(rows)} → Main line rows: {len(filtered)}")
    return filtered

def save_output(rows):
    """Save to data/call_history.json"""
    os.makedirs('data', exist_ok=True)
    output = {
        'updated_at': datetime.now().isoformat(),
        'from_date':  FROM_STR,
        'to_date':    TO_STR,
        'total_rows': len(rows),
        'calls':      rows,
    }
    with open('data/call_history.json', 'w') as f:
        json.dump(output, f, indent=2)
    print(f"Saved {len(rows)} calls to data/call_history.json")

def main():
    if not RICOCHET_EMAIL or not RICOCHET_PASSWORD:
        print("ERROR: RICOCHET_EMAIL and RICOCHET_PASSWORD must be set as environment variables")
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })

    # Step 1: Login
    if not login(session):
        print("ERROR: Login failed")
        sys.exit(1)

    # Step 2: Request report
    r = fetch_report(session)

    rows = []

    if r.status_code == 200:
        try:
            data = r.json()

            # Check if it's an async job
            if data.get('job_id') or data.get('request_id') or data.get('id'):
                result = poll_for_csv(session, data)
                if result:
                    dl_url = result.get('download_url') or result.get('file_url') or result.get('url')
                    if dl_url:
                        csv_text = download_csv(session, dl_url)
                        rows = parse_csv_to_json(csv_text)
                    else:
                        rows = result.get('rows') or result.get('data') or result.get('results') or []
            else:
                # Synchronous response
                rows = (data if isinstance(data, list) else
                        data.get('data') or data.get('rows') or
                        data.get('results') or data.get('calls') or [])

        except Exception as e:
            # Maybe it returned CSV directly
            print(f"JSON parse failed ({e}), trying CSV...")
            try:
                rows = parse_csv_to_json(r.text)
            except Exception as e2:
                print(f"CSV parse also failed: {e2}")
                print(f"Raw response: {r.text[:500]}")
                sys.exit(1)

    elif r.status_code == 302 or 'login' in r.url:
        print("ERROR: Got redirected to login — session may have expired")
        sys.exit(1)
    else:
        print(f"ERROR: Unexpected status {r.status_code}: {r.text[:300]}")
        sys.exit(1)

    if not rows:
        print("WARNING: No rows returned — saving empty dataset")

    # Step 3: Normalize and filter
    rows = normalize_rows(rows)
    rows = filter_main_lines(rows)

    # Step 4: Save
    save_output(rows)
    print("Done!")

if __name__ == '__main__':
    main()
