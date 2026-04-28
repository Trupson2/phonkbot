"""
Telegram Bot for PhonkBot — sends generated tracks for review.
User rates with inline buttons: Approve / Reject / Regenerate.
"""

import os
import threading
import requests
from modules.logger import log, log_error
from modules.database import get_db, get_config, set_config

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Pending reject reasons: {chat_id: track_id} — waiting for user to type reason
_pending_reject = {}


def send_track_for_review(track_id):
    """Send a track audio + video preview to Telegram with inline review buttons."""
    token = get_config('telegram_bot_token')
    chat_id = get_config('telegram_chat_id')
    if not token or not chat_id:
        log_error("Telegram not configured, can't send review")
        return False

    conn = get_db()
    track = conn.execute('SELECT * FROM tracks WHERE id = ?', (track_id,)).fetchone()
    if not track:
        log_error(f"Track {track_id} not found")
        return False

    audio_path = track['audio_path']
    if not audio_path or not os.path.exists(audio_path):
        log_error(f"Audio file not found: {audio_path}")
        return False

    # Check if video/thumbnail exists for preview
    video = conn.execute(
        'SELECT * FROM videos WHERE track_id = ? ORDER BY id DESC LIMIT 1', (track_id,)
    ).fetchone()

    # Send video preview or thumbnail first (before audio + buttons)
    if video:
        _send_video_preview(token, chat_id, track_id, video)

    # Send audio with caption and review buttons
    caption = (
        f"Track #{track_id}\n"
        f"Prompt: {track['suno_prompt'][:200]}\n"
        f"Style: {track['suno_style']}\n"
        f"Duration: {track['duration_seconds']:.0f}s"
    )

    # Inline keyboard: Approve / Reject / Regenerate
    keyboard = {
        "inline_keyboard": [[
            {"text": "\U0001f44d Publikuj", "callback_data": f"approve_{track_id}"},
            {"text": "\U0001f44e Odrzuc", "callback_data": f"reject_{track_id}"},
            {"text": "\U0001f504 Regeneruj", "callback_data": f"regen_{track_id}"},
        ]]
    }

    try:
        with open(audio_path, 'rb') as f:
            resp = requests.post(
                f'https://api.telegram.org/bot{token}/sendAudio',
                data={
                    'chat_id': chat_id,
                    'caption': caption,
                    'reply_markup': str(keyboard).replace("'", '"'),
                },
                files={'audio': (os.path.basename(audio_path), f, 'audio/mpeg')},
                timeout=60,
            )

        if resp.status_code == 200:
            conn.execute("UPDATE tracks SET status = 'ready_for_review' WHERE id = ?", (track_id,))
            conn.commit()
            log(f"Track #{track_id} sent to Telegram for review")
            return True
        else:
            log_error(f"Telegram API error: {resp.status_code} — {resp.text[:200]}")
            return False

    except Exception as e:
        log_error(f"Failed to send track to Telegram: {e}")
        return False


def _send_video_preview(token, chat_id, track_id, video):
    """Send video preview (short clip or thumbnail) to Telegram before review buttons."""
    # Try thumbnail first (fast, small)
    thumb_path = video['thumbnail_path'] if video and video['thumbnail_path'] else None
    if thumb_path and os.path.exists(thumb_path):
        try:
            with open(thumb_path, 'rb') as f:
                resp = requests.post(
                    f'https://api.telegram.org/bot{token}/sendPhoto',
                    data={
                        'chat_id': chat_id,
                        'caption': f"Podglad wizualizacji - Track #{track_id}",
                    },
                    files={'photo': (f'thumb_{track_id}.jpg', f, 'image/jpeg')},
                    timeout=30,
                )
            if resp.status_code == 200:
                log(f"Telegram: thumbnail preview sent for track #{track_id}")
                return True
        except Exception as e:
            log_error(f"Telegram thumbnail send error: {e}")

    # Try sending short video preview (first 15s, compressed)
    video_path = video['video_path'] if video and video['video_path'] else None
    if video_path and os.path.exists(video_path):
        preview_path = video_path.replace('.mp4', '_preview.mp4')
        try:
            import subprocess
            # Extract first 15s at lower quality for preview
            result = subprocess.run([
                'ffmpeg', '-i', video_path,
                '-t', '15',
                '-vf', 'scale=640:360',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
                '-c:a', 'aac', '-b:a', '96k',
                '-y', preview_path,
            ], capture_output=True, text=True, timeout=60)

            if result.returncode == 0 and os.path.exists(preview_path):
                file_size = os.path.getsize(preview_path)
                if file_size < 50 * 1024 * 1024:  # Telegram limit 50MB
                    with open(preview_path, 'rb') as f:
                        resp = requests.post(
                            f'https://api.telegram.org/bot{token}/sendVideo',
                            data={
                                'chat_id': chat_id,
                                'caption': f"Podglad wizualizacji (15s) - Track #{track_id}",
                                'supports_streaming': 'true',
                            },
                            files={'video': (f'preview_{track_id}.mp4', f, 'video/mp4')},
                            timeout=120,
                        )
                    if resp.status_code == 200:
                        log(f"Telegram: video preview sent for track #{track_id}")
                    else:
                        log_error(f"Telegram video preview error: {resp.status_code}")

                # Clean up preview file
                try:
                    os.remove(preview_path)
                except:
                    pass

        except Exception as e:
            log_error(f"Telegram video preview error: {e}")

    return False


def send_message(text):
    """Send a simple text message to the configured Telegram chat."""
    token = get_config('telegram_bot_token')
    chat_id = get_config('telegram_chat_id')
    if not token or not chat_id:
        return False

    try:
        resp = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
            timeout=10,
        )
        return resp.status_code == 200
    except:
        return False


def handle_callback(data):
    """Process inline button callback from Telegram."""
    parts = data.split('_', 1)
    if len(parts) != 2:
        return

    action, track_id_str = parts
    try:
        track_id = int(track_id_str)
    except ValueError:
        return

    conn = get_db()
    track = conn.execute('SELECT * FROM tracks WHERE id = ?', (track_id,)).fetchone()
    if not track:
        return

    if action == 'approve':
        conn.execute("""UPDATE tracks SET status = 'approved', rating = 1,
                        rating_timestamp = CURRENT_TIMESTAMP WHERE id = ?""", (track_id,))
        conn.commit()

        # Save training data
        _save_training_data(track_id, 1)

        log(f"Track #{track_id} APPROVED via Telegram")
        send_message(f"Track #{track_id} approved — rendering & uploading...")

        # Trigger render + upload pipeline for this track
        try:
            from modules.pipeline.scheduler import process_approved_track
            t = threading.Thread(target=process_approved_track, args=(track_id,), daemon=True)
            t.start()
        except Exception as e:
            log_error(f"Failed to start post-approval pipeline: {e}")

    elif action == 'reject':
        # Ask for reason instead of rejecting immediately
        chat_id = get_config('telegram_chat_id')
        if chat_id:
            _pending_reject[str(chat_id)] = track_id
            send_message(f"Track #{track_id} — dlaczego odrzucasz? Napisz powod:")

    elif action == 'regen':
        conn.execute("UPDATE tracks SET status = 'regenerate' WHERE id = ?", (track_id,))
        conn.commit()

        log(f"Track #{track_id} marked for REGENERATION")
        send_message(f"Track #{track_id} queued for regeneration with similar prompt.")

        try:
            from modules.pipeline.scheduler import regenerate_track
            t = threading.Thread(target=regenerate_track, args=(track_id,), daemon=True)
            t.start()
        except Exception as e:
            log_error(f"Failed to start regeneration: {e}")


def _do_reject(track_id, reason):
    """Reject a track with a reason from the user."""
    conn = get_db()
    conn.execute("""UPDATE tracks SET status = 'rejected', rating = 0,
                    rating_timestamp = CURRENT_TIMESTAMP, reject_reason = ?
                    WHERE id = ?""", (reason, track_id))
    conn.commit()

    _save_training_data(track_id, 0, reason)

    log(f"Track #{track_id} REJECTED — reason: {reason}")
    send_message(f"Track #{track_id} odrzucony.\nPowod: _{reason}_")


def _save_training_data(track_id, rating, reject_reason=None):
    """Save track data for quality model training."""
    conn = get_db()
    track = conn.execute('SELECT * FROM tracks WHERE id = ?', (track_id,)).fetchone()
    if not track:
        return

    # Extract audio features if available
    audio_features = '{}'
    if track['audio_path'] and os.path.exists(track['audio_path']):
        try:
            from modules.video import get_audio_features
            audio_features = get_audio_features(track['audio_path'])
        except:
            pass

    conn.execute('''INSERT INTO training_data (track_id, suno_prompt, suno_style, audio_features, rating, reject_reason)
                    VALUES (?, ?, ?, ?, ?, ?)''',
                 (track_id, track['suno_prompt'], track['suno_style'], audio_features, rating, reject_reason))
    conn.commit()


def _polling_loop(token):
    """Long-polling loop for Telegram updates."""
    offset = 0
    log("Telegram bot polling started")

    while True:
        try:
            resp = requests.get(
                f'https://api.telegram.org/bot{token}/getUpdates',
                params={'offset': offset, 'timeout': 30},
                timeout=35,
            )
            if resp.status_code != 200:
                continue

            data = resp.json()
            if not data.get('ok'):
                continue

            for update in data.get('result', []):
                offset = update['update_id'] + 1

                # Handle callback queries (inline buttons)
                if 'callback_query' in update:
                    cb = update['callback_query']
                    handle_callback(cb.get('data', ''))

                    # Answer callback to remove loading indicator
                    requests.post(
                        f'https://api.telegram.org/bot{token}/answerCallbackQuery',
                        json={'callback_query_id': cb['id']},
                        timeout=5,
                    )

                # Handle text commands
                elif 'message' in update:
                    msg = update['message']
                    text = msg.get('text', '')
                    sender_chat_id = str(msg.get('chat', {}).get('id', ''))

                    # Check if user is providing a reject reason
                    if sender_chat_id in _pending_reject and text and not text.startswith('/'):
                        track_id = _pending_reject.pop(sender_chat_id)
                        _do_reject(track_id, text)
                        continue

                    if text == '/status':
                        from modules.database import get_db as _gdb
                        c = _gdb()
                        pending = c.execute("SELECT COUNT(*) FROM tracks WHERE status = 'ready_for_review'").fetchone()[0]
                        published = c.execute("SELECT COUNT(*) FROM publications WHERE status = 'published'").fetchone()[0]
                        total = c.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
                        send_message(
                            f"*PhonkBot Status*\n"
                            f"Total tracks: {total}\n"
                            f"Pending review: {pending}\n"
                            f"Published: {published}"
                        )
                    elif text == '/generate':
                        send_message("Generating new track...")
                        try:
                            from modules.pipeline.scheduler import run_pipeline_once
                            t = threading.Thread(target=run_pipeline_once, daemon=True)
                            t.start()
                        except Exception as e:
                            send_message(f"Error: {e}")

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log_error(f"Telegram polling error: {e}")
            import time
            time.sleep(5)


def start_telegram_bot(token):
    """Start Telegram bot in background thread with long-polling."""
    t = threading.Thread(target=_polling_loop, args=(token,), daemon=True)
    t.start()
    return t


def handle_update(update):
    """Process a single Telegram update dict (used by webhook receiver).

    Same logic as the long-polling loop but stateless — Flask calls this
    per-request when telegram_webhook_enabled=1. Long-polling and webhook
    are mutually exclusive in practice (Telegram refuses both at once).
    """
    if not isinstance(update, dict):
        return

    if 'callback_query' in update:
        cb = update['callback_query']
        handle_callback(cb.get('data', ''))
        token = get_config('telegram_bot_token', '')
        if token:
            try:
                requests.post(
                    f'https://api.telegram.org/bot{token}/answerCallbackQuery',
                    json={'callback_query_id': cb.get('id')},
                    timeout=5,
                )
            except Exception:
                pass
        return

    if 'message' in update:
        msg = update['message']
        text = msg.get('text', '')
        sender_chat_id = str(msg.get('chat', {}).get('id', ''))

        if sender_chat_id in _pending_reject and text and not text.startswith('/'):
            track_id = _pending_reject.pop(sender_chat_id)
            _do_reject(track_id, text)
            return

        if text == '/status':
            from modules.database import get_db as _gdb
            c = _gdb()
            pending = c.execute("SELECT COUNT(*) FROM tracks WHERE status = 'ready_for_review'").fetchone()[0]
            published = c.execute("SELECT COUNT(*) FROM publications WHERE status = 'published'").fetchone()[0]
            total = c.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            send_message(
                f"*PhonkBot Status*\nTotal tracks: {total}\n"
                f"Pending review: {pending}\nPublished: {published}"
            )
        elif text == '/generate':
            send_message("Generating new track...")
            try:
                from modules.pipeline.scheduler import run_pipeline_once
                threading.Thread(target=run_pipeline_once, daemon=True).start()
            except Exception as e:
                send_message(f"Error: {e}")
