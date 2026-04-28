"""Pipeline orchestrator — generation cycle + post-approval flow + scheduler."""

import os
import threading
import time

from modules.database import get_config, get_db
from modules.logger import log, log_error


def run_pipeline_once():
    """One full cycle: generate -> pre-render -> Telegram review."""
    conn = get_db()

    # Captcha freshness gate — Pi can't refresh on its own
    captcha_ts = get_config('suno_captcha_token_time', '0')
    try:
        age = time.time() - float(captcha_ts or 0)
    except ValueError:
        age = float('inf')
    max_age = int(get_config('captcha_max_age_seconds', '5400'))
    if age > max_age:
        log(f"Pipeline: captcha token stale ({age:.0f}s > {max_age}s), skipping")
        _log_pipeline('generate', 'skipped', f'Captcha token age {int(age)}s > limit {max_age}s')
        try:
            from modules.telegram import send_message
            send_message(
                f"PhonkBot: stary captcha token ({int(age/60)} min). "
                "Odpal: python tools/refresh_captcha.py"
            )
        except Exception:
            pass
        return

    today_count = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE DATE(created_at) = DATE('now')"
    ).fetchone()[0]
    max_per_day = int(get_config('max_tracks_per_day', '3'))
    if today_count >= max_per_day:
        log(f"Daily limit reached ({today_count}/{max_per_day}), skipping")
        _log_pipeline('generate', 'skipped', f'Daily limit {today_count}/{max_per_day}')
        return

    log("Pipeline: starting generation cycle...")
    _log_pipeline('generate', 'info', 'Starting new generation cycle')

    try:
        from modules.suno.client import generate_and_download
        track_id = generate_and_download()

        if not track_id:
            _log_pipeline('generate', 'error', 'Generation returned no track')
            return

        # Pre-render video so the Telegram review has a preview
        _log_pipeline('render', 'info', f'Pre-rendering video for track #{track_id}', track_id)
        try:
            from modules.video import render_video
            video_path = render_video(track_id)
            if video_path:
                _log_pipeline('render', 'success', f'Video pre-rendered: {video_path}', track_id)
            else:
                log_error(f"Pre-render failed for track #{track_id}, sending audio only")
                _log_pipeline('render', 'error', 'Pre-render failed, audio-only review', track_id)
        except Exception as e:
            log_error(f"Pre-render error: {e}")
            _log_pipeline('render', 'error', str(e)[:500], track_id)

        from modules.telegram import send_track_for_review
        sent = send_track_for_review(track_id)
        if sent:
            _log_pipeline('review', 'success', f'Track #{track_id} sent to Telegram', track_id)
        else:
            _log_pipeline('review', 'error', f'Failed to send track #{track_id} to Telegram', track_id)

    except Exception as e:
        log_error(f"Pipeline error: {e}")
        _log_pipeline('generate', 'error', str(e)[:500])


def process_approved_track(track_id):
    """Approved track -> render -> metadata -> YouTube upload."""
    log(f"Processing approved track #{track_id}...")
    _log_pipeline('render', 'info', f'Starting render for track #{track_id}', track_id)

    try:
        conn = get_db()
        existing_video = conn.execute(
            "SELECT video_path FROM videos WHERE track_id = ? AND status = 'ready' "
            "ORDER BY id DESC LIMIT 1",
            (track_id,)
        ).fetchone()

        if existing_video and existing_video['video_path'] and os.path.exists(existing_video['video_path']):
            video_path = existing_video['video_path']
            log(f"Track #{track_id}: using pre-rendered video")
            _log_pipeline('render', 'success', f'Using pre-rendered video: {video_path}', track_id)
        else:
            from modules.video import render_video
            video_path = render_video(track_id)
            if not video_path:
                _log_pipeline('render', 'error', 'Render failed', track_id)
                return
            _log_pipeline('render', 'success', f'Video rendered: {video_path}', track_id)

        from modules.pipeline.metadata import generate_metadata
        metadata = generate_metadata(track_id)
        _log_pipeline('metadata', 'success', 'Metadata generated', track_id)

        from modules.youtube import upload_video
        youtube_id = upload_video(track_id, video_path, metadata)

        if youtube_id:
            _log_pipeline('upload', 'success',
                          f'Published: youtube.com/watch?v={youtube_id}', track_id)
            from modules.telegram import send_message
            send_message(f"Published: https://youtube.com/watch?v={youtube_id}")
        else:
            _log_pipeline('upload', 'error', 'Upload failed', track_id)

    except Exception as e:
        log_error(f"Post-approval pipeline error for track #{track_id}: {e}")
        _log_pipeline('pipeline', 'error', str(e)[:500], track_id)


def regenerate_track(track_id):
    """Regenerate a track with a mutated prompt."""
    log(f"Regenerating track #{track_id}...")
    try:
        from modules.suno.client import regenerate_from_track
        new_track_id = regenerate_from_track(track_id)
        if new_track_id:
            from modules.telegram import send_track_for_review
            send_track_for_review(new_track_id)
            _log_pipeline('regenerate', 'success',
                          f'New track #{new_track_id} from #{track_id}', new_track_id)
    except Exception as e:
        log_error(f"Regeneration error: {e}")
        _log_pipeline('regenerate', 'error', str(e)[:500], track_id)


def start_scheduler():
    """Start the background scheduler daemon."""
    import schedule

    interval = int(get_config('pipeline_interval_hours', '5'))
    schedule.every(interval).hours.do(run_pipeline_once)
    log(f"Scheduler started: every {interval}h")

    def _loop():
        while True:
            schedule.run_pending()
            time.sleep(60)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def _log_pipeline(step, status, message, track_id=None):
    """Write to pipeline_logs table."""
    try:
        conn = get_db()
        conn.execute(
            'INSERT INTO pipeline_logs (step, status, message, track_id) VALUES (?, ?, ?, ?)',
            (step, status, message, track_id)
        )
        conn.commit()
    except:
        pass
