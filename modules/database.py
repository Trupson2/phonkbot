"""
Database module for PhonkBot - SQLite with WAL mode and connection pooling.
"""

import sqlite3
import os
import time
import json
import threading
from pathlib import Path

_APP_DIR = Path(__file__).parent.parent
DATABASE = str(_APP_DIR / 'phonkbot.db')

_connection_pool = {}
_pool_lock = threading.Lock()


def get_db():
    thread_id = threading.get_ident()

    with _pool_lock:
        if thread_id in _connection_pool:
            conn = _connection_pool[thread_id]
            try:
                conn.execute('SELECT 1')
                return conn
            except:
                del _connection_pool[thread_id]

    conn = sqlite3.connect(DATABASE, timeout=60.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA wal_autocheckpoint=100')
    conn.execute('PRAGMA cache_size=-32000')
    conn.execute('PRAGMA temp_store=MEMORY')

    with _pool_lock:
        _connection_pool[thread_id] = conn

    return conn


def close_connection_pool():
    with _pool_lock:
        for conn in _connection_pool.values():
            try:
                conn.close()
            except:
                pass
        _connection_pool.clear()


def retry_db_operation(func, max_retries=5, delay=0.5):
    for attempt in range(max_retries):
        try:
            return func()
        except sqlite3.OperationalError as e:
            if 'database is locked' in str(e) and attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
                continue
            raise
    return None


def get_config(key, default=''):
    try:
        conn = get_db()
        row = conn.execute('SELECT value FROM config WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default
    except:
        return default


def set_config(key, value):
    conn = get_db()
    conn.execute('''INSERT INTO config (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP''',
                 (key, value, value))
    conn.commit()


def init_db():
    conn = get_db()

    conn.execute('''CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        youtube_channel_id TEXT DEFAULT '',
        language TEXT DEFAULT 'en',
        genre TEXT DEFAULT 'phonk',
        status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS tracks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER DEFAULT NULL,
        title TEXT DEFAULT '',
        suno_prompt TEXT DEFAULT '',
        suno_style TEXT DEFAULT 'phonk',
        suno_id TEXT DEFAULT '',
        audio_url TEXT DEFAULT '',
        audio_path TEXT DEFAULT '',
        duration_seconds REAL DEFAULT 0,
        status TEXT DEFAULT 'pending',
        rating INTEGER DEFAULT NULL,
        rating_timestamp TIMESTAMP DEFAULT NULL,
        error_message TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (channel_id) REFERENCES channels(id)
    )''')

    # Add title column if missing (migration for existing DBs)
    try:
        conn.execute("SELECT title FROM tracks LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE tracks ADD COLUMN title TEXT DEFAULT ''")
        conn.commit()

    # Add model column if missing
    try:
        conn.execute("SELECT model FROM tracks LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE tracks ADD COLUMN model TEXT DEFAULT ''")
        conn.commit()

    # Add reject_reason column if missing
    try:
        conn.execute("SELECT reject_reason FROM tracks LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE tracks ADD COLUMN reject_reason TEXT DEFAULT NULL")
        conn.commit()

    # Add reject_reason to training_data if missing
    try:
        conn.execute("SELECT reject_reason FROM training_data LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE training_data ADD COLUMN reject_reason TEXT DEFAULT NULL")
        conn.commit()

    conn.execute('''CREATE TABLE IF NOT EXISTS videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id INTEGER NOT NULL,
        video_path TEXT DEFAULT '',
        thumbnail_path TEXT DEFAULT '',
        duration_seconds REAL DEFAULT 0,
        status TEXT DEFAULT 'pending',
        error_message TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (track_id) REFERENCES tracks(id)
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id INTEGER NOT NULL,
        channel_id INTEGER DEFAULT NULL,
        youtube_video_id TEXT DEFAULT '',
        title TEXT DEFAULT '',
        description TEXT DEFAULT '',
        tags TEXT DEFAULT '',
        scheduled_at TIMESTAMP DEFAULT NULL,
        published_at TIMESTAMP DEFAULT NULL,
        views_24h INTEGER DEFAULT 0,
        views_48h INTEGER DEFAULT 0,
        views_7d INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending',
        error_message TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (video_id) REFERENCES videos(id),
        FOREIGN KEY (channel_id) REFERENCES channels(id)
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS pipeline_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        step TEXT DEFAULT '',
        status TEXT DEFAULT 'info',
        message TEXT DEFAULT '',
        track_id INTEGER DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS training_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id INTEGER NOT NULL,
        suno_prompt TEXT DEFAULT '',
        suno_style TEXT DEFAULT '',
        audio_features TEXT DEFAULT '{}',
        rating INTEGER DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (track_id) REFERENCES tracks(id)
    )''')

    # Default config values
    defaults = {
        'suno_api_url': 'http://localhost:3000',
        'suno_cookie': '',
        'pipeline_interval_hours': '5',
        'max_tracks_per_day': '3',
        'default_language': 'en',
        'visualization_style': 'waveform',
        'telegram_bot_token': '',
        'telegram_chat_id': '',
        'youtube_client_id': '',
        'youtube_client_secret': '',
        'youtube_access_token': '',
        'youtube_refresh_token': '',
        'youtube_token_expiry': '',
        'youtube_quota_used': '0',
        'youtube_quota_reset_date': '',
        'gemini_api_key': '',
        'auto_publish': '0',
    }
    for key, value in defaults.items():
        conn.execute('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)', (key, value))

    conn.commit()
    print("[PhonkBot] Database initialized")
