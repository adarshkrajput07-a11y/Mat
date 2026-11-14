"""Matrix Cricket Bot - Telegram Mini App (read-only tournament viewer).

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
CFG = {"token": "", "url": "", "port": 8080, "short": "", "dev": False, "T": None}
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
    for tid, tm in t["teams"].items():
        if uid and any(p["uid"] == uid for p in tm["players"]):
            mine = tid
        group = next((g for g, ids in t.get("groups", {}).items() if tid in ids), "")
        teams.append({"id": tid, "name": tm["name"], "code": tm["code"], "group": group,
                      "out": bool(tm.get("out")), "logo": bool(tm.get("logo", "").startswith("photo:")),
                      "players": [{"name": p["name"], "cap": p["uid"] == tm.get("cap"), "vc": p["uid"] == tm.get("vc")}
                                  for p in tm["players"]]})
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
        elif path.startswith("/api/"):
            user = self._user()
            if user is None:
                self._send(401, {"error": "Open this from Telegram."})
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
    CFG.update(token=bot_module.TOKEN, T=tournament_mode,
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
    app.job_queue.run_once(_set_menu_button, 3)


async def _set_menu_button(ctx):
    from telegram import MenuButtonWebApp, WebAppInfo
    try:
        await ctx.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("🏏 Tournament", WebAppInfo(url=CFG["url"])))
    except Exception as e:
        log.warning("Could not set the menu button: %s", e)


async def app_cmd(update, ctx):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    chat, T = update.effective_chat, CFG["T"]
    if chat.type == "private":
        btn = InlineKeyboardButton("🏏 Open Tournament App", web_app=WebAppInfo(url=CFG["url"]))
        await update.message.reply_text("📱 Schedule, points table, squads and caps — tap below.",
                                        reply_markup=InlineKeyboardMarkup([[btn]]))
        return
    me = await ctx.bot.get_me()  # Telegram only allows web-app buttons in DMs; groups get a link
    tid = next((t["id"] for t in T.DB["items"].values() if t.get("home") == chat.id), "")
    if CFG["short"]:
        link = f"https://t.me/{me.username}/{CFG['short']}" + (f"?startapp=tour{tid}" if tid else "")
    else:
        link = f"https://t.me/{me.username}"
    await update.message.reply_text("📱 Open the Tournament App:", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏏 Open Tournament App", url=link)]]))
