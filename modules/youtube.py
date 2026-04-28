"""
YouTube Data API v3 client for PhonkBot.
Handles OAuth2 authentication and resumable video uploads.
Quota: 10K units/day, 1 upload = 1600 units.
"""

import os
import json
import time
from datetime import datetime, timedelta
from modules.logger import log, log_error, log_warning
from modules.database import get_db, get_config, set_config

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# YouTube video category: 10 = Music
MUSIC_CATEGORY = '10'


def get_oauth_url():
    """Generate OAuth2 authorization URL for YouTube."""
    client_id = get_config('youtube_client_id')
    if not client_id:
        return None

    redirect_uri = 'http://localhost:5001/youtube/callback'
    scope = 'https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly'

    url = (
        f'https://accounts.google.com/o/oauth2/v2/auth'
        f'?client_id={client_id}'
        f'&redirect_uri={redirect_uri}'
        f'&response_type=code'
        f'&scope={scope}'
        f'&access_type=offline'
        f'&prompt=consent'
    )
    return url


def exchange_code(code):
    """Exchange authorization code for access + refresh tokens."""
    import requests

    client_id = get_config('youtube_client_id')
    client_secret = get_config('youtube_client_secret')
    redirect_uri = 'http://localhost:5001/youtube/callback'

    resp = requests.post('https://oauth2.googleapis.com/token', data={
        'code': code,
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code',
    }, timeout=15)

    if resp.status_code != 200:
        log_error(f"YouTube OAuth token exchange failed: {resp.text[:300]}")
        return False

    data = resp.json()
    set_config('youtube_access_token', data.get('access_token', ''))
    set_config('youtube_refresh_token', data.get('refresh_token', ''))

    expires_in = data.get('expires_in', 3600)
    expiry = datetime.utcnow() + timedelta(seconds=expires_in)
    set_config('youtube_token_expiry', expiry.isoformat())

    log("YouTube OAuth2 authorized successfully")
    return True


def refresh_token_if_needed():
    """Refresh access token if expired or about to expire."""
    import requests

    expiry_str = get_config('youtube_token_expiry')
    if not expiry_str:
        return False

    try:
        expiry = datetime.fromisoformat(expiry_str)
        if datetime.utcnow() < expiry - timedelta(minutes=5):
            return True  # Still valid
    except:
        pass

    refresh_token = get_config('youtube_refresh_token')
    client_id = get_config('youtube_client_id')
    client_secret = get_config('youtube_client_secret')

    if not all([refresh_token, client_id, client_secret]):
        log_error("YouTube: cannot refresh — missing credentials")
        return False

    resp = requests.post('https://oauth2.googleapis.com/token', data={
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token',
    }, timeout=15)

    if resp.status_code != 200:
        log_error(f"YouTube token refresh failed: {resp.text[:300]}")
        return False

    data = resp.json()
    set_config('youtube_access_token', data.get('access_token', ''))

    expires_in = data.get('expires_in', 3600)
    expiry = datetime.utcnow() + timedelta(seconds=expires_in)
    set_config('youtube_token_expiry', expiry.isoformat())

    log("YouTube token refreshed")
    return True


def _check_quota():
    """Check if we have enough quota for an upload (1650 units)."""
    today = datetime.utcnow().strftime('%Y-%m-%d')
    reset_date = get_config('youtube_quota_reset_date', '')

    if reset_date != today:
        set_config('youtube_quota_used', '0')
        set_config('youtube_quota_reset_date', today)

    used = int(get_config('youtube_quota_used', '0'))
    remaining = 10000 - used

    if remaining < 1650:
        log_warning(f"YouTube quota low: {used}/10000 used, need 1650")
        return False

    return True


def _add_quota(units):
    """Track quota usage."""
    used = int(get_config('youtube_quota_used', '0'))
    set_config('youtube_quota_used', str(used + units))


def upload_video(track_id, video_path, metadata):
    """
    Upload video to YouTube with resumable upload.
    Returns youtube_video_id on success, None on failure.
    """
    import requests

    if not refresh_token_if_needed():
        log_error("YouTube: not authorized")
        return None

    if not _check_quota():
        log_error("YouTube: daily quota exceeded")
        return None

    if not os.path.exists(video_path):
        log_error(f"YouTube: video file not found — {video_path}")
        return None

    access_token = get_config('youtube_access_token')
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
    }

    # Video metadata
    title = metadata.get('title', f'Phonk Beat #{track_id}')[:100]
    description = metadata.get('description', 'AI Generated Phonk Music')[:5000]
    tags = metadata.get('tags', ['phonk', 'drift phonk', 'dark phonk', '808 bass'])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',')]

    body = {
        'snippet': {
            'title': title,
            'description': description,
            'tags': tags[:30],  # YouTube limit: 30 tags
            'categoryId': MUSIC_CATEGORY,
            'defaultLanguage': metadata.get('language', 'en'),
        },
        'status': {
            'privacyStatus': 'public',
            'selfDeclaredMadeForKids': False,
        },
    }

    # Step 1: Initiate resumable upload
    file_size = os.path.getsize(video_path)
    log(f"YouTube: uploading {file_size // (1024*1024)}MB — {title}")

    try:
        init_resp = requests.post(
            'https://www.googleapis.com/upload/youtube/v3/videos'
            '?uploadType=resumable&part=snippet,status',
            headers={**headers, 'X-Upload-Content-Length': str(file_size),
                     'X-Upload-Content-Type': 'video/mp4'},
            json=body,
            timeout=30,
        )

        if init_resp.status_code not in (200, 308):
            log_error(f"YouTube: upload init failed — {init_resp.status_code} {init_resp.text[:300]}")
            return None

        upload_url = init_resp.headers.get('Location')
        if not upload_url:
            log_error("YouTube: no upload URL in response")
            return None

    except Exception as e:
        log_error(f"YouTube: upload init error — {e}")
        return None

    # Step 2: Upload file in 1MB chunks (Pi-friendly)
    chunk_size = 1024 * 1024  # 1MB
    uploaded = 0
    max_retries = 5

    try:
        with open(video_path, 'rb') as f:
            while uploaded < file_size:
                chunk = f.read(chunk_size)
                if not chunk:
                    break

                end = min(uploaded + len(chunk), file_size)
                content_range = f'bytes {uploaded}-{end - 1}/{file_size}'

                for attempt in range(max_retries):
                    try:
                        resp = requests.put(
                            upload_url,
                            headers={
                                'Content-Range': content_range,
                                'Content-Type': 'video/mp4',
                            },
                            data=chunk,
                            timeout=60,
                        )

                        if resp.status_code in (200, 201):
                            # Upload complete
                            result = resp.json()
                            youtube_id = result.get('id', '')
                            log(f"YouTube: upload complete — ID: {youtube_id}")

                            _add_quota(1600)
                            _save_publication(track_id, youtube_id, metadata)

                            return youtube_id

                        elif resp.status_code == 308:
                            # Chunk accepted, continue
                            uploaded = end
                            break

                        elif resp.status_code in (500, 502, 503, 504):
                            # Retryable server error
                            log_warning(f"YouTube: server error {resp.status_code}, retry {attempt+1}")
                            time.sleep(2 ** attempt)
                            continue

                        else:
                            log_error(f"YouTube: upload chunk failed — {resp.status_code} {resp.text[:300]}")
                            return None

                    except requests.exceptions.Timeout:
                        log_warning(f"YouTube: chunk timeout, retry {attempt+1}")
                        time.sleep(2 ** attempt)
                        continue

                progress = (uploaded / file_size) * 100
                if progress % 25 < (chunk_size / file_size * 100):
                    log(f"YouTube: upload {progress:.0f}%")

    except Exception as e:
        log_error(f"YouTube: upload error — {e}")
        return None

    log_error("YouTube: upload finished without completion response")
    return None


def set_thumbnail(youtube_video_id, thumbnail_path):
    """Set custom thumbnail for a YouTube video. Costs 50 quota units."""
    import requests

    if not refresh_token_if_needed():
        return False

    if not os.path.exists(thumbnail_path):
        return False

    access_token = get_config('youtube_access_token')

    try:
        with open(thumbnail_path, 'rb') as f:
            resp = requests.post(
                f'https://www.googleapis.com/upload/youtube/v3/thumbnails/set'
                f'?videoId={youtube_video_id}',
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Content-Type': 'image/jpeg',
                },
                data=f.read(),
                timeout=30,
            )

        if resp.status_code == 200:
            _add_quota(50)
            log(f"YouTube: thumbnail set for {youtube_video_id}")
            return True
        else:
            log_error(f"YouTube: thumbnail failed — {resp.status_code}")
            return False

    except Exception as e:
        log_error(f"YouTube: thumbnail error — {e}")
        return False


def get_video_stats(youtube_video_id):
    """Get view/like counts for a video. Costs 1 quota unit."""
    import requests

    if not refresh_token_if_needed():
        return None

    access_token = get_config('youtube_access_token')

    try:
        resp = requests.get(
            'https://www.googleapis.com/youtube/v3/videos',
            params={
                'part': 'statistics',
                'id': youtube_video_id,
            },
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        )

        if resp.status_code == 200:
            _add_quota(1)
            items = resp.json().get('items', [])
            if items:
                stats = items[0].get('statistics', {})
                return {
                    'views': int(stats.get('viewCount', 0)),
                    'likes': int(stats.get('likeCount', 0)),
                    'comments': int(stats.get('commentCount', 0)),
                }

    except Exception as e:
        log_error(f"YouTube: stats error — {e}")

    return None


def _save_publication(track_id, youtube_video_id, metadata):
    """Save publication record in DB."""
    conn = get_db()

    # Find video_id for this track
    video = conn.execute('SELECT id FROM videos WHERE track_id = ? ORDER BY id DESC LIMIT 1', (track_id,)).fetchone()
    video_id = video['id'] if video else None

    # Find channel_id from track
    track = conn.execute('SELECT channel_id FROM tracks WHERE id = ?', (track_id,)).fetchone()
    channel_id = track['channel_id'] if track else None

    conn.execute('''INSERT INTO publications
        (video_id, channel_id, youtube_video_id, title, description, tags, published_at, status)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'published')''',
        (video_id, channel_id, youtube_video_id,
         metadata.get('title', ''), metadata.get('description', ''),
         ','.join(metadata.get('tags', [])))
    )

    # Update track status
    conn.execute("UPDATE tracks SET status = 'published' WHERE id = ?", (track_id,))
    conn.commit()

    log(f"Publication saved: track #{track_id} → youtube.com/watch?v={youtube_video_id}")
