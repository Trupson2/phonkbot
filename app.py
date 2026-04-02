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

@app.route('/kiosk-mockup')
def kiosk_mockup():
    return render_template('phonkbot/kiosk_mockup.html')

@app.route('/launcher')
def launcher():
    return render_template('phonkbot/launcher.html', version=VERSION, kiosk='')

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
    )


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        fields = [
            'suno_api_url', 'suno_cookie',
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
    for key in ['suno_api_url', 'suno_cookie', 'telegram_bot_token', 'telegram_chat_id',
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


@app.route('/api/stats')
def api_stats():
    conn = get_db()
    return jsonify({
        'total_tracks': conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0],
        'published': conn.execute("SELECT COUNT(*) FROM publications WHERE status = 'published'").fetchone()[0],
        'pending_review': conn.execute("SELECT COUNT(*) FROM tracks WHERE status = 'ready_for_review'").fetchone()[0],
        'total_views': conn.execute("SELECT COALESCE(SUM(views_24h), 0) FROM publications").fetchone()[0],
    })


@app.route('/api/pipeline/run', methods=['POST'])
def api_pipeline_run():
    """Manually trigger one pipeline cycle."""
    try:
        from modules.scheduler import run_pipeline_once
        import threading
        t = threading.Thread(target=run_pipeline_once, daemon=True)
        t.start()
        return jsonify({'status': 'ok', 'message': 'Pipeline started'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ═══════════════════════════════════════
# YOUTUBE OAUTH
# ═══════════════════════════════════════

@app.route('/youtube/auth')
def youtube_auth():
    from modules.youtube_api import get_oauth_url
    url = get_oauth_url()
    if not url:
        return 'YouTube Client ID not configured. Go to Settings first.', 400
    return redirect(url)

@app.route('/youtube/callback')
def youtube_callback():
    code = request.args.get('code')
    if not code:
        return 'No authorization code received.', 400
    from modules.youtube_api import exchange_code
    if exchange_code(code):
        return redirect(url_for('settings'))
    return 'OAuth failed. Check logs.', 500

@app.route('/youtube/status')
def youtube_status():
    from modules.youtube_api import refresh_token_if_needed
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
            from modules.telegram_bot import start_telegram_bot
            start_telegram_bot(telegram_token)
            log("Telegram bot started")
        except Exception as e:
            log_error(f"Telegram bot failed to start: {e}")
    else:
        log("Telegram bot not configured - skipping")

    # Start scheduler in background
    auto_publish = get_config('auto_publish', '0')
    if auto_publish == '1':
        try:
            from modules.scheduler import start_scheduler
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
