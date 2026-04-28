#!/usr/bin/env python3
"""One-time YouTube OAuth wizard.

Runs the same flow as the /webhooks/youtube/auth route but from a terminal —
useful when the bot lives on a headless Pi: do the OAuth on your desktop,
then push the resulting token via tools/push_tokens.py (or paste it into
Settings on the Pi).

    python tools/setup_youtube.py
"""

import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from modules.database import init_db, get_config
    from modules.youtube import get_oauth_url, exchange_code

    init_db()

    if not get_config('youtube_client_id') or not get_config('youtube_client_secret'):
        print("Set youtube_client_id + youtube_client_secret in Settings first.")
        sys.exit(1)

    url = get_oauth_url()
    if not url:
        print("Failed to build OAuth URL — check client credentials.")
        sys.exit(1)

    print()
    print("Opening browser for YouTube OAuth...")
    print(f"If the browser does not open, visit:\n  {url}")
    print()
    try:
        webbrowser.open(url)
    except Exception:
        pass

    code = input("Paste the `code` query param from the redirect URL: ").strip()
    if not code:
        print("No code provided.")
        sys.exit(1)

    if exchange_code(code):
        print("OK — YouTube tokens saved to config.")
        print("To deploy to a remote phonkbot:")
        print("  python tools/push_tokens.py http://<pi-ip>:5001")
    else:
        print("Token exchange failed. Check logs.")
        sys.exit(1)


if __name__ == '__main__':
    main()
