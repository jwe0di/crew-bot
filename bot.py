import asyncio
import datetime as dt
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Optional
from urllib.parse import urlparse

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import config

load_dotenv()

BOT_VERSION = "v3.2.1"
DB = os.getenv("DB_PATH", "crewbot.db")
JSON_CONFIG_PATH = os.getenv("JSON_CONFIG_PATH") or os.path.join(os.path.dirname(DB) or ".", "servers_settings.json")
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

# ---- حدود المرفقات ----
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".mkv"}
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_TOTAL_FILES_SIZE = 50 * 1024 * 1024
MAX_FILES_COUNT = 5
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


# --- سيرفر ويب مصغر لـ Railway ---
def run_dummy_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"DragonSlayerBot {BOT_VERSION} is online!".encode())

        def log_message(self, format, *args):
            pass

    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ================================================================ الإعدادات
REQUIRED_TYPES = ("war", "1v1", "2v2", "training", "tryout")

_DEFAULT_LOG_TYPES = {
    "war": {"label": "Crew War Log", "fields": [
        ("my_team", "Who fought for Dragon Slayers", True, True),
        ("crew", "Enemy crew name", False, True),
        ("their_team", "Who fought for the enemy crew", True, True),
        ("score", "Score (e.g. 5-3)", False, True),
        ("notes", "Notes (optional)", True, False)]},
    "1v1": {"label": "1v1 Fight Log", "fields": [
        ("my_fighter", "Who fought for us", False, True),
        ("opponent", "Who we fought against", False, True),
        ("score", "Score (e.g. 5-3)", False, True),
        ("notes", "Notes (optional)", True, False)]},
    "2v2": {"label": "2v2 Fight Log", "fields": [
        ("my_team", "Our team", False, True),
        ("their_team", "Enemy team", False, True),
        ("score", "Score (e.g. 5-3)", False, True),
        ("notes", "Notes (optional)", True, False)]},
    "training": {"label": "Training Log", "fields": [
        ("participants", "Who trained", True, True),
        ("score", "Result / summary (optional)", False, False),
        ("notes", "Notes (optional)", True, False)]},
    "tryout": {"label": "Tryout Log", "fields": [
        ("score", "Tryout score / rating", False, True),
        ("roblox", "Candidate Roblox username (optional)", False, False),
        ("notes", "Notes (optional)", True, False)]},
}
_DEFAULT_POINTS = {"war_win": 10, "war_loss": 5, "1v1_win": 8, "1v1_loss": 4,
                   "2v2_win": 6, "2v2_loss": 3, "training": 3, "tryout": 5}
_DEFAULT_POINT_LABELS = {"war_win": "War - Win", "war_loss": "War - Loss",
                         "1v1_win": "1v1 - Win", "1v1_loss": "1v1 - Loss",
                         "2v2_win": "2v2 - Win", "2v2_loss": "2v2 - Loss",
                         "training": "Training", "tryout": "Tryout"}
_DEFAULT_LADDER = [("Lvl. 20", 20), ("Lvl. 40", 60), ("Lvl. 60", 130), ("Lvl. 80", 220), ("Lvl. 100", 350)]


def _load_log_types():
    cfg = getattr(config, "LOG_TYPES", None)
    if isinstance(cfg, dict) and all(
            t in cfg and isinstance(cfg[t].get("fields"), list) and all(len(f) == 4 for f in cfg[t]["fields"])
            for t in REQUIRED_TYPES):
        return {t: cfg[t] for t in REQUIRED_TYPES}
    return _DEFAULT_LOG_TYPES


LOG_TYPES = _load_log_types()
_cfg_points = getattr(config, "DEFAULT_POINTS", {}) or {}
DEFAULT_POINTS = {k: int(_cfg_points.get(k, v)) for k, v in _DEFAULT_POINTS.items()}
_cfg_labels = getattr(config, "POINT_LABELS", {}) or {}
POINT_LABELS = {k: str(_cfg_labels.get(k, v)) for k, v in _DEFAULT_POINT_LABELS.items()}
LADDER = [(str(n), int(p)) for n, p in (getattr(config, "LADDER", None) or _DEFAULT_LADDER)]
EDITOR_ROLES = list(getattr(config, "EDITOR_ROLES", ["The Admiral", "Empyrean Monarch", "Commander"]))
REVIEWER_ROLES = list(getattr(config, "REVIEWER_ROLES", ["The Admiral", "Empyrean Monarch", "Commander", "Astral Apotheosis"]))
TRYOUTER_ROLES = list(getattr(config, "TRYOUTER_ROLES", ["Tryout Master"]))
CREW_ROLE_KEYWORD = getattr(config, "CREW_ROLE_KEYWORD", "Crew")

RULE_DEFAULTS = {
    "daily_limit": 5,
    "reminder_hours": 48,
    "backup_hours": 24,
    "weekly_post": 1,
    "war_participants": 1,
    "tryout_autorole": 1,
}
RULE_DEFAULTS.update(getattr(config, "RULE_DEFAULTS", {}) or {})
RULE_LABELS = {
    "daily_limit": "Max logs per member per 24h (0 = unlimited)",
    "reminder_hours": "Remind reviewers after N hours (0 = off)",
    "backup_hours": "Auto backup to owner every N hours (0 = off)",
    "weekly_post": "Weekly leaderboard post (1 = on, 0 = off)",
    "war_participants": "War points to confirmed teammates (1 = on, 0 = off)",
    "tryout_autorole": "Give Crew role on passed tryout (1 = on, 0 = off)",
}

RESULT_LABEL = {"win": "✅ Win", "loss": "❌ Loss"}
TRYOUT_LABEL = {"win": "✅ Passed", "loss": "❌ Failed"}
STATUS_LABEL = {"pending": "⏳ Pending", "accepted": "✅ Accepted",
                "rejected": "❌ Rejected", "revoked": "🚫 Revoked"}
RESULT_CHOICES = [app_commands.Choice(name="Win", value="win"),
                  app_commands.Choice(name="Loss", value="loss")]
TRYOUT_CHOICES = [app_commands.Choice(name="Passed", value="win"),
                  app_commands.Choice(name="Failed", value="loss")]
LOG_TYPE_CHOICES = [app_commands.Choice(name="All types (default)", value="all")] + [
    app_commands.Choice(name=v["label"][:100], value=k) for k, v in LOG_TYPES.items()]
KIND_CHANNEL = [
    app_commands.Choice(name="Review channel (قناة المراجعة)", value="review_channel"),
    app_commands.Choice(name="Log channel (قناة اللوج العامة)", value="log_channel"),
    app_commands.Choice(name="Audit channel (سجل التعديلات)", value="audit_channel"),
    app_commands.Choice(name="Update notices channel (قناة التحديثات)", value="update_channel")]
KIND_PING = [
    app_commands.Choice(name="Review ping (منشن في رسالة المراجعة)", value="review_ping"),
    app_commands.Choice(name="Log ping (منشن في اللوج العام)", value="log_ping")]
KIND_ROLES = [
    app_commands.Choice(name="Reviewers (مين يقبل/يرفض)", value="review"),
    app_commands.Choice(name="Submitters (مين يقدر يسجّل لوج)", value="submit")]
ACTIONS = [app_commands.Choice(name="Add (إضافة)", value="add"),
           app_commands.Choice(name="Remove (إزالة)", value="remove")]
PERIOD_CHOICES = [app_commands.Choice(name="All time", value="all"),
                  app_commands.Choice(name="This week", value="week")]
RULE_CHOICES = [app_commands.Choice(name=l[:100], value=k) for k, l in RULE_LABELS.items()]
POINT_CHOICES = [app_commands.Choice(name=l[:100], value=k) for k, l in POINT_LABELS.items()]
RANK_CHOICES = [app_commands.Choice(name=n, value=n) for n, _ in LADDER]
BACKUP_SCOPE_CHOICES = [
    app_commands.Choice(name="Guild Activity & Today (نشاط السيرفر اليوم)", value="today"),
    app_commands.Choice(name="Full Database [Owner Only] (قاعدة البيانات كاملة)", value="full")]
BACKUP_DEST_CHOICES = [
    app_commands.Choice(name="Here in chat (هنا في الشات)", value="chat"),
    app_commands.Choice(name="Bot Owner DM (خاص صاحب البوت)", value="owner")]

UPDATE_PRESETS = {
    "bot_patch": {
        "label": f"🤖 Release notes {BOT_VERSION}",
        "title_en": f"DragonSlayerBot {BOT_VERSION}",
        "title_ar": f"إصدار البوت {BOT_VERSION}",
        "desc_en": "• Custom interactive crews management & language dropdown menu added.",
        "desc_ar": "• إضافة نظام إدارة الطواقم (Crews) التفاعلي وقائمة اختيار اللغات.",
    }
}
UPDATE_TYPE_CHOICES = [app_commands.Choice(name=v["label"][:100], value=k) for k, v in UPDATE_PRESETS.items()]

ALL_BOT_COMMANDS = [
    "log-war", "log-1v1", "log-2v2", "log-training", "log-tryout", "resubmit",
    "log-update", "set-log-template", "points", "leaderboard", "profile",
    "stats", "rates", "link", "pending", "revoke-log", "inactive",
    "season-reset", "season-top", "backup", "setchannel", "setping",
    "setroles", "setrule", "set-log-points", "set-rank-points", "set-points",
    "sync-ranks", "verify-points", "settings", "sync", "set-cmd-role", "set-update-ping",
    "help", "send-help-panel", "restore-backup", "my-logs", "log-info", "reviewer-stats",
    "language", "create-new-crew", "crews-list",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    guild_id INTEGER, key TEXT, value TEXT,
    PRIMARY KEY(guild_id, key)
);
CREATE TABLE IF NOT EXISTS members(
    guild_id INTEGER, user_id INTEGER, points INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS guild_channels(
    guild_id INTEGER PRIMARY KEY, review_channel INTEGER, log_channel INTEGER, audit_channel INTEGER
);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL,
    author_id INTEGER NOT NULL,
    result TEXT, data TEXT, flags TEXT,
    status TEXT DEFAULT 'pending',
    points INTEGER DEFAULT 0,
    reviewer_id INTEGER, reason TEXT,
    created_at TEXT NOT NULL,
    review_channel INTEGER, review_msg INTEGER,
    public_channel INTEGER, public_msg INTEGER,
    reviewed_at TEXT,
    reminded INTEGER DEFAULT 0,
    parent_id INTEGER,
    season_id TEXT DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS hashes(hash TEXT, log_id INTEGER, PRIMARY KEY(hash, log_id));
CREATE TABLE IF NOT EXISTS guild_cfg(
    guild_id INTEGER, log_type TEXT, key TEXT, value INTEGER,
    PRIMARY KEY(guild_id, log_type, key)
);
CREATE TABLE IF NOT EXISTS role_perms(
    guild_id INTEGER, log_type TEXT, kind TEXT, role_id INTEGER,
    PRIMARY KEY(guild_id, log_type, kind, role_id)
);
CREATE TABLE IF NOT EXISTS point_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL DEFAULT 0,
    log_id INTEGER,
    user_id INTEGER NOT NULL,
    points INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    type TEXT DEFAULT 'log',
    season_id TEXT DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS links(
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, roblox_id INTEGER, roblox_name TEXT,
    PRIMARY KEY(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS season_archive(
    season TEXT, guild_id INTEGER, user_id INTEGER, points INTEGER,
    PRIMARY KEY(season, guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS cmd_roles(
    guild_id INTEGER, cmd_name TEXT, role_id INTEGER,
    PRIMARY KEY(guild_id, cmd_name)
);
CREATE TABLE IF NOT EXISTS crews(
    guild_id INTEGER,
    crew_name TEXT,
    leader_id INTEGER,
    members_data TEXT,
    created_at TEXT,
    PRIMARY KEY(guild_id, crew_name)
);
"""

LEGACY_COPY = {
    "settings": "INSERT OR IGNORE INTO settings(guild_id,key,value) SELECT 0, key, CAST(value AS TEXT) FROM settings_legacy",
    "members": "INSERT OR IGNORE INTO members(guild_id,user_id,points) SELECT 0, user_id, points FROM members_legacy",
    "links": "INSERT OR IGNORE INTO links(guild_id,user_id,roblox_id,roblox_name) SELECT 0, user_id, roblox_id, roblox_name FROM links_legacy",
    "season_archive": "INSERT OR IGNORE INTO season_archive(season,guild_id,user_id,points) SELECT season, 0, user_id, points FROM season_archive_legacy",
    "point_events": "INSERT INTO point_events(guild_id,log_id,user_id,points,created_at,type,season_id) SELECT 0, log_id, user_id, points, created_at, 'log', 'main' FROM point_events_legacy",
}
LOG_COLUMNS = [
    ("guild_id", "INTEGER NOT NULL DEFAULT 0"), ("review_channel", "INTEGER"), ("review_msg", "INTEGER"),
    ("public_channel", "INTEGER"), ("public_msg", "INTEGER"), ("reviewed_at", "TEXT"),
    ("reminded", "INTEGER DEFAULT 0"), ("parent_id", "INTEGER"), ("season_id", "TEXT DEFAULT 'main'"),
]
LEGACY_TYPE_MAP = {"outside": "1v1", "internal": "training"}
LEGACY_POINT_KEYS = {"points:outside_win": "points:1v1_win", "points:outside_loss": "points:1v1_loss",
                     "points:internal": "points:training", "template:outside": "template:1v1",
                     "template:internal": "template:training"}

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_logs_guild_status ON logs(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_logs_author_created ON logs(guild_id, author_id, created_at);
CREATE INDEX IF NOT EXISTS idx_logs_season ON logs(guild_id, season_id);
CREATE INDEX IF NOT EXISTS idx_point_events_guild_season ON point_events(guild_id, season_id);
CREATE INDEX IF NOT EXISTS idx_point_events_user ON point_events(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_point_events_created ON point_events(guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_point_events_log ON point_events(log_id);
CREATE INDEX IF NOT EXISTS idx_point_events_season_user ON point_events(guild_id, season_id, user_id);
CREATE INDEX IF NOT EXISTS idx_logs_parent ON logs(parent_id);
CREATE INDEX IF NOT EXISTS idx_hashes_hash ON hashes(hash);
CREATE INDEX IF NOT EXISTS idx_cmd_roles_guild ON cmd_roles(guild_id);
"""

LOG_ID_RE = re.compile(r"`Log #(\d+)`")
LOG_ID_FALLBACK_RE = re.compile(r"Log #(\d+)")
TEMPLATE_VAR_RE = re.compile(r"\{(\w{1,32})\}")


def now_utc():
    return dt.datetime.now(timezone.utc)


def owner_id():
    try:
        return int(os.getenv("OWNER_ID") or getattr(config, "OWNER_ID", 0) or 0)
    except ValueError:
        return 0


# ================================================================ قاعدة البيانات
async def table_columns(db, table):
    cur = await db.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in await cur.fetchall()]


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=5000;")

        for table in LEGACY_COPY:
            cols = await table_columns(db, table)
            if cols and "guild_id" not in cols and not await table_columns(db, table + "_legacy"):
                await db.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
        await db.executescript(SCHEMA)

        migrated_members = False
        for table, copy_sql in LEGACY_COPY.items():
            if await table_columns(db, table + "_legacy"):
                await db.execute(copy_sql)
                await db.execute(f"DROP TABLE {table}_legacy")
                migrated_members = migrated_members or table == "members"
        if migrated_members:
            await db.execute(
                "INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) "
                "SELECT 0, NULL, m.user_id, m.points - COALESCE((SELECT SUM(e.points) FROM point_events e "
                "WHERE e.guild_id=0 AND e.user_id=m.user_id AND e.season_id='main'), 0), ?, 'opening', 'main' "
                "FROM members m WHERE m.guild_id=0 AND m.points - COALESCE((SELECT SUM(e.points) FROM point_events e "
                "WHERE e.guild_id=0 AND e.user_id=m.user_id AND e.season_id='main'), 0) <> 0", (now_utc().isoformat(),))

        have = await table_columns(db, "logs")
        for col, decl in LOG_COLUMNS:
            if col not in have:
                await db.execute(f"ALTER TABLE logs ADD COLUMN {col} {decl}")
        pe = await table_columns(db, "point_events")
        for col, decl in (("type", "TEXT DEFAULT 'log'"), ("season_id", "TEXT DEFAULT 'main'")):
            if col not in pe:
                await db.execute(f"ALTER TABLE point_events ADD COLUMN {col} {decl}")

        for old, new in LEGACY_TYPE_MAP.items():
            await db.execute("UPDATE logs SET type=? WHERE type=?", (new, old))
            await db.execute("UPDATE OR IGNORE guild_cfg SET log_type=? WHERE log_type=?", (new, old))
            await db.execute("DELETE FROM guild_cfg WHERE log_type=?", (old,))
            await db.execute("UPDATE OR IGNORE role_perms SET log_type=? WHERE log_type=?", (new, old))
            await db.execute("DELETE FROM role_perms WHERE log_type=?", (old,))
        for old_key, new_key in LEGACY_POINT_KEYS.items():
            await db.execute("UPDATE OR IGNORE settings SET key=? WHERE key=?", (new_key, old_key))
            await db.execute("DELETE FROM settings WHERE key=?", (old_key,))

        cur = await db.execute("SELECT guild_id, review_channel, log_channel, audit_channel FROM guild_channels")
        for g, r, l, a in await cur.fetchall():
            for key, v in (("review_channel", r), ("log_channel", l), ("audit_channel", a)):
                if v:
                    await db.execute("INSERT OR IGNORE INTO guild_cfg VALUES(?,?,?,?)", (g, "all", key, v))

        await db.executescript(INDEXES)
        await db.commit()
    await auto_restore_from_json()
    await save_config_snapshot_json()


async def auto_restore_from_json():
    if not os.path.exists(JSON_CONFIG_PATH):
        return
    try:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT (SELECT COUNT(*) FROM settings) + (SELECT COUNT(*) FROM guild_cfg) "
                                   "+ (SELECT COUNT(*) FROM role_perms) + (SELECT COUNT(*) FROM cmd_roles)")
            if (await cur.fetchone())[0] > 0:
                return
        raw = await asyncio.to_thread(_read_bytes, JSON_CONFIG_PATH)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return
        async with aiosqlite.connect(DB) as db:
            for r in data.get("settings", []):
                await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?,?)", (r["guild_id"], r["key"], str(r["value"])))
            for r in data.get("guild_cfg", []):
                await db.execute("INSERT OR IGNORE INTO guild_cfg VALUES(?,?,?,?)",
                                 (r["guild_id"], r["log_type"], r["key"], r["value"]))
            for r in data.get("role_perms", []):
                await db.execute("INSERT OR IGNORE INTO role_perms VALUES(?,?,?,?)",
                                 (r["guild_id"], r["log_type"], r["kind"], r["role_id"]))
            for r in data.get("cmd_roles", []):
                await db.execute("INSERT OR IGNORE INTO cmd_roles VALUES(?,?,?)", (r["guild_id"], r["cmd_name"], r["role_id"]))
            await db.commit()
    except Exception as e:
        print(f"⚠️ Could not restore settings snapshot: {e}")


async def adopt_legacy_data(guild_id: int):
    async with aiosqlite.connect(DB) as db:
        for table in ("settings", "members", "links", "season_archive", "crews"):
            await db.execute(f"UPDATE OR IGNORE {table} SET guild_id=? WHERE guild_id=0", (guild_id,))
            await db.execute(f"DELETE FROM {table} WHERE guild_id=0")
        for table in ("logs", "point_events"):
            await db.execute(f"UPDATE {table} SET guild_id=? WHERE guild_id=0", (guild_id,))
        await db.commit()


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, path)


async def save_config_snapshot_json():
    try:
        async with aiosqlite.connect(DB) as db:
            db.row_factory = aiosqlite.Row
            out = {}
            for name, sql in (("guild_cfg", "SELECT * FROM guild_cfg"), ("role_perms", "SELECT * FROM role_perms"),
                              ("cmd_roles", "SELECT * FROM cmd_roles"),
                              ("settings", "SELECT guild_id, key, value FROM settings WHERE key NOT LIKE 'state:%'")):
                cur = await db.execute(sql)
                out[name] = [dict(r) for r in await cur.fetchall()]
        out.update({"version": BOT_VERSION, "description": "Configuration snapshot only.",
                    "updated_at": now_utc().isoformat()})
        await asyncio.to_thread(_write_json, JSON_CONFIG_PATH, out)
    except Exception as e:
        print(f"⚠️ Failed to save config snapshot JSON: {e}")


async def db_exec(sql, params=()):
    async with aiosqlite.connect(DB) as db:
        await db.execute(sql, params)
        await db.commit()


async def raw_setting(guild_id: int, key: str):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
        row = await cur.fetchone()
    return row[0] if row else None


async def get_setting(guild_id: int, key: str):
    v = await raw_setting(guild_id, key)
    if v is not None:
        try:
            return int(v)
        except (TypeError, ValueError):
            return v
    if key.startswith("points:"):
        return DEFAULT_POINTS.get(key[len("points:"):], 0)
    if key.startswith("rank:"):
        n = key[len("rank:"):]
        return next((p for name, p in LADDER if name == n), 0)
    if key.startswith("rule:"):
        return RULE_DEFAULTS.get(key[len("rule:"):], 0)
    return None


async def set_setting(guild_id: int, key: str, value):
    await db_exec(
        "INSERT INTO settings(guild_id, key, value) VALUES(?,?,?) "
        "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (guild_id, str(key), str(value)))
    if not str(key).startswith("state:"):
        await save_config_snapshot_json()


async def rule(guild_id: int, name: str) -> int:
    v = await get_setting(guild_id, "rule:" + name)
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(RULE_DEFAULTS.get(name, 0))


async def get_current_season(guild_id: int) -> str:
    s = await raw_setting(guild_id, "state:current_season")
    return str(s) if s else "main"


async def cfg_get(guild_id, log_type, key):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT value FROM guild_cfg WHERE guild_id=? AND key=? AND log_type IN (?, 'all') "
            "ORDER BY (log_type='all') LIMIT 1", (guild_id, key, log_type))
        row = await cur.fetchone()
    return row[0] if row and row[0] is not None else None


async def cfg_set(guild_id, log_type, key, value):
    async with aiosqlite.connect(DB) as db:
        if value is None:
            await db.execute("DELETE FROM guild_cfg WHERE guild_id=? AND log_type=? AND key=?",
                             (guild_id, log_type, key))
        else:
            await db.execute(
                "INSERT INTO guild_cfg VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id, log_type, key) DO UPDATE SET value=excluded.value",
                (guild_id, log_type, key, value))
        await db.commit()
    await save_config_snapshot_json()


async def roles_get(guild_id, log_type, kind):
    async with aiosqlite.connect(DB) as db:
        for t in (log_type, "all"):
            cur = await db.execute(
                "SELECT role_id FROM role_perms WHERE guild_id=? AND log_type=? AND kind=?", (guild_id, t, kind))
            ids = [r[0] for r in await cur.fetchall()]
            if ids:
                return ids
    return []


async def roles_edit(guild_id, log_type, kind, role_id, add: bool):
    async with aiosqlite.connect(DB) as db:
        if add:
            await db.execute("INSERT OR IGNORE INTO role_perms VALUES(?,?,?,?)", (guild_id, log_type, kind, role_id))
        else:
            await db.execute("DELETE FROM role_perms WHERE guild_id=? AND log_type=? AND kind=? AND role_id=?",
                             (guild_id, log_type, kind, role_id))
        await db.commit()
    await save_config_snapshot_json()


async def get_ladder(guild_id: int):
    res = []
    for n, default_pts in LADDER:
        v = await get_setting(guild_id, "rank:" + n)
        res.append((n, v if isinstance(v, int) else default_pts))
    return res


async def get_log(log_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM logs WHERE id=?", (log_id,))
        return await cur.fetchone()


async def points_for(guild_id: int, log_type, result) -> int:
    key = f"{log_type}_{result}" if log_type in ("war", "1v1", "2v2") else log_type
    v = await get_setting(guild_id, "points:" + key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return DEFAULT_POINTS.get(key, 0)


async def recipients_for(row, data):
    if row["type"] == "tryout":
        return [int(data.get("tester_id") or row["author_id"])]
    ids = [row["author_id"]]
    if row["type"] in ("war", "2v2") and await rule(row["guild_id"], "war_participants"):
        confirmed = {int(x) for x in data.get("confirmed_participants", [])}
        for x in data.get("participants", []):
            x = int(x)
            if x not in ids and x in confirmed:
                ids.append(x)
    return ids


# ================================================================ الصلاحيات
def has_role(member, names) -> bool:
    return any(n.lower() in r.name.lower() for r in getattr(member, "roles", []) for n in names)


def is_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if owner_id() and member.id == owner_id():
        return True
    return member.guild_permissions.administrator


def is_editor(member) -> bool:
    return is_admin(member) or has_role(member, EDITOR_ROLES)


async def can_review(member, log_type) -> bool:
    if is_admin(member):
        return True
    ids = await roles_get(member.guild.id, log_type, "review")
    if ids:
        return any(r.id in ids for r in member.roles)
    return has_role(member, REVIEWER_ROLES)


async def can_review_any(member) -> bool:
    for t in LOG_TYPES:
        if await can_review(member, t):
            return True
    return False


async def can_submit(member, log_type) -> bool:
    if is_admin(member):
        return True
    ids = await roles_get(member.guild.id, log_type, "submit")
    if ids:
        return any(r.id in ids for r in member.roles)
    if log_type == "tryout":
        return has_role(member, TRYOUTER_ROLES)
    return True


async def check_cmd_perm(i: discord.Interaction, cmd_name: str) -> bool:
    if not i.guild or not isinstance(i.user, discord.Member):
        return False
    if is_admin(i.user):
        return True
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT role_id FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, cmd_name))
        row = await cur.fetchone()
    if not row:
        return True
    return any(r.id == row[0] for r in i.user.roles)


NO_PERM = "❌ مش معاك صلاحية."
NO_ROLE = "❌ مش معاك الرتبة المطلوبة لاستخدام الأمر ده."


async def gate(i: discord.Interaction, cmd_name: str, editor=False) -> bool:
    if not await check_cmd_perm(i, cmd_name):
        await i.response.send_message(NO_ROLE, ephemeral=True)
        return False
    if editor and not is_editor(i.user):
        await i.response.send_message(NO_PERM, ephemeral=True)
        return False
    return True


# ================================================================ مساعدات
async def audit(text, guild):
    ch_id = await cfg_get(guild.id, "all", "audit_channel")
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        try:
            await ch.send(text[:1990], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def dm_owner(text, file=None):
    if not owner_id():
        return False
    try:
        user = await bot.fetch_user(owner_id())
        if file:
            await user.send(text, file=file)
        else:
            await user.send(text)
        return True
    except (discord.HTTPException, discord.NotFound):
        return False


async def get_member(guild, user_id):
    return guild.get_member(user_id) or await guild.fetch_member(user_id)


def parse_log_id(message: discord.Message):
    for pattern in (LOG_ID_RE, LOG_ID_FALLBACK_RE):
        m = pattern.search(message.content or "")
        if m:
            return int(m.group(1))
    return None


def mentions(role_id=None, user_ids=None):
    return discord.AllowedMentions(
        roles=[discord.Object(id=role_id)] if role_id else False,
        users=[discord.Object(id=u) for u in user_ids] if user_ids else False,
        everyone=False)


def find_crew_role(guild):
    kw = CREW_ROLE_KEYWORD.lower()
    return discord.utils.find(lambda r: r.name.lower().rstrip().endswith(kw), guild.roles)


def ladder_roles(guild):
    return [discord.utils.find(lambda r, n=n: n.lower() in r.name.lower(), guild.roles) for n, _ in LADDER]


def valid_youtube(url: str):
    url = (url or "").strip()
    if not url or len(url) > 200 or any(c.isspace() for c in url):
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() not in YT_HOSTS:
        return None
    return url


def dedupe(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


async def report_error(i: discord.Interaction, error):
    print(f"❌ Unhandled error in interaction: {type(error).__name__}: {error}")
    msg = "❌ حصل خطأ غير متوقع. حاول تاني، ولو اتكرر بلّغ الإدارة."
    try:
        if i.response.is_done():
            await i.followup.send(msg, ephemeral=True)
        else:
            await i.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ================================================================ تشغيل البوت
class CrewBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._ready_once = False

    async def setup_hook(self):
        await init_db()
        self.add_view(ReviewView())
        try:
            await self.http.bulk_upsert_global_commands(self.application_id, [])
        except Exception as e:
            print(f"⚠️ Could not clear global commands: {e}")

    async def sync_guild(self, guild: discord.Guild):
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"✅ Commands synced to {guild.name} ({guild.id})")
            return True
        except Exception as e:
            print(f"❌ Sync failed for {guild.id}: {e}")
            return False

    async def on_ready(self):
        print(f"Bot logged in as {self.user} (Version {BOT_VERSION})")
        if not self._ready_once:
            self._ready_once = True
            try:
                if len(self.guilds) == 1:
                    await adopt_legacy_data(self.guilds[0].id)
            except Exception as e:
                print(f"⚠️ Legacy data adoption failed: {e}")
            for g in self.guilds:
                await self.sync_guild(g)
        if not maintenance.is_running():
            maintenance.start()

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)


bot = CrewBot()


@bot.tree.error
async def on_app_command_error(i: discord.Interaction, error):
    await report_error(i, error)


# ================================================================ شكل اللوج
_EMO = getattr(config, "EMOJIS", {}) or {}
ANGEL = _EMO.get("angel", "<:emoji_2:1568348048906649652>")
MOON = _EMO.get("bullet", "<a:emoji_15:1568348248522100757>")
ARROW = _EMO.get("arrow", "<a:emoji_8:1568348117236187237>")
CROWN = _EMO.get("crown", "<:emoji_11:1568348158495432754>")
_SPARK = _EMO.get("spark", "<a:emoji_6:1568348105781416008>")
DIVIDER = _EMO.get("divider", f"{_SPARK}   ●▬▬▬▬▬▬▬▬▬▬▬☭   ☭▬▬▬▬▬▬▬▬▬▬▬●   {_SPARK}")
MSG_LIMIT = 1900


def clip(v, n=350):
    v = str(v) if v not in (None, "") else "-"
    return v if len(v) <= n else v[: n - 1] + "…"


async def get_custom_template(guild_id, log_type):
    t = await raw_setting(guild_id, f"template:{log_type}")
    return t or None


def apply_template(template: str, values: dict) -> str:
    return TEMPLATE_VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def render(log_id, log_type, author_id, data, result, flags=None, status="pending",
           points=None, reviewer_id=None, reason=None, pts_to=None, custom_template=None):
    label = LOG_TYPES.get(log_type, {}).get("label", log_type)
    d = lambda k, default="-": clip(data.get(k, default))
    participants = dedupe([int(x) for x in data.get("participants", [])]) if isinstance(data.get("participants"), list) else []
    confirmed = {int(x) for x in data.get("confirmed_participants", [])}

    header = f"{ANGEL}  **{label}**  {ANGEL}  🆔 `Log #{log_id}`\n\n{DIVIDER}\n\n"
    body = None
    if custom_template:
        values = {
            "log_id": log_id, "author": f"<@{author_id}>", "score": d("score"), "notes": d("notes"),
            "youtube_link": data.get("youtube_link", "-"), "my_team": d("my_team"),
            "their_team": d("their_team"), "crew": d("crew"), "my_fighter": d("my_fighter"),
            "opponent": d("opponent"), "participants": d("participants") if not participants else
            " ".join(f"<@{p}>" for p in participants), "candidate": data.get("candidate", "-"),
            "roblox": d("roblox"), "p1": d("p1"), "p2": d("p2"),
        }
        body = header + apply_template(clip(custom_template, 1200), values) + "\n\n" + DIVIDER + "\n"

    if body is None:
        m = header + f"{MOON} **Logged by / مسجّل بواسطة:** <@{author_id}>\n"
        if log_type == "war":
            m += f"{MOON} **Dragon Slayer's Fighters:**\n{ARROW}  {d('my_team')}\n\n{DIVIDER}\n\n"
            m += f"{MOON} **{clip(data.get('crew', 'Enemy Crew'), 60)} Fighters:**\n{ARROW}  {d('their_team')}\n\n"
        elif log_type == "1v1":
            m += f"{MOON} **Dragon Slayer's Fighter:**\n{ARROW}  {d('my_fighter')}\n\n{DIVIDER}\n\n"
            m += f"{MOON} **Opponent:**\n{ARROW}  {d('opponent')}\n\n"
        elif log_type == "2v2":
            m += f"{MOON} **Dragon Slayer's Team:**\n{ARROW}  {d('my_team')}\n\n{DIVIDER}\n\n"
            m += f"{MOON} **Enemy Team:**\n{ARROW}  {d('their_team')}\n\n"
        elif log_type == "training":
            people = data.get("participants") if not isinstance(data.get("participants"), list) else None
            if not people and (data.get("p1") or data.get("p2")):
                people = f"{data.get('p1', '-')} vs {data.get('p2', '-')}"
            m += f"{MOON} **Training Participants:**\n{ARROW}  {clip(people)}\n\n"
        elif log_type == "tryout":
            tester = data.get("tester_id") or author_id
            m += f"{MOON} **Tested by:** <@{tester}>\n"
            cand = data.get("candidate", "-")
            if data.get("roblox"):
                cand += f" ({clip(data['roblox'], 40)})"
            m += f"{MOON} **Tryout Candidate:**\n{ARROW}  {cand}\n\n"

        if log_type in ("war", "2v2") and participants:
            m += f"{MOON} **Tagged Teammates Verification:**\n"
            for p_id in participants[:8]:
                m += f"• <@{p_id}> — **{'✅ Confirmed' if p_id in confirmed else '⏳ Unconfirmed'}**\n"
            m += "\n"

        m += f"{DIVIDER}\n\n"
        if result:
            labels = TRYOUT_LABEL if log_type == "tryout" else RESULT_LABEL
            m += f"      {CROWN} **Result / النتيجة:** {labels.get(result, result)}\n"
        m += f"      • **Score / السكور:** {d('score')}\n"
        if data.get("notes"):
            m += f"      • **Notes:** {d('notes')}\n"
        if data.get("youtube_link"):
            m += f"      • **YouTube Video:** <{data['youtube_link']}>\n"
        body = m

    footer = f"\n**Status / الحالة:** {STATUS_LABEL[status]}"
    if points is not None:
        footer += f" | **Points / النقاط:** +{points}"
        if pts_to:
            footer += " → " + " ".join(f"<@{u}>" for u in list(pts_to)[:6])
    if reviewer_id and status != "pending":
        who = {"accepted": "Accepted by / مقبول بواسطة", "rejected": "Rejected by / مرفوض بواسطة", "revoked": "Revoked by / ملغي بواسطة"}[status]
        footer += f"\n{MOON} **{who}:** <@{reviewer_id}>"
    if reason:
        footer += f"\n**Reason / السبب:** {clip(reason, 300)}"
    if flags:
        footer += "\n⚠️ **Warnings / تحذيرات:**\n" + "\n".join("• " + clip(f, 120) for f in flags[:5])
    if len(body) + len(footer) > MSG_LIMIT:
        body = body[: max(200, MSG_LIMIT - len(footer) - 1)] + "…"
    return (body + footer)[:MSG_LIMIT + 100]


# ================================================================ الرتب
def progress_bar(total, ladder, width=10):
    nxt = next(((n, p) for n, p in ladder if p > total), None)
    if not nxt:
        return "█" * width + " MAX"
    prev = max([p for _, p in ladder if p <= total], default=0)
    filled = max(0, min(width, int((total - prev) / max(1, nxt[1] - prev) * width)))
    return f"{'█' * filled}{'░' * (width - filled)} {total}/{nxt[1]} → {nxt[0]}"


def target_index(ladder, total):
    target = -1
    for idx, (_, pts) in enumerate(ladder):
        if total >= pts:
            target = idx
    return target


async def update_member_rank(guild, member, total, allow_demote=False):
    ladder = await get_ladder(guild.id)
    roles = ladder_roles(guild)
    target = target_index(ladder, total)
    held = [idx for idx, r in enumerate(roles) if r and r in member.roles]
    current = max(held) if held else -1

    if allow_demote:
        if target == current and len(held) <= 1:
            return None
    elif target <= current:
        return None

    target_role = roles[target] if 0 <= target < len(roles) else None
    if target >= 0 and target_role is None:
        return None

    to_add = [target_role] if target_role and target_role not in member.roles else []
    to_remove = [roles[idx] for idx in held if roles[idx] != target_role]
    try:
        if to_add:
            await member.add_roles(*to_add, reason="Rank update based on points")
        if to_remove:
            await member.remove_roles(*to_remove, reason="Rank update based on points")
    except Exception:
        pass
    return target_role


async def rank_is_above_points(guild, member, total) -> bool:
    ladder = await get_ladder(guild.id)
    roles = ladder_roles(guild)
    held = [idx for idx, r in enumerate(roles) if r and r in member.roles]
    return bool(held) and max(held) > target_index(ladder, total)


async def bulk_update_ranks(guild: discord.Guild, allow_demote=False):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM members WHERE guild_id=?", (guild.id,))
        rows = await cur.fetchall()
    changed = 0
    for uid, pts in rows:
        member = guild.get_member(uid)
        if not member:
            continue
        try:
            if await update_member_rank(guild, member, pts, allow_demote):
                changed += 1
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return changed


# ================================================================ المراجعة واللوجات
async def resolve_log(i: discord.Interaction, need_pending=True):
    if not i.guild or not isinstance(i.user, discord.Member):
        await i.response.send_message("Use this inside the server.", ephemeral=True)
        return None
    log_id = parse_log_id(i.message)
    row = await get_log(log_id) if log_id else None
    if (not row or row["guild_id"] != i.guild.id
            or (row["review_msg"] and row["review_msg"] != i.message.id)):
        await i.response.send_message("Log not found for this message.", ephemeral=True)
        return None
    if need_pending and row["status"] != "pending":
        await i.response.send_message("This log has already been reviewed or closed.", ephemeral=True)
        return None
    return row


async def accept_log(i: discord.Interaction):
    row = await resolve_log(i)
    if not row:
        return
    if not await can_review(i.user, row["type"]):
        return await i.response.send_message("You don't have permission to review this log.", ephemeral=True)
    if row["author_id"] == i.user.id:
        return await i.response.send_message("You cannot review your own log.", ephemeral=True)
    season = row["season_id"] or "main"
    if season != await get_current_season(i.guild.id):
        return await i.response.send_message("❌ This log belongs to a previous season.", ephemeral=True)
    await i.response.defer()

    log_id = row["id"]
    data = json.loads(row["data"])
    flags = json.loads(row["flags"] or "[]")
    pts = await points_for(i.guild.id, row["type"], row["result"])
    recips = await recipients_for(row, data)
    stamp = now_utc().isoformat()
    totals = {}

    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "UPDATE logs SET status='accepted', points=?, reviewer_id=?, reviewed_at=? "
                "WHERE id=? AND status='pending'", (pts, i.user.id, stamp, log_id))
            if cur.rowcount == 0:
                await db.rollback()
                return await i.followup.send("This log has already been reviewed or closed.", ephemeral=True)
            for uid in recips:
                await db.execute(
                    "INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (i.guild.id, uid, pts, pts))
                await db.execute(
                    "INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) "
                    "VALUES(?,?,?,?,?,?,?)", (i.guild.id, log_id, uid, pts, stamp, "log", season))
                cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, uid))
                totals[uid] = (await cur.fetchone())[0]
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await i.followup.send(f"❌ DB Error: {e}", ephemeral=True)

    custom_tmpl = await get_custom_template(i.guild.id, row["type"])
    pub_id = await cfg_get(i.guild.id, row["type"], "log_channel")
    log_ch = i.guild.get_channel(pub_id) if pub_id else None
    if log_ch:
        ping_id = await cfg_get(i.guild.id, row["type"], "log_ping")
        text = render(log_id, row["type"], row["author_id"], data, row["result"], None,
                      "accepted", pts, i.user.id, pts_to=recips, custom_template=custom_tmpl)
        content = (f"<@&{ping_id}>\n" if ping_id else "") + text
        cand = [int(data["candidate_id"])] if row["type"] == "tryout" and data.get("candidate_id") else None
        try:
            files = [await a.to_file() for a in i.message.attachments]
        except discord.HTTPException:
            files = []
        try:
            sent = await log_ch.send(content=content, files=files, allowed_mentions=mentions(ping_id, cand))
            await db_exec("UPDATE logs SET public_channel=?, public_msg=? WHERE id=?", (log_ch.id, sent.id, log_id))
        except Exception:
            pass

    reviewed = render(log_id, row["type"], row["author_id"], data, row["result"], flags,
                      "accepted", pts, i.user.id, pts_to=recips, custom_template=custom_tmpl)
    try:
        await i.message.edit(content=reviewed, view=None, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass

    for uid, total in totals.items():
        try:
            member = await get_member(i.guild, uid)
            await update_member_rank(i.guild, member, total)
        except Exception:
            pass


class RejectModal(discord.ui.Modal, title="Rejection Reason"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, log_id, message):
        super().__init__()
        self.log_id = log_id
        self.message = message

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        row = await get_log(self.log_id)
        if not row or row["guild_id"] != i.guild.id:
            return await i.followup.send("Log not found.", ephemeral=True)
        async with aiosqlite.connect(DB) as db:
            await db.execute("UPDATE logs SET status='rejected', reviewer_id=?, reason=?, reviewed_at=? WHERE id=? AND status='pending'",
                             (i.user.id, self.reason.value, now_utc().isoformat(), self.log_id))
            await db.commit()
        data = json.loads(row["data"])
        flags = json.loads(row["flags"] or "[]")
        text = render(self.log_id, row["type"], row["author_id"], data, row["result"], flags,
                      "rejected", reviewer_id=i.user.id, reason=self.reason.value)
        try:
            await self.message.edit(content=text, view=None)
        except Exception:
            pass
        await i.followup.send("Log rejected.", ephemeral=True)


class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, custom_id="log:accept")
    async def accept(self, i: discord.Interaction, button: discord.ui.Button):
        await accept_log(i)

    @discord.ui.button(label="Reject", emoji="❌", style=discord.ButtonStyle.danger, custom_id="log:reject")
    async def reject(self, i: discord.Interaction, button: discord.ui.Button):
        row = await resolve_log(i)
        if not row:
            return
        await i.response.send_modal(RejectModal(row["id"], i.message))


# ================================================================ مودال اللوجات
class LogModal(discord.ui.Modal):
    def __init__(self, log_type, result, files, extra, defaults=None, parent_id=None):
        super().__init__(title=LOG_TYPES[log_type]["label"][:45])
        self.log_type, self.result, self.files, self.extra = log_type, result, files, extra
        self.parent_id = parent_id
        self.inputs = {}
        defaults = defaults or {}
        for key, label, long, required in LOG_TYPES[log_type]["fields"]:
            max_len = 350 if long else 100
            dv = defaults.get(key)
            dv = str(dv)[:max_len] if isinstance(dv, (str, int)) and str(dv) else None
            ti = discord.ui.TextInput(
                label=label[:45], required=required, max_length=max_len, default=dv,
                style=discord.TextStyle.paragraph if long else discord.TextStyle.short)
            self.add_item(ti)
            self.inputs[key] = ti

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        season = await get_current_season(i.guild.id)
        data = {k: v.value.strip() for k, v in self.inputs.items()}
        data.update(self.extra)
        
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "INSERT INTO logs(guild_id, type, author_id, result, data, flags, created_at, season_id, parent_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (i.guild.id, self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False),
                 json.dumps([], ensure_ascii=False), now_utc().isoformat(), season, self.parent_id))
            log_id = cur.lastrowid
            await db.commit()

        ch_id = await cfg_get(i.guild.id, self.log_type, "review_channel")
        ch = i.guild.get_channel(ch_id) if ch_id else None
        if not ch:
            return await i.followup.send("❌ Review channel not set.", ephemeral=True)
        
        text = render(log_id, self.log_type, i.user.id, data, self.result, None)
        try:
            sent = await ch.send(content=text, view=ReviewView(), files=[discord.File(io.BytesIO(await a.read()), a.filename) for a in self.files if isinstance(a, discord.Attachment)])
            await db_exec("UPDATE logs SET review_channel=?, review_msg=? WHERE id=?", (ch.id, sent.id, log_id))
        except Exception:
            sent = await ch.send(content=text, view=ReviewView())
            await db_exec("UPDATE logs SET review_channel=?, review_msg=? WHERE id=?", (ch.id, sent.id, log_id))
        
        await i.followup.send(f"✅ Log #{log_id} submitted for review.", ephemeral=True)


async def start_log(i: discord.Interaction, log_type, result, files, extra=None):
    files = [f for f in files if f is not None]
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message("Use inside a server.", ephemeral=True)
    await i.response.send_modal(LogModal(log_type, result, files, extra or {}))


@bot.tree.command(name="log-war", description="Log a Crew War")
@app_commands.choices(result=RESULT_CHOICES)
async def log_war(i: discord.Interaction, result: app_commands.Choice[str], file1: discord.Attachment, file2: discord.Attachment):
    await start_log(i, "war", result.value, [file1, file2])


@bot.tree.command(name="log-1v1", description="Log a 1v1 fight")
@app_commands.choices(result=RESULT_CHOICES)
async def log_1v1(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment):
    await start_log(i, "1v1", result.value, [proof1])


@bot.tree.command(name="log-2v2", description="Log a 2v2 fight")
@app_commands.choices(result=RESULT_CHOICES)
async def log_2v2(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment):
    await start_log(i, "2v2", result.value, [proof1])


@bot.tree.command(name="log-training", description="Log training")
async def log_training(i: discord.Interaction, proof1: discord.Attachment):
    await start_log(i, "training", None, [proof1])


@bot.tree.command(name="log-tryout", description="Log a tryout")
@app_commands.choices(result=TRYOUT_CHOICES)
async def log_tryout(i: discord.Interaction, candidate: discord.Member, result: app_commands.Choice[str], proof: discord.Attachment):
    await start_log(i, "tryout", result.value, [proof], {"candidate": candidate.mention})


# ================================================================ 🛠️ نظام الطواقم المحسّن (Crews Management)
class CrewSelect(discord.ui.Select):
    def __init__(self, crews_list):
        options = []
        for cname, leader_id in crews_list[:25]:
            options.append(discord.SelectOption(label=cname[:100], value=cname, description=f"Leader ID: {leader_id}"))
        super().__init__(placeholder="📂 اختر طاقماً لعرض تفاصيله وأعضائه...", min_values=1, max_values=1, options=options)

    async def callback(self, i: discord.Interaction):
        selected_crew = self.values[0]
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT leader_id, members_data, created_at FROM crews WHERE guild_id=? AND crew_name=?", (i.guild.id, selected_crew))
            row = await cur.fetchone()
        if not row:
            return await i.response.send_message("❌ هذا الطاقم غير موجود أو تم حذفه.", ephemeral=True)
        
        leader_id, members_json, created_at = row
        members = json.loads(members_json) if members_json else []
        
        embed = discord.Embed(title=f"🛡️ تفاصيل الطاقم: {selected_crew}", color=discord.Color.gold(), timestamp=now_utc())
        embed.add_field(name="👑 قائد الطاقم", value=f"<@{leader_id}>", inline=False)
        members_text = " ".join(f"<@{m}>" for m in members) if members else "لا توجد أعضاء مضافة بعد"
        embed.add_field(name=f"👥 الأعضاء ({len(members)})", value=members_text, inline=False)
        embed.set_footer(text=f"تاريخ الإنشاء: {str(created_at)[:10]}")
        await i.response.send_message(embed=embed, ephemeral=True)


class CrewView(discord.ui.View):
    def __init__(self, crews_list):
        super().__init__(timeout=60)
        self.add_item(CrewSelect(crews_list))


@bot.tree.command(name="create-new-crew", description="إنشاء طاقم جديد مرتب مع اختيار أعضائه بطريقة ذكية")
@app_commands.describe(crew_name="اسم الطاقم الجديد", member1="عضو أساسي 1", member2="عضو أساسي 2", member3="عضو أساسي 3")
async def create_new_crew(i: discord.Interaction, crew_name: str, member1: Optional[discord.Member] = None, member2: Optional[discord.Member] = None, member3: Optional[discord.Member] = None):
    if not await gate(i, "create-new-crew", editor=True):
        return
    
    crew_name = crew_name.strip()
    members_ids = [m.id for m in (member1, member2, member3) if m and not m.bot]
    
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM crews WHERE guild_id=? AND crew_name=?", (i.guild.id, crew_name))
        if await cur.fetchone():
            return await i.response.send_message("❌ هذا الطاقم موجود بالفعل بهذا الاسم!", ephemeral=True)
        
        await db.execute("INSERT INTO crews(guild_id, crew_name, leader_id, members_data, created_at) VALUES(?,?,?,?,?)",
                         (i.guild.id, crew_name, i.user.id, json.dumps(members_ids), now_utc().isoformat()))
        await db.commit()
    
    await audit(f"🛡️ قام {i.user.mention} بإنشاء طاقم جديد باسم **{crew_name}**.", i.guild)
    await i.response.send_message(f"✅ تم إنشاء الطاقم **{crew_name}** بنجاح برئاستك ومعك {len(members_ids)} أعضاء أساسيين!", ephemeral=True)


@bot.tree.command(name="crews-list", description="عرض القائمة الحقيقية للطواقم المسجلة مع خيارات تفاعلية")
async def crews_list(i: discord.Interaction):
    if not await gate(i, "crews-list"):
        return
    
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT crew_name, leader_id FROM crews WHERE guild_id=?", (i.guild.id,))
        rows = await cur.fetchall()
    
    if not rows:
        return await i.response.send_message("⚠️ لا توجد أي طواقم مسجلة في هذا السيرفر حالياً. استخدم `/create-new-crew` لإنشاء طاقمك الأول!", ephemeral=True)
    
    await i.response.send_message("📂 **قائمة الطواقم المتاحة في السيرفر:**\nاختر طاقماً من القائمة أدناه لعرض تفاصيله الكاملة وأعضائه:", view=CrewView(rows), ephemeral=True)


# ================================================================ 🌐 نظام اللغة (Language Dropdown Menu)
class LanguageSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="العربية (Arabic)", value="ar", emoji="🇸🇦"),
            discord.SelectOption(label="English", value="en", emoji="🇬🇧"),
            discord.SelectOption(label="Deutsch (German)", value="de", emoji="🇩🇪"),
            discord.SelectOption(label="Italiano (Italian)", value="it", emoji="🇮🇹"),
            discord.SelectOption(label="हिन्दी (Hindi)", value="hi", emoji="🇮🇳"),
        ]
        super().__init__(placeholder="🌐 اختر لغتك المفضلة / Choose your language", min_values=1, max_values=1, options=options)

    async def callback(self, i: discord.Interaction):
        lang = self.values[0]
        await set_setting(i.guild.id, f"state:lang:{i.user.id}", lang)
        
        messages = {
            "ar": "✅ تم تغيير لغة التفاعل الخاصة بك إلى **العربية** بنجاح!",
            "en": "✅ Your preferred interaction language has been updated to **English**!",
            "de": "✅ Deine bevorzugte Interaktionssprache wurde auf **Deutsch** geändert!",
            "it": "✅ La tua lingua di interazione preferita è stata impostata su **Italiano**!",
            "hi": "✅ आपकी पसंदीदा भाषा को **हिन्दी** में बदल دیا गया है!"
        }
        await i.response.send_message(messages.get(lang, messages["en"]), ephemeral=True)


class LanguageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(LanguageSelect())


@bot.tree.command(name="language", description="Change your preferred interaction language | تغيير لغتك المفضلة للتفاعل")
async def language_cmd(i: discord.Interaction):
    if not await gate(i, "language"):
        return
    await i.response.send_message("🌐 **Please select your language / الرجاء اختيار لغتك المفضلة:**", view=LanguageView(), ephemeral=True)


# ================================================================ إعدادات البوت والتشغيل
@bot.tree.command(name="settings", description="عرض إعدادات السيرفر")
async def settings_cmd(i: discord.Interaction):
    if not await gate(i, "settings", editor=True):
        return
    await i.response.send_message(f"⚙️ Server Settings — Version {BOT_VERSION} is active and running smoothly.", ephemeral=True)


@bot.tree.command(name="sync", description="مزامنة الأوامر")
async def sync_cmd(i: discord.Interaction):
    if not await gate(i, "sync", editor=True):
        return
    await i.response.defer(ephemeral=True)
    ok = await bot.sync_guild(i.guild)
    await i.followup.send("✅ Synced successfully." if ok else "❌ Sync failed.")


@tasks.loop(minutes=30)
async def maintenance():
    pass


@maintenance.before_loop
async def before_maintenance():
    await bot.wait_until_ready()


if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("❌ DISCORD_TOKEN is missing in environment variables.")
    bot.run(token)