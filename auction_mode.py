"""ᴍᴀᴛʀɪx ᴀᴜᴄᴛɪᴏɴ ʙᴏᴛ
- Owner/auctioneer adds teams with /addteam (+ /addvc, /changevc); managers and VCs both bid
- Sets are added as "1. Name | Username | Userid" lists (/addaucset) or via DM registration
- Player cards carry the player's profile picture (common default picture via /addfile player)
- /end_auction posts the final squads and sends squad PDFs + the unsold list PDF
- Auctioneer controls Budget / Squad / Timer / Gap live (/settings)
- Fully automatic, timer-based bidding in the group
- Small-caps font, bold text, emojis, plus images / GIFs / video

- SUDO: only the owner (OWNER_ID) and sudo users can run auctions: /addsudo @user, /rmsudo @user, /sudo
- /start in a group = auction setup buttons; /start in the bot's DM = Player Registration button
  (registration: 1 join group+channel and verify, 2 username, 3 name, 4 role; then posts to the log channel set with /setlog;
  everyone gets the default base price; sets are random)

Run:  python bot.py   (token is read from .env)
"""
import copy
import csv
import html
import io
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
    "sold": ("🔨 SOLD GIF", "when a player is sold"),
    "unsold": ("❌ UNSOLD GIF", "when a player gets no bids"),
    "trophy": ("🏆 Final GIF", "end of auction, before the final squads"),
    "player": ("🧢 Default player picture", "player card of anyone with no profile picture"),
}
SLOT_ALIAS = {"default": "player", "noprofile": "player", "profile": "player", "avatar": "player"}


def _load_cfg() -> dict:
    try:
        return json.loads(MEDIA_CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}


MEDIA_DATA: dict = _load_cfg()  # {"<chat_id>" | "global": {slot: {"file", "kind"}}}


def _save_cfg():
    MEDIA_CFG.write_text(json.dumps(MEDIA_DATA, indent=1), encoding="utf-8")


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


AUCTIONS: dict[int, Auction] = {}


def max_bid(a: Auction, t: Team) -> int:
    """Keep enough points to reach the minimum squad at the cheapest price."""
    slots_left = (a.min_players or a.squad_size) - len(t.squad) - 1
    return t.budget - max(slots_left, 0) * MIN_BASE


def cancel_jobs(ctx, name):
    for j in ctx.job_queue.get_jobs_by_name(name):
        j.schedule_removal()


# ---------- SUDO: who may use the auction bot ----------
# The owner (OWNER_ID in .env) is always allowed. Everyone else needs the owner to run
# /addsudo @Username. Players (DM registration) and team managers / vice-captains (bid buttons,
# /team, /purse) do NOT need sudo - sudo is for people who run auctions.
SUDO_FILE = DATA / "auction_sudo.json"


def _load_sudo() -> list:
    try:
        data = json.loads(SUDO_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [e for e in data if isinstance(e, dict) and (e.get("id") or e.get("username"))]


SUDO: list = _load_sudo()  # [{"id": int | None, "username": str, "name": str}]


def _save_sudo():
    SUDO_FILE.write_text(json.dumps(SUDO, indent=1), encoding="utf-8")


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
    """['Mumbai_Indians', '@raj'] -> ('Mumbai Indians', 'raj'); None if invalid."""
    if len(args) < 2:
        return None
    user = args[-1].lstrip("@").strip()
    name = re.sub(r"[^\w \-'.&]", "", re.sub(r"[\s_]+", " ", " ".join(args[:-1]))).strip()[:30]
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
        f"{cmd('start')} → tap <b>Auction</b> — name, teams, max / min players 🚀\n"
        f"{cmd('createteam')} [Team_Name] [@Captain_Username] — create a team 🧢\n"
        f"{cmd('addvc')} / {cmd('changevc')} [Team_Name] [@Username] — vice-captain 🎖\n"
        f"{cmd('addaucset')} [Set_Number] [Set_Name] — reply to a set message 📦\n"
        f"{cmd('setpurse')} [amount] — purse for every team 💰\n"
        f"{cmd('editauc')} — change base price, gap, timer, players... ⚙️\n"
        f"{cmd('startauction')} — start the bidding 🔨\n"
        f"{cmd('end_auction')} — end it + send the squad / unsold PDFs 📄\n"
        f"{cmd('cancel')} — cancel everything 🛑\n"
        f"{cmd('squads')} — saved squads of finished tournaments 📚\n\n"
        f"🧢 <b>CAPTAINS &amp; VICE-CAPTAINS</b>: {cmd('team')} · {cmd('purse')} · tap the bid buttons\n\n"
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
    "team": ("🏟 <b>ADD A TEAM</b>", "Send <i>Team_Name @manager_username</i>\nExample: Mumbai_Indians @raj_k"),
    "vc": ("🎖 <b>VICE-CAPTAIN</b>", "Send <i>Team_Name @vc_username</i> — adds, or replaces, that team's VC"),
    "budget": ("💰 <b>PURSE</b>", "Send the points each team gets, e.g. 100"),
    "squad": ("👥 <b>SQUAD SIZE</b>", "Send how many players each team buys, e.g. 4"),
    "base": ("💎 <b>BASE PRICE</b>", "Send the base price every registered player gets, e.g. 1"),
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
            f"💎 Base: <b>{a.default_base}</b> · ⏱ Timer: <b>{a.bid_timer}s</b> · ⏳ Gap: <b>{a.gap}s</b>\n"
            f"📦 Sets: <b>{len(sets)}</b> · 🧢 Players: <b>{n_players}</b>\n"
            f"👑 Host: <b>{nm(a.host_name or 'owner')}</b>\n\n"
            f"📖 {cmd('help')} — all commands · {cmd('auc_help')} — full guide")


def panel_keyboard():
    def b(emoji, label, key):
        return Btn(f"{emoji} {sc(label)}", callback_data=f"su:{key}")
    return Markup([
        [b("🏟", "add team", "team"), b("🎖", "vice-captain", "vc")],
        [b("💰", "purse", "budget"), b("👥", "squad size", "squad")],
        [b("💎", "base price", "base"), b("⏱", "timer", "timer")],
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
            f"Then <b>reply</b> to it with {cmd('addaucset')} [Set_Number], e.g. {cmd('addaucset')} 1\n"
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
        f"👑 The owner or auctioneer adds you with {cmd('addteam')} [Team_Name] [Manager_Username].")


async def addteam(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/createteam [Team_Name] [Captain_Telegram_Username]  (owner / auctioneer, before the start)."""
    a = await host_auction(update)
    if not a:
        return
    if a.team_count and len(a.teams) >= a.team_count:
        return await rep(update.message,
            f"🚫 All <b>{a.team_count}</b> teams are created. Need more? {cmd('editauc')} teams [number]")
    parsed = parse_team_args(ctx.args)
    if not parsed:
        return await rep(update.message,
            "🏟 <b>CREATE A TEAM</b>\n"
            f"{cmd('createteam')} [Team_Name] [Captain_Username]\n"
            f"Example: {cmd('createteam')} Mumbai_Indians @raj_k\n\n"
            f"🎖 Then add a vice-captain: {cmd('addvc')} [Team_Name] [Username]")
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
    await rep(update.message,
        f"✅ Team <b>{nm(name)}</b> added! 🏟\n🧢 Captain: {nm('@' + user)}\n"
        f"👥 Teams: <b>{len(a.teams)}/{a.team_count or '?'}</b>\n"
        f"🎖 Add a vice-captain: {cmd('addvc')} {nm(name.replace(' ', '_'))} [Username]")


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
            f"{cmd(me)} [Team_Name] [Vice-Captain_Username]\n"
            f"Example: {cmd(me)} Mumbai_Indians @amit_s\n📋 {cmd('teams')}")
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
        return await rep(update.message, f"📭 No teams yet. Host: {cmd('addteam')} [Team_Name] [Manager_Username]")
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
    minutes = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 0
    a.reg_open = True
    cancel_jobs(ctx, f"reg-{chat_id}")
    link = f"https://t.me/{ctx.bot.username}?start=reg_{chat_id}"
    when = (f"⏳ Closes automatically in <b>{minutes} min</b>." if minutes
            else f"⏳ The host closes it with {cmd('closereg')}.")
    await send_media(ctx.bot, chat_id, "register",
        "📝 <b>PLAYER REGISTRATION IS OPEN!</b> 🔓\n"
        f"{when}\n\n"
        "🧢 Cricketers — tap the button below and fill in your details in DM 👇",
        reply_markup=Markup([[Btn("📝 " + sc("register in dm"), url=link)]]))
    if minutes:
        ctx.job_queue.run_once(auto_close_reg, minutes * 60, chat_id=chat_id,
                               name=f"reg-{chat_id}")


async def auto_close_reg(ctx: ContextTypes.DEFAULT_TYPE):
    a = AUCTIONS.get(ctx.job.chat_id)
    if a and a.reg_open:
        await close_registration(ctx, ctx.job.chat_id)


async def closereg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = await group_auction(update)
    if not a:
        return await rep(update.message, f"🤷 No auction here. Use {cmd('addteam')} or {cmd('openreg')}.")
    if not is_host(a, update.effective_user.id):
        return await rep(update.message, "🚫 Only the <b>host</b> can do that.")
    if not a.reg_open:
        return await rep(update.message, "🚫 Registration isn't open.")
    cancel_jobs(ctx, f"reg-{update.effective_chat.id}")
    await close_registration(ctx, update.effective_chat.id)


async def close_registration(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int):
    a = AUCTIONS[chat_id]
    a.reg_open = False
    sets = a.reg_sets = build_sets(list(a.players.values()), a.set_size)
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
                    f"💎 Base price: <b>{p.get('base') or a.default_base}</b>\n🍀 Good luck!")
            except Exception:
                pass


async def players_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = await group_auction(update)
    if not a or not a.players:
        return await rep(update.message, "📋 No players registered yet.")
    state = "🔓 open" if a.reg_open else "🔒 closed"
    lines = [f"📋 <b>{len(a.players)} REGISTERED</b> ({state})"]
    for p in a.players.values():
        r = norm_role(p)
        lines.append(f"{ROLE_EMOJI[r]} <b>{pname(p)}</b> · {ROLE_PLAIN[r]} · 💎 {p.get('base') or a.default_base}")
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
            lines.append(f"   {n}. {ROLE_EMOJI[p['role']]} <b>{pname(p)}</b> · 💎 {p.get('base') or a.default_base}")
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
            f"2️⃣ <b>Reply</b> to that message with {cmd('addaucset')} [Set_Number] [Set_Name]\n"
            f"Example: {cmd('addaucset')} 1 Marquee\n\n"
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
    lines += [f"   {n}. <b>{pname(p)}</b> · 💎 {p.get('base') or a.default_base}"
              for n, p in enumerate(g["players"], 1)]
    lines.append(bad_lines_note(errors, dups).strip())
    total = sum(x["total"] for x in a.custom_sets)
    lines.append(f"\n📚 Sets: <b>{len(a.custom_sets)}</b> · 🧢 Players: <b>{total}</b>\n"
                 f"➕ Add another with {cmd('addaucset')} [Set_Number] [Set_Name] · 📋 {cmd('aucsets')} · 🔨 {cmd('startauction')}")
    await send_long(ctx.bot, update.effective_chat.id, "\n".join(l for l in lines if l))


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
                         f"📭 No sets added yet. Reply to a set message with {cmd('addaucset')} [Set_Number].")
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
            f"➕ To add or replace <b>one</b> set use {cmd('addaucset')} [Set_Number] (reply to the set).\n"
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
    "base": ("default_base", "💎 Default base price", 1, 100000, True, "pts (for set lines with no base)"),
    "setsize": ("set_size", "📦 Players per set", 1, 100, True, "players (registered / default sets are random)"),
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
                "baseprice": "base", "gap": "gap", "timer": "timer", "setsize": "setsize"}


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
        "1️⃣ In your group send /start and tap <b>Auction</b>. Answer the questions: "
        "tournament name → number of teams → maximum players per team → minimum players per team.\n\n"
        f"2️⃣ Create every team with its captain: {cmd('createteam')} [Team_Name] [@Captain_Username]\n"
        f"    e.g. {cmd('createteam')} Mumbai_Indians @raj_k\n\n"
        f"3️⃣ Add each team's vice-captain: {cmd('addvc')} [Team_Name] [@VC_Username] "
        f"(change it any time with {cmd('changevc')}). The captain and the VC can both bid.\n\n"
        "4️⃣ Add the player sets. Send a message, one player per line:\n"
        f"<pre>{nm(SET_EXAMPLE)}</pre>"
        f"Then <b>reply</b> to it with {cmd('addaucset')} [Set_Number] [Set_Name], e.g. {cmd('addaucset')} 1 Marquee. "
        f"Same number again replaces the set · {cmd('delaucset')} N removes · {cmd('aucsets')} shows all.\n"
        "Optional extras per line: <i>| Base | Note</i>. Use <b>-</b> when a player has no username.\n\n"
        f"5️⃣ Set the purse: {cmd('setpurse')} [amount]\n\n"
        f"6️⃣ Start: {cmd('startauction')}. Bidding is automatic: tap the bid buttons, the timer resets on every bid, "
        "and a team can never bid more than it needs to still reach the minimum squad.\n\n"
        f"7️⃣ Finish: {cmd('end_auction')} — posts the final squads and sends a squad PDF per team "
        "(group + captain/VC DMs) and the unsold list PDF.\n\n"
        f"⚙️ {cmd('editauc')} — change tournament name, teams, purse, max / min players, base price, gap, timer.\n"
        f"🖼 {cmd('addfile')} [key] — change the GIFs / images. Keys: aucwelcome, aucregister, aucsets, aucintro, "
        f"auclive, aucsold, aucunsold, auctrophy, aucplayer (picture for players with no profile photo). "
        f"Reply to a file with {cmd('addfile')} aucsold · {cmd('files')} lists every key.\n"
        f"🛡 Owner: {cmd('addsudo')} / {cmd('rmsudo')} / {cmd('sudo')} · {cmd('setgroup')} / {cmd('setchannel')} / {cmd('setplayzone')} / {cmd('setlog')}")


# ---------- start the auction ----------
async def startauction(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    a = await group_auction(update)
    if not a or a.status != "lobby":
        return await rep(update.message, f"🚫 Tap <b>Auction</b> in /start and set it up first.")
    if not is_host(a, update.effective_user.id):
        return await rep(update.message, "🚫 Only the <b>host</b> can start.")
    if a.reg_open:
        return await rep(update.message, f"🚫 Close registration first with {cmd('closereg')}.")
    if len(a.teams) < 2:
        return await rep(update.message, f"🚫 Need at least <b>2 teams</b> ({cmd('createteam')}).")
    if a.team_count and len(a.teams) < a.team_count:
        return await rep(update.message,
            f"🚫 <b>{len(a.teams)}/{a.team_count}</b> teams created. Add the rest with {cmd('createteam')} "
            f"or lower the number: {cmd('editauc')} teams [number]")
    avail = [k for k, v in (("registered", a.reg_sets), ("custom", a.custom_sets)) if v]
    pick = ctx.args[0].lower() if ctx.args else None
    if pick in ("registered", "custom") and pick not in avail:
        return await rep(update.message, f"🚫 No <b>{pick}</b> players yet.")
    if pick not in ("registered", "custom", "default"):
        if len(avail) == 1:
            pick = avail[0]
        elif not avail:
            pick = "default"
        else:  # both exist: let the host choose
            reg_n = sum(s["total"] for s in a.reg_sets)
            cus_n = sum(s["total"] for s in a.custom_sets)
            rows = [[btn(f"📋 registered players ({reg_n})", callback_data="src:registered")],
                    [btn(f"🧩 my added sets ({cus_n})", callback_data="src:custom")],
                    [btn("🎲 default pool", callback_data="src:default")]]
            return await rep(update.message,
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
        f"🎯 Pool: <b>{label}</b>\n\n⏳ First lot in 3 seconds...")
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
    try:
        d = json.loads(TCFG_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


TCFG: dict = _load_tcfg()  # {"group": {"id","title","link"}, "channel": {...}}


def _save_tcfg():
    TCFG_FILE.write_text(json.dumps(TCFG, indent=1), encoding="utf-8")


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
    """/setgroup | /setchannel  [@username | chat_id | t.me/username] [invite_link]  (owner only)."""
    msg, chat = update.message, update.effective_chat
    me = {"group": "setgroup", "channel": "setchannel", "log": "setlog"}[kind]
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


async def setplayzone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/setplayzone [@username | chat_id | t.me link]  (owner only): the group where players come to play.
    Shows as a "Play Zone" button on the /start menu. Send it inside the group to use that group."""
    msg, chat = update.message, update.effective_chat
    if not is_owner(update.effective_user.id):
        return await rep(msg, "🚫 Only the <b>owner</b> can set the Play Zone.")
    args = ctx.args
    if args and args[0].lower() in ("off", "remove", "clear", "none"):
        TCFG.pop("playzone", None)
        _save_tcfg()
        return await rep(msg, "🗑 Play Zone removed from the start menu.")
    if not args:
        if chat.type == "private":
            cur = TCFG.get("playzone")
            return await rep(msg,
                "🎮 <b>PLAY ZONE</b>\n"
                + (f"Now: <b>{nm(cur['title'])}</b>\n\n" if cur else "Now: <i>not set</i>\n\n")
                + f"Set it: {cmd('setplayzone')} [@username, chat_id or t.me link]\n"
                f"💡 Or send {cmd('setplayzone')} inside the group itself.\n"
                f"🗑 Remove: {cmd('setplayzone')} off")
        ref = str(chat.id)
    else:
        ref = args[0]
    if ref.startswith("http") and not re.match(r"^(?:https?://)?t\.me/[A-Za-z][A-Za-z0-9_]{3,}/?$", ref):
        TCFG["playzone"] = {"id": 0, "title": "Play Zone", "link": ref}  # private invite link
        _save_tcfg()
        return await rep(msg, "✅ <b>Play Zone</b> set. Players now see a 🎮 Play Zone button on /start.")
    c = await _resolve_chat(ctx.bot, ref)
    if c is None:
        return await rep(msg,
            "🚫 I couldn't find that chat. Add me to the group first, then use its @username or numeric id.\n"
            f"For a private group, send its invite link: {cmd('setplayzone')} https://t.me/+abc")
    if c.type not in ("group", "supergroup"):
        return await rep(msg, f"🚫 That is a <b>{c.type}</b>, not a group.")
    link = next((x for x in args[1:] if x.startswith("http")), "")
    if not link:
        link = f"https://t.me/{c.username}" if c.username else (getattr(c, "invite_link", "") or "")
    if not link:
        return await rep(msg, f"⚠️ That group has no link. Add one: {cmd('setplayzone')} {c.id} https://t.me/+abc")
    TCFG["playzone"] = {"id": c.id, "title": c.title or str(c.id), "link": link}
    _save_tcfg()
    await rep(msg, f"✅ Play Zone set to <b>{nm(c.title)}</b>. Players now see a 🎮 Play Zone button on /start.")


def zone_buttons() -> list:
    """URL buttons for the /start menu: the set channel and the Play Zone."""
    row = []
    ch, pz = TCFG.get("channel"), TCFG.get("playzone")
    if ch and ch.get("link"):
        row.append(Btn("📢 Channel", url=ch["link"]))
    if pz and pz.get("link"):
        row.append(Btn("🎮 Play Zone", url=pz["link"]))
    return row


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
            f"🏆 Tournament: <b>{nm(TOURNAMENT)}</b>\n"
            + (f"🏷 Auction: <b>{nm(a.title)}</b>\n" if a.title else "") +
            f"👤 Username: <b>{nm(un)}</b>\n"
            f"🧢 Name: <b>{nm(r['name'])}</b>\n"
            f"🎭 Role: <b>{ROLES[r['role']]}</b>\n"
            f"🆔 Telegram ID: {nm(user.id)}\n"
            f"👥 Total registered: <b>{len(a.players)}</b>\n"
            f"🕒 {nm(time.strftime('%d %b %Y, %I:%M %p'))}")
    try:
        await say(bot, cid, text)
    except Exception as e:
        log.warning("could not post to log channel %s: %s", cid, e)


async def missing_joins(bot, user_id: int) -> list:
    """[(kind, entry, 'no' | 'error')] for each required chat the user is not in (or can't be checked)."""
    out = []
    for kind in ("group", "channel"):
        e = TCFG.get(kind)
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
    """Step 1: show the group + channel links and a Verify Membership button."""
    rows = []
    for kind, icon, word in (("group", "👥", "Tournament Group"), ("channel", "📢", "Tournament Channel")):
        e = TCFG.get(kind)
        if e and e.get("link"):
            rows.append([Btn(f"{icon} " + sc(f"join {word.lower()}"), url=e["link"])])
    rows.append([Btn("✅ " + sc("verify membership"), callback_data=f"reg:check:{chat_id}")])
    await rep(message, f"<b>1/{REG_STEPS}</b> 🔐 <b>Member Verification</b>\n\n"
                       "To continue with Player Registration, please join BOTH the tournament group "
                       "and channel. Then tap <b>Verify Membership</b>.",
              reply_markup=Markup(rows))


async def reg_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """'I've joined' button: re-check, then carry on with the registration."""
    q = update.callback_query
    try:
        chat_id = int(q.data.split(":")[2])
    except (IndexError, ValueError):
        await q.answer()
        return ConversationHandler.END
    if await missing_joins(ctx.bot, q.from_user.id):
        await q.answer(sc("❌ you haven't joined both yet. join, then tap verify again."), show_alert=True)
        return ConversationHandler.END
    await q.answer(sc("✅ verified"))
    try:
        await q.edit_message_text(fancy(f"<b>1/{REG_STEPS}</b> 🔐 Membership: <b>Verified</b> ✅"), parse_mode=ParseMode.HTML)
    except Exception:
        pass
    return await begin_reg(update, ctx, chat_id, checked=True)


USERNAME, NAME, ROLE = range(3)
REG_STEPS = 4


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
    if not checked:  # must have joined the tournament group / channel before anything else
        missing = await missing_joins(ctx.bot, user.id)
        if missing or TCFG.get("group") or TCFG.get("channel"):
            await ask_join(msg, chat_id)  # step 1 is always shown
            return ConversationHandler.END
    ctx.user_data["reg_chat"], ctx.user_data["reg"] = chat_id, {}
    old = a.players.get(user.id)
    prefix = (f"🔁 You're already registered as <b>{nm(old['name'])}</b>. "
              "Continue to update your details.\n\n" if old else "")
    await send_media(ctx.bot, user.id, "register",
                     "📝 <b>PLAYER REGISTRATION</b> 🏏" + (f"\n🏷 <b>{nm(a.title)}</b>" if a.title else ""),
                     media_chat=chat_id)
    if user.username:
        how = (f"Type it (e.g. {nm('@' + user.username)}) or tap below. "
               f"({cmd('cancelreg')} to stop)")
        kb = Markup([[Btn("✅ " + sc("use") + f" @{user.username}", callback_data="un_default")]])
    else:
        how = (f"You don't seem to have one — send <b>-</b> to continue without. "
               f"({cmd('cancelreg')} to stop)")
        kb = None
    await rep(msg, prefix + f"<b>2/{REG_STEPS}</b> 👤 What's your Telegram <b>username</b>?\n" + how,
              reply_markup=kb)
    return USERNAME


async def ask_name(message, user):
    await rep(message, f"<b>3/{REG_STEPS}</b> 🧢 What <b>name</b> should appear in the auction?\n"
                       "Type it, or tap below.",
              reply_markup=Markup([[Btn("✅ " + sc("use") + f' "{user.full_name}"',
                                        callback_data="name_default")]]))
    return NAME


async def username_default(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    un = q.from_user.username or ""
    ctx.user_data.setdefault("reg", {})["username"] = un
    await q.edit_message_text(fancy(f"<b>2/{REG_STEPS}</b> 👤 Username: <b>{nm('@' + un) if un else '—'}</b> ✅"), parse_mode=ParseMode.HTML)
    return await ask_name(q.message, q.from_user)


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
    return await ask_name(update.message, update.effective_user)


async def ask_role(message):
    rows = [[btn(label, callback_data=f"role:{code}")] for code, label in ROLES.items()]
    await rep(message, f"<b>4/{REG_STEPS}</b> 🎭 What's your <b>role</b>?", reply_markup=Markup(rows))
    return ROLE


async def name_default(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.setdefault("reg", {})["name"] = q.from_user.full_name
    await q.edit_message_text(fancy(f"<b>3/{REG_STEPS}</b> 🧢 Name: <b>{nm(q.from_user.full_name)}</b> ✅"), parse_mode=ParseMode.HTML)
    return await ask_role(q.message)


async def name_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.setdefault("reg", {})["name"] = update.message.text.strip()[:40]
    return await ask_role(update.message)


async def role_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data.split(":")[1]
    ctx.user_data.setdefault("reg", {})["role"] = code
    await q.edit_message_text(fancy(f"<b>4/{REG_STEPS}</b> 🎭 Role: <b>{ROLES[code]}</b> ✅"), parse_mode=ParseMode.HTML)
    return await finish_reg(update, ctx)


async def finish_reg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    a = AUCTIONS.get(ctx.user_data.get("reg_chat"))
    if not a or not a.reg_open:
        await rep(msg, "🔒 Registration has closed, nothing was saved.")
        return ConversationHandler.END
    r, user = ctx.user_data.get("reg", {}), update.effective_user
    if not all(k in r for k in ("username", "name", "role")):
        await rep(msg, "⚠️ Something went wrong, please tap <b>Player Registration</b> again.")
        return ConversationHandler.END
    updated = user.id in a.players
    # no base price is asked: the auctioneer's default base price applies at the auction
    a.players[user.id] = {"uid": user.id, "username": r["username"], "name": r["name"], "role": r["role"],
                          "base": None, "note": ""}
    await rep(msg,
        "✅ <b>REGISTERED!</b> 🎉\n\n"
        f"👤 Username: <b>{nm('@' + r['username']) if r['username'] else '—'}</b>\n"
        f"🧢 Name: <b>{nm(r['name'])}</b>\n"
        f"🎭 Role: <b>{ROLES[r['role']]}</b>\n\n"
        "📦 When registration closes you'll be placed in a random set and I'll DM you here.\n"
        "🔁 Tap Player Registration again to edit your details.")
    await send_reg_log(ctx.bot, a, user, r, updated)
    return ConversationHandler.END


async def cancelreg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await rep(update.message, "🛑 Registration cancelled (nothing saved).")
    return ConversationHandler.END


# ======================================================================
#  AUCTION FLOW
# ======================================================================
def lot_text(a: Auction) -> str:
    p, s = a.current, a.sets[a.set_idx]
    lot_no = s["total"] - len(s["queue"])
    who = (f"<b>{nm(a.teams[a.bidder].name)}</b>" + (f" · 🙋 {nm(a.bidder_by)}" if a.bidder_by else "")
           if a.bidder else "<i>no bids yet</i>")
    note = f"📝 <i>{nm(p['note'])}</i>\n" if p.get("note") else ""
    return (f"📦 <b>{set_label(s, a.set_idx).upper()}</b> · LOT {lot_no}/{s['total']}\n"
            "━━━━━━━━━━━━━━\n"
            f"{ROLE_EMOJI[p['role']]} <b>{pname(p)}</b>"
            f"{'' if p['role'] == 'PL' else ' (' + ROLE_PLAIN[p['role']] + ')'}\n"
            f"{note}"
            f"💎 Base price: <b>{p['base']}</b>\n"
            f"💰 Current bid: <b>{a.bid or '—'}</b>\n"
            f"👑 Leading: {who}\n"
            f"⏱ Sold <b>{a.bid_timer}s</b> after the last bid")


def lot_keyboard(a: Auction):
    first = a.current["base"] if not a.bidder else a.bid + 1
    amounts = sorted({first, a.bid + 2 if a.bidder else first + 1,
                      a.bid + 5 if a.bidder else first + 4})
    return Markup([[Btn(f"💰 {sc('bid')} {x}", callback_data=f"bid:{a.current['id']}:{x}")
                    for x in amounts]])


async def refresh_lot(bot, chat_id, a: Auction):
    text, kb = fancy(lot_text(a)), lot_keyboard(a)
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
    if not s["announced"]:
        s["announced"] = True
        await send_media(ctx.bot, chat_id, "live",
            f"🔔 <b>{set_label(s, a.set_idx).upper()}</b> ({a.set_idx + 1}/{len(a.sets)})\n"
            f"🧢 <b>{s['total']}</b> players up for grabs! 🔥")
    a.current, a.bid, a.bidder, a.bidder_by = s["queue"].pop(0), 0, None, ""
    a.current["set_name"] = s["name"]
    p = a.current
    # picture order: the player's Telegram profile picture -> the common picture set with
    # /addfile player -> a generated lot card -> plain text
    attempts = []
    photo = await player_photo(ctx.bot, a, p)
    if photo:
        attempts.append(("lot", photo))
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
                                            reply_markup=lot_keyboard(a), source=src, strict=True)
        if msg:
            break
    if not msg:
        msg, a.lot_media = await send_media(ctx.bot, chat_id, "lot", lot_text(a),
                                            reply_markup=lot_keyboard(a))
    a.msg_id = msg.message_id
    ctx.job_queue.run_once(close_lot, a.bid_timer, chat_id=chat_id, name=f"lot-{chat_id}")


async def on_bid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id, uid = q.message.chat_id, q.from_user.id
    a = AUCTIONS.get(chat_id)
    _, pid, amount = q.data.split(":")
    pid, amount = int(pid), int(amount)

    if not a or a.status != "running" or not a.current or a.current["id"] != pid:
        return await q.answer(sc("⌛ this lot is closed."), show_alert=True)
    remember_user(chat_id, q.from_user)
    t, _role = find_team(a, q.from_user)  # manager OR vice-captain can bid for the team
    if not t:
        return await q.answer(sc("🚫 only team managers and vice-captains can bid."), show_alert=True)
    if len(t.squad) >= a.squad_size:
        return await q.answer(sc("✅ your squad is full."), show_alert=True)
    if tkey(t.name) == a.bidder:
        return await q.answer(sc("👑 your team already has the top bid."))
    floor = a.current["base"] if not a.bidder else a.bid + 1
    if amount < floor:
        return await q.answer(sc("⚡ someone already bid that. try the new price."), show_alert=True)
    if amount > max_bid(a, t):
        return await q.answer(sc(f"💸 your max bid is {max_bid(a, t)} (you must afford a full squad)."),
                              show_alert=True)

    a.bid, a.bidder, a.bidder_by = amount, tkey(t.name), q.from_user.full_name
    cancel_jobs(ctx, f"lot-{chat_id}")
    ctx.job_queue.run_once(close_lot, a.bid_timer, chat_id=chat_id, name=f"lot-{chat_id}")
    await q.answer(sc(f"🔥 bid {amount} placed!"))
    await refresh_lot(ctx.bot, chat_id, a)


async def close_lot(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    a = AUCTIONS.get(chat_id)
    if not a or not a.current:
        return
    p, bidder, price = a.current, a.bidder, a.bid
    a.current = None  # late button presses are now rejected
    try:
        await ctx.bot.edit_message_reply_markup(chat_id, a.msg_id, reply_markup=None)
    except Exception:
        pass
    if bidder:
        t = a.teams[bidder]
        t.budget -= price
        t.squad.append((p, price))
        await send_media(ctx.bot, chat_id, "sold",
            "🔨 <b>SOLD!</b> 🎉\n"
            f"{ROLE_EMOJI[p['role']]} <b>{ment(p)}</b>"
            f"{'' if p['role'] == 'PL' else ' (' + ROLE_PLAIN[p['role']] + ')'}\n"
            f"👑 To: <b>{nm(t.name)}</b>\n"
            f"💰 Price: <b>{price}</b> pts · 🏦 Left: <b>{t.budget}</b>")
    else:
        a.unsold.append(p)
        await send_media(ctx.bot, chat_id, "unsold",
            f"❌ <b>UNSOLD</b> — <b>{pname(p)}</b> found no takers 😔")
    ctx.job_queue.run_once(nominate, a.gap, chat_id=chat_id, name=f"next-{chat_id}")


# ======================================================================
#  SAVED SQUADS: every finished auction keeps its squads under the tournament name
# ======================================================================
SQUADS_FILE = DATA / "auction_squads.json"


def _load_squads() -> dict:
    try:
        return json.loads(SQUADS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


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
        SQUADS_FILE.write_text(json.dumps(SQUADS, indent=1), encoding="utf-8")
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


async def deliver_reports(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Squad PDF per team -> group + that team's manager / vice-captain (DM);
    unsold list PDF -> group + owner DM + auctioneer DM."""
    a = AUCTIONS[chat_id]
    bot = ctx.bot
    if reports is None:
        return await say(bot, chat_id, "🚫 PDF reports need <b>reportlab</b>: run "
                                       "<code>pip install reportlab</code> and restart the bot.")
    await say(bot, chat_id, "📄 Making the PDFs...")
    missed = []
    for t in a.teams.values():
        rows = [(p["name"], p.get("username", ""), "" if p["role"] == "PL" else ROLE_PLAIN[p["role"]],
                 p["base"], price) for p, price in t.squad]
        try:
            data, fname = reports.squad_pdf(
                a.title or BRAND, t.name, _handle(t.manager_username, t.manager_name),
                _handle(t.vc_username, t.vc_name) if t.vc_username else "", rows,
                a.budget, t.budget, a.squad_size)
        except Exception as e:
            log.exception("squad pdf failed for %s", t.name)
            await say(bot, chat_id, f"⚠️ Couldn't make the PDF for <b>{nm(t.name)}</b>: {nm(e)}")
            continue
        cap = f"🏟 {t.name} — squad {len(t.squad)}/{a.squad_size} · 💰 left {t.budget}"
        try:
            await bot.send_document(chat_id, data, filename=fname, caption=fancy(cap), parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("group pdf failed: %s", e)
        for uid, user in ((t.manager_id, t.manager_username), (t.vc_id, t.vc_username)):
            if not user:
                continue
            if not uid:
                missed.append(f"{t.name}: @{user} (not seen in the group yet)")
                continue
            try:
                await bot.send_document(uid, data, filename=fname, caption=fancy(cap), parse_mode=ParseMode.HTML)
            except Exception:
                missed.append(f"{t.name}: @{user} (hasn't started the bot in DM)")

    rows = unsold_rows(a)
    try:
        data, fname = reports.unsold_pdf(a.title or BRAND, rows)
        cap = fancy(f"❌ Unsold players — {len(rows)}")
        await bot.send_document(chat_id, data, filename=fname, caption=cap, parse_mode=ParseMode.HTML)
        for uid, label in {x: l for x, l in ((a.host, "auctioneer"), (OWNER_ID, "owner")) if x}.items():
            try:
                await bot.send_document(uid, data, filename=fname, caption=cap, parse_mode=ParseMode.HTML)
            except Exception:
                missed.append(f"the {label} (hasn't started the bot in DM)")
    except Exception as e:
        log.exception("unsold pdf failed")
        await say(bot, chat_id, f"⚠️ Couldn't make the unsold list: {nm(e)}")

    note = ("✅ <b>All PDFs sent!</b> 📄 Team squads → group + managers/VCs · "
            "unsold list → group + owner + auctioneer.")
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
    "teams": ("🏟 <b>NUMBER OF TEAMS</b>", "How many teams will play? (2-30)"),
    "max": ("👥 <b>MAXIMUM PLAYERS PER TEAM</b>", "The most players a team can buy, e.g. 6"),
    "min": ("👥 <b>MINIMUM PLAYERS PER TEAM</b>", "The fewest players a team must have, e.g. 4"),
}


async def wiz_ask(bot, chat_id, a: Auction, step: str, banner: bool = False):
    a.wiz = step
    title, how = WIZ_ASK[step]
    text = f"{title}\n{how}\n<i>(send your answer in this chat · send “cancel” to stop)</i>"
    if banner:
        return await send_media(bot, chat_id, "welcome",
                                f"🏏 <b>{nm(BRAND)}</b> · AUCTION SETUP 🏆\n\n{text}")
    await say(bot, chat_id, text)


def flow_text(a: Auction) -> str:
    return (f"✅ <b>SETUP SAVED</b> — {nm(a.title)}\n"
            f"🏟 Teams: <b>{a.team_count}</b> · 👥 Players per team: <b>{a.min_players}–{a.squad_size}</b>\n\n"
            "📋 <b>NEXT STEPS</b>\n"
            f"1️⃣ Captains: {cmd('createteam')} [Team_Name] [@Captain_Username]\n"
            f"2️⃣ Vice-captains: {cmd('addvc')} [Team_Name] [@VC_Username]\n"
            "3️⃣ Sets: send a message with the players, one per line\n"
            f"<pre>{nm('1. NAME | USERNAME | USERID')}</pre>"
            f"then <b>reply</b> to it with {cmd('addaucset')} [Set_Number] [Set_Name]\n"
            f"4️⃣ Purse: {cmd('setpurse')} [amount]\n"
            f"5️⃣ Start: {cmd('startauction')}\n\n"
            f"⚙️ Change anything later (base price, gap, timer...): {cmd('editauc')}")


async def wizard_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE, a: Auction):
    msg, chat_id = update.message, update.effective_chat.id
    text = msg.text.strip()
    if text.lower() in ("cancel", "stop"):
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
        a.team_count = num
        await rep(msg, f"✅ Teams: <b>{num}</b>")
        return await wiz_ask(ctx.bot, chat_id, a, "max")
    if a.wiz == "max":
        if num is None or not 1 <= num <= 50:
            return await rep(msg, "🚫 Send a number from 1 to 50.")
        a.squad_size = num
        await rep(msg, f"✅ Maximum players per team: <b>{num}</b>")
        return await wiz_ask(ctx.bot, chat_id, a, "min")
    if a.wiz == "min":
        if num is None or not 1 <= num <= a.squad_size:
            return await rep(msg, f"🚫 Send a number from 1 to {a.squad_size} (the maximum).")
        a.min_players, a.wiz = num, ""
        await send_media(ctx.bot, chat_id, "sets", flow_text(a))


async def open_from_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """The Auction button of the group /start menu: starts the setup questions."""
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
    a = AUCTIONS[chat.id] = Auction(host=user.id, host_name=user.full_name, wiz_user=user.id)
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass
    await wiz_ask(ctx.bot, chat.id, a, "name", banner=True)


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
            ROLE: [CallbackQueryHandler(role_cb, pattern=r"^role:")],
        },
        fallbacks=[CommandHandler("cancelreg", cancelreg)],
        conversation_timeout=900,
    ))

    app.add_handler(CommandHandler(["createteam", "addteam"], addteam))
    app.add_handler(CommandHandler("editauc", editauc))
    app.add_handler(CommandHandler("squads", squads_cmd))
    app.add_handler(CallbackQueryHandler(squads_cb, pattern=r"^sq:"))
    app.add_handler(CommandHandler("setpurse", make_setter("budget")))
    for name, fn in [("help", help_cmd), ("openreg", openreg),
                     ("closereg", closereg), ("players", players_cmd),
                     ("startauction", startauction), ("cancel", cancel),
                     ("team", team), ("purse", purse), ("customsets", customsets),
                     ("auctions", auctions_cmd), ("auc_help", auc_help),
                     ("addaucset", addaucset), ("delaucset", delaucset), ("aucsets", aucsets),
                     ("settings", settings_cmd),
                     ("removeteam", removeteam),
                     ("addvc", addvc), ("changevc", changevc), ("end_auction", end_auction),
                     ("setname", setname), ("addsudo", addsudo), ("rmsudo", rmsudo),
                     ("setgroup", setgroup), ("setchannel", setchannel), ("setlog", setlog), ("setplayzone", setplayzone),
                     ("sudo", sudo_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    for key in SETTINGS:
        app.add_handler(CommandHandler("set" + key, make_setter(key)))
    any_media = filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.CaptionRegex(r"^/customsets") & filters.ChatType.GROUPS,
        customsets_file))
    app.add_handler(CallbackQueryHandler(on_source, pattern=r"^src:"))
    app.add_handler(CallbackQueryHandler(on_setup, pattern=r"^su:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, setup_input),
                    group=2)
    app.add_handler(CallbackQueryHandler(on_bid, pattern=r"^bid:"))
    # quietly learn who is who in the group (links @usernames of managers / VCs to Telegram ids)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, bind_seen), group=3)
