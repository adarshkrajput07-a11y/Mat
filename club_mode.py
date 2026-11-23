"""Clubs for Matrix Cricket Bot.

10 fixed clubs, paid for with the bot's Cash.
  Owning a club ........ 20000 Cash (/buyclub [Code])      - one club per player
  Joining a club ....... 2500 Cash  (/joinclub [Code])      - the club owner must agree first, then the player pays
  Max players .......... 12 per club (the owner counts as one)
  Changing the name .... 1000 Cash  (club owner; the club code changes with the name)
  Matrix Cricket Club (MCC) belongs to the bot owner by default.

Commands
  /clubs  /club [Code]  /myclub  /buyclub [Code]  /joinclub [Code]  /leaveclub [Code]
  /rmclubplayer [Code] (reply | @user | id)     club owner removes a player
  /setclublogo [Code]  (reply to a picture, or send the picture with this as caption)
  /clubranking (also /clubstandings)  /clubruns  /clubwickets  /clubschedule
  /clubmenu (also /clubhub)  the button hub: browse clubs, buy / join / leave with one tap, and flip between
                             Table · Runs · Wickets · Fixtures tabs without sending new commands
  Owner only: /setprice (monthly prize money: best club's captain + each player, best batter, best bowler)
              /clubsetup (buttons: overs, playing XI, Impact Player, Re-bat for every club fixture)
              /clubpannel (buttons)  /clubtime [Match_No] [date] [time]  /delclubmatch [Match_No]
              /setclubgroup (run inside a group to add it as a venue)

/clubpannel → Venues (add the groups where matches are played), Schedule → pick a match → Schedule
(date, time, venue buttons).  5 minutes before kickoff the bot pins a reminder in the venue group and posts a
"GROUP RESERVED" notice (/start, /1v1, /join are locked there until the match begins).

Club matches are played in the normal Team mode.  When a Team match ends and its two sides are made of
players of two clubs that have a scheduled fixture around that time, the bot records the result,
points, runs and wickets by itself (hook: on_team_finish in bot.py).

bot.py calls setup(<bot module>) and register(app) once.
"""
import html
import re
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, MessageHandler, filters

B = None                       # the bot.py module (set by setup)
DATA_KEY = "cricket_clubs"
DB = {}                        # persisted
PEND = {}                      # (chat_id, user_id) -> pending typed answer
PEND_TTL = 600
REQ_TTL = 24 * 3600            # a join request is forgotten after a day
TZ = "Asia/Kolkata"            # time zone used for the schedule
MAX_OWNED = 1                  # clubs one player may own (the bot owner can hand out more in /clubpannel)
PTS_WIN, PTS_TIE = 2, 1
WINDOW_BEFORE, WINDOW_AFTER = 30 * 60, 12 * 3600   # a played match is matched with a fixture inside this window

DEFAULT_FEES = {"own": 20000, "join": 2500, "change": 1000, "max": 12}
DEFAULT_PRIZES = {"captain": 0, "player": 0, "batter": 0, "bowler": 0}   # 0 = no prize until /setprice
PRIZE_LABEL = {"captain": "👑 Captain (club owner)", "player": "🧢 Each player", "batter": "🏏 Best batter",
               "bowler": "🎯 Best bowler"}
DEFAULT_RULES = {"overs": 5, "playing": 11, "impact": False, "rebat": False}
RULE_AFTER = 3 * 3600          # club rules stay on in the venue group this long after kickoff
FEE_LABEL = {"own": "Club ownership", "join": "Joining fee", "change": "Name change fee", "max": "Max players"}
DEFAULT_CLUBS = [
    ("𝑴𝒂𝒕𝒓𝒊𝒙 𝑪𝒓𝒊𝒄𝒌𝒆𝒕 𝑪𝒍𝒖𝒃", "MCC"),
    ("𝑻𝒊𝒕𝒂𝒏𝒔 𝑪𝒓𝒊𝒄𝒌𝒆𝒕 𝑪𝒍𝒖𝒃", "TCC"),
    ("𝑬𝒎𝒑𝒊𝒓𝒆 𝑬𝒍𝒆𝒗𝒆𝒏", "EE"),
    ("𝑩𝒍𝒂𝒛𝒆 𝑪𝒓𝒊𝒄𝒌𝒆𝒕 𝑪𝒍𝒖𝒃", "BCC"),
    ("𝑨𝒗𝒆𝒏𝒈𝒆𝒓𝒔 𝑬𝒍𝒆𝒗𝒆𝒏", "AE"),
    ("𝑨𝒔𝒕𝒓𝒂 𝑪𝒓𝒊𝒄𝒌𝒆𝒕 𝑪𝒍𝒖𝒃", "ACC"),
    ("𝑺𝒕𝒐𝒓𝒎 𝑹𝒊𝒅𝒆𝒓𝒔", "SR"),
    ("𝑰𝒏𝒇𝒆𝒓𝒏𝒐 𝑪𝒓𝒊𝒄𝒌𝒆𝒕 𝑪𝒍𝒖𝒃", "ICC"),
    ("𝑳𝒆𝒈𝒆𝒏𝒅𝒔 𝑼𝒏𝒊𝒕𝒆𝒅 𝑪𝒍𝒖𝒃", "LUC"),
    ("𝑨𝒍𝒑𝒉𝒂 𝑬𝒍𝒆𝒗𝒆𝒏", "ALE"),
]
BOT_OWNER_NAME = "Thalaa_0007"


# ------------------------------------------------------------------ plumbing
def setup(bot_module):
    global B, DB
    B = bot_module
    data = B.STORE.load_json(DATA_KEY, None)
    if not isinstance(data, dict) or "clubs" not in data:
        data = {"clubs": {}, "matches": [], "seq": 0, "venues": [], "fees": dict(DEFAULT_FEES)}
        for i, (name, code) in enumerate(DEFAULT_CLUBS, 1):
            data["clubs"][f"c{i}"] = _new_club(f"c{i}", name, code)
        owner = next(iter(sorted(getattr(B, "OWNER_IDS", ()))), None)
        if owner:  # Matrix Cricket Club belongs to the bot owner by default
            c = data["clubs"]["c1"]
            c["owner"], c["owner_name"] = owner, BOT_OWNER_NAME
            c["members"][str(owner)] = _member(BOT_OWNER_NAME)
    data.setdefault("matches", [])
    data.setdefault("seq", 0)
    data.setdefault("venues", [])
    if data.get("group") and not data["venues"]:  # older saves had one alert group
        data["venues"].append({"id": data["group"], "title": str(data["group"])})
    data["group"] = None
    for m in data["matches"]:
        m.setdefault("venue", None)
    data.setdefault("fees", {})
    for k, v in DEFAULT_FEES.items():
        data["fees"].setdefault(k, v)
    data.setdefault("rules", {})
    for k, v in DEFAULT_RULES.items():
        data["rules"].setdefault(k, v)
    data.setdefault("prizes", {})
    for k, v in DEFAULT_PRIZES.items():
        data["prizes"].setdefault(k, v)
    data.setdefault("awards", [])
    if not isinstance(data.get("season"), dict) or "month" not in data["season"]:
        data["season"] = _new_season(_month_key())
    DB = data
    _save()


def register(app):
    app.add_handler(CallbackQueryHandler(club_cb, pattern=r"^club:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & _Pending(), on_text))
    for names, fn in ((["clubs"], clubs_cmd), (["clubmenu", "clubhome", "clubhub"], clubmenu_cmd), (["club"], club_cmd), (["myclub"], myclub_cmd),
                      (["buyclub"], buyclub_cmd), (["joinclub"], joinclub_cmd), (["leaveclub"], leaveclub_cmd),
                      (["rmclubplayer"], rmclubplayer_cmd), (["setclublogo"], setclublogo_cmd),
                      (["clubranking", "clubrank", "clubstandings"], clubranking_cmd), (["clubruns"], clubruns_cmd),
                      (["clubwickets"], clubwickets_cmd), (["clubschedule"], clubschedule_cmd),
                      (["clubpannel", "clubpanel"], clubpanel_cmd), (["clubsetup", "clubrules"], clubsetup_cmd),
                      (["setprice"], setprice_cmd), (["clubawards", "clubprizes"], clubawards_cmd), (["clubtime"], clubtime_cmd),
                      (["delclubmatch"], delclubmatch_cmd), (["setclubgroup"], setclubgroup_cmd)):
        app.add_handler(CommandHandler(names, fn))
    app.add_handler(MessageHandler(filters.CaptionRegex(r"^/setclublogo"), setclublogo_cmd))
    # club-match lock: runs before every other handler (group -1)
    app.add_handler(CommandHandler(["start", "1v1", "join", "joingame"], _lock_cmd, filters=filters.ChatType.GROUPS),
                    group=-1)
    app.add_handler(CallbackQueryHandler(_lock_cb, pattern=r"^(mode:(solo|team)|auction:start|tour:new)$"), group=-1)
    app.job_queue.run_repeating(tick, interval=30, first=30)


def _save():
    try:
        B.STORE.save_json(DATA_KEY, DB)
    except Exception as e:
        print("[clubs] could not save:", e)
    try:
        B.backup_soon()
    except Exception:
        pass


class _Pending(filters.MessageFilter):
    """Lets a text message through only while this user is answering one of our questions."""

    def filter(self, message):
        if not message.from_user:
            return False
        key = (message.chat_id, message.from_user.id)
        st = PEND.get(key)
        if not st:
            return False
        if time.time() - st["ts"] > PEND_TTL:
            PEND.pop(key, None)
            return False
        return True


# ------------------------------------------------------------------ small helpers
def esc(s):
    return html.escape(str(s))


def plain(s):
    return unicodedata.normalize("NFKC", str(s))


def head(title):
    return f"<b>『 {title} 』</b>\n{B.LINE}\n"


def btn(text, data):
    return B.Btn(text, callback_data=data)


def kb(rows):
    return B.Markup(rows)


def fee(key):
    return int(DB["fees"].get(key, DEFAULT_FEES[key]))


def _member(name):
    return {"n": name, "r": 0, "w": 0}


def _new_club(cid, name, code):
    return {"id": cid, "name": name, "code": code, "owner": None, "owner_name": "", "members": {},
            "logo": None, "req": {}, "p": 0, "w": 0, "l": 0, "t": 0, "pts": 0, "runs": 0, "wkts": 0}


def money(n):
    return f"{int(n):,} 💵"


def by_id(cid):
    return DB["clubs"].get(cid)


def by_code(code):
    code = plain(code or "").strip().upper()
    for c in DB["clubs"].values():
        if c["code"].upper() == code:
            return c
    return None


def owned_by(uid):
    return [c for c in DB["clubs"].values() if c["owner"] == uid]


def member_of(uid):
    return [c for c in DB["clubs"].values() if str(uid) in c["members"]]


def is_staff(update):
    return B.owner_ok(update)


def can_manage(update, c):
    uid = update.effective_user.id
    return bool(c and (c["owner"] == uid or is_staff(update)))


def link(uid, name):
    return B.link(uid, name)


def club_title(c):
    return f"{esc(c['name'])} <b>[{esc(c['code'])}]</b>"


def gen_code(name, skip_id=None):
    words = re.findall(r"[A-Za-z0-9]+", plain(name))
    if len(words) >= 2:
        base = "".join(w[0] for w in words).upper()[:5]
    elif words:
        base = words[0][:3].upper()
    else:
        base = "CLB"
    taken = {c["code"].upper() for c in DB["clubs"].values() if c["id"] != skip_id}
    code, n = base, 2
    while code in taken:
        code = f"{base}{n}"
        n += 1
    return code


def name_taken(name, skip_id=None):
    key = plain(name).casefold().strip()
    return any(plain(c["name"]).casefold().strip() == key for c in DB["clubs"].values() if c["id"] != skip_id)


def valid_name(name):
    name = " ".join(name.split())
    if not 3 <= len(name) <= 30:
        return None, "Club name must be 3 to 30 characters."
    if not re.search(r"[A-Za-z0-9]", plain(name)):
        return None, "Club name needs some letters or numbers."
    return name, None


def charge(uid, name, amount):
    """Takes Cash from a player; False when they cannot pay."""
    if B.cash_total(uid) < amount:
        return False
    B.cash_add(uid, -amount, name)
    return True


def ranked():
    return sorted(DB["clubs"].values(), key=lambda c: (-c["pts"], -c["w"], -c["runs"], c["code"]))


def _tz():
    return ZoneInfo(TZ)


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, _tz()).strftime("%d %b %Y · %I:%M %p").lstrip("0")


def parse_when(text):
    """'25-11-2026 7:30pm', '25/11 19:30', 'today 8pm', 'tomorrow 9:15 pm' -> unix time (None when it can't be read)."""
    t = " ".join(text.lower().split())
    tm = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?|\b(\d{1,2})\s*(am|pm)\b", t)
    if not tm:
        return None
    if tm.group(1):
        h, mi, ap = int(tm.group(1)), int(tm.group(2)), tm.group(3)
    else:
        h, mi, ap = int(tm.group(4)), 0, tm.group(5)
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap == "pm" else 0)
    if h > 23 or mi > 59:
        return None
    rest = (t[:tm.start()] + " " + t[tm.end():]).strip()
    now = datetime.now(_tz())
    if rest == "today":
        day = now.date()
    elif rest in ("tomorrow", "tmrw", "tom"):
        day = (now + timedelta(days=1)).date()
    else:
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{2,4}))?", rest)
        if not m:
            return None
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else now.year
        if y < 100:
            y += 2000
        try:
            day = datetime(y, mo, d).date()
        except ValueError:
            return None
    try:
        return int(datetime(day.year, day.month, day.day, h, mi, tzinfo=_tz()).timestamp())
    except ValueError:
        return None


WHEN_HELP = "Send the date and time, e.g. <code>25-11-2026 7:30pm</code> or <code>tomorrow 8pm</code> (India time)."


def round_robin(ids):
    ids = list(ids)
    if len(ids) % 2:
        ids.append(None)
    n = len(ids)
    out = []
    for _ in range(n - 1):
        for i in range(n // 2):
            a, b = ids[i], ids[n - 1 - i]
            if a is not None and b is not None:
                out.append((a, b))
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]
    return out


def match_of(no):
    for m in DB["matches"]:
        if m["no"] == no:
            return m
    return None


def cname(cid):
    c = by_id(cid)
    return c["code"] if c else "?"


def mline(m):
    a, b = cname(m["a"]), cname(m["b"])
    if m["status"] == "done":
        return f"✅ <b>M{m['no']}</b> · {esc(a)} vs {esc(b)} — {esc(m['res']['text'])}"
    when = f"🗓 {fmt_ts(m['ts'])}" if m.get("ts") else "⏳ date & time not set"
    venue = f" · 🏟 {esc(vtitle(m['venue']))}" if m.get("venue") else (" · 🏟 venue not set" if m.get("ts") else "")
    return f"🏏 <b>M{m['no']}</b> · {esc(a)} vs {esc(b)} · {when}{venue}"


async def _reply(msg, text, markup=None):
    return await msg.reply_text(text, parse_mode=B.HTML, reply_markup=markup, disable_web_page_preview=True)


async def _edit(q, text, rows=None):
    try:
        await q.edit_message_text(text, parse_mode=B.HTML, reply_markup=kb(rows) if rows else None,
                                  disable_web_page_preview=True)
    except TelegramError as e:
        if "not modified" not in str(e).lower():
            print("[clubs] edit failed:", e)


def find_user(token):
    """'@name' or a numeric id -> (uid, name) or None."""
    token = token.strip()
    if token.startswith("@"):
        return B.known_unames.get(token.lstrip("@").lower())
    if token.isdigit():
        return int(token), "Player"
    return None


def target_of(update, words):
    """The player a command points at: replied message, @username or id.  Returns (uid, name, remaining words)."""
    msg = update.message
    rest = []
    hit = None
    for w in words:
        if hit is None and (w.startswith("@") or w.isdigit()):
            hit = find_user(w)
            if hit is None:
                rest.append(w)
        else:
            rest.append(w)
    if hit is None and msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        hit = (u.id, u.full_name)
    return (hit[0], hit[1], rest) if hit else (None, None, rest)


def locate(user_id, words, mine_only=True):
    """Picks a club from a code in `words`, or the user's only owned / joined club."""
    for w in words:
        c = by_code(w)
        if c:
            return c, None
    if words:
        return None, f"No club has the code <code>{esc(words[0])}</code>. See /clubs."
    pool = owned_by(user_id) or (member_of(user_id) if mine_only else [])
    if len(pool) == 1:
        return pool[0], None
    return None, "Tell me the club code, e.g. <code>MCC</code>. See /clubs."


# ------------------------------------------------------------------ premium views (text + buttons)
def bar(n, total, width=10):
    total = max(1, total)
    filled = min(width, round(width * n / total))
    return "▰" * filled + "▱" * (width - filled)


def _plain_alert(s):
    """HTML message -> plain text (popup alerts cannot show tags)."""
    return html.unescape(re.sub(r"<[^>]+>", "", s))[:190]


TABS = (("rank", "🏆 Table"), ("runs", "🏏 Runs"), ("wkts", "🎯 Wickets"), ("sch", "🗓 Fixtures"))


def tabs_rows(active=None):
    return [[btn(("✦ " if active == k else "") + t, f"club:v:{k}") for k, t in TABS[i:i + 2]] for i in (0, 2)]


def foot_row():
    return [btn("🏠 Clubs", "club:v:list"), btn("👤 My Club", "club:v:my"), btn("✖ Close", "club:v:x")]


async def _show(update, view):
    text, rows = view
    return await _reply(update.message, text, kb(rows))


def _club_line(c):
    n = len(c["members"])
    if c["owner"]:
        who = f"👑 {link(c['owner'], c['owner_name'] or 'Owner')}"
        tail = f"\n    👥 {bar(n, fee('max'), 8)} <b>{n}/{fee('max')}</b>"
    else:
        who = f"🆓 <i>Available</i> · <b>{money(fee('own'))}</b>"
        tail = ""
    return f"<b>{esc(c['code'])}</b> · {esc(c['name'])}\n    {who}{tail}"


def v_list(uid):
    body = "\n\n".join(_club_line(c) for c in DB["clubs"].values())
    text = (head("🏏 CRICKET CLUBS") + f"<blockquote>{body}</blockquote>{B.LINE}\n"
            f"👑 Own <b>{money(fee('own'))}</b> · 🙋 Join <b>{money(fee('join'))}</b> · 👥 Max <b>{fee('max')}</b>\n"
            "<i>⭐ your club · 👑 owned · 🆓 available — tap one 👇</i>")
    mine = {c["id"] for c in member_of(uid)}
    items = list(DB["clubs"].values())
    rows = []
    for i in range(0, len(items), 3):
        rows.append([btn(f"{'⭐' if c['id'] in mine else '👑' if c['owner'] else '🆓'} {c['code']}", f"club:v:c:{c['id']}")
                     for c in items[i:i + 3]])
    rows += tabs_rows()
    rows.append([btn("💰 Prizes", "club:v:prz")] + foot_row()[1:])
    return text, rows


def v_club(c, uid, banner=""):
    n, mx = len(c["members"]), fee("max")
    rank = ranked().index(c) + 1
    plist = []
    for i, (u, m) in enumerate(c["members"].items(), 1):
        crown = " 👑" if str(c["owner"]) == u else ""
        plist.append(f"{i:02d}. {link(int(u), m['n'])}{crown} — {m['r']} runs · {m['w']} wkts")
    tr, tw = _top_player(c, "r"), _top_player(c, "w")
    stars = ""
    if tr or tw:
        stars = "\n⭐ " + " · ".join(x for x in (
            f"Top bat <b>{esc(tr['n'])}</b> ({tr['r']})" if tr else "",
            f"Top bowler <b>{esc(tw['n'])}</b> ({tw['w']})" if tw else "") if x)
    text = (head(f"🏏 {c['code']}") + f"<b>{esc(c['name'])}</b>\n"
            f"👑 Owner: {link(c['owner'], c['owner_name']) if c['owner'] else '<i>none yet</i>'}\n"
            f"🏅 Rank <b>#{rank}</b> · Pts <b>{c['pts']}</b>\n"
            f"📊 P{c['p']} · W{c['w']} · L{c['l']} · T{c['t']}\n"
            f"🏏 Runs <b>{c['runs']:,}</b> · 🎯 Wickets <b>{c['wkts']:,}</b>{stars}\n{B.LINE}\n"
            f"👥 <b>SQUAD</b> {bar(n, mx)} <b>{n}/{mx}</b>\n"
            + ("<blockquote>" + "\n".join(plist) + "</blockquote>" if plist else "<i>No players yet.</i>"))
    if banner:
        text = banner + "\n\n" + text
    rows = []
    if str(uid) in c["members"]:
        if c["owner"] == uid:
            rows.append([btn(f"✏️ Rename · {fee('change'):,} 💵", f"club:o:rn:{c['id']}")])
        else:
            rows.append([btn("🚪 Leave Club", f"club:v:lv:{c['id']}")])
    elif not c["owner"]:
        rows.append([btn(f"👑 Buy Club · {fee('own'):,} 💵", f"club:v:buy:{c['id']}")])
    elif n < mx:
        rows.append([btn(f"🙋 Request to Join · {fee('join'):,} 💵", f"club:v:join:{c['id']}")])
    else:
        rows.append([btn("🚫 Club Full", "club:v:noop")])
    rows += tabs_rows()
    rows.append([btn("⬅️ All Clubs", "club:v:list"), btn("✖ Close", "club:v:x")])
    return text, rows


async def _send_logo(ctx, chat_id, c):
    if not c.get("logo"):
        return
    kind, fid = c["logo"].split(":", 1)
    send = {"photo": ctx.bot.send_photo, "video": ctx.bot.send_video, "animation": ctx.bot.send_animation}.get(kind)
    try:
        await send(chat_id, fid, caption=f"<b>{esc(c['name'])}</b> [{esc(c['code'])}]", parse_mode=B.HTML)
    except Exception:
        pass


def _my_text(uid):
    mine = member_of(uid)
    if not mine:
        return None
    out = []
    for c in mine:
        role = "👑 Owner" if c["owner"] == uid else "🏏 Player"
        m = c["members"][str(uid)]
        out.append(f"<b>{esc(c['code'])}</b> · {esc(c['name'])}\n"
                   f"   {role} · 👥 {len(c['members'])}/{fee('max')} · 🏅 #{ranked().index(c) + 1}\n"
                   f"   Your runs <b>{m['r']}</b> · wickets <b>{m['w']}</b>")
    return head("🏏 MY CLUBS") + "<blockquote>" + "\n\n".join(out) + "</blockquote>" + B.LINE + \
        f"\n💵 Cash: <b>{B.cash_total(uid):,}</b>"


def v_my(uid):
    text = _my_text(uid)
    if not text:
        return (head("🏏 MY CLUBS") + "You are not in any club yet.\n"
                f"👑 Own one for <b>{money(fee('own'))}</b> or 🙋 join one for <b>{money(fee('join'))}</b>.\n"
                f"💵 Cash: <b>{B.cash_total(uid):,}</b>",
                [[btn("🏏 Browse Clubs", "club:v:list")]] + tabs_rows() + [[btn("✖ Close", "club:v:x")]])
    rows = []
    for c in member_of(uid):
        rows.append([btn(f"🏏 {c['code']}", f"club:v:c:{c['id']}")] +
                    ([btn(f"✏️ Rename · {fee('change'):,} 💵", f"club:o:rn:{c['id']}")] if c["owner"] == uid
                     else [btn("🚪 Leave", f"club:v:lv:{c['id']}")]))
    rows += tabs_rows()
    rows.append([btn("🏠 Clubs", "club:v:list"), btn("✖ Close", "club:v:x")])
    return text, rows


def v_pick(kind, uid):
    """/joinclub or /buyclub without a code: all clubs as buttons."""
    join = kind == "join"
    items = list(DB["clubs"].values())
    rows = []
    for i in range(0, len(items), 3):
        row = []
        for c in items[i:i + 3]:
            if str(uid) in c["members"]:
                ic = "⭐"
            elif join:
                ic = "🚫" if not c["owner"] else "🔒" if len(c["members"]) >= fee("max") else "🙋"
            else:
                ic = "🆓" if not c["owner"] else "🔒"
            row.append(btn(f"{ic} {c['code']}", f"club:v:{'join' if join else 'buy'}:{c['id']}"))
        rows.append(row)
    rows.append([btn("🏠 All Clubs", "club:v:list"), btn("✖ Close", "club:v:x")])
    if join:
        text = (head("🙋 JOIN A CLUB") + f"Pick the club you want to join · fee <b>{money(fee('join'))}</b>\n"
                "<i>🙋 open · 🔒 full · 🚫 no owner · ⭐ your club</i>")
    else:
        text = (head("👑 BUY A CLUB") + f"Pick a club to own · price <b>{money(fee('own'))}</b>\n"
                "<i>🆓 available · 🔒 already owned · ⭐ your club</i>")
    return text, rows


def v_confirm(kind, c, uid):
    if kind == "buy":
        return (head("👑 BUY CLUB") + f"🏏 {club_title(c)}\n💵 Price: <b>{money(fee('own'))}</b>\n"
                f"💰 Your Cash: <b>{money(B.cash_total(uid))}</b>\n{B.LINE}\nConfirm your purchase?",
                [[btn("✅ Confirm Purchase", f"club:v:buyok:{c['id']}"), btn("⬅️ Cancel", f"club:v:c:{c['id']}")]])
    return (head("🚪 LEAVE CLUB") + f"Leave {club_title(c)}?\n<i>The joining fee is not refunded.</i>",
            [[btn("✅ Yes, Leave", f"club:v:lvok:{c['id']}"), btn("⬅️ Stay", f"club:v:c:{c['id']}")]])


# ------------------------------------------------------------------ public commands
async def clubs_cmd(update, ctx):
    """/clubs - all clubs, their owners and how full they are."""
    await _show(update, v_list(update.effective_user.id))


async def clubmenu_cmd(update, ctx):
    """/clubmenu - the club hub."""
    await _show(update, v_list(update.effective_user.id))


async def club_cmd(update, ctx):
    """/club [Code] - one club in detail."""
    c, err = locate(update.effective_user.id, ctx.args or [])
    if err:
        return await _reply(update.message, err, kb([[btn("🏏 Browse Clubs", "club:v:list")]]))
    await _send_logo(ctx, update.effective_chat.id, c)
    await _show(update, v_club(c, update.effective_user.id))


async def myclub_cmd(update, ctx):
    """/myclub - the clubs you are in."""
    await _show(update, v_my(update.effective_user.id))


def _buy_core(update, c):
    """Buys a free club.  Returns an error text, or None on success."""
    user = update.effective_user
    if c["owner"]:
        return f"{club_title(c)} already has an owner."
    if len(owned_by(user.id)) >= MAX_OWNED and not is_staff(update):
        return f"👑 You already own a club. One player can own {MAX_OWNED}."
    price = fee("own")
    if B.cash_total(user.id) < price:
        return f"💵 You need <b>{money(price)}</b> to own a club. You have <b>{money(B.cash_total(user.id))}</b>."
    charge(user.id, user.full_name, price)
    c["owner"], c["owner_name"] = user.id, user.full_name
    c["members"].setdefault(str(user.id), _member(user.full_name))
    _save()
    return None


async def buyclub_cmd(update, ctx):
    """/buyclub [Code] - become the owner of a free club."""
    msg, user = update.message, update.effective_user
    c = by_code(ctx.args[0]) if ctx.args else None
    if not c:
        return await _show(update, v_pick("buy", user.id))
    err = _buy_core(update, c)
    if err:
        return await _reply(msg, err)
    await _reply(msg, head("👑 NEW CLUB OWNER") + f"🏏 {club_title(c)}\n👑 Owner: {link(user.id, user.full_name)}\n"
                 f"💸 Paid <b>{money(fee('own'))}</b>\n{B.LINE}\nPlayers can now ask to join with "
                 f"<code>/joinclub {esc(c['code'])}</code>.",
                 kb([[btn("🏏 Open Club", f"club:v:c:{c['id']}"), btn("✏️ Rename", f"club:o:rn:{c['id']}")]]))


def _join_check(user, c):
    """Validates a join request and records it.  Returns an error text, or None when the request was recorded."""
    if not c["owner"]:
        return f"{club_title(c)} has no owner yet, so nobody can approve you."
    if str(user.id) in c["members"]:
        return "✅ You are already in this club."
    if len(c["members"]) >= fee("max"):
        return f"🚫 {club_title(c)} is full ({fee('max')}/{fee('max')})."
    if B.cash_total(user.id) < fee("join"):
        return f"💵 Joining costs <b>{money(fee('join'))}</b>. You have <b>{money(B.cash_total(user.id))}</b>."
    old = c["req"].get(str(user.id))
    if old and time.time() - old["ts"] < REQ_TTL:
        return "⏳ Your request is already waiting for the club owner."
    c["req"][str(user.id)] = {"n": user.full_name, "ts": int(time.time()), "st": "wait"}
    _save()
    return None


async def _tell(ctx, uid, req, text, markup=None):
    """Sends a message to a player's DM; if the bot can't DM them, posts it in the chat where they asked."""
    for chat in (uid, (req or {}).get("chat")):
        if not chat:
            continue
        try:
            return await ctx.bot.send_message(chat, text, parse_mode=B.HTML, reply_markup=markup,
                                              disable_web_page_preview=True)
        except TelegramError:
            continue
    return None


async def _post_join(ctx, msg, user, c):
    """The join request goes to the club owner's DM with the bot, with Accept / Decline buttons."""
    req = c["req"].get(str(user.id))
    if req:
        req["chat"] = msg.chat_id
        _save()
    text = (head("🙋 JOIN REQUEST") + f"👤 {link(user.id, user.full_name)} wants to join {club_title(c)}\n"
            f"💵 Joining fee: <b>{money(fee('join'))}</b> (paid by the player after you accept)\n{B.LINE}\n"
            "👑 Do you agree?")
    markup = kb([[btn("✅ Accept", f"club:j:ok:{c['id']}:{user.id}"),
                  btn("❌ Decline", f"club:j:no:{c['id']}:{user.id}")]])
    try:
        await ctx.bot.send_message(c["owner"], text, parse_mode=B.HTML, reply_markup=markup,
                                   disable_web_page_preview=True)
    except TelegramError:  # the owner never started the bot in DM -> ask in the chat instead
        return await _reply(msg, text + "\n<i>📭 The owner hasn't opened the bot's DM yet, so the request is here. "
                            "Owner: please /start the bot in DM to get requests privately.</i>", markup)
    return await _reply(msg, f"📨 Request sent to {link(c['owner'], c['owner_name'])}'s DM. "
                        "You'll get a message here or in the bot DM once they answer.\n"
                        "<i>Tip: /start the bot in DM so the payment button can reach you.</i>")


async def joinclub_cmd(update, ctx):
    """/joinclub [Code] - ask the club owner to let you in (you pay after the owner agrees)."""
    msg, user = update.message, update.effective_user
    c = by_code(ctx.args[0]) if ctx.args else None
    if not c:
        return await _show(update, v_pick("join", user.id))
    err = _join_check(user, c)
    if err:
        return await _reply(msg, err)
    return await _post_join(ctx, msg, user, c)


def _leave_core(user, c):
    if str(user.id) not in c["members"]:
        return "You are not in that club."
    if c["owner"] == user.id:
        return "👑 A club owner cannot leave. Ask the bot owner to change the owner."
    c["members"].pop(str(user.id), None)
    _save()
    return None


async def leaveclub_cmd(update, ctx):
    """/leaveclub [Code] - leave a club (owners cannot; ask the bot owner)."""
    user = update.effective_user
    c, err = locate(user.id, ctx.args or [])
    if err:
        return await _reply(update.message, err)
    err = _leave_core(user, c)
    if err:
        return await _reply(update.message, err)
    await _reply(update.message, f"👋 You left {club_title(c)}. The joining fee is not refunded.",
                 kb([[btn("🏏 Browse Clubs", "club:v:list")]]))


async def view_cb(update, ctx, parts):
    """Buttons of the public club views: club:v:<act>[:club_id]."""
    q = update.callback_query
    user = q.from_user
    act = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else None
    if act == "noop":
        return await q.answer()
    if act == "x":
        await q.answer()
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return
    if act in ("list", "my", "rank", "runs", "wkts", "sch", "pickj", "pickb", "prz"):
        await q.answer()
        view = {"prz": v_prizes, "pickj": lambda: v_pick("join", user.id), "pickb": lambda: v_pick("buy", user.id),
                "list": lambda: v_list(user.id), "my": lambda: v_my(user.id), "rank": v_rank,
                "runs": lambda: v_stat("r"), "wkts": lambda: v_stat("w"), "sch": v_sched}[act]()
        return await _edit(q, *view)
    c = by_id(arg) if arg else None
    if not c:
        return await q.answer("That club no longer exists.", show_alert=True)
    if act == "c":
        await q.answer()
        return await _edit(q, *v_club(c, user.id))
    if act == "buy":
        if c["owner"]:
            return await q.answer("This club already has an owner.", show_alert=True)
        await q.answer()
        return await _edit(q, *v_confirm("buy", c, user.id))
    if act == "lv":
        if str(user.id) not in c["members"] or c["owner"] == user.id:
            return await q.answer("You can't leave this club.", show_alert=True)
        await q.answer()
        return await _edit(q, *v_confirm("lv", c, user.id))
    if act == "buyok":
        err = _buy_core(update, c)
        if err:
            return await q.answer(_plain_alert(err), show_alert=True)
        await q.answer("👑 The club is yours!")
        return await _edit(q, *v_club(c, user.id, banner=f"🎉 <b>Congratulations {esc(user.full_name)}!</b> "
                                                          f"You paid <b>{money(fee('own'))}</b>."))
    if act == "join":
        err = _join_check(user, c)
        if err:
            return await q.answer(_plain_alert(err), show_alert=True)
        await q.answer("🙋 Request sent to the club owner!")
        return await _post_join(ctx, q.message, user, c)
    if act == "lvok":
        err = _leave_core(user, c)
        if err:
            return await q.answer(_plain_alert(err), show_alert=True)
        await q.answer("👋 You left the club.")
        return await _edit(q, *v_club(c, user.id, banner="👋 You left the club. The joining fee is not refunded."))


async def rmclubplayer_cmd(update, ctx):
    """/rmclubplayer [Code] (reply | @user | id) - the club owner removes a player."""
    msg, user = update.message, update.effective_user
    uid, name, rest = target_of(update, ctx.args or [])
    c, err = locate(user.id, rest, mine_only=False)
    if err or not uid:
        return await _reply(msg, (err + "\n\n" if err else "") +
                            "Usage: reply to the player with <code>/rmclubplayer [Code]</code> "
                            "(or <code>/rmclubplayer [Code] @username</code>).")
    if not can_manage(update, c):
        return await _reply(msg, "👑 Only the club owner can remove players.")
    if uid == c["owner"]:
        return await _reply(msg, "The club owner can't be removed.")
    mem = c["members"].pop(str(uid), None)
    if not mem:
        return await _reply(msg, "That player is not in this club.")
    _save()
    await _reply(msg, f"🚫 {link(uid, mem['n'])} was removed from {club_title(c)}. No refund is given.")


async def setclublogo_cmd(update, ctx):
    """/setclublogo [Code] - reply to a picture/GIF, or send it with this as caption."""
    msg, user = update.message, update.effective_user
    words = (msg.text or msg.caption or "").split()[1:]
    usage = ("🖼 Reply to the logo with <code>/setclublogo [Club_Code]</code>\n"
             "or send the picture with that as its caption.")
    c, err = locate(user.id, words, mine_only=False)
    if err:
        return await _reply(msg, err + "\n\n" + usage)
    if not can_manage(update, c):
        return await _reply(msg, "👑 Only the club owner or the bot owner can set a club logo.")
    val = B.media_of(msg) or B.media_of(msg.reply_to_message)
    if not val:
        return await _reply(msg, usage)
    cost = 0 if (is_staff(update) and c["owner"] != user.id) else (fee("change") if c.get("logo") else 0)
    if cost:
        if not charge(user.id, user.full_name, cost):
            return await _reply(msg, f"💵 Changing the logo costs <b>{money(cost)}</b>. "
                                f"You have <b>{money(B.cash_total(user.id))}</b>.")
    c["logo"] = val
    _save()
    await _reply(msg, f"🖼 Logo saved for {club_title(c)}." + (f"\n💸 Paid <b>{money(cost)}</b>" if cost else ""))


# ------------------------------------------------------------------ ranking, runs, wickets, schedule
def _medal(i):
    return ["🥇", "🥈", "🥉"][i] if i < 3 else f"<b>{i + 1}.</b>"


def v_rank():
    rows = []
    for i, c in enumerate(ranked()):
        rows.append(f"{_medal(i)} <b>{esc(c['code'])}</b> ▸ <b>{c['pts']}</b> pts · "
                    f"P{c['p']} W{c['w']} L{c['l']} T{c['t']}")
    text = (head("🏆 CLUB RANKING") + "<blockquote>" + "\n".join(rows) + "</blockquote>" +
            f"{B.LINE}\nWin = {PTS_WIN} pts · Tie = {PTS_TIE} pt")
    return text, tabs_rows("rank") + [foot_row()]


def _top_player(c, key):
    best = max(c["members"].values(), key=lambda m: m[key], default=None)
    return best if best and best[key] > 0 else None


def v_stat(key):
    title, unit, icon = (("CLUB RUNS", "runs", "🏏") if key == "r" else ("CLUB WICKETS", "wickets", "🎯"))
    pool = sorted(DB["clubs"].values(), key=lambda c: (-c["runs" if key == "r" else "wkts"], c["code"]))
    rows = []
    for i, c in enumerate(pool):
        val = c["runs" if key == "r" else "wkts"]
        top = _top_player(c, key)
        extra = f"\n      ⭐ {esc(top['n'])} · <b>{top[key]}</b>" if top else ""
        rows.append(f"{_medal(i)} <b>{esc(c['code'])}</b> · {esc(c['name'])} — <b>{val:,}</b> {unit}{extra}")
    text = head(f"{icon} {title}") + "<blockquote>" + "\n".join(rows) + "</blockquote>"
    return text, tabs_rows("runs" if key == "r" else "wkts") + [foot_row()]


def v_sched():
    ms = DB["matches"]
    if not ms:
        return (head("🗓 CLUB SCHEDULE") + "No club matches yet. The bot owner builds the schedule in /clubpannel.",
                tabs_rows("sch") + [foot_row()])
    todo = sorted((m for m in ms if m["status"] != "done"),
                  key=lambda m: (m.get("ts") is None, m.get("ts") or 0, m["no"]))
    done = [m for m in ms if m["status"] == "done"][-5:]
    played = sum(1 for m in ms if m["status"] == "done")
    text = head("🗓 CLUB SCHEDULE") + f"📈 Played <b>{played}/{len(ms)}</b> {bar(played, len(ms))}\n📜 {rules_line()}\n\n"
    text += "<blockquote>" + ("\n".join(mline(m) for m in todo[:15]) or "Nothing left to play 🎉") + "</blockquote>"
    if len(todo) > 15:
        text += f"<i>…and {len(todo) - 15} more</i>\n"
    if done:
        text += f"{B.LINE}\n<b>LATEST RESULTS</b>\n<blockquote>" + "\n".join(mline(m) for m in done) + "</blockquote>"
    return text, tabs_rows("sch") + [foot_row()]


async def clubranking_cmd(update, ctx):
    """/clubranking - points table of the clubs."""
    await _show(update, v_rank())


async def clubruns_cmd(update, ctx):
    """/clubruns - clubs ranked by total runs."""
    await _show(update, v_stat("r"))


async def clubwickets_cmd(update, ctx):
    """/clubwickets - clubs ranked by total wickets."""
    await _show(update, v_stat("w"))


async def clubschedule_cmd(update, ctx):
    """/clubschedule - upcoming club matches and the latest results."""
    await _show(update, v_sched())


# ------------------------------------------------------------------ bot-owner commands
async def clubtime_cmd(update, ctx):
    """/clubtime [Match_No] [date] [time] (owner)."""
    if not is_staff(update):
        return
    a = ctx.args or []
    if len(a) < 3 or not a[0].lstrip("Mm").isdigit():
        return await _reply(update.message, "Usage: <code>/clubtime [Match_No] [date] [time]</code>\n" + WHEN_HELP)
    m = match_of(int(a[0].lstrip("Mm")))
    if not m or m["status"] == "done":
        return await _reply(update.message, "No open match with that number. See /clubschedule.")
    ts = parse_when(" ".join(a[1:]))
    if not ts or ts < time.time():
        return await _reply(update.message, "I can't read that, or the time has passed.\n" + WHEN_HELP)
    await _set_slot(ctx, m, ts)
    await _reply(update.message, f"🗓 Set.\n{mline(m)}")


async def delclubmatch_cmd(update, ctx):
    """/delclubmatch [Match_No] (owner)."""
    if not is_staff(update):
        return
    a = ctx.args or []
    m = match_of(int(a[0].lstrip("Mm"))) if a and a[0].lstrip("Mm").isdigit() else None
    if not m:
        return await _reply(update.message, "Usage: <code>/delclubmatch [Match_No]</code>")
    await _clear_notice(ctx, m)
    DB["matches"].remove(m)
    _save()
    await _reply(update.message, f"🗑 Match {m['no']} deleted.")


async def setclubgroup_cmd(update, ctx):
    """/setclubgroup (owner, inside a group) - adds this group as a venue (same as /clubpannel -> Venues)."""
    if not is_staff(update):
        return
    chat = update.effective_chat
    if chat.type == "private":
        return await _reply(update.message, "Run this inside the group you want to use as a venue.")
    if not any(v["id"] == chat.id for v in DB["venues"]):
        DB["venues"].append({"id": chat.id, "title": chat.title or str(chat.id)})
        _save()
    await _reply(update.message, "🏟 This group is now a club-match venue. Pick it for matches in /clubpannel.")


# ------------------------------------------------------------------ venues, guided scheduling (date -> time -> venue)
def _venue(vid):
    return next((v for v in DB["venues"] if v["id"] == vid), None)


def vtitle(vid):
    v = _venue(vid)
    return v["title"] if v else "—"


def _wts(d, h, mi):
    return int(datetime(d.year, d.month, d.day, h, mi, tzinfo=_tz()).timestamp())


def _hlabel(h):
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


def _dparse(x):
    return datetime.strptime(x, "%Y%m%d").date()


def _wtop(m, step, what):
    return (head(f"📅 M{m['no']} · SCHEDULE") + f"{esc(cname(m['a']))} vs {esc(cname(m['b']))}\n"
            f"<b>Step {step}/3 · {what}</b> · <i>{esc(TZ)}</i>\n\n")


def _w_date(m):
    no = m["no"]
    today = datetime.now(_tz()).date()
    rows, row = [], []
    for off in range(8):
        d = today + timedelta(days=off)
        lab = "Today" if off == 0 else "Tomorrow" if off == 1 else d.strftime("%a")
        row.append(btn(f"{lab} · {d.strftime('%d %b')}", f"club:p:sd:{no}:{d.strftime('%Y%m%d')}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    rows.append([btn("✍️ Other date", f"club:p:sdc:{no}")])
    rows.append([btn("⬅️ Back", f"club:p:m:{no}")])
    return _wtop(m, 1, "Date") + "Pick the match day 👇", rows


def _w_time(m, d):
    no, now = m["no"], time.time()
    rows, row = [], []
    for h in range(8, 24):
        if _wts(d, h, 0) <= now:
            continue
        row.append(btn(_hlabel(h), f"club:p:st:{no}:{d.strftime('%Y%m%d')}:{h:02d}00"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("✍️ Other time", f"club:p:stc:{no}:{d.strftime('%Y%m%d')}")])
    rows.append([btn("⬅️ Date", f"club:p:sw:{no}")])
    return _wtop(m, 2, "Time") + f"📅 {d.strftime('%a, %d %b %Y')}\nPick the start time 👇", rows


def _busy_at(m, vid, ts):
    return any(o is not m and o.get("venue") == vid and o.get("ts") and abs(o["ts"] - ts) < 20 * 60
               and o["status"] != "done" for o in DB["matches"])


def _w_venue(m, d, hm):
    no = m["no"]
    ts = _wts(d, int(hm[:2]), int(hm[2:]))
    rows = [[btn(("⚠️ " if _busy_at(m, v["id"], ts) else "🏟 ") + v["title"][:28],
                 f"club:p:sv:{no}:{d.strftime('%Y%m%d')}:{hm}:{n}")] for n, v in enumerate(DB["venues"])]
    rows.append([btn("⬅️ Time", f"club:p:sd:{no}:{d.strftime('%Y%m%d')}")])
    note = "" if DB["venues"] else "\n<b>No venues yet.</b> Add one in /clubpannel → Venues first."
    warn = "\n<i>⚠️ = another match is within 20 min there.</i>" if any(
        _busy_at(m, v["id"], ts) for v in DB["venues"]) else ""
    return _wtop(m, 3, "Venue") + f"🗓 {fmt_ts(ts)}\nPick the venue 👇" + note + warn, rows


def _clash(m):
    o = next((o for o in DB["matches"] if o is not m and m.get("venue") and o.get("venue") == m["venue"]
              and o.get("ts") and m.get("ts") and abs(o["ts"] - m["ts"]) < 20 * 60 and o["status"] != "done"), None)
    return f"\n⚠️ M{o['no']} is at the same venue within 20 minutes." if o else ""


async def _set_slot(ctx, m, ts=None, venue=None):
    """Sets date/time and/or venue; the old reminder messages are removed and the countdown restarts."""
    await _clear_notice(ctx, m)
    if ts is not None:
        m["ts"] = ts
    if venue is not None:
        m["venue"] = venue
    m["w5"] = m["w0"] = 0
    _save()
    return _clash(m)


def _panel_venues():
    lines = [f"{n}. {esc(v['title'])} <code>{v['id']}</code>" for n, v in enumerate(DB["venues"], 1)] or ["No venues yet."]
    rows = [[btn(f"❌ {n}", f"club:p:vd:{n - 1}") for n in range(s, min(s + 5, len(DB["venues"]) + 1))]
            for s in range(1, len(DB["venues"]) + 1, 5)]
    rows.append([btn("➕ Add venue", "club:p:va")])
    rows.append([btn("⬅️ Back", "club:p:main")])
    return head("🏟 VENUES") + "\n".join(lines) + "\n\n<i>A venue is a group where club matches are played. " \
        "Add the bot to the group first (send /gid there to get its id).</i>", rows


async def _check_venues(ctx, text, here):
    ok, bad = [], []
    for tok in re.split(r"[,\s]+", text.strip()):
        if not tok or tok.lower() in ("skip", "none", "no"):
            continue
        if tok.lower() == "here":
            cid = here
        else:
            try:
                cid = int(tok)
            except ValueError:
                bad.append((tok, "not a number"))
                continue
        if any(v["id"] == cid for v in ok):
            continue
        try:
            chat = await ctx.bot.get_chat(cid)
        except TelegramError:
            bad.append((tok, "the bot can't see this group"))
            continue
        if chat.type not in ("group", "supergroup"):
            bad.append((tok, "not a group"))
            continue
        ok.append({"id": cid, "title": chat.title or str(cid)})
    return ok, bad


def _venue_report(ok, bad):
    return "".join(f"✅ {esc(v['title'])}\n" for v in ok) + "".join(f"⚠️ {esc(t)} — {why}\n" for t, why in bad)


# ------------------------------------------------------------------ /clubpannel  (bot owner, buttons)
def _panel_main():
    owned = sum(1 for c in DB["clubs"].values() if c["owner"])
    open_m = sum(1 for m in DB["matches"] if m["status"] != "done")
    text = (head("🛠 CLUB PANEL") + f"🏏 Clubs: <b>{len(DB['clubs'])}</b> · 👑 Owned: <b>{owned}</b>\n"
            f"🗓 Matches: <b>{len(DB['matches'])}</b> ({open_m} to play)\n"
            f"📈 Played {bar(len(DB['matches']) - open_m, len(DB['matches']))}\n"
            f"🏟 Venues: <b>{len(DB['venues'])}</b>\n"
            f"📜 {rules_line()}\n{B.LINE}\n"
            f"👑 Own {money(fee('own'))} · 🙋 Join {money(fee('join'))}\n"
            f"✏️ Change {money(fee('change'))} · 👥 Max {fee('max')}")
    rows = [[btn("🏏 Clubs", "club:p:clubs"), btn("🗓 Schedule", "club:p:sch:0")],
            [btn("🏟 Venues", "club:p:venues"), btn("🧱 Build Schedule", "club:p:build")],
            [btn("🗑 Clear Schedule", "club:p:clear"), btn("⚙️ Fees & Limits", "club:p:fees")],
            [btn("📜 Match Rules", "club:s:open"), btn("💰 Prize Money", "club:z:open")],
            [btn("📊 Public Hub", "club:v:list"), btn("✖ Close", "club:p:x")]]
    return text, rows


def _panel_clubs():
    items = list(DB["clubs"].values())
    rows = []
    for i in range(0, len(items), 2):
        rows.append([btn(f"{'👑' if c['owner'] else '🆓'} {c['code']}", f"club:p:c:{c['id']}") for c in items[i:i + 2]])
    rows.append([btn("⬅️ Back", "club:p:main")])
    return head("🏏 CLUBS") + "Pick a club to manage 👇", rows


def _panel_club(c):
    text = (head(f"🏏 {c['code']}") + f"<b>{esc(c['name'])}</b>\n"
            f"👑 Owner: {link(c['owner'], c['owner_name']) if c['owner'] else '<i>none</i>'}\n"
            f"👥 Players: <b>{len(c['members'])}/{fee('max')}</b> · 🙋 Requests: <b>{len(c['req'])}</b>\n"
            f"🏆 Pts <b>{c['pts']}</b> · 🏏 Runs <b>{c['runs']}</b> · 🎯 Wkts <b>{c['wkts']}</b>\n"
            f"🖼 Logo: <b>{'yes' if c.get('logo') else 'no'}</b>")
    i = c["id"]
    rows = [[btn("✏️ Rename (free)", f"club:p:rn:{i}"), btn("👑 Set Owner", f"club:p:so:{i}")],
            [btn("🚫 Remove Owner", f"club:p:ro:{i}"), btn("👥 Players", f"club:p:mem:{i}")],
            [btn("🖼 Remove Logo", f"club:p:logo:{i}"), btn("♻️ Reset Stats", f"club:p:reset:{i}")],
            [btn("⬅️ Back", "club:p:clubs")]]
    return text, rows


def _panel_members(c):
    rows = [[btn(f"❌ {m['n'][:24]}", f"club:p:kick:{c['id']}:{uid}")]
            for uid, m in c["members"].items() if str(c["owner"]) != uid]
    rows.append([btn("⬅️ Back", f"club:p:c:{c['id']}")])
    return head(f"👥 {c['code']} PLAYERS") + "Tap a player to remove them 👇", rows


def _panel_sched(page):
    ms = sorted(DB["matches"], key=lambda m: m["no"])
    per = 8
    pages = max(1, (len(ms) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    chunk = ms[page * per:(page + 1) * per]
    rows, row = [], []
    for m in chunk:
        icon = "✅" if m["status"] == "done" else ("🗓" if m.get("ts") and m.get("venue") else "⏳")
        row.append(btn(f"{icon} M{m['no']}", f"club:p:m:{m['no']}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"club:p:sch:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "club:p:noop"))
    if page < pages - 1:
        nav.append(btn("▶️", f"club:p:sch:{page + 1}"))
    rows.append(nav)
    rows.append([btn("⬅️ Back", "club:p:main")])
    text = head("🗓 CLUB MATCHES") + ("\n".join(mline(m) for m in chunk) + "\n\n<i>Tap a match to set date, time & venue.</i>"
                                      if ms else "No matches yet. Use <b>Build Schedule</b> first.")
    return text, rows


def _panel_match(m):
    no = m["no"]
    pos = sorted(x["no"] for x in DB["matches"]).index(no) // 8 if m in DB["matches"] else 0
    text = head(f"M{no} · {cname(m['a'])} vs {cname(m['b'])}") + mline(m)
    rows = []
    if m["status"] != "done":
        rows = [[btn("🔄 Reschedule" if m.get("ts") else "📅 Schedule", f"club:p:sw:{no}")],
                [btn("🗓 Set date & time", f"club:p:wh:{no}"), btn("🏟 Set venue", f"club:p:mv:{no}")],
                [btn("🧹 Clear", f"club:p:mc:{no}")]]
    rows.append([btn("🗑 Delete", f"club:p:dm:{no}"), btn("⬅️ Schedule", f"club:p:sch:{pos}")])
    return text, rows


def _panel_fees():
    rows = [[btn(f"{FEE_LABEL[k]} · {fee(k):,}", f"club:p:fee:{k}")] for k in DEFAULT_FEES]
    rows.append([btn("⬅️ Back", "club:p:main")])
    return head("⚙️ FEES & LIMITS") + "Tap one to change it 👇", rows


async def clubpanel_cmd(update, ctx):
    """/clubpannel (owner) - manage clubs, venues and the schedule with buttons."""
    if not is_staff(update):
        return
    text, rows = _panel_main()
    await _reply(update.message, text, kb(rows))


async def _ask(q, ctx, kind, prompt, **extra):
    PEND[(q.message.chat_id, q.from_user.id)] = {"kind": kind, "ts": time.time(), **extra}
    await q.message.reply_text(prompt, parse_mode=B.HTML)


async def panel_cb(update, ctx, parts):
    q = update.callback_query
    if not is_staff(update):
        return await q.answer("Only the bot owner can use this panel.", show_alert=True)
    act = parts[0]
    arg = parts[1] if len(parts) > 1 else None
    await q.answer()
    if act == "noop":
        return
    if act == "x":
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return
    if act == "main":
        return await _edit(q, *_panel_main())
    if act == "clubs":
        return await _edit(q, *_panel_clubs())
    if act == "sch":
        return await _edit(q, *_panel_sched(int(arg)))
    if act == "fees":
        return await _edit(q, *_panel_fees())
    if act == "fee":
        return await _ask(q, ctx, "fee", f"⚙️ Send the new value for <b>{FEE_LABEL[arg]}</b> (a number).", key=arg)
    # ---- venues
    if act == "venues":
        return await _edit(q, *_panel_venues())
    if act == "va":
        return await _ask(q, ctx, "venue", "🏟 Send the group id(s) separated by commas, e.g. "
                          "<code>-1001234567890, -1009876543210</code>\nor type <code>here</code> to use this group.")
    if act == "vd":
        n = int(arg)
        if 0 <= n < len(DB["venues"]):
            gone = DB["venues"].pop(n)
            for m in DB["matches"]:
                if m.get("venue") == gone["id"]:
                    await _clear_notice(ctx, m)
                    m["venue"] = None
            _save()
        return await _edit(q, *_panel_venues())
    # ---- schedule building
    if act == "build":
        owned = [c["id"] for c in DB["clubs"].values() if c["owner"]]
        if len(owned) < 2:
            return await q.answer("At least 2 owned clubs are needed.", show_alert=True)
        have = {frozenset((m["a"], m["b"])) for m in DB["matches"]}
        added = 0
        for a, b in round_robin(owned):
            if frozenset((a, b)) not in have:
                DB["seq"] += 1
                DB["matches"].append({"no": DB["seq"], "a": a, "b": b, "ts": None, "venue": None,
                                      "status": "pending", "res": None, "w5": 0, "w0": 0})
                added += 1
        _save()
        text, rows = _panel_sched(0)
        return await _edit(q, f"🧱 <b>{added}</b> new matches added.\n" + text, rows)
    if act == "clear":
        return await _edit(q, head("🗑 CLEAR SCHEDULE") + "Delete <b>every</b> club match (results included)? "
                           "Club points and runs stay.", [[btn("✅ Yes, clear", "club:p:clearok"),
                                                           btn("⬅️ No", "club:p:main")]])
    if act == "clearok":
        for m in DB["matches"]:
            await _clear_notice(ctx, m)
        DB["matches"].clear()
        _save()
        return await _edit(q, *_panel_main())
    # ---- one match
    if act in ("m", "wh", "dm", "sw", "sd", "sdc", "st", "stc", "sv", "mv", "mvs", "mc"):
        m = match_of(int(arg))
        if not m:
            return await _edit(q, *_panel_sched(0))
        if act == "m":
            return await _edit(q, *_panel_match(m))
        if act == "dm":
            await _clear_notice(ctx, m)
            DB["matches"].remove(m)
            _save()
            return await _edit(q, *_panel_sched(0))
        if m["status"] == "done":
            return await _edit(q, *_panel_match(m))
        if act == "wh":
            return await _ask(q, ctx, "when", f"📅 <b>Match {m['no']}</b> · {esc(cname(m['a']))} vs {esc(cname(m['b']))}\n"
                              + WHEN_HELP, no=m["no"])
        if act == "mc":
            await _clear_notice(ctx, m)
            m.update(ts=None, venue=None, w5=0, w0=0)
            _save()
            return await _edit(q, *_panel_match(m))
        if act == "sw":
            return await _edit(q, *_w_date(m))
        if act == "sdc":
            return await _ask(q, ctx, "wdate", "✍️ Send the date, e.g. <code>25-11-2026</code> or <code>25/11</code>.",
                              no=m["no"])
        if act == "sd":
            return await _edit(q, *_w_time(m, _dparse(parts[2])))
        if act == "stc":
            return await _ask(q, ctx, "wtime", "✍️ Send the start time, e.g. <code>7:30pm</code> or <code>19:30</code>.",
                              no=m["no"], date=parts[2])
        if act == "st":
            return await _edit(q, *_w_venue(m, _dparse(parts[2]), parts[3]))
        if act == "sv":
            d, hm, idx = _dparse(parts[2]), parts[3], int(parts[4])
            ts = _wts(d, int(hm[:2]), int(hm[2:]))
            if ts <= time.time() or not 0 <= idx < len(DB["venues"]):
                return await q.answer("That time has passed or the venue is gone.", show_alert=True)
            warn = await _set_slot(ctx, m, ts, DB["venues"][idx]["id"])
            text, rows = _panel_match(m)
            return await _edit(q, "✅ <b>Scheduled</b>" + warn + "\n\n" + text, rows)
        if act == "mv":
            rows = [[btn(("✅ " if m.get("venue") == v["id"] else "🏟 ") + v["title"][:28], f"club:p:mvs:{m['no']}:{n}")]
                    for n, v in enumerate(DB["venues"])]
            rows.append([btn("⬅️ Back", f"club:p:m:{m['no']}")])
            return await _edit(q, head(f"🏟 M{m['no']} · VENUE") + ("Pick the venue 👇" if DB["venues"] else
                               "No venues yet. Add one in the panel → Venues."), rows)
        if act == "mvs":
            idx = int(parts[2])
            if 0 <= idx < len(DB["venues"]):
                warn = await _set_slot(ctx, m, None, DB["venues"][idx]["id"])
                text, rows = _panel_match(m)
                return await _edit(q, "✅ <b>Venue set</b>" + warn + "\n\n" + text, rows)
            return await _edit(q, *_panel_match(m))
    # ---- one club
    c = by_id(arg)
    if not c:
        return await _edit(q, *_panel_clubs())
    if act == "c":
        return await _edit(q, *_panel_club(c))
    if act == "rn":
        return await _ask(q, ctx, "prename", f"✏️ Send the new name for <b>{esc(c['name'])}</b>. "
                          "The club code changes with it.", cid=c["id"])
    if act == "so":
        return await _ask(q, ctx, "powner", f"👑 Send the new owner of <b>{esc(c['code'])}</b>: <code>@username</code> or a user id "
                          "(they must have used the bot before).", cid=c["id"])
    if act == "ro":
        c["owner"], c["owner_name"] = None, ""
        _save()
        return await _edit(q, *_panel_club(c))
    if act == "mem":
        return await _edit(q, *_panel_members(c))
    if act == "kick":
        c["members"].pop(parts[2], None)
        _save()
        return await _edit(q, *_panel_members(c))
    if act == "logo":
        c["logo"] = None
        _save()
        return await _edit(q, *_panel_club(c))
    if act == "reset":
        return await _edit(q, head("♻️ RESET STATS") + f"Reset points, runs and wickets of <b>{esc(c['code'])}</b>?",
                           [[btn("✅ Yes, reset", f"club:p:resetok:{c['id']}"), btn("⬅️ No", f"club:p:c:{c['id']}")]])
    if act == "resetok":
        c.update(p=0, w=0, l=0, t=0, pts=0, runs=0, wkts=0)
        for m in c["members"].values():
            m["r"] = m["w"] = 0
        _save()
        return await _edit(q, *_panel_club(c))


# ------------------------------------------------------------------ callbacks for players
async def club_cb(update, ctx):
    q = update.callback_query
    parts = q.data.split(":")[1:]
    if not parts:
        return
    if parts[0] == "p":
        return await panel_cb(update, ctx, parts[1:])
    if parts[0] == "v":
        return await view_cb(update, ctx, parts[1:])
    if parts[0] == "s":
        return await setup_cb(update, ctx, parts[1:])
    if parts[0] == "z":
        return await prize_cb(update, ctx, parts[1:])
    user = q.from_user
    if parts[0] == "o" and parts[1] == "rn":  # club owner taps "Rename" in /myclub
        c = by_id(parts[2])
        if not c or c["owner"] != user.id:
            return await q.answer("Only the club owner can do this.", show_alert=True)
        if B.cash_total(user.id) < fee("change"):
            return await q.answer(f"You need {fee('change'):,} Cash to change the name.", show_alert=True)
        await q.answer()
        return await _ask(q, ctx, "rename", f"✏️ Send the new name for <b>{esc(c['name'])}</b>.\n"
                          f"It costs <b>{money(fee('change'))}</b> and the club code will change too.", cid=c["id"])
    if parts[0] != "j":
        return
    act, cid, uid = parts[1], parts[2], int(parts[3])
    c = by_id(cid)
    req = c["req"].get(str(uid)) if c else None
    if not c or not req or time.time() - req["ts"] > REQ_TTL:
        if c and req:
            c["req"].pop(str(uid), None)
            _save()
        await q.answer("This request has expired.", show_alert=True)
        return await _edit(q, "⌛ This join request has expired.")
    if act in ("ok", "no"):
        if user.id != c["owner"] and not is_staff(update):
            return await q.answer("Only the club owner can answer this.", show_alert=True)
        await q.answer()
        private = q.message.chat.type == "private"
        if act == "no":
            c["req"].pop(str(uid), None)
            _save()
            if private:
                await _tell(ctx, uid, req, head("❌ REQUEST DECLINED") +
                            f"The owner of {club_title(c)} declined your request.")
            return await _edit(q, f"❌ {link(uid, req['n'])}'s request to join {club_title(c)} was declined.")
        req["st"] = "ok"
        _save()
        if private:  # owner answered in DM -> the player gets the payment button
            sent = await _tell(ctx, uid, req, head("✅ REQUEST APPROVED") +
                               f"The owner of {club_title(c)} accepted you!\n"
                               f"💵 Joining fee: <b>{money(fee('join'))}</b> 👇",
                               kb([[btn(f"💵 Pay {fee('join'):,} & Join", f"club:j:pay:{c['id']}:{uid}")]]))
            note = "📨 Payment button sent to the player." if sent else \
                "⚠️ I couldn't reach the player. Ask them to /start the bot and send /joinclub again."
            return await _edit(q, head("✅ REQUEST APPROVED") + f"{link(uid, req['n'])} → {club_title(c)}\n{note}")
        return await _edit(q, head("✅ REQUEST APPROVED") + f"{link(uid, req['n'])} can join {club_title(c)}.\n"
                           f"💵 Joining fee: <b>{money(fee('join'))}</b> — only {esc(req['n'])} can tap below 👇",
                           [[btn(f"💵 Pay {fee('join'):,} & Join", f"club:j:pay:{c['id']}:{uid}")]])
    if act == "pay":
        if user.id != uid:
            return await q.answer("This button is for the approved player.", show_alert=True)
        if req["st"] != "ok":
            return await q.answer("The owner has not approved this yet.", show_alert=True)
        if len(c["members"]) >= fee("max"):
            c["req"].pop(str(uid), None)
            _save()
            await q.answer("The club is full.", show_alert=True)
            return await _edit(q, f"🚫 {club_title(c)} became full.")
        if not charge(uid, user.full_name, fee("join")):
            return await q.answer(f"You need {fee('join'):,} Cash to join.", show_alert=True)
        c["members"][str(uid)] = _member(user.full_name)
        c["req"].pop(str(uid), None)
        _save()
        await q.answer("Welcome to the club!")
        if c["owner"] and c["owner"] != uid:
            try:
                await ctx.bot.send_message(c["owner"], f"🎉 {user.full_name} paid and joined {c['name']} [{c['code']}] "
                                           f"({len(c['members'])}/{fee('max')}).")
            except TelegramError:
                pass
        return await _edit(q, head("🎉 WELCOME") + f"{link(uid, user.full_name)} joined {club_title(c)}!\n"
                           f"💸 Paid <b>{money(fee('join'))}</b> · 👥 {len(c['members'])}/{fee('max')}")


# ------------------------------------------------------------------ match rules  (/clubsetup)
def _max_overs():
    return int(getattr(B, "MAX_OVERS", 20))


def _max_team():
    return int(getattr(B, "MAX_TEAM", 11))


def rules_line():
    r = DB["rules"]
    return (f"🎯 {r['overs']} over{'s' if r['overs'] != 1 else ''} · 👥 {r['playing']} a side · "
            f"⚡ Impact {'ON' if r['impact'] else 'OFF'} · 🔁 Re-bat {'ON' if r['rebat'] else 'OFF'}")


def _fixture_for(chat_id):
    """The unplayed club fixture whose venue is this group and whose time is now (from 5 min before kickoff)."""
    now = time.time()
    return next((m for m in DB["matches"] if m["status"] != "done" and m.get("venue") == chat_id and m.get("ts")
                 and m["ts"] - 300 <= now <= m["ts"] + RULE_AFTER), None)


def rules_for(chat_id):
    """Rules of the club fixture being played in this group right now (None = a normal match). Used by bot.py."""
    m = _fixture_for(chat_id)
    return dict(DB["rules"], match=m["no"]) if m else None


def impact_bench(g, uid):
    """Club players of the asker's club who are not in the match yet -> [(uid, name)]. Used by bot.py /impact."""
    m = _fixture_for(g.chat_id)
    if not m:
        return []
    for cid in (m["a"], m["b"]):
        c = by_id(cid)
        if c and str(uid) in c["members"]:
            return [(int(u), mem["n"]) for u, mem in c["members"].items() if int(u) not in g.players]
    return []


def member_name(uid):
    for c in DB["clubs"].values():
        mem = c["members"].get(str(uid))
        if mem:
            return mem["n"]
    return None


def v_setup():
    r = DB["rules"]
    onoff = lambda v: "ON" if v else "OFF"
    text = (head("⚙️ CLUB MATCH SETUP") + f"🎯 Overs · <b>{r['overs']}</b> a side\n"
            f"👥 Playing XI · <b>{r['playing']}</b> a side\n"
            f"⚡ Impact Player · <b>{onoff(r['impact'])}</b>\n"
            f"🔁 Re-bat · <b>{onoff(r['rebat'])}</b>\n{B.LINE}\n"
            "<i>These rules apply to every scheduled club match in its venue group. Changes save at once.</i>")
    mark = lambda on, label: ("✅ " if on else "") + str(label)
    ov = [o for o in (1, 2, 3, 5, 10, 15, 20) if o <= _max_overs()]
    xs = list(range(4, _max_team() + 1))
    rows = [[btn(f"🎯 OVERS · {r['overs']}", "club:s:noop")],
            [btn(mark(o == r["overs"], o), f"club:s:ov:{o}") for o in ov[:4]],
            [btn(mark(o == r["overs"], o), f"club:s:ov:{o}") for o in ov[4:]] + [btn("✍️ Custom", "club:s:ovc")],
            [btn(f"👥 PLAYING XI · {r['playing']}", "club:s:noop")]]
    for i in range(0, len(xs), 4):
        rows.append([btn(mark(x == r["playing"], x), f"club:s:pl:{x}") for x in xs[i:i + 4]])
    rows.append([btn(f"⚡ Impact · {'ON ✅' if r['impact'] else 'OFF ❌'}", "club:s:im"),
                 btn(f"🔁 Re-bat · {'ON ✅' if r['rebat'] else 'OFF ❌'}", "club:s:rb")])
    rows.append([btn("♻️ Default", "club:s:reset"), btn("🛠 Panel", "club:p:main"), btn("✖ Close", "club:s:x")])
    return text, rows


async def clubsetup_cmd(update, ctx):
    """/clubsetup - the bot owner sets overs, playing XI, Impact Player and Re-bat for club matches."""
    if not is_staff(update):
        return await _reply(update.message, head("📜 CLUB MATCH RULES") + rules_line() +
                            "\n<i>Only the bot owner can change these.</i>")
    r, a, bad = DB["rules"], [x.lower() for x in (ctx.args or [])], []
    for i in range(0, len(a) - 1, 2):  # /clubsetup overs 5 playing 11 impact on rebat off
        k, v = a[i], a[i + 1]
        if k == "overs" and v.isdigit() and 1 <= int(v) <= _max_overs():
            r["overs"] = int(v)
        elif k in ("playing", "xi", "players") and v.isdigit() and 2 <= int(v) <= _max_team():
            r["playing"] = int(v)
        elif k in ("impact", "rebat") and v in ("on", "off", "yes", "no", "1", "0"):
            r[k] = v in ("on", "yes", "1")
        else:
            bad.append(f"{k} {v}")
    _save()
    text, rows = v_setup()
    if bad:
        text += "\n⚠️ Couldn't read: " + esc(", ".join(bad))
    await _reply(update.message, text, kb(rows))


async def setup_cb(update, ctx, parts):
    q = update.callback_query
    if not is_staff(update):
        return await q.answer("Only the bot owner can change the match rules.", show_alert=True)
    act, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    r = DB["rules"]
    if act == "noop":
        return await q.answer()
    if act == "x":
        await q.answer()
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return
    if act == "ovc":
        await q.answer()
        return await _ask(q, ctx, "rule_ov", f"🎯 Send the number of overs (1–{_max_overs()}).")
    if act == "ov" and arg.isdigit() and 1 <= int(arg) <= _max_overs():
        r["overs"] = int(arg)
    elif act == "pl" and arg.isdigit() and 2 <= int(arg) <= _max_team():
        r["playing"] = int(arg)
    elif act == "im":
        r["impact"] = not r["impact"]
    elif act == "rb":
        r["rebat"] = not r["rebat"]
    elif act == "reset":
        DB["rules"] = dict(DEFAULT_RULES)
    _save()
    await q.answer("✅ Saved" if act != "open" else None)
    await _edit(q, *v_setup())


# ------------------------------------------------------------------ monthly prize money  (/setprice)
def _month_key(ts=None):
    return datetime.fromtimestamp(ts or time.time(), _tz()).strftime("%Y-%m")


def _month_label(key):
    return datetime.strptime(key, "%Y-%m").strftime("%B %Y")


def _next_month_ts():
    n = datetime.now(_tz())
    first = (n.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)).replace(day=1)
    return first.timestamp()


def _dhm(sec):
    sec = max(0, int(sec))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m"


def _new_season(month):
    return {"month": month, "clubs": {}, "players": {}}


def _sc(cid):
    """This month's record of a club."""
    return DB["season"]["clubs"].setdefault(cid, {"p": 0, "w": 0, "l": 0, "t": 0, "pts": 0, "runs": 0, "wkts": 0})


def _sp(uid, name, cid):
    """This month's record of a player."""
    e = DB["season"]["players"].setdefault(str(uid), {"n": name, "c": cid, "r": 0, "w": 0})
    e["n"], e["c"] = name, cid
    return e


def month_leaders(S=None):
    """(clubs, batters, bowlers) of a month, best first. Clubs need a played match; players need a run / a wicket."""
    S = S or DB["season"]
    clubs = sorted(((cid, x) for cid, x in S["clubs"].items() if x["p"] > 0 and by_id(cid)),
                   key=lambda t: (-t[1]["pts"], -t[1]["w"], -t[1]["runs"], -t[1]["wkts"], t[0]))
    bats = sorted(((u, e) for u, e in S["players"].items() if e["r"] > 0), key=lambda t: (-t[1]["r"], -t[1]["w"], t[0]))
    bowl = sorted(((u, e) for u, e in S["players"].items() if e["w"] > 0), key=lambda t: (-t[1]["w"], -t[1]["r"], t[0]))
    return clubs, bats, bowl


def _ccode(cid):
    c = by_id(cid)
    return c["code"] if c else "?"


def _pay_month(S):
    """Pays the prizes of a finished month (no awaits: cash and the record change together). -> (record, payouts)."""
    P = DB["prizes"]
    clubs, bats, bowl = month_leaders(S)
    pays = []
    rec = {"month": S["month"], "club": None, "batter": None, "bowler": None, "prizes": dict(P), "paid": 0}
    if clubs:
        cid, x = clubs[0]
        c = by_id(cid)
        n = 0
        for u, mem in c["members"].items():
            if int(u) != c["owner"]:
                n += 1
                if P["player"]:
                    pays.append((int(u), mem["n"], P["player"], "player"))
        if c["owner"] and P["captain"]:
            pays.append((c["owner"], c["owner_name"], P["captain"], "captain"))
        rec["club"] = {"id": cid, "code": c["code"], "name": plain(c["name"]), "pts": x["pts"], "w": x["w"], "p": x["p"],
                       "owner": c["owner_name"], "players": n}
    if bats:
        u, e = bats[0]
        rec["batter"] = {"uid": int(u), "name": e["n"], "code": _ccode(e["c"]), "v": e["r"]}
        if P["batter"]:
            pays.append((int(u), e["n"], P["batter"], "batter"))
    if bowl:
        u, e = bowl[0]
        rec["bowler"] = {"uid": int(u), "name": e["n"], "code": _ccode(e["c"]), "v": e["w"]}
        if P["bowler"]:
            pays.append((int(u), e["n"], P["bowler"], "bowler"))
    for uid, name, amt, _ in pays:
        B.cash_add(uid, amt, name)
    rec["paid"] = sum(a for _, _, a, _ in pays)
    return rec, pays


def _awards_text(rec):
    P = rec["prizes"]
    t = head(f"🏆 CLUB AWARDS · {_month_label(rec['month']).upper()}")
    c = rec["club"]
    if c:
        t += (f"👑 <b>Best Club · {esc(c['code'])}</b> — {esc(c['name'])}\n"
              f"   {c['pts']} pts · {c['w']} wins in {c['p']} matches\n")
        if P["captain"]:
            t += f"   👑 Captain {esc(c['owner'] or '')} · <b>+{money(P['captain'])}</b>\n"
        if P["player"] and c["players"]:
            t += f"   🧢 Each of {c['players']} players · <b>+{money(P['player'])}</b>\n"
    if rec["batter"]:
        b = rec["batter"]
        t += (f"\n🏏 <b>Best Batter</b> · {link(b['uid'], b['name'])} ({esc(b['code'])}) — {b['v']} runs"
              + (f" · <b>+{money(P['batter'])}</b>" if P["batter"] else "") + "\n")
    if rec["bowler"]:
        b = rec["bowler"]
        t += (f"🎯 <b>Best Bowler</b> · {link(b['uid'], b['name'])} ({esc(b['code'])}) — {b['v']} wickets"
              + (f" · <b>+{money(P['bowler'])}</b>" if P["bowler"] else "") + "\n")
    t += B.LINE + "\n"
    if rec["paid"]:
        t += f"💸 Total paid: <b>{money(rec['paid'])}</b>\n"
    return t + "A new month starts now. Play, compete and win! 🔥 /clubawards"


async def _announce_awards(ctx, rec, pays):
    text = _awards_text(rec)
    for v in DB["venues"]:
        try:
            await ctx.bot.send_message(v["id"], text, parse_mode=B.HTML, disable_web_page_preview=True)
        except TelegramError:
            pass
    why = {"captain": "as captain of the best club", "player": "as a player of the best club",
           "batter": "as Best Batter", "bowler": "as Best Bowler"}
    got = {}
    for uid, _, amt, w in pays:
        got.setdefault(uid, []).append((amt, why[w]))
    for uid, items in got.items():
        lines = "\n".join(f"💵 <b>+{amt:,}</b> {w}" for amt, w in items)
        try:
            await ctx.bot.send_message(uid, f"🎉 <b>Club Awards · {_month_label(rec['month'])}</b>\n{lines}\n"
                                       f"Check your Cash balance!", parse_mode=B.HTML)
        except TelegramError:
            pass  # they never started the bot in DM; the group message already named them


async def _roll_month(ctx, force=False):
    """Pays and resets when the month has changed (or at once when forced). Returns the award record, or None."""
    S, cur = DB["season"], _month_key()
    if S["month"] == cur and not force:
        return None
    rec, pays = _pay_month(S)
    has = bool(rec["club"] or rec["batter"] or rec["bowler"])
    if has:
        DB["awards"] = (DB["awards"] + [rec])[-12:]
    DB["season"] = _new_season(cur)
    _save()
    if has:
        await _announce_awards(ctx, rec, pays)
    return rec if has else None


def _race_text(limit=3):
    clubs, bats, bowl = month_leaders()
    t = ""
    if clubs:
        t += "<b>🏆 Best club</b>\n" + "\n".join(
            f"{_medal(i)} <b>{esc(_ccode(cid))}</b> · {x['pts']} pts · P{x['p']} W{x['w']}"
            for i, (cid, x) in enumerate(clubs[:limit])) + "\n\n"
    if bats:
        t += "<b>🏏 Best batter</b>\n" + "\n".join(
            f"{_medal(i)} {link(int(u), e['n'])} ({esc(_ccode(e['c']))}) · <b>{e['r']}</b>"
            for i, (u, e) in enumerate(bats[:limit])) + "\n\n"
    if bowl:
        t += "<b>🎯 Best bowler</b>\n" + "\n".join(
            f"{_medal(i)} {link(int(u), e['n'])} ({esc(_ccode(e['c']))}) · <b>{e['w']}</b>"
            for i, (u, e) in enumerate(bowl[:limit]))
    return t.strip() or "<i>No club match has been played this month yet.</i>"


def _prize_lines():
    P = DB["prizes"]
    return "\n".join(f"{PRIZE_LABEL[k]} · <b>{money(P[k])}</b>" if P[k] else f"{PRIZE_LABEL[k]} · <i>no prize</i>"
                     for k in ("captain", "player", "batter", "bowler"))


def v_prizes():
    """/clubawards - prize money, this month's race and the last winners (public)."""
    S = DB["season"]
    left = _next_month_ts() - time.time()
    t = (head("💰 CLUB PRIZE MONEY") + f"📅 <b>{_month_label(S['month'])}</b> · payout in <b>{_dhm(left)}</b>\n{B.LINE}\n"
         + _prize_lines() + f"\n{B.LINE}\n🏁 <b>THIS MONTH'S RACE</b>\n<blockquote>{_race_text()}</blockquote>")
    if DB["awards"]:
        a = DB["awards"][-1]
        t += f"{B.LINE}\n🏅 <b>{_month_label(a['month'])}</b> winners\n"
        if a["club"]:
            t += f"👑 {esc(a['club']['code'])} · "
        if a["batter"]:
            t += f"🏏 {esc(a['batter']['name'])} · "
        if a["bowler"]:
            t += f"🎯 {esc(a['bowler']['name'])}"
    return t.rstrip(" ·\n"), [[btn("🏆 Table", "club:v:rank"), btn("🗓 Fixtures", "club:v:sch")], foot_row()]


async def clubawards_cmd(update, ctx):
    """/clubawards - prize money and this month's race."""
    await _show(update, v_prizes())


def v_setprice():
    P = DB["prizes"]
    left = _next_month_ts() - time.time()
    text = (head("💰 MONTHLY PRIZE MONEY") + _prize_lines() + f"\n{B.LINE}\n"
            f"📅 Paid on <b>{fmt_ts(_next_month_ts())}</b> (in {_dhm(left)})\n"
            "🏆 Best club = most points this month. Its owner gets the captain prize, every other player gets the "
            "player prize. Top run-scorer and top wicket-taker get their own prize.\n"
            "<i>Tap a prize to change it · 0 = no prize.</i>")
    rows = [[btn(f"👑 Captain · {P['captain']:,}", "club:z:set:captain"), btn(f"🧢 Player · {P['player']:,}", "club:z:set:player")],
            [btn(f"🏏 Batter · {P['batter']:,}", "club:z:set:batter"), btn(f"🎯 Bowler · {P['bowler']:,}", "club:z:set:bowler")],
            [btn("📊 This Month", "club:v:prz"), btn("🏁 Close Month Now", "club:z:close")],
            [btn("♻️ All to 0", "club:z:reset"), btn("🛠 Panel", "club:p:main"), btn("✖ Close", "club:z:x")]]
    return text, rows


async def setprice_cmd(update, ctx):
    """/setprice - the bot owner sets the monthly prize money."""
    if not is_staff(update):
        return await _show(update, v_prizes())
    P, a, bad = DB["prizes"], [x.lower() for x in (ctx.args or [])], []
    alias = {"captain": "captain", "owner": "captain", "player": "player", "players": "player", "batter": "batter",
             "bat": "batter", "bowler": "bowler", "bowl": "bowler"}
    for i in range(0, len(a) - 1, 2):  # /setprice captain 10000 player 2000 batter 5000 bowler 5000
        k, v = alias.get(a[i]), a[i + 1].replace(",", "")
        if k and v.isdigit() and int(v) <= 100_000_000:
            P[k] = int(v)
        else:
            bad.append(f"{a[i]} {a[i + 1]}")
    _save()
    text, rows = v_setprice()
    if bad:
        text += "\n⚠️ Couldn't read: " + esc(", ".join(bad))
    await _reply(update.message, text, kb(rows))


async def prize_cb(update, ctx, parts):
    q = update.callback_query
    if not is_staff(update):
        return await q.answer("Only the bot owner can change the prize money.", show_alert=True)
    act, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    if act == "x":
        await q.answer()
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return
    if act == "set" and arg in PRIZE_LABEL:
        await q.answer()
        return await _ask(q, ctx, "prize", f"{PRIZE_LABEL[arg]} — send the prize in Cash (0 = no prize).", key=arg)
    if act == "close":
        await q.answer()
        return await _edit(q, head("🏁 CLOSE THE MONTH?") + "Prizes are paid <b>now</b> to this month's leaders, then a "
                           f"fresh month starts.\n{B.LINE}\n{_race_text(1)}",
                           [[btn("✅ Yes, Pay Now", "club:z:closeok"), btn("⬅️ Cancel", "club:z:open")]])
    if act == "closeok":
        rec = await _roll_month(ctx, force=True)
        await q.answer("✅ Month closed" if rec else "Nothing to pay yet")
        note = (f"✅ Month closed. Paid <b>{money(rec['paid'])}</b>." if rec
                else "ℹ️ No club match was played this month, so nothing was paid.")
        text, rows = v_setprice()
        return await _edit(q, note + "\n\n" + text, rows)
    if act == "reset":
        DB["prizes"] = dict(DEFAULT_PRIZES)
        _save()
    await q.answer()
    await _edit(q, *v_setprice())


# ------------------------------------------------------------------ typed answers
async def on_text(update, ctx):
    msg, user = update.message, update.effective_user
    key = (msg.chat_id, user.id)
    st = PEND.pop(key, None)
    if not st:
        return
    text = (msg.text or "").strip()
    kind = st["kind"]
    if kind in ("prename", "powner", "when", "fee", "venue", "wdate", "wtime", "rule_ov", "prize") and not is_staff(update):
        return
    if kind == "prize":
        digits = text.replace(",", "")
        if not digits.isdigit() or int(digits) > 100_000_000:
            PEND[key] = st | {"ts": time.time()}
            return await _reply(msg, "Send a whole number of Cash (0 = no prize).")
        DB["prizes"][st["key"]] = int(digits)
        _save()
        t_, rows = v_setprice()
        return await _reply(msg, "✅ Saved.\n\n" + t_, kb(rows))
    if kind == "rule_ov":
        if not text.isdigit() or not 1 <= int(text) <= _max_overs():
            PEND[key] = st | {"ts": time.time()}
            return await _reply(msg, f"Send a whole number from 1 to {_max_overs()}.")
        DB["rules"]["overs"] = int(text)
        _save()
        t_, rows = v_setup()
        return await _reply(msg, "✅ Saved.\n\n" + t_, kb(rows))
    if kind == "venue":
        ok, bad = await _check_venues(ctx, text, msg.chat_id)
        for v in ok:
            if not any(x["id"] == v["id"] for x in DB["venues"]):
                DB["venues"].append(v)
        _save()
        if not ok and bad:
            PEND[key] = st | {"ts": time.time()}
        return await _reply(msg, (_venue_report(ok, bad) or "Nothing added.") +
                            ("\nSend the ids again, or open /clubpannel." if bad and not ok else "\n/clubpannel → Venues"))
    if kind in ("wdate", "wtime"):
        m = match_of(st["no"])
        if not m or m["status"] == "done":
            return
        if kind == "wdate":
            ts = parse_when(text + " 12:00pm")
            if not ts:
                PEND[key] = st | {"ts": time.time()}
                return await _reply(msg, "I can't read that date. Try <code>25-11-2026</code> or <code>25/11</code>.")
            d = datetime.fromtimestamp(ts, _tz()).date()
            t_, rows = _w_time(m, d)
            return await _reply(msg, t_, kb(rows))
        ts = parse_when("today " + text)
        if not ts:
            PEND[key] = st | {"ts": time.time()}
            return await _reply(msg, "I can't read that time. Try <code>7:30pm</code> or <code>19:30</code>.")
        dt = datetime.fromtimestamp(ts, _tz())
        t_, rows = _w_venue(m, _dparse(st["date"]), f"{dt.hour:02d}{dt.minute:02d}")
        return await _reply(msg, t_, kb(rows))
    if kind == "rename":
        c = by_id(st["cid"])
        if not c or c["owner"] != user.id:
            return
        name, err = valid_name(text)
        if err or name_taken(name, c["id"]):
            PEND[key] = st | {"ts": time.time()}
            return await _reply(msg, err or "Another club already has that name. Send a different one.")
        if not charge(user.id, user.full_name, fee("change")):
            return await _reply(msg, f"💵 You need {money(fee('change'))}.")
        old = c["code"]
        c["name"], c["code"] = name, gen_code(name, c["id"])
        _save()
        return await _reply(msg, head("✏️ CLUB UPDATED") + f"🏏 <b>{esc(c['name'])}</b>\n"
                            f"🔖 Code: <code>{esc(old)}</code> ➜ <b>{esc(c['code'])}</b>\n"
                            f"💸 Paid <b>{money(fee('change'))}</b>")
    if kind == "prename":
        c = by_id(st["cid"])
        name, err = valid_name(text)
        if not c:
            return
        if err or name_taken(name, c["id"]):
            PEND[key] = st | {"ts": time.time()}
            return await _reply(msg, err or "Another club already has that name. Send a different one.")
        old = c["code"]
        c["name"], c["code"] = name, gen_code(name, c["id"])
        _save()
        return await _reply(msg, f"✏️ Renamed. Code <code>{esc(old)}</code> ➜ <b>{esc(c['code'])}</b>")
    if kind == "powner":
        c = by_id(st["cid"])
        hit = find_user(text.split()[0]) if text.split() else None
        if not c or not hit:
            PEND[key] = st | {"ts": time.time()}
            return await _reply(msg, "I don't know that user yet. Send <code>@username</code> of someone who has used "
                                "the bot, or reply with a user id.")
        uid, name = hit
        c["owner"], c["owner_name"] = uid, name
        c["members"].setdefault(str(uid), _member(name))
        _save()
        return await _reply(msg, f"👑 {link(uid, name)} is now the owner of {club_title(c)}.")
    if kind == "when":
        m = match_of(st["no"])
        ts = parse_when(text)
        if not m:
            return
        if not ts or ts < time.time():
            PEND[key] = st | {"ts": time.time()}
            return await _reply(msg, "I can't read that, or the time has passed.\n" + WHEN_HELP)
        await _set_slot(ctx, m, ts)
        return await _reply(msg, f"🗓 Set.\n{mline(m)}")
    if kind == "fee":
        digits = text.replace(",", "")
        if not digits.isdigit() or int(digits) <= 0 or (st["key"] == "max" and int(digits) > 50):
            PEND[key] = st | {"ts": time.time()}
            return await _reply(msg, "Send a whole number (max players: 1–50).")
        DB["fees"][st["key"]] = int(digits)
        _save()
        return await _reply(msg, f"⚙️ <b>{FEE_LABEL[st['key']]}</b> is now <b>{int(digits):,}</b>.")


# ------------------------------------------------------------------ match reminders + group reservation
def _locked_match(chat_id):
    """A fixture whose venue is this group and that starts within 5 minutes."""
    now = time.time()
    return next((m for m in DB["matches"] if m["status"] != "done" and m.get("venue") == chat_id and m.get("ts")
                 and m["ts"] - 300 <= now < m["ts"]), None)


async def _lock_cmd(update, ctx):
    chat = update.effective_chat
    m = _locked_match(chat.id) if chat else None
    if m and not is_staff(update):
        await update.effective_message.reply_text(
            f"🔒 Group reserved: club match M{m['no']} ({cname(m['a'])} vs {cname(m['b'])}) starts in "
            f"{max(1, int((m['ts'] - time.time()) / 60 + 0.999))} min.")
        raise ApplicationHandlerStop


async def _lock_cb(update, ctx):
    q = update.callback_query
    m = _locked_match(q.message.chat_id) if q.message else None
    if m and not is_staff(update):
        await q.answer(f"🔒 Locked: club match M{m['no']} starts soon.", show_alert=True)
        raise ApplicationHandlerStop


async def _clear_notice(ctx, m):
    """Deletes the group's reminder and 'group reserved' messages (deleting the pinned one also unpins it)."""
    info = m.pop("rem_msgs", None)
    if not info:
        return
    for mid in info.get("ids", []):
        try:
            await ctx.bot.delete_message(info["chat"], mid)
        except TelegramError:
            pass


async def _group_notice(ctx, m, a, b, mins):
    chat = m.get("venue")
    if not chat or m.get("rem_msgs"):
        return False
    s_ = "s" if mins != 1 else ""
    venue = esc(vtitle(chat))
    remind = (f"⏰ <b>Reminder!</b> {esc(a['name'])} 🆚 {esc(b['name'])} kicks off in "
              f"<b>{mins} minute{s_}</b>!\n\n🏟 Venue: <b>{venue}</b>")
    reserve = (f"🔒 <b>GROUP RESERVED FOR THE CLUB MATCH</b>\n{B.LINE}\n"
               f"🏆 <b>M{m['no']}</b> · {esc(a['name'])} 🆚 {esc(b['name'])}\n"
               f"🏟 Venue: <b>{venue}</b>\n"
               f"⏳ Starting in about <b>{mins} minute{s_}</b>.\n{B.LINE}\n"
               f"📜 <b>Rules:</b> {rules_line()}\n"
               "⛔ /start, /1v1 and /join are locked until the fixture begins.")
    ids = []
    try:
        r1 = await B.say(ctx, chat, remind)
        ids.append(r1.message_id)
        try:
            await ctx.bot.pin_chat_message(chat, r1.message_id)  # needs the "pin messages" admin right
        except TelegramError:
            pass
        r2 = await B.say(ctx, chat, reserve)
        ids.append(r2.message_id)
    except (TelegramError, AttributeError):
        pass
    if ids:
        m["rem_msgs"] = {"chat": chat, "ids": ids}
    return bool(ids)


async def tick(ctx):
    try:
        await _roll_month(ctx)
    except Exception as e:  # never let a payout problem stop the match reminders
        print("[clubs] month payout failed:", e)
    now = time.time()
    for m in DB["matches"]:
        ts = m.get("ts")
        if not ts or m["status"] == "done" or not m.get("venue"):
            continue
        a, b = by_id(m["a"]), by_id(m["b"])
        if not a or not b:
            continue
        left = ts - now
        if not m.get("w5") and 0 < left <= 300:
            if await _group_notice(ctx, m, a, b, max(1, int(left / 60 + 0.999))):
                m["w5"] = 1
                _save()
        elif not m.get("w0") and left <= 0 and now < ts + 600:
            m["w0"] = 1
            await _clear_notice(ctx, m)
            _save()
            await _announce(ctx, m["venue"], m, a, b,
                            f"🔔 <b>Match {m['no']}</b> — it's time! Start a Team match with /start.")


async def _announce(ctx, gid, m, a, b, line):
    tags = []
    for c in (a, b):
        tags += [link(int(u), mem["n"]) for u, mem in c["members"].items()][:12]
    try:
        await B.say(ctx, gid, head("🏏 CLUB MATCH") + f"{line}\n{club_title(a)}  ⚔️  {club_title(b)}\n{B.LINE}\n"
                    f"📜 {rules_line()}\n" +
                    " ".join(tags))
    except TelegramError as e:
        print("[clubs] alert failed:", e)


# ------------------------------------------------------------------ result hook (called from bot.py)
def _side_club(g, side, pair):
    uids = list(g.teams[side])
    need = max(1, (len(uids) + 1) // 2)
    counts = {cid: sum(1 for u in uids if str(u) in by_id(cid)["members"]) for cid in pair}
    best = max(pair, key=lambda cid: counts[cid])
    return best if counts[best] >= need else None


async def on_team_finish(ctx, g, winner, inn):
    """Called by bot.py when a Team match ends.  Records it when it is a scheduled club fixture."""
    if getattr(g, "tour", None) or not DB.get("matches"):
        return
    await _roll_month(ctx)          # a finished month is paid out before this result is counted
    now = time.time()
    cands = sorted((m for m in DB["matches"] if m["status"] != "done" and m.get("ts")
                    and now - WINDOW_AFTER <= m["ts"] <= now + WINDOW_BEFORE),
                   key=lambda m: (m.get("venue") != g.chat_id, m["ts"]))
    for m in cands:
        pair = (m["a"], m["b"])
        if not by_id(pair[0]) or not by_id(pair[1]):
            continue
        sa, sb = _side_club(g, "A", pair), _side_club(g, "B", pair)
        if not sa or not sb or sa == sb:
            continue
        sides = {"A": by_id(sa), "B": by_id(sb)}
        for s, o in (("A", "B"), ("B", "A")):
            c = sides[s]
            b4 = (c["w"], c["l"], c["t"], c["pts"])
            c["p"] += 1
            c["runs"] += inn[s]["score"]
            c["wkts"] += inn[o]["wkts"]          # wickets this club's bowlers took
            sc = _sc(c["id"])                    # the same numbers, for this month's prizes
            sc["p"] += 1
            sc["runs"] += inn[s]["score"]
            sc["wkts"] += inn[o]["wkts"]
            for u in g.teams[s]:
                p, mem = g.players.get(u), c["members"].get(str(u))
                if p and mem:
                    mem["r"] += p.runs
                    mem["w"] += p.wickets
                    sp = _sp(u, mem["n"], c["id"])
                    sp["r"] += p.runs
                    sp["w"] += p.wickets
            if winner is None:
                c["t"] += 1
                c["pts"] += PTS_TIE
            elif winner == s:
                c["w"] += 1
                c["pts"] += PTS_WIN
            else:
                c["l"] += 1
            for k, v0 in zip(("w", "l", "t", "pts"), b4):
                sc[k] += c[k] - v0
        if winner is None:
            text = "Match tied"
        else:
            text = f"{sides[winner]['code']} won"
        await _clear_notice(ctx, m)
        m["status"] = "done"
        m["res"] = {"text": text, "winner": sides[winner]["id"] if winner else None,
                    "a": {"club": sides["A"]["id"], "score": inn["A"]["score"], "wkts": inn["A"]["wkts"]},
                    "b": {"club": sides["B"]["id"], "score": inn["B"]["score"], "wkts": inn["B"]["wkts"]}}
        _save()
        try:
            await B.say(ctx, g.chat_id, head("🏆 CLUB MATCH RECORDED") +
                        f"{club_title(sides['A'])} <b>{inn['A']['score']}/{inn['A']['wkts']}</b>\n"
                        f"{club_title(sides['B'])} <b>{inn['B']['score']}/{inn['B']['wkts']}</b>\n"
                        f"{'🤝' if winner is None else '🏆'} <b>{esc(text)}</b>\n{B.LINE}\n"
                        "Points, runs and wickets were added · /clubranking")
        except TelegramError:
            pass
        return
