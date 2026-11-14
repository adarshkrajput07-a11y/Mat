"""Matrix Cricket Bot - Telegram Mini App (live matches, tournament, squads, player profiles).

Serves one HTTPS page (index.html) plus a small JSON API from the same process as the bot.
Pure standard library: no extra packages.

Set in your .env:
    MINIAPP_URL=https://your-public-https-address      # required - where Telegram opens the app
    PORT=8080                                          # the port your host gives you (Render/Railway set it)
    MINIAPP_SHORTNAME=app                              # optional - the short name you gave in BotFather /newapp
"""
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("cricket.miniapp")
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = {"token": "", "url": "", "port": 8080, "short": "", "dev": False, "T": None, "B": None}
_LOGOS = {}  # (tour, team) -> (bytes, content type)


# ------------------------------------------------------------------ Telegram login check
def verify_init_data(init_data, token, max_age=86400):
    """Checks the signed data Telegram gives the page. -> the user dict, or None if it is fake/old."""
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not got or not hmac.compare_digest(got, want):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None


# ------------------------------------------------------------------ data for the page
def _row(t, r, pos, qn, stage):
    tm = t["teams"][r["tid"]]
    return {"pos": pos, "tid": r["tid"], "name": tm["name"], "code": tm["code"], "p": r["p"], "w": r["w"],
            "l": r["l"], "t": r["t"], "pts": r["pts"], "nrr": round(r["nrr"], 3),
            "form": CFG["T"]._form(t, r["tid"], stage), "q": pos <= qn}


def _tables(t):
    T, out = CFG["T"], []
    for gname, tids in t.get("groups", {}).items():
        qn = T._qual_count(t, len(tids))
        out.append({"name": f"Group {gname}", "key": gname, "stage": "group",
                    "rows": [_row(t, r, i, qn, "group") for i, r in enumerate(T.ranked(t, tids, "group"), 1)]})
    sup = t.get("super")
    if sup and sup.get("teams"):
        tids = sup["teams"]
        out.append({"name": f"Super {len(tids)}", "key": "SUPER", "stage": "super",
                    "rows": [_row(t, r, i, 4, "super") for i, r in enumerate(T.ranked(t, tids, "super"), 1)]})
    return out


def _caps(t):
    T = CFG["T"]
    tot = list(T.player_totals(t).values())
    orange = sorted((a for a in tot if a["runs"] > 0), key=lambda a: (-a["runs"], a["balls"]))[:10]
    purple = sorted((a for a in tot if a["wkts"] > 0), key=lambda a: (-a["wkts"], a["rc"], a["bb"]))[:10]
    code = lambda a: t["teams"][a["tid"]]["code"]
    return {
        "orange": [{"name": a["name"], "code": code(a), "runs": a["runs"], "inn": a["inn"], "hs": a["hs"],
                    "sr": round(a["runs"] * 100 / a["balls"], 1) if a["balls"] else 0} for a in orange],
        "purple": [{"name": a["name"], "code": code(a), "wkts": a["wkts"], "ov": f"{a['bb'] // 6}.{a['bb'] % 6}",
                    "eco": round(a["rc"] * 6 / a["bb"], 2) if a["bb"] else 0, "best": f"{a['bw']}/{a['br']}"}
                   for a in purple]}


def tour_payload(t, uid=None):
    T = CFG["T"]
    teams, mine = [], None
    tot = {(a["tid"], a["name"]): a for a in T.player_totals(t).values()}
    for tid, tm in t["teams"].items():
        if uid and any(p["uid"] == uid for p in tm["players"]):
            mine = tid
        group = next((g for g, ids in t.get("groups", {}).items() if tid in ids), "")
        teams.append({"id": tid, "name": tm["name"], "code": tm["code"], "group": group,
                      "out": bool(tm.get("out")), "logo": bool(tm.get("logo", "").startswith("photo:")),
                      "players": [{"uid": p["uid"], "name": p["name"], "cap": p["uid"] == tm.get("cap"),
                                   "vc": p["uid"] == tm.get("vc"),
                                   "r": tot.get((tid, p["name"]), {}).get("runs", 0),
                                   "w": tot.get((tid, p["name"]), {}).get("wkts", 0)} for p in tm["players"]]})
    matches = []
    for m in t["matches"]:
        res = m.get("res") if m["status"] == "done" else None
        a, b = t["teams"][m["a"]], t["teams"][m["b"]]
        matches.append({"no": m["no"], "stage": m.get("stage", "group"), "title": T.mtitle(m).title(),
                        "a": {"id": m["a"], "name": a["name"], "code": a["code"]},
                        "b": {"id": m["b"], "name": b["name"], "code": b["code"]},
                        "status": m["status"], "ts": m.get("ts"),
                        "when": T.fmt_ts(m["ts"], t["rules"]["tz"]) if m.get("ts") else "",
                        "venue": T._vtitle(t, m["venue"]) if m.get("venue") else "",
                        "result": res["text"] if res else "", "winner": res["winner"] if res else None})
    r = t["rules"]
    return {"id": t["id"], "name": t["name"], "phase": t.get("phase", "group"), "tz": r["tz"], "now": time.time(),
            "rules": {"overs": r["overs"], "playing": r["playing"], "impact": bool(r.get("impact")),
                      "rebat": bool(r.get("rebat"))},
            "tables": _tables(t), "teams": teams, "matches": matches, "caps": _caps(t), "mine": mine}


def _safe(fn, *a):
    for _ in range(3):  # the bot may be editing the data at that instant
        try:
            return fn(*a)
        except RuntimeError:
            time.sleep(0.05)
    return fn(*a)


def tour_list():
    T = CFG["T"]
    return [{"id": t["id"], "name": t["name"], "phase": t.get("phase", "group"), "teams": len(t["teams"]),
             "done": sum(1 for m in t["matches"] if m["status"] == "done"), "total": len(t["matches"])}
            for t in T.DB["items"].values()]


# ------------------------------------------------------------------ team logos (kept server-side: the token never leaves)
def _logo(tour_id, team_id):
    key = (tour_id, team_id)
    if key in _LOGOS:
        return _LOGOS[key]
    t = CFG["T"].DB["items"].get(str(tour_id))
    tm = t and t["teams"].get(team_id)
    val = tm.get("logo", "") if tm else ""
    if not val.startswith("photo:"):
        return None
    fid = val.split(":", 1)[1]
    base = f"https://api.telegram.org/bot{CFG['token']}"
    with urllib.request.urlopen(f"{base}/getFile?file_id={urllib.parse.quote(fid)}", timeout=10) as r:
        path = json.load(r)["result"]["file_path"]
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{CFG['token']}/{path}", timeout=15) as r:
        data = r.read()
    if len(_LOGOS) > 300:
        _LOGOS.clear()
    _LOGOS[key] = (data, mimetypes.guess_type(path)[0] or "image/jpeg")
    return _LOGOS[key]


def app_link(username, param=""):
    """Link that opens the mini app directly (never just the bot's DM).
    With MINIAPP_SHORTNAME (BotFather /newapp) it is https://t.me/<bot>/<short>?startapp=...
    Without it, it is the bot's MAIN mini app link https://t.me/<bot>?startapp=...
    (set it in BotFather: /mybots -> your bot -> Bot Settings -> Configure Mini App)."""
    if CFG["short"]:
        return f"https://t.me/{username}/{CFG['short']}" + (f"?startapp={param}" if param else "")
    return f"https://t.me/{username}?startapp={param or 'home'}"


# ------------------------------------------------------------------ live games, player profiles, leaderboards
def live_markup(username, chat_id):
    """Inline button for the live scoreboard. Telegram only allows web-app buttons in private chats,
    so groups get a direct link to the mini app (needs MINIAPP_SHORTNAME from BotFather /newapp)."""
    if not (CFG["url"] and CFG["short"] and username):
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    link = app_link(username, f"live{abs(chat_id)}")
    return InlineKeyboardMarkup([[InlineKeyboardButton("📲 See live scoring", url=link)]])


def live_payload():
    """Every match being played in the bot right now (team, solo, 1v1, tournament)."""
    B, out = CFG["B"], []
    for cid, g in list(B.tgames.items()):
        try:
            play = g.state in B.PLAY_STATES
            if not play and not g.players:
                continue
            inn = {}
            for t in "AB":
                r = B._team_innings(g, t) if play else None
                inn[t] = {"s": r[0], "w": r[1], "b": r[2]} if r else None
            st, bw = g.players.get(g.striker), g.players.get(g.bowler)
            teams = {t: [{"n": g.players[u].name, "r": g.players[u].runs, "b": g.players[u].balls,
                          "bat": bool(g.players[u].batted), "out": u in g.out, "st": u == g.striker,
                          "ns": u == g.non_striker, "bw": u == g.bowler, "w": g.players[u].wickets,
                          "rc": g.players[u].runs_conceded, "bb": g.players[u].balls_bowled}
                         for u in g.teams[t] if u in g.players] for t in "AB"}
            out.append({"id": cid, "kind": "team", "teams": teams, "log": list(reversed(getattr(g, "log", [])))[:80], "status": "live" if play else "setup", "title": g.chat_title or "Team match",
                        "overs": g.overs, "innings": g.innings, "bat": g.bat, "inn": inn, "target": g.target,
                        "n": {t: len(g.teams[t]) for t in "AB"},
                        "striker": {"n": st.name, "r": st.runs, "b": st.balls} if st and play else None,
                        "bowler": {"n": bw.name, "w": bw.wickets, "r": bw.runs_conceded} if bw and play else None,
                        "tour": list(g.tour) if g.tour else None})
        except Exception:
            log.exception("live team payload")
    for cid, g in list(B.games.items()):
        try:
            if g.state not in B.SOLO_LIVE:
                continue
            ps = sorted(g.players.values(), key=lambda p: -p.runs)
            bat = g.players.get(g.order[g.bat_idx]) if g.bat_idx < len(g.order) else None
            out.append({"id": cid, "kind": "solo", "status": "live", "duel": bool(g.duel), "title": g.chat_title or "Solo match",
                        "bat": bat.name if bat else "",
                        "rows": [{"n": p.name, "r": p.runs, "b": p.balls, "w": p.wickets} for p in ps],
                        "log": list(reversed(getattr(g, "log", [])))[:80],
                        "count": len(ps), "runs": sum(p.runs for p in ps)})
        except Exception:
            log.exception("live solo payload")
    return out


def _profile(r, rows):
    if not r or not r.get("matches"):
        return None
    B = CFG["B"]
    lvl, cur = r["xp"] // 1000 + 1, r["xp"] % 1000
    tier = next(t for lim, t in B.TIERS if lvl < lim)
    bat_s, bowl_s = r["runs"], r["wkts"] * 8
    if min(bat_s, bowl_s) and max(bat_s, bowl_s) / min(bat_s, bowl_s) < 2:
        role = "All Rounder"
    else:
        role = "Batter" if bat_s >= bowl_s else "Bowler"
    rk = lambda k: 1 + sum(1 for x in rows if x.get(k, 0) > r.get(k, 0)) if r.get(k) else None
    return {"name": r.get("name") or "Player", "lvl": lvl, "xp": cur, "tier": tier, "role": role, "gems": r["gems"],
            "bat": {"runs": r["runs"], "inn": r["innings"], "avg": round(r["runs"] / r["innings"], 1) if r["innings"] else 0,
                    "sr": round(r["runs"] / r["balls"] * 100, 1) if r["balls"] else 0, "hs": r["hs"],
                    "f4": r["fours"], "f6": r["sixes"], "f50": r["fifties"], "f100": r["hundreds"], "duck": r["ducks"]},
            "bowl": {"wkts": r["wkts"], "eco": round(r["conceded"] * 6 / r["bowl_balls"], 1) if r["bowl_balls"] else 0,
                     "best": f"{r['best_w']}/{r['best_r']}", "ht": r["hattricks"]},
            "car": {"m": r["matches"], "w": r["wins"], "wr": round(r["wins"] / r["matches"] * 100),
                    "orange": r["orange"], "purple": r["purple"], "potm": r["potm"]},
            "form": list(r.get("form") or ""),
            "rank": {"runs": rk("runs"), "wkts": rk("wkts"), "of": len(rows)}}


def me_payload(user):
    B = CFG["B"]
    uid = user.get("id")
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "Player"
    try:
        rows = B.STORE.all_stats()
        r = B.STORE.get_stats(uid) if uid else None
    except Exception:
        log.exception("stats read")
        rows, r = [], None
    return {"uid": uid, "name": name, "first": user.get("first_name") or name, "stats": _profile(r, rows)}


def player_payload(uid):
    B = CFG["B"]
    rows = B.STORE.all_stats()
    p = _profile(B.STORE.get_stats(uid), rows)
    return {"uid": uid, "stats": p}


def leaders_payload():
    rows = CFG["B"].STORE.all_stats()

    def top(k):
        return [{"uid": r.get("uid"), "name": r.get("name") or "Player", "v": r[k]}
                for r in sorted((x for x in rows if x.get(k)), key=lambda x: -x[k])[:10]]
    return {"players": len(rows), "runs": top("runs"), "wkts": top("wkts"), "sixes": top("sixes"), "hs": top("hs")}


def _avatar(uid):
    key = ("u", uid)
    if key in _LOGOS:
        return _LOGOS[key]
    base = f"https://api.telegram.org/bot{CFG['token']}"
    with urllib.request.urlopen(f"{base}/getUserProfilePhotos?user_id={uid}&limit=1", timeout=10) as r:
        ph = json.load(r)["result"]["photos"]
    if not ph:
        _LOGOS[key] = None
        return None
    fid = ph[0][0]["file_id"]  # the smallest size is plenty for an avatar
    with urllib.request.urlopen(f"{base}/getFile?file_id={urllib.parse.quote(fid)}", timeout=10) as r:
        path = json.load(r)["result"]["file_path"]
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{CFG['token']}/{path}", timeout=15) as r:
        data = r.read()
    _LOGOS[key] = (data, "image/jpeg")
    return _LOGOS[key]


# ------------------------------------------------------------------ HTTP server
class Handler(BaseHTTPRequestHandler):
    server_version = "MatrixMini/1.0"

    def log_message(self, *a):  # keep the bot log quiet
        pass

    def _send(self, code, body, ctype="application/json", cache="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self._route()
        except Exception:
            log.exception("mini app request failed")
            try:
                self._send(500, {"error": "server error"})
            except Exception:
                pass

    def _user(self):
        if CFG["dev"]:
            return {"id": 0}
        return verify_init_data(self.headers.get("X-Init-Data", ""), CFG["token"])

    def _route(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8", "no-cache")
        elif path == "/healthz":
            self._send(200, {"ok": True})
        elif path.startswith("/logo/"):
            try:
                _, _, tid, team = path.split("/", 3)
                got = _logo(int(tid), team)
            except Exception:
                got = None
            if got:
                self._send(200, got[0], got[1], "public, max-age=3600")
            else:
                self._send(404, {"error": "no logo"})
        elif path == "/bg":  # your own cricket background: put bg.jpg (or .png/.webp) next to miniapp.py
            for n in ("bg.jpg", "bg.jpeg", "bg.png", "bg.webp"):
                fp = os.path.join(HERE, n)
                if os.path.isfile(fp):
                    with open(fp, "rb") as f:
                        self._send(200, f.read(), mimetypes.guess_type(fp)[0] or "image/jpeg", "public, max-age=600")
                    break
            else:
                self._send(404, {"error": "no background"})
        elif path.startswith("/avatar/"):
            try:
                got = _avatar(int(path.rsplit("/", 1)[-1]))
            except Exception:
                got = None
            if got:
                self._send(200, got[0], got[1], "public, max-age=3600")
            else:
                self._send(404, {"error": "no avatar"})
        elif path.startswith("/api/"):
            user = self._user()
            if user is None:
                self._send(401, {"error": "Open this from Telegram."})
            elif path == "/api/me":
                self._send(200, _safe(me_payload, user))
            elif path == "/api/live":
                self._send(200, _safe(live_payload))
            elif path == "/api/leaders":
                self._send(200, _safe(leaders_payload))
            elif path.startswith("/api/player/"):
                try:
                    self._send(200, _safe(player_payload, int(path.rsplit("/", 1)[-1])))
                except ValueError:
                    self._send(404, {"error": "not found"})
            elif path == "/api/tours":
                self._send(200, _safe(tour_list))
            elif path.startswith("/api/tour/"):
                t = CFG["T"].DB["items"].get(path.rsplit("/", 1)[-1])
                if not t:
                    self._send(404, {"error": "Tournament not found."})
                else:
                    self._send(200, _safe(tour_payload, t, user.get("id")))
            else:
                self._send(404, {"error": "not found"})
        else:
            self._send(404, {"error": "not found"})


def _serve():
    srv = ThreadingHTTPServer(("0.0.0.0", CFG["port"]), Handler)
    log.info("Mini app server on port %s", CFG["port"])
    srv.serve_forever()


# ------------------------------------------------------------------ bot side
def register(app, bot_module):
    """Call once from main(): adds /app, the menu button and starts the web server."""
    import tournament_mode
    from telegram.ext import CommandHandler
    CFG.update(token=bot_module.TOKEN, T=tournament_mode, B=bot_module,
               url=os.getenv("MINIAPP_URL", "").strip().rstrip("/"),
               port=int(os.getenv("PORT") or os.getenv("MINIAPP_PORT") or 8080),
               short=os.getenv("MINIAPP_SHORTNAME", "").strip(),
               dev=os.getenv("MINIAPP_DEV") == "1")
    if not CFG["url"]:
        log.warning("MINIAPP_URL is not set - the mini app is disabled.")
        return
    if not CFG["url"].startswith("https://"):
        log.warning("MINIAPP_URL must start with https:// - Telegram refuses anything else.")
        return
    threading.Thread(target=_serve, daemon=True, name="miniapp").start()
    app.add_handler(CommandHandler("app", app_cmd))
    app.add_handler(CommandHandler("start", _start_app), group=-1)  # runs before the normal /start
    app.job_queue.run_once(_set_menu_button, 3)


async def _set_menu_button(ctx):
    from telegram import MenuButtonWebApp, WebAppInfo
    try:
        await ctx.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("🏏 Matrix Cricket", WebAppInfo(url=CFG["url"])))
    except Exception as e:
        log.warning("Could not set the menu button: %s", e)


async def _start_app(update, ctx):
    """t.me/<bot>?start=app (from the group /app button): reply with the button that opens the app."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    from telegram.ext import ApplicationHandlerStop
    if update.effective_chat.type != "private" or ctx.args != ["app"]:
        return  # normal /start handling carries on
    btn = InlineKeyboardButton("🏏 Open Tournament App", web_app=WebAppInfo(url=CFG["url"]))
    await update.message.reply_text("📱 Live matches, tournament, squads and your stats — tap below.",
                                    reply_markup=InlineKeyboardMarkup([[btn]]))
    raise ApplicationHandlerStop


async def app_cmd(update, ctx):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    chat, T = update.effective_chat, CFG["T"]
    if chat.type == "private":
        btn = InlineKeyboardButton("🏏 Open Tournament App", web_app=WebAppInfo(url=CFG["url"]))
        await update.message.reply_text("📱 Live matches, tournament, squads and your stats — tap below.",
                                        reply_markup=InlineKeyboardMarkup([[btn]]))
        return
    me = await ctx.bot.get_me()  # Telegram only allows web-app buttons in DMs; groups get a link
    tid = next((t["id"] for t in T.DB["items"].values() if t.get("home") == chat.id), "")
    has_main = getattr(me, "has_main_web_app", None)  # BotFather "Configure Mini App" is on
    if has_main is None:
        has_main = (getattr(me, "api_kwargs", None) or {}).get("has_main_web_app")
    if CFG["short"] or has_main:
        link = app_link(me.username, f"tour{tid}" if tid else "")  # opens the mini app itself
    else:  # no BotFather mini app set up: DM the bot, it answers with the open button at once
        link = f"https://t.me/{me.username}?start=app"
    await update.message.reply_text("📱 Open the Tournament App:", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏏 Open Tournament App", url=link)]]))
