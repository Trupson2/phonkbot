"""Direct Suno API client.

Talks to studio-api.prod.suno.com. Auth comes from `modules.suno.auth.get_valid_jwt()`
which encapsulates the refresh cascade. This module focuses on:
  - generate / status / download HTTP calls
  - high-level orchestration (generate_and_download saves to DB)
  - regenerate_from_track (mutated prompt re-roll)

Suno returns 2 clip IDs per request — both are saved as separate `tracks` rows
linked via `sibling_track_id` so credits aren't wasted.
"""

import json
import os
import time
import uuid

import requests

from modules.database import get_config, get_db, set_config
from modules.logger import log, log_error, log_warning
from modules.suno.auth import get_valid_jwt
from modules.suno.prompts import generate_suno_prompt

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SUNO_API_BASE = 'https://studio-api.prod.suno.com'


class SunoClient:
    """Stateless wrapper around the Suno HTTP API."""

    timeout = 60

    def _device_id(self):
        device_id = get_config('suno_device_id', '')
        if not device_id:
            device_id = str(uuid.uuid4())
            set_config('suno_device_id', device_id)
        return device_id

    def _headers(self):
        """Headers that mimic the Android client to avoid Turnstile."""
        h = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
            'Referer': 'https://suno.com/',
            'Origin': 'https://suno.com',
            'Device-Id': self._device_id(),
            'x-suno-client': 'Android prerelease-4nt180t 1.0.42',
            'X-Requested-With': 'com.suno.android',
            'sec-ch-ua': '"Chromium";v="130", "Android WebView";v="130", "Not?A_Brand";v="99"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
        }

        jwt = get_valid_jwt()
        if jwt:
            h['Authorization'] = f'Bearer {jwt}'

        browser_token = get_config('suno_browser_token', '')
        if browser_token:
            h['Browser-Token'] = browser_token

        return h

    def is_configured(self):
        return bool(get_valid_jwt())

    def captcha_required(self):
        """Ping /api/c/check — Suno demands a captcha token for generation now."""
        try:
            resp = requests.post(
                f'{SUNO_API_BASE}/api/c/check',
                json={"ctype": "generation"},
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                return bool(resp.json().get('required', False))
        except Exception:
            pass
        return False

    # ───── generation ─────

    def generate(self, prompt, style, instrumental=True):
        """Kick off a generation. Returns list of clip IDs (usually 2)."""
        if not get_valid_jwt():
            return []

        if self.captcha_required():
            log_warning("Suno: captcha required for this generation")

        captcha_token = _get_captcha_token()
        if captcha_token:
            log("Suno: using stored captcha token")

        # Field semantics:
        #   gpt_description_prompt = "Song Description" (Simple mode style brief)
        #   prompt                 = explicit lyrics (empty for instrumental)
        #   tags                   = comma-separated style tags
        # Putting the description in `prompt` with make_instrumental=True caused
        # Suno to render <30s clips (treated as malformed lyrics).
        payload = {
            "gpt_description_prompt": prompt or None,
            "prompt": "",
            "tags": style,
            "mv": get_config('suno_model', 'chirp-v4'),
            "title": "",
            "make_instrumental": instrumental,
            "generation_type": "TEXT",
            "token": captcha_token,
        }

        log(f"Suno: generating — prompt: {prompt[:80]}...")

        try:
            resp = self._post_generate(payload)

            if resp.status_code in (401, 422):
                log_warning(f"Suno: {resp.status_code}, forcing JWT refresh and retrying")
                set_config('suno_jwt', '')
                if get_valid_jwt():
                    resp = self._post_generate(payload)

            if resp.status_code == 429:
                log_warning("Suno: rate limited (429), retrying in 60s...")
                time.sleep(60)
                resp = self._post_generate(payload)

            if resp.status_code != 200:
                log_error(f"Suno API error: {resp.status_code} — {resp.text[:300]}")
                return []

            clips = resp.json().get('clips', []) or []
            ids = [c.get('id', '') for c in clips if c.get('id')]
            if not ids:
                log_error(f"Suno: no clip IDs in response — {resp.text[:300]}")
                return []
            log(f"Suno: generation started — IDs: {ids}")
            return ids

        except requests.exceptions.Timeout:
            log_error("Suno: request timeout")
        except Exception as e:
            log_error(f"Suno: generate error — {e}")
        return []

    def _post_generate(self, payload):
        return requests.post(
            f'{SUNO_API_BASE}/api/generate/v2/',
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )

    def check_status(self, task_ids):
        """Return list of song dicts (id, status, audio_url, title, duration)."""
        if not task_ids or not get_valid_jwt():
            return []

        try:
            resp = requests.get(
                f'{SUNO_API_BASE}/api/feed/v2',
                params={'ids': ','.join(task_ids)},
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

    def wait_for_completion(self, task_ids, poll_interval=30, max_wait=600):
        """Poll until status='complete' for at least one clip, or timeout.

        Suno returns audio_url early during 'streaming' — that file is a partial
        that grows. We require status='complete' to avoid downloading partials.
        """
        start = time.time()
        while time.time() - start < max_wait:
            songs = self.check_status(task_ids)

            errs = [s for s in songs if s.get('status') in ('error', 'failed')]
            if errs:
                log_error(f"Suno: generation failed — {errs}")
                return []

            done = [s for s in songs if s.get('status') == 'complete' and s.get('audio_url')]
            if done:
                return done

            statuses = [s.get('status', '?') for s in songs] or ['no-data']
            log(f"Suno: waiting... ({int(time.time() - start)}s, status: {','.join(statuses)})")
            time.sleep(poll_interval)

        log_error(f"Suno: timeout after {max_wait}s")
        return []

    def download(self, audio_url, dest_path):
        """Stream MP3 to disk. Returns True on success."""
        if not audio_url:
            return False
        try:
            resp = requests.get(audio_url, timeout=120, stream=True)
            if resp.status_code != 200:
                log_error(f"Suno: download HTTP {resp.status_code}")
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


# ─────────────────────────────────────────────────────────
# High-level helpers
# ─────────────────────────────────────────────────────────

def _get_captcha_token():
    """Get stored captcha token if still fresh."""
    try:
        from modules.suno.captcha import get_captcha_token
        return get_captcha_token()
    except Exception:
        return None


def captcha_token_age_seconds():
    """Return the age of the stored captcha token in seconds, or None."""
    ts = get_config('suno_captcha_token_time', '0')
    try:
        ts_f = float(ts)
        if ts_f <= 0:
            return None
        return time.time() - ts_f
    except (TypeError, ValueError):
        return None


def generate_and_download(channel_id=None):
    """Full cycle: prompt -> Suno -> poll -> download both versions.

    Returns the primary track_id (sibling track_id is linked via FK). Both versions
    appear in the tracks table — sibling has the same prompt/style and links back.
    """
    client = SunoClient()
    if not client.is_configured():
        log_error("Suno: auth failed. Check tokens in settings.")
        return None

    prompt, style = generate_suno_prompt()
    model = get_config('suno_model', 'chirp-v4')

    conn = get_db()
    cursor = conn.execute(
        '''INSERT INTO tracks (channel_id, suno_prompt, suno_style, status, model)
           VALUES (?, ?, ?, 'generating', ?)''',
        (channel_id, prompt, style, model),
    )
    primary_track_id = cursor.lastrowid
    conn.commit()
    log(f"Track #{primary_track_id}: generating...")

    task_ids = client.generate(prompt, style, instrumental=True)
    if not task_ids:
        conn.execute(
            "UPDATE tracks SET status = 'failed', error_message = 'Generation API returned no IDs' WHERE id = ?",
            (primary_track_id,),
        )
        conn.commit()
        return None

    conn.execute("UPDATE tracks SET suno_id = ? WHERE id = ?",
                 (','.join(task_ids), primary_track_id))
    conn.commit()

    songs = client.wait_for_completion(task_ids)
    if not songs:
        conn.execute(
            "UPDATE tracks SET status = 'failed', error_message = 'Generation timeout or error' WHERE id = ?",
            (primary_track_id,),
        )
        conn.commit()
        return None

    # Save primary
    primary_song = songs[0]
    if not _save_song_to_track(conn, primary_track_id, primary_song):
        return None

    # Save sibling versions (Suno returns 2 clips — keep both)
    audio_dir = os.path.join(_APP_DIR, 'output', 'audio')
    for sibling_song in songs[1:]:
        sib_cursor = conn.execute(
            '''INSERT INTO tracks (channel_id, suno_prompt, suno_style, status, model,
                                   sibling_track_id, suno_id)
               VALUES (?, ?, ?, 'downloading', ?, ?, ?)''',
            (channel_id, prompt, style, model, primary_track_id, sibling_song['id']),
        )
        sib_id = sib_cursor.lastrowid
        conn.commit()

        sib_path = os.path.join(audio_dir, f'track_{sib_id}.mp3')
        if client.download(sibling_song['audio_url'], sib_path):
            duration = _get_audio_duration(sib_path)
            conn.execute(
                '''UPDATE tracks SET status='ready_for_review', audio_url=?, audio_path=?,
                                     duration_seconds=?, title=? WHERE id=?''',
                (sibling_song['audio_url'], sib_path, duration,
                 sibling_song.get('title', ''), sib_id),
            )
        else:
            conn.execute(
                "UPDATE tracks SET status='failed', error_message='Download failed' WHERE id=?",
                (sib_id,),
            )
        conn.commit()

    return primary_track_id


def _save_song_to_track(conn, track_id, song):
    audio_dir = os.path.join(_APP_DIR, 'output', 'audio')
    audio_path = os.path.join(audio_dir, f'track_{track_id}.mp3')

    conn.execute(
        "UPDATE tracks SET status='downloading', audio_url=?, title=? WHERE id=?",
        (song['audio_url'], song.get('title', ''), track_id),
    )
    conn.commit()

    client = SunoClient()
    if not client.download(song['audio_url'], audio_path):
        conn.execute(
            "UPDATE tracks SET status='failed', error_message='Download failed' WHERE id=?",
            (track_id,),
        )
        conn.commit()
        return False

    duration = _get_audio_duration(audio_path)
    conn.execute(
        "UPDATE tracks SET status='ready_for_review', audio_path=?, duration_seconds=? WHERE id=?",
        (audio_path, duration, track_id),
    )
    conn.commit()
    log(f"Track #{track_id}: ready for review ({duration:.0f}s)")
    return True


def regenerate_from_track(old_track_id):
    """Generate a new track based on a mutated version of an old prompt."""
    import random

    conn = get_db()
    old = conn.execute('SELECT * FROM tracks WHERE id = ?', (old_track_id,)).fetchone()
    if not old:
        return None

    mutations = [
        " with more aggressive energy",
        " with heavier bass",
        " with darker atmosphere",
        " with faster tempo",
        " with more cowbell",
        " with deeper 808s",
    ]
    prompt = (old['suno_prompt'] or '') + random.choice(mutations)
    style = old['suno_style']

    client = SunoClient()
    cursor = conn.execute(
        '''INSERT INTO tracks (channel_id, suno_prompt, suno_style, status)
           VALUES (?, ?, ?, 'generating')''',
        (old['channel_id'], prompt, style),
    )
    track_id = cursor.lastrowid
    conn.commit()

    task_ids = client.generate(prompt, style, instrumental=True)
    if not task_ids:
        conn.execute(
            "UPDATE tracks SET status='failed', error_message='Regeneration failed' WHERE id=?",
            (track_id,),
        )
        conn.commit()
        return None

    conn.execute("UPDATE tracks SET suno_id=? WHERE id=?",
                 (','.join(task_ids), track_id))
    conn.commit()

    songs = client.wait_for_completion(task_ids)
    if not songs:
        conn.execute(
            "UPDATE tracks SET status='failed', error_message='Regeneration timeout' WHERE id=?",
            (track_id,),
        )
        conn.commit()
        return None

    if _save_song_to_track(conn, track_id, songs[0]):
        log(f"Track #{track_id}: regenerated from #{old_track_id}")
        return track_id
    return None


def _get_audio_duration(audio_path):
    """ffprobe -> mutagen -> bitrate-from-size estimate (so duration is never 0)."""
    try:
        import subprocess
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', audio_path],
            capture_output=True, text=True, timeout=10,
        )
        d = float(result.stdout.strip())
        if d > 0:
            return d
    except (FileNotFoundError, ValueError, subprocess.SubprocessError):
        pass

    try:
        from mutagen.mp3 import MP3
        return float(MP3(audio_path).info.length)
    except Exception:
        pass

    # Suno renders ~192 kbps mp3 → 24,000 bytes/sec
    try:
        return round(os.path.getsize(audio_path) / 24000, 1)
    except Exception:
        return 0.0
