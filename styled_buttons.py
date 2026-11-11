"""Coloured inline buttons (Bot API 9.4+) with optional premium-emoji icons.

Btn(...) works exactly like telegram.InlineKeyboardButton, but adds a colour
automatically:  blue = primary, green = success, red = danger.
Needs python-telegram-bot >= 22.7. On an older version it silently falls back
to plain grey buttons, so the bot never breaks.

Premium emoji icons are OFF by default. They only work for bots that bought
extra usernames on Fragment. To turn them on:
  1. put ids in button_icons.json, e.g. {"solo": "5368324170671202286"}
     (the key is any word from the button label, case-insensitive)
  2. set the environment variable BUTTON_ICONS=1
"""
import inspect
import json
import os
import re

from telegram import InlineKeyboardButton

try:
    _STYLE_OK = "style" in inspect.signature(InlineKeyboardButton.__init__).parameters
except Exception:
    _STYLE_OK = False

_DANGER_TXT = re.compile(r"❌|\b(cancel|reject|decline|leave|remove|delete|stop|close|exit)\b", re.I)
_DANGER_DATA = re.compile(r"(^|:)(cancel|no|leave|mc|rm|del|remove|reject|decline)(:|$)", re.I)
_SUCCESS_TXT = re.compile(
    r"✅|\b(accept|confirm|send|vote|yes|start|register|registration|join|auction|tournament|"
    r"again|deliver|save|add|bid|sold|open|play|done|ok)\b", re.I)

_ICONS = {}
if os.environ.get("BUTTON_ICONS") == "1":
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "button_icons.json")) as f:
            _ICONS = {k.lower(): str(v) for k, v in json.load(f).items()}
    except Exception:
        _ICONS = {}


def auto_style(text: str, data: str = "") -> str:
    data = data or ""
    if _DANGER_TXT.search(text) or _DANGER_DATA.search(data):
        return "danger"
    if _SUCCESS_TXT.search(text):
        return "success"
    return "primary"


def _icon_for(text: str):
    low = text.lower()
    for key, cid in _ICONS.items():
        if key in low:
            return cid
    return None


def Btn(text, *args, style=None, **kw):
    if _STYLE_OK:
        icon = _icon_for(text) if _ICONS else None
        if icon:
            kw["icon_custom_emoji_id"] = icon
            text = re.sub(r"^[^\w]+", "", text).strip() or text
        kw["style"] = style or auto_style(text, kw.get("callback_data") or "")
    return InlineKeyboardButton(text, *args, **kw)
