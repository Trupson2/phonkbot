"""
Scheduler for PhonkBot — orchestrates pipeline runs.
Stub for Phase 1. Full implementation in Phase 6.
"""

import threading
import time
from modules.logger import log, log_error
from modules.database import get_db, get_config


def run_pipeline_once():
    """Run one full pipeline cycle: generate → review → (render → upload)."""
    from modules.database import get_db as _gdb

    conn = _gdb()

    # Check daily limit
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
        from modules.suno_api import generate_and_download
        track_id = generate_and_download()

        if track_id:
            # Send to Telegram for review
            from modules.telegram_bot import send_track_for_review
            sent = send_track_for_review(track_id)
            if sent:
                _log_pipeline('review', 'success', f'Track #{track_id} sent to Telegram', track_id)
            else:
                _log_pipeline('review', 'error', f'Failed to send track #{track_id} to Telegram', track_id)
        else:
            _log_pipeline('generate', 'error', 'Generation returned no track')

    except Exception as e:
        log_error(f"Pipeline error: {e}")
        _log_pipeline('generate', 'error', str(e)[:500])


def process_approved_track(track_id):
    """Process an approved track: render video → generate metadata → upload to YouTube."""
    log(f"Processing approved track #{track_id}...")
    _log_pipeline('render', 'info', f'Starting render for track #{track_id}', track_id)

    try:
        # Render video
        from modules.video_renderer import render_video
        video_path = render_video(track_id)

        if not video_path:
            _log_pipeline('render', 'error', 'Render failed', track_id)
            return

        _log_pipeline('render', 'success', f'Video rendered: {video_path}', track_id)

        # Generate metadata
        from modules.metadata_gen import generate_metadata
        metadata = generate_metadata(track_id)
        _log_pipeline('metadata', 'success', f'Metadata generated', track_id)

        # Upload to YouTube
        from modules.youtube_api import upload_video
        youtube_id = upload_video(track_id, video_path, metadata)

        if youtube_id:
            _log_pipeline('upload', 'success', f'Published: youtube.com/watch?v={youtube_id}', track_id)

            # Notify on Telegram
            from modules.telegram_bot import send_message
            send_message(f"Published: https://youtube.com/watch?v={youtube_id}")
        else:
            _log_pipeline('upload', 'error', 'Upload failed', track_id)

    except Exception as e:
        log_error(f"Post-approval pipeline error for track #{track_id}: {e}")
        _log_pipeline('pipeline', 'error', str(e)[:500], track_id)


def regenerate_track(track_id):
    """Regenerate a track with a similar prompt."""
    log(f"Regenerating track #{track_id}...")

    try:
        from modules.suno_api import regenerate_from_track
        new_track_id = regenerate_from_track(track_id)

        if new_track_id:
            from modules.telegram_bot import send_track_for_review
            send_track_for_review(new_track_id)
            _log_pipeline('regenerate', 'success', f'New track #{new_track_id} from #{track_id}', new_track_id)

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
