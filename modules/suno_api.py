"""
Suno API client for PhonkBot — DIRECT API (no wrapper needed).
Talks directly to studio-api.prod.suno.com using Clerk auth.
Auto-refreshes JWT token via auth.suno.com.
"""

import os
import time
import json
import random
import base64
import threading
import requests
from modules.logger import log, log_error, log_warning
from modules.database import get_db, get_config, set_config

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SUNO_API_BASE = 'https://studio-api.prod.suno.com'
CLERK_BASE = 'https://auth.suno.com'


# ═══════════════════════════════════════
# PHONK PROMPT TEMPLATES
# ═══════════════════════════════════════

PHONK_PROMPTS = [
    # Drift Phonk
    "dark aggressive drift phonk beat, heavy distorted 808 bass, Memphis rap vocal chops, cowbell pattern, high energy racing vibe",
    "hard drift phonk, deep sub bass, chopped soul samples, aggressive cowbell, dark atmosphere, night driving energy",
    "phonk drift beat, distorted 808s, dark piano melody, Memphis vocal samples, cowbell hits, underground racing mood",
    "aggressive phonk with heavy bass drops, cowbell rhythm, pitched down vocals, dark synth melody, drift car energy",
    "drift phonk instrumental, hard hitting 808 bass, fast cowbell pattern, dark orchestral samples, midnight racing aesthetic",

    # Dark Phonk
    "dark phonk beat, evil piano melody, heavy 808 bass, horror movie atmosphere, Memphis rap style, deep and menacing",
    "sinister dark phonk, deep bass, eerie choir samples, slow heavy drums, haunted atmosphere, underground vibe",
    "dark atmospheric phonk, distorted bass, reversed vocals, horror synth pads, slow grinding beat, evil energy",

    # Aggressive Phonk
    "aggressive gym phonk, hard 808 bass, fast tempo, motivational dark energy, cowbell hits, intense workout beat",
    "hard phonk beat, maximum distortion, aggressive energy, fast cowbell, screaming vocal chops, fight music vibe",
    "intense aggressive phonk, heavy bass drops, rapid cowbell, distorted kicks, raw underground energy, no mercy",
    "brutal phonk instrumental, crushing 808s, relentless cowbell, dark vocal samples, combat sports energy",

    # Chill Phonk / Phonk House
    "chill phonk beat, smooth bass line, lo-fi atmosphere, relaxed cowbell, night city driving, aesthetic vibe",
    "phonk house instrumental, groovy bass, funky samples, cowbell groove, late night cruising mood, smooth energy",
    "laid back phonk, deep bass, vintage soul samples, relaxed tempo, night drive aesthetic, smooth and dark",

    # Memphis Phonk
    "Memphis phonk revival, triple six style, dark trap beats, heavy 808, chopped vocals, underground Memphis sound",
    "old school Memphis phonk, lo-fi production, dark samples, heavy bass, classic phonk cowbell, raw underground",

    # Brazilian Phonk
    "Brazilian phonk funk, heavy bass, rave energy, MC vocal chops, aggressive beat, high tempo party phonk",
    "hard Brazilian phonk, funk bass line, rave synths, cowbell, high energy dance phonk, club banger",

    # Experimental
    "experimental phonk, orchestral strings mixed with heavy 808 bass, cinematic dark atmosphere, epic cowbell drops",
    "phonk with guitar riffs, heavy metal meets 808 bass, aggressive energy, cowbell, dark rock phonk fusion",
]

PHONK_STYLES = [
    "phonk, drift phonk, dark, aggressive, 808 bass, cowbell",
    "phonk, dark phonk, Memphis, underground, heavy bass",
    "drift phonk, racing, aggressive, hard bass, cowbell",
    "phonk, gym music, workout, aggressive, motivational",
    "chill phonk, night drive, aesthetic, lo-fi, smooth bass",
    "Memphis phonk, triple six, dark trap, underground",
    "Brazilian phonk, funk, rave, high energy, party",
    "phonk, experimental, cinematic, orchestral, dark",
]


def _get_reject_feedback():
    """Check recent rejections and ask Gemini to summarize what to avoid."""
    try:
        conn = get_db()
        rejects = conn.execute('''
            SELECT t.suno_prompt, t.suno_style, td.reject_reason
            FROM training_data td
            JOIN tracks t ON t.id = td.track_id
            WHERE td.rating = 0 AND td.reject_reason IS NOT NULL
            ORDER BY td.created_at DESC LIMIT 10
        ''').fetchall()

        if not rejects:
            return None

        gemini_key = get_config('gemini_api_key')
        if not gemini_key:
            return None

        from google import genai
        client = genai.Client(api_key=gemini_key)

        feedback_lines = []
        for r in rejects:
            feedback_lines.append(f"- Prompt: {r['suno_prompt'][:100]} | Reason: {r['reject_reason']}")

        gemini_prompt = f"""You help improve AI music generation. Below are recently REJECTED phonk tracks with reasons (in Polish).

{chr(10).join(feedback_lines)}

Based on these rejections, write a SHORT (max 30 words) English instruction for the music AI about what to AVOID or IMPROVE.
Example: "avoid slow tempo, use more aggressive 808 bass, less piano"
Return ONLY the instruction, no explanation."""

        response = client.models.generate_content(
            model=get_config('gemini_model', 'gemini-2.0-flash'),
            contents=gemini_prompt,
        )
        result = response.text.strip().strip('"')
        log(f"Reject feedback applied: {result}")
        return result

    except Exception as e:
        log_error(f"Reject feedback analysis failed: {e}")
        return None


def generate_suno_prompt():
    """Generate a randomized phonk prompt with style tags, learning from rejections."""
    prompt = random.choice(PHONK_PROMPTS)
    style = random.choice(PHONK_STYLES)

    modifiers = [
        ", 150 BPM", ", 140 BPM", ", 160 BPM",
        ", hard hitting", ", maximum energy", ", dark vibes",
        ", street racing", ", underground", ", raw and unfiltered",
        "", "", "",
    ]
    prompt += random.choice(modifiers)

    # Apply feedback from recent rejections
    feedback = _get_reject_feedback()
    if feedback:
        prompt += f", {feedback}"

    return prompt, style


# ═══════════════════════════════════════
# AUTH — REFRESH TOKEN + CLERK FALLBACK
# ═══════════════════════════════════════

def _refresh_jwt_via_token():
    """
    Get fresh JWT using Suno refresh token (__client cookie).
    The refresh token IS the Clerk __client cookie value.
    Flow: __client cookie → get session ID → exchange for short-lived JWT.
    """
    refresh_token = get_config('suno_refresh_token', '')
    if not refresh_token:
        return None

    cookie_header = f'__client={refresh_token}'
    api_params = {'__clerk_api_version': '2025-11-10'}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }

    try:
        # Step 1: Get active session ID
        resp = requests.get(
            f'{CLERK_BASE}/v1/client',
            params=api_params,
            headers={**headers, 'Cookie': cookie_header},
            timeout=10,
        )
        if resp.status_code != 200:
            log_warning(f"Suno auth: /v1/client returned {resp.status_code}")
            return None

        data = resp.json()
        response = data.get('response', data)

        # Update __client from Set-Cookie if present
        _update_refresh_token(resp)

        # Find active session
        session_id = None
        sessions = response.get('sessions', [])
        for s in sessions:
            if s.get('status') == 'active':
                session_id = s.get('id')
                break
        if not session_id:
            session_id = response.get('last_active_session_id')
        if not session_id:
            log_warning("Suno auth: no active session found via refresh token")
            return None

        # Step 2: Exchange session for JWT
        resp2 = requests.post(
            f'{CLERK_BASE}/v1/client/sessions/{session_id}/tokens',
            params=api_params,
            headers={
                **headers,
                'Cookie': cookie_header,
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            timeout=15,
        )
        if resp2.status_code != 200:
            log_warning(f"Suno auth: tokens endpoint returned {resp2.status_code}")
            return None

        # Update __client from Set-Cookie (cookie rotation)
        _update_refresh_token(resp2)

        jwt_data = resp2.json()
        jwt = jwt_data.get('jwt', '') or jwt_data.get('response', {}).get('jwt', '')
        if jwt:
            log("Suno auth: JWT refreshed via refresh token")
            return jwt

        log_warning("Suno auth: no JWT in tokens response")

    except Exception as e:
        log_error(f"Suno auth: refresh token error — {e}")

    return None


def _update_refresh_token(resp):
    """Update stored refresh token from Set-Cookie header (cookie rotation)."""
    set_cookie = resp.headers.get('Set-Cookie', '')
    if not set_cookie or '__client=' not in set_cookie:
        return

    for part in set_cookie.split(','):
        if '__client=' in part:
            # Extract value from "  __client=eyJ...;  Path=/; ..."
            for segment in part.split(';'):
                segment = segment.strip()
                if segment.startswith('__client='):
                    new_token = segment[len('__client='):]
                    if new_token and len(new_token) > 100:
                        set_config('suno_refresh_token', new_token)
                        log("Suno auth: refresh token rotated (updated from Set-Cookie)")
                    return


def _get_session_id(cookie_str):
    """Extract Clerk session ID from __client cookie via API."""
    try:
        resp = requests.get(
            f'{CLERK_BASE}/v1/client',
            headers={'Cookie': cookie_str},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            sessions = data.get('response', {}).get('sessions', [])
            for s in sessions:
                if s.get('status') == 'active':
                    return s.get('id')
            last = data.get('response', {}).get('last_active_session_id')
            if last:
                return last
    except Exception as e:
        log_error(f"Suno auth: failed to get session — {e}")
    return None


def _refresh_jwt(cookie_str, session_id):
    """Get fresh JWT from Clerk token endpoint."""
    try:
        resp = requests.post(
            f'{CLERK_BASE}/v1/client/sessions/{session_id}/tokens',
            headers={
                'Cookie': cookie_str,
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            jwt = data.get('jwt', '')
            if jwt:
                return jwt
            jwt = data.get('response', {}).get('jwt', '')
            return jwt

        log_error(f"Suno auth: token refresh failed — HTTP {resp.status_code}")
    except Exception as e:
        log_error(f"Suno auth: token refresh error — {e}")
    return None


# ═══════════════════════════════════════
# DIRECT SUNO API CLIENT
# ═══════════════════════════════════════

class SunoClient:
    """Direct client for Suno API — no wrapper needed."""

    def __init__(self):
        self.cookie = get_config('suno_cookie', '')
        self.jwt = None
        self.session_id = None
        self.timeout = 60

    def _ensure_auth(self):
        """Ensure we have a valid JWT for API calls."""
        # Check if current JWT is still valid
        if self.jwt and self._jwt_valid(self.jwt):
            return True

        # Try saved JWT from DB
        saved_jwt = get_config('suno_jwt', '')
        if saved_jwt and self._jwt_valid(saved_jwt):
            self.jwt = saved_jwt
            return True

        # Method 1: Try refresh token (best — no cookie needed, valid until 2027)
        jwt = _refresh_jwt_via_token()
        if jwt:
            self.jwt = jwt
            set_config('suno_jwt', jwt)
            return True

        # Method 2: Try Clerk cookie refresh (fallback)
        if not self.cookie:
            self.cookie = get_config('suno_cookie', '')

        if self.cookie:
            if not self.session_id:
                self.session_id = get_config('suno_session_id', '') or _get_session_id(self.cookie)

            if self.session_id:
                log(f"Suno auth: session ID = {self.session_id[:30]}...")
                self.jwt = _refresh_jwt(self.cookie, self.session_id)
                if self.jwt:
                    set_config('suno_jwt', self.jwt)
                    log("Suno auth: JWT refreshed via Clerk")
                    return True

        log_error("Suno auth: all methods failed")
        return False

    def _jwt_valid(self, jwt_token):
        """Check if JWT is not expired."""
        try:
            parts = jwt_token.split('.')
            if len(parts) >= 2:
                payload = parts[1] + '=' * (4 - len(parts[1]) % 4)
                data = json.loads(base64.urlsafe_b64decode(payload))
                return time.time() < data.get('exp', 0) - 30
        except:
            pass
        return False

    def _headers(self):
        """Build request headers — mimic Android client to avoid Turnstile."""
        import uuid
        device_id = get_config('suno_device_id', '')
        if not device_id:
            device_id = str(uuid.uuid4())
            set_config('suno_device_id', device_id)

        h = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
            'Referer': 'https://suno.com/',
            'Origin': 'https://suno.com',
            'Device-Id': device_id,
            'x-suno-client': 'Android prerelease-4nt180t 1.0.42',
            'X-Requested-With': 'com.suno.android',
            'sec-ch-ua': '"Chromium";v="130", "Android WebView";v="130", "Not?A_Brand";v="99"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
        }
        if self.jwt:
            h['Authorization'] = f'Bearer {self.jwt}'

        # Add Browser-Token if available (from captcha solver)
        browser_token = get_config('suno_browser_token', '')
        if browser_token:
            h['Browser-Token'] = browser_token

        return h

    def is_configured(self):
        """Check if Suno auth works."""
        return self._ensure_auth()

    def _check_captcha(self):
        """Check if captcha is required for generation."""
        try:
            resp = requests.post(
                f'{SUNO_API_BASE}/api/c/check',
                json={"ctype": "generation"},
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                required = data.get('required', False)
                if required:
                    log_warning("Suno: captcha required! Generation may fail.")
                return required
        except Exception:
            pass
        return False

    def generate(self, prompt, style, instrumental=True):
        """Generate music via Suno API. Returns list of song IDs."""
        if not self._ensure_auth():
            return []

        # Check if captcha is required
        self._check_captcha()

        log(f"Suno: generating — prompt: {prompt[:80]}...")

        # Get captcha token if available
        captcha_token = None
        try:
            from modules.suno_captcha import get_captcha_token
            captcha_token = get_captcha_token()
            if captcha_token:
                log("Suno: using stored captcha token")
        except Exception:
            pass

        payload = {
            "gpt_description_prompt": None,
            "prompt": "",
            "tags": style,
            "mv": get_config('suno_model', 'chirp-v4'),
            "title": "",
            "make_instrumental": instrumental,
            "generation_type": "TEXT",
            "token": captcha_token,
        }

        # For custom generation, use prompt in the right field
        if prompt:
            payload["prompt"] = prompt

        try:
            resp = requests.post(
                f'{SUNO_API_BASE}/api/generate/v2/',
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )

            if resp.status_code in (401, 422):
                # Token expired or invalid, force refresh
                log_warning(f"Suno: {resp.status_code}, forcing token refresh...")
                self.jwt = None
                set_config('suno_jwt', '')
                if self._ensure_auth():
                    resp = requests.post(
                        f'{SUNO_API_BASE}/api/generate/v2/',
                        json=payload,
                        headers=self._headers(),
                        timeout=self.timeout,
                    )
                else:
                    return []

            if resp.status_code == 429:
                log_warning("Suno: rate limited (429), retrying in 60s...")
                time.sleep(60)
                resp = requests.post(
                    f'{SUNO_API_BASE}/api/generate/v2/',
                    json=payload,
                    headers=self._headers(),
                    timeout=self.timeout,
                )

            if resp.status_code != 200:
                log_error(f"Suno API error: {resp.status_code} — {resp.text[:300]}")
                return []

            data = resp.json()

            # Extract clip IDs from response
            clips = data.get('clips', [])
            if not clips:
                # Try alternative response format
                clips = data if isinstance(data, list) else []

            ids = [clip.get('id', '') for clip in clips if clip.get('id')]

            if not ids:
                log_error(f"Suno: no clip IDs in response — {json.dumps(data)[:300]}")
                return []

            log(f"Suno: generation started — IDs: {ids}")
            return ids

        except requests.exceptions.Timeout:
            log_error("Suno: request timeout")
            return []
        except Exception as e:
            log_error(f"Suno: generate error — {e}")
            return []

    def check_status(self, task_ids):
        """Check generation status. Returns list of song dicts."""
        if not task_ids:
            return []

        if not self._ensure_auth():
            return []

        ids_str = ','.join(task_ids)
        try:
            resp = requests.get(
                f'{SUNO_API_BASE}/api/feed/v2',
                params={'ids': ids_str},
                headers=self._headers(),
                timeout=self.timeout,
            )

            if resp.status_code != 200:
                log_error(f"Suno: feed error — HTTP {resp.status_code}")
                return []

            data = resp.json()
            songs = data if isinstance(data, list) else data.get('clips', data.get('data', []))

            return [
                {
                    'id': s.get('id', ''),
                    'status': s.get('status', ''),
                    'audio_url': s.get('audio_url', ''),
                    'title': s.get('title', ''),
                    'duration': s.get('duration', 0),
                }
                for s in songs
            ]

        except Exception as e:
            log_error(f"Suno: check_status error — {e}")
            return []

    def download(self, audio_url, dest_path):
        """Download audio file from Suno CDN."""
        if not audio_url:
            return False

        try:
            resp = requests.get(audio_url, timeout=120, stream=True)
            if resp.status_code != 200:
                log_error(f"Suno: download failed — HTTP {resp.status_code}")
                return False

            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            size = os.path.getsize(dest_path)
            if size < 100 * 1024:
                log_error(f"Suno: downloaded file too small ({size} bytes)")
                os.remove(dest_path)
                return False

            log(f"Suno: downloaded {size // 1024}KB -> {dest_path}")
            return True

        except Exception as e:
            log_error(f"Suno: download error — {e}")
            return False

    def wait_for_completion(self, task_ids, poll_interval=30, max_wait=600):
        """Poll until generation completes or timeout."""
        start = time.time()
        while time.time() - start < max_wait:
            songs = self.check_status(task_ids)
            completed = [s for s in songs if s.get('audio_url')]
            if completed:
                return completed

            failed = [s for s in songs if s.get('status') in ('error', 'failed', 'complete')]
            # 'complete' without audio_url means generation failed silently
            error_only = [s for s in failed if s.get('status') in ('error', 'failed')]
            if error_only:
                log_error(f"Suno: generation failed — {error_only}")
                return []

            log(f"Suno: waiting... ({int(time.time() - start)}s elapsed)")
            time.sleep(poll_interval)

        log_error(f"Suno: timeout after {max_wait}s")
        return []


# ═══════════════════════════════════════
# KEEP-ALIVE (background JWT refresh)
# ═══════════════════════════════════════

_keep_alive_thread = None

def _keep_alive_loop():
    """Background thread that refreshes JWT every 5 minutes."""
    session_id = None
    failures = 0

    while True:
        try:
            time.sleep(300)  # 5 minutes

            # Method 1: Try refresh token (best, no cookie needed)
            jwt = _refresh_jwt_via_token()
            if jwt:
                set_config('suno_jwt', jwt)
                failures = 0
                if int(time.time()) % 3600 < 300:
                    log("Suno keep-alive: JWT refreshed via refresh token")
                continue

            # Method 2: Try Clerk cookie refresh
            cookie = get_config('suno_cookie', '')
            if not cookie:
                failures += 1
                if failures >= 3:
                    if _try_autotoken():
                        failures = 0
                        continue
                    _alert_cookie_expired()
                    failures = 0
                    time.sleep(1800)
                continue

            if not session_id:
                session_id = _get_session_id(cookie)
                if not session_id:
                    failures += 1
                    if failures >= 3:
                        if _try_autotoken():
                            failures = 0
                            session_id = None
                            continue
                        _alert_cookie_expired()
                        failures = 0
                        time.sleep(1800)
                    continue

            jwt = _refresh_jwt(cookie, session_id)
            if jwt:
                set_config('suno_jwt', jwt)
                failures = 0
                if int(time.time()) % 3600 < 300:
                    log("Suno keep-alive: JWT refreshed via Clerk")
            else:
                failures += 1
                log_warning(f"Suno keep-alive: refresh failed ({failures}/3)")
                if failures >= 3:
                    session_id = None
                    if _try_autotoken():
                        failures = 0
                        continue
                    _alert_cookie_expired()
                    failures = 0
                    time.sleep(1800)

        except Exception as e:
            log_error(f"Suno keep-alive error: {e}")
            time.sleep(60)


_last_alert_time = 0


def _try_autotoken():
    """Try Playwright auto-refresh. Returns True on success."""
    try:
        from modules.suno_autotoken import refresh, is_available
        if is_available():
            log("Suno: trying auto-token (Playwright)...")
            if refresh():
                log("Suno: auto-token refresh OK!")
                return True
            log_warning("Suno: auto-token refresh failed")
    except Exception as e:
        log_error(f"Suno: auto-token error — {e}")
    return False


def _alert_cookie_expired():
    """Send Telegram alert that cookie needs refresh. Max 1 per day."""
    global _last_alert_time
    now = time.time()
    if now - _last_alert_time < 86400:  # 24 hours
        return
    _last_alert_time = now
    try:
        from modules.telegram_bot import send_message
        send_message(
            "Suno session wygasla! Auto-token tez nie pomogl.\n"
            "Na Pi uruchom:\n"
            "  python3 auto_token_setup.py\n"
            "Lub wklej cookie recznie w Settings."
        )
    except:
        pass


def start_keep_alive():
    """Start background JWT refresh thread."""
    global _keep_alive_thread
    has_cookie = bool(get_config('suno_cookie', ''))
    has_refresh = bool(get_config('suno_refresh_token', ''))

    if not has_cookie and not has_refresh:
        log("Suno: no cookie or refresh token, keep-alive not started")
        return

    _keep_alive_thread = threading.Thread(target=_keep_alive_loop, daemon=True)
    _keep_alive_thread.start()
    method = "refresh token" if has_refresh else "cookie"
    log(f"Suno: keep-alive started ({method}, refresh every 5min)")


# ═══════════════════════════════════════
# HIGH-LEVEL FUNCTIONS
# ═══════════════════════════════════════

def generate_and_download(channel_id=None):
    """
    Full cycle: generate prompt -> call Suno -> poll -> download -> save to DB.
    Returns track_id on success, None on failure.
    """
    client = SunoClient()

    if not client.is_configured():
        log_error("Suno: auth failed. Check cookie in settings.")
        return None

    prompt, style = generate_suno_prompt()

    model = get_config('suno_model', 'chirp-v4')

    conn = get_db()
    cursor = conn.execute(
        '''INSERT INTO tracks (channel_id, suno_prompt, suno_style, status, model)
           VALUES (?, ?, ?, 'generating', ?)''',
        (channel_id, prompt, style, model)
    )
    conn.commit()
    track_id = cursor.lastrowid
    log(f"Track #{track_id}: generating...")

    task_ids = client.generate(prompt, style, instrumental=True)
    if not task_ids:
        conn.execute("UPDATE tracks SET status = 'failed', error_message = 'Generation API returned no IDs' WHERE id = ?", (track_id,))
        conn.commit()
        return None

    conn.execute("UPDATE tracks SET suno_id = ? WHERE id = ?", (','.join(task_ids), track_id))
    conn.commit()

    songs = client.wait_for_completion(task_ids)
    if not songs:
        conn.execute("UPDATE tracks SET status = 'failed', error_message = 'Generation timeout or error' WHERE id = ?", (track_id,))
        conn.commit()
        return None

    song = songs[0]
    audio_url = song['audio_url']
    suno_title = song.get('title', '')

    audio_dir = os.path.join(_APP_DIR, 'output', 'audio')
    audio_path = os.path.join(audio_dir, f'track_{track_id}.mp3')

    conn.execute("UPDATE tracks SET status = 'downloading', audio_url = ?, title = ? WHERE id = ?", (audio_url, suno_title, track_id))
    conn.commit()

    if not client.download(audio_url, audio_path):
        conn.execute("UPDATE tracks SET status = 'failed', error_message = 'Download failed' WHERE id = ?", (track_id,))
        conn.commit()
        return None

    duration = _get_audio_duration(audio_path)

    conn.execute(
        "UPDATE tracks SET status = 'ready_for_review', audio_path = ?, duration_seconds = ? WHERE id = ?",
        (audio_path, duration, track_id)
    )
    conn.commit()

    log(f"Track #{track_id}: ready for review ({duration:.0f}s)")
    return track_id


def regenerate_from_track(old_track_id):
    """Generate a new track using a similar prompt to an existing one."""
    conn = get_db()
    old_track = conn.execute('SELECT * FROM tracks WHERE id = ?', (old_track_id,)).fetchone()
    if not old_track:
        return None

    prompt = old_track['suno_prompt']
    mutations = [
        " with more aggressive energy",
        " with heavier bass",
        " with darker atmosphere",
        " with faster tempo",
        " with more cowbell",
        " with deeper 808s",
    ]
    prompt += random.choice(mutations)

    client = SunoClient()
    style = old_track['suno_style']

    cursor = conn.execute(
        '''INSERT INTO tracks (channel_id, suno_prompt, suno_style, status)
           VALUES (?, ?, ?, 'generating')''',
        (old_track['channel_id'], prompt, style)
    )
    conn.commit()
    track_id = cursor.lastrowid

    task_ids = client.generate(prompt, style, instrumental=True)
    if not task_ids:
        conn.execute("UPDATE tracks SET status = 'failed', error_message = 'Regeneration failed' WHERE id = ?", (track_id,))
        conn.commit()
        return None

    conn.execute("UPDATE tracks SET suno_id = ? WHERE id = ?", (','.join(task_ids), track_id))
    conn.commit()

    songs = client.wait_for_completion(task_ids)
    if not songs:
        conn.execute("UPDATE tracks SET status = 'failed', error_message = 'Regeneration timeout' WHERE id = ?", (track_id,))
        conn.commit()
        return None

    song = songs[0]
    audio_path = os.path.join(_APP_DIR, 'output', 'audio', f'track_{track_id}.mp3')

    conn.execute("UPDATE tracks SET status = 'downloading', audio_url = ? WHERE id = ?", (song['audio_url'], track_id))
    conn.commit()

    if not client.download(song['audio_url'], audio_path):
        conn.execute("UPDATE tracks SET status = 'failed', error_message = 'Download failed' WHERE id = ?", (track_id,))
        conn.commit()
        return None

    duration = _get_audio_duration(audio_path)
    conn.execute(
        "UPDATE tracks SET status = 'ready_for_review', audio_path = ?, duration_seconds = ? WHERE id = ?",
        (audio_path, duration, track_id)
    )
    conn.commit()

    log(f"Track #{track_id}: regenerated from #{old_track_id}, ready for review")
    return track_id


def _get_audio_duration(audio_path):
    """Get audio duration in seconds using ffprobe."""
    try:
        import subprocess
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', audio_path],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except:
        return 0.0
