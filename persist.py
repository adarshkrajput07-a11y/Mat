"""Keeps stats, media and settings alive across restarts on hosts that wipe the disk.

The bot keeps ONE backup zip pinned in the owner's private chat (the owner must have
pressed /start in the bot's DM). On startup, if stats.db is missing (a fresh server),
the zip is downloaded from that pinned message and unpacked before the bot opens its
database. While running, bot.py updates the same pinned file every few minutes, but
only when something changed.
"""
import io
import json
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

BACKUP_NAME = "matrix_data_backup.zip"
API = "https://api.telegram.org"
MAX_BYTES = 49 * 1024 * 1024  # Telegram bots can upload up to 50 MB


def _api(token, method, params=None, timeout=30):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"{API}/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def safe_extract(blob: bytes, data_dir: str):
    root = os.path.realpath(data_dir)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            target = os.path.realpath(os.path.join(root, name))
            if os.path.isabs(name) or not (target == root or target.startswith(root + os.sep)):
                raise ValueError(f"unsafe path in backup: {name}")
        z.extractall(root)


def restore_if_missing(token, chat_id, data_dir, marker="stats.db") -> bool:
    """Run BEFORE the database is opened. Returns False only if a restore was needed
    but failed for a temporary reason (then backups stay off so a good backup is never
    overwritten by an empty one)."""
    if not chat_id or os.path.exists(os.path.join(data_dir, marker)):
        return True
    for attempt in range(3):
        try:
            chat = _api(token, "getChat", {"chat_id": chat_id})["result"]
            doc = (chat.get("pinned_message") or {}).get("document") or {}
            if doc.get("file_name") != BACKUP_NAME:
                print("[persist] No backup found - starting fresh.")
                return True
            info = _api(token, "getFile", {"file_id": doc["file_id"]})["result"]
            with urllib.request.urlopen(f"{API}/file/bot{token}/{info['file_path']}", timeout=120) as r:
                blob = r.read()
            safe_extract(blob, data_dir)
            print("[persist] Restored stats, media and settings from the pinned backup.")
            return True
        except urllib.error.HTTPError as e:
            if e.code in (400, 403, 404):  # chat unreachable: owner never started the bot
                print("[persist] Backup chat not reachable (send /start to the bot in DM). Starting fresh.")
                return True
            print(f"[persist] Restore attempt {attempt + 1} failed: {e}")
        except Exception as e:
            print(f"[persist] Restore attempt {attempt + 1} failed: {e}")
        time.sleep(3)
    print("[persist] Restore failed - automatic backups are OFF this run to protect the old backup.")
    return False


def _tracked(data_dir, files):
    out = [os.path.join(data_dir, n) for n in files]
    media = os.path.join(data_dir, "auction_media")
    if os.path.isdir(media):
        for base, _dirs, names in os.walk(media):
            out += [os.path.join(base, n) for n in names]
    return [p for p in out if os.path.isfile(p)]


def signature(data_dir, db, files):
    """Cheap fingerprint: changes whenever stats or any tracked file changes."""
    parts = []
    for p in _tracked(data_dir, files):
        st = os.stat(p)
        parts.append((p, st.st_mtime_ns, st.st_size))
    return (db.total_changes, tuple(parts))


def build_zip(data_dir, db, files):
    """Zip stats.db (consistent copy) + media/settings files. Returns a path or None."""
    tmpdir = tempfile.mkdtemp()
    snap = os.path.join(tmpdir, "stats.db")
    dst = sqlite3.connect(snap)
    db.backup(dst)
    dst.close()
    for with_media in (True, False):
        path = os.path.join(tmpdir, BACKUP_NAME)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "stats.db")
            for p in _tracked(data_dir, files):
                rel = os.path.relpath(p, data_dir)
                if rel.startswith("auction_media") and not with_media:
                    continue
                z.write(p, rel)
        if os.path.getsize(path) <= MAX_BYTES:
            return path
    return None
