"""Tournament mode for Matrix Cricket Bot.

Team matches only.  Flow:
  Menu -> Tournament -> name -> number of teams -> venue group ids -> import teams
  (manual and/or Auction squads) -> number of groups -> schedule is built automatically.
  /hpanel   host panel: venues, date/time/venue per match, fixtures, points table, auto-schedule
  /edittour tournament rules: overs, playing XI size, mode, pick time, timezone, points
  Teams: /addteam /addcap /addvc /addsquad /removesquad /squadcap /squadvc /changecap /tourteams
  Stats: /tourrecords /orangecap /purplecap /mvprace
  Stages: group -> /endgroupstage -> (Super N) -> /startplayoffs -> Qualifier 1 / Eliminator / Qualifier 2 / Final
  When a match's time arrives the bot opens the Playing-XI picker for the captain and vice
  captain of both teams, then hands the match to the normal Team engine in bot.py (toss -> play).

bot.py calls setup(<bot module>) and register(app) once, and on_finish() after every match.
"""
import html
import random
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram.error import TelegramError
from telegram.ext import (ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          MessageHandler, filters)

B = None                      # the bot.py module (set by setup)
DATA_KEY = "cricket_tournaments"
DB = {"seq": 0, "items": {}}  # persisted
INPUT = {}                    # (chat_id, user_id) -> pending typed answer
PICK = {}                     # (tour_id, match_no) -> playing-XI selection state
TEXT_KINDS = {"name", "n", "venues", "team", "groups", "vadd", "date", "rule", "auto1", "auto2"}
INPUT_TTL = 600               # a half-finished question is forgotten after 10 minutes
DEFAULT_RULES = {"overs": 5, "playing": 11, "spam_free": True, "pick_secs": 480,
                 "tz": "Asia/Kolkata", "win_pts": 2, "tie_pts": 1, "impact": False, "rebat": False}
STATUS_ICON = {"pending": "⏳", "scheduled": "🗓", "lineup": "📝", "live": "🔴", "done": "✅"}


# ------------------------------------------------------------------ plumbing
def setup(bot_module):
    global B, DB
    B = bot_module
    data = B.STORE.load_json(DATA_KEY, {"seq": 0, "items": {}})
    if not isinstance(data, dict) or "items" not in data:
        data = {"seq": 0, "items": {}}
    DB = data
    for t in DB["items"].values():
        t["rules"].setdefault("impact", False)
        t["rules"].setdefault("rebat", False)
        t.setdefault("phase", "group")
        t.setdefault("ngroups", max(1, len(t.get("groups", {}))))
        if t["rules"].get("pick_secs") == 300:  # the old default: squad selection is now 8 minutes
            t["rules"]["pick_secs"] = 480
        for tm in t["teams"].values():
            if not tm.get("code"):
                tm["code"] = _code_for(t, tm["name"])
        for m in t["matches"]:  # a restart interrupts live matches: they restart from the schedule
            if m["status"] in ("lineup", "live"):
                m["status"] = "scheduled" if (m.get("ts") and m.get("venue")) else "pending"


def register(app):
    app.add_handler(CallbackQueryHandler(tour_cb, pattern=r"^tour:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & _Pending(), on_text))
    app.add_handler(CommandHandler("hpanel", hpanel_cmd))
    app.add_handler(CommandHandler("edittour", edittour_cmd))
    app.add_handler(CommandHandler("gid", gid_cmd))
    for name, fn in (("addteam", addteam_cmd), ("addcap", addcap_cmd), ("addvc", addvc_cmd),
                     ("squadcap", squadcap_cmd), ("squadvc", squadvc_cmd), ("addsquad", addsquad_cmd),
                     ("removesquad", removesquad_cmd), ("tourteams", tourteams_cmd),
                     ("tourrecords", tourrecords_cmd), ("orangecap", orangecap_cmd),
                     ("purplecap", purplecap_cmd), ("mvprace", mvprace_cmd),
                     ("endgroupstage", endgroupstage_cmd), ("startplayoffs", startplayoffs_cmd)):
        app.add_handler(CommandHandler(name, fn))
    # match-time lock: runs before every other handler (group -1)
    app.add_handler(CommandHandler(["start", "1v1"], _lock_cmd, filters=filters.ChatType.GROUPS), group=-1)
    app.add_handler(CommandHandler(["join", "joingame"], _lock_join_cmd, filters=filters.ChatType.GROUPS), group=-1)
    app.add_handler(CallbackQueryHandler(_lock_cb, pattern=r"^(mode:(solo|team)|auction:start|tour:new)$"), group=-1)
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler(["timezone", "settimezone"], timezone_cmd))
    app.add_handler(CommandHandler(["pointstable", "points"], pointstable_cmd))
    app.add_handler(CommandHandler("endtour", endtour_cmd))
    app.add_handler(CommandHandler("freewin", freewin_cmd))
    app.add_handler(CommandHandler("forfeit", forfeit_cmd))
    app.add_handler(CommandHandler("toursquad", toursquad_cmd))
    app.add_handler(CommandHandler("squad", squad_cmd))
    app.add_handler(CommandHandler("teamlogo", teamlogo_cmd))
    app.add_handler(MessageHandler(filters.CaptionRegex(r"^/teamlogo"), teamlogo_cmd))
    # inside a live tournament match /changecap is handled here; every other chat falls through to bot.py
    app.add_handler(CommandHandler("changecap", changecap_live,
                                   filters=filters.ChatType.GROUPS & _TourGame()))
    app.job_queue.run_repeating(tick, interval=20, first=20)


def _save():
    try:
        B.STORE.save_json(DATA_KEY, DB)
    except Exception as e:
        print("[tournament] could not save:", e)


class _Pending(filters.MessageFilter):
    """Only lets a text message through while this user is answering one of our questions."""

    def filter(self, message):
        if not message.from_user:
            return False
        key = (message.chat_id, message.from_user.id)
        st = INPUT.get(key)
        if not st:
            return False
        if time.time() - st["ts"] > INPUT_TTL or st["kind"] not in TEXT_KINDS:
            if time.time() - st["ts"] > INPUT_TTL:
                INPUT.pop(key, None)
            return False
        return message.chat_id not in B.games and message.chat_id not in B.tgames


def btn(text, data):
    return B.Btn(text, callback_data=data)


def kb(rows):
    return B.Markup(rows)


_ENT = re.compile(r"(&\w+;|&#\d+;)")


def fancy(s):
    """ASCII letters/digits -> bold sans-serif Unicode (a cleaner look than plain bold); HTML entities are kept."""
    def conv(ch):
        if "A" <= ch <= "Z":
            return chr(0x1D5D4 + ord(ch) - 65)
        if "a" <= ch <= "z":
            return chr(0x1D5EE + ord(ch) - 97)
        if "0" <= ch <= "9":
            return chr(0x1D7EC + ord(ch) - 48)
        return ch
    return "".join(p if _ENT.fullmatch(p) else "".join(conv(c) for c in p) for p in _ENT.split(s))


def head(title):
    return f"<b>『 {fancy(title)} 』</b>\n{B.LINE}\n"


async def _banner(ctx, chat, key, caption):
    """Sends the image/GIF/video saved with /addfile <key>, if there is one (nothing otherwise)."""
    if getattr(B, "MEDIA", {}).get(key):
        try:
            await B.say_media(ctx, chat, key, caption)
        except TelegramError:
            pass


async def _send_logo(ctx, chat, tm, caption):
    val = tm.get("logo")
    if not val:
        return False
    kind, fid = val.split(":", 1)
    send = {"photo": ctx.bot.send_photo, "video": ctx.bot.send_video,
            "animation": ctx.bot.send_animation}.get(kind)
    try:
        await send(chat, fid, caption=caption, parse_mode=B.HTML)
        return True
    except Exception:
        return False


def esc(s):
    return html.escape(str(s))


def _tour(tid):
    return DB["items"].get(str(tid))


def _is_host(t, uid):
    return uid == t["host_id"] or uid in B.OWNER_IDS


def _match(t, no):
    return next((m for m in t["matches"] if m["no"] == int(no)), None)


def _tname(t, tid):
    return t["teams"][tid]["name"]


def _vtitle(t, vid):
    v = next((v for v in t["venues"] if v["id"] == vid), None)
    return v["title"] if v else "—"


async def _ask(ctx, chat_id, text, uid, st):
    st["ts"] = time.time()
    INPUT[(chat_id, uid)] = st
    await B.say(ctx, chat_id, text, kb([[btn("❌ Cancel", "tour:wc")]]))


# ------------------------------------------------------------------ time helpers
def _tz(name):
    m = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", (name or "").strip())
    if m:
        sign = -1 if m.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))


def _tz_ok(name):
    return bool(re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", name.strip())) or _zone_exists(name)


def _zone_exists(name):
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def fmt_ts(ts, tzname):
    return datetime.fromtimestamp(ts, _tz(tzname)).strftime("%d %b %H:%M")


_HM = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)?")
_AP = re.compile(r"(\d{1,2})\s*(am|pm)")
_DATE = re.compile(r"(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{2,4}))?")


def parse_when(text, tzname, now=None):
    """'15-11-2026 18:30', '15/11 6:30pm', 'tomorrow 20:00', '21:00' (today) -> unix time or None."""
    tz = _tz(tzname)
    now = now or datetime.now(tz)
    s = text.strip().lower()
    tm = _HM.search(s)
    if tm:
        h, mi, ap = int(tm.group(1)), int(tm.group(2)), tm.group(3)
    else:
        tm = _AP.search(s)
        if not tm:
            return None
        h, mi, ap = int(tm.group(1)), 0, tm.group(2)
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap == "pm" else 0)
    if h > 23 or mi > 59:
        return None
    rest = (s[:tm.start()] + " " + s[tm.end():]).strip()
    dm = _DATE.search(rest)
    try:
        if dm:
            y = dm.group(3)
            y = (int(y) + 2000 if len(y) == 2 else int(y)) if y else now.year
            dt = datetime(y, int(dm.group(2)), int(dm.group(1)), h, mi, tzinfo=tz)
        else:
            base = now + timedelta(days=1) if ("tomorrow" in rest or "tmrw" in rest) else now
            dt = base.replace(hour=h, minute=mi, second=0, microsecond=0)
    except ValueError:
        return None
    return int(dt.timestamp())


# ------------------------------------------------------------------ pure tournament logic
def split_groups(team_ids, g):
    """Shuffle the teams and deal them into g groups as evenly as possible (14 -> 7 + 7)."""
    ids = list(team_ids)
    random.shuffle(ids)
    base, extra = divmod(len(ids), g)
    out, i = {}, 0
    for k in range(g):
        size = base + (1 if k < extra else 0)
        out[chr(65 + k)] = ids[i:i + size]
        i += size
    return out


def round_robin(ids):
    """Circle method: list of rounds, each a list of (a, b) pairs; every pair meets once."""
    ids = list(ids)
    if len(ids) % 2:
        ids.append(None)
    n, rounds = len(ids), []
    for _ in range(n - 1):
        rounds.append([(ids[i], ids[n - 1 - i]) for i in range(n // 2)
                       if ids[i] is not None and ids[n - 1 - i] is not None])
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]
    return rounds


def build_matches(groups, start_no=0, stage="group"):
    per_group = {g: round_robin(ids) for g, ids in groups.items()}
    matches, no = [], start_no
    for r in range(max((len(x) for x in per_group.values()), default=0)):
        for g, rounds in per_group.items():  # round 1 of every group, then round 2 ...
            if r < len(rounds):
                for a, b in rounds[r]:
                    no += 1
                    matches.append({"no": no, "group": g, "round": r + 1, "a": a, "b": b,
                                    "ts": None, "venue": None, "status": "pending", "res": None,
                                    "stage": stage})
    return matches


def standings(t, stage="group"):
    rows = {tid: {"tid": tid, "p": 0, "w": 0, "l": 0, "t": 0, "pts": 0,
                  "rf": 0, "bf": 0, "ra": 0, "bb": 0, "nrr": 0.0} for tid in t["teams"]}
    rules = t["rules"]
    for m in t["matches"]:
        r = m.get("res")
        if m["status"] != "done" or not r or m.get("stage", "group") != stage:
            continue
        for x, key, okey in ((m["a"], "a", "b"), (m["b"], "b", "a")):
            row = rows[x]
            row["p"] += 1
            if r["winner"] == x:
                row["w"] += 1
                row["pts"] += rules["win_pts"]
            elif r["winner"] is None:
                row["t"] += 1
                row["pts"] += rules["tie_pts"]
            else:
                row["l"] += 1
            row["rf"] += r[key]["score"]
            row["bf"] += r[key]["faced"]
            row["ra"] += r[okey]["score"]
            row["bb"] += r[okey]["faced"]
    for row in rows.values():
        f = row["rf"] / (row["bf"] / 6) if row["bf"] else 0.0
        a = row["ra"] / (row["bb"] / 6) if row["bb"] else 0.0
        row["nrr"] = f - a
    return rows


def _rank_key(t):
    return lambda r: (-r["pts"], -r["nrr"], -r["w"], t["teams"][r["tid"]]["name"].lower())


def ranked(t, tids, stage="group"):
    st = standings(t, stage)
    return sorted((st[x] for x in tids), key=_rank_key(t))


def table_text(t, stage="group", only=None):
    out = []
    if stage == "super":
        groups = {"SUPER": t.get("super", {}).get("teams", [])}
    else:
        groups = t["groups"]
    for gname, tids in groups.items():
        if only and gname != only:
            continue
        lines = [f"{'Team':<12}{'P':>2}{'W':>3}{'L':>3}{'T':>3}{'Pts':>4}{'NRR':>7}"]
        for r in ranked(t, tids, stage):
            lines.append(f"{t['teams'][r['tid']]['name'][:12]:<12}{r['p']:>2}{r['w']:>3}{r['l']:>3}"
                         f"{r['t']:>3}{r['pts']:>4}{r['nrr']:>+7.2f}")
        title = f"SUPER {len(tids)}" if stage == "super" else f"GROUP {gname}"
        out.append(f"<b>{title}</b>\n<pre>{esc(chr(10).join(lines))}</pre>")
    return "\n".join(out)


def table_all(t):
    s = table_text(t, "group")
    if t.get("super"):
        s += "\n" + table_text(t, "super")
    return s


PO_LABEL = {"q1": "QUALIFIER 1", "el": "ELIMINATOR", "q2": "QUALIFIER 2", "final": "FINAL"}


def mtitle(m):
    s = m.get("stage", "group")
    if s == "playoff":
        return PO_LABEL.get(m.get("key"), "PLAYOFF")
    if s == "super":
        return "SUPER STAGE"
    return f"GROUP {m['group']}"


def _status(m):
    if m["status"] in ("pending", "scheduled"):
        m["status"] = "scheduled" if (m.get("ts") and m.get("venue")) else "pending"


def mline(t, m):
    a, b = _tname(t, m["a"]), _tname(t, m["b"])
    when = fmt_ts(m["ts"], t["rules"]["tz"]) if m.get("ts") else "no time"
    where = _vtitle(t, m["venue"]) if m.get("venue") else "no venue"
    tag = f" · <i>{mtitle(m).title()}</i>" if m.get("stage") == "playoff" else ""
    s = f"{STATUS_ICON[m['status']]} <b>M{m['no']}</b>{tag} · {esc(a)} vs {esc(b)}"
    if m["status"] == "done" and m.get("res"):
        return s + f"\n      {esc(m['res']['text'])}"
    return s + f"\n      {when} · 🏟 {esc(where)}"


# ------------------------------------------------------------------ auction squad import (best effort)
UID_KEYS = ("uid", "user_id", "userid", "telegram_id", "tg_id", "id")
NAME_KEYS = ("name", "player", "full_name", "player_name")
PL_KEYS = ("players", "squad", "members", "team_players", "roster")
TEAM_KEYS = ("name", "team", "team_name", "title")
CAP_KEYS = ("captain", "manager", "cap", "owner", "manager_id", "manager_username", "mgr")
VC_KEYS = ("vc", "vice_captain", "vicecaptain", "vice", "vc_id", "vc_username")


def _uname_of(uid):
    for u, (i, n) in B.known_unames.items():
        if i == uid:
            return n
    return f"Player {uid}"


def _player(x):
    """int / 'Name | @user | 123456789' / {uid, name, ...} -> {"uid", "name"} or None."""
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return {"uid": x, "name": _uname_of(x)} if abs(x) >= 10000 else None
    if isinstance(x, str):
        m = re.search(r"(?<!\d)\d{5,}(?!\d)", x)
        if not m:
            hit = B.known_unames.get(x.strip().lstrip("@").lower())
            return {"uid": hit[0], "name": hit[1]} if hit else None
        name = x.split("|")[0].strip() or _uname_of(int(m.group()))
        return {"uid": int(m.group()), "name": name}
    if isinstance(x, dict):
        uid = None
        for k in UID_KEYS:
            v = str(x.get(k, "")).strip()
            if v.lstrip("-").isdigit() and len(v.lstrip("-")) >= 5:
                uid = int(v)
                break
        if uid is None:
            return None
        name = next((str(x[k]) for k in NAME_KEYS if x.get(k)), None) or _uname_of(uid)
        return {"uid": uid, "name": name}
    return None


def _uid_of(x):
    p = _player(x)
    return p["uid"] if p else None


def _squad(obj, hint, players):
    name = next((str(obj[k]) for k in TEAM_KEYS if isinstance(obj, dict) and obj.get(k)), None) or hint or "Team"
    cap = next((_uid_of(obj[k]) for k in CAP_KEYS if isinstance(obj, dict) and obj.get(k)), None)
    vc = next((_uid_of(obj[k]) for k in VC_KEYS if isinstance(obj, dict) and obj.get(k)), None)
    cap = cap or players[0]["uid"]
    vc = vc or next((p["uid"] for p in players if p["uid"] != cap), cap)
    return {"name": name[:30], "cap": cap, "vc": vc, "players": players}


def find_squads(obj, hint="", depth=0):
    out = []
    if depth > 5:
        return out
    if isinstance(obj, dict):
        plist = next((obj[k] for k in PL_KEYS if isinstance(obj.get(k), (list, dict))), None)
        if plist is not None:
            items = plist if isinstance(plist, list) else list(plist.values())
            players = [p for p in (_player(x) for x in items) if p]
            if players:
                return [_squad(obj, hint, players)]
        for k, v in obj.items():
            out += find_squads(v, str(k), depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            out += find_squads(v, hint, depth + 1)
        if not out and obj and hint:
            players = [p for p in (_player(x) for x in obj) if p]
            if players:
                out.append(_squad({}, hint, players))
    return out


def auction_squads():
    raw = B.STORE.load_json("auction_squads", {})
    seen, res = set(), []
    for s in find_squads(raw):
        if s["name"].lower() not in seen:
            seen.add(s["name"].lower())
            res.append(s)
    return res, raw


def _shape(raw, limit=500):
    try:
        if isinstance(raw, dict):
            s = "dict keys: " + ", ".join(str(k) for k in list(raw)[:12])
            for k, v in list(raw.items())[:1]:
                s += f"\nfirst item [{k}] = {str(v)[:250]}"
        else:
            s = f"{type(raw).__name__}: {str(raw)[:limit]}"
        return s[:limit]
    except Exception:
        return "unreadable"


# ------------------------------------------------------------------ create wizard
async def _wiz_start(q, ctx):
    chat, user = q.message.chat, q.from_user
    if not _is_sudo(user.id):
        await q.answer("Only sudo users can host a tournament.", show_alert=True)
        return
    if chat.type == "private":
        await q.answer("Create the tournament inside a group.", show_alert=True)
        return
    if chat.id in B.games or chat.id in B.tgames:
        await q.answer("A game is running here. Finish it first.", show_alert=True)
        return
    await q.answer()
    st = {"kind": "name", "w": {"home": chat.id, "host": user.id, "host_name": user.full_name,
                                "teams": [], "venues": []}}
    await B.edit_any(q.message, head("🏆 NEW TOURNAMENT") + "Team matches only.\n\n"
                     "<b>1/5</b> · Send the <b>tournament name</b>.", kb([[btn("❌ Cancel", "tour:wc")]]))
    st["ts"] = time.time()
    INPUT[(chat.id, user.id)] = st


async def _wiz_cb(q, ctx, p):
    key = (q.message.chat_id, q.from_user.id)
    st = INPUT.get(key)
    act = p[1]
    if act == "wc":
        if INPUT.pop(key, None) is None:
            await q.answer("Nothing to cancel here.", show_alert=True)
            return
        await q.answer()
        await B.edit_any(q.message, "❌ Cancelled.")
        return
    if not st or "w" not in st:
        await q.answer("This setup belongs to someone else or has expired.", show_alert=True)
        return
    w = st["w"]
    n = w.get("n", 0)
    if act == "imp":
        await q.answer()
        if p[2] == "cmd":
            await B.edit_any(q.message, head("🆕 TEAMS LATER") +
                             f"{len(w['teams'])} of {w['n']} teams in. You'll add the rest with "
                             "<code>/addteam [Team Name]</code> after the tournament is created.")
            await _ask_groups(ctx, q.message.chat_id, q.from_user.id, w)
            return
        if p[2] == "man":
            st["kind"] = "team"
            await B.edit_any(q.message, _team_prompt(w))
            st["ts"] = time.time()
            return
        squads, raw = auction_squads()
        if not squads:
            await B.edit_any(q.message, head("📥 AUCTION SQUADS") +
                             "I couldn't read any squads from the saved auction data.\n\n"
                             f"<code>{esc(_shape(raw))}</code>\n\nUse manual import instead 👇",
                             kb([[btn("✍️ Manual import", "tour:imp:man")]]))
            return
        st["kind"], st["sq"] = "squads", {"list": squads, "sel": []}
        await B.edit_any(q.message, *_sq_view(st))
        return
    if st["kind"] != "squads":
        await q.answer("Pick an import type first.", show_alert=True)
        return
    sq, remaining = st["sq"], n - len(w["teams"])
    if act == "sq":
        i = int(p[2])
        if i in sq["sel"]:
            sq["sel"].remove(i)
        elif len(sq["sel"]) >= remaining:
            await q.answer(f"You only need {remaining} more team(s).", show_alert=True)
            return
        else:
            sq["sel"].append(i)
        await q.answer()
        await B.edit_any(q.message, *_sq_view(st))
    elif act == "sqall":
        await q.answer()
        sq["sel"] = list(range(min(remaining, len(sq["list"]))))
        await B.edit_any(q.message, *_sq_view(st))
    elif act == "sqgo":
        if not sq["sel"]:
            await q.answer("Select at least one squad.", show_alert=True)
            return
        await q.answer()
        used = {pl["uid"]: tm["name"] for tm in w["teams"] for pl in tm["players"]}
        msgs = []
        for i in sorted(sq["sel"]):
            s_ = sq["list"][i]
            clash = next((pl for pl in s_["players"] if pl["uid"] in used), None)
            if clash:
                msgs.append(f"⚠️ <b>{esc(s_['name'])}</b> skipped — {esc(clash['name'])} is already in "
                            f"{esc(used[clash['uid']])}.")
                continue
            w["teams"].append(s_)
            used.update({pl["uid"]: s_["name"] for pl in s_["players"]})
            msgs.append(f"✅ <b>{esc(s_['name'])}</b> imported · {len(s_['players'])} players")
        await B.edit_any(q.message, head("📥 AUCTION SQUADS") + "\n".join(msgs))
        await _after_import(ctx, q.message.chat_id, q.from_user.id, st)


def _sq_view(st):
    sq, w = st["sq"], st["w"]
    remaining = w["n"] - len(w["teams"])
    rows, row = [], []
    for i, s in enumerate(sq["list"][:40]):
        row.append(btn(("✅ " if i in sq["sel"] else "⬜ ") + f"{s['name'][:14]} ({len(s['players'])})",
                       f"tour:sq:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("☑️ Select all", "tour:sqall"), btn(f"📥 Import ({len(sq['sel'])})", "tour:sqgo")])
    rows.append([btn("✍️ Manual instead", "tour:imp:man"), btn("❌ Cancel", "tour:wc")])
    text = (head("📥 AUCTION SQUADS") + f"Pick up to <b>{remaining}</b> team(s) to import.\n"
            "Captain = manager, VC = vice-captain from the auction.")
    return text, kb(rows)


def _team_prompt(w):
    k = len(w["teams"]) + 1
    return (head("✍️ MANUAL IMPORT") + f"<b>Team {k} of {w['n']}</b>\n\n"
            "Send ONE message:\n<code>Team Name\n@captain\n@vice_captain\n@player3\n@player4 ...</code>\n\n"
            "• First line = team name\n• First player = captain, second = vice captain\n"
            "• Players must have messaged the bot once (or tap-mention them / use their numeric id)")


async def _ask_import(ctx, chat_id, uid, st):
    w = st["w"]
    st["kind"], st["ts"] = "choose", time.time()
    await B.say(ctx, chat_id, head("📥 IMPORT TEAMS") + f"<b>4/5</b> · {len(w['teams'])} of {w['n']} teams added.\n"
                "How do you want to import the next team(s)?",
                kb([[btn("📥 From Auction squads", "tour:imp:auc"), btn("✍️ Manual", "tour:imp:man")],
                    [btn("🆕 I'll use /addteam", "tour:imp:cmd")],
                    [btn("❌ Cancel", "tour:wc")]]))


async def _after_import(ctx, chat_id, uid, st):
    w = st["w"]
    if len(w["teams"]) < w["n"]:
        await _ask_import(ctx, chat_id, uid, st)
        return
    await _ask_groups(ctx, chat_id, uid, w)


async def _ask_groups(ctx, chat_id, uid, w):
    mx = max(1, min(26, w["n"] // 2))
    await _ask(ctx, chat_id, head("🧩 GROUPS") + f"<b>5/5</b> · Into how many <b>groups</b> should the "
               f"{w['n']} teams be divided? (1–{mx})\n<i>e.g. 14 teams into 2 → 7 + 7</i>", uid,
               {"kind": "groups", "w": w})


async def on_text(update, ctx):
    msg = update.message
    key = (msg.chat_id, msg.from_user.id)
    st = INPUT.get(key)
    if not st:
        return
    text = (msg.text or "").strip()
    if text.lower() == "cancel":
        INPUT.pop(key, None)
        await msg.reply_text("❌ Cancelled.")
        return
    fn = {"name": _w_name, "n": _w_n, "venues": _w_venues, "team": _w_team, "groups": _w_groups,
          "vadd": _i_vadd, "date": _i_date, "rule": _i_rule, "auto1": _i_auto1, "auto2": _i_auto2}[st["kind"]]
    await fn(update, ctx, st, text)


async def _w_name(update, ctx, st, text):
    if not text or len(text) > 40:
        await update.message.reply_text("Send a name up to 40 characters.")
        return
    msg = update.message
    st["w"]["name"] = text
    await _ask(ctx, msg.chat_id, head("🏆 NEW TOURNAMENT") + "<b>2/5</b> · How many <b>teams</b> (2–32)?",
               msg.from_user.id, {"kind": "n", "w": st["w"]})


async def _w_n(update, ctx, st, text):
    msg = update.message
    if not text.isdigit() or not 2 <= int(text) <= 32:
        await msg.reply_text("Send a number between 2 and 32.")
        return
    st["w"]["n"] = int(text)
    await _ask(ctx, msg.chat_id, head("🏟 VENUES") + "<b>3/5</b> · Send the venue <b>group ids</b> separated by "
               "commas, e.g. <code>-1001234567890, -1009876543210</code>\n"
               "• Add the bot to each venue group first (send /gid in a group to get its id)\n"
               "• Type <code>here</code> to use this group, or <code>skip</code> to add venues later in /hpanel",
               msg.from_user.id, {"kind": "venues", "w": st["w"]})


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
    s = "".join(f"✅ {esc(v['title'])}\n" for v in ok)
    s += "".join(f"⚠️ {esc(tok)} — {why}\n" for tok, why in bad)
    return s


async def _w_venues(update, ctx, st, text):
    msg = update.message
    ok, bad = await _check_venues(ctx, text, msg.chat_id)
    st["w"]["venues"] = ok
    if ok or bad:
        await msg.reply_text(_venue_report(ok, bad) + ("\nFix the ⚠️ ones later in /hpanel → Venues." if bad else ""),
                             parse_mode=B.HTML)
    await _ask_import(ctx, msg.chat_id, msg.from_user.id, st)


async def _parse_team(ctx, msg, used):
    text = msg.text or ""
    lines = text.split("\n")
    if len(lines) < 3 or not lines[0].strip():
        return None, ["Send the team name on line 1 and at least 2 players below it."]
    name = lines[0].strip()[:30]
    by_line, u16 = {}, text.encode("utf-16-le")
    for e in msg.entities or []:
        if e.type == "text_mention" and e.user:
            pos = len(u16[:e.offset * 2].decode("utf-16-le"))
            by_line.setdefault(text.count("\n", 0, pos), []).append((e.user.id, e.user.full_name))
    players, errs, seen = [], [], set()
    for i, line in enumerate(lines[1:], 1):
        found = list(by_line.get(i, []))
        for tok in re.findall(r"@(\w{3,})", line):
            hit = B.known_unames.get(tok.lower())
            if hit:
                found.append((hit[0], hit[1]))
            else:
                errs.append(f"@{tok} hasn't messaged the bot yet")
        for tok in re.findall(r"(?<![\w@])(\d{6,})(?!\w)", line):
            uid = int(tok)
            try:
                nm_ = (await ctx.bot.get_chat(uid)).full_name or f"Player {uid}"
            except TelegramError:
                nm_ = f"Player {uid}"
            found.append((uid, nm_))
        for uid, nm_ in found:
            if uid in seen:
                errs.append(f"{nm_} is listed twice")
            elif uid in used:
                errs.append(f"{nm_} is already in {used[uid]}")
            else:
                seen.add(uid)
                players.append({"uid": uid, "name": nm_})
    if not errs and len(players) < B.MIN_TEAM:
        errs.append(f"A team needs at least {B.MIN_TEAM} players.")
    if errs:
        return None, errs
    return {"name": name, "cap": players[0]["uid"], "vc": players[1]["uid"], "players": players}, []


async def _w_team(update, ctx, st, text):
    msg, w = update.message, st["w"]
    used = {pl["uid"]: tm["name"] for tm in w["teams"] for pl in tm["players"]}
    team, errs = await _parse_team(ctx, msg, used)
    if errs:
        await msg.reply_text("❌ " + "\n❌ ".join(errs) + "\n\nFix it and send the team again.")
        return
    w["teams"].append(team)
    await msg.reply_text(f"✅ {team['name']} added · {len(team['players'])} players\n"
                         f"© {team['players'][0]['name']} · VC {team['players'][1]['name']}")
    st["ts"] = time.time()
    if len(w["teams"]) < w["n"]:
        await B.say(ctx, msg.chat_id, _team_prompt(w), kb([[btn("❌ Cancel", "tour:wc")]]))
        return
    await _after_import(ctx, msg.chat_id, msg.from_user.id, st)


async def _w_groups(update, ctx, st, text):
    msg, w = update.message, st["w"]
    mx = max(1, min(26, w["n"] // 2))
    if not text.isdigit() or not 1 <= int(text) <= mx:
        await msg.reply_text(f"Send a number between 1 and {mx}.")
        return
    INPUT.pop((msg.chat_id, msg.from_user.id), None)
    await _create(ctx, w, int(text))


async def _create(ctx, w, ngroups):
    DB["seq"] += 1
    tid = DB["seq"]
    t = {"id": tid, "name": w["name"], "host_id": w["host"], "host_name": w["host_name"],
         "home": w["home"], "rules": dict(DEFAULT_RULES), "venues": w["venues"], "teams": {},
         "groups": {}, "matches": [], "phase": "setup", "n": w["n"], "ngroups": ngroups}
    for i, tm in enumerate(w["teams"], 1):
        t["teams"][str(i)] = {"id": str(i), "name": tm["name"], "code": _code_for(t, tm["name"]),
                              "cap": tm["cap"], "vc": tm["vc"], "players": tm["players"], "group": ""}
    DB["items"][str(tid)] = t
    if len(t["teams"]) >= t["n"]:
        _build_fixtures(t)
        _save()
        await _announce(ctx, t)
        return
    _save()
    await B.say_long(ctx, t["home"], head(f"🏆 {esc(t['name']).upper()}") +
                     f"🛠 <b>Team setup</b> · {len(t['teams'])} of {t['n']} teams added\n{B.LINE}\n"
                     "➕ <code>/addteam [Team Name]</code> — creates a team and gives it a code\n"
                     "🎖 <code>/addcap [Team Name]</code> · <code>/addvc [Team Name]</code> (reply to the player "
                     "or add @username)\n👥 <code>/addsquad [Team Name or Code] @player</code>\n"
                     f"{B.LINE}\nFixtures are built automatically when all {t['n']} teams are in "
                     f"({ngroups} group(s)). {B.link(t['host_id'], t['host_name'])}: /hpanel to add venues.")


def _build_fixtures(t):
    ids = list(t["teams"])
    g = max(1, min(t.get("ngroups", 1), len(ids) // 2))
    t["groups"] = split_groups(ids, g)
    for gname, members in t["groups"].items():
        for x in members:
            t["teams"][x]["group"] = gname
    t["matches"] = build_matches(t["groups"])
    t["phase"] = "group"


async def _announce(ctx, t):
    await _banner(ctx, t["home"], "tour_welcome", f"🏆 <b>{fancy(t['name'].upper())}</b>\n✨ The tournament begins!")
    lines = []
    for g, ids in t["groups"].items():
        lines.append(f"<b>GROUP {g}</b> · {len(ids)} teams")
        lines += [f"  {i}. {esc(t['teams'][x]['name'])} <code>{esc(t['teams'][x]['code'])}</code>"
                  for i, x in enumerate(ids, 1)]
        lines.append("")
    await B.say_long(ctx, t["home"],
                     head(f"🏆 {esc(t['name']).upper()}") +
                     f"👥 {len(t['teams'])} teams · {len(t['groups'])} group(s) · 📅 <b>{len(t['matches'])}</b> matches (round robin)\n"
                     f"🏏 {t['rules']['overs']} overs · {t['rules']['playing']} playing · "
                     f"🏟 {len(t['venues'])} venue(s)\n{B.LINE}\n" + "\n".join(lines) + B.LINE +
                     f"\n👑 {B.link(t['host_id'], t['host_name'])}: open <b>/hpanel</b> to add venues and set "
                     "date, time & venue for each match. Rules can be changed with <b>/edittour</b>.")


# ------------------------------------------------------------------ host panel screens
def screen_panel(t):
    done = sum(m["status"] == "done" for m in t["matches"])
    sched = sum(m["status"] in ("scheduled", "lineup", "live", "done") for m in t["matches"])
    r = t["rules"]
    phase = t.get("phase", "group")
    stage = {"setup": f"Team setup ({len(t['teams'])}/{t.get('n', len(t['teams']))} teams)",
             "group": "Group stage", "super": f"Super {len(t.get('super', {}).get('teams', []))}",
             "playoffs": "Playoffs", "done": "Completed"}[phase]
    text = (head(f"🏆 {esc(t['name']).upper()}") +
            f"📍 Stage: <b>{stage}</b>\n"
            f"👥 {len(t['teams'])} teams · {len(t['groups'])} group(s)\n"
            f"🏟 {len(t['venues'])} venue(s)\n"
            f"📅 Scheduled <b>{sched}/{len(t['matches'])}</b> · Played <b>{done}</b>\n{B.LINE}\n"
            f"🏏 {r['overs']} ov · 👥 {r['playing']} playing · "
            f"{'🛡 Spam-Free' if r['spam_free'] else '⚔️ Normal'} · 🌍 {esc(r['tz'])}")
    i = t["id"]
    rows = [
        [btn("🏟 Venues", f"tour:v:{i}"), btn("📅 Schedule", f"tour:s:{i}:0")],
        [btn("📋 Fixtures", f"tour:f:{i}"), btn("📊 Points table", f"tour:t:{i}")],
        [btn("⚡ Auto-schedule", f"tour:au:{i}"), btn("⚙️ Rules", f"tour:r:{i}")],
        [btn("🗑 Delete", f"tour:x:{i}"), btn("⬅️ My tournaments", "tour:l")]]
    if phase == "setup":
        rows.insert(0, [btn("✅ Finish teams & build fixtures", f"tour:fin:{i}")])
    return text, kb(rows)


def screen_venues(t):
    i = t["id"]
    lines = [f"{n}. {esc(v['title'])} <code>{v['id']}</code>" for n, v in enumerate(t["venues"], 1)] or ["No venues yet."]
    rows = [[btn(f"❌ {n}", f"tour:vd:{i}:{n - 1}") for n in range(s, min(s + 5, len(t['venues']) + 1))]
            for s in range(1, len(t["venues"]) + 1, 5)]
    rows.append([btn("➕ Add venue", f"tour:va:{i}")])
    rows.append([btn("⬅️ Back", f"tour:p:{i}")])
    return head("🏟 VENUES") + "\n".join(lines) + "\n\n<i>A venue is a group where matches are played.</i>", kb(rows)


def screen_sched(t, page):
    i, per = t["id"], 8
    pages = max(1, (len(t["matches"]) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    chunk = t["matches"][page * per:(page + 1) * per]
    rows, row = [], []
    for m in chunk:
        row.append(btn(f"M{m['no']}", f"tour:m:{i}:{m['no']}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"tour:s:{i}:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(btn("▶️", f"tour:s:{i}:{page + 1}"))
    rows += [nav, [btn("⬅️ Back", f"tour:p:{i}")]]
    text = head("📅 SCHEDULE") + "\n".join(mline(t, m) for m in chunk) + "\n\n<i>Tap a match to set date, time & venue.</i>"
    return text, kb(rows)


def screen_match(t, m):
    i, no = t["id"], m["no"]
    text = (head(f"M{no} · {mtitle(m)}") + f"{mline(t, m)}\n")
    rows = []
    if m["status"] in ("pending", "scheduled"):
        rows = [[btn("🗓 Set date & time", f"tour:md:{i}:{no}"), btn("🏟 Set venue", f"tour:mv:{i}:{no}")],
                [btn("🧹 Clear", f"tour:mc:{i}:{no}"), btn("▶️ Start now", f"tour:ms:{i}:{no}")]]
    rows.append([btn("⬅️ Schedule", f"tour:s:{i}:{(no - 1) // 8}")])
    return text, kb(rows)


RULE_LABEL = {"name": "✏️ Name", "overs": "🏏 Overs", "playing": "👥 Playing", "pick_secs": "⏱ Pick time (s)",
              "tz": "🌍 Timezone", "win_pts": "🏆 Win pts", "tie_pts": "🤝 Tie pts"}


def screen_rules(t):
    r, i = t["rules"], t["id"]
    text = (head("⚙️ TOURNAMENT RULES") + f"✏️ Name: <b>{esc(t['name'])}</b>\n"
            f"🏏 Overs: <b>{r['overs']}</b>\n👥 Playing: <b>{r['playing']}</b> per side\n"
            f"{'🛡' if r['spam_free'] else '⚔️'} Mode: <b>{'Spam-Free' if r['spam_free'] else 'Normal'}</b>\n"
            f"⏱ XI pick time: <b>{r['pick_secs']}s</b>\n🌍 Timezone: <b>{esc(r['tz'])}</b>\n"
            f"🏆 Win: <b>{r['win_pts']}</b> pts · 🤝 Tie: <b>{r['tie_pts']}</b> pts\n"
            f"⚡ Impact: <b>{'ON' if r.get('impact') else 'OFF'}</b> · 🔁 Rebat: <b>{'ON' if r.get('rebat') else 'OFF'}</b>\n\n"
            "<i>Changes apply to matches that haven't started yet.</i>")
    rows = [[btn(RULE_LABEL["overs"], f"tour:re:{i}:overs"), btn(RULE_LABEL["playing"], f"tour:re:{i}:playing")],
            [btn("🛡/⚔️ Toggle mode", f"tour:rm:{i}"), btn(RULE_LABEL["pick_secs"], f"tour:re:{i}:pick_secs")],
            [btn(RULE_LABEL["tz"], f"tour:re:{i}:tz"), btn(RULE_LABEL["name"], f"tour:re:{i}:name")],
            [btn(RULE_LABEL["win_pts"], f"tour:re:{i}:win_pts"), btn(RULE_LABEL["tie_pts"], f"tour:re:{i}:tie_pts")],
            [btn(f"⚡ Impact: {'ON' if r.get('impact') else 'OFF'}", f"tour:impact:{i}"),
             btn(f"🔁 Rebat: {'ON' if r.get('rebat') else 'OFF'}", f"tour:rebat:{i}")],
            [btn("⬅️ Back", f"tour:p:{i}")]]
    return text, kb(rows)


def _mine(uid):
    return [t for t in DB["items"].values() if _is_host(t, uid)]


def _list_screen(mine, act):
    rows = [[btn(f"🏆 {t['name'][:30]}", f"tour:{act}:{t['id']}" + (":0" if act == "s" else ""))] for t in mine]
    return head("🏆 MY TOURNAMENTS") + "Pick one 👇", kb(rows)


async def hpanel_cmd(update, ctx):
    await _open(update, "p")


async def edittour_cmd(update, ctx):
    await _open(update, "r")


async def _open(update, act):
    mine = _mine(update.effective_user.id)
    msg = update.message
    if not mine:
        await msg.reply_text("You aren't hosting a tournament yet. Send /start in a group → 🏆 Tournament.")
        return
    if len(mine) == 1:
        text, markup = screen_panel(mine[0]) if act == "p" else screen_rules(mine[0])
    else:
        text, markup = _list_screen(mine, act)
    await msg.reply_text(text, parse_mode=B.HTML, reply_markup=markup)


async def gid_cmd(update, ctx):
    chat = update.effective_chat
    await update.message.reply_text(f"Group id: {chat.id}")


# ------------------------------------------------------------------ host panel callbacks
async def tour_cb(update, ctx):
    q = update.callback_query
    uid, p = q.from_user.id, q.data.split(":")
    act = p[1]
    if act == "records":
        t_rec = _tour(p[2])
        if not t_rec:
            await q.answer("Tournament not found", show_alert=True); return
        text_rec, markup_rec = _records_screen(t_rec, int(p[3]))
        await q.answer(); await B.edit_any(q.message, text_rec, markup_rec); return
    if act == "rc":
        t_rec = _tour(p[2]); m_rec = _match(t_rec, p[3]) if t_rec else None
        if not m_rec or m_rec.get("status") != "done" or not m_rec.get("res"):
            await q.answer("Scorecard unavailable", show_alert=True); return
        await q.answer()
        await B.edit_any(q.message, head(f"📜 {esc(t_rec['name']).upper()} · M{m_rec['no']}") + scorecard_text(t_rec, m_rec),
                         kb([[btn("⬅️ Match records", f"tour:records:{t_rec['id']}:0")]])); return
    if act in ("ff", "ffn"):
        await _forfeit_cb(q, ctx, p)
        return
    if act in ("et", "etn"):
        await _endtour_cb(q, ctx, p)
        return
    if act == "close":
        await q.answer()
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return
    if act == "new":
        await _wiz_start(q, ctx)
        return
    if act in ("wc", "imp", "sq", "sqall", "sqgo"):
        await _wiz_cb(q, ctx, p)
        return
    if act in ("xi", "xo"):
        await _xi_cb(q, ctx, p)
        return
    if act == "l":
        mine = _mine(uid)
        await q.answer()
        text, markup = _list_screen(mine, "p") if mine else ("You aren't hosting a tournament.", None)
        await B.edit_any(q.message, text, markup)
        return
    t = _tour(p[2]) if len(p) > 2 else None
    if not t:
        await q.answer("This tournament no longer exists.", show_alert=True)
        return
    if not _is_host(t, uid):
        await q.answer("Only the tournament host can use this panel.", show_alert=True)
        return
    i, chat_id = t["id"], q.message.chat_id
    edit = lambda s: B.edit_any(q.message, s[0], s[1])  # noqa: E731

    if act == "p":
        await q.answer()
        await edit(screen_panel(t))
    elif act == "v":
        await q.answer()
        await edit(screen_venues(t))
    elif act == "va":
        await q.answer()
        await _ask(ctx, chat_id, head("🏟 ADD VENUE") + "Send the venue group id(s), comma separated.\n"
                   "The bot must already be in that group (send /gid there to get the id).",
                   uid, {"kind": "vadd", "tid": i})
    elif act == "vd":
        idx = int(p[3])
        if idx < len(t["venues"]):
            gone = t["venues"].pop(idx)
            for m in t["matches"]:
                if m.get("venue") == gone["id"] and m["status"] in ("pending", "scheduled"):
                    m["venue"] = None
                    _status(m)
            _save()
        await q.answer("Venue removed.")
        await edit(screen_venues(t))
    elif act == "s":
        await q.answer()
        await edit(screen_sched(t, int(p[3])))
    elif act == "m":
        m = _match(t, p[3])
        await q.answer()
        await edit(screen_match(t, m))
    elif act == "md":
        await q.answer()
        r = t["rules"]
        await _ask(ctx, chat_id, head(f"🗓 M{p[3]} DATE & TIME") +
                   f"Send the date and time ({esc(r['tz'])}), e.g.\n<code>15-11-2026 18:30</code>\n"
                   "<code>15/11 6:30pm</code> · <code>tomorrow 20:00</code> · <code>21:00</code> (today)",
                   uid, {"kind": "date", "tid": i, "no": int(p[3])})
    elif act == "mv":
        m = _match(t, p[3])
        if not t["venues"]:
            await q.answer("Add a venue first (Venues).", show_alert=True)
            return
        await q.answer()
        rows = [[btn(f"🏟 {v['title'][:28]}", f"tour:mvs:{i}:{m['no']}:{n}")] for n, v in enumerate(t["venues"])]
        rows.append([btn("⬅️ Back", f"tour:m:{i}:{m['no']}")])
        await B.edit_any(q.message, head(f"🏟 M{m['no']} VENUE") + "Pick the venue 👇", kb(rows))
    elif act == "mvs":
        m = _match(t, p[3])
        if m["status"] not in ("pending", "scheduled"):
            await q.answer("This match already started.", show_alert=True)
            return
        m["venue"] = t["venues"][int(p[4])]["id"]
        _status(m)
        _save()
        await q.answer("Venue set ✅")
        await edit(screen_match(t, m))
    elif act == "mc":
        m = _match(t, p[3])
        if m["status"] in ("pending", "scheduled"):
            m["ts"] = m["venue"] = None
            _status(m)
            _save()
        await q.answer("Cleared.")
        await edit(screen_match(t, m))
    elif act == "ms":
        m = _match(t, p[3])
        if m["status"] not in ("pending", "scheduled"):
            await q.answer("This match already started.", show_alert=True)
            return
        if not m.get("venue"):
            await q.answer("Pick a venue first.", show_alert=True)
            return
        if _venue_busy(t, m["venue"]):
            await q.answer("That venue is busy with another match right now.", show_alert=True)
            return
        short = _short_team(t, m)
        if short:
            await q.answer(f"{short} needs at least {B.MIN_TEAM} players first (/addsquad).", show_alert=True)
            return
        await q.answer("Starting…")
        m["ts"] = int(time.time())
        await _begin_lineup(ctx, t, m)
    elif act == "au":
        if not t["venues"]:
            await q.answer("Add at least one venue first (Venues).", show_alert=True)
            return
        await q.answer()
        await _ask(ctx, chat_id, head("⚡ AUTO-SCHEDULE") + "Fills every match that has no slot yet, rotating "
                   f"through your {len(t['venues'])} venue(s).\n\n<b>1/2</b> · When should the <b>first match</b> "
                   f"start? ({esc(t['rules']['tz'])})\n<code>15-11-2026 18:30</code>",
                   uid, {"kind": "auto1", "tid": i})
    elif act == "f":
        await q.answer()
        await _send_fixtures(ctx, t, chat_id)
    elif act == "t":
        await q.answer()
        await _banner(ctx, chat_id, "tour_table", f"📊 <b>{fancy('POINTS TABLE')}</b> · {esc(t['name'])}")
        await B.say_long(ctx, chat_id, (f"🏆 <b>{esc(t['name'])}</b> - <b>Group Stage Points Table</b>\n\n"
                                        + table_fancy(t, "group")) if t["groups"] else "No groups yet.")
    elif act in ("impact", "rebat"):
        field = act
        t["rules"][field] = not t["rules"].get(field, False)
        _save()
        await q.answer(f"{field.title()} {'ON' if t['rules'][field] else 'OFF'}")
        await edit(screen_rules(t))
    elif act == "r":
        await q.answer()
        await edit(screen_rules(t))
    elif act == "rm":
        t["rules"]["spam_free"] = not t["rules"]["spam_free"]
        _save()
        await q.answer("Mode changed.")
        await edit(screen_rules(t))
    elif act == "re":
        await q.answer()
        field = p[3]
        hints = {"name": "Send the new tournament name.", "overs": f"Send overs per side (1–{B.MAX_OVERS}).",
                 "playing": f"Send players per side in the XI (2–{B.MAX_TEAM}).",
                 "pick_secs": "Send seconds the captain/VC get to pick the XI (60–1800).",
                 "tz": "Send a timezone, e.g. <code>Asia/Kolkata</code> or <code>+05:30</code>.",
                 "win_pts": "Send points for a win (0–10).", "tie_pts": "Send points for a tie (0–10)."}
        await _ask(ctx, chat_id, head("⚙️ " + RULE_LABEL[field].split(" ", 1)[1].upper()) + hints[field],
                   uid, {"kind": "rule", "tid": i, "field": field})
    elif act == "fin":
        if t.get("phase") != "setup" or len(t["teams"]) < 2:
            await q.answer("Add at least 2 teams first (/addteam).", show_alert=True)
            return
        await q.answer("Building fixtures…")
        t["n"] = len(t["teams"])
        _build_fixtures(t)
        _save()
        await _announce(ctx, t)
    elif act == "egs":
        await q.answer()
        text, markup = _egs_screen(t)
        await B.edit_any(q.message, text, markup)
    elif act == "eq":
        if not _group_stage_over(t):
            await q.answer("The group stage isn't over.", show_alert=True)
            return
        await q.answer()
        n = int(p[3])
        await B.edit_any(q.message, head("🏁 END GROUP STAGE") + _qual_text(t, n),
                         kb([[btn("✅ Confirm", f"tour:eqy:{i}:{n}"), btn("⬅️ Back", f"tour:egs:{i}")]]))
    elif act == "eqy":
        if not _group_stage_over(t):
            await q.answer("The group stage isn't over (or was already ended).", show_alert=True)
            return
        await q.answer("Done ✅")
        await B.edit_any(q.message, head("🏁 GROUP STAGE ENDED") + _qual_text(t, int(p[3])))
        await _do_qualify(ctx, t, int(p[3]))
    elif act == "x":
        await q.answer()
        await B.edit_any(q.message, head("🗑 DELETE") + f"Delete <b>{esc(t['name'])}</b> with all its fixtures and results?",
                         kb([[btn("✅ Yes, delete", f"tour:xy:{i}"), btn("❌ No", f"tour:p:{i}")]]))
    elif act == "xy":
        DB["items"].pop(str(i), None)
        for k in [k for k in PICK if k[0] == i]:
            PICK.pop(k, None)
        _save()
        await q.answer("Deleted.")
        await B.edit_any(q.message, "🗑 Tournament deleted.")


async def _send_fixtures(ctx, t, chat_id):
    lines = []
    for g in t["groups"]:
        lines.append(f"<b>GROUP {g}</b>")
        lines += [mline(t, m) for m in t["matches"] if m.get("stage", "group") == "group" and m["group"] == g]
        lines.append("")
    sup = [m for m in t["matches"] if m.get("stage") == "super"]
    if sup:
        lines.append(f"<b>SUPER {len(t['super']['teams'])}</b>")
        lines += [mline(t, m) for m in sup]
        lines.append("")
    po = [m for m in t["matches"] if m.get("stage") == "playoff"]
    if po:
        lines.append("<b>PLAYOFFS</b>")
        lines += [mline(t, m) for m in po]
        lines.append("")
    await B.say_long(ctx, chat_id, head(f"📋 {esc(t['name']).upper()} · FIXTURES") +
                     ("\n".join(lines) or "No fixtures yet."))


# ------------------------------------------------------------------ typed answers from the panel
async def _i_vadd(update, ctx, st, text):
    msg, t = update.message, _tour(st["tid"])
    INPUT.pop((msg.chat_id, msg.from_user.id), None)
    if not t:
        return
    ok, bad = await _check_venues(ctx, text, msg.chat_id)
    added = []
    for v in ok:
        if not any(x["id"] == v["id"] for x in t["venues"]):
            t["venues"].append(v)
            added.append(v)
    _save()
    await msg.reply_text(_venue_report(added, bad) or "Nothing added.", parse_mode=B.HTML)
    s = screen_venues(t)
    await B.say(ctx, msg.chat_id, s[0], s[1])


async def _i_date(update, ctx, st, text):
    msg, t = update.message, _tour(st["tid"])
    if not t:
        INPUT.pop((msg.chat_id, msg.from_user.id), None)
        return
    ts = parse_when(text, t["rules"]["tz"])
    if not ts:
        await msg.reply_text("I couldn't read that. Try <code>15-11-2026 18:30</code> or "
                             "<code>tomorrow 8pm</code>.", parse_mode=B.HTML)
        return
    INPUT.pop((msg.chat_id, msg.from_user.id), None)
    m = _match(t, st["no"])
    if m["status"] not in ("pending", "scheduled"):
        await msg.reply_text("This match already started.")
        return
    m["ts"] = ts
    _status(m)
    _save()
    clash = next((o for o in t["matches"] if o is not m and m.get("venue") and o.get("venue") == m["venue"]
                  and o.get("ts") and abs(o["ts"] - ts) < 20 * 60 and o["status"] != "done"), None)
    if clash:
        await msg.reply_text(f"⚠️ M{clash['no']} is at the same venue within 20 minutes.")
    s = screen_match(t, m)
    await B.say(ctx, msg.chat_id, s[0], s[1])


async def _i_rule(update, ctx, st, text):
    msg, t, f = update.message, _tour(st["tid"]), st["field"]
    if not t:
        INPUT.pop((msg.chat_id, msg.from_user.id), None)
        return
    spec = {"overs": (1, B.MAX_OVERS), "playing": (2, B.MAX_TEAM), "pick_secs": (60, 1800),
            "win_pts": (0, 10), "tie_pts": (0, 10)}
    if f == "name":
        if not text or len(text) > 40:
            await msg.reply_text("Send a name up to 40 characters.")
            return
        t["name"] = text
    elif f == "tz":
        if not _tz_ok(text):
            await msg.reply_text("Unknown timezone. Use e.g. Asia/Kolkata or +05:30.")
            return
        t["rules"]["tz"] = text.strip()
    else:
        lo, hi = spec[f]
        if not text.isdigit() or not lo <= int(text) <= hi:
            await msg.reply_text(f"Send a number between {lo} and {hi}.")
            return
        t["rules"][f] = int(text)
    INPUT.pop((msg.chat_id, msg.from_user.id), None)
    _save()
    s = screen_rules(t)
    await B.say(ctx, msg.chat_id, "✅ Saved.\n\n" + s[0], s[1])


async def _i_auto1(update, ctx, st, text):
    msg, t = update.message, _tour(st["tid"])
    ts = parse_when(text, t["rules"]["tz"]) if t else None
    if not ts:
        await msg.reply_text("I couldn't read that. Try <code>15-11-2026 18:30</code>.", parse_mode=B.HTML)
        return
    await _ask(ctx, msg.chat_id, head("⚡ AUTO-SCHEDULE") + "<b>2/2</b> · Minutes between matches at the same venue "
               "(e.g. <code>45</code>)", msg.from_user.id, {"kind": "auto2", "tid": st["tid"], "ts0": ts})


async def _i_auto2(update, ctx, st, text):
    msg, t = update.message, _tour(st["tid"])
    if not text.isdigit() or not 5 <= int(text) <= 1440:
        await msg.reply_text("Send minutes between 5 and 1440.")
        return
    INPUT.pop((msg.chat_id, msg.from_user.id), None)
    k = 0
    for m in t["matches"]:
        if m["status"] != "pending":
            continue
        v = t["venues"][k % len(t["venues"])]
        m["venue"], m["ts"] = v["id"], st["ts0"] + (k // len(t["venues"])) * int(text) * 60
        _status(m)
        k += 1
    _save()
    await msg.reply_text(f"✅ {k} match(es) scheduled.")
    await _send_fixtures(ctx, t, msg.chat_id)


# ------------------------------------------------------------------ match start: playing XI -> engine
def _venue_busy(t, chat_id):
    if chat_id in B.tgames or chat_id in B.games:
        return True
    if B.auction_mode and B.auction_mode.active_auction(chat_id):
        return True
    return any(m["venue"] == chat_id and m["status"] in ("lineup", "live")
               for x in DB["items"].values() for m in x["matches"])


async def tick(ctx):
    now, changed = time.time(), False
    for t in list(DB["items"].values()):
        for m in t["matches"]:
            if m["status"] in ("pending", "scheduled") and t.get("phase") != "done":
                ao, bo = t["teams"][m["a"]].get("out"), t["teams"][m["b"]].get("out")
                if ao != bo:  # a team that left the tournament: its new fixtures are free wins for the other side
                    out, win = (m["a"], m["b"]) if ao else (m["b"], m["a"])
                    await _award(ctx, t, m, win, f"{_tname(t, win)} won by walkover ({_tname(t, out)} forfeited the tournament)",
                                 f"🚫 <b>{esc(_tname(t, out))}</b> has forfeited the tournament.", [t["home"]])
                    changed = True
                    continue
            if m["status"] == "live":
                if m["venue"] in B.tgames:
                    m.pop("gone", None)
                elif not m.get("gone"):  # give t_finish a moment to report the result first
                    m["gone"] = 1
                else:  # ended early (/endgame) without a result
                    m["status"], m["ts"] = "pending", None
                    m.pop("gone", None)
                    changed = True
                    await _tell(ctx, t, f"⚠️ <b>M{m['no']}</b> ({esc(_tname(t, m['a']))} vs "
                                f"{esc(_tname(t, m['b']))}) was ended without a result. Set a new date & time "
                                "in /hpanel to replay it.")
            elif m["status"] == "scheduled" and m["ts"] > now:
                if await _remind(ctx, t, m, now):
                    changed = True
            elif m["status"] == "scheduled" and m["ts"] <= now:
                short = _short_team(t, m)
                if short:
                    if not m.get("short"):
                        m["short"] = True
                        changed = True
                        await _tell(ctx, t, f"⏳ <b>M{m['no']}</b> is due but <b>{esc(short)}</b> has fewer than "
                                    f"{B.MIN_TEAM} players. Add players with /addsquad; the match starts "
                                    "automatically once both squads are ready.")
                    continue
                m.pop("short", None)
                if _venue_busy(t, m["venue"]):
                    if not m.get("late"):
                        m["late"] = True
                        changed = True
                        await _tell(ctx, t, f"⏳ <b>M{m['no']}</b> is due but its venue is busy. "
                                    "It starts automatically as soon as the venue is free.")
                    continue
                m.pop("late", None)
                try:
                    await _begin_lineup(ctx, t, m)
                except Exception as e:  # e.g. the bot was removed from the venue group
                    print("[tournament] could not start match:", e)
                    m["status"], m["venue"] = "pending", None
                    PICK.pop((t["id"], m["no"]), None)
                    await _tell(ctx, t, f"⚠️ <b>M{m['no']}</b> could not start (venue unreachable). "
                                "Pick another venue in /hpanel → Schedule.")
                changed = True
    if changed:
        _save()


async def _tell(ctx, t, text):
    for chat in {t["home"], t["host_id"]}:
        try:
            await B.say(ctx, chat, text)
        except TelegramError:
            pass


def _cap_idx(tm):
    """Index of the player who must be in the XI: the captain (else the vice captain, else the first)."""
    for who in (tm.get("cap"), tm.get("vc")):
        for i, pl in enumerate(tm["players"]):
            if pl["uid"] == who:
                return i
    return 0


def _pick_job(tid, no, side):
    return f"txi:{tid}:{no}:{side}"


async def _begin_lineup(ctx, t, m):
    tid, no, r = t["id"], m["no"], t["rules"]
    m["status"], m["cfg"] = "lineup", dict(r)
    _save()
    pk = {"sel": {"a": [], "b": []}, "need": {}, "done": {"a": False, "b": False}, "turn": None}
    PICK[(tid, no)] = pk
    chat = m["venue"]
    a, b = t["teams"][m["a"]], t["teams"][m["b"]]
    mins, secs = divmod(r["pick_secs"], 60)
    await _banner(ctx, chat, "tour_lineup", f"📝 <b>{fancy('PLAYING XI')}</b> · {esc(a['name'])} 🆚 {esc(b['name'])}")
    await B.say(ctx, chat, head(f"🏆 {esc(t['name']).upper()}") + f"<b>MATCH {no} · {mtitle(m)}</b>\n"
                f"🔴 {esc(a['name'])}  vs  🔵 {esc(b['name'])}\n{B.LINE}\n"
                f"🏏 {r['overs']} overs · 👥 {r['playing']} playing\n"
                f"📝 <b>Captain & Vice Captain</b> pick the Playing XI — one team at a time:\n"
                f"1️⃣ {esc(a['name'])} picks first, then 2️⃣ {esc(b['name'])}.\n"
                f"🧢 The captain must play.\n"
                f"⏳ Each team has {mins} min{f' {secs}s' if secs else ''}. A team that doesn't confirm in time "
                "forfeits: the other team gets a free win.")
    for side in ("a", "b"):
        tm = t["teams"][m[side]]
        pk["need"][side] = min(r["playing"], len(tm["players"]))
        if len(tm["players"]) <= r["playing"]:  # whole squad plays, nothing to pick
            pk["sel"][side] = list(range(len(tm["players"])))
            pk["done"][side] = True
            await B.say(ctx, chat, f"✅ <b>{esc(tm['name'])}</b>: whole squad ({len(tm['players'])}) plays.")
        else:
            pk["sel"][side] = [_cap_idx(tm)]
    await _next_pick(ctx, t, m)


async def _next_pick(ctx, t, m):
    """Opens the picker of the first team that hasn't locked its XI; starts the match when both have."""
    tid, no = t["id"], m["no"]
    pk = PICK.get((tid, no))
    if not pk:
        return
    side = next((x for x in ("a", "b") if not pk["done"][x]), None)
    if side is None:
        await _start_engine(ctx, t, m)
        return
    pk["turn"] = side
    text, markup = _xi_view(t, m, side)
    await B.say(ctx, m["venue"], text, markup)
    ctx.job_queue.run_once(_xi_timeout, m["cfg"]["pick_secs"], data=(tid, no, side), name=_pick_job(tid, no, side))


def _xi_view(t, m, side):
    pk = PICK[(t["id"], m["no"])]
    tm = t["teams"][m[side]]
    need, sel = pk["need"][side], pk["sel"][side]
    cap_i = _cap_idx(tm)
    rows = []
    for i, pl in enumerate(tm["players"]):  # one player per row
        tag = "🧢 " if i == cap_i else ""
        rows.append([btn(("✅ " if i in sel else "⬜ ") + tag + pl["name"][:30], f"tour:xi:{t['id']}:{m['no']}:{side}:{i}")])
    rows.append([btn(f"✔️ Confirm XI ({len(sel)}/{need})", f"tour:xo:{t['id']}:{m['no']}:{side}")])
    cap = next((p["name"] for p in tm["players"] if p["uid"] == tm["cap"]), None)
    vc = next((p["name"] for p in tm["players"] if p["uid"] == tm["vc"]), None)
    mins, secs = divmod(m["cfg"]["pick_secs"], 60)
    text = (f"{'🔴' if side == 'a' else '🔵'} <b>{esc(tm['name'])}</b> — pick <b>{need}</b> players "
            f"({len(sel)}/{need})\n" + (f"🧢 {esc(cap)} (must play)" if cap else "")
            + (f"  ·  🏅 VC {esc(vc)}" if vc else "")
            + f"\n⏳ {mins} min{f' {secs}s' if secs else ''} to confirm")
    return text, kb(rows)


def _may_pick(t, tm, uid):
    return uid in (tm["cap"], tm["vc"]) or _is_host(t, uid)


async def _xi_cb(q, ctx, p):
    act, tid, no, side = p[1], p[2], int(p[3]), p[4]
    t = _tour(tid)
    m = _match(t, no) if t else None
    pk = PICK.get((int(tid), no))
    if not t or not m or not pk or m["status"] != "lineup":
        await q.answer("This lineup is closed.", show_alert=True)
        return
    tm = t["teams"][m[side]]
    if not _may_pick(t, tm, q.from_user.id):
        await q.answer("Only the captain or vice captain can pick the XI.", show_alert=True)
        return
    if pk["done"][side]:
        await q.answer("This XI is already locked.", show_alert=True)
        return
    if pk.get("turn") != side:
        await q.answer("The other team is picking first. Wait for your turn.", show_alert=True)
        return
    sel, need = pk["sel"][side], pk["need"][side]
    if act == "xi":
        i = int(p[5])
        if i == _cap_idx(tm):
            await q.answer("The captain must play 🧢", show_alert=True)
            return
        if i in sel:
            sel.remove(i)
        elif len(sel) >= need:
            await q.answer(f"Already {need} selected — untick someone first.", show_alert=True)
            return
        else:
            sel.append(i)
        await q.answer()
        await B.edit_any(q.message, *_xi_view(t, m, side))
        return
    if len(sel) != need:
        await q.answer(f"Select exactly {need} players ({len(sel)} so far).", show_alert=True)
        return
    await q.answer("XI locked ✅")
    pk["done"][side] = True
    for j in ctx.job_queue.get_jobs_by_name(_pick_job(int(tid), no, side)):
        j.schedule_removal()
    await B.edit_any(q.message, _xi_locked(tm, sel))
    await _next_pick(ctx, t, m)


def _xi_locked(tm, sel):
    names = [f"{n}. {esc(tm['players'][i]['name'])}" + (" 🧢" if tm['players'][i]['uid'] == tm.get('cap') else "")
             for n, i in enumerate(sorted(sel), 1)]
    return f"✅ <b>{esc(tm['name'])}</b> — Playing XI locked\n" + "\n".join(names)


async def _xi_timeout(ctx):
    tid, no, side = ctx.job.data
    t = _tour(tid)
    m = _match(t, no) if t else None
    pk = PICK.get((tid, no))
    if not m or not pk or m["status"] != "lineup" or pk["done"][side] or pk.get("turn") != side:
        return
    await _forfeit(ctx, t, m, side)


def _award_res(t, m, winner, text):
    cfg = m.get("cfg") or t["rules"]
    blank = {"score": 0, "wkts": 0, "balls": 0, "faced": 0, "n": 0}
    m["res"], m["card"], m["status"] = ({"winner": winner, "overs": cfg["overs"], "walkover": True, "text": text,
                                         "a": dict(blank), "b": dict(blank)}, None, "done")
    m.pop("rem", None)
    m.pop("late", None)
    m.pop("short", None)


def _after_text(t, m):
    """Group table / stage-complete notes that follow a decided match."""
    stage, text = m.get("stage", "group"), ""
    if stage == "group":
        text += f"📊 <b>GROUP {m['group']}</b>\n" + table_text(t, "group", only=m["group"])
    elif stage == "super":
        text += table_text(t, "super")
    if stage == "group" and _group_stage_over(t):
        text += (f"\n{B.LINE}\n🏆 <b>Group stage complete!</b>\n" + table_all(t) +
                 f"\n👑 {B.link(t['host_id'], t['host_name'])}: send <b>/endgroupstage</b> to choose who qualifies.")
    elif stage == "super" and all(x["status"] == "done" for x in t["matches"] if x.get("stage") == "super"):
        text += (f"\n{B.LINE}\n⚡ <b>Super {len(t['super']['teams'])} complete!</b>\n" +
                 f"👑 {B.link(t['host_id'], t['host_name'])}: send <b>/startplayoffs</b> (the top 4 go through).")
    return text


async def _award(ctx, t, m, winner, res_text, why, chats):
    """Gives a match to one team without it being played (walkover / free win) and announces it."""
    PICK.pop((t["id"], m["no"]), None)
    for sd in ("a", "b"):
        for j in ctx.job_queue.get_jobs_by_name(_pick_job(t["id"], m["no"], sd)):
            j.schedule_removal()
    _award_res(t, m, winner, res_text)
    _save()
    text = (head(f"🏁 MATCH {m['no']} RESULT") + f"{why}\n🏆 <b>{esc(res_text)}</b>\n{B.LINE}\n" + _after_text(t, m))
    for chat in dict.fromkeys(chats):
        try:
            await B.say_long(ctx, chat, text)
        except TelegramError:
            pass
    if m.get("stage") == "playoff":
        await _progress_playoffs(ctx, t, m)


async def _forfeit(ctx, t, m, side):
    """A team didn't lock its XI in time: the other team wins by walkover."""
    loser, winner = m[side], m["b" if side == "a" else "a"]
    ln, wn = _tname(t, loser), _tname(t, winner)
    await _award(ctx, t, m, winner, f"{wn} won by walkover ({ln} didn't submit their Playing XI)",
                 f"⏱ <b>{esc(ln)}</b> didn't confirm their Playing XI in time.", [m["venue"], t["home"]])


async def _start_engine(ctx, t, m):
    tid, no = t["id"], m["no"]
    for sd in ("a", "b"):
        for j in ctx.job_queue.get_jobs_by_name(_pick_job(tid, no, sd)):
            j.schedule_removal()
    pk = PICK.pop((tid, no), None)
    if not pk:
        return
    cfg, chat = m.get("cfg") or t["rules"], m["venue"]
    try:
        title = (await ctx.bot.get_chat(chat)).title or ""
    except TelegramError:
        title = _vtitle(t, chat)
    g = B.TeamGame(chat_id=chat, host_id=t["host_id"], host_name=t["host_name"],
                   spam_free=cfg["spam_free"], chat_title=title, state="overs")
    g.overs, g.tour = cfg["overs"], (tid, no)
    for side, key in (("A", "a"), ("B", "b")):
        tm = t["teams"][m[key]]
        xi = [tm["players"][i] for i in sorted(pk["sel"][key])]
        for pl in xi:
            g.players[pl["uid"]] = B.Player(pl["uid"], pl["name"], batted=False, dm_ok=False)
            B.user_game[pl["uid"]] = chat
        g.teams[side] = [pl["uid"] for pl in xi]
        uids = g.teams[side]
        g.captains[side] = tm["cap"] if tm["cap"] in uids else (tm["vc"] if tm["vc"] in uids else uids[0])
    B.tgames[chat] = g
    m["status"] = "live"
    _save()
    failed = []
    for uid, p in g.players.items():
        p.dm_ok = await B.dm_ready(ctx, uid, f"🏆 You're in the Playing XI · {t['name']} · Match {no}")
        if not p.dm_ok:
            failed.append(p)
    a, b = t["teams"][m["a"]], t["teams"][m["b"]]
    for icon, tm_ in (("🔴", t["teams"][m["a"]]), ("🔵", t["teams"][m["b"]])):
        await _send_logo(ctx, chat, tm_, f"{icon} <b>{esc(tm_['name'])}</b> <code>{esc(tm_['code'])}</code>")
    await _banner(ctx, chat, "tour_start", f"🏟 <b>{fancy('MATCH ' + str(no))}</b> · {esc(t['teams'][m['a']]['name'])} 🆚 {esc(t['teams'][m['b']]['name'])}")
    text = (head("🏟 MATCH STARTING") + f"🔴 Team A = <b>{esc(a['name'])}</b>\n🔵 Team B = <b>{esc(b['name'])}</b>\n"
            f"🎯 {g.overs} over(s) a side")
    markup = None
    if failed:
        text += "\n⚠️ These players must press <b>Start</b> in my DM to bowl: " + ", ".join(B.nm(p) for p in failed)
        markup = B.start_dm_kb(ctx)
    await B.say(ctx, chat, text, markup)
    if chat != t["home"]:
        await B.say(ctx, t["home"], f"▶️ <b>M{no}</b> {esc(a['name'])} vs {esc(b['name'])} has started in "
                    f"<b>{esc(title or _vtitle(t, chat))}</b>.")
    await B.begin_toss(ctx, g)


# ------------------------------------------------------------------ team codes, lookups, permissions
SQUAD_MAX = 25
MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


def _code_for(t, name):
    """Soul Strikers -> SS, Shadow Strikers -> SS1, a one-word name uses its first 3 letters."""
    words = re.findall(r"\w+", name)
    if len(words) > 1:
        base = "".join(w[0] for w in words)[:4].upper()
    elif words:
        base = words[0][:3].upper()
    else:
        base = "T"
    used = {(x.get("code") or "").lower() for x in t["teams"].values()}
    code, n = base, 0
    while code.lower() in used:
        n += 1
        code = f"{base}{n}"
    return code


def _find_team(t, text):
    s = " ".join(text.split()).lower()
    if not s:
        return None
    for tm in t["teams"].values():
        if (tm.get("code") or "").lower() == s:
            return tm
    for tm in t["teams"].values():
        if tm["name"].lower() == s:
            return tm
    return None


def _split_team(t, args):
    """['Soul', 'Strikers', '@bob'] -> (team, ['@bob']); the longest leading words that name a team win."""
    for i in range(len(args), 0, -1):
        tm = _find_team(t, " ".join(args[:i]))
        if tm:
            return tm, list(args[i:])
    return None, list(args)


def _is_sudo(uid):
    if uid in B.OWNER_IDS:
        return True
    try:
        am = getattr(B, "auction_mode", None)
        return bool(am and am.is_sudo(uid))
    except Exception:
        return False


def _can_edit(t, uid):
    """Host, sudo user or owner (the owner can do everything)."""
    return _is_host(t, uid) or _is_sudo(uid)


def _is_lead(tm, uid):
    return uid in (tm.get("cap"), tm.get("vc"))


def _here(update):
    chat = update.effective_chat.id
    return sorted((t for t in DB["items"].values()
                   if chat == t["home"] or any(v["id"] == chat for v in t["venues"])),
                  key=lambda t: -t["id"])


def _candidates(update):
    uid, here = update.effective_user.id, _here(update)
    seen = {t["id"] for t in here}
    mine = [t for t in DB["items"].values() if t["id"] not in seen and _can_edit(t, uid)]
    seen |= {t["id"] for t in mine}
    return here + mine + [t for t in DB["items"].values() if t["id"] not in seen]


def _locate(update, args):
    """-> (tournament, team, remaining args, error)."""
    if not args:
        return None, None, [], "Tell me which team: use its name or code (see /tourteams)."
    cands = _candidates(update)
    if not cands:
        return None, None, [], "There is no tournament yet."
    for t in cands:
        tm, rest = _split_team(t, args)
        if tm:
            return t, tm, rest, None
    return None, None, [], "I couldn't find that team. Use its exact name or code (see /tourteams)."


def _read_tour(update):
    here = _here(update)
    if here:
        return here[0]
    allt = list(DB["items"].values())
    if len(allt) == 1:
        return allt[0]
    mine = [t for t in allt if _is_host(t, update.effective_user.id)]
    return mine[0] if len(mine) == 1 else None


def _host_tour(update):
    """The tournament the sender hosts (the one of this group if there are several)."""
    uid = update.effective_user.id
    mine = [t for t in DB["items"].values() if _is_host(t, uid)]
    here = [t for t in _here(update) if t in mine]
    return (here or mine or [None])[0] if (len(here) == 1 or len(mine) == 1) else None


def _team_busy(t, tid):
    return any(m["status"] in ("lineup", "live") and tid in (m["a"], m["b"]) for m in t["matches"])


def _team_of_uid(t, uid):
    return next((tm for tm in t["teams"].values() if any(p["uid"] == uid for p in tm["players"])), None)


def _short_team(t, m):
    for key in ("a", "b"):
        tm = t["teams"][m[key]]
        if len(tm["players"]) < B.MIN_TEAM:
            return tm["name"]
    return None


def _pname(tm, uid):
    return next((p["name"] for p in tm["players"] if p["uid"] == uid), None)


async def _players_from(ctx, msg, rest, pool=None):
    """Who the command points at: reply > tapped mentions > @username / numeric id > exact name."""
    found, errs, seen, names = [], [], set(), []

    def add(uid, name):
        if uid not in seen:
            seen.add(uid)
            found.append((uid, name))

    rep = msg.reply_to_message
    if rep and rep.from_user and not rep.from_user.is_bot:
        add(rep.from_user.id, rep.from_user.full_name)
    for e in msg.entities or []:
        if e.type == "text_mention" and e.user:
            add(e.user.id, e.user.full_name)
    for tok in rest:
        if tok.startswith("@"):
            hit = B.known_unames.get(tok[1:].lower())
            pl = next((p for p in (pool or []) if p["name"].lower() == tok[1:].lower()), None)
            if hit:
                add(hit[0], hit[1])
            elif pl:
                add(pl["uid"], pl["name"])
            else:
                errs.append(f"{esc(tok)} hasn't messaged the bot yet. Reply to their message instead.")
        elif tok.isdigit() and len(tok) >= 6:
            uid = int(tok)
            pl = next((p for p in (pool or []) if p["uid"] == uid), None)
            try:
                nm_ = pl["name"] if pl else ((await ctx.bot.get_chat(uid)).full_name or f"Player {uid}")
            except TelegramError:
                nm_ = f"Player {uid}"
            add(uid, nm_)
        else:
            names.append(tok)
    if names and not found:
        text = " ".join(names).lower()
        hits = [(p["uid"], p["name"]) for p in (pool or []) if p["name"].lower() == text]
        if not hits:
            hits = list({v[0]: v for v in B.known_unames.values() if v[1].lower() == text}.values())
        if len(hits) == 1:
            add(*hits[0])
        elif hits:
            errs.append(f"More than one player is called {esc(' '.join(names))}. Use @username or reply.")
        else:
            errs.append(f"I don't know a player called {esc(' '.join(names))}. Use @username or reply.")
    return found, errs


async def _reply(msg, text):
    await msg.reply_text(text, parse_mode=B.HTML)


# ------------------------------------------------------------------ /addteam /addcap /addvc /squadcap /squadvc
async def addteam_cmd(update, ctx):
    msg, user = update.message, update.effective_user
    name = " ".join(ctx.args).strip()[:30]
    if not name:
        await _reply(msg, "Usage: <code>/addteam [Team Name]</code>")
        return
    cands = [t for t in _candidates(update) if t.get("phase") == "setup" and _can_edit(t, user.id)]
    if not cands:
        await msg.reply_text("There's no tournament of yours that is still collecting teams.")
        return
    t = cands[0]
    if any(x["name"].lower() == name.lower() for x in t["teams"].values()):
        await msg.reply_text("A team with that name already exists.")
        return
    tid = str(max((int(x) for x in t["teams"]), default=0) + 1)
    code = _code_for(t, name)
    t["teams"][tid] = {"id": tid, "name": name, "code": code, "cap": None, "vc": None,
                       "players": [], "group": ""}
    _save()
    await _reply(msg, f"✅ <b>{esc(name)}</b> added · code <code>{esc(code)}</code> · "
                      f"{len(t['teams'])}/{t['n']} teams\n"
                      f"Next: <code>/addcap {esc(code)}</code> (reply to the captain), "
                      f"<code>/addvc {esc(code)}</code>, <code>/addsquad {esc(code)} @player</code>")
    if len(t["teams"]) >= t["n"]:
        _build_fixtures(t)
        _save()
        await _announce(ctx, t)


async def _role_cmd(update, ctx, role, mode):
    """role: cap | vc.  mode: add (host/sudo/owner) | change (also the team's captain)."""
    msg, user = update.message, update.effective_user
    cmd = {"cap": "captain", "vc": "vice-captain"}[role]
    name_cmd = ("/addcap" if role == "cap" else "/addvc") if mode == "add" else \
               ("/squadcap" if role == "cap" else "/squadvc")
    usage = (f"Usage: <code>{name_cmd} [Team Name or Code] @username</code>\n"
             f"Or reply to the player's message: <code>{name_cmd} [Team Name or Code]</code>")
    t, tm, rest, err = _locate(update, ctx.args)
    if err:
        await _reply(msg, err + "\n\n" + usage)
        return
    can_edit = _can_edit(t, user.id)
    if not (can_edit or (mode == "change" and user.id == tm.get("cap"))):
        await msg.reply_text("Only the tournament host, a sudo user or the owner"
                             + (" or this team's captain" if mode == "change" else "")
                             + f" can change the {cmd}.")
        return
    players, errs = await _players_from(ctx, msg, rest, tm["players"])
    if not players:
        await _reply(msg, "\n".join(errs + [usage]))
        return
    uid, name = players[0]
    added = ""
    if not any(p["uid"] == uid for p in tm["players"]):
        if not can_edit:
            await msg.reply_text("That player must be in the squad first (the host can add them).")
            return
        if _team_busy(t, tm["id"]):
            await msg.reply_text("This team is in a match right now. Try again when it ends.")
            return
        e = _squad_add(t, tm, uid, name)
        if e:
            await _reply(msg, "❌ " + e)
            return
        added = " (added to the squad)"
    name = _pname(tm, uid) or name
    other = "vc" if role == "cap" else "cap"
    if tm.get(role) == uid:
        await msg.reply_text(f"{name} is already the {cmd} of {tm['name']}.")
        return
    if role == "vc" and tm.get("cap") == uid:
        await msg.reply_text(f"{name} is the captain. Make someone else captain first with /squadcap.")
        return
    swapped = ""
    if role == "cap" and tm.get(other) == uid:  # the vice-captain becomes captain: the old captain takes the VC seat
        tm["vc"] = tm.get("cap")
        swapped = f"\n🎖 {esc(_pname(tm, tm['vc']) or '—')} is now the vice-captain."
    tm[role] = uid
    _save()
    live = " For the match in progress use /changecap." if _team_busy(t, tm["id"]) else ""
    await _reply(msg, f"{'🎖' if role == 'cap' else '🎗'} <b>{cmd.upper()}</b> · {esc(tm['name'])} "
                      f"<code>{esc(tm['code'])}</code>\n{B.link(uid, name)}{added}{swapped}{live}")


async def addcap_cmd(update, ctx):
    await _role_cmd(update, ctx, "cap", "add")


async def addvc_cmd(update, ctx):
    await _role_cmd(update, ctx, "vc", "add")


async def squadcap_cmd(update, ctx):
    await _role_cmd(update, ctx, "cap", "change")


async def squadvc_cmd(update, ctx):
    await _role_cmd(update, ctx, "vc", "change")


class _TourGame(filters.MessageFilter):
    """True while a tournament match is being played in this chat."""

    def filter(self, message):
        g = B.tgames.get(message.chat_id)
        return bool(g and g.tour)


async def changecap_live(update, ctx):
    """/changecap [A|B or Team] @user  (or reply) - swaps the captain for the match in progress."""
    chat, user, msg = update.effective_chat, update.effective_user, update.message
    g = B.tgames.get(chat.id)
    t = _tour(g.tour[0]) if g and g.tour else None
    m = _match(t, g.tour[1]) if t else None
    if not m:
        await msg.reply_text("This tournament match no longer exists.")
        return
    usage = ("Usage: <code>/changecap [A or B or Team Name] @username</code>\n"
             "Or reply to the player's message: <code>/changecap A</code>\n"
             "The new captain must be in the Playing XI.")
    args, side, rest = list(ctx.args), None, []
    if args and args[0].upper() in ("A", "B"):
        side, rest = args[0].upper(), args[1:]
    else:
        tm, rest = _split_team(t, args)
        if tm and tm["id"] in (m["a"], m["b"]):
            side = "A" if tm["id"] == m["a"] else "B"
    if not side:
        await _reply(msg, usage)
        return
    if not (_can_edit(t, user.id) or user.id == g.host_id or user.id == g.captains.get(side)):
        await msg.reply_text("Only the tournament host, the owner or that team's captain can change the captain.")
        return
    players, errs = await _players_from(ctx, msg, rest)
    if not players:
        await _reply(msg, "\n".join(errs + [usage]))
        return
    uid, _ = players[0]
    tm = t["teams"][m["a" if side == "A" else "b"]]
    if uid not in g.teams[side]:
        await msg.reply_text(f"That player isn't in {tm['name']}'s Playing XI.")
        return
    old = g.captains.get(side)
    if old == uid:
        await msg.reply_text("They are already the captain.")
        return
    g.captains[side] = uid
    was = f"{B.nm(g.players[old])} → " if old in g.players else ""
    await B.say(ctx, chat.id, f"🎖 <b>CAPTAIN CHANGED</b> · {esc(tm['name'])}\n{was}{B.nm(g.players[uid])}")


# ------------------------------------------------------------------ /addsquad /removesquad /tourteams
def _squad_add(t, tm, uid, name):
    if any(p["uid"] == uid for p in tm["players"]):
        return f"{esc(name)} is already in {esc(tm['name'])}."
    other = _team_of_uid(t, uid)
    if other:
        return f"{esc(name)} is already in {esc(other['name'])}."
    if len(tm["players"]) >= SQUAD_MAX:
        return f"{esc(tm['name'])} already has {SQUAD_MAX} players (the squad limit)."
    tm["players"].append({"uid": uid, "name": name})
    return None


async def addsquad_cmd(update, ctx):
    msg, user = update.message, update.effective_user
    usage = ("Usage: <code>/addsquad [Team Name or Code] @player</code>\n"
             "Or reply to the player's message: <code>/addsquad SS</code>")
    t, tm, rest, err = _locate(update, ctx.args)
    if err:
        await _reply(msg, err + "\n\n" + usage)
        return
    if not (_can_edit(t, user.id) or _is_lead(tm, user.id)):
        await msg.reply_text("Only the host, a sudo user, the owner or this team's captain / "
                             "vice-captain can add players.")
        return
    if _team_busy(t, tm["id"]):
        await msg.reply_text("This team is in a match right now. Squad changes wait until it ends.")
        return
    players, errs = await _players_from(ctx, msg, rest)
    if not players and not errs:
        await _reply(msg, usage)
        return
    out = [f"❌ {e}" for e in errs]
    for uid, name in players:
        e = _squad_add(t, tm, uid, name)
        out.append(f"❌ {e}" if e else f"✅ {B.link(uid, name)} added")
    _save()
    await _reply(msg, "\n".join(out) + f"\n\n👥 {esc(tm['name'])} <code>{esc(tm['code'])}</code> · "
                                       f"{len(tm['players'])} players")


async def removesquad_cmd(update, ctx):
    msg, user = update.message, update.effective_user
    usage = ("Usage: <code>/removesquad [Team Name or Code] @player</code>\n"
             "Or reply to the player's message: <code>/removesquad SS</code>")
    t, tm, rest, err = _locate(update, ctx.args)
    if err:
        await _reply(msg, err + "\n\n" + usage)
        return
    if not _can_edit(t, user.id):
        await msg.reply_text("Only the tournament host, a sudo user or the owner can remove players.")
        return
    if _team_busy(t, tm["id"]):
        await msg.reply_text("This team is in a match right now. Squad changes wait until it ends.")
        return
    players, errs = await _players_from(ctx, msg, rest, tm["players"])
    if not players and not errs:
        await _reply(msg, usage)
        return
    out = [f"❌ {e}" for e in errs]
    for uid, name in players:
        pl = next((p for p in tm["players"] if p["uid"] == uid), None)
        if not pl:
            out.append(f"❌ {esc(name)} isn't in {esc(tm['name'])}.")
        elif len(tm["players"]) <= B.MIN_TEAM:
            out.append(f"❌ {esc(tm['name'])} needs at least {B.MIN_TEAM} players.")
        else:
            tm["players"].remove(pl)
            note = ""
            if tm.get("cap") == uid:
                tm["cap"], tm["vc"] = tm.get("vc"), None
                note = " · captain slot passed to the vice-captain; set a new VC with /addvc"
            elif tm.get("vc") == uid:
                tm["vc"] = None
                note = " · set a new vice-captain with /addvc"
            out.append(f"🗑 {esc(pl['name'])} removed{note}")
    _save()
    await _reply(msg, "\n".join(out) + f"\n\n👥 {esc(tm['name'])} <code>{esc(tm['code'])}</code> · "
                                       f"{len(tm['players'])} players")


async def tourteams_cmd(update, ctx):
    t = _read_tour(update)
    if not t:
        await update.message.reply_text("I couldn't tell which tournament. Use this in its group.")
        return
    if ctx.args:
        tm, _ = _split_team(t, list(ctx.args))
        if not tm:
            await update.message.reply_text("No such team.")
            return
        rows = [tm]
        await _send_logo(ctx, update.effective_chat.id, tm, f"<b>{esc(tm['name'])}</b> <code>{esc(tm['code'])}</code>")
    else:
        rows = list(t["teams"].values())
    lines = []
    for tm in rows:
        cap, vc = _pname(tm, tm.get("cap")), _pname(tm, tm.get("vc"))
        lines.append(f"<code>{esc(tm['code'])}</code> · <b>{esc(tm['name'])}</b> · {len(tm['players'])} players"
                     + (f" · Group {tm['group']}" if tm.get("group") else ""))
        lines.append(f"    © {esc(cap or '—')} · VC {esc(vc or '—')}")
        if len(rows) == 1:
            lines += [f"    {n}. {esc(p['name'])}" for n, p in enumerate(tm["players"], 1)]
    await B.say_long(ctx, update.effective_chat.id, head(f"👥 {esc(t['name']).upper()} · TEAMS") + "\n".join(lines))


# ------------------------------------------------------------------ match-time lock, reminders, /schedule
LOCK_SECS = 300                    # normal play is locked 5 minutes before a scheduled match
REMIND_MARKS = (30, 15, 10, 5)     # minutes before the match: every squad player gets a DM
GROUP_DOT = {"A": "🔵", "B": "🟢", "C": "🟡", "D": "🔴", "E": "🟣", "F": "🟠", "G": "⚪", "H": "🟤"}


def _left(secs):
    secs = max(0, int(secs))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    mi = rem // 60
    if d:
        return f"{d}d {h}h {mi}m"
    if h:
        return f"{h}h {mi}m"
    return f"{mi}m" if mi else "less than a minute"


def lock_info(chat_id):
    """-> (tournament, match, kind) when this chat is reserved for a tournament match, else None.
    kind: 'soon' (scheduled, due within 5 minutes or waiting to start), 'lineup' or 'live'."""
    now, best = time.time(), None
    for t in DB["items"].values():
        for m in t["matches"]:
            if m.get("venue") != chat_id:
                continue
            if m["status"] in ("lineup", "live"):
                return t, m, m["status"]
            if m["status"] == "scheduled" and m.get("ts") and -1800 < m["ts"] - now <= LOCK_SECS:
                if best is None or m["ts"] < best[1]["ts"]:
                    best = (t, m, "soon")
    return best


def _lock_text(t, m, kind):
    a, b = _tname(t, m["a"]), _tname(t, m["b"])
    if kind == "soon":
        left = m["ts"] - time.time()
        when = f"starts in <b>{_left(left)}</b>" if left > 0 else "is about to start"
        why = f"<b>M{m['no']}</b> · {esc(a)} 🆚 {esc(b)} {when}."
    else:
        why = f"<b>M{m['no']}</b> · {esc(a)} 🆚 {esc(b)} is on in this group."
    return f"🔒 <b>Locked</b> — nobody can start a match here right now.\n{why}"


async def _lock_cmd(update, ctx):
    info = lock_info(update.effective_chat.id)
    if info:
        await update.message.reply_text(_lock_text(*info), parse_mode=B.HTML)
        raise ApplicationHandlerStop


async def _lock_join_cmd(update, ctx):
    info = lock_info(update.effective_chat.id)
    if info and info[2] != "live":  # joining is only blocked until the tournament match begins
        await update.message.reply_text(_lock_text(*info), parse_mode=B.HTML)
        raise ApplicationHandlerStop


async def _lock_cb(update, ctx):
    q = update.callback_query
    info = lock_info(q.message.chat_id)
    if info:
        t, m, kind = info
        left = m["ts"] - time.time() if kind == "soon" else 0
        txt = (f"🔒 Locked: M{m['no']} starts in {_left(left)}." if kind == "soon" and left > 0
               else f"🔒 Locked: M{m['no']} is about to be played here.")
        await q.answer(txt, show_alert=True)
        raise ApplicationHandlerStop


async def _remind(ctx, t, m, now):
    """DMs every squad player at 30 / 15 / 10 / 5 minutes before the match. -> True if state changed."""
    st = m.get("rem")
    if not st or st.get("ts") != m["ts"]:  # a new date/time starts the countdown again
        st = m["rem"] = {"ts": m["ts"], "sent": []}
    mins_left = (m["ts"] - now) / 60
    due = [k for k in REMIND_MARKS if mins_left <= k and k not in st["sent"]]
    if not due:
        return False
    st["sent"] += due
    mark = min(due)  # after a long downtime only the closest reminder goes out
    mins = max(1, int(mins_left + 0.999))
    mins = min(mins, mark)
    a, b = t["teams"][m["a"]], t["teams"][m["b"]]
    when = datetime.fromtimestamp(m["ts"], _tz(t["rules"]["tz"]))
    text = (f"⏰ <b>{fancy('MATCH REMINDER')}</b>\n{B.LINE}\n"
            f"🏆 {esc(t['name'])} · <b>M{m['no']}</b>\n"
            f"🔴 {esc(a['name'])}  🆚  🔵 {esc(b['name'])}\n"
            f"🕒 {when.strftime('%a %d %b, %I:%M %p')} ({when.tzname()})\n"
            f"🏟 {esc(_vtitle(t, m['venue']))}\n{B.LINE}\n"
            f"🚨 Your match starts in <b>{mins} minute{'s' if mins != 1 else ''}</b>. Be ready!")
    failed, seen = [], set()
    for tm in (a, b):
        for pl in tm["players"]:
            if pl["uid"] in seen:
                continue
            seen.add(pl["uid"])
            try:
                await ctx.bot.send_message(pl["uid"], text, parse_mode=B.HTML)
            except TelegramError:
                failed.append(pl)
    if failed and not m.get("rem_told"):  # one note in the group for players the bot can't DM yet
        m["rem_told"] = True
        names = ", ".join(B.link(p["uid"], p["name"]) for p in failed[:20])
        try:
            await B.say(ctx, t["home"], f"📣 <b>M{m['no']}</b> {esc(a['name'])} vs {esc(b['name'])} is coming up. "
                        f"I couldn't DM: {names}. Press <b>Start</b> in my DM to get match reminders.")
        except TelegramError:
            pass
    return True


async def schedule_cmd(update, ctx):
    """/schedule - upcoming tournament matches: group, teams, date & time, countdown, venue."""
    t = _read_tour(update)
    if not t:
        await update.message.reply_text("I couldn't tell which tournament. Use this in its group.")
        return
    now, tz = time.time(), t["rules"]["tz"]
    rows = [m for m in t["matches"] if m["status"] in ("lineup", "live")
            or (m["status"] == "scheduled" and m.get("ts"))]
    rows.sort(key=lambda m: (m["status"] not in ("lineup", "live"), m.get("ts") or 0, m["no"]))
    waiting = sum(1 for m in t["matches"] if m["status"] == "pending")
    if not rows:
        extra = f"\n{waiting} match(es) are waiting for a date & time." if waiting else ""
        await update.message.reply_text("No matches are scheduled right now." + extra)
        return
    out = [f"📅 <b>{esc(t['name'])}</b> — <b>CURRENTLY SCHEDULED MATCHES</b>", ""]
    for n, m in enumerate(rows, 1):
        a, b = _tname(t, m["a"]), _tname(t, m["b"])
        stage = m.get("stage", "group")
        if stage == "group":
            label = f"{GROUP_DOT.get(str(m['group']), '🔹')} <b>G{esc(m['group'])}:</b>"
        else:
            label = f"🏆 <b>{esc(mtitle(m).title())}:</b>"
        out.append(f"{n}. {label} {esc(a)} 🆚 {esc(b)}")
        if m["status"] in ("lineup", "live"):
            out.append("🔴 <b>LIVE NOW</b>" if m["status"] == "live" else "📝 <b>Picking Playing XI</b>")
        else:
            when = datetime.fromtimestamp(m["ts"], _tz(tz))
            out.append(f"⏰ {when.strftime('%a %d %b, %I:%M %p')} ({when.tzname()})")
            out.append(f"⏳ {_left(m['ts'] - now) if m['ts'] > now else 'starting soon'}")
        out.append(f"🏟 Venue: {esc(_vtitle(t, m['venue']))}")
        out.append("")
    if waiting:
        out.append(f"🗓 {waiting} more match(es) waiting for a date & time.")
    await B.say_long(ctx, update.effective_chat.id, "\n".join(out).rstrip())


# ------------------------------------------------------------------ /timezone [Country]
COUNTRY_TZ = {
    "india": "Asia/Kolkata", "pakistan": "Asia/Karachi", "bangladesh": "Asia/Dhaka", "nepal": "Asia/Kathmandu",
    "sri lanka": "Asia/Colombo", "srilanka": "Asia/Colombo", "bhutan": "Asia/Thimphu", "maldives": "Indian/Maldives",
    "afghanistan": "Asia/Kabul", "uae": "Asia/Dubai", "united arab emirates": "Asia/Dubai", "dubai": "Asia/Dubai",
    "saudi arabia": "Asia/Riyadh", "saudi": "Asia/Riyadh", "qatar": "Asia/Qatar", "kuwait": "Asia/Kuwait",
    "oman": "Asia/Muscat", "bahrain": "Asia/Bahrain", "iran": "Asia/Tehran", "iraq": "Asia/Baghdad",
    "turkey": "Europe/Istanbul", "israel": "Asia/Jerusalem", "jordan": "Asia/Amman", "lebanon": "Asia/Beirut",
    "singapore": "Asia/Singapore", "malaysia": "Asia/Kuala_Lumpur", "indonesia": "Asia/Jakarta",
    "thailand": "Asia/Bangkok", "vietnam": "Asia/Ho_Chi_Minh", "philippines": "Asia/Manila",
    "myanmar": "Asia/Yangon", "cambodia": "Asia/Phnom_Penh", "china": "Asia/Shanghai", "hong kong": "Asia/Hong_Kong",
    "taiwan": "Asia/Taipei", "japan": "Asia/Tokyo", "south korea": "Asia/Seoul", "korea": "Asia/Seoul",
    "mongolia": "Asia/Ulaanbaatar", "kazakhstan": "Asia/Almaty", "uzbekistan": "Asia/Tashkent",
    "australia": "Australia/Sydney", "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "perth": "Australia/Perth", "brisbane": "Australia/Brisbane", "adelaide": "Australia/Adelaide",
    "new zealand": "Pacific/Auckland", "nz": "Pacific/Auckland", "fiji": "Pacific/Fiji",
    "uk": "Europe/London", "united kingdom": "Europe/London", "england": "Europe/London", "britain": "Europe/London",
    "scotland": "Europe/London", "wales": "Europe/London", "ireland": "Europe/Dublin", "france": "Europe/Paris",
    "germany": "Europe/Berlin", "spain": "Europe/Madrid", "portugal": "Europe/Lisbon", "italy": "Europe/Rome",
    "netherlands": "Europe/Amsterdam", "belgium": "Europe/Brussels", "switzerland": "Europe/Zurich",
    "austria": "Europe/Vienna", "sweden": "Europe/Stockholm", "norway": "Europe/Oslo", "denmark": "Europe/Copenhagen",
    "finland": "Europe/Helsinki", "poland": "Europe/Warsaw", "greece": "Europe/Athens", "romania": "Europe/Bucharest",
    "ukraine": "Europe/Kyiv", "russia": "Europe/Moscow", "moscow": "Europe/Moscow",
    "egypt": "Africa/Cairo", "south africa": "Africa/Johannesburg", "nigeria": "Africa/Lagos", "kenya": "Africa/Nairobi",
    "ghana": "Africa/Accra", "ethiopia": "Africa/Addis_Ababa", "morocco": "Africa/Casablanca", "algeria": "Africa/Algiers",
    "zimbabwe": "Africa/Harare", "tanzania": "Africa/Dar_es_Salaam", "uganda": "Africa/Kampala",
    "usa": "America/New_York", "us": "America/New_York", "united states": "America/New_York", "america": "America/New_York",
    "usa eastern": "America/New_York", "usa central": "America/Chicago", "usa mountain": "America/Denver",
    "usa pacific": "America/Los_Angeles", "california": "America/Los_Angeles", "new york": "America/New_York",
    "canada": "America/Toronto", "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    "mexico": "America/Mexico_City", "brazil": "America/Sao_Paulo", "argentina": "America/Argentina/Buenos_Aires",
    "chile": "America/Santiago", "colombia": "America/Bogota", "peru": "America/Lima", "venezuela": "America/Caracas",
    "jamaica": "America/Jamaica", "trinidad": "America/Port_of_Spain", "west indies": "America/Port_of_Spain",
    "barbados": "America/Barbados", "guyana": "America/Guyana",
}
TZ_ABBR = {"ist": "Asia/Kolkata", "pkt": "Asia/Karachi", "gst": "Asia/Dubai", "utc": "UTC", "gmt": "UTC",
           "bst": "Europe/London", "cet": "Europe/Paris", "est": "America/New_York", "pst": "America/Los_Angeles",
           "aest": "Australia/Sydney", "sast": "Africa/Johannesburg", "nzst": "Pacific/Auckland"}


def resolve_tz(text):
    """'India' -> 'Asia/Kolkata'; also an IANA name, a UTC offset, an abbreviation (IST) or a city."""
    raw = " ".join(text.split())
    low = raw.lower().strip(" .")
    if not low:
        return None
    if re.fullmatch(r"(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", low):
        return re.sub(r"^(?:utc|gmt)\s*", "", low).replace(" ", "")
    if low in COUNTRY_TZ:
        return COUNTRY_TZ[low]
    if low in TZ_ABBR:
        return TZ_ABBR[low]
    if "/" in raw and _zone_exists(raw):
        return raw
    try:
        from zoneinfo import available_timezones
        zones = available_timezones()
    except Exception:
        zones = set()
    key = low.replace(" ", "_")
    for z in sorted(zones):  # a city name: 'Dubai', 'Karachi', 'New York'
        if z.lower().rsplit("/", 1)[-1] == key:
            return z
    return None


async def timezone_cmd(update, ctx):
    """/timezone [Country] - sets the timezone the tournament schedule is read and shown in."""
    msg, user = update.message, update.effective_user
    t = _read_tour(update)
    if not t:
        await msg.reply_text("I couldn't tell which tournament. Use this in the tournament group.")
        return
    cur = t["rules"]["tz"]
    if not ctx.args:
        now = datetime.now(_tz(cur))
        await _reply(msg, f"🌍 Tournament timezone: <b>{esc(cur)}</b> ({esc(now.tzname())}) · now "
                          f"{now.strftime('%d %b, %I:%M %p')}\nChange it: <code>/timezone [Country]</code>, "
                          "e.g. <code>/timezone India</code>")
        return
    if not _can_edit(t, user.id):
        await msg.reply_text("Only the tournament host or a sudo user can change the timezone.")
        return
    zone = resolve_tz(" ".join(ctx.args))
    if not zone or not _tz_ok(zone):
        await _reply(msg, "I don't know that place. Send a country or city, e.g. <code>/timezone India</code>, "
                          "<code>/timezone Dubai</code>, an IANA name like <code>Asia/Kolkata</code> or an "
                          "offset like <code>+05:30</code>.")
        return
    t["rules"]["tz"] = zone
    _save()
    now = datetime.now(_tz(zone))
    off = now.strftime("%z")
    await _reply(msg, f"🌍 <b>Timezone set</b> · <b>{esc(zone)}</b> ({esc(now.tzname())}, UTC{off[:3]}:{off[3:]})\n"
                      f"🕒 Time there now: {now.strftime('%a %d %b, %I:%M %p')}\n"
                      "📅 Times you set from now on are read in this zone; matches already scheduled keep "
                      "their exact moment and are just shown in the new zone.")


def _ord(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _form(t, tid, stage, last=5):
    """Last results of a team in the order they were played: 'W-L-W-W-W'."""
    ms = [m for m in t["matches"] if m["status"] == "done" and m.get("res") and m.get("stage", "group") == stage
          and tid in (m["a"], m["b"])]
    ms.sort(key=lambda m: (m.get("ts") or 0, m["no"]))
    out = ["T" if m["res"]["winner"] is None else ("W" if m["res"]["winner"] == tid else "L") for m in ms]
    return "-".join(out[-last:]) or "—"


def _qual_count(t, size):
    q = t.get("qual_per_group")
    if q:
        return q
    n = max(1, len(t["groups"]))
    return min(size, max(1, -(-4 // n)))


def table_fancy(t, stage="group"):
    """Points table with rank, team (code), P/W/L/Points, last-5 form and net run rate."""
    if stage == "super":
        groups = {"SUPER": t.get("super", {}).get("teams", [])}
    else:
        groups = t["groups"]
    out = []
    for gname, tids in groups.items():
        dot = GROUP_DOT.get(str(gname), "🔹") if stage != "super" else "⚡"
        title = f"SUPER {len(tids)}" if stage == "super" else f"GROUP {esc(gname)}"
        qn = 4 if stage == "super" else _qual_count(t, len(tids))
        lines = [f"{dot} <b>{title}</b>"]
        for pos, r in enumerate(ranked(t, tids, stage), 1):
            tm = t["teams"][r["tid"]]
            medal = "🏅 " if pos <= qn else ""
            tie = f" | T: {r['t']}" if r["t"] else ""
            lines.append(f"{medal}<b>{_ord(pos)}</b> | {esc(tm['name'])} ({esc(tm['code'])})\n"
                         f"      P: {r['p']} | W: {r['w']} | L: {r['l']}{tie} | Points: {r['pts']}\n"
                         f"      Form: {_form(t, r['tid'], stage)} | Net Run Rate: {r['nrr']:+.3f}")
        out.append(lines[0] + "\n" + "\n\n".join(lines[1:]))
    return "\n\n".join(out)


async def pointstable_cmd(update, ctx):
    """/pointstable - the points table (every group, plus the Super stage when there is one)."""
    t = _read_tour(update)
    if not t:
        await update.message.reply_text("I couldn't tell which tournament. Use this in its group.")
        return
    chat = update.effective_chat.id
    if not t["groups"]:
        await update.message.reply_text("There is no points table yet — the groups haven't been made.")
        return
    await _banner(ctx, chat, "tour_table", f"📊 <b>{fancy('POINTS TABLE')}</b>")
    text = f"🏆 <b>{esc(t['name'])}</b> - <b>Group Stage Points Table</b>\n\n" + table_fancy(t, "group")
    if t.get("super"):
        text += f"\n\n⚡ <b>Super Stage Points Table</b>\n\n" + table_fancy(t, "super")
    text += "\n\n<i>🏅 qualifying places · Form = last 5 results (oldest → latest)</i>"
    await B.say_long(ctx, chat, text)


# ------------------------------------------------------------------ /endtour
async def endtour_cmd(update, ctx):
    """/endtour - the host or a sudo user ends the tournament (unplayed matches are cancelled)."""
    msg, user = update.message, update.effective_user
    t = _read_tour(update)
    if not t:
        await msg.reply_text("I couldn't tell which tournament. Use this in the tournament group.")
        return
    if not _can_edit(t, user.id):
        await msg.reply_text("Only the tournament host or a sudo user can end the tournament.")
        return
    if t.get("phase") == "done":
        await msg.reply_text("This tournament has already ended.")
        return
    busy = [m for m in t["matches"] if m["status"] in ("lineup", "live")]
    if busy:
        await _reply(msg, f"🔴 <b>M{busy[0]['no']}</b> is being played right now. Let it finish "
                          "(or stop it with /endgame) before ending the tournament.")
        return
    left = sum(1 for m in t["matches"] if m["status"] in ("pending", "scheduled"))
    await _reply(msg, f"⚠️ <b>End {esc(t['name'])}?</b>\n{B.LINE}\n"
                      f"🗓 <b>{left}</b> unplayed match(es) will be cancelled.\n"
                      "📊 Results, points table and records stay and can still be viewed.\n"
                      "This can't be undone.")
    await msg.reply_text("Are you sure?", reply_markup=kb([[btn("✅ Yes, end it", f"tour:et:{t['id']}"),
                                                             btn("❌ No", f"tour:etn:{t['id']}")]]))


async def _endtour_cb(q, ctx, p):
    act, t = p[1], _tour(p[2]) if len(p) > 2 else None
    if not t:
        await q.answer("This tournament no longer exists.", show_alert=True)
        return
    if not _can_edit(t, q.from_user.id):
        await q.answer("Only the tournament host or a sudo user can do this.", show_alert=True)
        return
    if act == "etn":
        await q.answer()
        await B.edit_any(q.message, "👍 Cancelled — the tournament continues.")
        return
    if t.get("phase") == "done":
        await q.answer("Already ended.", show_alert=True)
        return
    if any(m["status"] in ("lineup", "live") for m in t["matches"]):
        await q.answer("A match is being played right now. Let it finish first.", show_alert=True)
        return
    await q.answer("Tournament ended 🏁")
    cancelled = 0
    for m in t["matches"]:
        if m["status"] in ("pending", "scheduled"):
            m["status"], m["ts"], m["venue"] = "pending", None, None
            m.pop("rem", None)
            cancelled += 1
    t["phase"], t["ended_early"] = "done", True
    _save()
    await B.edit_any(q.message, "🏁 Tournament ended.")
    parts = [head(f"🏁 {esc(t['name']).upper()} · ENDED"),
             f"🗓 {cancelled} unplayed match(es) cancelled."]
    if t["groups"] and any(m["status"] == "done" for m in t["matches"]):
        parts += [B.LINE, f"🏆 <b>Final Points Table</b>\n\n" + table_fancy(t, "group")]
    for title, fn in (("🟠 ORANGE CAP", orange_text), ("🟣 PURPLE CAP", purple_text),
                      ("🏅 MAN OF THE TOURNAMENT", mvp_text)):
        body = fn(t, 1)
        if body:
            parts.append(f"<b>{title}</b>\n{body}")
    await B.say_long(ctx, t["home"], "\n".join(parts))


# ------------------------------------------------------------------ /freewin  /forfeit
def _staff_tour(update):
    """-> (tournament, error) for host / sudo only commands."""
    t = _read_tour(update)
    if not t:
        return None, "I couldn't tell which tournament. Use this in the tournament group."
    if not _can_edit(t, update.effective_user.id):
        return None, "Only the tournament host or a sudo user can use this."
    if t.get("phase") == "done":
        return None, "This tournament has already ended."
    return t, None


async def freewin_cmd(update, ctx):
    """/freewin [Match_Number] [Winning_Team_Code] - gives that match to a team without playing it."""
    msg = update.message
    t, err = _staff_tour(update)
    if err:
        await msg.reply_text(err)
        return
    args = list(ctx.args)
    if len(args) < 2 or not args[0].lstrip("mM#").isdigit():
        await _reply(msg, "Usage: <code>/freewin [Match_Number] [Winning_Team_Code]</code>\n"
                          "Example: <code>/freewin 5 SS</code>")
        return
    m = _match(t, args[0].lstrip("mM#"))
    if not m:
        await msg.reply_text("There is no such match number.")
        return
    if m["status"] == "done":
        await msg.reply_text(f"M{m['no']} has already been completed.")
        return
    if m["status"] in ("lineup", "live"):
        await _reply(msg, f"🔴 <b>M{m['no']}</b> is being played right now. Let it finish or stop it with /endgame first.")
        return
    tm = _find_team(t, " ".join(args[1:]))
    if not tm or tm["id"] not in (m["a"], m["b"]):
        await _reply(msg, f"That team isn't in M{m['no']}: <b>{esc(_tname(t, m['a']))}</b> "
                          f"(<code>{esc(t['teams'][m['a']]['code'])}</code>) vs <b>{esc(_tname(t, m['b']))}</b> "
                          f"(<code>{esc(t['teams'][m['b']]['code'])}</code>).")
        return
    other = m["b"] if tm["id"] == m["a"] else m["a"]
    await _award(ctx, t, m, tm["id"], f"{tm['name']} won by free win",
                 f"🎁 Free win given to <b>{esc(tm['name'])}</b> over <b>{esc(_tname(t, other))}</b>.",
                 [update.effective_chat.id, t["home"]])


async def forfeit_cmd(update, ctx):
    """/forfeit [Team_Code] - the team leaves the tournament; every team it had a match with gets a free win."""
    msg = update.message
    t, err = _staff_tour(update)
    if err:
        await msg.reply_text(err)
        return
    if not ctx.args:
        await _reply(msg, "Usage: <code>/forfeit [Team_Code]</code>\nSee the codes with /toursquad.")
        return
    tm = _find_team(t, " ".join(ctx.args))
    if not tm:
        await msg.reply_text("I couldn't find that team. Use its code (see /toursquad).")
        return
    if tm.get("out"):
        await msg.reply_text(f"{tm['name']} has already forfeited.")
        return
    if any(m["status"] in ("lineup", "live") and tm["id"] in (m["a"], m["b"]) for m in t["matches"]):
        await _reply(msg, f"🔴 <b>{esc(tm['name'])}</b> are playing right now. Let that match finish "
                          "(or stop it with /endgame) first.")
        return
    left = [m for m in t["matches"] if m["status"] in ("pending", "scheduled") and tm["id"] in (m["a"], m["b"])]
    await _reply(msg, f"⚠️ <b>{esc(tm['name'])}</b> will forfeit <b>{esc(t['name'])}</b>.\n{B.LINE}\n"
                      f"🎁 <b>{len(left)}</b> unplayed match(es) become free wins for their opponents.\n"
                      "Matches already played keep their results. This can't be undone.")
    await msg.reply_text("Are you sure?", reply_markup=kb([[btn("✅ Yes, forfeit", f"tour:ff:{t['id']}:{tm['id']}"),
                                                             btn("❌ No", f"tour:ffn:{t['id']}:{tm['id']}")]]))


async def _forfeit_cb(q, ctx, p):
    act, t = p[1], _tour(p[2]) if len(p) > 3 else None
    tm = t["teams"].get(p[3]) if t else None
    if not t or not tm:
        await q.answer("This tournament no longer exists.", show_alert=True)
        return
    if not _can_edit(t, q.from_user.id):
        await q.answer("Only the tournament host or a sudo user can do this.", show_alert=True)
        return
    if act == "ffn":
        await q.answer()
        await B.edit_any(q.message, "👍 Cancelled.")
        return
    if tm.get("out") or t.get("phase") == "done":
        await q.answer("Already done.", show_alert=True)
        return
    if any(m["status"] in ("lineup", "live") and tm["id"] in (m["a"], m["b"]) for m in t["matches"]):
        await q.answer("This team is playing right now. Let that match finish first.", show_alert=True)
        return
    await q.answer("Forfeit recorded.")
    tm["out"] = True
    await B.edit_any(q.message, f"🚫 {tm['name']} forfeited the tournament.")
    lines, groups_hit, playoff = [], [], []
    for m in t["matches"]:
        if m["status"] in ("pending", "scheduled") and tm["id"] in (m["a"], m["b"]):
            win = m["b"] if tm["id"] == m["a"] else m["a"]
            _award_res(t, m, win, f"{_tname(t, win)} won by walkover ({tm['name']} forfeited the tournament)")
            lines.append(f"🎁 <b>M{m['no']}</b> · {esc(_tname(t, win))} beat {esc(tm['name'])} (free win)")
            if m.get("stage", "group") == "playoff":
                playoff.append(m)
            elif m.get("stage", "group") == "group" and m.get("group") not in groups_hit:
                groups_hit.append(m["group"])
    _save()
    text = (head(f"🚫 {esc(tm['name']).upper()} · FORFEIT") +
            f"{esc(tm['name'])} <code>{esc(tm['code'])}</code> has left <b>{esc(t['name'])}</b>.\n{B.LINE}\n" +
            ("\n".join(lines) if lines else "No unplayed matches were left."))
    for g in groups_hit:
        text += f"\n\n📊 <b>GROUP {esc(g)}</b>\n" + table_text(t, "group", only=g)
    last = next((m for m in reversed(t["matches"]) if m["status"] == "done" and m.get("stage", "group") == "group"), None)
    if _group_stage_over(t) and last:
        text += (f"\n{B.LINE}\n🏆 <b>Group stage complete!</b>\n" +
                 f"👑 {B.link(t['host_id'], t['host_name'])}: send <b>/endgroupstage</b> to choose who qualifies.")
    await B.say_long(ctx, t["home"], text)
    if q.message.chat_id != t["home"]:
        await B.say_long(ctx, q.message.chat_id, text)
    for m in playoff:
        await _progress_playoffs(ctx, t, m)


# ------------------------------------------------------------------ /toursquad  /squad
def _squad_text(t, tm):
    cap, vc = tm.get("cap"), tm.get("vc")
    lines = [f"🪪 <b>SQUAD - {esc(tm['name'])} ({esc(tm['code'])})</b>",
             f"🏆 Tour: <b>{esc(t['name'])}</b>"]
    if tm.get("group"):
        lines.append(f"🧩 Group: <b>{esc(tm['group'])}</b>")
    lines += ["", f"👥 Players: <b>{len(tm['players'])}/{SQUAD_MAX}</b>"]
    for n, p in enumerate(tm["players"], 1):
        tag = " 🧢" if p["uid"] == cap else (" 🏅" if p["uid"] == vc else "")
        lines.append(f"  {n:02d}. {esc(p['name'])}{tag}")
    if not tm["players"]:
        lines.append("  No players yet.")
    lines += ["", "🧢 Captain  ·  🏅 Vice-captain"]
    return "\n".join(lines)


async def squad_cmd(update, ctx):
    """/squad [Team Name or Code] - the full squad of a tournament team, with its logo."""
    msg, chat = update.message, update.effective_chat.id
    if not DB["items"]:
        await msg.reply_text("There is no tournament yet.")
        return
    if not ctx.args:
        await _reply(msg, "Usage: <code>/squad [Team Name or Code]</code>\nSee all teams with /toursquad.")
        return
    t, tm, _, err = _locate(update, list(ctx.args))
    if err:
        await _reply(msg, "I couldn't find that team. Use its exact name or code (see /toursquad).")
        return
    text = _squad_text(t, tm)
    close = kb([[btn("✖ Close", "tour:close")]])
    sent = False
    val = tm.get("logo")
    if val:
        kind, fid = val.split(":", 1)
        send = {"photo": ctx.bot.send_photo, "video": ctx.bot.send_video,
                "animation": ctx.bot.send_animation}.get(kind)
        try:
            if len(text) <= 1000:  # a caption holds 1024 characters: the whole squad fits under the logo
                await send(chat, fid, caption=text, parse_mode=B.HTML, reply_markup=close)
            else:
                await send(chat, fid)
                await ctx.bot.send_message(chat, text, parse_mode=B.HTML, reply_markup=close)
            sent = True
        except TelegramError:
            sent = False
    if not sent:
        await ctx.bot.send_message(chat, text, parse_mode=B.HTML, reply_markup=close)


async def toursquad_cmd(update, ctx):
    """/toursquad - every team in the tournament of this group."""
    t = _read_tour(update)
    if not t:
        await update.message.reply_text("I couldn't tell which tournament. Use this in its group.")
        return
    teams = list(t["teams"].values())
    if not teams:
        await update.message.reply_text("No teams have been added to this tournament yet.")
        return
    lines = [f"🏆 <b>{esc(t['name'])}</b>", f"👥 Teams: <b>{len(teams)}</b>", ""]
    for n, tm in enumerate(teams, 1):
        cap = _pname(tm, tm.get("cap"))
        lines.append(f"<b>{n:02d}. {esc(tm['name'])}</b> (<code>{esc(tm['code'])}</code>)"
                     + (f" · Group {esc(tm['group'])}" if tm.get("group") else ""))
        lines.append(f"      👤 {len(tm['players'])} players · 🧢 {esc(cap or '—')}")
    lines += ["", "Use <code>/squad [Team Name or Code]</code> for a full squad, e.g. <code>/squad "
              + esc(teams[0]["code"]) + "</code>"]
    await B.say_long(ctx, update.effective_chat.id, head("🏆 TOURNAMENT TEAMS") + "\n".join(lines))


# ------------------------------------------------------------------ records, caps, MVP
def _card_of(g, side):
    out = []
    for u in g.teams[side]:
        p = g.players[u]
        out.append({"uid": p.uid, "name": p.name, "runs": p.runs, "balls": p.balls, "fours": p.fours,
                    "sixes": p.sixes, "wkts": p.wickets, "bb": p.balls_bowled, "rc": p.runs_conceded,
                    "bat": bool(p.batted)})
    return out


def _card_line(p):
    bat = f"<b>{p['runs']}</b> ({p['balls']})" if p["bat"] else "DNB"
    bowl = f" · 🎯 {p['wkts']}/{p['rc']} ({p['bb'] // 6}.{p['bb'] % 6})" if p["bb"] else ""
    return f"• {esc(p['name'])} — {bat}{bowl}"


def scorecard_text(t, m):
    r, c = m["res"], m.get("card")
    lines = [f"<b>M{m['no']} · {mtitle(m)}</b>"]
    for key, icon in (("a", "🔴"), ("b", "🔵")):
        s = r[key]
        lines.append(f"{icon} <b>{esc(_tname(t, m[key]))}</b> · {s['score']}/{s['wkts']} "
                     f"({s['balls'] // 6}.{s['balls'] % 6} ov)")
        if c:
            lines += [_card_line(p) for p in c[key]]
    lines.append(f"{'🏆' if r['winner'] else '🤝'} <b>{esc(r['text'])}</b>")
    if c and c.get("potm_name"):
        lines.append(f"🏅 Player of the Match: {esc(c['potm_name'])}")
    if not c:
        lines.append("<i>(walkover — the match was not played)</i>" if r.get("walkover")
                     else "<i>(no detailed scorecard was saved for this match)</i>")
    return "\n".join(lines)


def player_totals(t):
    agg = {}
    for m in t["matches"]:
        if m["status"] != "done" or not m.get("card"):
            continue
        for key in ("a", "b"):
            for p in m["card"][key]:
                a = agg.setdefault(p["uid"], {"uid": p["uid"], "name": p["name"], "tid": m[key], "m": 0,
                                              "inn": 0, "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
                                              "hs": 0, "wkts": 0, "bb": 0, "rc": 0, "bw": 0, "br": 0,
                                              "potm": 0})
                a["name"], a["m"] = p["name"], a["m"] + 1
                a["inn"] += int(p["bat"])
                for k in ("runs", "balls", "fours", "sixes", "wkts", "bb", "rc"):
                    a[k] += p[k]
                a["hs"] = max(a["hs"], p["runs"])
                if p["wkts"] > a["bw"] or (p["wkts"] and p["wkts"] == a["bw"] and p["rc"] < a["br"]):
                    a["bw"], a["br"] = p["wkts"], p["rc"]
        pu = m["card"].get("potm")
        if pu in agg:
            agg[pu]["potm"] += 1
    return agg


def mvp_points(a):
    return a["runs"] + a["fours"] + 2 * a["sixes"] + 20 * a["wkts"] + 25 * a["potm"]


def _code_of(t, a):
    return esc(t["teams"][a["tid"]]["code"])


def orange_text(t, n=5):
    rows = sorted((a for a in player_totals(t).values() if a["runs"] > 0),
                  key=lambda a: (-a["runs"], a["balls"]))[:n]
    if not rows:
        return None
    return "\n".join(f"{MEDALS[i]} <b>{esc(a['name'])}</b> <code>{_code_of(t, a)}</code> · <b>{a['runs']}</b> runs · "
                     f"{a['inn']} inn · HS {a['hs']} · SR {a['runs'] * 100 / a['balls'] if a['balls'] else 0:.1f}"
                     for i, a in enumerate(rows))


def purple_text(t, n=5):
    rows = sorted((a for a in player_totals(t).values() if a["wkts"] > 0),
                  key=lambda a: (-a["wkts"], a["rc"], a["bb"]))[:n]
    if not rows:
        return None
    return "\n".join(f"{MEDALS[i]} <b>{esc(a['name'])}</b> <code>{_code_of(t, a)}</code> · <b>{a['wkts']}</b> wkts · "
                     f"{a['bb'] // 6}.{a['bb'] % 6} ov · Eco {a['rc'] * 6 / a['bb'] if a['bb'] else 0:.2f} · "
                     f"Best {a['bw']}/{a['br']}" for i, a in enumerate(rows))


def mvp_text(t, n=5):
    rows = sorted((a for a in player_totals(t).values() if mvp_points(a) > 0),
                  key=lambda a: (-mvp_points(a), -a["wkts"], -a["runs"]))[:n]
    if not rows:
        return None
    return "\n".join(f"{MEDALS[i]} <b>{esc(a['name'])}</b> <code>{_code_of(t, a)}</code> · <b>{mvp_points(a)}</b> pts · "
                     f"{a['runs']} runs · {a['wkts']} wkts · {a['potm']} POTM" for i, a in enumerate(rows))


async def _stat_cmd(update, ctx, title, fn, foot="", key=None):
    t = _read_tour(update)
    if not t:
        await update.message.reply_text("I couldn't tell which tournament. Use this in its group.")
        return
    body = fn(t)
    if not body:
        await update.message.reply_text("No matches have been completed yet.")
        return
    if key:
        await _banner(ctx, update.effective_chat.id, key, f"<b>{fancy(title.split(' ', 1)[1])}</b>")
    await _reply(update.message, head(f"{title} · {esc(t['name']).upper()}") + body + foot)


async def orangecap_cmd(update, ctx):
    await _stat_cmd(update, ctx, "🟠 ORANGE CAP", orange_text, key="tour_orange")


async def purplecap_cmd(update, ctx):
    await _stat_cmd(update, ctx, "🟣 PURPLE CAP", purple_text, key="tour_purple")


async def mvprace_cmd(update, ctx):
    await _stat_cmd(update, ctx, "🏅 MAN OF THE TOURNAMENT RACE", mvp_text,
                    "\n\n<i>1 pt per run · +1 per four · +2 per six · 20 per wicket · 25 per Player of the Match</i>",
                    key="tour_mvp")


def _records_screen(t, page=0):
    done = [m for m in t["matches"] if m["status"] == "done" and m.get("res")]
    per = 10
    pages = max(1, (len(done) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    chunk = done[page * per:(page + 1) * per]
    rows = [[btn(f"M{m['no']} · {t['teams'].get(m.get('a'), {}).get('name', 'Team A')} vs {t['teams'].get(m.get('b'), {}).get('name', 'Team B')}"[:60],
                 f"tour:rc:{t['id']}:{m['no']}")] for m in chunk]
    nav = []
    if page > 0: nav.append(btn("◀️ Previous", f"tour:records:{t['id']}:{page-1}"))
    nav.append(btn(f"{page+1}/{pages}", f"tour:records:{t['id']}:{page}"))
    if page + 1 < pages: nav.append(btn("Next ▶️", f"tour:records:{t['id']}:{page+1}"))
    if nav: rows.append(nav)
    return head(f"📜 {esc(t['name']).upper()} · MATCH RECORDS") + f"Completed matches: {len(done)}\nSelect a match scorecard:", kb(rows) if rows else None

async def tourrecords_cmd(update, ctx):
    """Show completed match scorecards as buttons, 10 matches per page."""
    t = _read_tour(update)
    if not t:
        await update.message.reply_text("I couldn't tell which tournament. Use this in its group.")
        return
    arg = (ctx.args[0] if ctx.args else "").lstrip("mM#")
    if arg.isdigit():
        m = _match(t, arg)
        if not m or m["status"] != "done" or not m.get("res"):
            await update.message.reply_text("That match hasn't been completed.")
            return
        await B.say_long(ctx, update.effective_chat.id, head(f"📜 {esc(t['name']).upper()}") + scorecard_text(t, m))
        return
    done = [m for m in t["matches"] if m["status"] == "done" and m.get("res")]
    if not done:
        await update.message.reply_text("No match has been completed yet.")
        return
    text, markup = _records_screen(t, 0)
    await update.message.reply_text(text, parse_mode=B.HTML, reply_markup=markup)


# ------------------------------------------------------------------ group stage -> Super N -> playoffs
def _group_stage_over(t):
    gm = [m for m in t["matches"] if m.get("stage", "group") == "group"]
    return t.get("phase") == "group" and bool(gm) and all(m["status"] == "done" for m in gm)


def _egs_screen(t):
    sizes = [len(x) for x in t["groups"].values()]
    rows = []
    for q in range(1, (min(sizes) if sizes else 0) + 1):
        total = q * len(sizes)
        if total >= 4:
            rows.append([btn(f"{q} per group → {total} teams · " + ("Playoffs" if total == 4 else f"Super {total}"),
                             f"tour:eq:{t['id']}:{q}")])
    text = (head("🏁 END GROUP STAGE") + "All group matches are done 🎉\n\n"
            "How many teams qualify from <b>each group</b>?\n"
            "<i>4 teams in total → straight to the Playoffs; more → a Super stage first.</i>")
    return text, (kb(rows) if rows else None)


def _qualifiers(t, q):
    qual = []
    for tids in t["groups"].values():
        for pos, r in enumerate(ranked(t, tids, "group")[:q]):
            qual.append((pos, r))
    qual.sort(key=lambda x: (x[0], -x[1]["pts"], -x[1]["nrr"], -x[1]["w"]))
    return [r["tid"] for _, r in qual]


def _qual_text(t, q):
    ids = _qualifiers(t, q)
    nxt = "the <b>Playoffs</b>" if len(ids) == 4 else f"<b>Super {len(ids)}</b>"
    return (f"Top <b>{q}</b> from each group → <b>{len(ids)}</b> teams → {nxt}\n\n" +
            "\n".join(f"{i}. {esc(_tname(t, x))} <code>{esc(t['teams'][x]['code'])}</code>"
                      for i, x in enumerate(ids, 1)))


async def endgroupstage_cmd(update, ctx):
    msg, t = update.message, _host_tour(update)
    if not t:
        await msg.reply_text("I couldn't find a tournament you host here. Use it in the tournament's group.")
        return
    if t.get("phase") != "group":
        await msg.reply_text({"setup": "The teams aren't complete yet, so there's no group stage to end.",
                              "done": "This tournament is already complete."}.get(
            t.get("phase"), "The group stage has already been ended."))
        return
    left = [m for m in t["matches"] if m.get("stage", "group") == "group" and m["status"] != "done"]
    if left:
        await msg.reply_text(f"{len(left)} group match(es) are still to be played "
                             f"({', '.join('M' + str(m['no']) for m in left[:12])}"
                             f"{'…' if len(left) > 12 else ''}).")
        return
    text, markup = _egs_screen(t)
    if not markup:
        t["phase"] = "done"
        _save()
        await B.say_long(ctx, msg.chat_id, head(f"🏁 {esc(t['name']).upper()}") +
                         "Fewer than 4 teams can qualify, so there are no playoffs. Final standings:\n" + table_all(t))
        return
    await B.say(ctx, msg.chat_id, text, markup)


def _add_match(t, key, a, b):
    m = {"no": len(t["matches"]) + 1, "group": key.upper(), "round": 1, "a": a, "b": b, "ts": None,
         "venue": None, "status": "pending", "res": None, "stage": "playoff", "key": key}
    t["matches"].append(m)
    return m


async def _do_qualify(ctx, t, q):
    ids = _qualifiers(t, q)
    t["qual_per_group"] = q
    if len(ids) == 4:
        await _start_playoffs(ctx, t, ids)
        return
    t["phase"], t["super"] = "super", {"teams": ids, "q": q}
    t["matches"] += build_matches({"S": ids}, start_no=len(t["matches"]), stage="super")
    _save()
    n_new = len(t["matches"]) - sum(1 for m in t["matches"] if m.get("stage", "group") == "group")
    await _banner(ctx, t["home"], "tour_super", f"⚡ <b>{fancy('SUPER ' + str(len(ids)))}</b> · {esc(t['name'])}")
    await B.say_long(ctx, t["home"], head(f"⚡ SUPER {len(ids)}") +
                     "\n".join(f"{i}. {esc(_tname(t, x))} <code>{esc(t['teams'][x]['code'])}</code>"
                               for i, x in enumerate(ids, 1)) +
                     f"\n{B.LINE}\n📅 <b>{n_new}</b> matches (everyone plays everyone once, fresh points table)\n"
                     f"👑 {B.link(t['host_id'], t['host_name'])}: schedule them in /hpanel. "
                     "When the Super stage ends, send <b>/startplayoffs</b>.")


async def _start_playoffs(ctx, t, seeds):
    t["phase"], t["po"] = "playoffs", {"seeds": seeds}
    q1 = _add_match(t, "q1", seeds[0], seeds[1])
    el = _add_match(t, "el", seeds[2], seeds[3])
    _save()
    await _banner(ctx, t["home"], "tour_playoffs", f"🔥 <b>{fancy('PLAYOFFS')}</b> · {esc(t['name'])}")
    await B.say_long(ctx, t["home"], head(f"🏆 {esc(t['name']).upper()} · PLAYOFFS") +
                     "\n".join(f"#{i} {esc(_tname(t, x))} <code>{esc(t['teams'][x]['code'])}</code>"
                               for i, x in enumerate(seeds, 1)) + f"\n{B.LINE}\n" +
                     mline(t, q1) + "\n" + mline(t, el) +
                     f"\n{B.LINE}\nQualifier 2 (loser of Q1 vs winner of the Eliminator) and the Final are "
                     f"created automatically.\n👑 {B.link(t['host_id'], t['host_name'])}: schedule M{q1['no']} and "
                     f"M{el['no']} in /hpanel.")


async def startplayoffs_cmd(update, ctx):
    msg, t = update.message, _host_tour(update)
    if not t:
        await msg.reply_text("I couldn't find a tournament you host here. Use it in the tournament's group.")
        return
    phase = t.get("phase")
    if phase == "super":
        left = [m for m in t["matches"] if m.get("stage") == "super" and m["status"] != "done"]
        if left:
            await msg.reply_text(f"{len(left)} Super stage match(es) are still to be played "
                                 f"({', '.join('M' + str(m['no']) for m in left[:12])}"
                                 f"{'…' if len(left) > 12 else ''}).")
            return
        seeds = [r["tid"] for r in ranked(t, t["super"]["teams"], "super")[:4]]
        await _start_playoffs(ctx, t, seeds)
        return
    await msg.reply_text({"group": "The group stage isn't ended yet. Use /endgroupstage first.",
                          "setup": "The teams aren't complete yet.",
                          "playoffs": "The playoffs have already started.",
                          "done": "This tournament is already complete."}.get(phase, "Nothing to start."))


def _po_match(t, key):
    return next((m for m in t["matches"] if m.get("stage") == "playoff" and m.get("key") == key), None)


def _won(m):
    return bool(m and m["status"] == "done" and m.get("res") and m["res"]["winner"])


def _loser(m):
    return m["b"] if m["res"]["winner"] == m["a"] else m["a"]


async def _progress_playoffs(ctx, t, m):
    """IPL style: Q1 winner -> Final; Q1 loser vs Eliminator winner -> Q2; Q2 winner -> Final."""
    q1, el, q2, fin = (_po_match(t, k) for k in ("q1", "el", "q2", "final"))
    host = B.link(t["host_id"], t["host_name"])
    lines = []
    w = m["res"]["winner"]
    if m["key"] == "q1":
        lines.append(f"🏅 {esc(_tname(t, w))} are in the <b>Final</b>. {esc(_tname(t, _loser(m)))} get another chance in Qualifier 2.")
    elif m["key"] == "el":
        lines.append(f"❌ {esc(_tname(t, _loser(m)))} are eliminated. {esc(_tname(t, w))} play Qualifier 2.")
    elif m["key"] == "q2":
        lines.append(f"🏅 {esc(_tname(t, w))} reach the <b>Final</b>. ❌ {esc(_tname(t, _loser(m)))} are eliminated.")
    if _won(q1) and _won(el) and not q2:
        q2 = _add_match(t, "q2", _loser(q1), el["res"]["winner"])
        lines += ["", mline(t, q2), f"👑 {host}: schedule M{q2['no']} in /hpanel."]
    if _won(q2) and _won(q1) and not fin:
        fin = _add_match(t, "final", q1["res"]["winner"], q2["res"]["winner"])
        await _banner(ctx, t["home"], "tour_final",
                      f"🏆 <b>{fancy('THE FINAL')}</b>\n{esc(_tname(t, fin['a']))} 🆚 {esc(_tname(t, fin['b']))}")
        lines += ["", mline(t, fin), f"👑 {host}: schedule M{fin['no']} in /hpanel."]
    _save()
    if lines:
        await B.say_long(ctx, t["home"], "\n".join(lines))
    if _won(fin):
        await _finish_tournament(ctx, t, fin)


async def _finish_tournament(ctx, t, fin):
    t["phase"] = "done"
    _save()
    champ, runner = fin["res"]["winner"], _loser(fin)
    cap = f"🏆 <b>{fancy('CHAMPIONS')}</b>\n👑 {esc(_tname(t, champ))}"
    if not await _send_logo(ctx, t["home"], t["teams"][champ], cap):
        await _banner(ctx, t["home"], "tour_champion", cap)
    parts = [head(f"🏆 {esc(t['name']).upper()} · CHAMPIONS"),
             f"🥇 <b>{esc(_tname(t, champ))}</b>\n🥈 {esc(_tname(t, runner))}", B.LINE]
    for title, fn in (("🟠 ORANGE CAP", orange_text), ("🟣 PURPLE CAP", purple_text),
                      ("🏅 MAN OF THE TOURNAMENT", mvp_text)):
        body = fn(t, 1)
        if body:
            parts.append(f"<b>{title}</b>\n{body}")
    await B.say_long(ctx, t["home"], "\n".join(parts))


# ------------------------------------------------------------------ result hook (called by bot.t_finish)
async def on_finish(ctx, g, winner, inn):
    tid, no = g.tour
    t = _tour(tid)
    m = _match(t, no) if t else None
    if not m:
        return
    stage = m.get("stage", "group")
    cfg = m.get("cfg") or t["rules"]
    quota = cfg["overs"] * 6
    res = {"winner": None, "overs": cfg["overs"]}
    for side, key in (("A", "a"), ("B", "b")):
        i = inn[side]
        all_out = i["wkts"] >= len(g.teams[side]) - 1
        res[key] = {"score": i["score"], "wkts": i["wkts"], "balls": i["balls"],
                    "faced": quota if all_out else i["balls"], "n": len(g.teams[side])}
    a, b = _tname(t, m["a"]), _tname(t, m["b"])
    if winner:
        res["winner"] = m["a"] if winner == "A" else m["b"]
        wn = a if winner == "A" else b
        if winner == g.first["team"]:
            res["text"] = f"{wn} won by {abs(inn['A']['score'] - inn['B']['score'])} runs"
        else:
            left = len(g.teams[winner]) - 1 - inn[winner]["wkts"]
            res["text"] = f"{wn} won by {left} wicket{'s' if left != 1 else ''}"
    else:
        res["text"] = "Match tied"
    sa, sb = res["a"], res["b"]
    head_txt = (head(f"🏁 MATCH {no} RESULT") + f"🔴 {esc(a)} <b>{sa['score']}/{sa['wkts']}</b> ({sa['balls'] // 6}.{sa['balls'] % 6})\n"
                f"🔵 {esc(b)} <b>{sb['score']}/{sb['wkts']}</b> ({sb['balls'] // 6}.{sb['balls'] % 6})\n"
                f"{'🏆' if winner else '🤝'} <b>{esc(res['text'])}</b>\n{B.LINE}\n")
    if stage == "playoff" and not winner:  # a knockout can't end level: it is replayed
        m.update(status="pending", ts=None, res=None, card=None)
        m.pop("cfg", None)
        _save()
        await B.say_long(ctx, t["home"], head_txt + f"🔁 A playoff match can't be tied, so <b>M{no}</b> will be replayed.\n"
                         f"👑 {B.link(t['host_id'], t['host_name'])}: set a new date & time in /hpanel.")
        return
    winners = set(g.teams[winner]) if winner else set()
    pool = [p for p in g.players.values() if p.uid in winners] or list(g.players.values())
    potm = max(pool, key=lambda p: p.runs + 5 * p.wickets)  # same rule as the match scorecard
    card = {"a": _card_of(g, "A"), "b": _card_of(g, "B"), "potm": potm.uid, "potm_name": potm.name}
    m["res"], m["card"], m["status"] = res, card, "done"
    _save()
    text = head_txt
    if stage == "group":
        text += f"📊 <b>GROUP {m['group']}</b>\n" + table_text(t, "group", only=m["group"])
    elif stage == "super":
        text += table_text(t, "super")
    else:
        text += f"🏅 Player of the Match: {esc(potm.name)}"
    if stage == "group" and _group_stage_over(t):
        text += (f"\n{B.LINE}\n🏆 <b>Group stage complete!</b>\n" + table_all(t) +
                 f"\n👑 {B.link(t['host_id'], t['host_name'])}: send <b>/endgroupstage</b> to choose who qualifies.")
    elif stage == "super" and all(x["status"] == "done" for x in t["matches"] if x.get("stage") == "super"):
        text += (f"\n{B.LINE}\n⚡ <b>Super {len(t['super']['teams'])} complete!</b>\n" +
                 f"👑 {B.link(t['host_id'], t['host_name'])}: send <b>/startplayoffs</b> "
                 "(the top 4 go through).")
    await _banner(ctx, t["home"], "tour_result", f"🏁 <b>{fancy('MATCH ' + str(no) + ' RESULT')}</b>")
    await B.say_long(ctx, t["home"], text)
    if stage == "playoff":
        await _progress_playoffs(ctx, t, m)


# ------------------------------------------------------------------ /teamlogo
async def teamlogo_cmd(update, ctx):
    """/teamlogo [Team Name or Code] - reply to a photo/GIF, or send the picture with this as its caption."""
    msg, user = update.message, update.effective_user
    words = (msg.text or msg.caption or "").split()[1:]
    usage = ("🖼 Reply to the logo with <code>/teamlogo [Team Name or Code]</code>\n"
             "or send the picture with that as its caption.")
    t, tm, _, err = _locate(update, words)
    if err:
        await _reply(msg, err + "\n\n" + usage)
        return
    if not (_can_edit(t, user.id) or _is_lead(tm, user.id)):
        await msg.reply_text("Only the host, a sudo user, the owner or this team's captain / "
                             "vice-captain can set the logo.")
        return
    val = B.media_of(msg) or B.media_of(msg.reply_to_message)
    if not val:
        await _reply(msg, usage)
        return
    tm["logo"] = val
    _save()
    await _reply(msg, f"🖼 Logo saved for <b>{esc(tm['name'])}</b> <code>{esc(tm['code'])}</code>. "
                      "It appears when its matches start.")
