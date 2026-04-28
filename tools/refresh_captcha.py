#!/usr/bin/env python3
"""Refresh Suno captcha + JWT via Playwright, then optionally push to a Pi.

Replaces the old triplet of `solve_captcha.py`, `auto_captcha.py`,
`import_cookies.py` and `auto_token_setup.py` — one entry point, three
modes via flags:

    python tools/refresh_captcha.py                 # one-shot, headed (you click Create)
    python tools/refresh_captcha.py --loop          # run forever, refresh hourly
    python tools/refresh_captcha.py --push-to PI    # push tokens to remote phonkbot via HTTP

The `--push-to` value is a base URL like `http://192.168.100.200:5001`.
The remote phonkbot must be reachable on LAN and have `admin_push_secret`
set to the same value as your local DB (it is — both come from the same
git history of database.py defaults).
"""

import argparse
import os
import sys
import time

# Make `modules` importable when run from project root or tools/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_once(headed=True):
    from modules.suno.captcha import solve, solve_headless
    if headed:
        return solve()
    return solve_headless()


def push_to_remote(base_url):
    import requests
    from modules.database import get_config

    secret = get_config('admin_push_secret', '')
    if not secret:
        print("ERROR: admin_push_secret missing locally — run app.py once to seed it")
        return False

    payload = {
        k: get_config(k, '')
        for k in (
            'suno_jwt', 'suno_refresh_token', 'suno_session_id',
            'suno_cookie', 'suno_browser_token',
            'suno_captcha_token', 'suno_captcha_token_time',
        )
    }
    payload = {k: v for k, v in payload.items() if v}

    if not payload:
        print("ERROR: no tokens in local DB to push")
        return False

    url = base_url.rstrip('/') + '/api/admin/push-tokens'
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={'X-Auth-Secret': secret},
            timeout=10,
        )
        print(f"Push -> {url}: HTTP {resp.status_code}")
        print(f"Response: {resp.text[:300]}")
        return resp.status_code == 200
    except Exception as e:
        print(f"Push error: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--loop', action='store_true',
                    help='Refresh hourly forever (use with cron / Task Scheduler instead if possible)')
    ap.add_argument('--headless', action='store_true',
                    help='Headless mode (only works if Gemini Vision can solve the captcha alone)')
    ap.add_argument('--push-to', metavar='URL',
                    help='After capturing tokens, push them to a remote phonkbot via HTTP')
    ap.add_argument('--interval', type=int, default=3600,
                    help='Loop interval seconds (default 3600)')
    args = ap.parse_args()

    def _cycle():
        token = run_once(headed=not args.headless)
        ok = bool(token)
        if ok and args.push_to:
            push_to_remote(args.push_to)
        return ok

    if not args.loop:
        ok = _cycle()
        sys.exit(0 if ok else 1)

    while True:
        try:
            _cycle()
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            print(f"Cycle error: {e}")
        print(f"Sleeping {args.interval}s before next refresh...")
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
