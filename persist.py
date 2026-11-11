"""Keeps stats / media / auction data alive on hosts whose disk is wiped on every restart.

How it works (no extra service or database needed):
  * Every few minutes (only if something changed) the bot zips all its data and sends the zip to the
    backup chat (BACKUP_CHAT_ID, default: first owner) and PINS it there.
  * On startup, if the data folder is empty, the bot reads the pinned zip from that chat and unpacks it
    BEFORE the database is opened. So after any restart / redeploy everything is back automatically.

The owner must have pressed /start on the bot in DM once (so the bot can message the backup chat).
"""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

BACKUP_NAME = "matrix_backup.zip"
JSON_FILES = ("media.json", "auction_media_config.json", "auction_sudo.json",
              "auction_tournament.json", "auction_squads.json")
MAX_ZIP = 19 * 1024 * 1024  # Telegram lets bots download files up to 20 MB
_last_fp = None


def _api(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data, timeout=30) as r:
        return json.load(r)


def restore(token, data_dir, chat_id):
    """Call once at startup, before stats.db / media.json are opened."""
    if os.path.exists(os.path.join(data_dir, "stats.db")):
        return  # disk survived, nothing to restore
    try:
        chat = _api(token, "getChat", chat_id=chat_id)["result"]
        doc = (chat.get("pinned_message") or {}).get("document")
        if not doc or doc.get("file_name") != BACKUP_NAME:
            print("persist: no pinned backup found - starting fresh.")
            return
        path = _api(token, "getFile", file_id=doc["file_id"])["result"]["file_path"]
        tmp = os.path.join(tempfile.gettempdir(), BACKUP_NAME)
        urllib.request.urlretrieve(f"https://api.telegram.org/file/bot{token}/{path}", tmp)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(data_dir)
        os.remove(tmp)
        print("persist: data restored from the pinned Telegram backup.")
    except Exception as e:  # never stop the bot from starting
        print(f"persist: could not restore ({e}) - starting fresh.")


def _fingerprint(db, data_dir):
    parts = [str(db.total_changes)]
    for n in JSON_FILES:
        p = os.path.join(data_dir, n)
        parts.append(str(os.path.getmtime(p)) if os.path.exists(p) else "-")
    md = os.path.join(data_dir, "auction_media")
    if os.path.isdir(md):
        parts.append(str(sorted((f, os.path.getmtime(os.path.join(md, f))) for f in os.listdir(md))))
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _build_zip(db, data_dir):
    db.commit()
    tmpdir = tempfile.mkdtemp()
    try:
        snap = os.path.join(tmpdir, "stats.db")
        dst = sqlite3.connect(snap)
        db.backup(dst)  # consistent copy while the bot is running
        dst.close()
        out = os.path.join(tempfile.gettempdir(), BACKUP_NAME)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "stats.db")
            for n in JSON_FILES:
                p = os.path.join(data_dir, n)
                if os.path.exists(p):
                    z.write(p, n)
            md = os.path.join(data_dir, "auction_media")
            if os.path.isdir(md) and os.path.getsize(out) < MAX_ZIP:
                for f in os.listdir(md):
                    fp = os.path.join(md, f)
                    if os.path.isfile(fp) and os.path.getsize(out) + os.path.getsize(fp) < MAX_ZIP:
                        z.write(fp, f"auction_media/{f}")
        return out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def backup_now(bot, db, data_dir, chat_id, force=False):
    """Send + pin a fresh backup if anything changed. Returns True when a backup was sent."""
    global _last_fp
    fp = _fingerprint(db, data_dir)
    if fp == _last_fp and not force:
        return False
    out = _build_zip(db, data_dir)
    with open(out, "rb") as f:
        msg = await bot.send_document(chat_id, f, filename=BACKUP_NAME, disable_notification=True,
                                      caption=f"💾 Auto-backup {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    try:
        await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
    except Exception as e:
        print(f"persist: backup sent but could not pin it ({e})")
    _last_fp = fp
    return True
