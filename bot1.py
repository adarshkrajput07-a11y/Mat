"""Matrix Cricket Bot - Solo + Team mode (FCG-style hand cricket for Telegram groups).

Install:  pip install "python-telegram-bot[job-queue]>=21"
Run:      BOT_TOKEN=123:abc python bot.py
"""
import asyncio
import html
import json
import logging
import os
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

from styled_buttons import Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, TypeHandler, filters)

def _load_env(path: str = ".env"):
    """Tiny .env loader (no extra dependency): KEY=value lines."""
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
TOKEN = "9157459094:AAHoK9cA3F1ws5kqLYutEzq5ePjf7wr29yA"
if not TOKEN:
    raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
LOBBY_SECONDS = 50
MAX_PLAYERS = 10
MIN_PLAYERS = 2
MOVE_TIMEOUT = 60  # seconds a bowler/batter gets before being removed
WARN_AT = [30, 15, 5, 3, 2, 1]  # warnings sent when this many seconds are left
# Team mode: captain gets 5 min to pick the batter / bowler
PICK_TIMEOUT = 300
PICK_WARN_AT = [240, 180, 120, 60, 30, 10, 5, 4, 3, 2, 1]
AUCTION_BACKUP_FILES = ("auction_media_config.json", "auction_sudo.json",
                        "auction_tournament.json", "auction_squads.json", "playzone.json")
TIMEOUT_PENALTY = 6  # runs deducted from a team when its batter/bowler times out
HTML = ParseMode.HTML
SUPPORT_URL = "https://t.me/Suiiiiiii_0007"
# Telegram user ids of the bot owners (comma separated in .env: OWNER_IDS=111,222)
OWNER_IDS = {6092755101}
if not OWNER_IDS:
    print("WARNING: OWNER_IDS is empty - nobody can use the owner commands. See .env.example.")
DATA_DIR = os.environ.get("DATA_DIR", ".")  # point this at a persistent volume on your host
os.makedirs(DATA_DIR, exist_ok=True)
MEDIA_FILE = os.path.join(DATA_DIR, "media.json")
# chat that gets a fresh media.json copy after every /addfile or /delfile (default: first owner)
BACKUP_CHAT_ID = int(os.environ.get("BACKUP_CHAT_ID") or (min(OWNER_IDS) if OWNER_IDS else 0))
import persist  # keeps stats/media alive when the host wipes the disk on restart (no-MongoDB mode)
import store as storage  # MongoDB (MONGO_URI in .env) or, if empty, stats.db + json files
if storage.USE_MONGO:
    BACKUP_OK = False  # MongoDB keeps everything; the pinned Telegram zip backup is not needed
    _mongo = storage.get_store()
    if not _mongo.migrated():
        # first run on MongoDB: bring over the old stats.db / media.json / auction files (from this
        # folder, or from the pinned Telegram backup on a fresh server). Existing data is never overwritten.
        if persist.restore_if_missing(TOKEN, BACKUP_CHAT_ID, DATA_DIR):
            print("[store] First MongoDB run - imported:", _mongo.import_from_dir(DATA_DIR) or "nothing to import")
else:
    BACKUP_OK = persist.restore_if_missing(TOKEN, BACKUP_CHAT_ID, DATA_DIR)  # must run before the DB opens
STORE = storage.get_store()
_LAST_SIG = None
ALIASES = {str(i): f"run_{i}" for i in range(0, 7)}
ALIASES.update({
    "wicket": "wicket", "out": "wicket", "duck": "duck", "hattrick": "hattrick",
    "fifty": "fifty", "50": "fifty", "halfcentury": "fifty",
    "century": "century", "100": "century",
    "welcome": "welcome", "groupwelcome": "group_welcome", "lobby": "lobby",
    "arena": "solo_arena", "soloarena": "solo_arena",
    "bowling": "bowling_call", "bowlingcall": "bowling_call",
    "facetheball": "face_ball", "faceball": "face_ball",
    "newbatter": "new_batter", "newbatterin": "new_batter",
    "scorecard": "scorecard", "potm": "potm", "playerofthematch": "potm",
    "usercard": "user_card",
    # Auction mode media (/addfile aucsold ...): welcome banner, registration, sets, intro video,
    # new-set banner, SOLD / UNSOLD GIFs, final GIF, default player picture
    "aucwelcome": "auc_welcome", "aucregister": "auc_register", "aucsets": "auc_sets",
    "aucintro": "auc_intro", "auclive": "auc_live", "aucbid": "auc_bid", "aucsold": "auc_sold",
    "aucunsold": "auc_unsold", "auctrophy": "auc_trophy", "aucplayer": "auc_player",
    "aucdefault": "auc_player", "aucnoprofile": "auc_player",
    "1v1": "duel", "duel": "duel", "challenge": "duel",
    "teamlobby": "team_lobby", "teamarena": "team_arena", "captain": "captain",
})
MEDIA_KEYS = list(dict.fromkeys(ALIASES.values()))
KEY_HELP = ("0-6, wicket, duck, hattrick, fifty, century, welcome, groupwelcome, "
            "lobby, arena, bowling, facetheball, newbatter, scorecard, potm, usercard, "
            "1v1, teamlobby, teamarena, captain, aucwelcome, aucregister, aucsets, aucintro, auclive, "
            "aucbid, aucsold, aucunsold, auctrophy, aucplayer")
MEDIA = STORE.load_json("media", {})  # {"run_4": ["video:<file_id>", "photo:<file_id>"], ...}
# older saves hold one string per key; every key now holds a list of files
MEDIA = {k: ([v] if isinstance(v, str) else list(v)) for k, v in MEDIA.items() if v}

db = STORE.db  # sqlite handle (None when MongoDB is used)
known_users = set(STORE.user_ids())
known_chats = set(STORE.chat_ids())
known_unames = STORE.unames()
pending_bc = {}  # owner id -> (recipient ids, payload)
LINE = "━━━━━━━━━━━━━━"

COMMENT = {
    0: "Dot ball. No run.", 1: "Quick single.", 2: "Pushed into the gap for two.",
    3: "Good running, three!", 4: "Pierces the field!",
    5: "Fives! Rare and brilliant.", 6: "SIX! Out of the park!",
}


@dataclass
class Player:
    uid: int
    name: str
    runs: int = 0
    balls: int = 0
    fours: int = 0
    sixes: int = 0
    wickets: int = 0
    balls_bowled: int = 0
    runs_conceded: int = 0
    wkt_streak: int = 0
    fifties: int = 0
    hundreds: int = 0
    ducks: int = 0
    hattricks: int = 0
    dm_ok: bool = True  # False until the player has started the bot in DM
    batted: bool = True  # team mode: False until the player reaches the crease


@dataclass
class Game:
    chat_id: int
    host_id: int
    host_name: str
    spell: int = 6
    spam_free: bool = True
    state: str = "setup"  # setup | lobby | await_bowl | await_bat | busy
    players: dict = field(default_factory=dict)
    order: list = field(default_factory=list)
    bat_idx: int = 0
    bowl_ptr: int = 0
    bowler_id: Optional[int] = None
    spell_balls: int = 0
    bowl_value: Optional[int] = None
    form: list = field(default_factory=list)
    chat_title: str = ""
    ball_no: int = 0
    removed: set = field(default_factory=set)
    join_requests: dict = field(default_factory=dict)
    duel: bool = False       # True for a /1v1 challenge (two players, each bats until out)
    duel_msg: object = None  # the challenge message (edited when it is accepted / expires)


try:
    import auction_mode  # the Auction mode (auction_mode.py + cards.py + reports.py + media/)
except Exception as e:  # pragma: no cover - the cricket bot still runs without it
    auction_mode = None
    logging.getLogger("cricket").warning("Auction mode not loaded: %s", e)

games: dict = {}      # chat_id -> Game
user_game: dict = {}  # user_id -> chat_id

WELCOME = ("<b>『 🏏 MATRIX CRICKET BOT 』</b>\n" + LINE + "\n"
           "Grab your bat, pick your side and walk out to the middle\n\n"
           "<i>“Every ball is a new chance to write your story.”</i>\n\n"
           "Choose a mode to continue 👇")


def menu_kb(uid: int):
    rows = [[Btn("🧍 Solo", callback_data="mode:solo"),
             Btn("👥 Team", callback_data="mode:team")],
            [Btn("🔨 Auction", callback_data="auction:start"),
             Btn("🏆 Tournament", callback_data="soon:tournament")]]
    rows.append([Btn("❌ Cancel", callback_data=f"mc:{uid}")])
    return Markup(rows)


SPELL_KB = Markup([[Btn("⚡ 3 Balls", callback_data="spell:3"),
                    Btn("⚡ 6 Balls", callback_data="spell:6")],
                   [Btn("❌ Cancel", callback_data="cancel")]])
MODE_KB = Markup([[Btn("🛡 Spam-Free", callback_data="gm:spam"),
                   Btn("⚔️ Normal", callback_data="gm:normal")],
                  [Btn("⬅️ Back", callback_data="back")]])


# ---------- helpers ----------
def link(uid: int, name: str) -> str:
    """Blue, bold name that opens the player's profile when tapped."""
    return f'<a href="tg://user?id={uid}"><b>{html.escape(name)}</b></a>'


def nm(p: Player) -> str:
    return link(p.uid, p.name)


async def say(ctx, chat_id, text, markup=None):
    return await ctx.bot.send_message(chat_id, text, parse_mode=HTML,
                                      reply_markup=markup)


async def say_media(ctx, chat_id, key, text, markup=None):
    """Send text with the saved image/video/GIF for `key`; plain text if none."""
    files = MEDIA.get(key)
    m = random.choice(files) if files else None  # several files per key: pick one at random
    if m:
        kind, fid = m.split(":", 1)
        send = {"photo": ctx.bot.send_photo, "video": ctx.bot.send_video,
                "animation": ctx.bot.send_animation}[kind]
        try:
            return await send(chat_id, fid, caption=text, parse_mode=HTML,
                              reply_markup=markup)
        except Exception:
            pass
    return await say(ctx, chat_id, text, markup)


def norm_key(raw):
    return ALIASES.get("".join(c for c in raw.lower() if c.isalnum()))


def media_of(m):
    if not m:
        return None
    if m.photo:
        return "photo:" + m.photo[-1].file_id
    if m.animation:
        return "animation:" + m.animation.file_id
    if m.video:
        return "video:" + m.video.file_id
    return None


def save_media_file():
    STORE.save_json("media", MEDIA)


def owner_ok(update, strict: bool = False) -> bool:
    """Owners, plus sudo users (given access with /sudo) unless strict (backup / restore)."""
    uid = update.effective_user.id
    if uid in OWNER_IDS:
        return True
    return bool(not strict and auction_mode and auction_mode.is_sudo(uid))


# ---------- Play Zone (its own setting: not linked with auction or registration groups) ----------
PLAYZONE_LINK = "https://t.me/+2QKC82TNZSk0YjNl"  # Play Zone group (change with /setplayzone)
CHANNEL_LINK = "https://t.me/MatrixXParadise"  # welcome-message Channel button (change with /setchannel)


def _zone_data() -> dict:
    return STORE.load_json("playzone", {})


def _zone_save(data: dict):
    STORE.save_json("playzone", {k: v for k, v in data.items() if v})


def playzone_link() -> str:
    """Link set with /setplayzone (saved in playzone.json), else PLAYZONE_LINK above."""
    return _zone_data().get("link", "") or PLAYZONE_LINK


def channel_link() -> str:
    """Link set with /setchannel (saved in playzone.json), else CHANNEL_LINK above.
    Not linked with the auction registration channel (that one is /setaucchannel)."""
    return _zone_data().get("channel", "") or CHANNEL_LINK


def _make_link(ref: str, c=None) -> str:
    if ref.startswith(("http://", "https://")):
        return ref
    if ref.startswith("@"):
        return "https://t.me/" + ref[1:]
    if c is not None:
        return f"https://t.me/{c.username}" if c.username else (c.invite_link or "")
    return ""


async def _set_zone_link(update, ctx, key: str, label: str, cmdname: str, in_group_ok: bool):
    """Shared by /setplayzone and /setchannel. Saves {key: link} in playzone.json."""
    if not owner_ok(update):
        return
    msg, chat = update.message, update.effective_chat
    args = ctx.args
    data = _zone_data()
    if args and args[0].lower() in ("off", "remove", "clear", "none"):
        data.pop(key, None)
        _zone_save(data)
        await msg.reply_text(f"🗑 {label} removed.")
        await auto_backup(ctx)
        return
    if not args and (chat.type == ChatType.PRIVATE or not in_group_ok):
        cur = (playzone_link() if key == "link" else channel_link()) or "not set"
        await msg.reply_text(
            f"{label}: {cur}\n\nSet it: /{cmdname} <link | @username"
            + (" | chat id>" if in_group_ok else ">")
            + (f"\nOr send /{cmdname} inside the group itself." if in_group_ok else "")
            + f"\nRemove: /{cmdname} off", disable_web_page_preview=True)
        return
    ref = args[0] if args else str(chat.id)
    c = None
    if not ref.startswith(("http://", "https://", "@")):
        try:
            c = await ctx.bot.get_chat(int(ref) if ref.lstrip("-").isdigit() else ref)
        except Exception:
            c = None
    link = _make_link(ref, c)
    if not link:
        await msg.reply_text(f"🚫 I couldn't make a link from that. Send the link instead:\n"
                             f"/{cmdname} https://t.me/yourlink")
        return
    data[key] = link
    _zone_save(data)
    await msg.reply_text(f"✅ {label} set: {link}\nIt shows on the bot's welcome message (DM).",
                         disable_web_page_preview=True)
    await auto_backup(ctx)


async def setplayzone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/setplayzone <link | @group | chat id> - owner: the 🏟 Playzone button on the welcome message."""
    await _set_zone_link(update, ctx, "link", "🏟 Play Zone", "setplayzone", True)


async def setchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/setchannel <link | @channel> - owner: the 📢 Channel button on the welcome message."""
    await _set_zone_link(update, ctx, "channel", "📢 Channel", "setchannel", False)


async def addfile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/addfile 6  - reply to a photo/video/GIF, or send it with this caption."""
    msg = update.message
    if not owner_ok(update):
        return
    parts = (msg.text or msg.caption or "").split(maxsplit=1)
    key = norm_key(parts[1]) if len(parts) > 1 else None
    if not key:
        await msg.reply_text("Usage: reply to a photo/video/GIF with\n/addfile <key>\n\n"
                             f"Keys: {KEY_HELP}")
        return
    val = media_of(msg) or media_of(msg.reply_to_message)
    if not val:
        await msg.reply_text("Reply to a photo/video/GIF with /addfile "
                             f"{parts[1]}, or send the media with that caption.")
        return
    files = MEDIA.setdefault(key, [])
    if val in files:
        await msg.reply_text(f"That file is already saved for <b>{key}</b>.", parse_mode=HTML)
        return
    files.append(val)
    save_media_file()
    await auto_backup(ctx)
    await msg.reply_text(f"✅ Saved for <b>{key}</b> (file #{len(files)}). "
                         f"It now has <b>{len(files)}</b> file{'s' if len(files) != 1 else ''}; "
                         "one is sent at random each time.", parse_mode=HTML)


async def delfile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_ok(update):
        return
    args = list(ctx.args)
    num = int(args.pop()) if len(args) > 1 and args[-1].isdigit() else None
    key = norm_key(" ".join(args))
    files = MEDIA.get(key) if key else None
    if not files:
        await update.message.reply_text("Usage: /delfile <key>  (all files)\n"
                                        "or /delfile <key> <number>  (one file; see /files)")
    elif num is None:
        MEDIA.pop(key)
        save_media_file()
        await auto_backup(ctx)
        await update.message.reply_text(f"🗑 Removed all {len(files)} file(s) of {key}.")
    elif 1 <= num <= len(files):
        files.pop(num - 1)
        if not files:
            MEDIA.pop(key)
        save_media_file()
        await auto_backup(ctx)
        await update.message.reply_text(f"🗑 Removed file #{num} of {key}.")
    else:
        await update.message.reply_text(f"{key} has {len(files)} file(s). Pick 1-{len(files)}.")


async def full_backup(ctx, force=False):
    """Keep ONE pinned zip (stats + media + settings) in the owner's DM up to date."""
    global _LAST_SIG
    if STORE.kind != "sqlite" or not BACKUP_OK or not BACKUP_CHAT_ID:
        return  # (with MongoDB there is nothing to zip: the data is already in the database)
    try:
        from telegram import InputMediaDocument
        db.commit()
        files = ("media.json",) + AUCTION_BACKUP_FILES
        sig = persist.signature(DATA_DIR, db, files)
        if sig == _LAST_SIG and not force:
            return
        path = persist.build_zip(DATA_DIR, db, files)
        if not path:
            return
        caption = "🗂 Auto-backup · stats, media & settings · " + time.strftime("%d %b %H:%M UTC", time.gmtime())
        old = (await ctx.bot.get_chat(BACKUP_CHAT_ID)).pinned_message
        if old and old.document and old.document.file_name == persist.BACKUP_NAME:
            try:  # update the pinned file in place: no new messages, no pin notices
                with open(path, "rb") as f:
                    await ctx.bot.edit_message_media(
                        chat_id=BACKUP_CHAT_ID, message_id=old.message_id,
                        media=InputMediaDocument(f, filename=persist.BACKUP_NAME, caption=caption))
                _LAST_SIG = sig
                return
            except Exception:
                pass  # fall through: send a fresh copy and pin it
        with open(path, "rb") as f:
            sent = await ctx.bot.send_document(BACKUP_CHAT_ID, f, filename=persist.BACKUP_NAME,
                                               caption=caption, disable_notification=True)
        await ctx.bot.pin_chat_message(BACKUP_CHAT_ID, sent.message_id, disable_notification=True)
        _LAST_SIG = sig
    except Exception as e:
        print("Auto-backup failed:", e)


async def auto_backup(ctx):
    """Called after /addfile and /delfile: back everything up right away."""
    await full_backup(ctx, force=True)


async def backup_job(ctx):
    await full_backup(ctx)


async def backup_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/backup - owner gets media.json and stats.db as files."""
    if not owner_ok(update, strict=True):
        return
    import tempfile
    if STORE.kind == "mongo":  # data lives in MongoDB: send a JSON snapshot as an extra safety copy
        path = os.path.join(tempfile.gettempdir(), "matrix_mongo_backup.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(STORE.export_all(), f)
        with open(path, "rb") as f:
            await update.message.reply_document(f, filename="matrix_mongo_backup.json")
        os.remove(path)
        await update.message.reply_text(
            "✅ Snapshot sent. Your live data is stored in MongoDB, so a server move needs no restore - "
            "just use the same MONGO_URI. To load this file into a database: reply to it with /restore.")
        return
    db.commit()
    with open(MEDIA_FILE, "rb") as f:
        await update.message.reply_document(f, filename="media.json")
    tmp = os.path.join(tempfile.gettempdir(), "stats.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    dst = sqlite3.connect(tmp)
    db.backup(dst)  # consistent copy even while the bot is running
    dst.close()
    with open(tmp, "rb") as f:
        await update.message.reply_document(f, filename="stats.db")
    for name in AUCTION_BACKUP_FILES:  # Auction mode settings (sudo list, tournament, media choices)
        path = os.path.join(DATA_DIR, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                await update.message.reply_document(f, filename=name)
    media_dir = os.path.join(DATA_DIR, "auction_media")
    if os.path.isdir(media_dir) and os.listdir(media_dir):  # files added with /addfile in auction chats
        import shutil
        archive = shutil.make_archive(os.path.join(tempfile.gettempdir(), "auction_media"),
                                      "zip", media_dir)
        with open(archive, "rb") as f:
            await update.message.reply_document(f, filename="auction_media.zip")
    await update.message.reply_text(
        "✅ Backup sent. On the new server, send each file back with /restore "
        "(reply to the file), then restart the bot.")


async def restore_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/restore - reply to a media.json or stats.db backup file."""
    if not owner_ok(update, strict=True):
        return
    doc = update.message.reply_to_message and update.message.reply_to_message.document
    allowed = ("media.json", "stats.db", "auction_media.zip", "matrix_mongo_backup.json") + AUCTION_BACKUP_FILES
    if not doc or doc.file_name not in allowed:
        await update.message.reply_text("Reply to a backed-up file (media.json, stats.db, "
                                        "auction_*.json / auction_media.zip) with /restore")
        return
    if STORE.kind == "mongo":  # load a snapshot or an old backup file into MongoDB (replaces matching data)
        import shutil
        import tempfile
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, doc.file_name)
        try:
            await (await ctx.bot.get_file(doc.file_id)).download_to_drive(path)
            if doc.file_name == "matrix_mongo_backup.json":
                with open(path, encoding="utf-8") as f:
                    STORE.import_all(json.load(f))
            elif doc.file_name == "auction_media.zip":
                await update.message.reply_text("ℹ️ auction_media.zip is not used with MongoDB. Nothing to restore.")
                return
            else:
                STORE.import_from_dir(tmpdir, overwrite=True)
            await update.message.reply_text(f"✅ {doc.file_name} restored into MongoDB. Restart the bot to reload.")
        except Exception as e:
            await update.message.reply_text(f"❌ Restore failed: {e}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return
    path = os.path.join(DATA_DIR, "restore_tmp")
    await (await ctx.bot.get_file(doc.file_id)).download_to_drive(path)
    try:
        if doc.file_name == "media.json":
            with open(path) as f:
                data = json.load(f)
            MEDIA.clear()
            MEDIA.update({k: ([v] if isinstance(v, str) else list(v))
                          for k, v in data.items() if v})
            save_media_file()
            await update.message.reply_text(f"✅ Restored media for {len(MEDIA)} keys.")
        elif doc.file_name == "stats.db":
            src = sqlite3.connect(path)
            src.backup(db)
            src.close()
            await update.message.reply_text("✅ Stats restored. Restart the bot to reload.")
        elif doc.file_name == "auction_media.zip":
            import shutil
            shutil.unpack_archive(path, os.path.join(DATA_DIR, "auction_media"), "zip")
            await update.message.reply_text("✅ Auction media restored. Restart the bot.")
        else:  # auction settings json
            import shutil
            shutil.copy(path, os.path.join(DATA_DIR, doc.file_name))
            await update.message.reply_text(f"✅ {doc.file_name} restored. Restart the bot.")
    except Exception as e:
        await update.message.reply_text(f"❌ Restore failed: {e}")
    finally:
        if os.path.exists(path):
            os.remove(path)


async def files_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_ok(update):
        return
    lines = [f"{'✅' if MEDIA.get(k) else '▫️'} {k.replace('auc_', 'auc')}"
             + (f" ({len(MEDIA[k])} files)" if len(MEDIA.get(k, [])) > 1 else "")
             for k in MEDIA_KEYS]
    await update.message.reply_text("Media status:\n" + "\n".join(lines))


def save_match(ps, orange, purple, potm, winners=None):
    """Write one finished match into the database (MongoDB or stats.db).
    Solo: winner = player of the match. Team: winners = set of winning uids."""
    lines, updates = [], []
    rows = STORE.get_stats_many([p.uid for p in ps])
    for p in ps:
        row = rows.get(p.uid) or {"best_w": 0, "best_r": 0, "form": ""}
        win = (p.uid in winners) if winners is not None else p is potm
        xp, gems = 20 + p.runs * 2 + p.wickets * 10, 5
        if p is orange:
            xp, gems = xp + 100, gems + 50
        if p is purple:
            xp, gems = xp + 100, gems + 50
        if p is potm:
            xp, gems = xp + 150, gems + 100
        if winners is not None and win:
            xp, gems = xp + 50, gems + 25
        best_w, best_r = row["best_w"], row["best_r"]
        if p.wickets > best_w or (p.wickets and p.wickets == best_w and p.runs_conceded < best_r):
            best_w, best_r = p.wickets, p.runs_conceded
        form = (row["form"] + ("W" if win else "L"))[-5:]
        updates.append({
            "uid": p.uid, "name": p.name, "hs": p.runs, "best_w": best_w, "best_r": best_r, "form": form,
            "inc": {"runs": p.runs, "balls": p.balls, "innings": int(p.batted), "fours": p.fours,
                    "sixes": p.sixes, "fifties": p.fifties, "hundreds": p.hundreds, "ducks": p.ducks,
                    "wkts": p.wickets, "bowl_balls": p.balls_bowled, "conceded": p.runs_conceded,
                    "hattricks": p.hattricks, "matches": 1, "wins": int(win), "orange": int(p is orange),
                    "purple": int(p is purple), "potm": int(p is potm), "xp": xp, "gems": gems}})
        lines.append(f"{nm(p)}: +{gems} 💎 · +{xp} XP")
    try:
        STORE.update_matches(updates)
    except Exception as e:  # never break the game because the database hiccuped
        print("[store] could not save match stats:", e)
    return "\n".join(lines)


TIERS = [(5, "Academy"), (10, "Club"), (20, "Pro"), (10 ** 9, "Legend")]


async def user_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/user_card - your stats, or reply to someone's message to see theirs."""
    msg = update.message
    target = msg.reply_to_message.from_user if msg.reply_to_message else update.effective_user
    try:
        r = STORE.get_stats(target.id)
    except Exception as e:
        print("[store] could not read stats:", e)
        r = None
    if not r or not r["matches"]:
        await msg.reply_text("No stats yet. Play a match first! 🏏")
        return
    lvl, cur = r["xp"] // 1000 + 1, r["xp"] % 1000
    pct = cur // 10
    bar = "▰" * (pct // 10) + "▱" * (10 - pct // 10)
    tier = next(t for lim, t in TIERS if lvl < lim)
    bat_s, bowl_s = r["runs"], r["wkts"] * 8
    if min(bat_s, bowl_s) and max(bat_s, bowl_s) / min(bat_s, bowl_s) < 2:
        role = "All Rounder"
    else:
        role = "Batter" if bat_s >= bowl_s else "Bowler"
    avg = r["runs"] / r["innings"] if r["innings"] else 0
    sr = r["runs"] / r["balls"] * 100 if r["balls"] else 0
    eco = r["conceded"] / (r["bowl_balls"] / 6) if r["bowl_balls"] else 0
    wr = r["wins"] / r["matches"] * 100
    form = "".join("🟢" if c == "W" else "🔴" for c in r["form"]) or "—"
    name = html.escape(r["name"])
    text = (
        f"🏏 <b>MATRIX CRICKET BOT</b>\n\n"
        f"👤 <b>{name}</b>\n"
        f"  ✦ Level: {lvl} · Tier: {tier}\n  ✦ Role: {role}\n\n"
        f"✨ <b>LEVEL {lvl}</b> · {cur} / 1,000 XP\n{bar} {pct}%\n"
        f"💎 Wallet: {r['gems']} gems\n\n"
        f"<blockquote>🏏 <b>BATTING</b>\nRuns: {r['runs']} · Avg: {avg:.1f}\n"
        f"SR: {sr:.1f} · HS: {r['hs']}\n4s: {r['fours']} · 6s: {r['sixes']}\n"
        f"50s: {r['fifties']} · 100s: {r['hundreds']}\nDucks: {r['ducks']}</blockquote>\n"
        f"<blockquote>⚾ <b>BOWLING</b>\nWkts: {r['wkts']} · Eco: {eco:.1f}\n"
        f"Best: {r['best_w']}/{r['best_r']} · Hat-tricks: {r['hattricks']}</blockquote>\n"
        f"<blockquote>👑 <b>CAREER</b>\nMatches: {r['matches']} · Wins: {r['wins']}\n"
        f"Win Rate: {wr:.0f}%\n🟠 Orange Cap: {r['orange']}\n🟣 Purple Cap: {r['purple']}\n"
        f"🌟 POTM Awards: {r['potm']}</blockquote>\n"
        f"<blockquote>📈 <b>FORM</b>\n{form}</blockquote>")
    chat_id = update.effective_chat.id
    try:  # one message: the person's own profile picture with the whole card as caption
        photos = await ctx.bot.get_user_profile_photos(target.id, limit=1)
        if photos.total_count and photos.photos:
            await ctx.bot.send_photo(chat_id, photos.photos[0][-1].file_id,
                                     caption=text, parse_mode=HTML)
            return
    except TelegramError:
        pass
    if MEDIA.get("user_card"):  # no (or hidden) picture -> the /addfile usercard image
        if await say_media(ctx, chat_id, "user_card", text):
            return
    await say(ctx, chat_id, text)


async def edit_any(msg, text, reply_markup=None, parse_mode=None):
    """Edit a message whether it is plain text or a photo/video/GIF with caption."""
    try:
        if msg.text is not None:
            await msg.edit_text(text, reply_markup=reply_markup, parse_mode=HTML)
        else:
            await msg.edit_caption(caption=text, reply_markup=reply_markup,
                                   parse_mode=HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ---------- join a running match ----------
async def join_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/join - ask to join a running solo match; the host must accept."""
    chat, user, msg = update.effective_chat, update.effective_user, update.message
    g = games.get(chat.id)
    if not g or g.state == "setup":
        await msg.reply_text("No match is running. Use /start to create one.")
        return
    if g.duel:
        await msg.reply_text("⚔️ This is a 1v1 match. Only two players can play.")
        return
    if g.state == "lobby":
        await joingame(update, ctx)
        return
    if user.id in g.removed:
        await msg.reply_text("You were removed from this match, so you can't rejoin it.")
        return
    if user.id in g.players:
        await msg.reply_text("You're already in this match.")
        return
    if user_game.get(user.id, chat.id) != chat.id:
        await msg.reply_text("You're already in a match in another group.")
        return
    if len(g.order) >= MAX_PLAYERS:
        await msg.reply_text("The match is full.")
        return
    if user.id in g.join_requests:
        await msg.reply_text("Your request is already waiting for the host.")
        return
    try:  # also verifies the user has started the bot in DM
        await ctx.bot.send_message(
            user.id, "📨 Join request sent to the host. I'll DM you when it's answered.")
    except Forbidden:
        await msg.reply_text(
            "Start me in DM first, then send /join again 👇",
            reply_markup=Markup([[Btn("Start bot in DM", url=f"https://t.me/{ctx.bot.username}?start=register")]]))
        return
    g.join_requests[user.id] = user.full_name
    kb = Markup([[Btn("✅ Accept", callback_data=f"jn:yes:{user.id}"),
                  Btn("❌ Reject", callback_data=f"jn:no:{user.id}")]])
    await say(ctx, chat.id,
              f"🙋 {link(user.id, user.full_name)} wants to join the match!\n"
              f"👑 Host {link(g.host_id, g.host_name)}, please confirm.", kb)
    ctx.job_queue.run_once(join_expire, 60, data=(chat.id, user.id),
                           name=f"jreq:{chat.id}:{user.id}")


async def join_expire(ctx):
    chat_id, uid = ctx.job.data
    g = games.get(chat_id)
    if g and g.join_requests.pop(uid, None):
        await say(ctx, chat_id, "⌛ A join request expired.")


async def jn_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, ans, uid_s = q.data.split(":")
    uid = int(uid_s)
    g = games.get(q.message.chat_id)
    if not g:
        await q.answer("This match has ended.", show_alert=True)
        return
    if q.from_user.id != g.host_id:
        await q.answer("Only the host can confirm.", show_alert=True)
        return
    name = g.join_requests.pop(uid, None)
    if name is None:
        await q.answer("This request is no longer active.", show_alert=True)
        return
    cancel_jobs(ctx, f"jreq:{g.chat_id}:{uid}")
    await q.answer()
    if ans == "no":
        await edit_any(q.message, f"❌ Host rejected {link(uid, name)}.")
        try:
            await ctx.bot.send_message(uid, "❌ The host rejected your join request.")
        except Forbidden:
            pass
        return
    if len(g.order) >= MAX_PLAYERS or user_game.get(uid, g.chat_id) != g.chat_id:
        await edit_any(q.message, f"⚠️ {link(uid, name)} can't be added right now.")
        return
    g.players[uid] = Player(uid, name)
    g.order.append(uid)  # joins the end of the batting order
    user_game[uid] = g.chat_id
    await edit_any(q.message, f"✅ {link(uid, name)} joined the match! "
                              f"They bat last. [{len(g.order)}/{MAX_PLAYERS}]")
    try:
        await ctx.bot.send_message(uid, "✅ The host accepted you. You're in the match!")
    except Forbidden:
        pass


# ---------- broadcast ----------
async def track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remember every DM user and group the bot sees, for /broadcast."""
    chat = update.effective_chat
    if not chat:
        return
    u = update.effective_user
    if u and u.username and known_unames.get(u.username.lower()) != (u.id, u.full_name):
        known_unames[u.username.lower()] = (u.id, u.full_name)  # lets the host /add @username
        STORE.add_uname(u.username.lower(), u.id, u.full_name)
    if chat.type == ChatType.PRIVATE and chat.id not in known_users:
        known_users.add(chat.id)
        name = update.effective_user.full_name if update.effective_user else ""
        STORE.add_user(chat.id, name)
    elif chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and chat.id not in known_chats:
        known_chats.add(chat.id)
        STORE.add_chat(chat.id, chat.title or "")


BC_USAGE = ("📣 <b>Broadcast</b>\n\n"
            "Reply to any message (text, photo, video…) with:\n"
            "<code>/broadcast</code> - everyone\n"
            "<code>/broadcast users</code> - DM users only\n"
            "<code>/broadcast groups</code> - groups only\n\n"
            "Or send plain text: <code>/broadcast groups Hello everyone!</code>")


async def broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not owner_ok(update):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        await msg.reply_text("Please use /broadcast in my DM.")
        return
    parts = (msg.text or "").split(maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""
    target = "all"
    first = rest.split(maxsplit=1)[0].lower() if rest else ""
    if first in ("users", "groups", "all"):
        target, rest = first, rest[len(first):].strip()
    src = msg.reply_to_message
    if src:
        payload = {"kind": "copy", "chat_id": src.chat_id, "msg_id": src.message_id}
    elif rest:
        payload = {"kind": "text", "text": rest}
    else:
        await msg.reply_text(BC_USAGE, parse_mode=HTML)
        return
    users = STORE.user_ids() if target != "groups" else []
    groups = STORE.chat_ids() if target != "users" else []
    ids = users + groups
    if not ids:
        await msg.reply_text("No recipients saved yet.")
        return
    pending_bc[update.effective_user.id] = (ids, payload)
    kb = Markup([[Btn("✅ Send", callback_data="bc:yes"), Btn("❌ Cancel", callback_data="bc:no")]])
    await msg.reply_text(
        f"📣 <b>Broadcast preview</b>\nTarget: <b>{target}</b>\n"
        f"Recipients: <b>{len(ids)}</b> (👤 {len(users)} · 💬 {len(groups)})\n\nSend it now?",
        reply_markup=kb, parse_mode=HTML)


async def bc_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in OWNER_IDS:
        await q.answer("Owners only.", show_alert=True)
        return
    job = pending_bc.pop(q.from_user.id, None)
    await q.answer()
    if q.data == "bc:no":
        await edit_any(q.message, "❌ Broadcast cancelled.")
        return
    if not job:
        await edit_any(q.message, "⌛ Nothing pending. Use /broadcast again.")
        return
    ids, payload = job
    await edit_any(q.message, f"📣 Broadcasting to {len(ids)} chats…")
    ctx.application.create_task(run_broadcast(ctx, q.from_user.id, ids, payload))


async def run_broadcast(ctx, owner_id, ids, payload):
    ok = failed = removed = 0
    for cid in ids:
        for _ in range(2):  # one retry after a flood-wait
            try:
                if payload["kind"] == "copy":
                    await ctx.bot.copy_message(cid, payload["chat_id"], payload["msg_id"])
                else:
                    await ctx.bot.send_message(cid, payload["text"])
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Forbidden:  # blocked the bot / bot was kicked
                STORE.remove_chat(cid)
                known_users.discard(cid)
                known_chats.discard(cid)
                removed += 1
                break
            except TelegramError:
                failed += 1
                break
        else:
            failed += 1
        await asyncio.sleep(0.05)  # stay under Telegram's rate limits
    await ctx.bot.send_message(
        owner_id, f"✅ Broadcast finished\n📨 Delivered: {ok}\n"
                  f"🚫 Blocked or removed: {removed}\n⚠️ Failed: {failed}")


async def leavesolo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/leavesolo - leave the solo lobby (not allowed once the match has started)."""
    chat, user, msg = update.effective_chat, update.effective_user, update.message
    g = games.get(chat.id)
    if not g or g.state == "setup":
        await msg.reply_text("There's no open lobby here.")
        return
    if g.duel:
        await msg.reply_text("⚔️ Use /endgame to cancel a 1v1 challenge.")
        return
    if g.state != "lobby":
        await msg.reply_text("🚫 The match has already started, so you can't leave now.")
        return
    if user.id not in g.players:
        await msg.reply_text("You're not in the lobby.")
        return
    del g.players[user.id]
    user_game.pop(user.id, None)
    await msg.reply_text(f"👋 {link(user.id, user.full_name)} left the lobby. "
                         f"[{len(g.players)}/{MAX_PLAYERS}]", parse_mode=HTML)


def cancel_jobs(ctx, name):
    for j in ctx.job_queue.get_jobs_by_name(name):
        j.schedule_removal()


def cleanup(ctx, g):
    cancel_jobs(ctx, f"lobby:{g.chat_id}")
    cancel_jobs(ctx, f"warn:{g.chat_id}")
    cancel_jobs(ctx, f"move:{g.chat_id}")
    cancel_jobs(ctx, f"duel:{g.chat_id}")
    for uid in g.players:
        user_game.pop(uid, None)
    games.pop(g.chat_id, None)


async def is_admin(ctx, g, user_id) -> bool:
    if g and user_id == g.host_id:
        return True
    m = await ctx.bot.get_chat_member(g.chat_id, user_id)
    return m.status in ("administrator", "creator")


def striker(g) -> Player:
    return g.players[g.order[g.bat_idx]]


def pick_bowler(g):
    """Advance to the next player who is not the current striker."""
    n = len(g.order)
    while True:
        g.bowl_ptr = (g.bowl_ptr + 1) % n
        if g.bowl_ptr != g.bat_idx:
            break
    g.bowler_id = g.order[g.bowl_ptr]
    g.spell_balls = 0


# ---------- start & menus ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        uid = update.effective_user.id
        gm = games.get(user_game.get(uid))
        if gm and uid in gm.players and not gm.players[uid].dm_ok:
            gm.players[uid].dm_ok = True
            await say(ctx, gm.chat_id, f"✅ {nm(gm.players[uid])} is ready, DM connected!")
        name = html.escape(update.effective_user.first_name)
        add_url = f"https://t.me/{ctx.bot.username}?startgroup=true"
        pz, chl = playzone_link(), channel_link()
        kb = Markup([
            [Btn("🏟 Playzone", url=pz) if pz else Btn("🏟 Playzone", callback_data="noop"),
             Btn("📢 Channel", url=chl) if chl else Btn("📢 Channel", callback_data="noop")],
            [Btn("📝 Player Registration", callback_data="reg:start")],
            [Btn("➕ Add to Group", url=add_url)],
            [Btn("🛟 Contact Support", url=SUPPORT_URL)],
        ])
        await say_media(
            ctx, update.effective_chat.id, "welcome",
            f"🏏 <b>Hey {name}, welcome to Matrix Cricket Bot!</b>\n\n"
            "Play hand cricket with your friends right inside Telegram. "
            "Bat, bowl and take wickets in fast solo matches, then check "
            "the scorecard to see who ruled the pitch.\n\n"
            "🎯 <b>How it works</b>\n"
            "Add me to a group and send /start. Bowlers send their "
            "delivery (1–6) here in DM, and batters play their shot in the group.\n\n"
            "<i>Tap below to get started 👇</i>", kb)
        return
    if update.effective_chat.id in games or update.effective_chat.id in tgames:
        await update.message.reply_text(
            "⚠️ A match is already running in this group. Finish it first, "
            "or ask an admin to use /endgame.")
        return
    await say_media(ctx, update.effective_chat.id, "group_welcome", WELCOME,
                    menu_kb(update.effective_user.id))


async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id, uid = q.message.chat_id, q.from_user.id
    action, _, arg = q.data.partition(":")
    g = games.get(chat_id)

    if action == "other":
        await q.answer()
        await say_media(ctx, chat_id, "group_welcome", WELCOME, menu_kb(uid))
        return
    if action == "auction":
        if auction_mode is None:
            await q.answer("Auction mode is not installed on this bot.", show_alert=True)
        elif games.get(chat_id) or tgames.get(chat_id):
            await q.answer("A cricket match is running here. End it first.", show_alert=True)
        else:
            await auction_mode.open_from_menu(update, ctx)
        return
    if action == "soon":  # modes that are not built yet
        await q.answer(f"{arg.title()} mode is coming soon!", show_alert=True)
        return
    if action == "mc":  # cancel the start menu: starter or a group admin only
        allowed = str(uid) == arg
        if not allowed:
            try:
                allowed = (await ctx.bot.get_chat_member(chat_id, uid)).status in (
                    "administrator", "creator")
            except TelegramError:
                pass
        if not allowed:
            await q.answer("Only the person who started this or an admin can cancel.",
                           show_alert=True)
            return
        await q.answer()
        await edit_any(q.message, "❌ Cancelled.")
        return
    if action == "mode":
        if arg == "team":
            await team_menu(q, ctx)
            return
        if g or chat_id in tgames:
            await q.answer("A game is already running here.", show_alert=True)
            return
        games[chat_id] = Game(chat_id, uid, q.from_user.full_name,
                              chat_title=q.message.chat.title or "")
        await q.answer()
        await edit_any(q.message, 
            "<b>『 🧍 SOLO MODE 』</b>\n" + LINE + "\nSelect balls per bowler spell 👇",
            reply_markup=SPELL_KB, parse_mode=HTML)
        return

    if not g or g.state != "setup" or uid != g.host_id:
        await q.answer("Only the host can use these buttons.", show_alert=True)
        return
    await q.answer()
    if action == "cancel":
        cleanup(ctx, g)
        await edit_any(q.message, "❌ Match cancelled.")
    elif action == "spell":
        g.spell = int(arg)
        await edit_any(q.message, 
            f"👑 <b>HOST:</b> {link(g.host_id, g.host_name)}\n" + LINE +
            "\nSelect gameplay mode 👇", reply_markup=MODE_KB, parse_mode=HTML)
    elif action == "back":
        await edit_any(q.message, 
            "<b>『 🧍 SOLO MODE 』</b>\n" + LINE + "\nSelect balls per bowler spell 👇",
            reply_markup=SPELL_KB, parse_mode=HTML)
    elif action == "gm":
        g.spam_free = arg == "spam"
        await open_lobby(ctx, g, q.message)


async def noop_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()  # intentionally does nothing


async def again_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    if chat_id in games or chat_id in tgames:
        await q.answer("A game is already running here.", show_alert=True)
        return
    _, spell, spam = q.data.split(":")
    g = Game(chat_id, q.from_user.id, q.from_user.full_name,
             spell=int(spell), spam_free=spam == "1",
             chat_title=q.message.chat.title or "")
    games[chat_id] = g
    await q.answer()
    await open_lobby(ctx, g)


# ---------- lobby ----------
async def open_lobby(ctx, g: Game, msg=None):
    g.state = "lobby"
    mode = "🛡 Spam-Free" if g.spam_free else "⚔️ Normal"
    text = (f"<b>『 ⚡ SOLO LOBBY OPEN 』</b>\n{LINE}\n"
            f"👑 Host: {link(g.host_id, g.host_name)}\n"
            f"🎯 Spell: {g.spell} balls | {mode}\n\n"
            f"Join lobby: /joingame\n"
            f"⏳ Auto-locks in {LOBBY_SECONDS}s (Min: {MIN_PLAYERS} players)\n"
            f"{LINE}\n🛡 Admins: Use /forcestart to begin immediately")
    if msg and not MEDIA.get("lobby"):
        await edit_any(msg, text)
    else:
        if msg:
            try:
                await msg.delete()
            except Exception:
                pass
        await say_media(ctx, g.chat_id, "lobby", text)
    ctx.job_queue.run_once(lobby_warn, LOBBY_SECONDS - 30, data=g.chat_id,
                           name=f"warn:{g.chat_id}")
    ctx.job_queue.run_once(lobby_lock, LOBBY_SECONDS, data=g.chat_id,
                           name=f"lobby:{g.chat_id}")


async def lobby_warn(ctx):
    g = games.get(ctx.job.data)
    if g and g.state == "lobby":
        await say(ctx, g.chat_id, "⏳ <b>30 SECONDS REMAINING!</b> Join now: /joingame")


async def lobby_lock(ctx):
    g = games.get(ctx.job.data)
    if not g or g.state != "lobby":
        return
    if len(g.players) < MIN_PLAYERS:
        cleanup(ctx, g)
        await say(ctx, g.chat_id, "❌ Not enough players. Lobby closed.")
        return
    await start_game(ctx, g)


async def joingame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    g = games.get(chat.id)
    if not g or g.state != "lobby":
        await update.message.reply_text("No open lobby. Use /start to create one.")
        return
    if g.duel:
        await update.message.reply_text("⚔️ This is a 1v1 challenge. Tap its Accept button to play.")
        return
    if user.id in g.players:
        await update.message.reply_text("You're already in the lobby.")
        return
    if user_game.get(user.id, chat.id) != chat.id:
        await update.message.reply_text("You're already in a match in another group.")
        return
    if len(g.players) >= MAX_PLAYERS:
        await update.message.reply_text("Lobby is full.")
        return
    dm_ok = True
    try:  # can the bot DM this player yet?
        await ctx.bot.send_message(
            user.id, "✅ You're in the Solo lobby. I'll DM you when it's your turn to bowl.")
    except Forbidden:
        dm_ok = False
    g.players[user.id] = Player(user.id, user.full_name, dm_ok=dm_ok)
    user_game[user.id] = chat.id
    text = f"✅ {link(user.id, user.full_name)} joined! [{len(g.players)}/{MAX_PLAYERS}]"
    kb = None
    if not dm_ok:
        text += ("\n⚠️ Tap the button and press <b>Start</b> so I can DM you "
                 "when it's your turn to bowl.")
        kb = Markup([[Btn("▶️ Start bot in DM",
                          url=f"https://t.me/{ctx.bot.username}?start=register")]])
    await update.message.reply_text(text, reply_markup=kb, parse_mode=HTML)


async def forcestart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id in tgames:
        await team_forcestart(update, ctx, tgames[update.effective_chat.id])
        return
    g = games.get(update.effective_chat.id)
    if not g or g.state != "lobby":
        return
    if not await is_admin(ctx, g, update.effective_user.id):
        await update.message.reply_text("Only admins can force start.")
        return
    if len(g.players) < MIN_PLAYERS:
        await update.message.reply_text(f"Need at least {MIN_PLAYERS} players.")
        return
    await start_game(ctx, g)


def _current_match(chat_id: int):
    return tgames.get(chat_id) or games.get(chat_id)


def _may_end(update: Update, m) -> bool:
    """Only the match host, the bot owner or a sudo user."""
    return update.effective_user.id == m.host_id or owner_ok(update)


async def endgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/endgame - asks for confirmation, then ends the running solo / team / 1v1 match.
    Only the match host, the bot owner or a sudo user can use it."""
    m = _current_match(update.effective_chat.id)
    if not m:
        return
    if not _may_end(update, m):
        await update.message.reply_text("🚫 Only the match host, the bot owner or a sudo user can end this match.")
        return
    await update.message.reply_text(
        "<b>『 🛑 END THE MATCH? 』</b>\n" + LINE + "\n"
        "Press <b>Continue</b> to end the match for everyone, or <b>Cancel</b> to keep playing.",
        reply_markup=Markup([[Btn("✅ Continue", callback_data=f"eg:yes:{id(m)}"),
                              Btn("❌ Cancel", callback_data=f"eg:no:{id(m)}")]]),
        parse_mode=HTML)


async def endgame_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, ans, mid = q.data.split(":")
    m = _current_match(q.message.chat_id)
    if not m or id(m) != int(mid):
        await q.answer("This match is already over.", show_alert=True)
        await edit_any(q.message, "ℹ️ That match has already ended.")
        return
    if not _may_end(update, m):
        await q.answer("Only the match host, the bot owner or a sudo user can do this.", show_alert=True)
        return
    await q.answer()
    if ans == "no":
        await edit_any(q.message, "▶️ <b>Cancelled.</b> The match goes on!")
        return
    if isinstance(m, TeamGame):
        await unpin_reg(ctx, m)
        cleanup_team(ctx, m)
    else:
        if m.duel and m.state == "lobby" and m.duel_msg:
            try:
                await edit_any(m.duel_msg, "🛑 <b>1v1 challenge cancelled.</b>")
            except Exception:
                pass
        cleanup(ctx, m)
    await edit_any(q.message, f"🛑 <b>Match ended</b> by {html.escape(q.from_user.full_name)}.")


# ---------- match flow ----------
async def start_game(ctx, g: Game):
    cancel_jobs(ctx, f"lobby:{g.chat_id}")
    cancel_jobs(ctx, f"warn:{g.chat_id}")
    gone = [p for p in g.players.values() if not p.dm_ok]
    for p in gone:  # bowlers must be reachable in DM
        del g.players[p.uid]
        user_game.pop(p.uid, None)
    if gone:
        await say(ctx, g.chat_id, "⚠️ Removed (didn't start the bot in DM): "
                  + ", ".join(nm(p) for p in gone))
    if len(g.players) < MIN_PLAYERS:
        cleanup(ctx, g)
        await say(ctx, g.chat_id, "❌ Not enough players. Lobby closed.")
        return
    g.order = list(g.players)
    random.shuffle(g.order)  # random batting order -> random first batter
    g.bat_idx, g.bowl_ptr = 0, 0
    pick_bowler(g)           # random-order neighbour becomes first bowler
    order = "\n".join(f"  {i}. {nm(g.players[u])}" for i, u in enumerate(g.order, 1))
    await say_media(ctx, g.chat_id, "solo_arena",
              f"<b>『 🏟 SOLO ARENA: LIVE 』</b>\n{LINE}\n📋 <b>Order:</b>\n{order}\n\n"
              f"🏏 Striker: {nm(striker(g))}\n🎯 Bowler: {nm(g.players[g.bowler_id])}")
    await call_bowl(ctx, g)


def ball_label(g: Game) -> str:
    return f"Ball {striker(g).balls + 1}" if g.duel else f"Bowl {g.spell_balls + 1}/{g.spell}"


async def call_bowl(ctx, g: Game):
    g.state, g.bowl_value = "await_bowl", None
    bowler = g.players[g.bowler_id]
    dm_url = f"https://t.me/{ctx.bot.username}"  # no start param: just opens the DM
    await say_media(ctx, g.chat_id, "bowling_call",
              f"<b>『 ⚾ BOWLING CALL 』</b>\n{LINE}\n{nm(bowler)} · "
              f"{ball_label(g)}\nSend your delivery in DM",
              Markup([[Btn("⚾ Deliver Bowl", url=dm_url)]]))
    try:
        bat = striker(g)
        where = f"🏟 <b>{html.escape(g.chat_title)}</b>\n" if g.chat_title else ""
        await ctx.bot.send_message(
            bowler.uid,
            f"📊 <b>LIVE MATCH</b>\n{where}\n"
            f"🏏 <b>Batter:</b> {nm(bat)} ({bat.runs} off {bat.balls})\n"
            f"💥 <b>Form:</b> {' '.join(g.form[-6:]) or 'None'}\n"
            + (f"🥎 <b>Ball:</b> {bat.balls + 1}\n" if g.duel else
               f"🥎 <b>Over:</b> {g.spell_balls}/{g.spell} balls.\n") +
            f"🔻 <b>Your turn:</b> type a number (1–6) here to bowl.",
            parse_mode=HTML)
    except Forbidden:
        pass
    schedule_move(ctx, g)


async def dm_delivery(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tg = tgames.get(user_game.get(uid))
    if tg:
        await team_dm(update, ctx, tg)
        return
    g = games.get(user_game.get(uid))
    if not g or g.state != "await_bowl" or g.bowler_id != uid:
        await update.message.reply_text("It's not your turn to bowl right now.")
        return
    g.bowl_value = int(update.message.text)
    cancel_jobs(ctx, f"move:{g.chat_id}")
    await update.message.reply_text(f"✅ Delivery locked: {g.bowl_value}")
    await call_bat(ctx, g)


async def call_bat(ctx, g: Game):
    g.state = "await_bat"
    bt = striker(g)
    await say_media(ctx, g.chat_id, "face_ball",
              f"<b>『 🏏 FACE THE BALL 』</b>\n{LINE}\n{nm(bt)} <b>{bt.runs}[{bt.balls}]</b> · "
              f"{ball_label(g)}\nSend your shot (1–6) here")
    schedule_move(ctx, g)


async def group_shot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id in tgames:
        await team_shot(update, ctx)
        return
    g = games.get(update.effective_chat.id)
    if not g or g.state not in ("await_bowl", "await_bat"):
        return
    uid = update.effective_user.id
    if g.state == "await_bat" and uid == g.order[g.bat_idx]:
        if update.message.text == "0":  # solo batters play 1-6 only; the timer keeps running
            await update.message.reply_text("Bat from 1-6")
            return
        await resolve(ctx, g, int(update.message.text))
    elif g.spam_free:  # keep the chat clean
        try:
            await update.message.delete()
        except Exception:
            pass


def schedule_move(ctx, g: Game):
    """Start the countdown for the current bowler/batter."""
    g.ball_no += 1
    name = f"move:{g.chat_id}"
    cancel_jobs(ctx, name)
    for left in WARN_AT + [0]:
        ctx.job_queue.run_once(move_tick, MOVE_TIMEOUT - left,
                               data=(g.chat_id, g.ball_no, left), name=name)


async def move_tick(ctx):
    chat_id, ball_no, left = ctx.job.data
    g = games.get(chat_id)
    if not g or g.ball_no != ball_no or g.state not in ("await_bowl", "await_bat"):
        return
    bowling = g.state == "await_bowl"
    p = g.players[g.bowler_id] if bowling else striker(g)
    if left:
        text = f"⏰ {nm(p)}, only <b>{left}s</b> left to {'bowl' if bowling else 'play your shot'}!"
        await say(ctx, chat_id, text)
        if bowling:
            try:
                await ctx.bot.send_message(p.uid, text, parse_mode=HTML)
            except Forbidden:
                pass
        return
    await timeout_player(ctx, g, p, bowling)


async def timeout_player(ctx, g: Game, p: Player, bowling: bool):
    """Remove an AFK bowler/batter from the match and carry on."""
    g.state = "busy"
    cancel_jobs(ctx, f"move:{g.chat_id}")
    if g.duel:  # in a 1v1 an AFK player forfeits
        await say(ctx, g.chat_id, f"⛔ {nm(p)} timed out. The 1v1 is forfeited!")
        await finish_duel(ctx, g, forfeit_uid=p.uid)
        return
    await say(ctx, g.chat_id,
              f"⛔ {nm(p)} timed out and was <b>removed</b> from the match.")
    i = g.order.index(p.uid)
    g.order.pop(i)
    g.removed.add(p.uid)
    user_game.pop(p.uid, None)
    if i < g.bat_idx:
        g.bat_idx -= 1
    if i < g.bowl_ptr:
        g.bowl_ptr -= 1
    if len(g.order) < 2:
        await finish(ctx, g)
        return
    if bowling:  # the next player in line takes over the over
        g.bowl_ptr = (i - 1) % len(g.order)
        pick_bowler(g)
    else:        # same index now points at the next batter
        g.form = []
        if g.bat_idx >= len(g.order):
            await finish(ctx, g)
            return
        if g.bowler_id == g.order[g.bat_idx]:
            pick_bowler(g)
        await say_media(ctx, g.chat_id, "new_batter",
                        f"🟢 <b>NEW BATTER IN!</b>\n\n{nm(striker(g))} takes guard at the crease. Play continues!")
    await call_bowl(ctx, g)


async def resolve(ctx, g: Game, shot: int):
    g.state = "busy"
    cancel_jobs(ctx, f"move:{g.chat_id}")
    bat, bowl = striker(g), g.players[g.bowler_id]
    bat.balls += 1
    bowl.balls_bowled += 1
    g.spell_balls += 1

    if shot == g.bowl_value:
        bowl.wickets += 1
        bowl.wkt_streak += 1
        bat.ducks += bat.runs == 0
        g.form.append("W")
        kind = "DUCK" if bat.runs == 0 else "BOWLED"
        await say_media(ctx, g.chat_id, "duck" if bat.runs == 0 else "wicket",
                  f"⚠️ <b>WICKET!</b>\n\n<b>{kind}</b> · {nm(bowl)} STRIKES!\n\n"
                  f"❌ {nm(bat)} dismissed for <b>{bat.runs}</b> ({bat.balls}b)\n"
                  f"└ The stumps are shattered!")
        if bowl.wkt_streak == 3:
            bowl.hattricks += 1
            await say_media(ctx, g.chat_id, "hattrick",
                            f"🎩 <b>HAT-TRICK!</b> {nm(bowl)} takes three in three balls!")
        g.bat_idx += 1
        g.form = []
        if g.bat_idx >= len(g.order):
            await finish(ctx, g)
            return
        if g.bowler_id == g.order[g.bat_idx]:
            pick_bowler(g)
        await say_media(ctx, g.chat_id, "new_batter",
                  f"🟢 <b>NEW BATTER IN!</b>\n\n{nm(striker(g))} takes guard at the crease. Play continues!")
        if g.duel:
            await say(ctx, g.chat_id, f"🎯 {nm(striker(g))} needs <b>{g.players[g.order[0]].runs + 1}</b> to win!")
    else:
        before = bat.runs
        bowl.wkt_streak = 0
        bat.runs += shot
        bowl.runs_conceded += shot
        bat.fours += shot == 4
        bat.sixes += shot == 6
        g.form.append(str(shot))
        await say_media(ctx, g.chat_id, f"run_{shot}",
                        f"🚀 <b>{shot} RUNS</b> · {nm(bat)} {bat.runs} ({bat.balls}b)\n<blockquote>{COMMENT[shot]}</blockquote>")
        bat.fifties += before < 50 <= bat.runs
        bat.hundreds += before < 100 <= bat.runs
        if before < 100 <= bat.runs:
            await say_media(ctx, g.chat_id, "century",
                            f"💯 <b>CENTURY!</b> {nm(bat)} reaches 100 off {bat.balls} balls!")
        elif before < 50 <= bat.runs:
            await say_media(ctx, g.chat_id, "fifty",
                            f"🔥 <b>HALF-CENTURY!</b> {nm(bat)} reaches 50 off {bat.balls} balls!")

    if g.duel and g.bat_idx == 1 and bat.runs > g.players[g.order[0]].runs:
        await finish_duel(ctx, g)  # the chaser has passed the target
        return
    if g.spell_balls >= g.spell:
        pick_bowler(g)
        await say(ctx, g.chat_id, f"🔔 <b>OVER COMPLETE</b>\n"
                                  f"⚾ Next bowler: {nm(g.players[g.bowler_id])}")
    await call_bowl(ctx, g)


async def finish(ctx, g: Game):
    if g.duel:
        await finish_duel(ctx, g)
        return
    ps = list(g.players.values())
    active = [p for p in ps if p.uid not in g.removed] or ps
    total_runs = sum(p.runs for p in ps)
    total_balls = sum(p.balls for p in ps)
    total_wkts = sum(p.wickets for p in ps)
    lines = []
    for i, p in enumerate(ps, 1):
        sr = p.runs / p.balls * 100 if p.balls else 0
        overs = f"{p.balls_bowled // 6}.{p.balls_bowled % 6}"
        econ = p.runs_conceded / (p.balls_bowled / 6) if p.balls_bowled else 0
        lines.append(f"{i:02d} · {nm(p)}{' ⛔ removed' if p.uid in g.removed else ''} — <b>{p.runs}</b> ({p.balls})\n"
                     f"   ├ 4s {p.fours} · 6s {p.sixes} · SR {sr:.1f} · Wkts {p.wickets}\n"
                     f"   └ Bowled {overs} · Economy {econ:.2f}")
    orange = max(active, key=lambda p: p.runs)
    purple = max(active, key=lambda p: p.wickets)
    potm = max(active, key=lambda p: p.runs + 5 * p.wickets)
    reward_txt = save_match(active, orange, purple, potm)
    text = (f"<b>『 🏁 SOLO MATCH · FINAL SCORECARD 』</b>\n"
            f"📊 <b>{total_runs}</b> runs · <b>{total_balls}</b> balls · <b>{total_wkts}</b> wickets\n{LINE}\n"
            f"<b>PLAYER SCORES</b>\n\n" + "\n\n".join(lines) + f"\n{LINE}\n"
            f"🟠 <b>ORANGE CAP</b> ➜ {nm(orange)} · {orange.runs} runs\n"
            f"🟣 <b>PURPLE CAP</b> ➜ {nm(purple)} · {purple.wickets} wickets\n"
            f"🏆 <b>PLAYER OF THE MATCH</b> ➜ {nm(potm)}\n{LINE}\n"
            f"🎁 <b>REWARDS</b>\n<blockquote>{reward_txt}</blockquote>\n"
            f"🏆 <b>MATCH COMPLETED</b>")
    if MEDIA.get("scorecard"):
        await say_media(ctx, g.chat_id, "scorecard", "📊 <b>FINAL SCORECARD</b>")
    if MEDIA.get("potm"):
        await say_media(ctx, g.chat_id, "potm",
                        f"🏆 <b>PLAYER OF THE MATCH</b>\n{nm(potm)}")
    again = f"again:{g.spell}:{int(g.spam_free)}"
    cleanup(ctx, g)
    await say(ctx, g.chat_id, text,
              Markup([[Btn("🔄 Play Again", callback_data=again),
                       Btn("🎮 Other Modes", callback_data="other")]]))


# =====================================================================
#                              TEAM MODE
# =====================================================================
TEAM_LOBBY_SECONDS = 90   # registration window
CAPTAIN_SECONDS = 60      # time to claim captaincy before auto-assign
OVERS_SECONDS = 90        # host time to /setover before defaulting to 2 overs
MIN_TEAM, MAX_TEAM, MAX_OVERS = 2, 11, 20
ICON = {"A": "🔴", "B": "🔵"}
PLAY_STATES = ("pick_bat", "pick_bowl", "await_bowl", "await_bat", "busy")
WAIT_STATES = ("pick_bat", "pick_bowl", "await_bowl", "await_bat")
TOSS_STATES = ("toss_call", "toss_choice")
LOBBY_WARN = {
    60: "⏳ <b>1 MINUTE REMAINING!</b> Join Team A or Team B now!",
    30: "⏳ <b>30 SECONDS REMAINING!</b> Choose your side before registration locks!",
    15: "🚨 <b>15 SECONDS REMAINING!</b> Last chance to pick your team!",
    5: "⚠️ <b>5 SECONDS REMAINING!</b> Locking teams now!",
}
HOST_TEXT = "<b>『 👑 HOST SELECTION 』</b>\n" + LINE + "\nClaim the match host seat 👇"
HOST_KB = Markup([[Btn("👑 I am Host", callback_data="t:host"),
                   Btn("❌ Cancel", callback_data="t:cancel")]])
TMODE_KB = Markup([[Btn("🛡 Spam-Free", callback_data="t:gm:spam"),
                    Btn("⚔️ Normal", callback_data="t:gm:normal")],
                   [Btn("⬅️ Back", callback_data="t:back")]])
TEAM_KB = Markup([[Btn("🔴 Join Team A", callback_data="t:join:A"),
                   Btn("🔵 Join Team B", callback_data="t:join:B")],
                  [Btn("🚪 Leave Lobby", callback_data="t:leave")]])
CAP_KB = Markup([[Btn("🎖 Captain · Team A", callback_data="t:cap:A"),
                  Btn("🎖 Captain · Team B", callback_data="t:cap:B")]])


@dataclass
class TeamGame:
    chat_id: int
    host_id: Optional[int] = None
    host_name: str = ""
    spam_free: bool = True
    chat_title: str = ""
    # host | mode | lobby | closed | captains | overs | toss_call | toss_choice
    # | pick_bat | pick_bowl
    # | await_bowl | await_bat | busy
    state: str = "host"
    teams: dict = field(default_factory=lambda: {"A": [], "B": []})
    players: dict = field(default_factory=dict)    # uid -> Player
    captains: dict = field(default_factory=dict)   # "A"/"B" -> uid
    overs: int = 0
    innings: int = 1
    bat: str = "A"                 # team currently batting
    score: int = 0
    wkts: int = 0
    balls: int = 0                 # legal balls bowled this innings
    striker: Optional[int] = None
    non_striker: Optional[int] = None
    bowler: Optional[int] = None
    last_bowler: Optional[int] = None
    bowl_value: Optional[int] = None
    out: set = field(default_factory=set)
    first: Optional[dict] = None   # summary of the first innings
    target: int = 0
    seq: int = 0                   # invalidates stale countdown jobs
    reg_msg: Optional[int] = None
    toss_caller: str = ""          # team whose captain calls the toss
    toss_winner: str = ""
    hv: Optional[dict] = None      # active vote to change host
    bat_list_shown: bool = False   # batting list is posted only once per innings
    bowl_list_shown: bool = False  # bowling list is posted only once per innings
    carry: int = 0                 # penalty carried into the next innings' score


tgames: dict = {}  # chat_id -> TeamGame


# ---------- team helpers ----------
def other(t: str) -> str:
    return "B" if t == "A" else "A"


def tname(t: str) -> str:
    return f"{ICON[t]} Team {t}"


def team_of(g: TeamGame, uid: int):
    for t in "AB":
        if uid in g.teams[t]:
            return t
    return None


def fmt_secs(s: int) -> str:
    m, s = divmod(s, 60)
    parts = []
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(parts)


def teams_body(g: TeamGame) -> str:
    out = []
    for t in "AB":
        n = len(g.teams[t])
        cap = g.captains.get(t)
        out.append(f"{tname(t)} · {n} player{'s' if n != 1 else ''}"
                   + (f" · © {nm(g.players[cap])}" if cap else ""))
        out += [f"  {i}. {nm(g.players[u])}" for i, u in enumerate(g.teams[t], 1)] or ["  —"]
        out.append("")
    return "\n".join(out).rstrip()


def roster(g: TeamGame, team: str) -> str:
    rows = []
    for i, uid in enumerate(g.teams[team], 1):
        tag = " ©" if g.captains.get(team) == uid else ""
        if team == g.bat:
            if uid in g.out:
                tag += " ❌ out"
            elif uid == g.striker:
                tag += " 🏏 striker"
            elif uid == g.non_striker:
                tag += " 🏏"
        elif uid == g.bowler:
            tag += " 🎯 bowling"
        elif uid == g.last_bowler:
            tag += " 🚫 bowled last over"
        rows.append(f"{i}. {nm(g.players[uid])}{tag}")
    return "\n".join(rows)


def lineups_text(g: TeamGame) -> str:
    bt, wt = g.bat, other(g.bat)
    return (f"🏏 <b>BATTING · {tname(bt)}</b>\n{roster(g, bt)}\n{LINE}\n"
            f"⚾ <b>BOWLING · {tname(wt)}</b>\n{roster(g, wt)}")


def score_line(g: TeamGame) -> str:
    return f"{ICON[g.bat]} <b>{g.score}/{g.wkts}</b> ({g.balls // 6}.{g.balls % 6} ov)"


def chase_line(g: TeamGame) -> str:
    if g.innings != 2:
        return ""
    return (f"\n🎯 Need <b>{max(g.target - g.score, 0)}</b> from "
            f"<b>{g.overs * 6 - g.balls}</b> balls")


def eligible_batters(g: TeamGame) -> list:
    return [u for u in g.teams[g.bat]
            if u not in g.out and u not in (g.striker, g.non_striker)]


def eligible_bowlers(g: TeamGame) -> list:
    return [u for u in g.teams[other(g.bat)] if u != g.last_bowler]


def innings_over(g: TeamGame) -> bool:
    return (g.balls >= g.overs * 6
            or g.wkts >= len(g.teams[g.bat]) - 1
            or (g.innings == 2 and g.score >= g.target))


def reset_innings(g: TeamGame):
    g.score = g.wkts = g.balls = 0
    g.striker = g.non_striker = g.bowler = g.last_bowler = g.bowl_value = None
    g.out = set()
    g.bat_list_shown = g.bowl_list_shown = False
    for p in g.players.values():
        p.wkt_streak = 0


async def say_long(ctx, chat_id, text, markup=None):
    """Send text split on line breaks so it stays under Telegram's 4096 limit."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for i, c in enumerate(chunks):
        await say(ctx, chat_id, c.rstrip("\n"), markup if i == len(chunks) - 1 else None)


async def dm_ready(ctx, uid: int, text: str) -> bool:
    """Send a DM; False means the player has not started the bot."""
    try:
        await ctx.bot.send_message(uid, text, parse_mode=HTML)
        return True
    except TelegramError:
        return False


def start_dm_kb(ctx):
    return Markup([[Btn("▶️ Start bot in DM",
                        url=f"https://t.me/{ctx.bot.username}?start=register")]])


def cleanup_team(ctx, g: TeamGame):
    for n in ("tlobby", "tcap", "tover", "ttoss", "tmove", "thv"):
        cancel_jobs(ctx, f"{n}:{g.chat_id}")
    for uid in list(g.players):
        user_game.pop(uid, None)
    tgames.pop(g.chat_id, None)


async def unpin_reg(ctx, g: TeamGame):
    if g.reg_msg:
        try:
            await ctx.bot.unpin_chat_message(g.chat_id, g.reg_msg)
        except Exception:
            pass
        g.reg_msg = None


# ---------- menu / setup callbacks ----------
async def team_menu(q, ctx):
    """Called from the Solo/Team menu when someone taps Team."""
    chat_id = q.message.chat_id
    if q.message.chat.type == ChatType.PRIVATE:
        await q.answer("Team mode works inside groups.", show_alert=True)
        return
    if chat_id in games or chat_id in tgames:
        await q.answer("A game is already running here.", show_alert=True)
        return
    tgames[chat_id] = TeamGame(chat_id, chat_title=q.message.chat.title or "")
    await q.answer()
    await edit_any(q.message, HOST_TEXT, reply_markup=HOST_KB)


async def team_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id, user = q.message.chat_id, q.from_user
    parts = q.data.split(":")
    act = parts[1]

    if act == "again":
        if chat_id in games or chat_id in tgames:
            await q.answer("A game is already running here.", show_alert=True)
            return
        tgames[chat_id] = TeamGame(chat_id, chat_title=q.message.chat.title or "")
        await q.answer()
        await say(ctx, chat_id, HOST_TEXT, HOST_KB)
        return

    g = tgames.get(chat_id)
    if not g:
        await q.answer("This match has ended.", show_alert=True)
        return

    if act == "host":
        if g.state != "host":
            await q.answer("This match already has a host.", show_alert=True)
            return
        g.host_id, g.host_name, g.state = user.id, user.full_name, "mode"
        await q.answer()
        await edit_any(q.message,
                       f"👑 <b>HOST:</b> {link(user.id, user.full_name)}\n{LINE}\n"
                       "Select gameplay mode 👇", reply_markup=TMODE_KB)

    elif act == "cancel":
        if g.state != "host":
            await q.answer("Too late to cancel here. Admins can use /endgame.", show_alert=True)
            return
        cleanup_team(ctx, g)
        await q.answer()
        await edit_any(q.message, "❌ Match cancelled.")

    elif act in ("back", "gm"):
        if g.state != "mode" or user.id != g.host_id:
            await q.answer("Only the host can use these buttons.", show_alert=True)
            return
        await q.answer()
        if act == "back":
            g.host_id, g.host_name, g.state = None, "", "host"
            await edit_any(q.message, HOST_TEXT, reply_markup=HOST_KB)
        else:
            g.spam_free = parts[2] == "spam"
            await edit_any(q.message, "✅ <b>Match rules saved.</b>")
            await open_team_lobby(ctx, g)

    elif act == "join":
        side = parts[2]
        if g.state != "lobby":
            await q.answer("Registration isn't open.", show_alert=True)
            return
        cur = team_of(g, user.id)
        if cur:
            await q.answer()
            await say(ctx, chat_id, f"⚠️ {link(user.id, user.full_name)}: You are already in "
                                    f"{tname(cur)}. Use Leave Lobby first to switch sides.")
            return
        if user_game.get(user.id, chat_id) != chat_id:
            await q.answer("You're already in a match in another group.", show_alert=True)
            return
        if len(g.teams[side]) >= MAX_TEAM:
            await q.answer(f"{tname(side)} is full.", show_alert=True)
            return
        await q.answer()
        text, kb = await place(ctx, g, user.id, user.full_name, side)
        await say(ctx, chat_id, text, kb)

    elif act == "leave":
        if g.state != "lobby":
            await q.answer("You can't leave once registration has closed.", show_alert=True)
            return
        side = team_of(g, user.id)
        if not side:
            await q.answer("You haven't joined a team.", show_alert=True)
            return
        g.teams[side].remove(user.id)
        g.players.pop(user.id, None)
        user_game.pop(user.id, None)
        await q.answer()
        await say(ctx, chat_id, f"👋 {link(user.id, user.full_name)} left the lobby.")

    elif act == "hv":
        side = team_of(g, user.id)
        if not g.hv:
            await q.answer("This vote has ended.", show_alert=True)
            return
        if not side:
            await q.answer("Only players in the match can vote.", show_alert=True)
            return
        if user.id in g.hv["votes"][side]:
            await q.answer("You've already voted.", show_alert=True)
            return
        g.hv["votes"][side].add(user.id)
        await q.answer("Vote counted ✅")
        v = g.hv
        if all(len(v["votes"][t]) >= HOST_VOTES for t in "AB"):
            await edit_any(q.message, "✅ <b>Vote passed.</b>")
            await set_host(ctx, g, v["uid"], v["name"])
        else:
            await edit_any(q.message, hv_text(g), reply_markup=HV_KB)

    elif act == "toss":
        if g.state != "toss_call":
            await q.answer("The toss isn't open.", show_alert=True)
            return
        if user.id != g.captains[g.toss_caller]:
            await q.answer(f"Only the {tname(g.toss_caller)} captain calls the toss.", show_alert=True)
            return
        await q.answer()
        await toss_flip(ctx, g, parts[2])

    elif act == "opt":
        if g.state != "toss_choice":
            await q.answer("There's no toss decision to make.", show_alert=True)
            return
        if user.id != g.captains[g.toss_winner]:
            await q.answer(f"Only the {tname(g.toss_winner)} captain chooses.", show_alert=True)
            return
        await q.answer()
        await toss_decide(ctx, g, parts[2])

    elif act == "cap":
        side = parts[2]
        if g.state != "captains":
            await q.answer("Captain selection isn't open.", show_alert=True)
            return
        if team_of(g, user.id) != side:
            await q.answer(f"Only {tname(side)} players can pick its captain.", show_alert=True)
            return
        if side in g.captains:
            await q.answer(f"{tname(side)} already has a captain.", show_alert=True)
            return
        g.captains[side] = user.id
        await q.answer()
        await say(ctx, chat_id, f"🎖 {link(user.id, user.full_name)} is now the captain of {tname(side)}!")
        if len(g.captains) == 2:
            await after_captains(ctx, g)


# ---------- registration ----------
async def place(ctx, g: TeamGame, uid: int, name: str, side: str, by_host=False):
    """Put a player into a team (moving them if needed). Returns (text, keyboard)."""
    for t in "AB":
        if uid in g.teams[t]:
            g.teams[t].remove(uid)
    p = g.players.get(uid)
    if not p:
        p = g.players[uid] = Player(uid, name, batted=False, dm_ok=False)
    p.name = name
    if not p.dm_ok:
        p.dm_ok = await dm_ready(
            ctx, uid, f"✅ You're in {tname(side)}. I'll DM you here if you're picked to bowl.")
    g.teams[side].append(uid)
    user_game[uid] = g.chat_id
    n = len(g.teams[side])
    head = (f"👑 Host added 👤 {link(uid, name)} to {tname(side)}!" if by_host
            else f"👤 {link(uid, name)} joined {tname(side)}!")
    text = (f"<b>『 ✅ NEW PLAYER IN! 』</b>\n{LINE}\n{head}\n"
            f"Team size: <b>{n}</b> player{'s' if n != 1 else ''} ⚡")
    kb = None
    if not p.dm_ok:
        text += "\n⚠️ Tap below and press <b>Start</b> so I can DM you if you're picked to bowl."
        kb = start_dm_kb(ctx)
    return text, kb


async def open_team_lobby(ctx, g: TeamGame):
    g.state = "lobby"
    cancel_jobs(ctx, f"tlobby:{g.chat_id}")
    text = (f"<b>『 ✅ TEAM REGISTRATION IS OPEN 』</b>\n{LINE}\n"
            f"⏳ Lobby closes in <b>{fmt_secs(TEAM_LOBBY_SECONDS)}</b>\n"
            f"👥 Minimum <b>{MIN_TEAM} players</b> per side\n\n"
            "<i>Tap a team to join — switched sides? Hit 🚪 Leave Lobby and "
            "pick again before the lock!</i> 👇")
    m = await say_media(ctx, g.chat_id, "team_lobby", text, TEAM_KB)
    g.reg_msg = m.message_id
    try:
        await ctx.bot.pin_chat_message(g.chat_id, m.message_id, disable_notification=True)
    except Exception:
        pass
    ctx.job_queue.run_once(t_lobby_lock, TEAM_LOBBY_SECONDS, data=g.chat_id,
                           name=f"tlobby:{g.chat_id}")
    for left in LOBBY_WARN:
        ctx.job_queue.run_once(t_lobby_warn, TEAM_LOBBY_SECONDS - left,
                               data=(g.chat_id, left), name=f"tlobby:{g.chat_id}")


async def t_lobby_warn(ctx):
    chat_id, left = ctx.job.data
    g = tgames.get(chat_id)
    if g and g.state == "lobby":
        await say(ctx, chat_id, LOBBY_WARN[left])


async def t_lobby_lock(ctx):
    g = tgames.get(ctx.job.data)
    if g and g.state == "lobby":
        await lock_teams(ctx, g)


async def lock_teams(ctx, g: TeamGame):
    cancel_jobs(ctx, f"tlobby:{g.chat_id}")
    a, b = len(g.teams["A"]), len(g.teams["B"])
    if a < MIN_TEAM or b < MIN_TEAM:
        g.state = "closed"
        await unpin_reg(ctx, g)
        await say(ctx, g.chat_id,
                  f"<b>『 ⚠️ REGISTRATION CLOSED 』</b>\n{LINE}\n"
                  f"{tname('A')} — {a}\n{tname('B')} — {b}\n\n"
                  f"❌ Not enough players on both sides yet.\n"
                  f"👑 {link(g.host_id, g.host_name)}: use <code>/add A</code> or "
                  f"<code>/add B</code>, or /rejointeams to reopen registration.")
        return
    await start_captains(ctx, g)


async def start_captains(ctx, g: TeamGame):
    g.state, g.captains = "captains", {}
    await unpin_reg(ctx, g)
    await say_media(ctx, g.chat_id, "captain",
                    f"<b>『 🔒 TEAMS LOCKED 』</b>\n{LINE}\n{teams_body(g)}\n{LINE}\n"
                    f"🎖 One player from each team taps their button to become captain.\n"
                    f"⏳ Auto-assigned in {CAPTAIN_SECONDS}s if nobody claims it.", CAP_KB)
    ctx.job_queue.run_once(t_cap_timeout, CAPTAIN_SECONDS, data=g.chat_id,
                           name=f"tcap:{g.chat_id}")


async def t_cap_timeout(ctx):
    g = tgames.get(ctx.job.data)
    if not g or g.state != "captains":
        return
    for t in "AB":
        if t not in g.captains:
            uid = random.choice(g.teams[t])
            g.captains[t] = uid
            await say(ctx, g.chat_id, f"🎲 No captain claimed — {nm(g.players[uid])} "
                                      f"was auto-assigned captain of {tname(t)}.")
    await after_captains(ctx, g)


async def after_captains(ctx, g: TeamGame):
    if g.state != "captains":
        return
    cancel_jobs(ctx, f"tcap:{g.chat_id}")
    g.state = "overs"
    if g.overs:  # host already ran /setover
        await begin_toss(ctx, g)
        return
    await say(ctx, g.chat_id,
              f"👑 {link(g.host_id, g.host_name)}, set the match length with "
              f"<code>/setover &lt;overs&gt;</code> (1–{MAX_OVERS})\n"
              f"⏳ Defaults to 2 overs in {OVERS_SECONDS}s.")
    ctx.job_queue.run_once(t_over_default, OVERS_SECONDS, data=g.chat_id,
                           name=f"tover:{g.chat_id}")


async def t_over_default(ctx):
    g = tgames.get(ctx.job.data)
    if g and g.state == "overs":
        g.overs = 2
        await say(ctx, g.chat_id, "⏱ The host didn't set the overs, so the match is <b>2 overs</b> a side.")
        await begin_toss(ctx, g)


# ---------- commands ----------
def find_target(msg, args):
    """Who does a command point at? reply > text mention > username argument.
    Returns ((uid, name), None), (None, error_text) or (None, None)."""
    rep = msg.reply_to_message
    if rep and rep.from_user and not rep.from_user.is_bot:
        return (rep.from_user.id, rep.from_user.full_name), None
    for e in msg.entities or []:
        if e.type == "text_mention" and e.user:
            return (e.user.id, e.user.full_name), None
    if args:
        hit = known_unames.get(args[0].lstrip("@").lower())
        if hit:
            return hit, None
        return None, (f"I haven't seen {html.escape(args[0])} in a chat with me yet. Ask them to "
                      "send any message here, or reply to their message with the command.")
    return None, None


async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/add A|B @username  - or reply to a player's message with /add A|B. Works at any stage."""
    chat, user, msg = update.effective_chat, update.effective_user, update.message
    g = tgames.get(chat.id)
    if not g or g.state in ("host", "mode"):
        await msg.reply_text("There's no team match to add players to.")
        return
    if not await is_admin(ctx, g, user.id):
        await msg.reply_text("Only the host or an admin can add players.")
        return
    usage = ("Usage: <code>/add A @username</code> or <code>/add B @username</code>\n"
             "Or reply to the player's message with <code>/add A</code>.")
    if not ctx.args or ctx.args[0].upper() not in ("A", "B"):
        await msg.reply_text(usage, parse_mode=HTML)
        return
    side = ctx.args[0].upper()
    target, err = find_target(msg, ctx.args[1:])
    if err:
        await msg.reply_text(err, parse_mode=HTML)
        return
    if not target:
        await msg.reply_text(usage, parse_mode=HTML)
        return
    uid, name = target
    cur = team_of(g, uid)
    if cur == side:
        await msg.reply_text(f"{name} is already in {tname(side)}.")
        return
    if cur and g.state not in ("lobby", "closed"):
        await msg.reply_text(f"{name} is already in {tname(cur)}. Players can't switch "
                             "sides once the teams are locked.")
        return
    if user_game.get(uid, chat.id) != chat.id:
        await msg.reply_text("That player is already in a match in another group.")
        return
    if len(g.teams[side]) >= MAX_TEAM:
        await msg.reply_text(f"{tname(side)} is full.")
        return
    text, kb = await place(ctx, g, uid, name, side, by_host=True)
    await say(ctx, chat.id, text, kb)
    if g.state == "closed" and min(len(g.teams["A"]), len(g.teams["B"])) >= MIN_TEAM:
        await start_captains(ctx, g)


async def changecap_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/changecap A|B @username - host swaps a team's captain at any point."""
    chat, user, msg = update.effective_chat, update.effective_user, update.message
    g = tgames.get(chat.id)
    if not g or g.state in ("host", "mode", "lobby", "closed"):
        await msg.reply_text("Captains haven't been chosen yet.")
        return
    if user.id != g.host_id:
        await msg.reply_text("Only the host can change a captain.")
        return
    usage = ("Usage: <code>/changecap A @username</code> or <code>/changecap B @username</code>\n"
             "Or reply to the player's message with <code>/changecap A</code>.")
    if not ctx.args or ctx.args[0].upper() not in ("A", "B"):
        await msg.reply_text(usage, parse_mode=HTML)
        return
    side = ctx.args[0].upper()
    target, err = find_target(msg, ctx.args[1:])
    if err:
        await msg.reply_text(err, parse_mode=HTML)
        return
    if not target:
        await msg.reply_text(usage, parse_mode=HTML)
        return
    uid, name = target
    if team_of(g, uid) != side:
        await msg.reply_text(f"{name} isn't in {tname(side)}. Add them first with /add {side}.")
        return
    old = g.captains.get(side)
    if old == uid:
        await msg.reply_text(f"{name} is already the captain of {tname(side)}.")
        return
    g.captains[side] = uid
    was = f"{nm(g.players[old])} → " if old else ""
    await say(ctx, chat.id, f"🎖 <b>CAPTAIN CHANGED</b> · {tname(side)}\n{was}{nm(g.players[uid])}")
    if g.state == "captains" and len(g.captains) == 2:
        await after_captains(ctx, g)


async def changehost_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/changehost - the current host replies to a player (or names @username) to hand over."""
    chat, user, msg = update.effective_chat, update.effective_user, update.message
    g = tgames.get(chat.id) or games.get(chat.id)
    if not g or not g.host_id:
        await msg.reply_text("There's no match with a host here.")
        return
    if user.id != g.host_id:
        await msg.reply_text("Only the current host can hand over hosting. "
                             "Team players can use /votehost.")
        return
    target, err = find_target(msg, ctx.args)
    if err:
        await msg.reply_text(err, parse_mode=HTML)
        return
    if not target:
        await msg.reply_text("Reply to the new host's message with /changehost, "
                             "or use <code>/changehost @username</code>.", parse_mode=HTML)
        return
    if target[0] == g.host_id:
        await msg.reply_text("They're already the host.")
        return
    await set_host(ctx, g, *target)


async def set_host(ctx, g, uid: int, name: str):
    old = g.host_name
    g.host_id, g.host_name = uid, name
    if isinstance(g, TeamGame) and g.hv:
        g.hv = None
        cancel_jobs(ctx, f"thv:{g.chat_id}")
    await say(ctx, g.chat_id, f"👑 <b>HOST CHANGED</b>\n{html.escape(old)} → {link(uid, name)}")


HV_KB = Markup([[Btn("✅ Vote", callback_data="t:hv")]])
HOST_VOTES = 2   # votes needed from EACH team
HV_SECONDS = 90


def hv_text(g: TeamGame) -> str:
    v = g.hv
    return (f"<b>『 🗳 VOTE TO CHANGE HOST 』</b>\n{LINE}\n"
            f"Proposed host: {link(v['uid'], v['name'])}\n"
            f"Needs <b>{HOST_VOTES}</b> votes from each team.\n\n"
            f"{tname('A')}: <b>{len(v['votes']['A'])}/{HOST_VOTES}</b>\n"
            f"{tname('B')}: <b>{len(v['votes']['B'])}/{HOST_VOTES}</b>\n\n"
            f"⏳ Closes in {HV_SECONDS}s")


async def votehost_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/votehost @username - a team player proposes a new host (2 votes from each team)."""
    chat, user, msg = update.effective_chat, update.effective_user, update.message
    g = tgames.get(chat.id)
    if not g or g.state in ("host", "mode"):
        await msg.reply_text("There's no team match here yet.")
        return
    side = team_of(g, user.id)
    if not side:
        await msg.reply_text("Only players in the match can start a host vote.")
        return
    if g.hv:
        await msg.reply_text("A host vote is already running.")
        return
    target, err = find_target(msg, ctx.args)
    if err:
        await msg.reply_text(err, parse_mode=HTML)
        return
    if not target:
        await msg.reply_text("Reply to a player's message with /votehost, "
                             "or use <code>/votehost @username</code>.", parse_mode=HTML)
        return
    uid, name = target
    if uid == g.host_id:
        await msg.reply_text(f"{name} is already the host.")
        return
    if not team_of(g, uid):
        await msg.reply_text("The new host must be a player in this match.")
        return
    g.hv = {"uid": uid, "name": name, "votes": {"A": set(), "B": set()}, "id": random.random()}
    g.hv["votes"][side].add(user.id)
    await say(ctx, chat.id, hv_text(g), HV_KB)
    ctx.job_queue.run_once(t_hv_expire, HV_SECONDS, data=(chat.id, g.hv["id"]),
                           name=f"thv:{chat.id}")


async def t_hv_expire(ctx):
    chat_id, vid = ctx.job.data
    g = tgames.get(chat_id)
    if g and g.hv and g.hv["id"] == vid:
        g.hv = None
        await say(ctx, chat_id, "⌛ The host vote expired without enough votes.")


async def rejointeams_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    g = tgames.get(update.effective_chat.id)
    if not g or g.state not in ("closed", "captains", "overs") + TOSS_STATES:
        await update.message.reply_text("There's nothing to reopen right now.")
        return
    if not await is_admin(ctx, g, update.effective_user.id):
        await update.message.reply_text("Only the host or an admin can reopen registration.")
        return
    cancel_jobs(ctx, f"tcap:{g.chat_id}")
    cancel_jobs(ctx, f"tover:{g.chat_id}")
    cancel_jobs(ctx, f"ttoss:{g.chat_id}")
    g.captains = {}
    await open_team_lobby(ctx, g)


async def teams_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    g = tgames.get(update.effective_chat.id)
    if not g or g.state in ("host", "mode"):
        await update.message.reply_text("No team match is set up here yet.")
        return
    await say_long(ctx, g.chat_id, f"<b>『 👥 TEAMS 』</b>\n{LINE}\n{teams_body(g)}")


async def setover_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.message
    g = tgames.get(chat.id)
    if not g or g.state in ("host", "mode"):
        await msg.reply_text("No team match is being set up here.")
        return
    if g.state in PLAY_STATES or g.state in TOSS_STATES:
        await msg.reply_text("The match has already started.")
        return
    if not await is_admin(ctx, g, user.id):
        await msg.reply_text("Only the host can set the overs.")
        return
    if not ctx.args or not ctx.args[0].isdigit() or not 1 <= int(ctx.args[0]) <= MAX_OVERS:
        await msg.reply_text(f"Usage: /setover <1-{MAX_OVERS}>")
        return
    g.overs = int(ctx.args[0])
    if g.state == "overs":
        await begin_toss(ctx, g)
    else:
        await msg.reply_text(f"✅ Overs set to {g.overs}. The match starts once both captains are chosen.")


async def team_forcestart(update: Update, ctx: ContextTypes.DEFAULT_TYPE, g: TeamGame):
    if g.state != "lobby":
        return
    if not await is_admin(ctx, g, update.effective_user.id):
        await update.message.reply_text("Only admins can force start.")
        return
    if min(len(g.teams["A"]), len(g.teams["B"])) < MIN_TEAM:
        await update.message.reply_text(f"Need at least {MIN_TEAM} players on each side.")
        return
    await lock_teams(ctx, g)


async def lineups_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/batting or /bowling with no number: show both full teams."""
    g = tgames.get(update.effective_chat.id)
    if not g or g.state not in PLAY_STATES:
        await update.message.reply_text("No team match is in progress.")
        return
    await say_long(ctx, g.chat_id, lineups_text(g))


async def pick_number(update, ctx, g: TeamGame, pool: list):
    """Validate '/cmd <number>' against a team list; returns the uid or None."""
    msg = update.message
    if not ctx.args[0].isdigit() or not 1 <= int(ctx.args[0]) <= len(pool):
        await msg.reply_text(f"Pick a number between 1 and {len(pool)}.")
        return None
    return pool[int(ctx.args[0]) - 1]


async def batting_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/batting [number] - batting captain picks striker, non-striker, new batters."""
    g = tgames.get(update.effective_chat.id)
    if not g or g.state not in PLAY_STATES:
        await update.message.reply_text("No team match is in progress.")
        return
    if not ctx.args:
        await lineups_cmd(update, ctx)
        return
    if g.state != "pick_bat":
        await update.message.reply_text("I'm not asking for a batter right now.")
        return
    cap = g.captains[g.bat]
    if update.effective_user.id not in (cap, g.host_id):
        await update.message.reply_text(
            f"Only the batting captain ({g.players[cap].name}) or the host can pick the batter.")
        return
    uid = await pick_number(update, ctx, g, g.teams[g.bat])
    if uid is None:
        return
    if uid in g.out:
        await update.message.reply_text("That batter is already out. Pick someone else.")
        return
    if uid in (g.striker, g.non_striker):
        await update.message.reply_text("That batter is already at the crease.")
        return
    await assign_batter(ctx, g, uid)


async def bowling_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/bowling [number] - bowling captain picks the bowler for the over."""
    g = tgames.get(update.effective_chat.id)
    if not g or g.state not in PLAY_STATES:
        await update.message.reply_text("No team match is in progress.")
        return
    if not ctx.args:
        await lineups_cmd(update, ctx)
        return
    if g.state != "pick_bowl":
        await update.message.reply_text("I'm not asking for a bowler right now.")
        return
    cap = g.captains[other(g.bat)]
    if update.effective_user.id not in (cap, g.host_id):
        await update.message.reply_text(
            f"Only the bowling captain ({g.players[cap].name}) or the host can pick the bowler.")
        return
    uid = await pick_number(update, ctx, g, g.teams[other(g.bat)])
    if uid is None:
        return
    if uid == g.last_bowler:
        await update.message.reply_text("That player bowled the last over. Pick someone else.")
        return
    ok = await dm_ready(ctx, uid, f"🎯 You've been picked to bowl over {g.balls // 6 + 1}. "
                                  "I'll ask for each delivery here.")
    if not ok:
        await update.message.reply_text(
            f"{g.players[uid].name} hasn't started me in DM, so can't bowl. "
            "Ask them to tap Start, then pick again 👇", reply_markup=start_dm_kb(ctx))
        return
    g.players[uid].dm_ok = True
    await assign_bowler(ctx, g, uid)


# ---------- toss ----------
async def begin_toss(ctx, g: TeamGame):
    cancel_jobs(ctx, f"tover:{g.chat_id}")
    g.state, g.toss_caller = "toss_call", random.choice("AB")
    cap = g.players[g.captains[g.toss_caller]]
    await say(ctx, g.chat_id,
              f"<b>『 🪙 TOSS TIME 』</b>\n{LINE}\n{nm(cap)} ({tname(g.toss_caller)} captain) "
              f"calls the toss. Heads or Tails? 👇\n⏳ Auto-called in {CAPTAIN_SECONDS}s.",
              Markup([[Btn("🪙 Heads", callback_data="t:toss:H"),
                       Btn("🪙 Tails", callback_data="t:toss:T")]]))
    ctx.job_queue.run_once(t_toss_timeout, CAPTAIN_SECONDS, data=(g.chat_id, "toss_call"),
                           name=f"ttoss:{g.chat_id}")


async def toss_flip(ctx, g: TeamGame, call: str):
    cancel_jobs(ctx, f"ttoss:{g.chat_id}")
    g.state = "toss_choice"
    coin = random.choice("HT")
    names = {"H": "Heads", "T": "Tails"}
    g.toss_winner = g.toss_caller if coin == call else other(g.toss_caller)
    cap = g.players[g.captains[g.toss_winner]]
    await say(ctx, g.chat_id,
              f"🪙 {tname(g.toss_caller)} called <b>{names[call]}</b> — it's <b>{names[coin]}</b>!\n"
              f"🏆 {tname(g.toss_winner)} wins the toss.\n{LINE}\n"
              f"{nm(cap)}, will you bat or bowl first? 👇\n⏳ Auto-bat in {CAPTAIN_SECONDS}s.",
              Markup([[Btn("🏏 Bat", callback_data="t:opt:bat"),
                       Btn("⚾ Bowl", callback_data="t:opt:bowl")]]))
    ctx.job_queue.run_once(t_toss_timeout, CAPTAIN_SECONDS, data=(g.chat_id, "toss_choice"),
                           name=f"ttoss:{g.chat_id}")


async def toss_decide(ctx, g: TeamGame, choice: str):
    cancel_jobs(ctx, f"ttoss:{g.chat_id}")
    g.state = "busy"
    g.bat = g.toss_winner if choice == "bat" else other(g.toss_winner)
    await begin_match(ctx, g)


async def t_toss_timeout(ctx):
    chat_id, st = ctx.job.data
    g = tgames.get(chat_id)
    if not g or g.state != st:
        return
    if st == "toss_call":
        call = random.choice("HT")
        await say(ctx, chat_id, f"⏱ {nm(g.players[g.captains[g.toss_caller]])} didn't call in time "
                                f"— auto-called {'Heads' if call == 'H' else 'Tails'}.")
        await toss_flip(ctx, g, call)
    else:
        await say(ctx, chat_id, f"⏱ {nm(g.players[g.captains[g.toss_winner]])} didn't choose "
                                "in time — batting first.")
        await toss_decide(ctx, g, "bat")


# ---------- match flow ----------
async def begin_match(ctx, g: TeamGame):
    cancel_jobs(ctx, f"tover:{g.chat_id}")
    g.innings = 1  # g.bat was decided at the toss
    reset_innings(g)
    mode = "🛡 Spam-Free" if g.spam_free else "⚔️ Normal"
    await say_media(ctx, g.chat_id, "team_arena",
                    f"<b>『 🏟 TEAM ARENA: LIVE 』</b>\n{LINE}\n{teams_body(g)}\n{LINE}\n"
                    f"🪙 Toss: {tname(g.toss_winner)} won and chose to "
                    f"{'bat' if g.bat == g.toss_winner else 'bowl'}\n"
                    f"🎯 <b>{g.overs}</b> over{'s' if g.overs != 1 else ''} a side | {mode}")
    await prompt_bat(ctx, g)


def apply_penalty(g: TeamGame, team: str):
    """Deduct TIMEOUT_PENALTY runs from `team`, wherever its score currently lives."""
    if team == g.bat:                      # currently batting
        g.score -= TIMEOUT_PENALTY
    elif g.innings == 1:                   # bowling now, bats next innings
        g.carry -= TIMEOUT_PENALTY
    else:                                  # already batted: lower its total, ease the chase
        g.first["score"] -= TIMEOUT_PENALTY
        g.target -= TIMEOUT_PENALTY


def t_schedule(ctx, g: TeamGame):
    """Start the countdown for whoever the match is waiting on.
    Captains picking a batter/bowler get 5 min; the batter/bowler get 60 s."""
    g.seq += 1
    name = f"tmove:{g.chat_id}"
    cancel_jobs(ctx, name)
    if g.state in ("pick_bat", "pick_bowl"):
        total, warns = PICK_TIMEOUT, PICK_WARN_AT
    else:
        total, warns = MOVE_TIMEOUT, WARN_AT
    for left in warns + [0]:
        ctx.job_queue.run_once(t_tick, total - left,
                               data=(g.chat_id, g.seq, left), name=name)


def waiting_on(g: TeamGame):
    if g.state == "pick_bat":
        return g.captains[g.bat], "pick the batter"
    if g.state == "pick_bowl":
        return g.captains[other(g.bat)], "pick the bowler"
    if g.state == "await_bowl":
        return g.bowler, "bowl"
    return g.striker, "play your shot"


async def t_tick(ctx):
    chat_id, seq, left = ctx.job.data
    g = tgames.get(chat_id)
    if not g or g.seq != seq or g.state not in WAIT_STATES:
        return
    if left:
        uid, verb = waiting_on(g)
        left_txt = f"{left // 60} min" if left >= 60 else f"{left} sec"
        text = f"⏰ {nm(g.players[uid])}, only <b>{left_txt}</b> left to {verb}!"
        await say(ctx, chat_id, text)
        if g.state == "await_bowl":
            await dm_ready(ctx, uid, text)
        return
    await t_timeout(ctx, g)


async def t_timeout(ctx, g: TeamGame):
    st = g.state
    uid, _ = waiting_on(g)
    g.state = "busy"
    cancel_jobs(ctx, f"tmove:{g.chat_id}")
    who = nm(g.players[uid])
    if st == "pick_bat":
        pick = random.choice(eligible_batters(g))
        await say(ctx, g.chat_id, f"⏱ {who} didn't pick in time — "
                                  f"{nm(g.players[pick])} was auto-selected.")
        await assign_batter(ctx, g, pick)
    elif st == "pick_bowl":
        pool = eligible_bowlers(g)
        random.shuffle(pool)
        pick = None
        for cand in pool:  # prefer someone I can actually DM
            if await dm_ready(ctx, cand, "🎯 You were auto-picked to bowl this over."):
                pick = cand
                break
        pick = pick or pool[0]
        await say(ctx, g.chat_id, f"⏱ {who} didn't pick in time — "
                                  f"{nm(g.players[pick])} was auto-selected.")
        await assign_bowler(ctx, g, pick)
    elif st == "await_bowl":
        g.bowl_value = random.randint(1, 6)
        apply_penalty(g, other(g.bat))
        await say(ctx, g.chat_id, f"⏱ {who} didn't deliver in time — a random ball was bowled.\n"
                                  f"🚨 {tname(other(g.bat))} get a <b>-{TIMEOUT_PENALTY}</b> run penalty!")
        await call_tbat(ctx, g)
    else:
        apply_penalty(g, g.bat)
        await say(ctx, g.chat_id, f"⛔ {who} timed out and is <b>OUT</b>!\n"
                                  f"🚨 {tname(g.bat)} get a <b>-{TIMEOUT_PENALTY}</b> run penalty!")
        await t_resolve(ctx, g, 0, timed_out=True)


async def prompt_bat(ctx, g: TeamGame):
    g.state = "pick_bat"
    opening = g.balls == 0 and not g.out
    need = ("STRIKER" if g.striker is None else "NON-STRIKER") if opening else "NEW BATTER"
    cap = g.players[g.captains[g.bat]]
    intro = ""
    if opening and g.striker is None:
        intro = (f"🔔 <b>INNINGS {g.innings}</b> · {tname(g.bat)} bat"
                 f"{chase_line(g)}\n\n")
    lst = ""
    if not g.bat_list_shown:  # full list only once per innings
        g.bat_list_shown = True
        lst = f"\n\n{roster(g, g.bat)}"
    else:
        lst = "\n<i>(Send /batting to view the list.)</i>"
    await say(ctx, g.chat_id,
              f"{intro}<b>『 🏏 BATTING CALL · {need} 』</b>\n{LINE}\n"
              f"{nm(cap)}, choose the {need.lower()}:\n"
              f"<code>/batting &lt;number&gt;</code>{lst}")
    t_schedule(ctx, g)


async def assign_batter(ctx, g: TeamGame, uid: int):
    cancel_jobs(ctx, f"tmove:{g.chat_id}")
    opening = g.balls == 0 and not g.out
    if g.striker is None:
        g.striker, role = uid, ("STRIKER" if opening else "NEW BATTER")
    else:
        g.non_striker, role = uid, ("NON-STRIKER" if opening else "NEW BATTER")
    g.players[uid].batted = True
    text = f"🟢 <b>{role} IN!</b>\n\n{nm(g.players[uid])} takes guard at the crease."
    if role == "NEW BATTER":
        await say_media(ctx, g.chat_id, "new_batter", text)
    else:
        await say(ctx, g.chat_id, text)
    await advance(ctx, g)


async def advance(ctx, g: TeamGame):
    """Work out what the match needs next: batters, then a bowler, then a ball."""
    if g.striker is None or g.non_striker is None:
        await prompt_bat(ctx, g)
    elif g.bowler is None:
        await prompt_bowl(ctx, g)
    else:
        await call_tbowl(ctx, g)


async def prompt_bowl(ctx, g: TeamGame):
    g.state = "pick_bowl"
    wt = other(g.bat)
    cap = g.players[g.captains[wt]]
    if not g.bowl_list_shown:  # full list only once per innings
        g.bowl_list_shown = True
        lst = f"\n\n{roster(g, wt)}"
    else:
        lst = "\n<i>(Send /bowling to view the list.)</i>"
    await say(ctx, g.chat_id,
              f"<b>『 ⚾ BOWLER CALL · OVER {g.balls // 6 + 1}/{g.overs} 』</b>\n{LINE}\n"
              f"{nm(cap)}, choose the bowler:\n<code>/bowling &lt;number&gt;</code>\n"
              f"<i>(The last over's bowler can't bowl again.)</i>{lst}")
    t_schedule(ctx, g)


async def assign_bowler(ctx, g: TeamGame, uid: int):
    cancel_jobs(ctx, f"tmove:{g.chat_id}")
    g.bowler = uid
    await say(ctx, g.chat_id, f"⚾ {nm(g.players[uid])} will bowl "
                              f"<b>over {g.balls // 6 + 1}/{g.overs}</b>.")
    await advance(ctx, g)


async def call_tbowl(ctx, g: TeamGame):
    g.state, g.bowl_value = "await_bowl", None
    bowler, bat = g.players[g.bowler], g.players[g.striker]
    ball = g.balls % 6 + 1
    await say_media(ctx, g.chat_id, "bowling_call",
                    f"<b>『 ⚾ BOWLING CALL 』</b>\n{LINE}\n{nm(bowler)} · Over "
                    f"{g.balls // 6 + 1}/{g.overs} · Ball {ball}/6\nSend your delivery in DM",
                    Markup([[Btn("⚾ Deliver Bowl", url=f"https://t.me/{ctx.bot.username}")]]))
    where = f"🏟 <b>{html.escape(g.chat_title)}</b>\n" if g.chat_title else ""
    await dm_ready(ctx, bowler.uid,
                   f"📊 <b>LIVE MATCH</b>\n{where}\n"
                   f"🏏 <b>Batter:</b> {nm(bat)} ({bat.runs} off {bat.balls})\n"
                   f"📈 <b>Score:</b> {score_line(g)}{chase_line(g)}\n"
                   f"🥎 <b>Ball:</b> {ball}/6 of over {g.balls // 6 + 1}\n"
                   f"🔻 <b>Your turn:</b> type a number (1–6) here to bowl.")
    t_schedule(ctx, g)


async def team_dm(update: Update, ctx: ContextTypes.DEFAULT_TYPE, g: TeamGame):
    uid = update.effective_user.id
    if g.state != "await_bowl" or g.bowler != uid:
        await update.message.reply_text("It's not your turn to bowl right now.")
        return
    g.bowl_value = int(update.message.text)
    cancel_jobs(ctx, f"tmove:{g.chat_id}")
    await update.message.reply_text(f"✅ Delivery locked: {g.bowl_value}")
    await call_tbat(ctx, g)


async def call_tbat(ctx, g: TeamGame):
    g.state = "await_bat"
    await say_media(ctx, g.chat_id, "face_ball",
                    f"<b>『 🏏 FACE THE BALL 』</b>\n{LINE}\n{nm(g.players[g.striker])} "
                    f"<b>{g.players[g.striker].runs}[{g.players[g.striker].balls}]</b> · Over "
                    f"{g.balls // 6 + 1}/{g.overs} · Ball {g.balls % 6 + 1}/6"
                    f"{chase_line(g)}\nSend your shot (0–6) here")
    t_schedule(ctx, g)


async def team_shot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    g = tgames.get(update.effective_chat.id)
    if not g or g.state not in ("await_bowl", "await_bat"):
        return
    if g.state == "await_bat" and update.effective_user.id == g.striker:
        await t_resolve(ctx, g, int(update.message.text))
    elif g.spam_free:  # keep the chat clean
        try:
            await update.message.delete()
        except Exception:
            pass


async def t_resolve(ctx, g: TeamGame, shot: int, timed_out: bool = False):
    g.state = "busy"
    cancel_jobs(ctx, f"tmove:{g.chat_id}")
    bat, bowl = g.players[g.striker], g.players[g.bowler]
    bat.balls += 1
    bowl.balls_bowled += 1
    g.balls += 1

    if timed_out or shot == g.bowl_value:
        g.wkts += 1
        g.out.add(bat.uid)
        g.striker = None
        bat.ducks += bat.runs == 0
        if not timed_out:
            bowl.wickets += 1
            bowl.wkt_streak += 1
            kind = "DUCK" if bat.runs == 0 else "BOWLED"
            line = f"<b>{kind}</b> · {nm(bowl)} STRIKES!"
        else:
            kind, line = "TIMED OUT", "<b>TIMED OUT</b> · walked off the field!"
        await say_media(ctx, g.chat_id, "duck" if bat.runs == 0 else "wicket",
                        f"⚠️ <b>WICKET!</b>\n\n<blockquote>{line}</blockquote>\n"
                        f"❌ {nm(bat)} dismissed for <b>{bat.runs}</b> ({bat.balls}b)\n"
                        f"└ {score_line(g)}")
        if bowl.wkt_streak == 3:
            bowl.hattricks += 1
            await say_media(ctx, g.chat_id, "hattrick",
                            f"🎩 <b>HAT-TRICK!</b> {nm(bowl)} takes three in three balls!")
    else:
        before = bat.runs
        bowl.wkt_streak = 0
        bat.runs += shot
        bowl.runs_conceded += shot
        g.score += shot
        bat.fours += shot == 4
        bat.sixes += shot == 6
        bat.fifties += before < 50 <= bat.runs
        bat.hundreds += before < 100 <= bat.runs
        await say_media(ctx, g.chat_id, f"run_{shot}",
                        f"🚀 <b>{shot} RUNS</b> · {nm(bat)} {bat.runs} ({bat.balls}b)\n"
                        f"<blockquote>{COMMENT[shot]}</blockquote>📊 {score_line(g)}")
        if before < 100 <= bat.runs:
            await say_media(ctx, g.chat_id, "century",
                            f"💯 <b>CENTURY!</b> {nm(bat)} reaches 100 off {bat.balls} balls!")
        elif before < 50 <= bat.runs:
            await say_media(ctx, g.chat_id, "fifty",
                            f"🔥 <b>HALF-CENTURY!</b> {nm(bat)} reaches 50 off {bat.balls} balls!")
        if shot % 2:  # odd runs: batters cross
            g.striker, g.non_striker = g.non_striker, g.striker

    over_done = g.balls % 6 == 0
    if over_done:  # ends change, and the bowler is rested
        g.striker, g.non_striker = g.non_striker, g.striker
        g.last_bowler, g.bowler = g.bowler, None
    if innings_over(g):
        await end_innings(ctx, g)
        return
    if over_done:
        await say(ctx, g.chat_id, f"🔔 <b>END OF OVER {g.balls // 6}</b>\n"
                                  f"{score_line(g)}{chase_line(g)}")
    await advance(ctx, g)


async def end_innings(ctx, g: TeamGame):
    cancel_jobs(ctx, f"tmove:{g.chat_id}")
    if g.innings == 2:
        await t_finish(ctx, g)
        return
    first = g.bat
    g.first = {"team": first, "score": g.score, "wkts": g.wkts, "balls": g.balls}
    g.target = g.score + 1
    g.innings, g.bat = 2, other(first)
    await say(ctx, g.chat_id,
              f"<b>『 🏁 INNINGS BREAK 』</b>\n{LINE}\n"
              f"{tname(first)}: <b>{g.score}/{g.wkts}</b> ({g.balls // 6}.{g.balls % 6} ov)\n\n"
              f"🎯 {tname(g.bat)} need <b>{g.target}</b> runs in {g.overs * 6} balls "
              f"({g.overs} over{'s' if g.overs != 1 else ''})")
    carry, g.carry = g.carry, 0
    reset_innings(g)
    g.score = carry  # penalties earned while bowling count against this innings
    await prompt_bat(ctx, g)


def card_line(p: Player) -> str:
    bat = f"<b>{p.runs}</b> ({p.balls})" if p.batted else "DNB"
    bowl = (f" · 🎯 {p.wickets}/{p.runs_conceded} ({p.balls_bowled // 6}.{p.balls_bowled % 6})"
            if p.balls_bowled else "")
    return f"• {nm(p)} — {bat}{bowl}"


async def t_finish(ctx, g: TeamGame):
    f = g.first
    inn = {f["team"]: f, g.bat: {"score": g.score, "wkts": g.wkts, "balls": g.balls}}
    if g.score > f["score"]:
        winner = g.bat
        wk = len(g.teams[winner]) - 1 - g.wkts
        result = (f"🏆 {tname(winner)} won by <b>{wk}</b> wicket{'s' if wk != 1 else ''} "
                  f"({g.overs * 6 - g.balls} balls left)")
    elif g.score < f["score"]:
        winner = f["team"]
        result = f"🏆 {tname(winner)} won by <b>{f['score'] - g.score}</b> runs"
    else:
        winner, result = None, "🤝 <b>MATCH TIED!</b>"
    winners = set(g.teams[winner]) if winner else set()

    ps = list(g.players.values())
    pool = [p for p in ps if p.uid in winners] or ps
    potm = max(pool, key=lambda p: p.runs + 5 * p.wickets)
    orange = max(ps, key=lambda p: p.runs)
    orange = orange if orange.runs else None
    purple = max(ps, key=lambda p: (p.wickets, -p.runs_conceded))
    purple = purple if purple.wickets else None
    reward_txt = save_match(ps, orange, purple, potm, winners)

    sections = []
    for t in "AB":
        i = inn[t]
        sections.append(f"{ICON[t]} <b>TEAM {t}</b> · {i['score']}/{i['wkts']} "
                        f"({i['balls'] // 6}.{i['balls'] % 6} ov)\n"
                        + "\n".join(card_line(g.players[u]) for u in g.teams[t]))
    card = (f"<b>『 🏁 TEAM MATCH · FINAL SCORECARD 』</b>\n{LINE}\n{result}\n{LINE}\n"
            + "\n\n".join(sections) + f"\n{LINE}\n"
            f"🟠 <b>ORANGE CAP</b>: {nm(orange) + f' · {orange.runs} runs' if orange else '—'}\n"
            f"🟣 <b>PURPLE CAP</b>: {nm(purple) + f' · {purple.wickets} wickets' if purple else '—'}\n"
            f"🏆 <b>PLAYER OF THE MATCH</b>: {nm(potm)}")
    rewards = f"🎁 <b>REWARDS</b>\n{reward_txt}\n\n🏆 <b>MATCH COMPLETED</b>"
    cleanup_team(ctx, g)
    if MEDIA.get("scorecard"):
        await say_media(ctx, g.chat_id, "scorecard", "📊 <b>FINAL SCORECARD</b>")
    if MEDIA.get("potm"):
        await say_media(ctx, g.chat_id, "potm", f"🏆 <b>PLAYER OF THE MATCH</b>\n{nm(potm)}")
    await say_long(ctx, g.chat_id, card)
    await say_long(ctx, g.chat_id, rewards,
                   Markup([[Btn("🔄 Play Again", callback_data="t:again"),
                            Btn("🎮 Other Modes", callback_data="other")]]))


# =====================================================================
#                      1v1 DUEL  (/1v1)
# =====================================================================
# Someone sends /1v1, a challenge card appears with an "Accept Challenge" button. The first person
# to tap it plays: a coin toss decides who bats first, each batter bats until out (the other player
# bowls by DM, exactly like solo mode) and the higher score wins. It runs on the solo engine.
DUEL_SECONDS = 120


def duel_text(g: Game) -> str:
    return (f"<b>『 ⚔️ 1V1 CHALLENGE 』</b>\n{LINE}\n"
            f"👤 Challenger : {nm(g.players[g.host_id])}\n"
            f"🏏 Format - Bat until you get out\n\n"
            f"⏳ Challenge auto closes in <b>{DUEL_SECONDS} seconds</b>.\n"
            f"Tap the button below to accept the challenge 👇")


def _start_dm_kb(ctx):
    return Markup([[Btn("▶️ Start bot in DM", url=f"https://t.me/{ctx.bot.username}?start=register")]])


async def duel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/1v1 - open a challenge anyone in the group can accept."""
    chat, user, msg = update.effective_chat, update.effective_user, update.message
    if chat.id in games or chat.id in tgames or in_auction(update):
        await msg.reply_text("⚠️ A match is already running in this group. Finish it first, "
                             "or ask an admin to use /endgame.")
        return
    if user_game.get(user.id) is not None:
        await msg.reply_text("You're already in a match in another group.")
        return
    try:  # the bowler plays by DM, so the bot must be able to message the challenger
        await ctx.bot.send_message(user.id, "⚔️ Your 1v1 challenge is live! I'll DM you here when it's "
                                            "your turn to bowl.")
    except Forbidden:
        await msg.reply_text("Start me in DM first (so I can message you when you bowl), "
                             "then send /1v1 again 👇", reply_markup=_start_dm_kb(ctx))
        return
    g = Game(chat.id, user.id, user.full_name, spell=10 ** 9, spam_free=True,
             chat_title=chat.title or "")
    g.duel, g.state = True, "lobby"
    g.players[user.id] = Player(user.id, user.full_name)
    user_game[user.id] = chat.id
    games[chat.id] = g
    g.duel_msg = await say_media(ctx, chat.id, "duel", duel_text(g),
                                 Markup([[Btn("Accept Challenge 🏆", callback_data="duel:accept")]]))
    ctx.job_queue.run_once(duel_expire, DUEL_SECONDS, data=chat.id, name=f"duel:{chat.id}")


async def duel_expire(ctx):
    g = games.get(ctx.job.data)
    if not g or not g.duel or g.state != "lobby":
        return
    msg = g.duel_msg
    cleanup(ctx, g)
    text = (f"⌛ <b>1v1 challenge closed.</b>\nNobody accepted {link(g.host_id, g.host_name)}'s challenge "
            f"in {DUEL_SECONDS} seconds.")
    try:
        await edit_any(msg, text)
    except Exception:
        await say(ctx, g.chat_id, text)


async def duel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id, user = q.message.chat_id, q.from_user
    g = games.get(chat_id)
    if not g or not g.duel or g.state != "lobby":
        await q.answer("This challenge is no longer open.", show_alert=True)
        return
    if user.id == g.host_id:
        await q.answer("You can't accept your own challenge 😄", show_alert=True)
        return
    if user_game.get(user.id, chat_id) != chat_id:
        await q.answer("You're already in a match in another group.", show_alert=True)
        return
    try:
        await ctx.bot.send_message(user.id, "✅ Challenge accepted! I'll DM you here when it's your turn to bowl.")
    except Forbidden:
        await q.answer("Open the bot in DM and press Start first, then tap Accept again.", show_alert=True)
        await say(ctx, chat_id, f"⚠️ {link(user.id, user.full_name)}, start me in DM first, "
                                "then tap <b>Accept Challenge</b> again 👇", _start_dm_kb(ctx))
        return
    g.state = "starting"  # nobody else can take the challenge now
    cancel_jobs(ctx, f"duel:{chat_id}")
    g.players[user.id] = Player(user.id, user.full_name)
    user_game[user.id] = chat_id
    await q.answer()
    await edit_any(q.message, f"<b>『 ⚔️ 1V1 CHALLENGE 』</b>\n{LINE}\n"
                              f"{link(g.host_id, g.host_name)}  🆚  {link(user.id, user.full_name)}\n"
                              "✅ Accepted")
    await say(ctx, chat_id, f"🍃 {link(user.id, user.full_name)} has accepted the challenge!")
    await start_duel(ctx, g)


async def start_duel(ctx, g: Game):
    g.order = list(g.players)
    random.shuffle(g.order)  # coin toss: the first name wins it and bats first
    g.bat_idx, g.bowl_ptr = 0, 0
    pick_bowler(g)           # the other player bowls
    first, second = g.players[g.order[0]], g.players[g.order[1]]
    toss = await say(ctx, g.chat_id, "🪙 <b>TOSS TIME!</b> Flipping the coin...")
    await asyncio.sleep(2)
    await edit_any(toss, f"🪙 <b>TOSS</b>\n{LINE}\n{nm(first)} won the toss and bats first!\n"
                         f"🎯 {nm(second)} will bowl.")
    await asyncio.sleep(1)
    await say(ctx, g.chat_id,
              f"<b>『 ⚔️ 1V1 ARENA: LIVE 』</b>\n{LINE}\n"
              f"🏏 Batting: {nm(first)}\n🎯 Bowling: {nm(second)} (send your numbers in DM)\n\n"
              f"Format: bat until you get out. The winner is decided by runs "
              f"(then wickets if the runs are level).")
    await call_bowl(ctx, g)


async def finish_duel(ctx, g: Game, forfeit_uid=None):
    cancel_jobs(ctx, f"move:{g.chat_id}")
    p1, p2 = list(g.players.values())
    if forfeit_uid is not None:
        winner = p2 if forfeit_uid == p1.uid else p1
        result = f"🏆 {nm(winner)} wins! Their opponent timed out."
    elif (p1.runs, p1.wickets) != (p2.runs, p2.wickets):  # runs decide, then wickets
        winner = p1 if (p1.runs, p1.wickets) > (p2.runs, p2.wickets) else p2
        loser = p2 if winner is p1 else p1
        by = (f"by <b>{winner.runs - loser.runs}</b> runs" if winner.runs != loser.runs
              else f"on wickets ({winner.wickets} vs {loser.wickets})")
        result = f"🏆 {nm(winner)} wins {by}!"
    else:
        winner, result = None, "🤝 <b>IT'S A TIE!</b>"
    reward_txt = save_match([p1, p2], None, None, None, {winner.uid} if winner else set())
    rows = []
    for p in (p1, p2):
        sr = p.runs / p.balls * 100 if p.balls else 0
        rows.append(f"🏏 {nm(p)} — <b>{p.runs}</b> runs ({p.balls}b) · 🎯 <b>{p.wickets}</b> "
                    f"wkt{'s' if p.wickets != 1 else ''}\n   └ 4s {p.fours} · 6s {p.sixes} · SR {sr:.1f}")
    text = (f"<b>『 🏁 1V1 · FINAL RESULT 』</b>\n{LINE}\n" + "\n\n".join(rows) + f"\n{LINE}\n{result}")
    cleanup(ctx, g)
    sent = False
    if winner:  # the winner's profile picture with the result as its caption
        try:
            photos = await ctx.bot.get_user_profile_photos(winner.uid, limit=1)
            if photos.total_count and photos.photos and len(text) <= 1000:
                await ctx.bot.send_photo(g.chat_id, photos.photos[0][-1].file_id,
                                         caption=text, parse_mode=HTML)
                sent = True
        except TelegramError:
            pass
    if not sent:
        await say(ctx, g.chat_id, text)
    await say(ctx, g.chat_id, f"🎁 <b>REWARDS</b>\n<blockquote>{reward_txt}</blockquote>\n"
                              "⚔️ Challenge someone again with /1v1")


# =====================================================================
#            LIVE SCOREBOARD  (/score = team mode, /solo_score = solo mode)
# =====================================================================
# The bot posts the scoreboard, pins it, and keeps editing the same message while the match
# is running. When the match ends it shows the final state and unpins it.
LIVE_REFRESH = 4                                  # seconds between refresh checks
SOLO_LIVE = ("await_bowl", "await_bat", "busy")   # solo states in which a match is being played
live_boards: dict = {}  # (mode, chat_id) -> {"g", "msg_id", "text", "pinned", "posted"}


def _pn(p: Player) -> str:
    """Plain bold name (no profile link, so a refreshing board never pings anyone)."""
    return f"<b>{html.escape(p.name)}</b>"


def _team_innings(g: TeamGame, t: str):
    """(score, wkts, balls) of team t so far, or None if it has not batted yet."""
    if g.innings == 1:
        return (g.score, g.wkts, g.balls) if t == g.bat else None
    if g.first and t == g.first["team"]:
        return (g.first["score"], g.first["wkts"], g.first["balls"])
    return (g.score, g.wkts, g.balls)


def _team_row(g: TeamGame, t: str, uid: int) -> str:
    p = g.players[uid]
    mark = ""
    if t == g.bat and g.state in PLAY_STATES:
        mark = "❌ " if uid in g.out else "🏏 " if uid == g.striker else "🏃 " if uid == g.non_striker else ""
    elif t != g.bat and uid == g.bowler:
        mark = "🎯 "
    bat = f"<b>{p.runs}</b> ({p.balls})" if p.batted else "DNB"
    bowl = (f" · 🎯 {p.wickets}/{p.runs_conceded} ({p.balls_bowled // 6}.{p.balls_bowled % 6})"
            if p.balls_bowled else "")
    return f"  {mark}{_pn(p)} — {bat}{bowl}"


def team_board(g: TeamGame, final: bool = False) -> str:
    out = [f"<b>『 📊 LIVE SCOREBOARD · TEAM MATCH 』</b>", LINE,
           f"🏏 Innings <b>{g.innings}</b> of 2 · {g.overs} over{'s' if g.overs != 1 else ''} a side"]
    for t in "AB":
        inn = _team_innings(g, t)
        head = (f"{ICON[t]} <b>TEAM {t}</b> · <b>{inn[0]}/{inn[1]}</b> ({inn[2] // 6}.{inn[2] % 6} ov)"
                if inn else f"{ICON[t]} <b>TEAM {t}</b> · yet to bat")
        if t == g.bat and not final:
            head += "  🏏 batting"
        out += ["", head] + [_team_row(g, t, u) for u in g.teams[t]]
    out.append(LINE)
    if g.innings == 2:
        out.append(f"🎯 Target <b>{g.target}</b>" + ("" if final else
                   f" · need <b>{max(g.target - g.score, 0)}</b> from <b>{max(g.overs * 6 - g.balls, 0)}</b> balls"))
    if not final:
        bat, bowl = g.players.get(g.striker), g.players.get(g.bowler)
        if bat:
            out.append(f"🏏 Striker: {_pn(bat)} · {bat.runs} ({bat.balls})")
        if bowl:
            out.append(f"⚾ Bowler: {_pn(bowl)} · {bowl.wickets}/{bowl.runs_conceded}")
    else:
        out.append("🏁 <b>MATCH OVER</b> · see the final scorecard below")
    return "\n".join(out)


def solo_board(g: Game, final: bool = False) -> str:
    ps = list(g.players.values())
    pos = {u: i for i, u in enumerate(g.order)}
    lines = []
    for n, p in enumerate(ps, 1):
        if p.uid in g.removed or p.uid not in pos:
            tag = " ⛔"
        elif pos[p.uid] < g.bat_idx:
            tag = " ❌"
        elif pos[p.uid] == g.bat_idx and not final:
            tag = " 🏏"
        else:
            tag = " ⏳"
        if not final and p.uid == g.bowler_id:
            tag += " 🎯"
        sr = p.runs / p.balls * 100 if p.balls else 0
        lines.append(f"{n:02d} · {_pn(p)}{tag} — <b>{p.runs}</b> ({p.balls})\n"
                     f"   ├ 4s {p.fours} · 6s {p.sixes} · SR {sr:.1f}\n"
                     f"   └ 🎯 {p.wickets}/{p.runs_conceded} ({p.balls_bowled // 6}.{p.balls_bowled % 6})")
    out = [f"<b>『 📊 LIVE SCOREBOARD · SOLO MATCH 』</b>",
           f"📊 <b>{sum(p.runs for p in ps)}</b> runs · <b>{sum(p.balls for p in ps)}</b> balls · "
           f"<b>{sum(p.wickets for p in ps)}</b> wickets", LINE]
    if not final and g.bat_idx < len(g.order) and g.order[g.bat_idx] in g.players:
        out.append(f"🏏 Batting: {_pn(g.players[g.order[g.bat_idx]])}")
        if g.bowler_id in g.players:
            out.append(f"⚾ Bowling: {_pn(g.players[g.bowler_id])}")
        out.append(LINE)
    out += ["\n\n".join(lines), LINE,
            "🏁 <b>MATCH OVER</b> · see the final scorecard below" if final
            else "🔄 <i>This board refreshes by itself.</i>"]
    return "\n".join(out)


async def _drop_board(ctx, rec):
    """Unpin (if we pinned it) - used when the match is over."""
    if rec.get("pinned"):
        try:
            await ctx.bot.unpin_chat_message(rec["g"].chat_id, rec["msg_id"])
        except Exception:
            pass


async def _refresh_board(ctx, key, force: bool = False):
    rec = live_boards.get(key)
    if not rec:
        return
    mode, chat_id = key
    g = rec["g"]
    live = (tgames if mode == "team" else games).get(chat_id) is g
    text = (team_board if mode == "team" else solo_board)(g, final=not live)
    if text != rec["text"] or force:
        try:
            await ctx.bot.edit_message_text(text, chat_id=chat_id, message_id=rec["msg_id"], parse_mode=HTML)
            rec["text"] = text
        except RetryAfter:
            return  # Telegram says slow down: try again on the next round
        except BadRequest as e:
            if "not modified" in str(e).lower():
                rec["text"] = text
            else:  # the board message was deleted: stop refreshing it
                live_boards.pop(key, None)
                return
        except TelegramError:
            pass
    if not live:
        await _drop_board(ctx, rec)
        live_boards.pop(key, None)


async def board_job(ctx):
    for key in list(live_boards):
        try:
            await _refresh_board(ctx, key)
        except Exception as e:  # never let one chat stop the others
            logging.getLogger("cricket").warning("scoreboard refresh failed: %s", e)
        await asyncio.sleep(0.2)


async def _post_board(ctx, mode: str, g):
    key = (mode, g.chat_id)
    rec = live_boards.get(key)
    if rec and rec["g"] is g and time.time() - rec["posted"] < 8:
        return await _refresh_board(ctx, key, force=True)  # repeated /score: just refresh the board
    if rec:  # move the board to the bottom of the chat: remove the old one
        await _drop_board(ctx, rec)
        try:
            await ctx.bot.delete_message(g.chat_id, rec["msg_id"])
        except Exception:
            pass
    text = (team_board if mode == "team" else solo_board)(g)
    msg = await ctx.bot.send_message(g.chat_id, text, parse_mode=HTML)
    pinned = False
    try:
        await ctx.bot.pin_chat_message(g.chat_id, msg.message_id, disable_notification=True)
        pinned = True
    except Exception:
        pass
    live_boards[key] = {"g": g, "msg_id": msg.message_id, "text": text, "pinned": pinned,
                        "posted": time.time()}
    if not pinned:
        await say(ctx, g.chat_id, "📌 I couldn't pin the scoreboard. Make me an admin with the "
                                  "<b>Pin Messages</b> right and use the command again. "
                                  "It still refreshes by itself until the match ends.")


async def score_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/score - live scoreboard of the TEAM match (pinned + refreshed automatically)."""
    chat_id = update.effective_chat.id
    g = tgames.get(chat_id)
    if g and g.state in PLAY_STATES:
        return await _post_board(ctx, "team", g)
    if games.get(chat_id) and games[chat_id].state in SOLO_LIVE:
        return await update.message.reply_text("🏏 This is a solo match. Use /solo_score for its scoreboard.")
    await update.message.reply_text("No team match is in progress.")


async def solo_score_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/solo_score - live scoreboard of the SOLO match (pinned + refreshed automatically)."""
    chat_id = update.effective_chat.id
    g = games.get(chat_id)
    if g and g.state in SOLO_LIVE:
        return await _post_board(ctx, "solo", g)
    if tgames.get(chat_id) and tgames[chat_id].state in PLAY_STATES:
        return await update.message.reply_text("👥 This is a team match. Use /score for its scoreboard.")
    await update.message.reply_text("No solo match is in progress.")


# ---- commands both modes use: send them to the right mode ----
def in_auction(update: Update) -> bool:
    chat = update.effective_chat
    return bool(auction_mode and chat and auction_mode.active_auction(chat.id))


def routed(cricket_fn, auction_fn):
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if in_auction(update):
            return await auction_fn(update, ctx)
        return await cricket_fn(update, ctx)
    return handler


def main():
    app = Application.builder().token(TOKEN).build()
    app.job_queue.run_repeating(backup_job, interval=180, first=45)  # saves stats if anything changed
    app.job_queue.run_repeating(board_job, interval=LIVE_REFRESH, first=LIVE_REFRESH)  # live scoreboards
    grp = filters.ChatType.GROUPS
    app.add_handler(TypeHandler(Update, track), group=-1)
    if auction_mode:  # registered first so its DM registration conversation wins while active
        auction_mode.register(app, OWNER_IDS, in_auction,
                              lambda key: random.choice(MEDIA.get("auc_" + key) or [None]))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setplayzone", setplayzone))
    app.add_handler(CommandHandler("setchannel", setchannel))
    app.add_handler(CommandHandler("join", routed(join_cmd, auction_mode.join) if auction_mode else join_cmd,
                                   filters=grp))
    app.add_handler(CommandHandler("leavesolo", leavesolo, filters=grp))
    app.add_handler(CommandHandler("1v1", duel_cmd, filters=grp))
    app.add_handler(CallbackQueryHandler(duel_cb, pattern=r"^duel:accept$"))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(jn_cb, pattern=r"^jn:(yes|no):\d+$"))
    app.add_handler(CallbackQueryHandler(bc_cb, pattern=r"^bc:(yes|no)$"))
    app.add_handler(CommandHandler("joingame", joingame, filters=grp))
    app.add_handler(CommandHandler("forcestart", forcestart, filters=grp))
    app.add_handler(CommandHandler("endgame", endgame, filters=grp))
    app.add_handler(CallbackQueryHandler(endgame_cb, pattern=r"^eg:(yes|no):\d+$"))
    app.add_handler(CallbackQueryHandler(
        menu_cb, pattern=r"^(mode|spell|gm|cancel|back|other|mc|soon|auction)(:|$)"))
    app.add_handler(CallbackQueryHandler(again_cb, pattern=r"^again:"))
    app.add_handler(CallbackQueryHandler(noop_cb, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(team_cb, pattern=r"^t:"))
    app.add_handler(CommandHandler("add", add_cmd, filters=grp))
    app.add_handler(CommandHandler("rejointeams", rejointeams_cmd, filters=grp))
    app.add_handler(CommandHandler("teams", routed(teams_cmd, auction_mode.teams_cmd) if auction_mode else teams_cmd,
                                   filters=grp))
    app.add_handler(CommandHandler("changecap", changecap_cmd, filters=grp))
    app.add_handler(CommandHandler("changehost", changehost_cmd, filters=grp))
    app.add_handler(CommandHandler("votehost", votehost_cmd, filters=grp))
    app.add_handler(CommandHandler("setover", setover_cmd, filters=grp))
    app.add_handler(CommandHandler("score", score_cmd, filters=grp))
    app.add_handler(CommandHandler("solo_score", solo_score_cmd, filters=grp))
    app.add_handler(CommandHandler("batting", batting_cmd, filters=grp))
    app.add_handler(CommandHandler("bowling", bowling_cmd, filters=grp))
    app.add_handler(CommandHandler("addfile", addfile))
    app.add_handler(MessageHandler(filters.CaptionRegex(r"^/addfile"), addfile))
    app.add_handler(CommandHandler(["user_card", "usercard"], user_card))
    app.add_handler(CommandHandler("delfile", delfile))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("restore", restore_cmd))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Regex(r"^[1-6]$"), dm_delivery))
    app.add_handler(MessageHandler(grp & filters.Regex(r"^[0-6]$"), group_shot))
    app.run_polling()


if __name__ == "__main__":
    main()
