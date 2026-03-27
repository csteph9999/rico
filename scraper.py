"""
Stephenson Agency — Ricochet360 Call History Scraper v2
Logs into reports.ricochet.me directly to get the session cookie accepted
"""

import requests
import json
import csv
import io
import os
import sys
import re
import time
from datetime import datetime, timedelta

RICOCHET_EMAIL    = os.environ.get('RICOCHET_EMAIL', '')
RICOCHET_PASSWORD = os.environ.get('RICOCHET_PASSWORD', '')
RICOCHET_DOMAIN   = 'stephenson.ricochet.me'
REPORTS_BASE      = 'https://reports.ricochet.me/stephenson'
MAIN_LINES        = {'15094081190', '15095162250', '15095353171'}

today   = datetime.now()
from_dt = today - timedelta(days=60)
FROM_STR = from_dt.strftime('%Y-%m-%d')
TO_STR   = today.strftime('%Y-%m-%d')

REPORT_FIELDS = [
    'lead_id', 'created_at', 'call_type', 'user_name',
    'Duration', 'caller_id', 'to', 'call_campaign', 'lead_name'
]

def get_csrf(session, url):
    r = session.get(url)
    r.raise_for_status()
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    return match.group(1) if match else ''

def login_main(session):
    """Log into the main Ricochet360 site"""
    print(f"Logging into main site as {RICOCHET_EMAIL}...")
    csrf = get_csrf(session, f'https://{RICOCHET_DOMAIN}/login')
    r = session.post(f'https://{RICOCHET_DOMAIN}/login', data={
        '_token': csrf,
        'email': RICOCHET_EMAIL,
        'password': RICOCHET_PASSWORD,
    }, headers={'Referer': f'https://{RICOCHET_DOMAIN}/login'}, allow_redirects=True)
    success = 'dashboard' in r.url or r.status_code == 200 and 'login' not in r.url
    print(f"Main login: {'OK' if success else 'FAILED'} — at {r.url}")
    return success

def login_reports(session):
    """Log into the reports subdomain — tries multiple approaches"""
    print("Logging into reports subdomain...")

    # Approach 1: reports subdomain may have its own login page
    try:
        r = session.get(f'{REPORTS_BASE}/login', allow_redirects=True)
        print(f"Reports login page status: {r.status_code} url: {r.url}")
        
        if 'login' in r.url or 'login' in r.text.lower():
            csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
            csrf_token = csrf.group(1) if csrf else ''
            
            r2 = session.post(f'{REPORTS_BASE}/login', data={
                '_token': csrf_token,
                'email': RICOCHET_EMAIL,
                'password': RICOCHET_PASSWORD,
            }, headers={'Referer': f'{REPORTS_BASE}/login'}, allow_redirects=True)
            print(f"Reports login post: {r2.status_code} url: {r2.url}")
    except Exception as e:
        print(f"Reports login approach 1 failed: {e}")

    # Approach 2: hit the reports index to trigger auth via shared session
    try:
        r = session.get(f'{REPORTS_BASE}/', allow_redirects=True)
        print(f"Reports index: {r.status_code} url: {r.url}")
    except Exception as e:
        print(f"Reports index failed: {e}")

    # Approach 3: try SSO endpoint that syncs the session across subdomains
    for sso_path in ['/auth/sso', '/sso', '/api/auth', '/api/login']:
        try:
            r = session.post(f'https://{RICOCHET_DOMAIN}{sso_path}', json={
                'email': RICOCHET_EMAIL, 'password': RICOCHET_PASSWORD
            }, headers={'Accept': 'application/json'})
            print(f"SSO {sso_path}: {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                token = data.get('token') or data.get('access_token') or data.get('auth_token')
                if token:
                    print(f"Got SSO token: {token[:20]}...")
                    session.headers.update({'Authorization': f'Bearer {token}'})
                    return token
        except Exception as e:
            continue

    # Approach 4: get API token from authenticated session
    try:
        r = session.get(f'https://{RICOCHET_DOMAIN}/api/v4/me', 
                       headers={'Accept': 'application/json'})
        print(f"API /me: {r.status_code} — {r.text[:200]}")
        if r.ok:
            data = r.json()
            token = data.get('api_token') or data.get('token') or data.get('auth_token')
            if token:
                print(f"Got user API token: {token[:20]}...")
                return token
    except Exception as e:
        print(f"/me failed: {e}")

    return None

def fetch_report(session, token=None):
    """POST report request to reports subdomain"""
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

    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Referer': f'{REPORTS_BASE}/',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://reports.ricochet.me',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'

    # Try with session (cookie auth)
    r = session.post(f'{REPORTS_BASE}/api/reports/report_request',
                     json=payload, headers=headers)
    print(f"Report request: {r.status_code} — {r.text[:300]}")
    return r

def handle_response(session, r):
    """Parse response — handle sync data, async job, or CSV download"""
    if not r.ok:
        return None

    # Check content type — might be CSV directly
    ct = r.headers.get('Content-Type', '')
    if 'text/csv' in ct or 'application/csv' in ct:
        print("Got CSV directly")
        return parse_csv(r.text)

    try:
        data = r.json()
    except Exception:
        # Try as CSV
        try:
            return parse_csv(r.text)
        except Exception as e:
            print(f"Could not parse response: {e}")
            print(f"Raw: {r.text[:300]}")
            return None

    print(f"Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")

    # Async job
    job_id = (data.get('job_id') or data.get('request_id') or
              data.get('id') if isinstance(data, dict) else None)
    if job_id and not data.get('data') and not data.get('rows'):
        print(f"Async job: {job_id} — polling...")
        return poll_job(session, job_id)

    # Direct data
    if isinstance(data, list):
        return data
    for key in ('data', 'rows', 'results', 'calls', 'call_history', 'records'):
        if key in data and isinstance(data[key], list):
            return data[key]

    # Download URL
    dl = data.get('download_url') or data.get('file_url') or data.get('url')
    if dl:
        print(f"Download URL: {dl}")
        r2 = session.get(dl)
        return parse_csv(r2.text) if r2.ok else None

    print(f"Unknown response structure: {json.dumps(data)[:400]}")
    return None

def poll_job(session, job_id, max_attempts=15):
    for i in range(max_attempts):
        time.sleep(3)
        try:
            r = session.get(f'{REPORTS_BASE}/api/reports/report_request/{job_id}',
                           headers={'Accept': 'application/json'})
            data = r.json()
            status = data.get('status', '')
            print(f"  Poll {i+1}: status={status} keys={list(data.keys())}")

            dl = data.get('download_url') or data.get('file_url') or data.get('url')
            if dl:
                r2 = session.get(dl)
                return parse_csv(r2.text) if r2.ok else None

            for key in ('rows', 'data', 'results', 'calls'):
                if key in data and isinstance(data[key], list):
                    return data[key]

            if status in ('complete', 'done', 'finished', 'completed'):
                return data.get('rows') or data.get('data') or []
        except Exception as e:
            print(f"  Poll {i+1} error: {e}")
    print("Polling timed out")
    return None

def parse_csv(text):
    try:
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        print(f"Parsed {len(rows)} CSV rows")
        return rows
    except Exception as e:
        print(f"CSV parse error: {e}")
        return None

def normalize(rows):
    out = []
    for r in rows:
        out.append({
            'Lead ID':       r.get('lead_id')      or r.get('Lead ID')       or '',
            'Call Date':     r.get('created_at')   or r.get('Call Date')     or '',
            'Call Type':     r.get('call_type')    or r.get('Call Type')     or '',
            'Agent':         r.get('user_name')    or r.get('Agent')         or '',
            'Call Duration': r.get('Duration')     or r.get('Call Duration') or '0',
            'From Number':   r.get('caller_id')    or r.get('From Number')   or '',
            'To Number':     r.get('to')           or r.get('To Number')     or '',
            'Call Campaign': r.get('call_campaign')or r.get('Call Campaign') or '',
            'Full Name':     r.get('lead_name')    or r.get('Full Name')     or '',
        })
    return out

def filter_main_lines(rows):
    clean = lambda n: ''.join(c for c in str(n) if c.isdigit())
    filtered = [r for r in rows if clean(r.get('To Number','')) in MAIN_LINES]
    print(f"Total: {len(rows)} → Main line: {len(filtered)}")
    return filtered

def save(rows):
    os.makedirs('data', exist_ok=True)
    out = {
        'updated_at': datetime.now().isoformat(),
        'from_date':  FROM_STR,
        'to_date':    TO_STR,
        'total_rows': len(rows),
        'calls':      rows,
    }
    with open('data/call_history.json', 'w') as f:
        json.dump(out, f, indent=2)
    print(f"Saved {len(rows)} calls to data/call_history.json")

def main():
    if not RICOCHET_EMAIL or not RICOCHET_PASSWORD:
        print("ERROR: Set RICOCHET_EMAIL and RICOCHET_PASSWORD as GitHub Secrets")
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36'
    })

    # Step 1: Login to main site
    if not login_main(session):
        print("ERROR: Main site login failed")
        sys.exit(1)

    # Step 2: Authenticate with reports subdomain
    token = login_reports(session)

    # Step 3: Fetch report
    r = fetch_report(session, token)

    if not r.ok:
        print(f"ERROR: Report request failed with {r.status_code}: {r.text[:300]}")
        sys.exit(1)

    # Step 4: Parse response
    rows = handle_response(session, r)
    if rows is None:
        print("ERROR: Could not extract rows from response")
        sys.exit(1)

    # Step 5: Normalize, filter, save
    rows = normalize(rows)
    rows = filter_main_lines(rows)
    save(rows)
    print("Done!")

if __name__ == '__main__':
    main()
