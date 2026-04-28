#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════╗
║     PhonkBot v0.1.0                      ║
║     AI Phonk Music Generator & Publisher ║
║     Suno → Telegram Review → YouTube     ║
╚══════════════════════════════════════════╝
"""

import sys
import os

# UTF-8 fix for Windows
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except:
        pass

from flask import Flask, render_template, request, redirect, url_for, jsonify, session
from flask_cors import CORS
from dotenv import load_dotenv

# Load .env
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# Init Flask
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'phonkbot-dev-key-change-me')
CORS(app)

VERSION = '0.1.0'

# Ensure output directories exist
for d in ['output/audio', 'output/videos', 'output/thumbnails', 'logs', 'models']:
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), d), exist_ok=True)

# Init database
from modules.database import init_db, get_db, get_config, set_config
from modules.logger import log, log_error

init_db()
log(f"PhonkBot v{VERSION} starting...")


# ═══════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════

@app.route('/output/<path:subpath>')
def output_file(subpath):
    """Serve generated audio/video/thumbnail files for the in-app preview.
    Restricted to subdirs we own — no traversal."""
    from flask import send_from_directory, abort
    parts = subpath.split('/', 1)
    if len(parts) != 2 or parts[0] not in ('audio', 'video', 'videos', 'thumbnails'):
        abort(404)
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output', parts[0])
    return send_from_directory(folder, parts[1])


@app.route('/')
def dashboard():
    conn = get_db()

    # Stats
    total_tracks = conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0]
    approved = conn.execute("SELECT COUNT(*) FROM tracks WHERE rating = 1").fetchone()[0]
    rejected = conn.execute("SELECT COUNT(*) FROM tracks WHERE rating = 0").fetchone()[0]
    pending_review = conn.execute("SELECT COUNT(*) FROM tracks WHERE status = 'ready_for_review'").fetchone()[0]
    published = conn.execute("SELECT COUNT(*) FROM publications WHERE status = 'published'").fetchone()[0]
    total_views = conn.execute("SELECT COALESCE(SUM(views_24h), 0) FROM publications").fetchone()[0]

    # Recent tracks
    recent_tracks = conn.execute('''
        SELECT t.*, p.youtube_video_id, p.title as pub_title, p.views_24h
        FROM tracks t
        LEFT JOIN videos v ON v.track_id = t.id
        LEFT JOIN publications p ON p.video_id = v.id
        ORDER BY t.created_at DESC LIMIT 20
    ''').fetchall()

    # Recent pipeline logs
    recent_logs = conn.execute('''
        SELECT * FROM pipeline_logs ORDER BY created_at DESC LIMIT 15
    ''').fetchall()

    # Config status
    suno_ok = bool(get_config('suno_cookie'))
    telegram_ok = bool(get_config('telegram_bot_token')) and bool(get_config('telegram_chat_id'))
    youtube_ok = bool(get_config('youtube_access_token'))
    auto_publish = get_config('auto_publish', '0') == '1'

    config_gemini = bool(get_config('gemini_api_key'))

    return render_template('phonkbot/dashboard.html',
        version=VERSION,
        config_gemini=config_gemini,
        stats={
            'total_tracks': total_tracks,
            'approved': approved,
            'rejected': rejected,
            'pending_review': pending_review,
            'published': published,
            'total_views': total_views,
            'approval_rate': f"{(approved / max(approved + rejected, 1)) * 100:.0f}",
        },
        recent_tracks=recent_tracks,
        recent_logs=recent_logs,
        suno_ok=suno_ok,
        telegram_ok=telegram_ok,
        youtube_ok=youtube_ok,
        auto_publish=auto_publish,
        suno_model=get_config('suno_model', 'chirp-v4'),
    )


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        fields = [
            'suno_api_url', 'suno_cookie', 'suno_model',
            'telegram_bot_token', 'telegram_chat_id',
            'youtube_client_id', 'youtube_client_secret',
            'gemini_api_key',
            'pipeline_interval_hours', 'max_tracks_per_day',
            'default_language', 'visualization_style',
            'auto_publish',
        ]
        for f in fields:
            val = request.form.get(f, '')
            if f == 'auto_publish':
                val = '1' if val else '0'
            set_config(f, val)
        return redirect(url_for('settings'))

    config = {}
    for key in ['suno_api_url', 'suno_cookie', 'suno_model',
                'telegram_bot_token', 'telegram_chat_id',
                'youtube_client_id', 'youtube_client_secret', 'gemini_api_key',
                'pipeline_interval_hours', 'max_tracks_per_day',
                'default_language', 'visualization_style', 'auto_publish']:
        config[key] = get_config(key, '')

    return render_template('phonkbot/settings.html', version=VERSION, config=config)


@app.route('/tracks')
def tracks():
    conn = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = 50
    offset = (page - 1) * per_page

    tracks_list = conn.execute('''
        SELECT t.*, p.youtube_video_id, p.title as pub_title
        FROM tracks t
        LEFT JOIN videos v ON v.track_id = t.id
        LEFT JOIN publications p ON p.video_id = v.id
        ORDER BY t.created_at DESC
        LIMIT ? OFFSET ?
    ''', (per_page, offset)).fetchall()

    total = conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0]

    return render_template('phonkbot/tracks.html',
        version=VERSION, tracks=tracks_list,
        page=page, total=total, per_page=per_page)


@app.route('/tracks/<int:track_id>')
def track_detail(track_id):
    """Single-track view with audio/video preview + approve/reject/regen buttons.
    Replaces Telegram-only review for users that prefer the web UI."""
    conn = get_db()
    track = conn.execute('SELECT * FROM tracks WHERE id = ?', (track_id,)).fetchone()
    if not track:
        return 'Track not found', 404

    video = conn.execute(
        "SELECT * FROM videos WHERE track_id = ? ORDER BY id DESC LIMIT 1",
        (track_id,)
    ).fetchone()

    sibling = None
    if track['sibling_track_id']:
        sibling = conn.execute(
            'SELECT id, title, duration_seconds, status FROM tracks WHERE id = ?',
            (track['sibling_track_id'],)
        ).fetchone()
    else:
        sibling = conn.execute(
            'SELECT id, title, duration_seconds, status FROM tracks WHERE sibling_track_id = ?',
            (track_id,)
        ).fetchone()

    publication = conn.execute(
        '''SELECT p.* FROM publications p
           JOIN videos v ON v.id = p.video_id
           WHERE v.track_id = ? ORDER BY p.id DESC LIMIT 1''',
        (track_id,)
    ).fetchone()

    return render_template(
        'phonkbot/track_detail.html',
        version=VERSION,
        track=track, video=video, sibling=sibling, publication=publication,
    )


@app.route('/tracks/<int:track_id>/approve', methods=['POST'])
def track_approve(track_id):
    """UI-side approval — same effect as the Telegram callback."""
    import threading
    conn = get_db()
    track = conn.execute('SELECT id FROM tracks WHERE id = ?', (track_id,)).fetchone()
    if not track:
        return jsonify({'status': 'error', 'message': 'track not found'}), 404

    conn.execute('''UPDATE tracks SET status='approved', rating=1,
                    rating_timestamp=CURRENT_TIMESTAMP WHERE id=?''', (track_id,))
    conn.commit()

    try:
        from modules.pipeline.scheduler import process_approved_track
        threading.Thread(target=process_approved_track, args=(track_id,), daemon=True).start()
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

    return jsonify({'status': 'ok'})


@app.route('/tracks/<int:track_id>/reject', methods=['POST'])
def track_reject(track_id):
    """UI-side rejection. Optional reject_reason in body."""
    reason = (request.json or {}).get('reason') if request.is_json else request.form.get('reason', '')
    conn = get_db()
    track = conn.execute('SELECT suno_prompt, suno_style FROM tracks WHERE id = ?', (track_id,)).fetchone()
    if not track:
        return jsonify({'status': 'error', 'message': 'track not found'}), 404

    conn.execute('''UPDATE tracks SET status='rejected', rating=0,
                    rating_timestamp=CURRENT_TIMESTAMP, reject_reason=? WHERE id=?''',
                 (reason or None, track_id))

    # Also save to training_data so the rejection feeds the learning loop
    conn.execute('''INSERT INTO training_data (track_id, suno_prompt, suno_style, rating, reject_reason)
                    VALUES (?, ?, ?, 0, ?)''',
                 (track_id, track['suno_prompt'], track['suno_style'], reason or None))
    conn.commit()
    return jsonify({'status': 'ok'})


@app.route('/tracks/<int:track_id>/regen', methods=['POST'])
def track_regenerate(track_id):
    """Trigger regenerate-from-track flow."""
    import threading
    try:
        from modules.pipeline.scheduler import regenerate_track
        threading.Thread(target=regenerate_track, args=(track_id,), daemon=True).start()
        return jsonify({'status': 'ok', 'message': f'Regeneration started for #{track_id}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/stats')
def api_stats():
    conn = get_db()
    return jsonify({
        'total_tracks': conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0],
        'published': conn.execute("SELECT COUNT(*) FROM publications WHERE status = 'published'").fetchone()[0],
        'pending_review': conn.execute("SELECT COUNT(*) FROM tracks WHERE status = 'ready_for_review'").fetchone()[0],
        'total_views': conn.execute("SELECT COALESCE(SUM(views_24h), 0) FROM publications").fetchone()[0],
    })


@app.route('/api/set-model', methods=['POST'])
def api_set_model():
    """Set Suno model from dashboard."""
    data = request.get_json(silent=True) or {}
    model = data.get('model', 'chirp-v4')
    set_config('suno_model', model)
    return jsonify({'status': 'ok', 'model': model})


@app.route('/api/track/status')
def api_track_status():
    """Get latest track status for live progress."""
    conn = get_db()
    track = conn.execute('''SELECT id, title, status, suno_prompt, duration_seconds, created_at
                           FROM tracks ORDER BY id DESC LIMIT 1''').fetchone()
    if not track:
        return jsonify({'active': False})

    status = track['status']
    created = track['created_at'] or ''

    # Calculate elapsed time
    elapsed = 0
    try:
        from datetime import datetime
        if created:
            ct = datetime.fromisoformat(created)
            elapsed = int((datetime.now() - ct).total_seconds())
    except Exception:
        pass

    phases = {
        'generating': {'label': 'Generowanie muzyki...', 'icon': 'music_note', 'progress': 20},
        'downloading': {'label': 'Pobieranie audio...', 'icon': 'download', 'progress': 50},
        'ready_for_review': {'label': 'Renderowanie wideo...', 'icon': 'movie', 'progress': 70},
        'rendering': {'label': 'Renderowanie wideo...', 'icon': 'movie', 'progress': 75},
        'uploading': {'label': 'Upload na YouTube...', 'icon': 'upload', 'progress': 90},
        'published': {'label': 'Opublikowany!', 'icon': 'check_circle', 'progress': 100},
        'approved': {'label': 'Zatwierdzony', 'icon': 'thumb_up', 'progress': 80},
        'failed': {'label': 'Blad generacji', 'icon': 'error', 'progress': 0},
    }

    phase = phases.get(status, {'label': status, 'icon': 'hourglass_empty', 'progress': 10})
    active = status in ('generating', 'downloading', 'rendering', 'uploading')

    return jsonify({
        'active': active,
        'track_id': track['id'],
        'title': track['title'] or f"Track #{track['id']}",
        'status': status,
        'label': phase['label'],
        'icon': phase['icon'],
        'progress': phase['progress'],
        'elapsed': elapsed,
        'prompt': (track['suno_prompt'] or '')[:80],
    })


@app.route('/api/pipeline/run', methods=['POST'])
def api_pipeline_run():
    """Manually trigger one pipeline cycle."""
    try:
        from modules.pipeline.scheduler import run_pipeline_once
        import threading
        t = threading.Thread(target=run_pipeline_once, daemon=True)
        t.start()
        return jsonify({'status': 'ok', 'message': 'Pipeline started'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ═══════════════════════════════════════
# ADMIN — token push (Pi receives fresh JWT/captcha from Windows)
# ═══════════════════════════════════════

_TOKEN_PUSH_FIELDS = (
    'suno_jwt', 'suno_refresh_token', 'suno_session_id',
    'suno_cookie', 'suno_browser_token',
    'suno_captcha_token', 'suno_captcha_token_time',
)

def _is_lan_ip(ip):
    """Cheap private-network check — defense in depth alongside the secret."""
    if not ip:
        return False
    if ip in ('127.0.0.1', '::1', 'localhost'):
        return True
    return (
        ip.startswith('192.168.') or ip.startswith('10.')
        or any(ip.startswith(f'172.{n}.') for n in range(16, 32))
    )


@app.route('/api/admin/push-tokens', methods=['POST'])
def api_push_tokens():
    """Receive Suno tokens from a trusted desktop running tools/refresh_captcha.py.

    Auth: X-Auth-Secret header == config.admin_push_secret. Also LAN-only.
    """
    secret_header = request.headers.get('X-Auth-Secret', '')
    secret_expected = get_config('admin_push_secret', '')
    if not secret_expected or secret_header != secret_expected:
        return jsonify({'status': 'error', 'message': 'forbidden'}), 403

    remote = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    if not _is_lan_ip(remote):
        return jsonify({'status': 'error', 'message': 'lan-only'}), 403

    payload = request.get_json(silent=True) or {}
    pushed = []
    for k in _TOKEN_PUSH_FIELDS:
        if k in payload and isinstance(payload[k], str) and payload[k]:
            set_config(k, payload[k])
            pushed.append(k)

    # Stamp captcha time if token was pushed without explicit ts
    if 'suno_captcha_token' in pushed and 'suno_captcha_token_time' not in pushed:
        import time as _t
        set_config('suno_captcha_token_time', str(_t.time()))
        pushed.append('suno_captcha_token_time')

    if not pushed:
        return jsonify({'status': 'error', 'message': 'no recognized fields'}), 400

    conn = get_db()
    conn.execute(
        'INSERT INTO token_push_log (source_ip, keys_pushed) VALUES (?, ?)',
        (remote, ','.join(pushed)),
    )
    conn.commit()
    log(f"Admin: tokens pushed from {remote}: {','.join(pushed)}")
    return jsonify({'status': 'ok', 'pushed': pushed})


@app.route('/api/admin/backup', methods=['POST'])
def api_backup():
    """Trigger a full bundle backup. Returns the filename (not the bytes)."""
    secret_header = request.headers.get('X-Auth-Secret', '')
    if secret_header != get_config('admin_push_secret', ''):
        return jsonify({'status': 'error', 'message': 'forbidden'}), 403
    try:
        from modules.backup import full_bundle
        path = full_bundle()
        if not path:
            return jsonify({'status': 'error', 'message': 'bundle failed'}), 500
        import os as _os
        return jsonify({
            'status': 'ok',
            'filename': _os.path.basename(path),
            'size_bytes': _os.path.getsize(path),
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ═══════════════════════════════════════
# WEBHOOK — Telegram (optional alternative to long-polling)
# ═══════════════════════════════════════

@app.route('/webhooks/telegram', methods=['POST'])
def webhook_telegram():
    """Telegram webhook receiver. Only active if telegram_webhook_enabled=1."""
    if get_config('telegram_webhook_enabled', '0') != '1':
        return 'webhook disabled', 403
    try:
        from modules.telegram import handle_update
        handle_update(request.get_json(force=True, silent=True) or {})
    except Exception as e:
        log_error(f"Telegram webhook error: {e}")
    return 'ok'


# ═══════════════════════════════════════
# YOUTUBE OAUTH (under /webhooks/ for clarity)
# ═══════════════════════════════════════

@app.route('/webhooks/youtube/auth')
def youtube_auth():
    from modules.youtube import get_oauth_url
    url = get_oauth_url()
    if not url:
        return 'YouTube Client ID not configured. Go to Settings first.', 400
    return redirect(url)

@app.route('/webhooks/youtube/callback')
def youtube_callback():
    code = request.args.get('code')
    if not code:
        return 'No authorization code received.', 400
    from modules.youtube import exchange_code
    if exchange_code(code):
        return redirect(url_for('settings'))
    return 'OAuth failed. Check logs.', 500

@app.route('/webhooks/youtube/status')
def youtube_status():
    from modules.youtube import refresh_token_if_needed
    authorized = refresh_token_if_needed()
    quota_used = get_config('youtube_quota_used', '0')
    return jsonify({
        'authorized': authorized,
        'quota_used': int(quota_used),
        'quota_limit': 10000,
    })


# ═══════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════

if __name__ == '__main__':
    # Start Telegram bot in background
    telegram_token = get_config('telegram_bot_token')
    if telegram_token:
        try:
            from modules.telegram import start_telegram_bot
            start_telegram_bot(telegram_token)
            log("Telegram bot started")
        except Exception as e:
            log_error(f"Telegram bot failed to start: {e}")
    else:
        log("Telegram bot not configured - skipping")

    # Start Suno keep-alive (JWT auto-refresh — cascade)
    try:
        from modules.suno.auth import start_keep_alive
        start_keep_alive()
    except Exception as e:
        log_error(f"Suno keep-alive failed to start: {e}")

    # Start Suno auto-token (Playwright browser refresh every 4h)
    try:
        from modules.suno.autotoken import start_auto_refresh
        start_auto_refresh()
    except Exception as e:
        log_error(f"Suno auto-token failed to start: {e}")

    # Start daily DB backup snapshot
    try:
        from modules.backup import start_daily_backup
        start_daily_backup()
    except Exception as e:
        log_error(f"Backup daemon failed to start: {e}")

    # Start scheduler in background
    auto_publish = get_config('auto_publish', '0')
    if auto_publish == '1':
        try:
            from modules.pipeline.scheduler import start_scheduler
            start_scheduler()
            log("Scheduler started")
        except Exception as e:
            log_error(f"Scheduler failed to start: {e}")

    log(f"PhonkBot v{VERSION} running on http://0.0.0.0:5001")

    # Use waitress in production, Flask dev server otherwise
    try:
        from waitress import serve
        serve(app, host='0.0.0.0', port=5001, threads=4)
    except ImportError:
        app.run(host='0.0.0.0', port=5001, debug=True)
