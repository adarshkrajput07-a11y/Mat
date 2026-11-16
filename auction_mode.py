"""ᴍᴀᴛʀɪx ᴀᴜᴄᴛɪᴏɴ ʙᴏᴛ
- Flow: Become Auctioneer -> tournament name -> number of teams -> /addcap [Username] [Team Name] one by one
  -> optional /addvc [Username] [Team Name] -> /addset (manual or import registered players)
  -> /startauction (asks purse, base price, max / min players if not set yet)
- Before every set the bot posts the full set, pins it and starts bidding SET_WAIT seconds (60) later
- Sets are added as "1. Name | Username | Userid" lists (/addset) or via DM registration
- Player cards carry the player's profile picture (common default picture via /addfile player)
- /end_auction posts the final squads and sends squad PDFs + the unsold list PDF
- Auctioneer controls Budget / Squad / Timer / Gap live (/settings)
- Fully automatic, timer-based bidding in the group
- Small-caps font, bold text, emojis, plus images / GIFs / video

- SUDO: only the owner (OWNER_ID) and sudo users can run auctions: /addsudo @user, /rmsudo @user, /sudo
- /start in a group = auction setup buttons; /start in the bot's DM = Player Registration button
  (host: /openreg asks tournament name, group id, channel id, then opens registration;
  players in DM: /register -> 1 join group+channel and verify, 2 name, 3 username; then posts to the log channel set with /setlog;
  everyone gets the default base price; sets are random)

Run:  python bot.py   (token is read from .env)
"""
import copy
import csv
import html
import io
import dataclasses
import json
import logging
import os
import random
import re
import string
import time
import types
from dataclasses import dataclass, field
from pathlib import Path

from telegram import BotCommand
from styled_buttons import Btn, auto_style
from telegram import InlineKeyboardMarkup as Markup
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, Defaults,
                          MessageHandler, TypeHandler, filters)

try:  # optional: live lot cards need Pillow
    from cards import lot_card
except Exception:  # pragma: no cover
    lot_card = None

try:  # optional: /end_auction PDFs need reportlab
    import reports
except Exception:  # pragma: no cover
    reports = None

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("auction")


def _load_env(path=".env"):
    """Tiny .env loader (no extra dependency)."""
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
BRAND = os.getenv("BRAND", "Matrix Auction Bot")
TOURNAMENT = os.getenv("TOURNAMENT", "Matrix Premier League")  # this bot runs ONE tournament
OWNER_IDS: set = set()  # filled by register(): the cricket bot's owners can control every auction
OWNER_ID = 0  # (kept for the messages below; set by register() to the first owner)
# These are only the DEFAULTS. The auctioneer changes them live, per group, with
# /setbudget /setsquad /settimer /setgap /setbase (see /settings) - no code edit needed.
BUDGET = int(os.getenv("BUDGET", 100))        # points per manager
SQUAD_SIZE = int(os.getenv("SQUAD_SIZE", 4))  # players each manager must buy
BID_TIMER = int(os.getenv("BID_TIMER", 20))   # seconds; resets on every bid
GAP = int(os.getenv("GAP", 5))                # pause between lots
DEFAULT_BASE = int(os.getenv("DEFAULT_BASE", 1))  # base price when a set line has none
SET_SIZE = int(os.getenv("SET_SIZE", 5))          # players per random set (registered / default pool)
SET_WAIT = int(os.getenv("SET_WAIT", 60))        # seconds between pinning a set and its first lot
MIN_BASE = 1                                  # cheapest possible player
BASE_OPTIONS_ALL = [int(x) for x in os.getenv("BASE_OPTIONS", "1,2,3,5,8,10").split(",")
                    if x.strip().isdigit()]


def base_options(a) -> list:
    return [b for b in BASE_OPTIONS_ALL if b <= a.budget] or [1]


ROLE_PLAIN = {"WK": "Wicketkeeper", "BAT": "Batter", "AR": "All-rounder", "BOWL": "Bowler"}
ROLE_EMOJI = {"WK": "🧤", "BAT": "🏏", "AR": "⚡", "BOWL": "🎯"}
ROLES = {k: f"{ROLE_EMOJI[k]} {v}" for k, v in ROLE_PLAIN.items()}
SET_TITLES = {"WK": "Wicketkeepers", "BAT": "Batters", "AR": "All-rounders", "BOWL": "Bowlers"}
ROLE_PLAIN["PL"], ROLE_EMOJI["PL"] = "Player", "🏏"  # generic role for "Name | Username | Userid" sets

DEFAULT_PLAYERS = [
    ("Virat Kohli", "BAT", 10), ("Rohit Sharma", "BAT", 10),
    ("Jasprit Bumrah", "BOWL", 10), ("Ravindra Jadeja", "AR", 8),
    ("Hardik Pandya", "AR", 8), ("Rishabh Pant", "WK", 8),
    ("Shubman Gill", "BAT", 6), ("Mohammed Siraj", "BOWL", 5),
    ("Kuldeep Yadav", "BOWL", 5), ("KL Rahul", "WK", 6),
    ("Suryakumar Yadav", "BAT", 6), ("Axar Patel", "AR", 4),
    ("Arshdeep Singh", "BOWL", 4), ("Ishan Kishan", "WK", 3),
    ("Washington Sundar", "AR", 2), ("Yuzvendra Chahal", "BOWL", 3),
]

# ======================================================================
#  STYLE: small-caps font, bold, protected names/commands
# ======================================================================
_SC = "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
assert len(_SC) == 26
SC_TABLE = {ord(c): s for c, s in zip(string.ascii_lowercase, _SC)}
SC_TABLE.update({ord(c.upper()): s for c, s in zip(string.ascii_lowercase, _SC)})
_TOKENS = re.compile(r"(<[^>]+>|&#?\w+;|\x01.*?\x02)", re.S)


DIV = "┈┈┈┈┈┈✦┈┈┈┈┈┈"  # card divider


def sc(text: str) -> str:
    """Plain small-caps (for buttons / alerts)."""
    return text.translate(SC_TABLE)


def nm(x) -> str:
    """Mark user-supplied text (names, notes): escaped and NOT small-capsed."""
    return "\x01" + html.escape(str(x)) + "\x02"


def cmd(name: str) -> str:
    """Mark a /command so it stays tappable (not small-capsed)."""
    return "\x01/" + name + "\x02"


def fancy(text: str) -> str:
    """Small-caps all letters, except HTML tags, entities, names and commands."""
    out = []
    for part in _TOKENS.split(text):
        if part.startswith("\x01"):
            out.append(part[1:-1])
        elif part.startswith("<") or part.startswith("&"):
            out.append(part)
        else:
            out.append(part.translate(SC_TABLE))
    return "".join(out)


def btn(label: str, **kw):
    return Btn(sc(label), style=auto_style(label, kw.get("callback_data")), **kw)


async def say(bot, chat_id, text, **kw):
    return await bot.send_message(chat_id, fancy(text), parse_mode=ParseMode.HTML, **kw)


async def rep(message, text, **kw):
    return await message.reply_text(fancy(text), parse_mode=ParseMode.HTML, **kw)


async def send_long(bot, chat_id, text, limit=3500):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit:
            await say(bot, chat_id, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await say(bot, chat_id, chunk)


# ======================================================================
#  MEDIA: photos / GIFs / video.  Drop your own files in ./media with the
#  same names (or set MEDIA_<NAME>=https://... in .env) to override.
# ======================================================================
MEDIA_DIR = Path(__file__).resolve().parent / "media"  # bundled banners / GIFs (read-only)
DATA = Path(os.environ.get("DATA_DIR", "."))  # persistent folder (same DATA_DIR as the cricket bot)
CUSTOM_DIR = DATA / "auction_media"          # files added with /addfile (auction chats)
MEDIA_CFG = DATA / "auction_media_config.json"  # remembers /addfile choices across restarts
MEDIA_EXTS = ("mp4", "gif", "png", "jpg", "jpeg", "webp")
FILE_IDS: dict[str, str] = {}  # cache so files upload to Telegram only once

# slot -> (label, where it shows up)
MEDIA_SLOTS = {
    "welcome": ("🖼 Welcome banner", "/help and /start"),
    "register": ("📝 Registration banner", "when registration opens + player DM"),
    "sets": ("📦 Sets banner", "when registration closes / sets are built"),
    "intro": ("🎬 Intro video", "the moment /startauction begins"),
    "live": ("🔴 New-set banner", "every time a new set starts"),
    "bid": ("💰 NEW BID GIF", "every time a bid is placed"),
    "sold": ("🔨 SOLD GIF", "when a player is sold"),
    "unsold": ("❌ UNSOLD GIF", "when a player gets no bids"),
    "trophy": ("🏆 Final GIF", "end of auction, before the final squads"),
    "player": ("🧢 Default player picture", "player card of anyone with no profile picture"),
}
SLOT_ALIAS = {"default": "player", "noprofile": "player", "profile": "player", "avatar": "player"}


import store as _storage  # MongoDB or json files, chosen by MONGO_URI in .env


def _db():
    return _storage.get_store()


def _load_cfg() -> dict:
    d = _db().load_json("auction_media_config", {})
    return d if isinstance(d, dict) else {}


MEDIA_DATA: dict = _load_cfg()  # {"<chat_id>" | "global": {slot: {"file", "kind"}}}


def _save_cfg():
    _db().save_json("auction_media_config", MEDIA_DATA)


def kind_of(src) -> str:
    ext = str(src).split("?")[0].rsplit(".", 1)[-1].lower()
    return {"mp4": "video", "gif": "animation"}.get(ext, "photo")


EXT_MEDIA = None  # set by the cricket bot: key -> "kind:file_id" (files added with /addfile auc_<key>)


def find_media(key: str, chat_id=None):
    """(source, kind) for `key`, or None. Order: this group's /addfile, owner's global
    /addfile, MEDIA_<KEY> in .env, then ./media/<key>.<ext>."""
    if EXT_MEDIA:  # /addfile aucsold, aucwelcome, ... (random one if several were added)
        ext = EXT_MEDIA(key)
        if ext:
            kind, fid = ext.split(":", 1)
            return fid, (kind if kind in ("video", "animation") else "photo")
    for scope in (str(chat_id), "global"):
        ent = MEDIA_DATA.get(scope, {}).get(key)
        if ent and (CUSTOM_DIR / ent["file"]).exists():
            return CUSTOM_DIR / ent["file"], ent["kind"]
    url = os.getenv(f"MEDIA_{key.upper()}")
    if url:
        return url, kind_of(url)
    for ext in MEDIA_EXTS:
        p = MEDIA_DIR / f"{key}.{ext}"
        if p.exists():
            return p, kind_of(p)
    return None


async def send_media(bot, chat_id, key, caption, reply_markup=None, source=None, media_chat=None,
                     strict=False):
    """Send the picture/GIF/video for `key` with a caption.
    Falls back to a plain text message (unless strict=True, then returns (None, False)).
    Returns (message, sent_as_media)."""
    found = (source, "photo") if source is not None else find_media(key, media_chat or chat_id)
    cap = fancy(caption)
    if found is not None:
        src, kind = found
        send = {"video": bot.send_video, "animation": bot.send_animation}.get(kind, bot.send_photo)
        cache_key = None if isinstance(src, io.BytesIO) else str(src)
        try:
            msg = await send(chat_id, FILE_IDS.get(cache_key, src), caption=cap,
                             parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            if cache_key and cache_key not in FILE_IDS:
                if msg.animation:
                    FILE_IDS[cache_key] = msg.animation.file_id
                elif msg.video:
                    FILE_IDS[cache_key] = msg.video.file_id
                elif msg.photo:
                    FILE_IDS[cache_key] = msg.photo[-1].file_id
            return msg, True
        except Exception as e:
            log.warning("media %s failed (%s), falling back to text", key, e)
    if strict:
        return None, False
    return await bot.send_message(chat_id, cap, parse_mode=ParseMode.HTML,
                                  reply_markup=reply_markup), False


# ======================================================================
#  DATA
# ======================================================================
def load_fallback_players():
    """Used only if nobody registered. players.json: [{"name","role","base"}]"""
    if os.path.exists("players.json"):
        with open("players.json", encoding="utf-8") as f:
            return json.load(f)
    return [{"name": n, "role": r, "base": b} for n, r, b in DEFAULT_PLAYERS]


def norm_role(p) -> str:
    return p.get("role") if p.get("role") in ROLE_PLAIN else "PL"


def pname(p) -> str:
    """Player name with @username (plain text, not small-capsed)."""
    return nm(p["name"] + (f" (@{p['username']})" if p.get("username") else ""))


def ment(p) -> str:
    """Clickable mention (pings the player) when we know their Telegram id."""
    if p.get("uid"):
        return ("\x01" + f'<a href="tg://user?id={int(p["uid"])}">{html.escape(p["name"])}</a>'
                + (html.escape(f" (@{p['username']})") if p.get("username") else "") + "\x02")
    return pname(p)


def _alpha(text: str, upper: int, lower: int, digit: int | None) -> str:
    out = []
    for ch in str(text):
        if "A" <= ch <= "Z":
            out.append(chr(upper + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(lower + ord(ch) - 97))
        elif "0" <= ch <= "9" and digit:
            out.append(chr(digit + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)


def mbold(text) -> str:
    """𝐁𝐨𝐥𝐝 math letters (for names)."""
    return _alpha(text, 0x1D400, 0x1D41A, 0x1D7CE)


def mbolditalic(text) -> str:
    """𝑩𝒐𝒍𝒅 𝒊𝒕𝒂𝒍𝒊𝒄 math letters (for team names)."""
    return _alpha(text, 0x1D468, 0x1D482, 0x1D7CE)


def mono(text) -> str:
    """𝙼𝚘𝚗𝚘 math letters (for titles and numbers)."""
    return _alpha(text, 0x1D670, 0x1D68A, 0x1D7F6)


def bid_text(p: dict, t, user, amount: int) -> str:
    """The 'New Bid' post: styled names, @usernames on their own line."""
    pu = f"\n       (@{p['username']})" if p.get("username") else ""
    bu = f"\n       (@{user.username})" if user.username else ""
    return (f"💰 {nm(mono('New Bid'))}\n{DIV}\n"
            f"{ROLE_EMOJI[p['role']]} Player ➳ {nm(mbold(p['name']))}{nm(pu)}\n"
            f"🏆 Team ➳ {nm(mbolditalic(t.name))}\n"
            f"🎯 Bidder ➳ {nm(mbold(user.full_name))}{nm(bu)}\n\n"
            f"💵 Current Bid ➳ {nm(mono(amount))} pts")


def set_label(s, i) -> str:
    return s["name"] if s.get("num") else f"Set {i + 1}: {s['name']}"


def build_sets(players, size=None):
    """Shuffle everyone and cut them into RANDOM sets of `size` players (the last set may be smaller).
    Roles don't matter for the sets any more."""
    size = max(int(size or SET_SIZE), 1)
    pool = [dict(p) for p in players]
    random.shuffle(pool)
    for pid, p in enumerate(pool, 1):
        p["id"], p["role"] = pid, norm_role(p)
    sets = []
    for n, k in enumerate(range(0, len(pool), size), 1):
        queue = pool[k:k + size]
        sets.append({"num": n, "name": f"Set {n}", "queue": queue, "total": len(queue),
                     "announced": False})
    return sets


@dataclass
class Team:
    name: str
    budget: int = BUDGET  # always set from the auction's /setbudget when the team is added
    squad: list = field(default_factory=list)  # (player dict, price)
    # people who can bid for this team (added by the owner / auctioneer with /addteam, /addvc)
    manager_username: str = ""
    manager_id: int | None = None   # filled in the first time we see that user in the group
    manager_name: str = ""
    vc_username: str = ""
    vc_id: int | None = None
    vc_name: str = ""


@dataclass
class Auction:
    host: int
    status: str = "lobby"  # lobby | running | finished
    teams: dict = field(default_factory=dict)      # team key (lower-case name) -> Team
    players: dict = field(default_factory=dict)    # registered cricketers
    reg_open: bool = False
    reg_group: dict | None = None    # tournament group players must join: {id, title, link}
    reg_channel: dict | None = None  # tournament channel players must join
    reg_minutes: int = 0             # auto-close minutes given with /openreg
    reg_configured: bool = False     # name / group / channel already asked once
    reg_close_at: float = 0.0        # epoch time the auto-close fires (so a restart can re-arm it)
    sets: list = field(default_factory=list)         # sets in play
    reg_sets: list = field(default_factory=list)     # built from registrations
    custom_sets: list = field(default_factory=list)  # host-provided sets
    set_idx: int = 0
    current: dict | None = None
    bid: int = 0
    bidder: str | None = None       # key of the team that holds the top bid
    bidder_by: str = ""             # who placed it (manager or vice-captain)
    msg_id: int | None = None
    lot_media: bool = False
    # live settings (auctioneer changes them with /setbudget, /setsquad, /settimer, /setgap, /setbase)
    budget: int = BUDGET
    squad_size: int = SQUAD_SIZE
    bid_timer: int = BID_TIMER
    gap: int = GAP
    default_base: int = DEFAULT_BASE
    set_size: int = SET_SIZE
    custom_groups: dict = field(default_factory=dict)  # set number -> {"title", "players"}
    title: str = TOURNAMENT                          # fixed: the one tournament this bot runs
    host_name: str = ""
    unsold: list = field(default_factory=list)       # players nobody bid for
    photo_ids: dict = field(default_factory=dict)    # player uid -> profile photo file_id ("" = none)
    ended_early: bool = False
    min_players: int = 0     # minimum squad a team must reach (0 = same as the maximum)
    team_count: int = 0      # how many teams the host said will play
    wiz: str = ""            # setup question being asked right now ("" = done)
    wiz_user: int = 0        # who answers the setup questions
    set_wait: int = SET_WAIT  # pause between pinning a set and its first lot (seconds)
    pin_id: int | None = None  # message id of the pinned set
    purse_set: bool = False  # purse / base / max / min are confirmed before the start
    base_set: bool = False
    max_set: bool = False
    min_set: bool = False
    order_set: bool = False   # the set order was answered in /next
    set_order: list = field(default_factory=list)  # e.g. [2, 5, 4, 1, 3] (empty = as added)
    start_after_wiz: bool = False  # /startauction was typed, finish the questions then begin
    start_args: list = field(default_factory=list)


AUCTIONS: dict[int, Auction] = {}


# ---------- keep set-up auctions + registrations across restarts / redeploys ----------
# Only auctions still in the lobby (setup, teams, player registration) are saved: a restart used
# to wipe them and the host had to /openreg again. Saved in the same store as the other
# settings (MongoDB if MONGO_URI is set, else a json file in the data folder).
STATE_KEY = "auction_state"
_LAST_SAVED = ""


def save_auctions(force: bool = False):
    global _LAST_SAVED
    try:
        data = {str(cid): dataclasses.asdict(a) for cid, a in AUCTIONS.items() if a.status == "lobby"}
        blob = json.dumps(data, sort_keys=True, default=str)
        if blob == _LAST_SAVED and not force:
            return
        _db().save_json(STATE_KEY, data)
        _LAST_SAVED = blob
    except Exception as e:
        log.warning("could not save auctions: %s", e)


def _restore_auction(d: dict) -> Auction:
    d = {k: v for k, v in d.items() if k in {f.name for f in dataclasses.fields(Auction)}}
    tnames = {f.name for f in dataclasses.fields(Team)}
    teams = {}
    for k, t in (d.get("teams") or {}).items():
        t = {x: y for x, y in t.items() if x in tnames}
        t["squad"] = [tuple(x) for x in t.get("squad", [])]
        teams[k] = Team(**t)
    d["teams"] = teams
    d["players"] = {int(k): v for k, v in (d.get("players") or {}).items()}
    d["photo_ids"] = {int(k): v for k, v in (d.get("photo_ids") or {}).items()}
    d["custom_groups"] = {(int(k) if str(k).isdigit() else k): v
                          for k, v in (d.get("custom_groups") or {}).items()}
    return Auction(**d)


def load_auctions(app):
    """Called once at start-up: bring back saved auctions and re-arm the registration auto-close."""
    global _LAST_SAVED
    try:
        data = _db().load_json(STATE_KEY, {})
    except Exception as e:
        log.warning("could not load saved auctions: %s", e)
        return
    for cid, d in (data if isinstance(data, dict) else {}).items():
        try:
            AUCTIONS[int(cid)] = a = _restore_auction(d)
        except Exception as e:
            log.warning("skipped a saved auction (%s): %s", cid, e)
            continue
        if a.reg_open and a.reg_close_at and app.job_queue:
            app.job_queue.run_once(auto_close_reg, max(a.reg_close_at - time.time(), 5),
                                   chat_id=int(cid), name=f"reg-{cid}")
    _LAST_SAVED = json.dumps({str(c): dataclasses.asdict(a) for c, a in AUCTIONS.items()
                              if a.status == "lobby"}, sort_keys=True, default=str)
    if AUCTIONS:
        log.info("restored %d auction(s) from the last run", len(AUCTIONS))


async def _autosave(ctx):
    save_auctions()


def max_bid(a: Auction, t: Team) -> int:
    """Keep enough points to reach the minimum squad at the cheapest price."""
    slots_left = (a.min_players or a.squad_size) - len(t.squad) - 1
    return t.budget - max(slots_left, 0) * MIN_BASE


def cancel_jobs(ctx, name):
    for j in ctx.job_queue.get_jobs_by_name(name):
        j.schedule_removal()


# ---------- SUDO: who may use the auction bot ----------
# The owner (OWNER_ID in .env) is always allowed. Everyone else needs the owner to run
# /addsudo @Username. Players (DM registration) and team managers / vice-captains (/bid,
# /team, /purse) do NOT need sudo - sudo is for people who run auctions.
SUDO_FILE = DATA / "auction_sudo.json"


def _load_sudo() -> list:
    data = _db().load_json("auction_sudo", [])
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and (e.get("id") or e.get("username"))]


SUDO: list = _load_sudo()  # [{"id": int | None, "username": str, "name": str}]


def _save_sudo():
    _db().save_json("auction_sudo", SUDO)


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def is_sudo(user_id: int) -> bool:
    return is_owner(user_id) or any(e.get("id") == user_id for e in SUDO)


def sudo_entry(username: str = "", user_id: int | None = None):
    un = username.lower()
    for e in SUDO:
        if (user_id and e.get("id") == user_id) or (un and (e.get("username") or "").lower() == un):
            return e
    return None


async def sudo_bind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Runs before every other handler: links a sudo @username to its Telegram id the first time
    that person talks to the bot (ids are what we trust afterwards, so a later username change
    or reuse can't take over the access)."""
    user = update.effective_user
    if not user or user.is_bot:
        return
    un, changed = (user.username or "").lower(), False
    for e in SUDO:
        if e.get("id") is None and un and (e.get("username") or "").lower() == un:
            e["id"], e["name"], changed = user.id, user.full_name, True
        elif e.get("id") == user.id and user.username and e.get("username") != user.username:
            e["username"], changed = user.username, True
    if changed:
        _save_sudo()


NO_SUDO = ("🔒 <b>No access.</b> Only the owner and sudo users can use this bot.\n"
           f"👑 Ask the owner to add you with {cmd('addsudo')} [@Username].")


def _sudo_target(msg, args):
    """(username, user_id, name) from '/addsudo @user' or from replying to that person's message;
    None if neither was given, 'bad' if the username is malformed."""
    if args:
        un = args[0].lstrip("@").strip()
        if not USER_RE.match(un):
            return "bad"
        uid, name = None, ""
        for seen in SEEN.values():  # already seen talking in a group? then we know the id
            if un.lower() in seen:
                uid, name = seen[un.lower()]
                break
        return un, uid, name
    src = msg.reply_to_message.from_user if msg.reply_to_message else None
    if src and not src.is_bot:
        return src.username or "", src.id, src.full_name
    return None


async def addsudo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/addsudo @Username  (owner only). Reply to someone's message with /addsudo to add them by id."""
    msg = update.message
    if not is_owner(update.effective_user.id):
        return await rep(msg, "🚫 Only the <b>owner</b> can add sudo users.")
    target = _sudo_target(msg, ctx.args)
    if target is None or target == "bad":
        return await rep(msg,
            ("🚫 That doesn't look like a Telegram username.\n\n" if target == "bad" else "")
            + "🛡 <b>ADD SUDO</b>\n"
            f"{cmd('addsudo')} [@Username]\n"
            f"Example: {cmd('addsudo')} {nm('@raj_k')}\n"
            "💡 Or reply to their message with the command.\n"
            f"📋 {cmd('sudo')} — list · {cmd('rmsudo')} — remove")
    un, uid, name = target
    if uid and is_owner(uid):
        return await rep(msg, "😅 That's the owner — the owner always has full access.")
    if sudo_entry(un, uid):
        return await rep(msg, f"😅 {nm('@' + un if un else name)} is already a sudo user.")
    SUDO.append({"id": uid, "username": un, "name": name})
    _save_sudo()
    who = nm("@" + un) if un else nm(name or uid)
    await rep(msg,
        f"✅ {who} is now a <b>sudo user</b> 🛡\n"
        + ("🔗 Linked and ready." if uid else
           "⏳ They're linked automatically as soon as they message me (or talk in a group I'm in).")
        + f"\n📋 {cmd('sudo')}")


async def rmsudo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/rmsudo @Username  (owner only)."""
    msg = update.message
    if not is_owner(update.effective_user.id):
        return await rep(msg, "🚫 Only the <b>owner</b> can remove sudo users.")
    target = _sudo_target(msg, ctx.args)
    if target is None or target == "bad":
        return await rep(msg, f"🗑 Usage: {cmd('rmsudo')} [@Username]  ·  📋 {cmd('sudo')}")
    un, uid, _ = target
    e = sudo_entry(un, uid)
    if not e:
        return await rep(msg, f"🤷 {nm('@' + un if un else uid)} isn't a sudo user. See {cmd('sudo')}.")
    SUDO.remove(e)
    _save_sudo()
    await rep(msg, f"🗑 {nm('@' + e['username'] if e.get('username') else e.get('name') or e.get('id'))} "
                   "is no longer a sudo user.")


async def sudo_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/sudo = list the sudo users. Reply to someone's message with /sudo = give them sudo (owner only)."""
    if update.message.reply_to_message and not ctx.args:
        return await addsudo(update, ctx)
    return await sudo_list(update, ctx)


async def sudo_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/sudo - owner and sudo users can see who has access."""
    msg = update.message
    if not is_sudo(update.effective_user.id):
        return await rep(msg, NO_SUDO)
    lines = ["🛡 <b>SUDO USERS</b>",
             f"👑 Owner: <code>{OWNER_ID}</code>" if OWNER_ID else "👑 Owner: <i>not set (OWNER_ID in .env)</i>"]
    if not SUDO:
        lines.append(f"\n📭 No sudo users yet. Owner: {cmd('addsudo')} [@Username]")
    for n, e in enumerate(SUDO, 1):
        label = nm("@" + e["username"]) if e.get("username") else nm(e.get("name") or e.get("id"))
        state = "✅" if e.get("id") else "⏳ waiting for their first message"
        lines.append(f"{n}. {label} {state}")
    await rep(msg, "\n".join(lines))


async def group_auction(update: Update, create=False):
    """Return the auction for this group chat (optionally creating it).
    Creating / controlling an auction needs sudo (or the owner)."""
    chat = update.effective_chat
    if chat.type == "private":
        await rep(update.message, "👥 Please use this command in your <b>group</b>.")
        return None
    if create and not is_sudo(update.effective_user.id):
        await rep(update.message, NO_SUDO)
        return None
    a = AUCTIONS.get(chat.id)
    if not a and create:
        a = AUCTIONS[chat.id] = Auction(host=update.effective_user.id,
                                        host_name=update.effective_user.full_name)
    return a


def is_host(a: Auction, user_id: int) -> bool:
    """The auctioneer who started this auction, or the owner - and they must still have sudo."""
    return is_sudo(user_id) and (user_id == a.host or is_owner(user_id))


# ---------- teams: managers + vice-captains ----------
SEEN: dict = {}  # chat_id -> {username_lower: (user_id, full_name)}; lets us link @usernames to ids


def remember_user(chat_id: int, user):
    if user and user.username and not user.is_bot:
        SEEN.setdefault(chat_id, {})[user.username.lower()] = (user.id, user.full_name)


def tkey(name: str) -> str:
    return re.sub(r"[\s_]+", " ", name).strip().lower()


def parse_team_args(args):
    """['@raj', 'Mumbai_Indians'] -> ('Mumbai Indians', 'raj').  The username comes FIRST
    (/addcap [Username] [Team Name]); the old order (team first, @username last) still works."""
    if len(args) < 2:
        return None
    if args[0].startswith("@") or not args[-1].startswith("@"):
        user, rest = args[0], args[1:]
    else:
        user, rest = args[-1], args[:-1]
    user = user.lstrip("@").strip()
    name = re.sub(r"[^\w \-'.&]", "", re.sub(r"[\s_]+", " ", " ".join(rest))).strip()[:30]
    if not name or not USER_RE.match(user):
        return None
    return name, user


def who_has(a: Auction, username: str):
    """(Team, 'manager'|'vice-captain') if this @username is already on a team."""
    un = username.lower()
    for t in a.teams.values():
        if t.manager_username.lower() == un:
            return t, "manager"
        if t.vc_username.lower() == un:
            return t, "vice-captain"
    return None, None


def find_team(a: Auction, user):
    """(Team, 'manager'|'vice-captain') for this Telegram user, else (None, None).
    Matches by id, or by @username the first time (then remembers the id)."""
    un = (user.username or "").lower()
    for t in a.teams.values():
        if t.manager_id == user.id or (un and t.manager_username.lower() == un and t.manager_id in (None, user.id)):
            t.manager_id, t.manager_name = user.id, user.full_name
            return t, "manager"
        if t.vc_id == user.id or (un and t.vc_username.lower() == un and t.vc_id in (None, user.id)):
            t.vc_id, t.vc_name = user.id, user.full_name
            return t, "vice-captain"
    return None, None


def people_line(t: Team) -> str:
    vc = nm("@" + t.vc_username) if t.vc_username else "—"
    return f"🧢 Captain: {nm('@' + t.manager_username)} · 🎖 VC: {vc}"


# ======================================================================
#  HELP
# ======================================================================
async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_media(ctx.bot, update.effective_chat.id, "welcome",
        f"🏏 <b>{nm(BRAND)}</b> 🏆\n"
        "Tournament auctions with live bidding, right in your group chat.\n\n"
        f"📖 {cmd('auc_help')} — <b>full step-by-step guide</b>\n\n"
        "👑 <b>HOST</b>\n"
        f"{cmd('start')} → tap <b>Auction</b> → <b>Become Auctioneer</b> — tournament name, number of teams 🚀\n"
        f"{cmd('addcap')} [@Captain_Username] [Team_Name] — add a team 🧢\n"
        f"{cmd('addvc')} / {cmd('changevc')} [@Username] [Team_Name] — vice-captain 🎖\n"
        f"{cmd('addset')} — add players: manually or import registered ones 📦\n"
        f"{cmd('setpurse')} [amount] — purse for every team 💰\n"
        f"{cmd('editauc')} — change base price, gap, timer, players... ⚙️\n"
        f"{cmd('startauction')} — asks purse / base price if missing, then starts 🔨\n"
        f"{cmd('end_auction')} — end it + send the squad / unsold PDFs 📄\n"
        f"{cmd('playerlist')} — see who has registered 📋\n"
        f"{cmd('addplayerreg')} / {cmd('deregister')} [@Username] — add / remove a registered player by hand ✍️\n"
        f"{cmd('addaucplayer')} [@Username] — add a player at the end of a running auction ➕\n"
        f"{cmd('skip')} — skip the player up for bidding ⏭\n"
        f"{cmd('resold')} [@Username] — take back a sold player, refund the team, re-auction at the end ♻️\n"
        f"{cmd('cancel')} — cancel everything 🛑\n"
        f"{cmd('squads')} — saved squads of finished tournaments 📚\n\n"
        f"🧢 <b>CAPTAINS &amp; VICE-CAPTAINS</b>: {cmd('team')} · {cmd('purse')} · {cmd('bid')} [points]\n\n"
        f"🛡 <b>ACCESS</b>: only the owner and <b>sudo</b> users can run auctions. "
        f"Owner: reply to someone's message with {cmd('sudo')} to give access · {cmd('addsudo')} [@Username] · "
        f"{cmd('rmsudo')} [@Username] · {cmd('sudo')} (list)\n"
        f"🧢 <b>PLAYERS</b>: send {cmd('start')} to me in DM → tap <b>Player Registration</b>")


# ======================================================================
#  /start: welcome + setup panel (buttons).  /help keeps the command list.
# ======================================================================
class ArgCtx:
    """Context wrapper with custom args, so button / typed input can reuse the command handlers."""
    def __init__(self, ctx, args):
        self._c, self.args = ctx, args

    def __getattr__(self, name):
        return getattr(self._c, name)


SETUP_PROMPTS = {
    "name": ("✏️ <b>AUCTION NAME</b>", "Send the name, e.g. <i>Soul Strikers Premier League</i>"),
    "team": ("🏟 <b>ADD A TEAM</b>", "Send <i>@captain_username Team_Name</i>\nExample: @raj_k Mumbai_Indians"),
    "vc": ("🎖 <b>VICE-CAPTAIN</b>", "Send <i>@vc_username Team_Name</i> — adds, or replaces, that team's VC"),
    "budget": ("💰 <b>PURSE</b>", "Send the points each team gets, e.g. 100"),
    "squad": ("👥 <b>SQUAD SIZE</b>", "Send how many players each team buys, e.g. 4"),
    "base": ("<b>BASE PRICE</b>", "Send the base price every registered player gets, e.g. 1"),
    "setsize": ("📦 <b>PLAYERS PER SET</b>", "Send how many players go in each random set, e.g. 5"),
    "timer": ("⏱ <b>BID TIMER</b>", "Send seconds after the last bid before SOLD, e.g. 20"),
    "gap": ("⏳ <b>GAP BETWEEN LOTS</b>", "Send seconds between players, e.g. 5"),
}


def panel_text(a: Auction) -> str:
    teams = ", ".join(t.name for t in a.teams.values())
    sets = a.custom_sets or a.reg_sets
    n_players = sum(x["total"] for x in sets)
    return (f"🏏 <b>{nm(BRAND)}</b> 🏆\n"
            "Welcome! Set up your auction with the buttons below 👇\n\n"
            f"🏆 Tournament: <b>{nm(a.title)}</b>\n"
            f"🏟 Teams: <b>{len(a.teams)}</b>" + (f" ({nm(teams)})" if teams else "") + "\n"
            f"💰 Purse: <b>{a.budget}</b> · 👥 Squad size: <b>{a.squad_size}</b>\n"
            f"Base: <b>{a.default_base}</b> · ⏱ Timer: <b>{a.bid_timer}s</b> · ⏳ Gap: <b>{a.gap}s</b>\n"
            f"📦 Sets: <b>{len(sets)}</b> · 🧢 Players: <b>{n_players}</b>\n"
            f"👑 Host: <b>{nm(a.host_name or 'owner')}</b>\n\n"
            f"📖 {cmd('help')} — all commands · {cmd('auc_help')} — full guide")


def panel_keyboard():
    def b(emoji, label, key):
        return Btn(f"{emoji} {sc(label)}", callback_data=f"su:{key}")
    return Markup([
        [b("🏟", "add team", "team"), b("🎖", "vice-captain", "vc")],
        [b("💰", "purse", "budget"), b("👥", "squad size", "squad")],
        [b("🏷", "base price", "base"), b("⏱", "timer", "timer")],
        [b("⏳", "gap", "gap"), b("📦", "players per set", "setsize")],
        [b("📋", "add sets", "sets"), b("🖼", "media", "media")],
        [b("🔨", "start auction", "start"), b("❓", "help", "help")],
    ])


async def send_panel(bot, chat_id, a: Auction, media: bool = True):
    if media:
        return await send_media(bot, chat_id, "welcome", panel_text(a), reply_markup=panel_keyboard())
    return await say(bot, chat_id, panel_text(a), reply_markup=panel_keyboard())


async def welcome_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/start in a group: welcome message with the auction setup buttons."""
    a = await group_auction(update, create=True)
    if a:
        await send_panel(ctx.bot, update.effective_chat.id, a)


async def on_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat, key = q.message.chat, q.data.split(":")[1]
    if key == "help":
        await q.answer()
        return await help_cmd(update, ctx)
    a = AUCTIONS.get(chat.id)
    if not a or not is_host(a, q.from_user.id):
        return await q.answer(sc("🚫 only the host can do that."), show_alert=True)
    await q.answer()
    if key == "start":
        if not (a.custom_sets or a.reg_sets):
            return await say(ctx.bot, chat.id,
                f"📦 Add the player sets first (tap <b>add sets</b>). To try the demo players use {cmd('startauction')} default.")
        fake = types.SimpleNamespace(effective_chat=chat, effective_user=q.from_user, message=q.message)
        return await startauction(fake, ArgCtx(ctx, []))
    if key == "sets":  # two ways to add sets
        return await say(ctx.bot, chat.id,
            "📦 <b>ADD SETS</b>\nHow do you want to add the players?",
            reply_markup=Markup([
                [Btn("📋 " + sc("import set from registered list"), callback_data="su:sets_reg")],
                [Btn("✍️ " + sc("add set manually"), callback_data="su:sets_manual")]]))
    if key == "sets_manual":
        return await say(ctx.bot, chat.id,
            "✍️ <b>ADD SET MANUALLY</b>\nSend the set as a message, one player per line:\n"
            f"<pre>{nm(SET_EXAMPLE)}</pre>"
            f"Then <b>reply</b> to it with {cmd('addset')} [Set_Number] [Set_Name], e.g. {cmd('addset')} 1 Marquee\n"
            f"More ways: {cmd('customsets')} (many sets at once) · {cmd('auc_help')}")
    if key == "sets_reg":  # build the sets from the players who registered in DM
        if a.status != "lobby":
            return await say(ctx.bot, chat.id, "🚫 The auction has already started.")
        if not a.players:
            return await say(ctx.bot, chat.id,
                "📭 <b>No registered players yet.</b>\n"
                f"Open registration with {cmd('openreg')} — players tap <b>Player Registration</b> in the bot's DM.")
        sets = a.reg_sets = build_sets(list(a.players.values()), a.set_size)
        total = sum(x["total"] for x in sets)
        await send_media(ctx.bot, chat.id, "sets",
            "📋 <b>SETS IMPORTED FROM REGISTERED PLAYERS</b>\n"
            f"🧢 Players: <b>{total}</b> · 📦 Sets: <b>{len(sets)}</b> (random, {a.set_size} per set)")
        await send_long(ctx.bot, chat.id, "\n".join(set_lines(a, sets)))
        note = (f"⚠️ Registration is still open — tap import again after {cmd('closereg')} to include late sign-ups.\n"
                if a.reg_open else "")
        return await say(ctx.bot, chat.id, f"{note}🔨 Ready? Tap <b>start auction</b> or send {cmd('startauction')}.")
    if key == "media":
        text, kb = slot_menu(str(chat.id))
        return await say(ctx.bot, chat.id, text, reply_markup=kb)
    if key not in SETUP_PROMPTS:
        return
    title, how = SETUP_PROMPTS[key]
    cur = ""
    if key in SETTINGS:
        cur = f"\nNow: <b>{getattr(a, SETTINGS[key][0])}</b>"
    elif key == "name" and a.title:
        cur = f"\nNow: <b>{nm(a.title)}</b>"
    ctx.user_data["setup"] = (chat.id, key)
    await say(ctx.bot, chat.id, f"{title}\n{how}{cur}\n<i>(send the value in this chat · send “cancel” to stop)</i>")


async def setup_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """The host's typed answer after tapping a setup button."""
    pend, msg = ctx.user_data.get("setup"), update.message
    wa = AUCTIONS.get(update.effective_chat.id)
    if wa and wa.wiz and msg and msg.text and update.effective_user.id == wa.wiz_user:
        return await wizard_input(update, ctx, wa)
    if not pend or not msg or not msg.text or pend[0] != update.effective_chat.id:
        return
    ctx.user_data.pop("setup", None)
    text = msg.text.strip()
    if text.lower() in ("cancel", "stop"):
        return await rep(msg, "🛑 Cancelled.")
    key, parts = pend[1], text.split()
    a = AUCTIONS.get(update.effective_chat.id)
    if key == "name":
        await setname(update, ArgCtx(ctx, parts))
    elif key == "team":
        await addteam(update, ArgCtx(ctx, parts))
    elif key == "vc":
        parsed = parse_team_args(parts)
        t = a.teams.get(tkey(parsed[0])) if a and parsed else None
        await set_vc(update, ArgCtx(ctx, parts), change=bool(t and t.vc_username))
    elif key in SETTINGS:
        await make_setter(key)(update, ArgCtx(ctx, parts[:1]))
    a = AUCTIONS.get(update.effective_chat.id)
    if a:  # show the refreshed panel so the host can carry on
        await send_panel(ctx.bot, update.effective_chat.id, a, media=False)


async def setname(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = await group_auction(update, create=True)
    if not a:
        return
    if not is_host(a, update.effective_user.id):
        return await rep(update.message, "🚫 Only the <b>host</b> can do that.")
    await rep(update.message, f"🏆 This bot runs one tournament: <b>{nm(TOURNAMENT)}</b>. The name is fixed.")


# ======================================================================
#  GROUP: managers + registration control
# ======================================================================
async def join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Managers can't join by themselves any more - the owner / auctioneer adds them."""
    await rep(update.message,
        "🚫 Managers can't join by themselves.\n"
        f"👑 The owner or auctioneer adds you with {cmd('addcap')} [Username] [Team_Name].")


async def addteam(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/addcap [Captain_Username] [Team_Name]  (owner / auctioneer, before the start)."""
    a = await host_auction(update)
    if not a:
        return
    if a.team_count and len(a.teams) >= a.team_count:
        return await rep(update.message,
            f"🚫 All <b>{a.team_count}</b> teams are added. Need more? {cmd('editauc')} teams [number]\n"
            f"🎖 Vice-captains: {cmd('addvc')} [Username] [Team_Name] · 📦 Players: {cmd('addset')}")
    parsed = parse_team_args(ctx.args)
    if not parsed:
        return await rep(update.message,
            "🏟 <b>ADD A TEAM</b>\n"
            f"{cmd('addcap')} [Captain_Username] [Team_Name]\n"
            f"Example: {cmd('addcap')} @raj_k Mumbai_Indians\n\n"
            f"🎖 Vice-captain (optional): {cmd('addvc')} [Username] [Team_Name]")
    name, user = parsed
    if tkey(name) in a.teams:
        return await rep(update.message, f"🚫 A team called <b>{nm(name)}</b> already exists.")
    other, role = who_has(a, user)
    if other:
        return await rep(update.message,
            f"🚫 {nm('@' + user)} is already the {role} of <b>{nm(other.name)}</b>.")
    t = Team(name=name, budget=a.budget, manager_username=user)
    seen = SEEN.get(update.effective_chat.id, {}).get(user.lower())
    if seen:
        t.manager_id, t.manager_name = seen
    a.teams[tkey(name)] = t
    n, total = len(a.teams), a.team_count
    if total and n >= total:
        nxt = (f"🎉 <b>All {total} teams are added!</b>\n"
               f"🎖 Vice-captains (optional): {cmd('addvc')} [Username] [Team_Name]\n"
               f"📦 Next, the players: {cmd('addset')}")
    else:
        nxt = (f"➕ Next team ({n + 1}/{total or '?'}): {cmd('addcap')} [Username] [Team_Name]\n"
               f"🎖 VC for this team: {cmd('addvc')} [Username] {nm(name.replace(' ', '_'))}")
    await rep(update.message,
        f"✅ Team <b>{nm(name)}</b> added! 🏟\n🧢 Captain: {nm('@' + user)}\n"
        f"👥 Teams: <b>{n}/{total or '?'}</b>\n{nxt}")


async def removeteam(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = await host_auction(update)
    if not a:
        return
    t = a.teams.pop(tkey(" ".join(ctx.args)), None) if ctx.args else None
    if not t:
        return await rep(update.message, f"🗑 Usage: {cmd('removeteam')} [Team_Name] · see {cmd('teams')}")
    await rep(update.message, f"🗑 Team <b>{nm(t.name)}</b> removed.")


async def set_vc(update: Update, ctx: ContextTypes.DEFAULT_TYPE, change: bool):
    """/addvc and /changevc [Team_Name] [Username] - owner / auctioneer, also during the auction."""
    a = await host_auction(update, lobby_only=False)
    if not a:
        return
    me = "changevc" if change else "addvc"
    if a.status == "finished":
        return await rep(update.message, "🚫 The auction is over.")
    parsed = parse_team_args(ctx.args)
    if not parsed:
        return await rep(update.message,
            f"🎖 <b>{'CHANGE' if change else 'ADD'} VICE-CAPTAIN</b>\n"
            f"{cmd(me)} [Vice-Captain_Username] [Team_Name]\n"
            f"Example: {cmd(me)} @amit_s Mumbai_Indians\n📋 {cmd('teams')}")
    name, user = parsed
    t = a.teams.get(tkey(name))
    if not t:
        return await rep(update.message, f"🚫 No team called <b>{nm(name)}</b>. See {cmd('teams')}.")
    if change and not t.vc_username:
        return await rep(update.message, f"🚫 <b>{nm(t.name)}</b> has no vice-captain yet. Use {cmd('addvc')}.")
    if not change and t.vc_username:
        return await rep(update.message,
            f"🚫 <b>{nm(t.name)}</b> already has {nm('@' + t.vc_username)} as vice-captain. "
            f"Use {cmd('changevc')} to replace.")
    other, role = who_has(a, user)
    if other is t and role == "vice-captain":
        return await rep(update.message, f"😅 {nm('@' + user)} is already the vice-captain.")
    if other:
        return await rep(update.message,
            f"🚫 {nm('@' + user)} is already the {role} of <b>{nm(other.name)}</b>.")
    old = t.vc_username
    t.vc_username, t.vc_id, t.vc_name = user, None, ""
    seen = SEEN.get(update.effective_chat.id, {}).get(user.lower())
    if seen:
        t.vc_id, t.vc_name = seen
    await rep(update.message,
        f"✅ Vice-captain of <b>{nm(t.name)}</b> "
        + (f"changed: {nm('@' + old)} ➜ {nm('@' + user)}" if change else f"is {nm('@' + user)}")
        + " 🎖\n💡 Manager and vice-captain can both bid for the team.")


async def addvc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await set_vc(update, ctx, change=False)


async def changevc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await set_vc(update, ctx, change=True)


async def teams_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = AUCTIONS.get(update.effective_chat.id)
    if not a or not a.teams:
        return await rep(update.message, f"📭 No teams yet. Host: {cmd('addcap')} [Username] [Team_Name]")
    lines = [f"🏟 <b>{len(a.teams)} TEAMS</b>"]
    for t in a.teams.values():
        lines.append(f"• <b>{nm(t.name)}</b>\n   {people_line(t)}")
    await send_long(ctx.bot, update.effective_chat.id, "\n".join(lines))


async def bind_seen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Quietly remember who is who in the group so @usernames can be linked to ids."""
    if update.effective_chat and update.effective_user:
        remember_user(update.effective_chat.id, update.effective_user)
        a = AUCTIONS.get(update.effective_chat.id)
        if a:
            find_team(a, update.effective_user)


async def openreg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/openreg [minutes] - asks the tournament name, group id and channel id, then opens registration.
    Used again later it just re-opens registration (/openreg setup asks the questions again)."""
    a = await group_auction(update, create=True)
    if not a:
        return
    chat_id = update.effective_chat.id
    if not is_host(a, update.effective_user.id):
        return await rep(update.message, "🚫 Only the <b>host</b> can do that.")
    if a.status != "lobby":
        return await rep(update.message, "🚫 The auction has already started.")
    if a.reg_open:
        return await rep(update.message, "📝 Registration is already open.")
    if a.wiz and a.wiz not in REG_WIZ:
        return await rep(update.message, "✏️ Finish the setup question I asked first.")
    a.reg_minutes = next((int(x) for x in ctx.args if x.isdigit()), 0)
    again = any(x.lower() == "setup" for x in ctx.args)
    a.wiz_user = update.effective_user.id
    if a.reg_configured and not again:
        return await open_registration(ctx, chat_id, a)
    await wiz_ask(ctx.bot, chat_id, a, "regname")


async def open_registration(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, a: Auction):
    """Registration goes live: announcement + Register in DM button."""
    a.reg_open, a.reg_configured, a.wiz = True, True, ""
    cancel_jobs(ctx, f"reg-{chat_id}")
    link = f"https://t.me/{ctx.bot.username}?start=reg_{chat_id}"
    minutes = a.reg_minutes
    when = (f"⏳ Closes automatically in <b>{minutes} min</b>." if minutes
            else f"⏳ The host closes it with {cmd('closereg')}.")
    await send_media(ctx.bot, chat_id, "register",
        "📝 <b>PLAYER REGISTRATION IS OPEN!</b> 🔓\n"
        f"🏆 <b>{nm(a.title)}</b>\n{DIV}\n"
        f"<blockquote>{when}\n"
        "🧢 Cricketers — tap the button below, or send "
        f"{cmd('register')} to me in DM 👇</blockquote>",
        reply_markup=Markup([[Btn("📝 " + sc("register in dm"), url=link)]]))
    a.reg_close_at = (time.time() + minutes * 60) if minutes else 0.0
    if minutes:
        ctx.job_queue.run_once(auto_close_reg, minutes * 60, chat_id=chat_id,
                               name=f"reg-{chat_id}")
    save_auctions()


REG_WIZ = ("regname", "reggroup", "regchannel")


async def resolve_join(bot, text: str, kind: str, here_id=None):
    """'<id | @username | t.me/name | here> [invite link]' -> ({id, title, link}, None) or (None, error)."""
    parts = text.split()
    ref = parts[0] if parts else ""
    if ref.lower() in ("here", "this") and here_id:
        ref = str(here_id)
    c = await _resolve_chat(bot, ref)
    if c is None:
        return None, ("🚫 I couldn't find that chat. Add me to it first (as admin for a channel), then "
                      f"send its numeric id, e.g. {nm('-1001234567890')}, or its @username.")
    if c.type not in (("group", "supergroup") if kind == "group" else ("channel",)):
        return None, f"🚫 That is a <b>{c.type}</b>, not a {kind}. Send the {kind}'s id."
    try:
        st = (await bot.get_chat_member(c.id, bot.id)).status
    except Exception:
        st = None
    if st not in ("member", "administrator", "creator"):
        return None, (f"🚫 I'm not in <b>{nm(c.title)}</b>. Add me there "
                      f"({'as admin' if kind == 'channel' else 'as a member or admin'}) and send the id again.")
    if kind == "channel" and st != "administrator":
        return None, (f"🚫 Make me an <b>admin</b> of <b>{nm(c.title)}</b> — otherwise I can't check "
                      "who has joined. Then send the id again.")
    link = next((x for x in parts[1:] if x.startswith("http")), "")
    if not link and c.username:
        link = f"https://t.me/{c.username}"
    if not link:
        link = getattr(c, "invite_link", "") or ""
    if not link:
        try:
            link = await bot.export_chat_invite_link(c.id)
        except Exception:
            link = ""
    if not link:  # the join button needs a link
        return None, (f"🚫 I couldn't get a join link for <b>{nm(c.title)}</b>. Either make me an admin with the "
                      "<b>Invite users</b> permission and send the id again, or send the id followed by the "
                      f"invite link, e.g. {nm('-1001234567890 https://t.me/+abc')}")
    return {"id": c.id, "title": c.title or str(c.id), "link": link}, None


async def reg_wizard_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE, a: Auction) -> bool:
    """Host answers of the /openreg questions. True if the message was handled here."""
    msg, chat_id = update.message, update.effective_chat.id
    text = msg.text.strip()
    if a.wiz not in REG_WIZ:
        return False
    if text.lower() in ("cancel", "stop"):
        a.wiz = ""
        await rep(msg, f"🛑 Registration setup cancelled. Send {cmd('openreg')} to start again.")
        return True
    if a.wiz == "regname":
        a.title = text[:40]
        await rep(msg, f"✅ Tournament: <b>{nm(a.title)}</b>")
        await wiz_ask(ctx.bot, chat_id, a, "reggroup")
        return True
    kind = "group" if a.wiz == "reggroup" else "channel"
    if text in ("-", "skip"):
        entry, err = None, None
    else:
        entry, err = await resolve_join(ctx.bot, text, kind, here_id=chat_id)
        if err:
            await rep(msg, err)
            return True
    if kind == "group":
        a.reg_group = entry
        await rep(msg, f"✅ Tournament group: <b>{nm(entry['title']) if entry else '—'}</b>"
)
        await wiz_ask(ctx.bot, chat_id, a, "regchannel")
        return True
    a.reg_channel = entry
    await rep(msg, f"✅ Tournament channel: <b>{nm(entry['title']) if entry else '—'}</b>"
)
    await open_registration(ctx, chat_id, a)
    return True


async def auto_close_reg(ctx: ContextTypes.DEFAULT_TYPE):
    a = AUCTIONS.get(ctx.job.chat_id)
    if a and a.reg_open:
        await close_registration(ctx, ctx.job.chat_id)


async def closereg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = await group_auction(update)
    if not a:
        return await rep(update.message, f"🤷 No auction here. Use {cmd('addcap')} or {cmd('openreg')}.")
    if not is_host(a, update.effective_user.id):
        return await rep(update.message, "🚫 Only the <b>host</b> can do that.")
    if not a.reg_open:
        return await rep(update.message, "🚫 Registration isn't open.")
    cancel_jobs(ctx, f"reg-{update.effective_chat.id}")
    await close_registration(ctx, update.effective_chat.id)


async def close_registration(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int):
    a = AUCTIONS[chat_id]
    a.reg_open, a.reg_close_at = False, 0.0
    sets = a.reg_sets = build_sets(list(a.players.values()), a.set_size)
    save_auctions()
    if not sets:
        return await say(ctx.bot, chat_id, "🔒 Registration closed, but <b>nobody registered</b> 😢")
    total = sum(s["total"] for s in sets)
    await send_media(ctx.bot, chat_id, "sets",
        "🔒 <b>REGISTRATION CLOSED!</b>\n"
        f"🧢 Players: <b>{total}</b> · 📦 Sets: <b>{len(sets)}</b> (random)")
    await send_long(ctx.bot, chat_id, "\n".join(set_lines(a, sets)))
    for i, s in enumerate(sets, 1):  # tell each player their set via DM
        for p in s["queue"]:
            try:
                await say(ctx.bot, p["uid"],
                    "🔒 <b>Registration closed!</b>\n"
                    f"📦 You're in <b>{set_label(s, i - 1)}</b>\n"
                    f"Base price: <b>{p.get('base') or a.default_base}</b>\n🍀 Good luck!")
            except Exception:
                pass


async def players_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/playerlist (or /players) - everyone who has registered so far."""
    a = await group_auction(update)
    if not a:
        return
    state = "🔓 Open" if a.reg_open else "🔒 Closed"
    if not a.players:
        return await rep(update.message, f"📋 <b>No players registered yet.</b>\n"
                                         f"<blockquote>Registration ➳ <b>{state}</b></blockquote>")
    lines = [f"📋 <b>REGISTERED PLAYERS</b>\n{DIV}\n"
             f"<blockquote>🏆 Tournament ➳ <b>{nm(a.title or TOURNAMENT)}</b>\n"
             f"🧢 Total ➳ <b>{len(a.players)}</b>\n"
             f"📝 Registration ➳ <b>{state}</b></blockquote>\n{DIV}"]
    for n, p in enumerate(a.players.values(), 1):
        lines.append(f"<b>{n}.</b> <b>{ment(p)}</b>")
    await send_long(ctx.bot, update.effective_chat.id, "\n".join(lines))


# ======================================================================
#  GROUP: start / cancel / info
# ======================================================================
# ---------- sets:  "1. Name | Username | Userid" ----------
def clean_set_name(name: str, default: str) -> str:
    cleaned = re.sub(r"[^\w \-'.]", "", name).strip()[:30]
    return cleaned or default


SET_RE = re.compile(r"^\s*set\b\s*(\d*)\s*[:\-]?\s*(.*)$", re.I)
NUM_RE = re.compile(r"^\s*\d+\s*[.)]\s*")
USER_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
NO_USER = {"", "-", "none", "na", "n/a", "null", "nil"}
SET_EXAMPLE = ("1. Virat Kohli | @imVkohli | 123456789\n2. Rohit Sharma | @ImRo45 | 987654321\n"
               "3. Jasprit Bumrah | - | 555555555")


def parse_player_line(line: str, budget: int):
    """'1. Name | Username | Userid [| Base [| Note]]' -> player dict, or None if invalid."""
    parts = [x.strip() for x in NUM_RE.sub("", line, count=1).split("|", 4)]
    if len(parts) < 3 or not parts[0]:
        return None
    name, user, uid = parts[0][:40], parts[1].lstrip("@").strip(), parts[2]
    if user.lower() in NO_USER:
        user = ""
    elif not USER_RE.match(user):
        return None
    if not (uid.isdigit() and int(uid) > 0):
        return None
    base = None
    if len(parts) > 3 and parts[3]:
        if not (parts[3].isdigit() and 0 < int(parts[3]) <= budget):
            return None
        base = int(parts[3])
    return {"name": name, "username": user, "uid": int(uid), "base": base, "role": "PL",
            "note": parts[4][:100] if len(parts) > 4 else ""}


def parse_groups(body: str, budget: int):
    """Text -> ([{num, title, players}], bad_lines). 'Set 2' / 'Set: Marquee' lines start a set."""
    groups, errors, cur = [], [], None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = SET_RE.match(line)
        if m and "|" not in line:
            cur = {"num": int(m.group(1)) if m.group(1) else None,
                   "title": clean_set_name(m.group(2), ""), "players": []}
            groups.append(cur)
            continue
        p = parse_player_line(line, budget)
        if not p:
            errors.append(line[:30])
            continue
        if cur is None:
            cur = {"num": None, "title": "", "players": []}
            groups.append(cur)
        cur["players"].append(p)
    return [g for g in groups if g["players"]], errors


def rows_to_groups(rows, budget: int):
    """CSV/JSON rows (set, name, username, userid, base, note) -> same shape as parse_groups."""
    groups, errors = {}, []
    for r in rows:
        r = {str(k or "").strip().lower(): v for k, v in dict(r).items()}
        line = " | ".join(str(r.get(k) or "").strip() for k in ("name", "username", "userid", "base", "note"))
        p = parse_player_line(line.rstrip(" |"), budget)
        if not p:
            errors.append(str(r.get("name") or "?")[:30])
            continue
        raw = str(r.get("set") or "").strip()
        key = int(raw) if raw.isdigit() else raw
        g = groups.setdefault(key, {"num": key if isinstance(key, int) else None,
                                    "title": "" if isinstance(key, int) else clean_set_name(raw, ""),
                                    "players": []})
        g["players"].append(p)
    return list(groups.values()), errors


def parse_custom_file(filename: str, data: bytes, budget: int):
    text = data.decode("utf-8-sig", errors="replace")
    if filename.lower().endswith(".json"):
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = rows.get("players", [])
        return rows_to_groups(rows, budget)
    return rows_to_groups(csv.DictReader(io.StringIO(text)), budget)


def build_custom_sets(groups: dict):
    sets, pid = [], 1
    for num in sorted(groups):
        g, queue = groups[num], []
        for p in g["players"]:
            p = dict(p)
            p["id"] = pid
            pid += 1
            queue.append(p)
        name = f"Set {num}" + (f" - {g['title']}" if g.get("title") else "")
        sets.append({"num": num, "name": name, "queue": queue, "total": len(queue),
                     "announced": False})
    return sets


def refresh_custom(a: Auction):
    a.custom_sets = build_custom_sets(a.custom_groups)


def add_group(a: Auction, num: int, title: str, players: list):
    """Save/replace set `num`. A Telegram userid can only be in one set. Returns skipped names."""
    seen = {p["uid"] for n, g in a.custom_groups.items() if n != num for p in g["players"]}
    keep, dups = [], []
    for p in players:
        if p["uid"] in seen:
            dups.append(p["name"])
        else:
            seen.add(p["uid"])
            keep.append(p)
    if keep:
        a.custom_groups[num] = {"title": title, "players": keep}
        refresh_custom(a)
    return dups


def set_lines(a: Auction, sets) -> list:
    lines = []
    for i, s in enumerate(sets):
        lines.append(f"📦 <b>{set_label(s, i).upper()}</b> ({s['total']})")
        for n, p in enumerate(s["queue"], 1):
            lines.append(f"   {n}. {ROLE_EMOJI[p['role']]} <b>{pname(p)}</b> · Base {p.get('base') or a.default_base}")
        lines.append("")
    return lines


async def host_auction(update: Update, lobby_only: bool = True):
    """Auction for this group, only if the sender is the host/owner (and still in the lobby)."""
    a = await group_auction(update, create=True)
    if not a:
        return None
    if not is_host(a, update.effective_user.id):
        await rep(update.message, "🚫 Only the <b>host</b> can do that.")
        return None
    if lobby_only and a.status != "lobby":
        await rep(update.message, "🚫 The auction has already started.")
        return None
    return a


def bad_lines_note(errors, dups=()) -> str:
    out = ""
    if errors:
        out += f"\n⚠️ Skipped {len(errors)} bad line(s): {nm('; '.join(errors[:5]))}"
    if dups:
        out += f"\n⚠️ Already in another set (skipped): {nm('; '.join(dups[:5]))}"
    return out


async def addaucset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reply to a set message with /addaucset [Set_Number]."""
    a = await host_auction(update)
    if not a:
        return
    msg = update.message
    src = msg.reply_to_message
    text = ((src.text or src.caption or "") if src else "").strip()
    if not (ctx.args and ctx.args[0].isdigit() and 0 < int(ctx.args[0]) < 1000 and text):
        return await rep(msg,
            "📦 <b>ADD A SET</b>\n" +
            "1️⃣ Send the set as a message (one player per line):\n"
            f"<pre>{nm(SET_EXAMPLE)}</pre>\n"
            f"2️⃣ <b>Reply</b> to that message with {cmd('addset')} [Set_Number] [Set_Name]\n"
            f"Example: {cmd('addset')} 1 Marquee\n\n"
            "Format: <i>Name | Username | Userid</i> (optional: <i>| Base | Note</i>). "
            "Use <b>-</b> if a player has no username.")
    num = int(ctx.args[0])
    groups, errors = parse_groups(text, a.budget)
    players = [p for g in groups for p in g["players"]]
    if not players:
        return await rep(msg,
            "🚫 I couldn't find any valid player lines in that message.\n"
            f"Each line must look like:\n<pre>{nm('1. Name | Username | Userid')}</pre>" + bad_lines_note(errors))
    title = clean_set_name(" ".join(ctx.args[1:]), "") or next((g["title"] for g in groups if g["title"]), "")
    dups = add_group(a, num, title, players)
    if num not in a.custom_groups:
        return await rep(msg, "🚫 All of those players are already in other sets." + bad_lines_note([], dups))
    g = a.custom_groups[num]
    s = next(x for x in a.custom_sets if x["num"] == num)
    lines = [f"✅ <b>{s['name'].upper()} SAVED</b> 📦 — <b>{len(g['players'])}</b> players"]
    lines += [f"   {n}. <b>{pname(p)}</b> · Base {p.get('base') or a.default_base}"
              for n, p in enumerate(g["players"], 1)]
    lines.append(bad_lines_note(errors, dups).strip())
    total = sum(x["total"] for x in a.custom_sets)
    lines.append(f"\n📚 Sets: <b>{len(a.custom_sets)}</b> · 🧢 Players: <b>{total}</b>\n"
                 f"➕ Add another: {cmd('addset')} · 📋 {cmd('aucsets')} · 🔨 {cmd('startauction')} "
                 "(I'll ask the purse and base price first)")
    await send_long(ctx.bot, update.effective_chat.id, "\n".join(l for l in lines if l))


ADDSET_KB = Markup([
    [Btn("📋 " + sc("import set from registered list"), callback_data="su:sets_reg")],
    [Btn("✍️ " + sc("add set manually"), callback_data="su:sets_manual")]])


async def addset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/addset - choose how to add players: manually, or import the registered players.
    Replying to a set message with /addset [Set_Number] [Set_Name] saves that set directly."""
    if update.message.reply_to_message and ctx.args:
        return await addaucset(update, ctx)
    a = await host_auction(update)
    if not a:
        return
    await rep(update.message, "📦 <b>ADD SETS</b>\nHow do you want to add the players?", reply_markup=ADDSET_KB)


async def delaucset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = await host_auction(update)
    if not a:
        return
    if not (ctx.args and ctx.args[0].isdigit()):
        return await rep(update.message, f"🗑 Usage: {cmd('delaucset')} [Set_Number]")
    if a.custom_groups.pop(int(ctx.args[0]), None) is None:
        return await rep(update.message, "🤷 There's no such set.")
    refresh_custom(a)
    await rep(update.message, f"🗑 Set <b>{int(ctx.args[0])}</b> removed.")


async def aucsets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show every manually added set."""
    a = await group_auction(update)
    if not a or not a.custom_sets:
        return await rep(update.message,
                         f"📭 No sets added yet. Reply to a set message with {cmd('addset')} [Set_Number].")
    total = sum(x["total"] for x in a.custom_sets)
    await send_long(ctx.bot, update.effective_chat.id,
                    f"📚 <b>{len(a.custom_sets)} SETS · {total} PLAYERS</b>\n\n" + "\n".join(set_lines(a, a.custom_sets)))


async def save_custom(ctx, chat_id, a: Auction, groups, errors):
    """Replace ALL manual sets with the parsed groups (used by /customsets and file upload)."""
    if not groups:
        return await say(ctx.bot, chat_id,
            f"🚫 I couldn't find any valid players. Format: <b>1. Name | Username | Userid</b> "
            f"(send {cmd('customsets')} for an example)." + bad_lines_note(errors))
    a.custom_groups, dups, nxt = {}, [], 1
    for g in groups:
        num = g["num"] or nxt
        while num in a.custom_groups:
            num += 1
        nxt = max(nxt, num) + 1
        dups += add_group(a, num, g["title"], g["players"])
    refresh_custom(a)
    total = sum(x["total"] for x in a.custom_sets)
    lines = [f"✅ <b>SETS SAVED</b> 🧩 — <b>{len(a.custom_sets)}</b> sets, <b>{total}</b> players\n"]
    lines += set_lines(a, a.custom_sets)
    lines.append(bad_lines_note(errors, dups).strip())
    lines.append(f"🔨 Start with {cmd('startauction')}")
    await send_long(ctx.bot, chat_id, "\n".join(l for l in lines if l is not None))


async def customsets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bulk: /customsets + several sets in one message (replaces all manual sets)."""
    a = await host_auction(update)
    if not a:
        return
    parts = (update.message.text or "").split("\n", 1)
    body = parts[1] if len(parts) > 1 else ""
    if ctx.args and ctx.args[0].lower() == "clear" and not body.strip():
        a.custom_groups = {}
        refresh_custom(a)
        return await rep(update.message, "🧹 All sets cleared.")
    if not body.strip():
        return await rep(update.message,
            "🧩 <b>BULK SETS</b>\n"
            "Send several sets in one message, one player per line: "
            "<i>Name | Username | Userid</i>. This <b>replaces</b> all current sets.\n\n"
            f"<pre>{nm('/customsets' + chr(10) + SET_EXAMPLE + chr(10) + 'Set 2' + chr(10) + '1. Hardik Pandya | @hardik | 111222333')}</pre>\n"
            f"➕ To add or replace <b>one</b> set use {cmd('addset')} [Set_Number] (reply to the set).\n"
            f"📎 Or upload a <b>.csv / .json</b> with the caption {cmd('customsets')} "
            "(columns: set, name, username, userid, base, note).\n"
            f"🧹 {cmd('customsets')} clear — remove all sets")
    groups, errors = parse_groups(body, a.budget)
    await save_custom(ctx, update.effective_chat.id, a, groups, errors)


async def customsets_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = await host_auction(update)
    if not a:
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 1_000_000:
        return await rep(update.message, "🚫 File too large (max 1 MB).")
    try:
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        groups, errors = parse_custom_file(doc.file_name or "", data, a.budget)
    except Exception as e:
        log.warning("custom file failed: %s", e)
        return await rep(update.message, "🚫 I couldn't read that file. Use .csv or .json (set, name, username, userid).")
    await save_custom(ctx, update.effective_chat.id, a, groups, errors)


# ======================================================================
#  SETTINGS: the auctioneer edits these live (no code changes)
# ======================================================================
# key -> (attribute, label, min, max, only before the auction starts, unit)
SETTINGS = {
    "budget": ("budget", "💰 Purse", 1, 100000, True, "pts per team"),
    "squad": ("squad_size", "👥 Maximum players", 1, 50, True, "players per team (max)"),
    "min": ("min_players", "👥 Minimum players", 1, 50, True, "players per team (min)"),
    "timer": ("bid_timer", "⏱ Bid timer", 5, 300, False, "seconds after the last bid"),
    "gap": ("gap", "⏳ Gap between lots", 1, 120, False, "seconds"),
    "base": ("default_base", "Default base price", 1, 100000, True, "pts (for set lines with no base)"),
    "setsize": ("set_size", "📦 Players per set", 1, 100, True, "players (registered / default sets are random)"),
    "wait": ("set_wait", "📌 Pause after pinning a set", 0, 600, False, "seconds before its first lot"),
}


def settings_text(a: Auction) -> str:
    rows = [f"🏆 Tournament: <b>{nm(a.title)}</b> — {cmd('editauc')} name [new name]",
            f"🏟 Teams: <b>{a.team_count or len(a.teams)}</b> — {cmd('editauc')} teams [number]"]
    for key, (attr, label, lo, hi, _, unit) in SETTINGS.items():
        if key == "setsize":
            continue
        rows.append(f"{label}: <b>{getattr(a, attr) or '—'}</b> — {cmd('editauc')} "
                    f"{ {'budget': 'purse', 'squad': 'max'}.get(key, key) } [{lo}-{hi}]")
    return ("⚙️ <b>AUCTION SETTINGS</b>\n" + "\n".join(rows) +
            "\n\n💡 Name, teams, purse, players and base can be changed until the auction starts. "
            "Timer and gap can be changed any time (they apply from the next lot).")


EDIT_ALIASES = {"purse": "budget", "budget": "budget", "max": "squad", "squad": "squad",
                "maximum": "squad", "min": "min", "minimum": "min", "base": "base",
                "baseprice": "base", "gap": "gap", "timer": "timer", "setsize": "setsize",
                "wait": "wait", "setwait": "wait", "pause": "wait"}


async def editauc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/editauc [what] [value] - change any auction setting."""
    a = await group_auction(update)
    if not a:
        return await rep(update.message, "📭 No auction in this group yet. Tap <b>Auction</b> in /start.")
    if not is_host(a, update.effective_user.id):
        return await rep(update.message, "🚫 Only the <b>host</b> can do that.")
    if len(ctx.args) < 2:
        return await rep(update.message, settings_text(a) + f"\n\nExample: {cmd('editauc')} base 2")
    what, value = ctx.args[0].lower(), " ".join(ctx.args[1:])
    if what in ("name", "title", "tournament"):
        if a.status != "lobby":
            return await rep(update.message, "🚫 That can only be changed <b>before</b> the auction starts.")
        a.title = value.strip()[:40]
        return await rep(update.message, f"✅ Tournament name: <b>{nm(a.title)}</b>")
    if what == "teams":
        if a.status != "lobby":
            return await rep(update.message, "🚫 That can only be changed <b>before</b> the auction starts.")
        if not (value.isdigit() and 2 <= int(value) <= 30 and int(value) >= len(a.teams)):
            return await rep(update.message,
                f"🚫 Send a number from 2 to 30 (not below the <b>{len(a.teams)}</b> teams already created).")
        a.team_count = int(value)
        return await rep(update.message, f"✅ Teams: <b>{a.team_count}</b>")
    key = EDIT_ALIASES.get(what.replace("_", ""))
    if not key:
        return await rep(update.message, "🤷 I don't know that setting.\n\n" + settings_text(a))
    await make_setter(key)(update, ArgCtx(ctx, [value.split()[0]]))


async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = await group_auction(update)
    await rep(update.message, settings_text(a or Auction(host=0)))


def make_setter(key: str):
    attr, label, lo, hi, lobby_only, unit = SETTINGS[key]

    async def setter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        a = await group_auction(update, create=True)
        if not a:
            return
        if not is_host(a, update.effective_user.id):
            return await rep(update.message, "🚫 Only the <b>host</b> can do that.")
        if not ctx.args or not ctx.args[0].isdigit():
            return await rep(update.message,
                f"{label}: <b>{getattr(a, attr)}</b> ({unit})\n"
                f"Change it: {cmd('set' + key)} [{lo}-{hi}]\n📋 All settings: {cmd('settings')}")
        val = int(ctx.args[0])
        if not lo <= val <= hi:
            return await rep(update.message, f"🚫 {label} must be between <b>{lo}</b> and <b>{hi}</b>.")
        if lobby_only and a.status != "lobby":
            return await rep(update.message, "🚫 That can only be changed <b>before</b> the auction starts.")
        if key == "budget":
            top = max([p.get("base") or 0 for g in a.custom_groups.values() for p in g["players"]]
                      + [p.get("base") or 0 for p in a.players.values()] + [a.default_base])
            if val < top:
                return await rep(update.message,
                    f"🚫 A player's base price is <b>{top}</b>, which is above that budget. "
                    "Lower the base prices first.")
            for t in a.teams.values():
                t.budget = val
        if key == "min" and val > a.squad_size:
            return await rep(update.message, f"🚫 Minimum can't be above the maximum (<b>{a.squad_size}</b>).")
        if key == "squad" and a.min_players and val < a.min_players:
            return await rep(update.message, f"🚫 Maximum can't be below the minimum (<b>{a.min_players}</b>).")
        if key == "base" and val > a.budget:
            return await rep(update.message, f"🚫 Base price can't be higher than the budget (<b>{a.budget}</b>).")
        setattr(a, attr, val)
        if key == "budget":
            a.purse_set = True
        elif key == "base":
            a.base_set = True
        elif key == "squad":
            a.max_set = True
        elif key == "min":
            a.min_set = True
        await rep(update.message, f"✅ {label} set to <b>{val}</b> {unit}")

    return setter


# ======================================================================
#  /addfile: replace GIFs / images / video without touching the code
# ======================================================================
def media_from_message(msg):
    """(file object, kind, extension) for a photo / GIF / video message, else None."""
    if msg is None:
        return None
    if msg.animation:
        return msg.animation, "animation", "mp4"
    if msg.video:
        return msg.video, "video", "mp4"
    if msg.photo:
        return msg.photo[-1], "photo", "jpg"
    d = msg.document
    if d and d.mime_type:  # sent "as file" = original quality
        if d.mime_type == "image/gif":
            return d, "animation", "gif"
        if d.mime_type in ("image/png", "image/jpeg"):
            return d, "photo", "png" if d.mime_type == "image/png" else "jpg"
        if d.mime_type.startswith("video/"):
            return d, "video", "mp4"
    return None


async def media_scope(update: Update):
    """'global' (owner in DM) or this group's id (host) - None if not allowed."""
    chat, user = update.effective_chat, update.effective_user
    if chat.type == "private":
        if user.id in OWNER_IDS:
            return "global"
        await rep(update.message, f"🚫 Use {cmd('addfile')} in your <b>group</b> (host only).")
        return None
    a = await group_auction(update, create=True)
    if not a:
        return None
    if not is_host(a, user.id):
        await rep(update.message, "🚫 Only the <b>host</b> can do that.")
        return None
    return str(chat.id)


def slot_menu(scope: str):
    mine = MEDIA_DATA.get(scope, {})
    lines = ["🗂 <b>ADD / CHANGE MEDIA</b>",
             "Tap a place below, then send the photo / GIF / video.",
             "💡 Send it <b>as a file</b> (not compressed) for the best quality.\n"]
    for slot, (label, where) in MEDIA_SLOTS.items():
        mark = "✅ yours" if slot in mine else "⚙️ default"
        lines.append(f"{label} — {where} ({mark})")
    lines.append(f"\n⚡ Quick way: reply to any file with {cmd('addfile')} sold  (or put it in the caption)")
    lines.append(f"♻️ Go back to default: {cmd('delfile')} sold  /  {cmd('delfile')} all")
    rows, row = [], []
    for slot, (label, _) in MEDIA_SLOTS.items():
        row.append(Btn(sc(label.split(" ", 1)[1]), callback_data=f"af:{slot}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return "\n".join(lines), Markup(rows)


async def store_media(update: Update, ctx, scope: str, slot: str, msg) -> bool:
    got = media_from_message(msg)
    if not got:
        await rep(update.message, "🚫 That isn't a photo, GIF or video. Send one (as a file for best quality).")
        return False
    obj, kind, ext = got
    if getattr(obj, "file_size", 0) and obj.file_size > 20 * 1024 * 1024:
        await rep(update.message, "🚫 File too big — Telegram bots can read up to <b>20 MB</b>.")
        return False
    try:
        CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{scope.replace('-', 'n')}_{slot}_{int(time.time())}.{ext}"
        f = await obj.get_file()
        await f.download_to_drive(CUSTOM_DIR / name)
    except Exception as e:
        log.warning("addfile failed: %s", e)
        await rep(update.message, "🚫 I couldn't download that file. Try again.")
        return False
    slots = MEDIA_DATA.setdefault(scope, {})
    old = slots.get(slot)
    slots[slot] = {"file": name, "kind": kind}
    if old:
        (CUSTOM_DIR / old["file"]).unlink(missing_ok=True)
    _save_cfg()
    label = MEDIA_SLOTS[slot][0]
    await rep(update.message, f"✅ {label} updated! It will be used from now on.\n"
                              f"➕ Add another with {cmd('addfile')} · ♻️ {cmd('delfile')} {slot} to undo")
    return True


async def addfile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    scope = await media_scope(update)
    if not scope:
        return
    msg = update.message
    slot = ctx.args[0].lower() if ctx.args else None
    slot = SLOT_ALIAS.get(slot, slot)
    if slot == "cancel":
        ctx.user_data.pop("addfile", None)
        return await rep(msg, "🛑 Cancelled.")
    if slot not in MEDIA_SLOTS:
        text, kb = slot_menu(scope)
        pre = f"🚫 Unknown place <b>{nm(slot)}</b>.\n\n" if slot else ""
        return await rep(msg, pre + text, reply_markup=kb)
    if media_from_message(msg.reply_to_message):
        return await store_media(update, ctx, scope, slot, msg.reply_to_message)
    ctx.user_data["addfile"] = (scope, slot)
    await rep(msg, f"📤 Now send the file for <b>{MEDIA_SLOTS[slot][0]}</b> "
                   f"(photo, GIF or video). {cmd('addfile')} cancel to stop.")


async def addfile_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """A photo/GIF/video sent with the caption '/addfile <place>'."""
    scope = await media_scope(update)
    if not scope:
        return
    parts = (update.message.caption or "").split()
    slot = parts[1].lower() if len(parts) > 1 else None
    slot = SLOT_ALIAS.get(slot, slot)
    if slot not in MEDIA_SLOTS:
        text, kb = slot_menu(scope)
        return await rep(update.message, "🚫 Add the place to the caption, e.g. <b>/addfile sold</b>\n\n" + text,
                         reply_markup=kb)
    await store_media(update, ctx, scope, slot, update.message)


async def on_addfile_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat, uid = q.message.chat, q.from_user.id
    slot = q.data.split(":")[1]
    if chat.type == "private":
        ok, scope = uid in OWNER_IDS, "global"
    else:
        a = AUCTIONS.get(chat.id)
        ok, scope = bool(a and is_host(a, uid)), str(chat.id)
    if slot not in MEDIA_SLOTS or not ok:
        return await q.answer(sc("🚫 only the host can do that."), show_alert=True)
    ctx.user_data["addfile"] = (scope, slot)
    await q.answer()
    await say(ctx.bot, chat.id, f"📤 Now send the file for <b>{MEDIA_SLOTS[slot][0]}</b> "
                                f"(photo, GIF or video). {cmd('addfile')} cancel to stop.")


async def media_receiver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Catches the file the host sends after choosing a place."""
    pend = ctx.user_data.get("addfile")
    msg = update.message
    if not pend or not msg:
        return
    scope, slot = pend
    chat = update.effective_chat
    if scope != ("global" if chat.type == "private" else str(chat.id)):
        return
    if await store_media(update, ctx, scope, slot, msg):
        ctx.user_data.pop("addfile", None)


async def delfile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    scope = await media_scope(update)
    if not scope:
        return
    arg = ctx.args[0].lower() if ctx.args else ""
    arg = SLOT_ALIAS.get(arg, arg)
    slots = MEDIA_DATA.get(scope, {})
    if arg == "all":
        targets = list(slots)
    elif arg in MEDIA_SLOTS:
        targets = [arg] if arg in slots else []
    else:
        return await rep(update.message, f"♻️ Usage: {cmd('delfile')} [place] or {cmd('delfile')} all\n"
                                         f"Places: {nm(', '.join(MEDIA_SLOTS))}")
    if not targets:
        return await rep(update.message, "🤷 Nothing custom there — already using the default.")
    for t in targets:
        (CUSTOM_DIR / slots.pop(t)["file"]).unlink(missing_ok=True)
    _save_cfg()
    await rep(update.message, f"♻️ Back to default media for: <b>{nm(', '.join(targets))}</b>")


# ======================================================================
#  /auc_help: the full guide
# ======================================================================
async def auc_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_long(ctx.bot, update.effective_chat.id,
        "📖 <b>HOW TO RUN AN AUCTION</b>\n\n"
        "1️⃣ In your group send /start and tap <b>Auction</b>, then <b>Become Auctioneer</b>. "
        "I ask the <b>tournament name</b> and the <b>number of teams</b> — just send the answers, no command.\n\n"
        f"2️⃣ Add every team one by one: {cmd('addcap')} [@Captain_Username] [Team_Name]\n"
        f"    e.g. {cmd('addcap')} @raj_k Mumbai_Indians\n\n"
        f"3️⃣ Optional vice-captain: {cmd('addvc')} [@VC_Username] [Team_Name] "
        f"(change it any time with {cmd('changevc')}). The captain and the VC can both bid.\n\n"
        f"4️⃣ Add the players with {cmd('addset')}. I ask: <b>add manually</b> or <b>import from registered players</b>.\n"
        "Manual: send a message, one player per line:\n"
        f"<pre>{nm(SET_EXAMPLE)}</pre>"
        f"then <b>reply</b> to it with {cmd('addset')} [Set_Number] [Set_Name], e.g. {cmd('addset')} 1 Marquee. "
        f"Same number again replaces the set · {cmd('delaucset')} N removes · {cmd('aucsets')} shows all.\n"
        "Optional extras per line: <i>| Base | Note</i>. Use <b>-</b> when a player has no username.\n\n"
        f"5️⃣ Send {cmd('next')} — I ask the <b>total purse</b>, <b>base price</b>, <b>minimum players</b>, "
        f"<b>maximum players</b> and the <b>set order</b> (e.g. 2,5,4,1,3). "
        f"Then send {cmd('startauction')}. ({cmd('setpurse')} · {cmd('setbase')} also work.)\n\n"
        "6️⃣ Before every set I post the <b>full list of its players and pin it</b>; bidding starts "
        f"<b>1 minute</b> later ({cmd('setwait')} [seconds] changes it). Each player card carries the player's "
        f"profile picture. Bid with {cmd('bid')} [points] — the timer resets on every bid (with calls at 10s, going once / twice / thrice), and a team can never bid more "
        "than it needs to still reach the minimum squad.\n\n"
        f"7️⃣ Finish: {cmd('end_auction')} — posts the final squads and sends a squad PDF per team "
        "(DM only: owner, auctioneer and captains/VCs) and the unsold list PDF.\n\n"
        f"⚙️ {cmd('editauc')} — change tournament name, teams, purse, max / min players, base price, gap, timer, set pause.\n"
        f"🖼 {cmd('addfile')} [key] — change the GIFs / images. Keys: aucwelcome, aucregister, aucsets, aucintro, "
        f"auclive, aucsold, aucunsold, auctrophy, aucplayer (picture for players with no profile photo). "
        f"Reply to a file with {cmd('addfile')} aucsold · {cmd('files')} lists every key.\n"
        f"🛡 Owner: {cmd('addsudo')} / {cmd('rmsudo')} / {cmd('sudo')} · {cmd('setlog')}")


# ---------- start the auction ----------
PRE_STEPS = ("purse", "base", "min", "max", "order")
PRE_FLAG = {"purse": "purse_set", "base": "base_set", "min": "min_set", "max": "max_set",
            "order": "order_set"}


def pool_counts(a: Auction) -> list:
    """Number of sets of each player pool the host has built (registered / added)."""
    return sorted({len(x) for x in (a.reg_sets, a.custom_sets) if x})


def next_pre(a: Auction) -> str:
    """The first purse / base / max / min question that hasn't been answered yet ('' = all done)."""
    for step in PRE_STEPS:
        if step == "order" and not any(n > 1 for n in pool_counts(a)):
            continue  # nothing to reorder
        if not getattr(a, PRE_FLAG[step]):
            return step
    return ""


async def next_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/next - after the sets are imported: asks purse, base price, min / max players and the set order."""
    chat_id = update.effective_chat.id
    a = await group_auction(update)
    if not a or a.status != "lobby":
        return await rep(update.message, "🚫 Set up an auction first.")
    if not is_host(a, update.effective_user.id):
        return await rep(update.message, "🚫 Only the <b>host</b> can do that.")
    if a.wiz and a.wiz not in PRE_STEPS:
        return await rep(update.message, "✏️ Finish the setup questions first (tournament name / teams).")
    if not (a.reg_sets or a.custom_sets):
        return await rep(update.message, f"📦 Add the players first with {cmd('addset')}, then send {cmd('next')}.")
    a.purse_set = a.base_set = a.min_set = a.max_set = a.order_set = False  # ask everything again
    a.set_order, a.start_after_wiz, a.wiz_user = [], False, update.effective_user.id
    await wiz_ask(ctx.bot, chat_id, a, next_pre(a))


async def startauction(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    a = await group_auction(update)
    if not a or a.status != "lobby":
        return await rep(update.message, f"🚫 Tap <b>Auction</b> in /start and set it up first.")
    if not is_host(a, update.effective_user.id):
        return await rep(update.message, "🚫 Only the <b>host</b> can start.")
    if a.wiz and a.wiz not in PRE_STEPS:
        return await rep(update.message, "✏️ Finish the setup questions first (tournament name / teams).")
    if a.reg_open:
        return await rep(update.message, f"🚫 Close registration first with {cmd('closereg')}.")
    if len(a.teams) < 2:
        return await rep(update.message, f"🚫 Need at least <b>2 teams</b> ({cmd('addcap')}).")
    if a.team_count and len(a.teams) < a.team_count:
        return await rep(update.message,
            f"🚫 <b>{len(a.teams)}/{a.team_count}</b> teams added. Add the rest with {cmd('addcap')} "
            f"or lower the number: {cmd('editauc')} teams [number]")
    wants_demo = bool(ctx.args and ctx.args[0].lower() == "default")
    if not (a.reg_sets or a.custom_sets or wants_demo):
        return await rep(update.message,
            f"📦 Add the players first with {cmd('addset')} (manual or import registered players).")
    step = next_pre(a)
    if step:  # purse / base price / max / min are asked here, then the auction starts by itself
        a.start_after_wiz, a.start_args, a.wiz_user = True, list(ctx.args), update.effective_user.id
        return await wiz_ask(ctx.bot, chat_id, a, step)
    await proceed_start(ctx, chat_id, list(ctx.args))


async def proceed_start(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, args: list):
    """Pick the player pool and begin (all checks were done by startauction)."""
    a = AUCTIONS.get(chat_id)
    if not a or a.status != "lobby":
        return
    avail = [k for k, v in (("registered", a.reg_sets), ("custom", a.custom_sets)) if v]
    pick = args[0].lower() if args else None
    if pick in ("registered", "custom") and pick not in avail:
        return await say(ctx.bot, chat_id, f"🚫 No <b>{pick}</b> players yet.")
    if pick not in ("registered", "custom", "default"):
        if len(avail) == 1:
            pick = avail[0]
        elif not avail:
            return await say(ctx.bot, chat_id, f"📦 Add the players first with {cmd('addset')}.")
        else:  # both exist: let the host choose
            reg_n = sum(s["total"] for s in a.reg_sets)
            cus_n = sum(s["total"] for s in a.custom_sets)
            rows = [[btn(f"📋 registered players ({reg_n})", callback_data="src:registered")],
                    [btn(f"🧩 my added sets ({cus_n})", callback_data="src:custom")]]
            return await say(ctx.bot, chat_id,
                "🎯 <b>CHOOSE THE PLAYER POOL</b>\nYou have both registered players and added sets. "
                "Which one should I use?", reply_markup=Markup(rows))
    await begin_auction(ctx, chat_id, pick)


async def on_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    a = AUCTIONS.get(chat_id)
    if not a or a.status != "lobby":
        return await q.answer(sc("⌛ too late."), show_alert=True)
    if not is_host(a, q.from_user.id):
        return await q.answer(sc("🚫 only the host can choose."), show_alert=True)
    pick = q.data.split(":")[1]
    if (pick == "registered" and not a.reg_sets) or (pick == "custom" and not a.custom_sets):
        return await q.answer(sc("🚫 nothing saved for that option."), show_alert=True)
    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await begin_auction(ctx, chat_id, pick)


async def begin_auction(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, source: str):
    a = AUCTIONS.get(chat_id)
    if not a or a.status != "lobby":
        return
    if source == "registered":
        sets = copy.deepcopy(a.reg_sets)
    elif source == "custom":
        sets = copy.deepcopy(a.custom_sets)
    else:
        sets = build_sets(load_fallback_players(), a.set_size)
    if a.set_order and sorted(a.set_order) == list(range(1, len(sets) + 1)):
        sets = [sets[i - 1] for i in a.set_order]  # the order chosen in /next
    for st in sets:  # players without their own base price get the auctioneer's default
        for p in st["queue"]:
            p["base"] = p.get("base") or a.default_base
    total, need = sum(s["total"] for s in sets), len(a.teams) * (a.min_players or a.squad_size)
    if total < need:
        return await say(ctx.bot, chat_id,
            f"🚫 Only <b>{total}</b> players but <b>{need}</b> needed "
            f"({len(a.teams)} teams × {a.min_players or a.squad_size} minimum).\n🧢 Add more players!")
    a.sets, a.status, a.set_idx = sets, "running", 0
    label = {"registered": "📋 Registered players", "custom": "🧩 Added sets",
             "default": "🎲 Default pool"}[source]
    await send_media(ctx.bot, chat_id, "intro" if find_media("intro", chat_id) else "live",
        "🔔 <b>THE AUCTION BEGINS!</b> 🎉\n" + (f"🏷 <b>{nm(a.title)}</b>\n" if a.title else "") +
        f"🏟 Teams: <b>{len(a.teams)}</b> · 🧢 Players: <b>{total}</b> · 📦 Sets: <b>{len(sets)}</b>\n"
        f"💰 Purse: <b>{a.budget}</b> pts · 👥 Players: <b>{a.min_players or a.squad_size}–{a.squad_size}</b>\n"
        f"⏱ Timer: <b>{a.bid_timer}s</b> · ⏳ Gap: <b>{a.gap}s</b>\n"
        f"🎯 Pool: <b>{label}</b>\n\n📌 The first set is posted and pinned in 3 seconds — "
        f"bidding starts <b>{a.set_wait}s</b> after that.")
    ctx.job_queue.run_once(nominate, 3, chat_id=chat_id, name=f"next-{chat_id}")


async def auctions_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner only: list every running auction."""
    if update.effective_user.id not in OWNER_IDS:
        return
    if not AUCTIONS:
        return await rep(update.message, "📭 No active auctions.")
    lines = ["👑 <b>ACTIVE AUCTIONS</b>"]
    for cid, a in AUCTIONS.items():
        lines.append(f"• <code>{cid}</code> · {a.status} · 💼 {len(a.teams)} · "
                     f"🧢 {len(a.players)} · 🧩 {sum(s['total'] for s in a.custom_sets)}")
    await rep(update.message, "\n".join(lines))


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    a = AUCTIONS.get(chat_id)
    if a and is_host(a, update.effective_user.id):
        for n in ("lot", "next", "reg"):
            cancel_jobs(ctx, f"{n}-{chat_id}")
        del AUCTIONS[chat_id]
        await rep(update.message, "🛑 <b>Auction cancelled.</b>")


def render_team(a: Auction, t: Team) -> str:
    rows = [f"   {ROLE_EMOJI[p['role']]} <b>{pname(p)}</b> · 💸 {price}" for p, price in t.squad]
    return (f"🏟 <b>{nm(t.name)}</b>\n{people_line(t)}\n"
            f"💰 Left: <b>{t.budget}</b> · 💸 Spent: <b>{a.budget - t.budget}</b>"
            f" · 👥 {len(t.squad)}/{a.squad_size}\n" + ("\n".join(rows) or "   <i>no players yet</i>"))


async def team(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Your team (manager or vice-captain). The host can pass a team name: /team Mumbai_Indians."""
    a = AUCTIONS.get(update.effective_chat.id)
    if not a:
        return await rep(update.message, "🤷 No auction here.")
    t, _ = find_team(a, update.effective_user)
    if ctx.args and is_host(a, update.effective_user.id):
        t = a.teams.get(tkey(" ".join(ctx.args))) or t
    if not t:
        return await rep(update.message, "🚫 You're not a manager or vice-captain in this auction.")
    await rep(update.message, render_team(a, t))


async def purse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = AUCTIONS.get(update.effective_chat.id)
    if not a:
        return await rep(update.message, "🤷 No auction here.")
    lines = [f"🏟 <b>{nm(t.name)}</b> — 💰 {t.budget} · 👥 {len(t.squad)}/{a.squad_size}"
             for t in a.teams.values()]
    await rep(update.message, "💰 <b>PURSES</b>\n" + "\n".join(lines))


# ======================================================================
#  DM: player registration
# ======================================================================
# ---------- tournament group + channel (set by the owner; players must join before registering) ----------
TCFG_FILE = DATA / "auction_tournament.json"


def _load_tcfg() -> dict:
    d = _db().load_json("auction_tournament", {})
    return d if isinstance(d, dict) else {}


TCFG: dict = _load_tcfg()  # {"group": {"id","title","link"}, "channel": {...}}


def _save_tcfg():
    _db().save_json("auction_tournament", TCFG)


async def _resolve_chat(bot, ref: str):
    ref = ref.strip()
    m = re.match(r"^(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,})/?$", ref)
    if m:
        ref = "@" + m.group(1)
    elif re.fullmatch(r"-?\d{5,}", ref):
        ref = int(ref)
    elif re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", ref):
        ref = "@" + ref.lstrip("@")
    else:
        return None
    try:
        return await bot.get_chat(ref)
    except Exception as e:
        log.info("get_chat(%s) failed: %s", ref, e)
        return None


def join_status() -> str:
    rows = []
    for kind, icon in (("group", "👥 Group"), ("channel", "📢 Channel")):
        e = TCFG.get(kind)
        rows.append(f"{icon}: " + (f"<b>{nm(e['title'])}</b>" if e else "<i>not set</i>"))
    return "\n".join(rows)


async def _set_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE, kind: str):
    """/setaucgroup | /setaucchannel  [@username | chat_id | t.me/username] [invite_link]  (owner only)."""
    msg, chat = update.message, update.effective_chat
    me = {"group": "setaucgroup", "channel": "setaucchannel", "log": "setlog"}[kind]
    if not is_owner(update.effective_user.id):
        return await rep(msg, "🚫 Only the <b>owner</b> can set the tournament " + kind + ".")
    args = ctx.args
    if args and args[0].lower() in ("off", "remove", "clear", "none"):
        TCFG.pop(kind, None)
        _save_tcfg()
        return await rep(msg, f"🗑 Tournament {kind} removed — players won't be asked to join it.")
    if not args:
        if kind in ("group", "log") and chat.type != "private":
            ref = str(chat.id)  # sent inside the group itself: use this chat
        else:
            return await rep(msg,
                f"🔗 <b>TOURNAMENT {kind.upper()}</b>\n{join_status()}\n\n"
                f"Set it: {cmd(me)} [@username or chat_id]"
                + (f"\n💡 Or send {cmd(me)} inside the group itself." if kind == "group" else "")
                + f"\nPrivate {kind}? Add the id, then the invite link: {cmd(me)} -1001234567890 https://t.me/+abc\n"
                f"🗑 Remove: {cmd(me)} off\n\n"
                "⚠️ Add me to it first (as <b>admin</b> for a channel) so I can check who has joined.")
    else:
        ref = args[0]
    c = await _resolve_chat(ctx.bot, ref)
    if c is None:
        return await rep(msg,
            "🚫 I couldn't find that chat. Add me to it first (admin for a channel), then use its "
            f"@username or numeric id, e.g. {nm('-1001234567890')}.\n"
            "Private invite links can't be looked up — send the id, then the link as a second value.")
    ok_types = {"group": ("group", "supergroup"), "channel": ("channel",),
                "log": ("channel", "group", "supergroup")}[kind]
    if c.type not in ok_types:
        return await rep(msg, f"🚫 That is a <b>{c.type}</b>, not a {kind}.")
    try:  # I must be in there to see members
        st = (await ctx.bot.get_chat_member(c.id, ctx.bot.id)).status
    except Exception:
        st = None
    if kind == "log":
        TCFG["log"] = {"id": c.id, "title": c.title or str(c.id), "link": ""}
        _save_tcfg()
        try:
            await ctx.bot.send_message(c.id, fancy("✅ <b>Registration log connected.</b> New player registrations will appear here."), parse_mode=ParseMode.HTML)
        except Exception as ex:
            TCFG.pop("log", None)
            _save_tcfg()
            return await rep(msg, f"🚫 I can't post in <b>{nm(c.title)}</b> ({nm(ex)}). Make me an admin there and try again.")
        return await rep(msg, f"✅ Log channel set to <b>{nm(c.title)}</b>.")
    if st not in ("member", "administrator", "creator"):
        return await rep(msg, f"🚫 I'm not in <b>{nm(c.title)}</b>. Add me there "
                              f"({'as admin' if kind == 'channel' else 'as a member or admin'}), then try again.")
    link = next((x for x in args[1:] if x.startswith("http")), "")
    if not link:
        link = f"https://t.me/{c.username}" if c.username else (getattr(c, "invite_link", "") or "")
    TCFG[kind] = {"id": c.id, "title": c.title or str(c.id), "link": link}
    _save_tcfg()
    warn = ""
    if kind == "channel" and st != "administrator":
        warn += "\n⚠️ Make me an <b>admin</b> of the channel, otherwise I can't see its members."
    if not link:
        warn += (f"\n⚠️ No join link found. Add one: {cmd(me)} {c.id} https://t.me/+abc")
    await rep(msg, f"✅ Tournament {kind} set to <b>{nm(c.title)}</b>.\n\n{join_status()}\n"
                   f"👥 Players must join before they can register.{warn}")


async def setgroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _set_join(update, ctx, "group")


async def setchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _set_join(update, ctx, "channel")


async def setlog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _set_join(update, ctx, "log")


def log_chat_id():
    """Log channel: /setlog (saved) or LOG_CHANNEL_ID in .env."""
    e = TCFG.get("log")
    if e:
        return e["id"]
    v = os.getenv("LOG_CHANNEL_ID", "").strip()
    return int(v) if re.fullmatch(r"-?\d+", v) else (v or None)


async def send_reg_log(bot, a, user, r, updated: bool):
    cid = log_chat_id()
    if not cid:
        log.warning("registration not logged: no log channel (use /setlog or LOG_CHANNEL_ID)")
        return
    un = f"@{r['username']}" if r["username"] else "—"
    text = (("🔁 <b>PLAYER REGISTRATION UPDATED</b>" if updated else "🆕 <b>NEW PLAYER REGISTRATION</b>") + "\n\n"
            f"🏆 Tournament: <b>{nm(a.title or TOURNAMENT)}</b>\n"
            f"👤 Username: <b>{nm(un)}</b>\n"
            f"🧢 Name: <b>{nm(r['name'])}</b>\n"
            f"🆔 Telegram ID: {nm(user.id)}\n"
            f"👥 Total registered: <b>{len(a.players)}</b>\n"
            f"🕒 {nm(time.strftime('%d %b %Y, %I:%M %p'))}")
    try:
        await say(bot, cid, text)
    except Exception as e:
        log.warning("could not post to log channel %s: %s", cid, e)


async def missing_joins(bot, user_id: int, a: Auction) -> list:
    """[(kind, entry, 'no' | 'error')] for each required chat the user is not in (or can't be checked)."""
    out = []
    for kind, e in (("group", a.reg_group), ("channel", a.reg_channel)):
        if not e:
            continue
        try:
            m = await bot.get_chat_member(e["id"], user_id)
            ok = m.status in ("member", "administrator", "creator") or \
                 (m.status == "restricted" and getattr(m, "is_member", False))
            state = None if ok else "no"
        except Exception as ex:
            log.warning("membership check of %s in %s failed: %s", user_id, e["id"], ex)
            state = "error"
        if state:
            out.append((kind, e, state))
    return out


async def ask_join(message, chat_id: int):
    """Step 1: Join Channel + Join Group buttons and a Verify Membership button."""
    a = AUCTIONS[chat_id]
    bot = message.get_bot()
    rows = []
    for e, icon, word in ((a.reg_channel, "📢", "tournament channel"), (a.reg_group, "👥", "tournament group")):
        if not e:
            continue
        if not e.get("link"):  # no stored link: try to make one now
            try:
                e["link"] = await bot.export_chat_invite_link(e["id"])
            except Exception:
                e["link"] = ""
        if e["link"]:
            rows.append([Btn(f"{icon} " + sc(f"join {word}"), url=e["link"])])
    rows.append([Btn("✅ " + sc("verify membership"), callback_data=f"reg:check:{chat_id}")])
    what = " and ".join(w for e, w in ((a.reg_channel, "channel"), (a.reg_group, "group")) if e)
    both = "BOTH the" if a.reg_group and a.reg_channel else "the"
    await rep(message, "🔐 <b>MANDATORY STEP</b>\n"
                       f"<b>1/{REG_STEPS}</b> · <b>Member Verification</b>\n{DIV}\n"
                       f"<blockquote>Before registration, join <b>{both}</b> official <b>{nm(a.title)}</b> "
                       f"tournament <b>{what}</b>. Then tap <b>Verify Membership</b>.</blockquote>",
              reply_markup=Markup(rows))


async def reg_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Verify Membership button: re-check, then carry on with the registration."""
    q = update.callback_query
    try:
        chat_id = int(q.data.split(":")[2])
    except (IndexError, ValueError):
        await q.answer()
        return ConversationHandler.END
    a = AUCTIONS.get(chat_id)
    if not a or not a.reg_open:
        await q.answer(sc("🔒 registration is closed."), show_alert=True)
        return ConversationHandler.END
    if await missing_joins(ctx.bot, q.from_user.id, a):
        await q.answer(sc("❌ you haven't joined yet. join, then tap verify again."), show_alert=True)
        return ConversationHandler.END
    await q.answer(sc("✅ verified"))
    try:
        await q.edit_message_text(fancy(f"<b>1/{REG_STEPS}</b> 🔐 Membership: <b>Verified</b> ✅"), parse_mode=ParseMode.HTML)
    except Exception:
        pass
    return await begin_reg(update, ctx, chat_id, checked=True)


USERNAME, NAME, ROLE = range(3)
REG_STEPS = 3


async def private_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/start in the bot's DM: welcome + the Player Registration button."""
    if ctx.args and ctx.args[0].startswith("reg_"):  # the "register in dm" link from a group
        try:
            return await begin_reg(update, ctx, int(ctx.args[0][4:]))
        except ValueError:
            pass
    await send_media(ctx.bot, update.effective_chat.id, "welcome",
        f"🏏 <b>{nm(BRAND)}</b> 🏆\n🏆 <b>{nm(TOURNAMENT)}</b>\n\n"
        "🧢 <b>Cricketers:</b> tap <b>Player Registration</b> below to sign up for an auction.\n"
        f"👑 <b>Hosts:</b> add me to your group and send {cmd('start')} there to set up the auction "
        "(sudo access needed — ask the owner).\n"
        f"📖 {cmd('help')} — all commands",
        reply_markup=Markup([[Btn("📝 " + sc("player registration"), callback_data="reg:start")]]))
    return ConversationHandler.END


async def register_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/register and the Player Registration button: join the auction whose registration is open."""
    q = update.callback_query
    if q:
        await q.answer()
    msg = update.effective_message
    open_chats = [c for c, a in AUCTIONS.items() if a.reg_open]
    if not open_chats:
        await rep(msg, "🔒 <b>No registration is open right now.</b>\n"
                       "Ask your host to open it, then tap <b>Player Registration</b> again 🙏")
        return ConversationHandler.END
    last = ctx.user_data.get("reg_chat")
    if len(open_chats) == 1 or (last in open_chats and not q):
        return await begin_reg(update, ctx, open_chats[0] if len(open_chats) == 1 else last)
    rows = []
    for c in open_chats:  # several auctions are taking players: let the player choose
        try:
            label = (await ctx.bot.get_chat(c)).title
        except Exception:
            label = AUCTIONS[c].title
        rows.append([Btn("🏏 " + (label or f"Auction {c}")[:40], callback_data=f"reg:pick:{c}")])
    await rep(msg, "🏏 <b>Which auction do you want to join?</b>", reply_markup=Markup(rows))
    return ConversationHandler.END


async def reg_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        chat_id = int(q.data.split(":")[2])
    except (IndexError, ValueError):
        return ConversationHandler.END
    return await begin_reg(update, ctx, chat_id)


async def begin_reg(update: Update, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, checked: bool = False):
    msg = update.effective_message
    a = AUCTIONS.get(chat_id)
    if not a or not a.reg_open:
        await rep(msg, "🔒 Registration isn't open for that auction.")
        return ConversationHandler.END
    user = update.effective_user
    if not checked and (a.reg_group or a.reg_channel):  # step 1: join + verify
        await ask_join(msg, chat_id)
        return ConversationHandler.END
    ctx.user_data["reg_chat"], ctx.user_data["reg"] = chat_id, {}
    old = a.players.get(user.id)
    prefix = (f"🔁 You're already registered as <b>{nm(old['name'])}</b>. "
              "Continue to update your details.\n\n" if old else "")
    await send_media(ctx.bot, user.id, "register",
                     "📝 <b>PLAYER REGISTRATION</b> 🏏" + (f"\n🏆 <b>{nm(a.title)}</b>" if a.title else ""),
                     media_chat=chat_id)
    return await ask_name(msg, user, prefix)


async def ask_name(message, user, prefix: str = ""):
    """Step 2: the name shown in the auction."""
    await rep(message, prefix + f"<b>2/{REG_STEPS}</b> 🧢 What <b>name</b> should appear in the auction?\n"
                                f"Type it, or tap below. ({cmd('cancelreg')} to stop)",
              reply_markup=Markup([[Btn("✅ " + sc("use") + f' "{user.full_name}"',
                                        callback_data="name_default")]]))
    return NAME


async def ask_username(message, user):
    """Step 3: the Telegram username."""
    if user.username:
        how = (f"Type it (e.g. {nm('@' + user.username)}) or tap below.")
        kb = Markup([[Btn("✅ " + sc("use") + f" @{user.username}", callback_data="un_default")]])
    else:
        how = "You don't seem to have one — send <b>-</b> to continue without."
        kb = None
    await rep(message, f"<b>3/{REG_STEPS}</b> 👤 What's your Telegram <b>username</b>?\n" + how, reply_markup=kb)
    return USERNAME


async def name_default(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.setdefault("reg", {})["name"] = q.from_user.full_name
    await q.edit_message_text(fancy(f"<b>2/{REG_STEPS}</b> 🧢 Name: <b>{nm(q.from_user.full_name)}</b> ✅"), parse_mode=ParseMode.HTML)
    return await ask_username(q.message, q.from_user)


async def name_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()[:40]
    ctx.user_data.setdefault("reg", {})["name"] = name
    await rep(update.message, f"🧢 Name: <b>{nm(name)}</b> ✅")
    return await ask_username(update.message, update.effective_user)


async def username_default(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    un = q.from_user.username or ""
    ctx.user_data.setdefault("reg", {})["username"] = un
    await q.edit_message_text(fancy(f"<b>3/{REG_STEPS}</b> 👤 Username: <b>{nm('@' + un) if un else '—'}</b> ✅"), parse_mode=ParseMode.HTML)
    return await finish_reg(update, ctx)


async def username_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lstrip("@")
    if raw.lower() in NO_USER:
        un = ""
    elif USER_RE.match(raw):
        un = raw
    else:
        await rep(update.message, "🚫 A username uses only letters, numbers and _ (3-32 characters).\n"
                                  "Try again, or send <b>-</b> if you have none.")
        return USERNAME
    ctx.user_data.setdefault("reg", {})["username"] = un
    await rep(update.message, f"👤 Username: <b>{nm('@' + un) if un else '—'}</b> ✅")
    return await finish_reg(update, ctx)


async def finish_reg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    a = AUCTIONS.get(ctx.user_data.get("reg_chat"))
    if not a or not a.reg_open:
        await rep(msg, "🔒 Registration has closed, nothing was saved.")
        return ConversationHandler.END
    r, user = ctx.user_data.get("reg", {}), update.effective_user
    if not all(k in r for k in ("username", "name")):
        await rep(msg, "⚠️ Something went wrong, please send /register again.")
        return ConversationHandler.END
    r["role"] = "PL"  # no role question any more
    updated = user.id in a.players
    # no base price is asked: the auctioneer's default base price applies at the auction
    a.players[user.id] = {"uid": user.id, "username": r["username"], "name": r["name"], "role": "PL",
                          "base": None, "note": ""}
    await rep(msg,
        "✅ <b>REGISTRATION DONE!</b> 🎉\n"
        f"🏆 <b>{nm(a.title)}</b>\n{DIV}\n"
        f"<blockquote>🧢 Name ➳ <b>{nm(r['name'])}</b>\n"
        f"👤 Username ➳ <b>{nm('@' + r['username']) if r['username'] else '—'}</b></blockquote>\n"
        "📦 When registration closes you'll be placed in a random set and I'll DM you here.\n"
        f"🔁 Send {cmd('register')} again to edit your details.")
    save_auctions()
    await send_reg_log(ctx.bot, a, user, r, updated)
    return ConversationHandler.END


async def cancelreg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await rep(update.message, "🛑 Registration cancelled (nothing saved).")
    return ConversationHandler.END


# ======================================================================
#  AUCTION FLOW
# ======================================================================
def lot_text(a: Auction) -> str:
    """Premium player card: bold headings + quoted blocks (the profile picture is attached above it)."""
    p, s = a.current, a.sets[a.set_idx]
    lot_no = s["total"] - len(s["queue"])
    who = ((f"<b>{nm(a.teams[a.bidder].name)}</b>"
            + (f" · 🙋 <b>{nm(a.bidder_by)}</b>" if a.bidder_by else ""))
           if a.bidder else "<b>No bids yet</b>")
    role = "" if p["role"] == "PL" else f" · <b>{ROLE_PLAIN[p['role']]}</b>"
    note = f"<blockquote><i>{nm(p['note'])}</i></blockquote>\n" if p.get("note") else ""
    return (f"🔨 <b>PLAYER UP FOR AUCTION</b> · <b>LOT {lot_no}/{s['total']}</b>\n{DIV}\n"
            f"<blockquote>{ROLE_EMOJI[p['role']]} <b>{ment(p)}</b>{role}</blockquote>\n"
            f"{note}"
            f"<blockquote>Base Price ➳ <b>{p['base']}</b>\n"
            f"Current Bid ➳ <b>{a.bid or '—'}</b>\n"
            f"Leading ➳ {who}</blockquote>\n{DIV}\n"
            f"📦 <b>{set_label(s, a.set_idx)}</b>\n"
            f"<blockquote>⌨️ Bid with <b>{cmd('bid')} [points]</b>\n"
            f"⏱ Sold <b>{a.bid_timer}s</b> after the last bid</blockquote>")


async def refresh_lot(bot, chat_id, a: Auction):
    text, kb = fancy(lot_text(a)), None  # no buttons: bids come from /bid [points]
    try:
        if a.lot_media:
            await bot.edit_message_caption(chat_id=chat_id, message_id=a.msg_id,
                                           caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=a.msg_id,
                                        parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        log.debug("lot edit skipped: %s", e)


async def player_photo(bot, a: Auction, p: dict):
    """file_id of the player's Telegram profile picture, or None (no picture / hidden by privacy)."""
    uid = p.get("uid")
    if not uid:
        return None
    if uid not in a.photo_ids:
        fid = ""
        try:
            ph = await bot.get_user_profile_photos(uid, limit=1)
            if ph.total_count and ph.photos:
                fid = ph.photos[0][-1].file_id
        except Exception as e:
            log.info("profile photo of %s not available: %s", uid, e)
        a.photo_ids[uid] = fid
    return a.photo_ids[uid] or None


async def small_photo(bot, file_id: str, size: int = 320):
    """Shrink the profile picture so the player card stays small in the chat (needs Pillow)."""
    try:
        from PIL import Image
        f = await bot.get_file(file_id)
        raw = await f.download_as_bytearray()
        im = Image.open(io.BytesIO(bytes(raw))).convert("RGB")
        im.thumbnail((size, size))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        out.seek(0)
        out.name = "player.jpg"
        return out
    except Exception as e:  # no Pillow / download failed: the normal picture is used
        log.info("small photo skipped: %s", e)
        return None


def fmt_wait(sec: int) -> str:
    if sec and sec % 60 == 0:
        m = sec // 60
        return f"{m} minute" + ("s" if m != 1 else "")
    return f"{sec} seconds"


async def announce_set(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, a: Auction, s: dict):
    """Post the FULL set (every player) and pin it."""
    lines = [f"📌 <b>{set_label(s, a.set_idx).upper()}</b> · <b>{a.set_idx + 1}/{len(a.sets)}</b>", DIV,
             f"<blockquote>🧢 Players ➳ <b>{s['total']}</b>\n"
             f"⏱ Bidding starts in ➳ <b>{fmt_wait(a.set_wait)}</b></blockquote>", DIV,
             "<blockquote>"]
    for n, p in enumerate(s["queue"], 1):
        lines.append(f"<b>{n}.</b> {ROLE_EMOJI[p['role']]} <b>{ment(p)}</b> · Base <b>{p.get('base') or a.default_base}</b>")
    lines.append("</blockquote>")
    text = "\n".join(lines)
    if len(text) <= 900 and find_media("live", chat_id):
        msg, _ = await send_media(ctx.bot, chat_id, "live", text)
    else:
        msg = None
        for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
            m = await say(ctx.bot, chat_id, chunk)
            msg = msg or m
    if a.pin_id:  # drop the previous set's pin
        try:
            await ctx.bot.unpin_chat_message(chat_id, a.pin_id)
        except Exception:
            pass
        a.pin_id = None
    try:
        await ctx.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=False)
        a.pin_id = msg.message_id
    except Exception as e:
        log.info("pin failed: %s", e)
        await say(ctx.bot, chat_id, "⚠️ I couldn't pin the set — make me an admin with the <b>Pin messages</b> permission.")


async def nominate(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    a = AUCTIONS.get(chat_id)
    if not a or a.status != "running":
        return
    open_teams = [t for t in a.teams.values() if len(t.squad) < a.squad_size]
    while a.set_idx < len(a.sets) and not a.sets[a.set_idx]["queue"]:
        a.set_idx += 1
    if a.set_idx >= len(a.sets) or not open_teams:
        return await finish(ctx, chat_id)
    s = a.sets[a.set_idx]
    if not s["announced"]:  # new set: post + pin the full list, bidding starts after the pause
        s["announced"] = True
        await announce_set(ctx, chat_id, a, s)
        ctx.job_queue.run_once(nominate, a.set_wait, chat_id=chat_id, name=f"next-{chat_id}")
        return
    a.current, a.bid, a.bidder, a.bidder_by = s["queue"].pop(0), 0, None, ""
    a.current["set_name"] = s["name"]
    p = a.current
    # picture order: the player's Telegram profile picture -> the common picture set with
    # /addfile player -> a generated lot card -> plain text
    attempts = []
    photo = await player_photo(ctx.bot, a, p)
    if photo:
        attempts.append(("lot", await small_photo(ctx.bot, photo) or photo))
    if find_media("player", chat_id):
        attempts.append(("player", None))
    if lot_card:
        try:
            attempts.append(("lot", lot_card(p["name"], ROLE_PLAIN[p["role"]], p["base"], s["name"],
                                             s["total"] - len(s["queue"]), s["total"],
                                             username=p.get("username", ""))))
        except Exception as e:
            log.warning("lot card failed: %s", e)
    msg, a.lot_media = None, False
    for key, src in attempts:
        msg, a.lot_media = await send_media(ctx.bot, chat_id, key, lot_text(a),
                                            source=src, strict=True)
        if msg:
            break
    if not msg:
        msg, a.lot_media = await send_media(ctx.bot, chat_id, "lot", lot_text(a))
    a.msg_id = msg.message_id
    arm_lot(ctx, chat_id, a)


# (seconds left, call) - after a bid the bot counts the lot down like a real auctioneer
COUNTDOWN = [
    (10, "⏳ <b>10 SECONDS LEFT!</b>"),
    (3, "🔨 <b>GOING ONCE!</b>"),
    (2, "🔨🔨 <b>GOING TWICE!</b>"),
    (1, "🔨🔨🔨 <b>GOING THRICE!</b>"),
]


def arm_lot(ctx, chat_id: int, a: Auction):
    """(Re)start the lot clock. With a bid on the table the countdown calls are scheduled too
    (10s left, going once / twice / thrice at 3 / 2 / 1s), then close_lot sells the player."""
    cancel_jobs(ctx, f"lot-{chat_id}")
    total = a.bid_timer
    if a.bidder:
        for left, _ in COUNTDOWN:
            if total - left > 0:  # short timers simply skip the calls that don't fit
                ctx.job_queue.run_once(lot_call, total - left, chat_id=chat_id,
                                       name=f"lot-{chat_id}", data=left)
    ctx.job_queue.run_once(close_lot, total, chat_id=chat_id, name=f"lot-{chat_id}")


async def lot_call(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    a = AUCTIONS.get(chat_id)
    if not a or not a.current or not a.bidder:
        return
    text = dict(COUNTDOWN)[ctx.job.data]
    await say(ctx.bot, chat_id,
              f"{text}\n<blockquote>{ROLE_EMOJI[a.current['role']]} <b>{pname(a.current)}</b>\n"
              f"💰 Bid ➳ <b>{a.bid}</b> · 👑 <b>{nm(a.teams[a.bidder].name)}</b></blockquote>")


async def bid_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/bid [points] - a team manager / vice-captain bids on the player that is up."""
    msg, user, chat = update.message, update.effective_user, update.effective_chat
    if not msg or chat.type == "private":
        return await rep(msg, "🚫 <b>Use /bid inside the auction group.</b>")
    chat_id = chat.id
    a = AUCTIONS.get(chat_id)
    if not a or a.status != "running" or not a.current:
        return await rep(msg, "⌛ <b>No player is up for bidding right now.</b>")
    amount = ctx.args[0] if ctx.args else ""
    if not amount.isdigit():
        return await rep(msg, f"⌨️ <b>Usage:</b> {cmd('bid')} [points]  e.g. {cmd('bid')} 5")
    amount = int(amount)
    remember_user(chat_id, user)
    t, _role = find_team(a, user)  # manager OR vice-captain can bid for the team
    if not t:
        return await rep(msg, "🚫 <b>Only team managers and vice-captains can bid.</b>")
    if len(t.squad) >= a.squad_size:
        return await rep(msg, "✅ <b>Your squad is full.</b>")
    if tkey(t.name) == a.bidder:
        return await rep(msg, "👑 <b>Your team already has the top bid.</b>")
    floor = a.current["base"] if not a.bidder else a.bid + 1
    if amount < floor:
        return await rep(msg, f"⚡ <b>Your bid must be at least {floor}.</b>")
    if amount > max_bid(a, t):
        return await rep(msg, f"💸 <b>Your max bid is {max_bid(a, t)}</b> "
                              "(you must afford a full squad).")

    a.bid, a.bidder, a.bidder_by = amount, tkey(t.name), user.full_name
    arm_lot(ctx, chat_id, a)
    # uses the picture / GIF saved with /addfile aucbid (plain text if none is saved)
    await send_media(ctx.bot, chat_id, "bid", bid_text(a.current, t, user, amount))
    await refresh_lot(ctx.bot, chat_id, a)


async def notify_sold(bot, a: Auction, p: dict, t: Team):
    """DM the player that he was sold (silently skipped if he never started the bot)."""
    if not p.get("uid"):
        return
    cap = (f"@{t.manager_username}" if t.manager_username else t.manager_name) or "—"
    try:
        await say(bot, p["uid"],
                  f"🎉 <b>YOU'VE BEEN SOLD!</b> 🔨\n{DIV}\n"
                  f"<blockquote>You are sold in <b>{nm(a.title)}</b> to <b>{nm(t.name)}</b>\n"
                  f"👑 Captain ➳ <b>{nm(cap)}</b></blockquote>")
    except Exception as e:
        log.info("sold DM to %s failed: %s", p.get("uid"), e)


async def close_lot(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    a = AUCTIONS.get(chat_id)
    if not a or not a.current:
        return
    p, bidder, price = a.current, a.bidder, a.bid
    a.current = None  # late /bid commands are now rejected
    if bidder:
        t = a.teams[bidder]
        t.budget -= price
        t.squad.append((p, price))
        await send_media(ctx.bot, chat_id, "sold",
            f"🎉 <b>SOLD!</b> 🔨\n{DIV}\n"
            f"<blockquote>{ROLE_EMOJI[p['role']]} PLAYER ➳ <b>{ment(p)}</b>"
            f"{'' if p['role'] == 'PL' else ' (<b>' + ROLE_PLAIN[p['role']] + '</b>)'}\n"
            f"🏆 TEAM ➳ <b>{nm(t.name)}</b>"
            + (f"\n🎯 BIDDER ➳ <b>{nm(a.bidder_by)}</b>" if a.bidder_by else "") +
            f"</blockquote>\n{DIV}\n"
            f"<blockquote>💰 FINAL PRICE ➳ <b>{price}</b> pts\n"
            f"🏦 PURSE LEFT ➳ <b>{t.budget}</b></blockquote>")
        await notify_sold(ctx.bot, a, p, t)
    else:
        a.unsold.append(p)
        await send_media(ctx.bot, chat_id, "unsold",
            f"❌ <b>UNSOLD</b>\n{DIV}\n"
            f"<blockquote>{ROLE_EMOJI[p['role']]} PLAYER ➳ <b>{pname(p)}</b>\n"
            f"😔 <b>No team placed a bid.</b></blockquote>")
    ctx.job_queue.run_once(nominate, a.gap, chat_id=chat_id, name=f"next-{chat_id}")


# ======================================================================
#  LIVE CONTROLS: /addaucplayer  /skip  /resold  (host only, auction running)
# ======================================================================
def auction_players(a: Auction):
    """Every player the auction knows about: (state, player, team|None)."""
    for st in a.sets:
        for p in st["queue"]:
            yield "queued", p, None
    if a.current:
        yield "live", a.current, None
    for t in a.teams.values():
        for p, _price in t.squad:
            yield "sold", p, t
    for p in a.unsold:
        yield "unsold", p, None


def same_player(p: dict, username: str, uid) -> bool:
    if uid and p.get("uid") == uid:
        return True
    return bool(username) and (p.get("username") or "").lower() == username.lower()


def queue_at_end(a: Auction, p: dict):
    """Put a player at the very end of the auction (the last set's queue)."""
    last = a.sets[-1]
    last["queue"].append(p)
    last["total"] += 1


def live_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """(username, uid, full_name) from '@Username' or from the replied-to message, else None."""
    arg = next((x for x in (ctx.args or []) if not x.isdigit()), "")
    if arg:
        un = arg.lstrip("@").strip()
        return (un, None, "") if USER_RE.match(un) else None
    r = update.message.reply_to_message
    if r and r.from_user and not r.from_user.is_bot:
        u = r.from_user
        return (u.username or "", u.id, u.full_name)
    return None


async def live_host_auction(update: Update):
    """The running auction of this group, only for its host / the owner."""
    a = await group_auction(update)
    if not a:
        return None
    if not is_host(a, update.effective_user.id):
        await rep(update.message, "🚫 <b>Only the host can do that.</b>")
        return None
    if a.status != "running":
        await rep(update.message, "🚫 <b>This works only while the auction is running.</b>")
        return None
    return a


async def addaucplayer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/addaucplayer @Username [base] - add a player at the END of the running auction."""
    a = await live_host_auction(update)
    if not a:
        return
    msg, chat_id = update.message, update.effective_chat.id
    tgt = live_target(update, ctx)
    if not tgt:
        return await rep(msg, f"➕ <b>Usage:</b> {cmd('addaucplayer')} [@Username] [base]  "
                              "(or reply to the player's message)")
    un, uid, full = tgt
    base = next((int(x) for x in ctx.args if x.isdigit()), None)
    if base is not None and not 1 <= base <= a.budget:
        return await rep(msg, f"🚫 <b>Base price must be from 1 to {a.budget}.</b>")

    p = None
    for state, q, team in auction_players(a):
        if not same_player(q, un, uid):
            continue
        if state in ("queued", "live"):
            return await rep(msg, f"🚫 <b>{pname(q)}</b> is already in the auction "
                                  f"({'up for bidding now' if state == 'live' else 'still to come'}).")
        if state == "sold":
            return await rep(msg, f"🚫 <b>{pname(q)}</b> is already sold to <b>{nm(team.name)}</b>. "
                                  f"Use {cmd('resold')} to take the player back.")
        a.unsold.remove(q)  # unsold earlier: goes back into the auction
        p = q
        break
    if p is None:
        reg = next((r for r in a.players.values() if same_player(r, un, uid)), None)
        seen = SEEN.get(chat_id, {}).get(un.lower()) if un else None
        if reg:
            p = {"uid": reg.get("uid"), "username": reg.get("username", ""), "name": reg["name"],
                 "role": norm_role(reg), "note": ""}
        else:
            p = {"uid": uid or (seen[0] if seen else None), "username": un,
                 "name": full or (seen[1] if seen else un) or "Player", "role": "PL", "note": ""}
        p["id"] = 1 + max([q.get("id", 0) for _s, q, _t in auction_players(a)] + [0])
    p["base"] = base or p.get("base") or a.default_base
    queue_at_end(a, p)
    await rep(msg, f"➕ <b>PLAYER ADDED</b>\n{DIV}\n"
                   f"<blockquote>{ROLE_EMOJI[p['role']]} Player ➳ <b>{pname(p)}</b>\n"
                   f"Base Price ➳ <b>{p['base']}</b></blockquote>\n"
                   "📍 <b>Comes up at the end of the auction.</b>")


# ======================================================================
#  MANUAL REGISTRATION: /addplayerreg  /deregister  (host or sudo users, registration open)
# ======================================================================
async def reg_staff_auction(update: Update):
    """The auction of this group, only for its host / the owner / any sudo user, with registration open."""
    a = await group_auction(update)
    if not a:
        return None
    uid = update.effective_user.id
    if not (is_host(a, uid) or is_sudo(uid)):
        await rep(update.message, "🚫 Only the <b>host</b> or a <b>sudo user</b> can do that.")
        return None
    if a.status != "lobby":
        await rep(update.message, "🚫 The auction has already started.")
        return None
    if not a.reg_open:
        await rep(update.message, f"🔒 Registration isn't open. Use {cmd('openreg')} to open it first.")
        return None
    return a


async def addplayerreg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/addplayerreg [@Username] (or reply to the player's message) - register a player by hand."""
    a = await reg_staff_auction(update)
    if not a:
        return
    msg, chat_id = update.message, update.effective_chat.id
    tgt = live_target(update, ctx)
    if not tgt:
        return await rep(msg, f"➕ <b>Usage:</b> {cmd('addplayerreg')} [@Username]  "
                              "(or reply to the player's message)\n"
                              f"🗑 Remove: {cmd('deregister')} [@Username]")
    un, uid, full = tgt
    if not uid:
        seen = SEEN.get(chat_id, {}).get(un.lower())
        if not seen:
            return await rep(msg,
                f"🤷 I haven't seen {nm('@' + un)} in this group yet, so I can't tell who that is.\n"
                f"Ask them to send any message here, then try again, or reply to their message "
                f"with {cmd('addplayerreg')}.")
        uid, full = seen
    if uid in a.players or any(same_player(r, un, uid) for r in a.players.values()):
        return await rep(msg, f"🚫 {nm('@' + un) if un else nm(full)} is already registered.")
    r = {"username": un, "name": (full or un or "Player")[:40]}
    a.players[uid] = {"uid": uid, "username": r["username"], "name": r["name"], "role": "PL",
                      "base": None, "note": ""}
    save_auctions()
    await rep(msg, "✅ <b>PLAYER REGISTERED</b>\n"
                   f"{DIV}\n<blockquote>🧢 Name ➳ <b>{nm(r['name'])}</b>\n"
                   f"👤 Username ➳ <b>{nm('@' + un) if un else '—'}</b>\n"
                   f"👥 Total ➳ <b>{len(a.players)}</b></blockquote>")
    try:  # same registration log as a normal sign-up
        await send_reg_log(ctx.bot, a, type("U", (), {"id": uid})(), r, False)
    except Exception:
        pass


async def deregister(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/deregister [@Username] (or reply to the player's message) - remove a registered player."""
    a = await reg_staff_auction(update)
    if not a:
        return
    tgt = live_target(update, ctx)
    if not tgt:
        return await rep(update.message, f"🗑 <b>Usage:</b> {cmd('deregister')} [@Username]  "
                                         "(or reply to the player's message)")
    un, uid, full = tgt
    key = next((k for k, r in a.players.items() if same_player(r, un, uid)), None)
    if key is None:
        return await rep(update.message, f"🤷 {nm('@' + un) if un else nm(full)} isn't registered.")
    r = a.players.pop(key)
    save_auctions()
    await rep(update.message, "🗑 <b>PLAYER REMOVED</b>\n"
                              f"{DIV}\n<blockquote>🧢 <b>{nm(r['name'])}</b>"
                              + (f" ({nm('@' + r['username'])})" if r.get("username") else "")
                              + f"\n👥 Total ➳ <b>{len(a.players)}</b></blockquote>")


async def skip_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/skip - drop the player who is up for bidding (no sale, goes to the unsold list)."""
    a = await live_host_auction(update)
    if not a:
        return
    chat_id = update.effective_chat.id
    if not a.current:
        return await rep(update.message, "⌛ <b>No player is up for bidding right now.</b>")
    p, had_bid = a.current, bool(a.bidder)
    a.current = None  # late /bid commands are rejected from here on
    cancel_jobs(ctx, f"lot-{chat_id}")
    a.unsold.append(p)
    await rep(update.message,
              f"⏭ <b>SKIPPED</b>\n{DIV}\n"
              f"<blockquote>{ROLE_EMOJI[p['role']]} Player ➳ <b>{pname(p)}</b>"
              + ("\n🚫 <b>The bid was discarded.</b>" if had_bid else "") + "</blockquote>\n"
              f"📋 <b>Moved to the unsold list</b> · add the player back with {cmd('addaucplayer')}")
    ctx.job_queue.run_once(nominate, a.gap, chat_id=chat_id, name=f"next-{chat_id}")


async def resold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/resold @Username - take a sold player off his team, refund the price, auction him again."""
    a = await live_host_auction(update)
    if not a:
        return
    tgt = live_target(update, ctx)
    if not tgt:
        return await rep(update.message, f"♻️ <b>Usage:</b> {cmd('resold')} [@Username]  "
                                         "(or reply to the player's message)")
    un, uid, _full = tgt
    for t in a.teams.values():
        for i, (p, price) in enumerate(t.squad):
            if same_player(p, un, uid):
                t.squad.pop(i)
                t.budget += price
                queue_at_end(a, p)
                return await rep(update.message,
                    f"♻️ <b>RESOLD</b>\n{DIV}\n"
                    f"<blockquote>{ROLE_EMOJI[p['role']]} Player ➳ <b>{pname(p)}</b>\n"
                    f"🏆 Removed from ➳ <b>{nm(t.name)}</b></blockquote>\n"
                    f"<blockquote>💰 Refunded ➳ <b>{price}</b> pts\n"
                    f"🏦 Purse now ➳ <b>{t.budget}</b></blockquote>\n"
                    "📍 <b>Back in the auction, at the end.</b>")
    await rep(update.message, "🤷 <b>That player isn't in any team's squad.</b>")


# ======================================================================
#  SAVED SQUADS: every finished auction keeps its squads under the tournament name
# ======================================================================
SQUADS_FILE = DATA / "auction_squads.json"


def _load_squads() -> dict:
    d = _db().load_json("auction_squads", {})
    return d if isinstance(d, dict) else {}


SQUADS: dict = _load_squads()  # tournament id -> {"title", "chat_id", "saved", "teams": [...]}


def save_squads(chat_id: int, a: Auction) -> bool:
    """Store the final squads of `a` under its tournament name (same group + name = replaced)."""
    try:
        tid = next((k for k, v in SQUADS.items()
                    if v.get("chat_id") == chat_id and v.get("title", "").lower() == a.title.lower()), None)
        tid = tid or "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        SQUADS[tid] = {
            "title": a.title, "chat_id": chat_id, "saved": time.strftime("%d %b %Y"),
            "teams": [{"name": t.name, "captain": t.manager_username, "vc": t.vc_username,
                       "left": t.budget, "purse": a.budget, "max": a.squad_size,
                       "players": [{"name": p["name"], "username": p.get("username", ""),
                                    "price": price} for p, price in t.squad]}
                      for t in a.teams.values()]}
        _db().save_json("auction_squads", SQUADS)
        return True
    except Exception as e:  # never break the auction because of this
        log.warning("could not save squads: %s", e)
        return False


def _visible(chat) -> dict:
    """Tournaments this chat may see: its own; the owner in DM sees all."""
    return {k: v for k, v in SQUADS.items()
            if chat.type == "private" or v.get("chat_id") == chat.id}


def _close_row():
    return [Btn("✖ " + sc("close"), callback_data="sq:X")]


def squads_home(chat):
    tours = _visible(chat)
    if not tours:
        return "📭 No saved squads yet. They appear here after an auction ends.", None
    n = sum(len(v["teams"]) for v in tours.values())
    rows = [[Btn(f"🏆 {v['title']} ({len(v['teams'])})", callback_data=f"sq:T:{k}")]
            for k, v in tours.items()]
    rows.append(_close_row())
    return (f"📚 <b>SAVED SQUADS</b>\n{len(tours)} tournament(s) · {n} squads\n"
            "Pick a tournament to see its teams."), Markup(rows)


def squads_teams(tid: str):
    v = SQUADS.get(tid)
    if not v:
        return None, None
    rows = [[Btn(f"🏟 {t['name']} ({len(t['players'])})", callback_data=f"sq:M:{tid}:{i}")]
            for i, t in enumerate(v["teams"])]
    rows.append([Btn("⬅ " + sc("tournaments"), callback_data="sq:L"), *_close_row()])
    return (f"🏆 <b>{nm(v['title'])}</b>\n📅 Saved: {v['saved']} · 🏟 Teams: <b>{len(v['teams'])}</b>\n"
            "Pick a team to see its squad."), Markup(rows)


def squads_team(tid: str, i: int):
    v = SQUADS.get(tid)
    if not v or not 0 <= i < len(v["teams"]):
        return None, None
    t = v["teams"][i]
    rows_txt = [f"   {n}. <b>{nm(p['name'])}</b>" + (f" ({nm('@' + p['username'])})" if p["username"] else "")
                + f" · 💸 {p['price']}" for n, p in enumerate(t["players"], 1)]
    vc = nm("@" + t["vc"]) if t["vc"] else "—"
    text = (f"🏟 <b>{nm(t['name'])}</b> · {nm(v['title'])}\n"
            f"🧢 Captain: {nm('@' + t['captain'])} · 🎖 VC: {vc}\n"
            f"💰 Left: <b>{t['left']}</b> · 💸 Spent: <b>{t['purse'] - t['left']}</b> · "
            f"👥 {len(t['players'])}/{t['max']}\n" + ("\n".join(rows_txt) or "   <i>no players</i>"))
    return text, Markup([[Btn("⬅ " + sc("teams"), callback_data=f"sq:T:{tid}"), *_close_row()]])


async def squads_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = squads_home(update.effective_chat)
    await rep(update.message, text, reply_markup=kb)


async def squads_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    await q.answer()
    if parts[1] == "X":
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    if parts[1] == "L":
        text, kb = squads_home(q.message.chat)
    elif parts[1] == "T":
        text, kb = squads_teams(parts[2])
    else:
        text, kb = squads_team(parts[2], int(parts[3]))
    if not text:
        text, kb = "🤷 That squad isn't saved any more.", Markup([_close_row()])
    try:
        await q.edit_message_text(fancy(text), parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        pass


async def finish(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, early: bool = False):
    a = AUCTIONS[chat_id]
    a.status, a.ended_early = "finished", early
    if a.pin_id:
        try:
            await ctx.bot.unpin_chat_message(chat_id, a.pin_id)
        except Exception:
            pass
        a.pin_id = None
    saved = save_squads(chat_id, a)
    await send_media(ctx.bot, chat_id, "trophy",
                     ("🛑 <b>AUCTION ENDED BY THE AUCTIONEER!</b> 🏆" if early
                      else "🏁 <b>AUCTION COMPLETE!</b> 🏆") + "\nHere are the final squads 👇")
    await send_long(ctx.bot, chat_id, "\n\n".join(render_team(a, t) for t in a.teams.values()))
    if saved:
        await say(ctx.bot, chat_id, f"💾 Squads saved under <b>{nm(a.title)}</b> — view them any time with {cmd('squads')}")
    if early:
        await deliver_reports(ctx, chat_id)
    else:
        await say(ctx.bot, chat_id,
                  f"📄 Host: send {cmd('end_auction')} to get the squad PDFs and the unsold list.")


def settle_current(a: Auction):
    """Close a live lot right now: sold to the top bidder, otherwise unsold."""
    p = a.current
    if not p:
        return None
    a.current = None
    if a.bidder:
        t = a.teams[a.bidder]
        t.budget -= a.bid
        t.squad.append((p, a.bid))
        return p, t, a.bid
    a.unsold.append(p)
    return p, None, 0


async def end_auction(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/end_auction - owner / auctioneer: finish the auction, post squads, send the PDFs."""
    chat_id = update.effective_chat.id
    a = await group_auction(update)
    if not a:
        return
    if not is_host(a, update.effective_user.id):
        return await rep(update.message, "🚫 Only the <b>host</b> can end the auction.")
    if a.status == "lobby":
        return await rep(update.message,
                         f"🚫 The auction hasn't started. Use {cmd('cancel')} to discard it.")
    if a.status == "finished":  # already over: just (re)send the PDFs
        return await deliver_reports(ctx, chat_id)
    for n in ("lot", "next"):
        cancel_jobs(ctx, f"{n}-{chat_id}")
    res = settle_current(a)
    if res:
        try:
            await ctx.bot.edit_message_reply_markup(chat_id, a.msg_id, reply_markup=None)
        except Exception:
            pass
        p, t, price = res
        await say(ctx.bot, chat_id,
                  (f"🔨 <b>SOLD!</b> {pname(p)} ➜ <b>{nm(t.name)}</b> for <b>{price}</b>" if t
                   else f"❌ <b>UNSOLD</b> — {pname(p)}"))
        if t:
            await notify_sold(ctx.bot, a, p, t)
    await finish(ctx, chat_id, early=True)


def _handle(username: str, name: str = "") -> str:
    return f"@{username}" + (f" ({name})" if name else "")


def unsold_rows(a: Auction):
    """Players nobody bought: bid-less lots + players the auction never reached."""
    rows = [(p["name"], p.get("username", ""), p.get("set_name", ""), p.get("base") or a.default_base,
             "Unsold") for p in a.unsold]
    for i, st in enumerate(a.sets):
        rows += [(p["name"], p.get("username", ""), set_label(st, i), p.get("base") or a.default_base,
                  "Not auctioned") for p in st["queue"]]
    return rows


def _raw(data) -> bytes:
    return data.getvalue() if hasattr(data, "getvalue") else bytes(data)


async def deliver_reports(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """All squad PDFs + the unsold PDF are sent in DM only (never in the group) to the owner(s), the
    auctioneer and EVERY team captain / vice-captain (each gets all teams' squads and the unsold list)."""
    a = AUCTIONS[chat_id]
    bot = ctx.bot
    if reports is None:
        return await say(bot, chat_id, "🚫 PDF reports need <b>reportlab</b>: run "
                                       "<code>pip install reportlab</code> and restart the bot.")
    await say(bot, chat_id, "📄 Making the PDFs...")
    docs = []  # (bytes, filename, caption)
    for t in a.teams.values():
        rows = [(p["name"], p.get("username", ""), "" if p["role"] == "PL" else ROLE_PLAIN[p["role"]],
                 p["base"], price) for p, price in t.squad]
        try:
            data, fname = reports.squad_pdf(
                a.title or BRAND, t.name, _handle(t.manager_username, t.manager_name),
                _handle(t.vc_username, t.vc_name) if t.vc_username else "", rows,
                a.budget, t.budget, a.squad_size)
            docs.append((_raw(data), fname, f"🏟 {t.name} — squad {len(t.squad)}/{a.squad_size} · 💰 left {t.budget}"))
        except Exception as e:
            log.exception("squad pdf failed for %s", t.name)
            await say(bot, chat_id, f"⚠️ Couldn't make the PDF for <b>{nm(t.name)}</b>: {nm(e)}")
    rows = unsold_rows(a)
    try:
        data, fname = reports.unsold_pdf(a.title or BRAND, rows)
        docs.append((_raw(data), fname, f"❌ Unsold players — {len(rows)}"))
    except Exception as e:
        log.exception("unsold pdf failed")
        await say(bot, chat_id, f"⚠️ Couldn't make the unsold list: {nm(e)}")
    if not docs:
        return

    async def send_all(target, strict=False):
        ok = False
        for raw, fname, cap in docs:
            try:
                await bot.send_document(target, io.BytesIO(raw), filename=fname,
                                        caption=fancy(cap), parse_mode=ParseMode.HTML)
                ok = True
            except Exception as e:
                log.warning("pdf to %s failed: %s", target, e)
                if strict:
                    return False
        return ok

    # (the PDFs are NOT posted in the group - DMs only)
    # who gets the DM copies: owner(s), auctioneer, every captain and vice-captain
    people, missed = {}, []
    for uid in set(OWNER_IDS) | {OWNER_ID, a.host}:
        if uid:
            people[uid] = "owner / auctioneer"
    for t in a.teams.values():
        for uid, user, role in ((t.manager_id, t.manager_username, "captain"),
                                (t.vc_id, t.vc_username, "vice-captain")):
            if not user:
                continue
            if not uid:
                missed.append(f"{t.name}: @{user} (not seen in the group yet)")
            else:
                people.setdefault(uid, f"{t.name} {role}")
    for uid, label in people.items():
        if not await send_all(uid, strict=True):
            missed.append(f"{label} (hasn't started the bot in DM)")

    note = ("✅ <b>PDFs sent in DM!</b> 📄 Every team's squad + the unsold list → "
            "owner · auctioneer · all captains &amp; vice-captains.")
    if missed:
        note += ("\n\n⚠️ <b>Couldn't DM:</b>\n" + "\n".join("• " + nm(m) for m in missed)
                 + "\n💡 They must open the bot and press Start, then the host sends "
                   f"{cmd('end_auction')} again.")
    await say(bot, chat_id, note)


# ======================================================================
#  SETUP
# ======================================================================
# ======================================================================
#  HOOKS FOR THE CRICKET BOT (one bot, one token: auction is a mode)
# ======================================================================
def active_auction(chat_id: int):
    """The running / lobby auction of this chat, or None (finished ones don't count)."""
    a = AUCTIONS.get(chat_id)
    return a if a and a.status != "finished" else None


WIZ_ASK = {
    "name": ("🏆 <b>TOURNAMENT NAME</b>", "Send the name of your tournament."),
    "regname": ("🏆 <b>TOURNAMENT NAME</b>", "Send the name of the tournament players are registering for."),
    "reggroup": ("👥 <b>TOURNAMENT GROUP ID</b>",
                 "Send the tournament group's id (e.g. -1001234567890) or @username, or <b>here</b> for this group. "
                 "Add me to it first. Private group? Add the invite link after the id. Send <b>-</b> to skip."),
    "regchannel": ("📢 <b>TOURNAMENT CHANNEL ID</b>",
                   "Send the tournament channel's id (e.g. -1001234567890) or @username. "
                   "Make me an admin there first. Send <b>-</b> to skip."),
    "teams": ("🏟 <b>NUMBER OF TEAMS</b>", "How many teams will play? (2-30)"),
    "purse": ("💰 <b>PURSE</b>", "Points every team gets to spend, e.g. 100"),
    "base": ("<b>BASE PRICE</b>", "Base price of each player, e.g. 1 (a set line with its own base keeps it)"),
    "min": ("👥 <b>MINIMUM PLAYERS PER TEAM</b>", "The fewest players a team must have, e.g. 4"),
    "max": ("👥 <b>MAXIMUM PLAYERS PER TEAM</b>", "The most players a team can buy, e.g. 6"),
    "order": ("🔀 <b>SET ORDER</b>",
              "In which order should the sets be auctioned? Send every set number once, e.g. "
              "<b>2,5,4,1,3</b>. Send <b>-</b> to keep the order they were added."),
}


async def wiz_ask(bot, chat_id, a: Auction, step: str, banner: bool = False):
    a.wiz = step
    title, how = WIZ_ASK[step]
    if step == "order":  # show the numbered sets to choose from
        for label, sets in (("📋 Registered players", a.reg_sets), ("🧩 Added sets", a.custom_sets)):
            if sets:
                how += (f"\n\n<b>{label}</b>\n<blockquote>"
                        + "\n".join(f"<b>{i}.</b> {nm(x['name'])} · {x['total']} players"
                                    for i, x in enumerate(sets, 1)) + "</blockquote>")
    text = f"{title}\n{how}\n<i>(send your answer in this chat · send “cancel” to stop)</i>"
    if banner:
        return await send_media(bot, chat_id, "welcome",
                                f"🏏 <b>{nm(BRAND)}</b> · AUCTION SETUP 🏆\n\n{text}")
    await say(bot, chat_id, text)


def flow_text(a: Auction) -> str:
    return (f"✅ <b>SETUP SAVED</b> — {nm(a.title)}\n"
            f"🏟 Teams: <b>{a.team_count}</b>\n\n"
            "📋 <b>NEXT STEPS</b>\n"
            f"1️⃣ Add the teams one by one: {cmd('addcap')} [Username] [Team_Name]\n"
            f"2️⃣ Vice-captains (optional): {cmd('addvc')} [Username] [Team_Name]\n"
            f"3️⃣ Players: {cmd('addset')} — add manually or import from registered players\n"
            f"4️⃣ After the players are added: {cmd('next')} — I ask the purse, base price, min / max players and the set order\n"
            f"5️⃣ Start: {cmd('startauction')}\n\n"
            f"⚙️ Change anything later (base price, gap, timer...): {cmd('editauc')}")


def top_base(a: Auction) -> int:
    return max([p.get("base") or 0 for g in a.custom_groups.values() for p in g["players"]]
               + [p.get("base") or 0 for p in a.players.values()] + [1])


async def after_pre(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, a: Auction):
    """Ask the next purse / base / max / min question, or start the auction when all are answered."""
    step = next_pre(a)
    if step:
        return await wiz_ask(ctx.bot, chat_id, a, step)
    a.wiz = ""
    if a.start_after_wiz:
        a.start_after_wiz = False
        return await proceed_start(ctx, chat_id, list(a.start_args))
    await say(ctx.bot, chat_id,
              f"✅ <b>AUCTION SETTINGS SAVED</b>\n{DIV}\n"
              f"<blockquote>💰 Purse ➳ <b>{a.budget}</b>\n"
              f"Base Price ➳ <b>{a.default_base}</b>\n"
              f"👥 Players per team ➳ <b>{a.min_players}–{a.squad_size}</b>\n"
              f"🔀 Set order ➳ <b>{' → '.join(map(str, a.set_order)) if a.set_order else 'as added'}</b>"
              f"</blockquote>\n🚀 Send {cmd('startauction')} when you are ready.")


async def wizard_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE, a: Auction):
    msg, chat_id = update.message, update.effective_chat.id
    text = msg.text.strip()
    if await reg_wizard_input(update, ctx, a):
        return
    if text.lower() in ("cancel", "stop"):
        if a.wiz in PRE_STEPS:  # only the pre-start questions stop: teams and sets stay
            a.wiz, a.start_after_wiz = "", False
            return await rep(msg, f"🛑 Cancelled. Teams and sets are safe — send {cmd('startauction')} when ready.")
        AUCTIONS.pop(chat_id, None)
        return await rep(msg, "🛑 Auction setup cancelled.")
    num = int(text) if text.isdigit() else None
    if a.wiz == "name":
        a.title = text[:40]
        await rep(msg, f"✅ Tournament: <b>{nm(a.title)}</b>")
        return await wiz_ask(ctx.bot, chat_id, a, "teams")
    if a.wiz == "teams":
        if num is None or not 2 <= num <= 30:
            return await rep(msg, "🚫 Send a number from 2 to 30.")
        a.team_count, a.wiz = num, ""
        await rep(msg, f"✅ Teams: <b>{num}</b>")
        return await send_media(ctx.bot, chat_id, "sets", flow_text(a))
    if a.wiz == "purse":
        if num is None or not max(1, top_base(a)) <= num <= 100000:
            return await rep(msg, f"🚫 Send a number from {max(1, top_base(a))} to 100000.")
        a.budget, a.purse_set = num, True
        for t in a.teams.values():
            t.budget = num
        await rep(msg, f"✅ Purse: <b>{num}</b> points per team")
        return await after_pre(ctx, chat_id, a)
    if a.wiz == "base":
        if num is None or not 1 <= num <= a.budget:
            return await rep(msg, f"🚫 Send a number from 1 to {a.budget} (the purse).")
        a.default_base, a.base_set = num, True
        await rep(msg, f"✅ Base price: <b>{num}</b>")
        return await after_pre(ctx, chat_id, a)
    if a.wiz == "min":
        if num is None or not 1 <= num <= 50:
            return await rep(msg, "🚫 Send a number from 1 to 50.")
        a.min_players, a.min_set = num, True
        await rep(msg, f"✅ Minimum players per team: <b>{num}</b>")
        return await after_pre(ctx, chat_id, a)
    if a.wiz == "max":
        low = a.min_players or 1
        if num is None or not low <= num <= 50:
            return await rep(msg, f"🚫 Send a number from {low} to 50 (not below the minimum).")
        a.squad_size, a.max_set = num, True
        await rep(msg, f"✅ Maximum players per team: <b>{num}</b>")
        return await after_pre(ctx, chat_id, a)
    if a.wiz == "order":
        if text in ("-", "skip"):
            a.set_order, a.order_set = [], True
            await rep(msg, "✅ Set order: <b>as added</b>")
            return await after_pre(ctx, chat_id, a)
        order = [int(x) for x in re.findall(r"\d+", text)]
        if not any(sorted(order) == list(range(1, n + 1)) for n in pool_counts(a)):
            n = max(pool_counts(a))
            return await rep(msg, f"🚫 Send each set number from 1 to {n} exactly once, "
                                  f"e.g. <b>{','.join(str(i) for i in range(n, 0, -1))}</b>.")
        a.set_order, a.order_set = order, True
        await rep(msg, f"✅ Set order: <b>{' → '.join(map(str, order))}</b>")
        return await after_pre(ctx, chat_id, a)


async def edit_card(msg, text: str, kb=None):
    """Edit a menu card whether it is a photo / video (caption) or a plain text message."""
    text = fancy(text)
    try:
        if msg.photo or msg.video or msg.animation or msg.document:
            await msg.edit_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return True
    except Exception as e:
        log.info("card edit failed: %s", e)
        return False


async def open_from_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """The Auction button of the group /start menu: shows the Become Auctioneer card."""
    q = update.callback_query
    chat, user = q.message.chat, q.from_user
    if chat.type == "private":
        return await q.answer("Use the Auction button inside your group.", show_alert=True)
    if not is_sudo(user.id):
        return await q.answer("No access. Only the owner and sudo users can run auctions.",
                              show_alert=True)
    old = active_auction(chat.id)
    if old and old.status == "running":
        return await q.answer("An auction is running here. Use /cancel first.", show_alert=True)
    await q.answer()
    kb = Markup([[btn("🔨 become auctioneer", callback_data="aucb:become")]])
    card = ("🔨 <b>AUCTION MODE</b>\n"
            f"{DIV}\n"
            "<blockquote>An IPL-style player auction — team captains bid on players "
            "with their purse, live in this group.</blockquote>\n"
            "🏁 <b>Ready to run it?</b>")
    if not await edit_card(q.message, card, kb):
        await send_media(ctx.bot, chat.id, "welcome", card, reply_markup=kb)


async def on_become(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Become Auctioneer button: creates the auction, then asks the tournament name."""
    q = update.callback_query
    chat, user = q.message.chat, q.from_user
    if chat.type == "private":
        return await q.answer("Use this inside your group.", show_alert=True)
    if not is_sudo(user.id):
        return await q.answer("No access. Only the owner and sudo users can run auctions.",
                              show_alert=True)
    old = active_auction(chat.id)
    if old and old.status == "running":
        return await q.answer("An auction is running here. Use /cancel first.", show_alert=True)
    if old and old.host != user.id and not is_owner(user.id):
        return await q.answer("Another auctioneer already set this group up.", show_alert=True)
    a = AUCTIONS[chat.id] = Auction(host=user.id, host_name=user.full_name, wiz_user=user.id)
    await q.answer("✅ You are now the Auctioneer!", show_alert=True)
    await edit_card(q.message,
                    f"🔨 <b>AUCTIONEER</b> ➳ {nm(user.full_name)}\n{DIV}\n"
                    "🏆 Let's set up your tournament 👇")
    await wiz_ask(ctx.bot, chat.id, a, "name")


def register(app: Application, owner_ids, in_auction_chat, media_lookup=None):
    """Add every auction handler to the cricket bot's Application.
    The commands both bots use (/join /teams /addfile /delfile /start) are NOT added here:
    the cricket bot routes them (see in_auction_chat)."""
    global OWNER_ID, EXT_MEDIA
    EXT_MEDIA = media_lookup
    OWNER_IDS.update(owner_ids)
    OWNER_ID = min(OWNER_IDS) if OWNER_IDS else 0
    private = filters.ChatType.PRIVATE
    app.add_handler(TypeHandler(Update, sudo_bind), group=-1)  # link sudo @usernames to ids
    load_auctions(app)  # bring back set-up auctions / registrations after a restart
    if app.job_queue:   # catch-all: every change (teams, settings, sets...) is saved within 10 s
        app.job_queue.run_repeating(_autosave, interval=10, first=10, name="auction-autosave")

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("register", register_cmd, filters=private),
                      CallbackQueryHandler(register_cmd, pattern=r"^reg:start$"),
                      CallbackQueryHandler(reg_pick, pattern=r"^reg:pick:"),
                      CallbackQueryHandler(reg_check, pattern=r"^reg:check:")],
        states={
            USERNAME: [CallbackQueryHandler(username_default, pattern="^un_default$"),
                       MessageHandler(filters.TEXT & ~filters.COMMAND, username_text)],
            NAME: [CallbackQueryHandler(name_default, pattern="^name_default$"),
                   MessageHandler(filters.TEXT & ~filters.COMMAND, name_text)],
        },
        fallbacks=[CommandHandler("cancelreg", cancelreg)],
        conversation_timeout=900,
    ))

    # /addcap /addteam /addvc also exist in Tournament mode: the auction takes them only while an
    # auction is set up / running in this chat, otherwise they fall through to the tournament.
    class _InAuction(filters.UpdateFilter):
        def filter(self, update):
            return bool(in_auction_chat(update))
    in_auc = _InAuction()
    app.add_handler(CommandHandler(["addcap", "createteam", "addteam"], addteam, filters=in_auc))
    app.add_handler(CommandHandler("addvc", addvc, filters=in_auc))
    app.add_handler(CommandHandler("skip", skip_cmd, filters=in_auc))
    app.add_handler(CommandHandler("next", next_cmd, filters=in_auc))
    app.add_handler(CommandHandler("editauc", editauc))
    app.add_handler(CommandHandler("squads", squads_cmd))
    app.add_handler(CallbackQueryHandler(squads_cb, pattern=r"^sq:"))
    app.add_handler(CommandHandler("setpurse", make_setter("budget")))
    for name, fn in [("help", help_cmd), ("openreg", openreg),
                     ("closereg", closereg), ("players", players_cmd), ("playerlist", players_cmd),
                     ("startauction", startauction), ("cancel", cancel),
                     ("addaucplayer", addaucplayer), ("resold", resold),
                     ("addplayerreg", addplayerreg), ("deregister", deregister),
                     ("team", team), ("purse", purse), ("customsets", customsets),
                     ("auctions", auctions_cmd), ("auc_help", auc_help),
                     ("addset", addset), ("addaucset", addaucset), ("delaucset", delaucset), ("aucsets", aucsets),
                     ("settings", settings_cmd),
                     ("removeteam", removeteam),
                     ("changevc", changevc), ("end_auction", end_auction),
                     ("setname", setname), ("addsudo", addsudo), ("rmsudo", rmsudo),
                     ("setlog", setlog),
                     ("sudo", sudo_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    for key in SETTINGS:
        app.add_handler(CommandHandler("set" + key, make_setter(key)))
    any_media = filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.CaptionRegex(r"^/customsets") & filters.ChatType.GROUPS,
        customsets_file))
    app.add_handler(CallbackQueryHandler(on_source, pattern=r"^src:"))
    app.add_handler(CallbackQueryHandler(on_become, pattern=r"^aucb:become$"))
    app.add_handler(CallbackQueryHandler(on_setup, pattern=r"^su:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, setup_input),
                    group=2)
    app.add_handler(CommandHandler("bid", bid_cmd, filters=filters.ChatType.GROUPS))
    # quietly learn who is who in the group (links @usernames of managers / VCs to Telegram ids)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, bind_seen), group=3)
