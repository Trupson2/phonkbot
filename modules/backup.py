"""DB snapshots + on-demand full bundle (DB + recent output + logs).

Daily snapshot is cheap (DB is a few MB) — good safety net. Full bundle is
manual via /api/admin/backup since it can be huge.
"""

import os
import shutil
import sqlite3
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from modules.database import DATABASE, get_config
from modules.logger import log, log_error

_APP_DIR = Path(__file__).parent.parent
BACKUP_DIR = _APP_DIR / 'backups'
BACKUP_DIR.mkdir(exist_ok=True)

DAILY_INTERVAL = 24 * 3600


def snapshot_db():
    """Copy DB via SQLite VACUUM INTO. Returns path or None."""
    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        dest = BACKUP_DIR / f'db_{stamp}.db'

        src = sqlite3.connect(DATABASE)
        try:
            # VACUUM INTO is the canonical online-backup primitive
            src.execute(f"VACUUM INTO '{dest.as_posix()}'")
        finally:
            src.close()

        size_kb = dest.stat().st_size // 1024
        log(f"Backup: DB snapshot {dest.name} ({size_kb}KB)")
        prune_old_snapshots()
        return str(dest)
    except Exception as e:
        log_error(f"Backup: snapshot failed — {e}")
        return None


def prune_old_snapshots():
    """Keep only the last N days of `db_*.db` files."""
    try:
        retention_days = int(get_config('backup_retention_days', '14'))
    except ValueError:
        retention_days = 14
    cutoff = time.time() - retention_days * 86400

    for f in BACKUP_DIR.glob('db_*.db'):
        if f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                log(f"Backup: pruned old snapshot {f.name}")
            except OSError as e:
                log_error(f"Backup: prune failed for {f.name} — {e}")


def full_bundle(include_audio_days=7):
    """Build a zip with DB + last N days of output/audio + recent logs.

    Returns the bundle path. Caller serves it (or scp's it) — we don't stream
    huge files through Flask.
    """
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = BACKUP_DIR / f'full_{stamp}.zip'

    db_snap = snapshot_db()
    cutoff = time.time() - include_audio_days * 86400

    try:
        with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as zf:
            if db_snap and os.path.exists(db_snap):
                zf.write(db_snap, arcname=f'phonkbot.db')

            # Recent audio + video + thumbnails
            for sub in ('audio', 'video', 'thumbnails'):
                src_dir = _APP_DIR / 'output' / sub
                if not src_dir.exists():
                    continue
                for f in src_dir.iterdir():
                    if f.is_file() and f.stat().st_mtime > cutoff:
                        zf.write(f, arcname=f'output/{sub}/{f.name}')

            # Logs (small, always include)
            log_dir = _APP_DIR / 'logs'
            if log_dir.exists():
                for f in log_dir.iterdir():
                    if f.is_file():
                        zf.write(f, arcname=f'logs/{f.name}')

        size_mb = dest.stat().st_size / (1024 * 1024)
        log(f"Backup: full bundle {dest.name} ({size_mb:.1f}MB)")
        return str(dest)
    except Exception as e:
        log_error(f"Backup: full bundle failed — {e}")
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return None


# ─────────────────────────────────────────────────────────
# Daily background daemon
# ─────────────────────────────────────────────────────────

_backup_thread = None


def _daily_loop():
    """Sleep + snapshot in a tight loop. Runs forever."""
    log(f"Backup: daily snapshot loop started (every {DAILY_INTERVAL // 3600}h)")
    # First snapshot 60s after boot so startup logs flush
    time.sleep(60)
    while True:
        snapshot_db()
        time.sleep(DAILY_INTERVAL)


def start_daily_backup():
    """Launch the background thread. Idempotent."""
    global _backup_thread
    if _backup_thread and _backup_thread.is_alive():
        return _backup_thread
    _backup_thread = threading.Thread(target=_daily_loop, daemon=True)
    _backup_thread.start()
    return _backup_thread
