#!/usr/bin/env python3
"""Push current Suno tokens from local DB to a remote phonkbot via HTTP.

For when you've already refreshed tokens locally (e.g. via the Web UI or
`refresh_captcha.py` without `--push-to`) and just need to ship them.

    python tools/push_tokens.py http://192.168.100.200:5001
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.refresh_captcha import push_to_remote  # reuse


def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/push_tokens.py <remote_base_url>")
        print("Example: python tools/push_tokens.py http://192.168.100.200:5001")
        sys.exit(2)
    sys.exit(0 if push_to_remote(sys.argv[1]) else 1)


if __name__ == '__main__':
    main()
