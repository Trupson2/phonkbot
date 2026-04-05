"""
Suno Auto-Token — automatic session renewal via Playwright.

When the Clerk API-based keep-alive fails (session expired, 403),
this module opens suno.com in a headless Chromium browser that has
a saved Google/Discord OAuth session — the browser auto-re-logs
and we extract fresh cookies.

Setup (one-time, needs display):
    python3 auto_token_setup.py

Then it works fully automatically in the background.

Requires:
    pip install playwright
    playwright install chromium --with-deps
"""

import os
import time
import asyncio
import threading
from modules.logger import log, log_error, log_warning
from modules.database import get_config, set_config

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSER_STATE = os.path.join(_APP_DIR, 'data', 'browser_state')
SUNO_URL = 'https://suno.com'

# How often to do a full browser refresh (keeps OAuth session alive)
BROWSER_REFRESH_INTERVAL = 4 * 3600  # 4 hours

# Check if Playwright is installed
_pw_available = False
try:
    from playwright.async_api import async_playwright
    _pw_available = True
except ImportError:
    pass


def is_available():
    """Check if auto-token is set up (Playwright installed + browser state exists)."""
    return _pw_available and os.path.isdir(BROWSER_STATE)


def is_installed():
    """Check if Playwright is installed (even without browser state)."""
    return _pw_available


# ─────────────────────────────────────
# Core: browser-based session refresh
# ─────────────────────────────────────

async def _do_refresh(headless=True, login_timeout=300000):
    """
    Open suno.com in persistent Chromium to refresh all cookies.
    The browser profile stores the OAuth session (Google/Discord),
    so even if Clerk session expired, the browser re-authenticates via OAuth.

    Returns {'cookie': str, 'jwt': str|None} on success, None on failure.
    """
    if not _pw_available:
        return None

    os.makedirs(BROWSER_STATE, exist_ok=True)

    ctx = None
    pw = None
    try:
        pw = await async_playwright().start()
        ctx = await pw.chromium.launch_persistent_context(
            BROWSER_STATE,
            headless=headless,
            args=[
                '--no-sandbox',
                '--disable-gpu',
                '--disable-dev-shm-usage',
                '--disable-software-rasterizer',
                '--disable-blink-features=AutomationControlled',
            ],
            viewport={'width': 1280, 'height': 720},
            locale='en-US',
            ignore_https_errors=True,
        )

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Handle OAuth popups (Google/Discord open in new window)
        popup_done = asyncio.Event()

        def _on_page(new_page):
            """When OAuth popup opens, wait for it to close (login done)."""
            async def _wait_popup():
                try:
                    await new_page.wait_for_close(timeout=login_timeout)
                    popup_done.set()
                except Exception:
                    pass
            asyncio.ensure_future(_wait_popup())

        ctx.on('page', _on_page)

        # Navigate to suno.com
        await page.goto(SUNO_URL, wait_until='domcontentloaded', timeout=60000)
        await page.wait_for_timeout(3000)

        # Check if logged in by looking for Clerk auth cookies
        def _has_auth_cookies(cookies_list):
            names = {c['name'] for c in cookies_list}
            return '__client' in names or '__session' in names

        initial_cookies = await ctx.cookies('https://suno.com')
        clerk_cookies = await ctx.cookies('https://clerk.suno.com')
        logged_in = _has_auth_cookies(initial_cookies + clerk_cookies)

        if not logged_in and headless:
            # Wait for auto-login via saved OAuth session (Google/Discord)
            log("AutoToken: waiting for auto-login via saved OAuth session...")
            for i in range(20):  # wait up to 60 seconds
                await page.wait_for_timeout(3000)
                all_c = []
                for domain in ['https://suno.com', 'https://clerk.suno.com', 'https://auth.suno.com']:
                    try:
                        all_c.extend(await ctx.cookies(domain))
                    except Exception:
                        pass
                if _has_auth_cookies(all_c):
                    logged_in = True
                    log("AutoToken: auto-login successful!")
                    break

            if not logged_in:
                log_warning("AutoToken: not logged in — run: python3 auto_token_setup.py")
                await ctx.close()
                await pw.stop()
                return None

        if not logged_in and not headless:
            print()
            print("  Nie jestes zalogowany.")
            print("  Kliknij 'Sign In' i zaloguj sie przez Google/Discord.")
            print("  Czekam max 5 minut...")
            print()
            log("AutoToken: waiting for login (up to 5 min)...")

            # Poll every 3 seconds for auth cookies to appear
            try:
                max_checks = login_timeout // 3000
                for i in range(max_checks):
                    await page.wait_for_timeout(3000)

                    # Check all cookie domains
                    all_c = []
                    for domain in ['https://suno.com', 'https://clerk.suno.com', 'https://auth.suno.com']:
                        try:
                            all_c.extend(await ctx.cookies(domain))
                        except Exception:
                            pass

                    if _has_auth_cookies(all_c):
                        log("AutoToken: login detected! Waiting for session to settle...")
                        await page.wait_for_timeout(5000)
                        logged_in = True
                        break

                    # Also check if popup login happened
                    if popup_done.is_set():
                        log("AutoToken: OAuth popup closed, checking cookies...")
                        await page.wait_for_timeout(5000)
                        for domain in ['https://suno.com', 'https://clerk.suno.com', 'https://auth.suno.com']:
                            try:
                                all_c.extend(await ctx.cookies(domain))
                            except Exception:
                                pass
                        if _has_auth_cookies(all_c):
                            logged_in = True
                            break

                if not logged_in:
                    log_error("AutoToken: login timeout (5 min)")
                    await ctx.close()
                    await pw.stop()
                    return None

            except Exception as e:
                log_error(f"AutoToken: login wait error — {e}")
                await ctx.close()
                await pw.stop()
                return None

        # Visit /create to trigger full API auth flow
        try:
            await page.goto(f'{SUNO_URL}/create', wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception:
            pass

        # Extract cookies from all relevant domains
        all_cookies = []
        for domain in ['https://suno.com', 'https://clerk.suno.com', 'https://auth.suno.com']:
            try:
                all_cookies.extend(await ctx.cookies(domain))
            except Exception:
                pass

        # Deduplicate by name (prefer later entries = fresher)
        seen = {}
        for c in all_cookies:
            seen[c['name']] = c['value']

        cookie_str = '; '.join(f'{k}={v}' for k, v in seen.items())

        # Try to extract JWT from __session cookie
        jwt = seen.get('__session')

        await ctx.close()
        await pw.stop()

        if not cookie_str or len(cookie_str) < 50:
            log_error("AutoToken: cookies too short, likely not logged in")
            return None

        return {'cookie': cookie_str, 'jwt': jwt}

    except Exception as e:
        log_error(f"AutoToken: browser error — {e}")
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        return None


# ─────────────────────────────────────
# Public API
# ─────────────────────────────────────

def _ensure_xvfb():
    """Start a virtual X display if no real display is available."""
    if os.environ.get('DISPLAY'):
        return  # already have a display

    try:
        import subprocess
        # Check if Xvfb is installed
        result = subprocess.run(['which', 'Xvfb'], capture_output=True)
        if result.returncode != 0:
            return

        # Start Xvfb on display :99
        subprocess.Popen(
            ['Xvfb', ':99', '-screen', '0', '1280x720x24', '-nolisten', 'tcp'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        os.environ['DISPLAY'] = ':99'
        time.sleep(1)
        log("AutoToken: started Xvfb on :99")
    except Exception as e:
        log_error(f"AutoToken: Xvfb setup failed — {e}")


def refresh():
    """
    Try to auto-refresh Suno session via headless browser.
    Returns True on success, False on failure.
    """
    if not _pw_available:
        log_warning("AutoToken: playwright not installed (pip install playwright)")
        return False

    if not os.path.isdir(BROWSER_STATE):
        log_warning("AutoToken: no browser state — run: python3 auto_token_setup.py")
        return False

    # Ensure virtual display on headless systems (Raspberry Pi)
    _ensure_xvfb()

    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_do_refresh(headless=True))
        loop.close()

        if result:
            set_config('suno_cookie', result['cookie'])
            if result.get('jwt'):
                set_config('suno_jwt', result['jwt'])
            log(f"AutoToken: session refreshed OK ({len(result['cookie'])} chars)")
            return True

        return False

    except Exception as e:
        log_error(f"AutoToken: refresh error — {e}")
        return False


def setup():
    """
    Interactive setup: opens visible browser for first-time Suno login.
    Must run on machine with display (VNC, SSH -X, or direct).
    Returns True on success.
    """
    if not _pw_available:
        print("ERROR: Playwright not installed!")
        print("Run:")
        print("  pip install playwright")
        print("  playwright install chromium --with-deps")
        return False

    print("Opening browser... Log in to suno.com, then wait.")
    print()

    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_do_refresh(headless=False, login_timeout=300000))
        loop.close()

        if result:
            set_config('suno_cookie', result['cookie'])
            if result.get('jwt'):
                set_config('suno_jwt', result['jwt'])
            print(f"\nSetup OK! Cookie saved ({len(result['cookie'])} chars)")
            return True
        else:
            print("\nSetup failed — no cookies captured.")
            return False

    except Exception as e:
        print(f"\nSetup error: {e}")
        return False


# ─────────────────────────────────────
# Background auto-refresh thread
# ─────────────────────────────────────

_refresh_thread = None


def _auto_refresh_loop():
    """Background: refresh browser session every 4 hours to keep OAuth alive."""
    log(f"AutoToken: background loop started (every {BROWSER_REFRESH_INTERVAL // 3600}h)")

    # Refresh immediately on startup
    try:
        ok = refresh()
        if ok:
            log("AutoToken: initial refresh OK")
        else:
            log_warning("AutoToken: initial refresh failed")
    except Exception as e:
        log_error(f"AutoToken: initial refresh error — {e}")

    while True:
        time.sleep(BROWSER_REFRESH_INTERVAL)

        try:
            ok = refresh()
            if ok:
                log("AutoToken: periodic refresh OK")
            else:
                log_warning("AutoToken: periodic refresh failed (will retry next cycle)")
        except Exception as e:
            log_error(f"AutoToken: loop error — {e}")
            time.sleep(300)


def start_auto_refresh():
    """Start background auto-refresh. Call once at app startup."""
    global _refresh_thread

    if not is_available():
        log("AutoToken: not available (playwright missing or no browser state)")
        return None

    _refresh_thread = threading.Thread(target=_auto_refresh_loop, daemon=True)
    _refresh_thread.start()
    log("AutoToken: background thread started")
    return _refresh_thread
