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


def send_track_for_review(track_id):
    """Send a track audio to Telegram with inline review buttons."""
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

    # Send audio with caption
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
            from modules.scheduler import process_approved_track
            t = threading.Thread(target=process_approved_track, args=(track_id,), daemon=True)
            t.start()
        except Exception as e:
            log_error(f"Failed to start post-approval pipeline: {e}")

    elif action == 'reject':
        conn.execute("""UPDATE tracks SET status = 'rejected', rating = 0,
                        rating_timestamp = CURRENT_TIMESTAMP WHERE id = ?""", (track_id,))
        conn.commit()

        _save_training_data(track_id, 0)

        log(f"Track #{track_id} REJECTED via Telegram")
        send_message(f"Track #{track_id} rejected. Data saved for training.")

    elif action == 'regen':
        conn.execute("UPDATE tracks SET status = 'regenerate' WHERE id = ?", (track_id,))
        conn.commit()

        log(f"Track #{track_id} marked for REGENERATION")
        send_message(f"Track #{track_id} queued for regeneration with similar prompt.")

        try:
            from modules.scheduler import regenerate_track
            t = threading.Thread(target=regenerate_track, args=(track_id,), daemon=True)
            t.start()
        except Exception as e:
            log_error(f"Failed to start regeneration: {e}")


def _save_training_data(track_id, rating):
    """Save track data for quality model training."""
    conn = get_db()
    track = conn.execute('SELECT * FROM tracks WHERE id = ?', (track_id,)).fetchone()
    if not track:
        return

    # Extract audio features if available
    audio_features = '{}'
    if track['audio_path'] and os.path.exists(track['audio_path']):
        try:
            from modules.video_renderer import get_audio_features
            audio_features = get_audio_features(track['audio_path'])
        except:
            pass

    conn.execute('''INSERT INTO training_data (track_id, suno_prompt, suno_style, audio_features, rating)
                    VALUES (?, ?, ?, ?, ?)''',
                 (track_id, track['suno_prompt'], track['suno_style'], audio_features, rating))
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
                            from modules.scheduler import run_pipeline_once
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
