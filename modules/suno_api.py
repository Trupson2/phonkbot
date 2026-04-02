"""
Suno API client for PhonkBot.
Uses unofficial Suno API wrapper (gcui-art/suno-api or compatible).

RISK: Unofficial API — cookie auth expires, may break without notice.
MusicProvider abstraction allows swapping to another provider.
"""

import os
import time
import json
import random
import requests
from abc import ABC, abstractmethod
from modules.logger import log, log_error, log_warning
from modules.database import get_db, get_config, set_config

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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

# Style tags for Suno
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


def generate_suno_prompt():
    """Generate a randomized phonk prompt with style tags."""
    prompt = random.choice(PHONK_PROMPTS)
    style = random.choice(PHONK_STYLES)

    # Add slight randomization
    modifiers = [
        ", 150 BPM", ", 140 BPM", ", 160 BPM",
        ", hard hitting", ", maximum energy", ", dark vibes",
        ", street racing", ", underground", ", raw and unfiltered",
        "",  # no modifier
        "",
        "",
    ]
    prompt += random.choice(modifiers)

    return prompt, style


# ═══════════════════════════════════════
# ABSTRACT PROVIDER
# ═══════════════════════════════════════

class MusicProvider(ABC):
    """Abstract base class for music generation providers."""

    @abstractmethod
    def generate(self, prompt, style, instrumental=True):
        """Start generation. Returns task/song IDs."""
        pass

    @abstractmethod
    def check_status(self, task_ids):
        """Check generation status. Returns list of results."""
        pass

    @abstractmethod
    def download(self, audio_url, dest_path):
        """Download generated audio to local path. Returns True on success."""
        pass


# ═══════════════════════════════════════
# SUNO CLIENT
# ═══════════════════════════════════════

class SunoClient(MusicProvider):
    """Client for unofficial Suno API (gcui-art/suno-api compatible)."""

    def __init__(self):
        self.base_url = get_config('suno_api_url', 'http://localhost:3000').rstrip('/')
        self.cookie = get_config('suno_cookie', '')
        self.timeout = 30

    def _headers(self):
        headers = {'Content-Type': 'application/json'}
        if self.cookie:
            headers['Cookie'] = self.cookie
        return headers

    def is_configured(self):
        """Check if Suno API is reachable."""
        try:
            resp = requests.get(f'{self.base_url}/', timeout=5)
            return resp.status_code == 200
        except:
            return False

    def generate(self, prompt, style, instrumental=True):
        """Generate music via Suno API. Returns list of song IDs."""
        log(f"Suno: generating — prompt: {prompt[:80]}...")

        payload = {
            "prompt": prompt,
            "tags": style,
            "make_instrumental": instrumental,
            "wait_audio": False,
        }

        try:
            resp = requests.post(
                f'{self.base_url}/api/custom_generate',
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )

            if resp.status_code == 429:
                log_warning("Suno: rate limited (429), retrying in 60s...")
                time.sleep(60)
                resp = requests.post(
                    f'{self.base_url}/api/custom_generate',
                    json=payload,
                    headers=self._headers(),
                    timeout=self.timeout,
                )

            if resp.status_code != 200:
                log_error(f"Suno API error: {resp.status_code} — {resp.text[:300]}")
                return []

            data = resp.json()
            # Response is a list of song objects with 'id' field
            if isinstance(data, list):
                ids = [song.get('id', '') for song in data if song.get('id')]
            elif isinstance(data, dict) and 'data' in data:
                ids = [song.get('id', '') for song in data['data'] if song.get('id')]
            else:
                ids = []

            log(f"Suno: generation started — IDs: {ids}")
            return ids

        except requests.exceptions.Timeout:
            log_error("Suno: request timeout")
            return []
        except Exception as e:
            log_error(f"Suno: generate error — {e}")
            return []

    def check_status(self, task_ids):
        """Poll Suno API for generation status. Returns list of completed song dicts."""
        if not task_ids:
            return []

        ids_str = ','.join(task_ids)
        try:
            resp = requests.get(
                f'{self.base_url}/api/get',
                params={'ids': ids_str},
                headers=self._headers(),
                timeout=self.timeout,
            )

            if resp.status_code != 200:
                return []

            data = resp.json()
            songs = data if isinstance(data, list) else data.get('data', data.get('result', []))

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

            # Validate file
            size = os.path.getsize(dest_path)
            if size < 100 * 1024:  # < 100KB = probably failed
                log_error(f"Suno: downloaded file too small ({size} bytes)")
                os.remove(dest_path)
                return False

            log(f"Suno: downloaded {size // 1024}KB → {dest_path}")
            return True

        except Exception as e:
            log_error(f"Suno: download error — {e}")
            return False

    def wait_for_completion(self, task_ids, poll_interval=30, max_wait=600):
        """Poll until generation completes or timeout. Returns completed songs."""
        start = time.time()
        while time.time() - start < max_wait:
            songs = self.check_status(task_ids)
            completed = [s for s in songs if s.get('audio_url')]
            if completed:
                return completed

            # Check for errors
            failed = [s for s in songs if s.get('status') in ('error', 'failed')]
            if failed:
                log_error(f"Suno: generation failed — {failed}")
                return []

            log(f"Suno: waiting... ({int(time.time() - start)}s elapsed)")
            time.sleep(poll_interval)

        log_error(f"Suno: timeout after {max_wait}s")
        return []


# ═══════════════════════════════════════
# HIGH-LEVEL FUNCTIONS
# ═══════════════════════════════════════

def generate_and_download(channel_id=None):
    """
    Full cycle: generate prompt → call Suno → poll → download → save to DB.
    Returns track_id on success, None on failure.
    """
    client = SunoClient()

    if not client.is_configured():
        log_error("Suno API not reachable. Check suno_api_url in settings.")
        return None

    # Generate prompt
    prompt, style = generate_suno_prompt()

    # Insert track record
    conn = get_db()
    cursor = conn.execute(
        '''INSERT INTO tracks (channel_id, suno_prompt, suno_style, status)
           VALUES (?, ?, ?, 'generating')''',
        (channel_id, prompt, style)
    )
    conn.commit()
    track_id = cursor.lastrowid
    log(f"Track #{track_id}: generating...")

    # Call Suno API
    task_ids = client.generate(prompt, style, instrumental=True)
    if not task_ids:
        conn.execute("UPDATE tracks SET status = 'failed', error_message = 'Generation API returned no IDs' WHERE id = ?", (track_id,))
        conn.commit()
        return None

    # Store Suno IDs
    conn.execute("UPDATE tracks SET suno_id = ? WHERE id = ?", (','.join(task_ids), track_id))
    conn.commit()

    # Poll for completion
    songs = client.wait_for_completion(task_ids)
    if not songs:
        conn.execute("UPDATE tracks SET status = 'failed', error_message = 'Generation timeout or error' WHERE id = ?", (track_id,))
        conn.commit()
        return None

    # Pick best song (first completed)
    song = songs[0]
    audio_url = song['audio_url']

    # Download
    audio_dir = os.path.join(_APP_DIR, 'output', 'audio')
    audio_path = os.path.join(audio_dir, f'track_{track_id}.mp3')

    conn.execute("UPDATE tracks SET status = 'downloading', audio_url = ? WHERE id = ?", (audio_url, track_id))
    conn.commit()

    if not client.download(audio_url, audio_path):
        conn.execute("UPDATE tracks SET status = 'failed', error_message = 'Download failed' WHERE id = ?", (track_id,))
        conn.commit()
        return None

    # Get duration via ffprobe
    duration = _get_audio_duration(audio_path)

    # Update track as ready
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

    # Mutate the prompt slightly
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

    # Reuse the flow but with new prompt
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
