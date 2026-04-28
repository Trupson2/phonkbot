"""Suno authentication — cascade JWT refresh.

Three strategies in order of preference (cheapest -> most expensive):
  1. refresh_token  — POST __client cookie value to Clerk; no browser needed
  2. cookie+Clerk   — extract session ID from cookie, exchange for fresh JWT
  3. autotoken      — Playwright headless re-login (last resort)

Replaces the old `suno_cookie.py` + auth helpers in `suno_api.py`. Single
keep-alive thread, single source of truth for JWT in DB.
"""

import base64
import json
import threading
import time
from http.cookies import SimpleCookie

import requests

from modules.database import get_config, set_config
from modules.logger import log, log_error, log_warning

CLERK_BASE = 'https://auth.suno.com'
REFRESH_INTERVAL = 300        # 5 minutes
MAX_FAILURES = 3
ALERT_COOLDOWN = 86400        # 24h between Telegram alerts

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}
_API_PARAMS = {'__clerk_api_version': '2025-11-10'}


# ─────────────────────────────────────────────────────────
# JWT helpers
# ─────────────────────────────────────────────────────────

def jwt_is_valid(jwt_token, skew_seconds=30):
    """True if JWT not expired (with safety margin)."""
    if not jwt_token:
        return False
    try:
        parts = jwt_token.split('.')
        if len(parts) < 2:
            return False
        payload = parts[1] + '=' * (4 - len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return time.time() < data.get('exp', 0) - skew_seconds
    except Exception:
        return False


def get_valid_jwt():
    """Return a fresh JWT, refreshing if needed. None if all strategies fail."""
    saved = get_config('suno_jwt', '')
    if jwt_is_valid(saved):
        return saved

    for strategy in (_refresh_via_refresh_token, _refresh_via_cookie, _refresh_via_autotoken):
        jwt = strategy()
        if jwt:
            set_config('suno_jwt', jwt)
            return jwt

    return None


# ─────────────────────────────────────────────────────────
# Strategy 1: refresh token (__client cookie value)
# ─────────────────────────────────────────────────────────

def _refresh_via_refresh_token():
    """Use stored __client cookie value to fetch a fresh JWT from Clerk."""
    refresh_token = get_config('suno_refresh_token', '')
    if not refresh_token:
        return None

    cookie_header = f'__client={refresh_token}'

    try:
        # Step 1: get active session
        resp = requests.get(
            f'{CLERK_BASE}/v1/client',
            params=_API_PARAMS,
            headers={**_HEADERS, 'Cookie': cookie_header},
            timeout=10,
        )
        if resp.status_code != 200:
            log_warning(f"Suno auth: refresh_token /v1/client returned {resp.status_code}")
            return None

        _store_rotated_client_cookie(resp)

        data = resp.json().get('response', {})
        session_id = _pick_session_id(data)
        if not session_id:
            log_warning("Suno auth: no active session via refresh token")
            return None

        # Step 2: exchange for JWT
        resp2 = requests.post(
            f'{CLERK_BASE}/v1/client/sessions/{session_id}/tokens',
            params=_API_PARAMS,
            headers={
                **_HEADERS,
                'Cookie': cookie_header,
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            timeout=15,
        )
        if resp2.status_code != 200:
            log_warning(f"Suno auth: tokens endpoint returned {resp2.status_code}")
            return None

        _store_rotated_client_cookie(resp2)

        body = resp2.json()
        jwt = body.get('jwt') or body.get('response', {}).get('jwt', '')
        if jwt:
            return jwt

        log_warning("Suno auth: empty JWT in tokens response")
    except Exception as e:
        log_error(f"Suno auth: refresh_token error — {e}")

    return None


# ─────────────────────────────────────────────────────────
# Strategy 2: cookie + Clerk (extract session, exchange for JWT)
# ─────────────────────────────────────────────────────────

def _refresh_via_cookie():
    """Use full suno_cookie to fetch session ID then exchange for JWT."""
    cookie = get_config('suno_cookie', '')
    if not cookie:
        return None

    session_id = get_config('suno_session_id', '') or extract_session_id(cookie)
    if session_id:
        set_config('suno_session_id', session_id)
    else:
        return None

    try:
        resp = requests.post(
            f'{CLERK_BASE}/v1/client/sessions/{session_id}/tokens',
            params=_API_PARAMS,
            headers={
                **_HEADERS,
                'Cookie': cookie,
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            timeout=15,
        )
        if resp.status_code == 401:
            log_warning("Suno auth: cookie expired (401)")
            set_config('suno_session_id', '')
            return None
        if resp.status_code != 200:
            log_warning(f"Suno auth: cookie tokens returned {resp.status_code}")
            return None

        # Update __client cookie if rotated
        _store_rotated_client_cookie(resp)

        data = resp.json()
        return data.get('jwt') or data.get('response', {}).get('jwt', '')
    except Exception as e:
        log_error(f"Suno auth: cookie refresh error — {e}")
        return None


def extract_session_id(cookie_str):
    """Pull active session ID from Clerk by hitting /v1/client with the cookie."""
    if not cookie_str:
        return None
    try:
        resp = requests.get(
            f'{CLERK_BASE}/v1/client',
            params=_API_PARAMS,
            headers={**_HEADERS, 'Cookie': cookie_str},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        return _pick_session_id(resp.json().get('response', {}))
    except Exception as e:
        log_error(f"Suno auth: extract_session_id error — {e}")
        return None


def _pick_session_id(client_payload):
    """From a Clerk /v1/client response, return active or last-active session ID."""
    for s in client_payload.get('sessions', []):
        if s.get('status') == 'active':
            return s.get('id')
    return client_payload.get('last_active_session_id')


# ─────────────────────────────────────────────────────────
# Strategy 3: Playwright autotoken
# ─────────────────────────────────────────────────────────

def _refresh_via_autotoken():
    """Last resort: open headless Chromium, re-auth via OAuth, harvest cookies."""
    try:
        from modules.suno.autotoken import is_available, refresh
    except ImportError:
        return None

    if not is_available():
        return None

    log("Suno auth: trying autotoken (Playwright)...")
    if not refresh():
        log_warning("Suno auth: autotoken refresh failed")
        return None

    jwt = get_config('suno_jwt', '')
    return jwt if jwt_is_valid(jwt) else None


# ─────────────────────────────────────────────────────────
# Cookie rotation
# ─────────────────────────────────────────────────────────

def _store_rotated_client_cookie(resp):
    """If response sets a new __client cookie, save it as suno_refresh_token."""
    set_cookie = resp.headers.get('Set-Cookie', '')
    if not set_cookie or '__client=' not in set_cookie:
        return

    # Set-Cookie can have multiple comma-separated cookies; we look for __client
    for raw in set_cookie.split(','):
        for segment in raw.split(';'):
            segment = segment.strip()
            if segment.startswith('__client='):
                token = segment[len('__client='):]
                if token and len(token) > 100:
                    set_config('suno_refresh_token', token)
                return


# ─────────────────────────────────────────────────────────
# Keep-alive thread
# ─────────────────────────────────────────────────────────

_keep_alive_thread = None
_last_alert_time = 0


def _keep_alive_loop():
    """Refresh JWT every REFRESH_INTERVAL seconds via cascade."""
    failures = 0
    log("Suno auth: keep-alive loop started (cascade: refresh_token -> cookie -> autotoken)")

    while True:
        time.sleep(REFRESH_INTERVAL)
        try:
            jwt = get_valid_jwt()
            if jwt:
                failures = 0
                # Log occasionally to avoid spam (~once/h)
                if int(time.time()) % 3600 < REFRESH_INTERVAL:
                    log("Suno auth: JWT refreshed via cascade")
            else:
                failures += 1
                log_warning(f"Suno auth: refresh failed ({failures}/{MAX_FAILURES})")
                if failures >= MAX_FAILURES:
                    _alert_expired()
                    failures = 0
                    time.sleep(REFRESH_INTERVAL * 6)  # back off
        except Exception as e:
            log_error(f"Suno auth: keep-alive error — {e}")
            time.sleep(60)


def _alert_expired():
    """Send Telegram alert at most once per ALERT_COOLDOWN."""
    global _last_alert_time
    now = time.time()
    if now - _last_alert_time < ALERT_COOLDOWN:
        return
    _last_alert_time = now
    try:
        from modules.telegram import send_message
        send_message(
            "Suno auth: wszystkie metody odswiezenia JWT zfailowaly.\n"
            "Odpal na Windows: python tools/refresh_captcha.py\n"
            "(zlapie nowy token i pchnie do Pi przez HTTP)"
        )
    except Exception:
        pass


def start_keep_alive():
    """Launch the background JWT refresher. Safe to call multiple times."""
    global _keep_alive_thread
    if _keep_alive_thread and _keep_alive_thread.is_alive():
        return _keep_alive_thread

    has_creds = bool(
        get_config('suno_refresh_token', '') or
        get_config('suno_cookie', '')
    )
    if not has_creds:
        log("Suno auth: no credentials, keep-alive not started")
        return None

    _keep_alive_thread = threading.Thread(target=_keep_alive_loop, daemon=True)
    _keep_alive_thread.start()
    log("Suno auth: keep-alive started (every 5min)")
    return _keep_alive_thread
