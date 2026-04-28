"""
Suno hCaptcha Solver — uses Gemini Vision to solve captcha challenges.

Flow:
1. Playwright opens suno.com/create
2. Triggers generation → captcha appears
3. Screenshots captcha → Gemini Vision analyzes
4. Clicks correct images based on Gemini response
5. Intercepts captcha token from the generate request
6. Saves token to DB for PhonkBot API calls

Run on Windows: python solve_captcha.py
Token valid ~20min-2hrs, reused for multiple generations.
"""

import os
import re
import json
import time
import asyncio
from modules.logger import log, log_error, log_warning
from modules.database import get_config, set_config

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSER_STATE = os.path.join(_APP_DIR, 'data', 'browser_state')
SUNO_CREATE_URL = 'https://suno.com/create'

# Token expiry (20 min safety margin from ~2hr validity)
TOKEN_VALIDITY_SECONDS = 20 * 60


def get_captcha_token():
    """Get stored captcha token if still valid."""
    token = get_config('suno_captcha_token', '')
    token_time = get_config('suno_captcha_token_time', '0')

    if token and token_time:
        try:
            elapsed = time.time() - float(token_time)
            if elapsed < TOKEN_VALIDITY_SECONDS:
                return token
        except (ValueError, TypeError):
            pass

    return None


def save_captcha_token(token):
    """Save captcha token with timestamp."""
    set_config('suno_captcha_token', token)
    set_config('suno_captcha_token_time', str(time.time()))
    log(f"Captcha: token saved ({len(token)} chars, valid ~20min)")


async def _solve_captcha(headless=False):
    """
    Main captcha solving flow:
    1. Open suno.com/create in Playwright
    2. Trigger generation to provoke captcha
    3. Solve with Gemini Vision
    4. Intercept token from the resulting request
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log_error("Captcha: playwright not installed")
        return None

    gemini_key = get_config('gemini_api_key')
    if not gemini_key:
        log_error("Captcha: Gemini API key not configured")
        return None

    os.makedirs(BROWSER_STATE, exist_ok=True)

    captured_token = {'value': None}

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            BROWSER_STATE,
            headless=headless,
            args=[
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
            ],
            viewport={'width': 1280, 'height': 800},
            locale='en-US',
        )

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Intercept generate requests to capture token + headers
        async def _intercept_request(route):
            request = route.request
            if '/api/generate/' in request.url and request.method == 'POST':
                try:
                    body = request.post_data
                    headers = request.headers

                    print(f"\n[INTERCEPTED] {request.method} {request.url}")

                    if body:
                        data = json.loads(body)
                        token = data.get('token')

                        # Save real token, never overwrite with null
                        if token and token != 'null':
                            captured_token['value'] = token
                            print(f"  Token: {token[:50]}...")
                        elif not captured_token['value']:
                            print("  Token: null (no captcha token in this request)")

                        # Save the Authorization header for our API use
                        auth = headers.get('authorization', '')
                        if auth:
                            jwt = auth.replace('Bearer ', '')
                            set_config('suno_jwt', jwt)
                            print(f"  JWT saved: {jwt[:50]}...")

                        # Save browser-token if present
                        browser_token = headers.get('browser-token', '')
                        if browser_token:
                            set_config('suno_browser_token', browser_token)
                            print(f"  Browser-Token saved!")

                except Exception as e:
                    print(f"  Parse error: {e}")

                # Let the request through (don't abort — let browser generate)
                await route.continue_()
            else:
                await route.continue_()

        await page.route('**/*', _intercept_request)

        # Navigate to suno.com/create
        print("Opening suno.com/create...")
        await page.goto(SUNO_CREATE_URL, wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(5000)

        # Check if logged in
        if '/sign-in' in page.url or '/login' in page.url:
            print("Not logged in! Run: python import_cookies.py")
            await ctx.close()
            return None

        print("Logged in! Extracting tokens...")

        # Method 1: Extract Browser-Token from page JavaScript
        try:
            browser_token = await page.evaluate('''() => {
                // Clerk stores the session token in window.__clerk
                // Try various approaches to get auth tokens
                const cookies = document.cookie;
                const session = cookies.match(/__session=([^;]+)/);
                return session ? session[1] : null;
            }''')
            if browser_token:
                print(f"  __session cookie: {browser_token[:50]}...")
                set_config('suno_jwt', browser_token)
        except Exception:
            pass

        # Method 2: Extract all cookies and build auth
        all_cookies = await ctx.cookies('https://suno.com')
        clerk_cookies = await ctx.cookies('https://auth.suno.com')
        all_cookies.extend(clerk_cookies)

        cookie_dict = {c['name']: c['value'] for c in all_cookies}

        # Save __client as refresh token
        if '__client' in cookie_dict:
            set_config('suno_refresh_token', cookie_dict['__client'])
            print(f"  Refresh token updated: {cookie_dict['__client'][:50]}...")

        # Save __session as JWT
        if '__session' in cookie_dict:
            set_config('suno_jwt', cookie_dict['__session'])
            print(f"  JWT updated: {cookie_dict['__session'][:50]}...")

        # Method 3: Make a fetch request from the page to get Browser-Token
        print("Fetching Browser-Token via API call...")
        try:
            result = await page.evaluate('''async () => {
                try {
                    const resp = await fetch('https://studio-api-prod.suno.com/api/c/check', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({"ctype": "generation"}),
                        credentials: 'include',
                    });
                    // Get the request headers that were sent (browser adds auth automatically)
                    return {status: resp.status, ok: resp.ok};
                } catch(e) {
                    return {error: e.message};
                }
            }''')
            print(f"  API check: {result}")
        except Exception as e:
            print(f"  API check failed: {e}")

        # Method 4: Try triggering Create via JavaScript to capture request
        if not headless:
            print("\nIn the browser window, click Create to generate a track.")
            print("The token will be intercepted automatically.")
            print("Waiting up to 2 minutes...")

            for _ in range(24):
                await page.wait_for_timeout(5000)
                if captured_token['value']:
                    break
        else:
            # Headless: try clicking Create button
            print("Trying to click Create...")
            try:
                # Use JavaScript to find and click
                clicked = await page.evaluate('''() => {
                    const buttons = document.querySelectorAll('button');
                    for (const btn of buttons) {
                        const text = btn.textContent.toLowerCase().trim();
                        if (text.includes('create') || text.includes('generate')) {
                            btn.click();
                            return text;
                        }
                    }
                    return null;
                }''')
                if clicked:
                    print(f"  Clicked button: '{clicked}'")
                    await page.wait_for_timeout(10000)
                else:
                    print("  No Create button found via JS")
            except Exception as e:
                print(f"  Click failed: {e}")

        # Check results
        if captured_token['value']:
            save_captcha_token(captured_token['value'])
            print(f"\nToken captured! ({len(captured_token['value'])} chars)")

        # Even without captcha token, we got JWT + refresh token
        jwt = get_config('suno_jwt', '')
        rt = get_config('suno_refresh_token', '')

        if jwt or rt:
            print(f"\nTokens refreshed:")
            if jwt:
                print(f"  JWT: {jwt[:50]}...")
            if rt:
                print(f"  Refresh token: {rt[:50]}...")
            await ctx.close()
            return captured_token['value'] or '__TOKENS_REFRESHED__'

        print("\nNo tokens captured.")
        await ctx.close()
        return None


async def _ask_gemini(screenshot_path, api_key):
    """
    Send captcha screenshot to Gemini Vision for analysis.
    Returns a solution dict with click coordinates or grid positions.
    """
    try:
        from google import genai
        from google.genai import types
        import base64

        client = genai.Client(api_key=api_key)

        # Read screenshot
        with open(screenshot_path, 'rb') as f:
            image_data = f.read()

        prompt = """You are solving an hCaptcha challenge. Look at this screenshot carefully.

1. First, identify the CAPTCHA challenge prompt/question (e.g., "Click on all images containing a motorcycle")
2. Then identify which images in the grid match the criteria.
3. Report the matching images by their grid position.

The grid is typically 3x3 (9 images). Positions are:
Row 1: top-left, top-center, top-right
Row 2: middle-left, middle-center, middle-right
Row 3: bottom-left, bottom-center, bottom-right

If it's a 4x4 grid, use: row1-col1, row1-col2, etc.

Respond ONLY with a JSON object like:
{"challenge": "description of what to find", "matches": ["top-left", "middle-center", "bottom-right"], "grid_size": "3x3"}

If you see a different type of captcha (puzzle, click on object, drag), respond with:
{"challenge": "description", "type": "click", "x_percent": 50, "y_percent": 50}

If you cannot see a captcha or cannot solve it, respond with:
{"error": "description of what you see"}"""

        response = client.models.generate_content(
            model=get_config('gemini_model', 'gemini-2.0-flash'),
            contents=[
                types.Part.from_bytes(data=image_data, mime_type='image/png'),
                prompt,
            ],
        )

        text = response.text.strip()
        # Clean markdown
        if text.startswith('```'):
            text = text.split('\n', 1)[1] if '\n' in text else text[3:]
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()
        if text.startswith('json'):
            text = text[4:].strip()

        solution = json.loads(text)
        print(f"Gemini analysis: {json.dumps(solution, indent=2)}")

        if 'error' in solution:
            log_warning(f"Captcha: Gemini error — {solution['error']}")
            return None

        return solution

    except json.JSONDecodeError:
        log_error(f"Captcha: Gemini returned non-JSON: {text[:200]}")
        return None
    except Exception as e:
        log_error(f"Captcha: Gemini error — {e}")
        return None


async def _execute_solution(page, captcha_frame, solution):
    """Execute clicks based on Gemini's solution."""
    try:
        target = captcha_frame or page

        if solution.get('type') == 'click':
            # Single click solution (object detection, puzzle)
            x_pct = solution.get('x_percent', 50)
            y_pct = solution.get('y_percent', 50)

            viewport = page.viewport_size
            x = int(viewport['width'] * x_pct / 100)
            y = int(viewport['height'] * y_pct / 100)

            print(f"Clicking at ({x}, {y})...")
            await page.mouse.click(x, y)
            return

        # Grid-based solution
        matches = solution.get('matches', [])
        grid_size = solution.get('grid_size', '3x3')

        if not matches:
            print("No matches found by Gemini")
            return

        rows, cols = 3, 3
        try:
            parts = grid_size.split('x')
            rows, cols = int(parts[0]), int(parts[1])
        except Exception:
            pass

        # Map position names to grid indices
        position_map = {
            'top-left': (0, 0), 'top-center': (0, 1), 'top-right': (0, 2),
            'middle-left': (1, 0), 'middle-center': (1, 1), 'middle-right': (1, 2),
            'bottom-left': (2, 0), 'bottom-center': (2, 1), 'bottom-right': (2, 2),
        }

        # For 4x4 grids
        for r in range(4):
            for c in range(4):
                position_map[f'row{r+1}-col{c+1}'] = (r, c)

        # Find the captcha grid element
        grid_el = await target.query_selector('.task-image, .challenge-container, .task-grid, [class*="grid"]')

        if grid_el:
            bbox = await grid_el.bounding_box()
            if bbox:
                cell_w = bbox['width'] / cols
                cell_h = bbox['height'] / rows

                for pos_name in matches:
                    pos_name_lower = pos_name.lower().strip()
                    if pos_name_lower in position_map:
                        r, c = position_map[pos_name_lower]
                        x = bbox['x'] + c * cell_w + cell_w / 2
                        y = bbox['y'] + r * cell_h + cell_h / 2
                        print(f"Clicking {pos_name} at ({x:.0f}, {y:.0f})...")
                        await page.mouse.click(x, y)
                        await page.wait_for_timeout(500)
        else:
            # Fallback: try clicking on task images directly
            images = await target.query_selector_all('.task-image img, .challenge-image, [class*="image"] img')
            if images:
                print(f"Found {len(images)} captcha images")
                for pos_name in matches:
                    pos_name_lower = pos_name.lower().strip()
                    if pos_name_lower in position_map:
                        r, c = position_map[pos_name_lower]
                        idx = r * cols + c
                        if idx < len(images):
                            await images[idx].click()
                            print(f"Clicked image {idx} ({pos_name})")
                            await page.wait_for_timeout(500)

        # Click verify/submit button
        await page.wait_for_timeout(1000)
        verify_btn = await target.query_selector('button:has-text("Verify"), button:has-text("Submit"), .verify-button, button[type="submit"]')
        if verify_btn:
            print("Clicking Verify...")
            await verify_btn.click()
        else:
            # Try in main page too
            verify_btn = await page.query_selector('button:has-text("Verify"), .verify-button')
            if verify_btn:
                await verify_btn.click()

    except Exception as e:
        log_error(f"Captcha: execution error — {e}")


def solve():
    """Synchronous wrapper: solve captcha and return token."""
    try:
        loop = asyncio.new_event_loop()
        token = loop.run_until_complete(_solve_captcha(headless=False))
        loop.close()
        return token
    except Exception as e:
        log_error(f"Captcha: solve error — {e}")
        return None


def solve_headless():
    """Headless version for automated solving."""
    try:
        loop = asyncio.new_event_loop()
        token = loop.run_until_complete(_solve_captcha(headless=True))
        loop.close()
        return token
    except Exception as e:
        log_error(f"Captcha: headless solve error — {e}")
        return None
