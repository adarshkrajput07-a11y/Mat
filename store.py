"""Storage for the Matrix Cricket Bot.

Set MONGO_URI in .env and EVERYTHING (player stats, users, groups, @usernames, /addfile media,
Play Zone / channel links, auction sudo list, tournament settings, saved squads) lives in MongoDB,
so it survives restarts, redeploys and server moves.

Leave MONGO_URI empty and the bot keeps working exactly as before (stats.db + .json files in
DATA_DIR), so you can still test locally without a database.

bot.py and auction_mode.py only talk to the small API below, never to a database directly:
    get_stats_many(uids) / get_stats(uid) / update_matches(updates)
    add_user / add_chat / add_uname / user_ids / chat_ids / unames / remove_chat
    load_json(name, default) / save_json(name, data)
"""
import json
import os
import sqlite3

# --- tiny .env loader (this module can be imported before bot.py has read .env) ---


def _load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

MONGO_URI = (os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI") or "").strip()
MONGO_DB = (os.environ.get("MONGO_DB") or "matrix_cricket_bot").strip()
USE_MONGO = bool(MONGO_URI)

# numeric stat columns (everything except uid / name / form)
STAT_FIELDS = ("runs", "balls", "innings", "hs", "fours", "sixes", "fifties", "hundreds", "ducks",
               "wkts", "bowl_balls", "conceded", "best_w", "best_r", "hattricks", "matches", "wins",
               "orange", "purple", "potm", "xp", "gems")
# counters that are ADDED to after every match (hs / best_w / best_r are set separately)
INC_FIELDS = ("runs", "balls", "innings", "fours", "sixes", "fifties", "hundreds", "ducks", "wkts",
              "bowl_balls", "conceded", "hattricks", "matches", "wins", "orange", "purple", "potm",
              "xp", "gems")
# json "documents" the bot keeps; each is one file (name + ".json") in sqlite mode, one doc in Mongo
KV_NAMES = ("media", "playzone", "auction_media_config", "auction_sudo", "auction_tournament",
            "auction_squads")
EMPTY_JSON = ({}, [], None)


def _blank(uid, name=""):
    row = {f: 0 for f in STAT_FIELDS}
    row.update(uid=uid, name=name, form="")
    return row


class SqliteStore:
    """The original storage: stats.db + json files inside DATA_DIR."""
    kind = "sqlite"

    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "stats.db"))
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS stats (
            uid INTEGER PRIMARY KEY, name TEXT,
            runs INT DEFAULT 0, balls INT DEFAULT 0, innings INT DEFAULT 0, hs INT DEFAULT 0,
            fours INT DEFAULT 0, sixes INT DEFAULT 0, fifties INT DEFAULT 0,
            hundreds INT DEFAULT 0, ducks INT DEFAULT 0,
            wkts INT DEFAULT 0, bowl_balls INT DEFAULT 0, conceded INT DEFAULT 0,
            best_w INT DEFAULT 0, best_r INT DEFAULT 0, hattricks INT DEFAULT 0,
            matches INT DEFAULT 0, wins INT DEFAULT 0, orange INT DEFAULT 0,
            purple INT DEFAULT 0, potm INT DEFAULT 0, xp INT DEFAULT 0,
            gems INT DEFAULT 0, form TEXT DEFAULT '')""")
        self.db.execute("CREATE TABLE IF NOT EXISTS users (uid INTEGER PRIMARY KEY, name TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, title TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS unames (uname TEXT PRIMARY KEY, uid INTEGER, name TEXT)")
        self.db.commit()

    def commit(self):
        self.db.commit()

    # ---- stats ----
    def get_stats(self, uid):
        r = self.db.execute("SELECT * FROM stats WHERE uid=?", (uid,)).fetchone()
        return dict(r) if r else None

    def get_stats_many(self, uids):
        return {u: s for u in uids if (s := self.get_stats(u))}

    def all_stats(self):
        """Every player who has played at least one match (for /botstats)."""
        return [dict(r) for r in self.db.execute("SELECT * FROM stats WHERE matches > 0")]

    def update_matches(self, updates):
        """updates: [{uid, name, inc: {field: n}, hs, best_w, best_r, form}]"""
        for u in updates:
            self.db.execute("INSERT OR IGNORE INTO stats (uid, name) VALUES (?, ?)", (u["uid"], u["name"]))
            sets = ", ".join(f"{k}={k}+?" for k in u["inc"])
            self.db.execute(
                f"UPDATE stats SET name=?, {sets}, hs=MAX(hs, ?), best_w=?, best_r=?, form=? WHERE uid=?",
                (u["name"], *u["inc"].values(), u["hs"], u["best_w"], u["best_r"], u["form"], u["uid"]))
        self.db.commit()

    # ---- users / groups / usernames (for /broadcast and /add @username) ----
    def add_user(self, uid, name):
        self.db.execute("INSERT OR REPLACE INTO users (uid, name) VALUES (?, ?)", (uid, name))
        self.db.commit()

    def add_chat(self, chat_id, title):
        self.db.execute("INSERT OR REPLACE INTO chats (chat_id, title) VALUES (?, ?)", (chat_id, title))
        self.db.commit()

    def add_uname(self, uname, uid, name):
        self.db.execute("INSERT OR REPLACE INTO unames VALUES (?, ?, ?)", (uname, uid, name))
        self.db.commit()

    def user_ids(self):
        return [r["uid"] for r in self.db.execute("SELECT uid FROM users")]

    def chat_ids(self):
        return [r["chat_id"] for r in self.db.execute("SELECT chat_id FROM chats")]

    def unames(self):
        return {r["uname"]: (r["uid"], r["name"]) for r in self.db.execute("SELECT * FROM unames")}

    def remove_chat(self, cid):
        self.db.execute("DELETE FROM users WHERE uid=?", (cid,))
        self.db.execute("DELETE FROM chats WHERE chat_id=?", (cid,))
        self.db.commit()

    # ---- json documents ----
    def _path(self, name):
        return os.path.join(self.data_dir, name + ".json")

    def load_json(self, name, default=None):
        try:
            with open(self._path(name), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {} if default is None else default

    def save_json(self, name, data):
        with open(self._path(name), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)

    # ---- the pinned-Telegram-zip backup (persist.py) only exists in this mode ----
    def migrated(self):
        return True


class MongoStore:
    """Everything in MongoDB (one database, a few small collections)."""
    kind = "mongo"
    db = None  # (no sqlite handle in this mode)

    def __init__(self, uri, db_name):
        try:
            from pymongo import MongoClient
        except ImportError:
            raise SystemExit("MONGO_URI is set but pymongo is missing. Run: pip install \"pymongo[srv]\"")
        try:
            self.client = MongoClient(uri, serverSelectionTimeoutMS=10000, socketTimeoutMS=20000,
                                      connectTimeoutMS=10000, retryWrites=True)
            self.client.admin.command("ping")  # fail fast instead of quietly losing data
        except Exception as e:
            raise SystemExit(f"Could not connect to MongoDB: {e}\n"
                             "Check MONGO_URI, the database user's password, and that your host's "
                             "IP is allowed in Atlas -> Network Access (0.0.0.0/0 for any host).")
        self.m = self.client[db_name]
        self.stats = self.m["stats"]
        self.users = self.m["users"]
        self.chats = self.m["chats"]
        self.unames_c = self.m["unames"]
        self.kv = self.m["kv"]
        self.meta = self.m["meta"]
        print(f"[store] Connected to MongoDB database '{db_name}'.")

    def commit(self):
        pass

    # ---- stats ----
    @staticmethod
    def _row(doc):
        row = _blank(doc["_id"], doc.get("name", ""))
        for f in STAT_FIELDS:
            row[f] = int(doc.get(f, 0) or 0)
        row["form"] = doc.get("form", "") or ""
        return row

    def get_stats(self, uid):
        d = self.stats.find_one({"_id": uid})
        return self._row(d) if d else None

    def get_stats_many(self, uids):
        uids = list(uids)
        if not uids:
            return {}
        return {d["_id"]: self._row(d) for d in self.stats.find({"_id": {"$in": uids}})}

    def all_stats(self):
        """Every player who has played at least one match (for /botstats)."""
        return [self._row(d) for d in self.stats.find({"matches": {"$gt": 0}})]

    def update_matches(self, updates):
        from pymongo import UpdateOne
        ops = [UpdateOne({"_id": u["uid"]},
                         {"$set": {"name": u["name"], "best_w": u["best_w"], "best_r": u["best_r"],
                                   "form": u["form"]},
                          "$inc": dict(u["inc"]), "$max": {"hs": u["hs"]}}, upsert=True)
               for u in updates]
        if ops:
            self.stats.bulk_write(ops, ordered=False)  # one round trip for the whole match

    # ---- users / groups / usernames ----
    def add_user(self, uid, name):
        self.users.replace_one({"_id": uid}, {"name": name}, upsert=True)

    def add_chat(self, chat_id, title):
        self.chats.replace_one({"_id": chat_id}, {"title": title}, upsert=True)

    def add_uname(self, uname, uid, name):
        self.unames_c.replace_one({"_id": uname}, {"uid": uid, "name": name}, upsert=True)

    def user_ids(self):
        return [d["_id"] for d in self.users.find({}, {"_id": 1})]

    def chat_ids(self):
        return [d["_id"] for d in self.chats.find({}, {"_id": 1})]

    def unames(self):
        return {d["_id"]: (d["uid"], d.get("name", "")) for d in self.unames_c.find()}

    def remove_chat(self, cid):
        self.users.delete_one({"_id": cid})
        self.chats.delete_one({"_id": cid})

    # ---- json documents (stored as a JSON string so any key is safe in Mongo) ----
    def load_json(self, name, default=None):
        try:
            d = self.kv.find_one({"_id": name})
            if d:
                return json.loads(d["data"])
        except Exception as e:
            print(f"[store] could not load '{name}': {e}")
        return {} if default is None else default

    def save_json(self, name, data):
        self.kv.replace_one({"_id": name}, {"data": json.dumps(data)}, upsert=True)

    # ---- migration from the old stats.db / json files ----
    def migrated(self):
        return bool(self.meta.find_one({"_id": "migrated"}))

    def _put_many(self, coll, items, overwrite):
        """items: [(_id, doc)]. Without overwrite only MISSING documents are added."""
        from pymongo import ReplaceOne, UpdateOne
        ops = [ReplaceOne({"_id": i}, d, upsert=True) if overwrite
               else UpdateOne({"_id": i}, {"$setOnInsert": d}, upsert=True) for i, d in items]
        for k in range(0, len(ops), 500):
            coll.bulk_write(ops[k:k + 500], ordered=False)
        return len(ops)

    def import_from_dir(self, data_dir, overwrite=False):
        """Copy stats.db and the *.json files found in data_dir into MongoDB.
        Safe to run again: existing documents are kept unless overwrite=True."""
        counts = {}
        path = os.path.join(data_dir, "stats.db")
        if os.path.isfile(path):
            src = sqlite3.connect(path)
            src.row_factory = sqlite3.Row

            def rows(sql):
                try:
                    return src.execute(sql).fetchall()
                except sqlite3.Error:
                    return []
            counts["stats"] = self._put_many(self.stats, [
                (r["uid"], {k: r[k] for k in r.keys() if k != "uid"}) for r in rows("SELECT * FROM stats")], overwrite)
            counts["users"] = self._put_many(self.users, [
                (r["uid"], {"name": r["name"]}) for r in rows("SELECT * FROM users")], overwrite)
            counts["chats"] = self._put_many(self.chats, [
                (r["chat_id"], {"title": r["title"]}) for r in rows("SELECT * FROM chats")], overwrite)
            counts["unames"] = self._put_many(self.unames_c, [
                (r["uname"], {"uid": r["uid"], "name": r["name"]}) for r in rows("SELECT * FROM unames")], overwrite)
            src.close()
        for name in KV_NAMES:
            p = os.path.join(data_dir, name + ".json")
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"[store] skipped {name}.json: {e}")
                continue
            if data in EMPTY_JSON:
                continue
            counts[name] = self._put_many(self.kv, [(name, {"data": json.dumps(data)})], overwrite)
        self.meta.replace_one({"_id": "migrated"}, {"done": True}, upsert=True)
        return counts

    # ---- /backup + /restore (a JSON snapshot of the whole database) ----
    def export_all(self):
        return {
            "format": "matrix-mongo-backup-1",
            "stats": list(self.stats.find()), "users": list(self.users.find()),
            "chats": list(self.chats.find()), "unames": list(self.unames_c.find()),
            "kv": {d["_id"]: d["data"] for d in self.kv.find()},
        }

    def import_all(self, snap):
        if snap.get("format") != "matrix-mongo-backup-1":
            raise ValueError("not a Matrix Mongo backup file")
        counts = {}
        for key, coll in (("stats", self.stats), ("users", self.users),
                          ("chats", self.chats), ("unames", self.unames_c)):
            items = [(d["_id"], {k: v for k, v in d.items() if k != "_id"}) for d in snap.get(key, [])]
            counts[key] = self._put_many(coll, items, True)
        kv = [(n, {"data": s}) for n, s in (snap.get("kv") or {}).items()]
        counts["kv"] = self._put_many(self.kv, kv, True)
        return counts


_STORE = None


def get_store():
    """The one shared store (created on first use)."""
    global _STORE
    if _STORE is None:
        if USE_MONGO:
            _STORE = MongoStore(MONGO_URI, MONGO_DB)
        else:
            _STORE = SqliteStore(os.environ.get("DATA_DIR", "."))
    return _STORE
