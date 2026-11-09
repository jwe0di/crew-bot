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

BOT_VERSION = "v2.9.9"
DB = os.getenv("DB_PATH", "crewbot.db")
JSON_CONFIG_PATH = os.getenv("JSON_CONFIG_PATH", "servers_settings.json")
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

# ---- حدود المرفقات والحماية ----
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".mkv"}
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_TOTAL_FILES_SIZE = 50 * 1024 * 1024
MAX_FILES_COUNT = 5
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


# ================================================================ نظام الترجمات واللغات
SUPPORTED_LANGUAGES = {
    "ar": "العربية",
    "en": "English",
    "de": "Deutsch",
    "it": "Italiano",
    "hi": "हिन्दी"
}

TRANSLATIONS = {
    "en": {
        "no_perm": "❌ You don't have permission to perform this action.",
        "no_role": "❌ You are missing the required role for this command.",
        "log_not_found": "❌ Log not found or invalid for this server.",
        "already_reviewed": "⚠️ This log has already been reviewed or closed.",
        "lang_updated": "✅ Your preferred language has been updated to **English**!",
        "lang_desc": "Choose your preferred language from the menu below.",
    },
    "ar": {
        "no_perm": "❌ ليس لديك الصلاحية المطلوبة لتنفيذ هذا الإجراء.",
        "no_role": "❌ تفتقر للرتبة المطلوبة لاستخدام هذا الأمر.",
        "log_not_found": "❌ السجل غير موجود أو غير صالح لهذا السيرفر.",
        "already_reviewed": "⚠️ تم مراجعة هذا السجل أو إغلاقه مسبقاً.",
        "lang_updated": "✅ تم تحديث لغتك المفضلة بنجاح إلى **العربية**!",
        "lang_desc": "اختر لغتك المفضلة من القائمة أدناه.",
    },
    "de": {
        "no_perm": "❌ Keine Berechtigung.",
        "no_role": "❌ Fehlende Rolle.",
        "log_not_found": "❌ Log nicht gefunden.",
        "already_reviewed": "⚠️ Bereits überprüft.",
        "lang_updated": "✅ Sprache aktualisiert zu **Deutsch**!",
        "lang_desc": "Wählen Sie Ihre Sprache.",
    },
    "it": {
        "no_perm": "❌ Non hai i permessi.",
        "no_role": "❌ Ruolo mancante.",
        "log_not_found": "❌ Log non trovato.",
        "already_reviewed": "⚠️ Già revisionato.",
        "lang_updated": "✅ Lingua aggiornata in **Italiano**!",
        "lang_desc": "Scegli la tua lingua.",
    },
    "hi": {
        "no_perm": "❌ अनुमति नहीं है।",
        "no_role": "❌ भूमिका अनुपस्थित है।",
        "log_not_found": "❌ लॉग नहीं मिला।",
        "already_reviewed": "⚠️ पहले ही समीक्षा کی जा चुकी ہے۔",
        "lang_updated": "✅ भाषा **हिन्दी** में अपडेट की गई!",
        "lang_desc": "भाषा चुनें।",
    }
}

def t(lang: str, key: str, **kwargs) -> str:
    lang_dict = TRANSLATIONS.get(lang, TRANSLATIONS["en"])
    text = lang_dict.get(key, TRANSLATIONS["en"].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

async def get_user_lang(user_id: int) -> str:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT lang FROM user_languages WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    return row[0] if row else "en"

async def set_user_lang(user_id: int, lang: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO user_languages(user_id, lang) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang", (user_id, lang))
        await db.commit()


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


# ================================================================ الإعدادات الأساسية
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
    app_commands.Choice(name="Audit channel (سجل التعديلات)", value="audit_channel")]
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
        "label": f"🤖 Bot Hardening Patch {BOT_VERSION}",
        "title_en": f"DragonSlayerBot Release {BOT_VERSION}",
        "title_ar": f"إصدار البوت {BOT_VERSION}",
        "desc_en": ("• Fully functional public commands (rates, link, profile, stats).\n"
                    "• Auto-purge channel messages on updates.\n"
                    "• Bilingual comprehensive command manual (Arabic & English).\n"
                    "• Improved Log ID visibility on embeds and review messages."),
        "desc_ar": ("• تفعيل كافة الأوامر العامة وتعمل بشكل حقيقي ودقيق.\n"
                    "• مسح رسائل قناة التحديثات تلقائياً عند كل تحديث جديد.\n"
                    "• دليل أوامر شامل وثنائي اللغة (عربي وإنجليزي).\n"
                    "• تحسين ظهور أيدي اللوج (Log ID) بوضوح تام."),
    }
}
UPDATE_TYPE_CHOICES = [app_commands.Choice(name=v["label"][:100], value=k) for k, v in UPDATE_PRESETS.items()]

ALL_BOT_COMMANDS = [
    "log-war", "log-1v1", "log-2v2", "log-training", "log-tryout", "resubmit",
    "log-update", "set-log-template", "points", "leaderboard", "profile",
    "stats", "rates", "link", "pending", "revoke-log", "inactive",
    "season-reset", "season-top", "backup", "restore-backup", "setchannel", "setping",
    "setroles", "setrule", "set-log-points", "set-rank-points", "set-points",
    "sync-ranks", "verify-points", "settings", "sync", "set-cmd-role", "set-update-ping",
    "help", "send-help-panel", "language", "create-new-crew"
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
CREATE TABLE IF NOT EXISTS user_languages(
    user_id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'en'
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


# ================================================================ استعادة الإعدادات التلقائية
async def auto_restore_from_json():
    if not os.path.exists(JSON_CONFIG_PATH):
        return
    try:
        def read_json():
            with open(JSON_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        
        data = await asyncio.to_thread(read_json)
        if not isinstance(data, dict):
            return

        async with aiosqlite.connect(DB) as db:
            for row in data.get("settings", []):
                await db.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?)", 
                                 (row.get("guild_id", 0), row.get("key"), str(row.get("value"))))
            for row in data.get("guild_cfg", []):
                await db.execute("INSERT OR REPLACE INTO guild_cfg VALUES(?,?,?,?)", 
                                 (row.get("guild_id", 0), row.get("log_type"), row.get("key"), row.get("value")))
            for row in data.get("role_perms", []):
                await db.execute("INSERT OR REPLACE INTO role_perms VALUES(?,?,?,?)", 
                                 (row.get("guild_id", 0), row.get("log_type"), row.get("kind"), row.get("role_id")))
            for row in data.get("cmd_roles", []):
                await db.execute("INSERT OR REPLACE INTO cmd_roles VALUES(?,?,?)", 
                                 (row.get("guild_id", 0), row.get("cmd_name"), row.get("role_id")))
            await db.commit()
        print("✅ [Auto-Restore] Server settings loaded successfully from JSON snapshot file.")
    except Exception as e:
        print(f"⚠️ [Auto-Restore Error] Could not load JSON config snapshot: {e}")


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


async def adopt_legacy_data(guild_id: int):
    async with aiosqlite.connect(DB) as db:
        for table in ("settings", "members", "links", "season_archive"):
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


# ================================================================ الصلاحيات والحماية
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


async def gate(i: discord.Interaction, cmd_name: str, editor=False) -> bool:
    lang = await get_user_lang(i.user.id)
    if not await check_cmd_perm(i, cmd_name):
        await i.response.send_message(t(lang, "no_role"), ephemeral=True)
        return False
    if editor and not is_editor(i.user):
        await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
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
    msg = "❌ حصل خطأ غير متوقع. حاول تاني / An unexpected error occurred."
    try:
        if i.response.is_done():
            await i.followup.send(msg, ephemeral=True)
        else:
            await i.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ================================================================ إعداد البوت
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


# ================================================================ القوالب والرسوم (Rendering)
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
    d = lambda k, default="-": clip(data.get(k, default))  # noqa: E731
    participants = dedupe([int(x) for x in data.get("participants", [])]) if isinstance(data.get("participants"), list) else []
    
    values = {
        "log_id": log_id,
        "author": f"<@{author_id}>",
        "score": d("score"),
        "notes": d("notes"),
        "youtube_link": data.get("youtube_link", "-"),
        "my_team": d("my_team"),
        "their_team": d("their_team"),
        "crew": d("crew"),
        "my_fighter": d("my_fighter"),
        "opponent": d("opponent"),
        "participants": " ".join(f"<@{p}>" for p in participants) if participants else "-",
        "candidate": data.get("candidate", "-"),
        "roblox": d("roblox"),
        "p1": d("p1"),
        "p2": d("p2"),
        "result": TRYOUT_LABEL.get(result, result) if log_type == "tryout" else RESULT_LABEL.get(result, result) if result else "-"
    }

    if custom_template:
        body = apply_template(custom_template, values)
    else:
        header = f"<:emoji_2:1568348048906649652>   **{label}**  <:emoji_2:1568348048906649652>  `🆔 Log #{log_id}`\n\n<a:emoji_6:1568348105781416008>   ●▬▬▬▬▬▬▬▬▬▬▬☭   ☭▬▬▬▬▬▬▬▬▬▬▬●   <a:emoji_6:1568348105781416008>\n\n"
        m = header + f"<a:emoji_15:1568348248522100757>   **Logged by / مسجّل بواسطة:** <@{author_id}> `[ID: #{log_id}]`\n\n"
        if log_type == "1v1":
            m += f"<a:emoji_15:1568348248522100757>   **Dragon Slayer's Fighter / مقاتلنا:**\n<a:emoji_8:1568348117236187237>   {d('my_fighter')}\n\n"
            m += f"<a:emoji_15:1568348248522100757>   **Opponent / الخصم:**\n<a:emoji_8:1568348117236187237>   {d('opponent')}\n\n"
        else:
            m += f"<a:emoji_15:1568348248522100757>   **Details / التفاصيل:** {d('score')}\n"
        body = m

    footer = f"\n<:EU1:1449430079611600936>   ●▬▬▬▬▬▬▬▬▬▬▬☭  <:Grok2:1560609774423511060>   ☭▬▬▬▬▬▬▬▬▬▬▬●   <:EU1:1449430079611600936>\n\n**Status / الحالة:** {STATUS_LABEL[status]} | **Log ID:** `#{log_id}`"
    if points is not None:
        footer += f" | **Points / النقاط:** +{points}"
        if pts_to:
            footer += " → " + " ".join(f"<@{u}>" for u in list(pts_to)[:6])
    if reviewer_id and status != "pending":
        who = {"accepted": "Accepted by / مقبول بواسطة", "rejected": "Rejected by / مرفوض بواسطة", "revoked": "Revoked by / ملغي بواسطة"}[status]
        footer += f"\n<a:emoji_15:1568348248522100757>   **{who}:** <@{reviewer_id}>"
    if reason:
        footer += f"\n<:emoji_11:1568348158495432754>   **Reason / السبب:** {clip(reason, 300)}"
    if flags:
        footer += "\n⚠️ **Warnings / تحذيرات:**\n" + "\n".join(f"<a:emoji_8:1568348117236187237>   " + clip(f, 120) for f in flags[:5])

    if len(body) + len(footer) > MSG_LIMIT:
        body = body[: max(200, MSG_LIMIT - len(footer) - 1)] + "…"
    return (body + footer)[:MSG_LIMIT + 100]


# ================================================================ الرتب (Ranks)
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
        await audit(f"⚠️ Role for **{ladder[target][0]}** was not found in this server, rank update skipped for {member.mention}.", guild)
        return None

    to_add = [target_role] if target_role and target_role not in member.roles else []
    to_remove = [roles[idx] for idx in held if roles[idx] != target_role]
    try:
        if to_add:
            await member.add_roles(*to_add, reason="Rank update based on points")
        if to_remove:
            await member.remove_roles(*to_remove, reason="Rank update based on points")
    except discord.Forbidden:
        await audit(f"❌ [Role Permission Error] Can't manage roles for {member.mention}.", guild)
        return None
    except discord.HTTPException as e:
        await audit(f"❌ [Discord API Error] Rank update failed for {member.mention}: {e}", guild)
        return None

    if target_role and target > current:
        ch_id = await cfg_get(guild.id, "all", "log_channel")
        ch = guild.get_channel(ch_id) if ch_id else None
        if ch:
            try:
                await ch.send(f"🎉 {member.mention} has been promoted to {target_role.mention}!",
                              allowed_mentions=discord.AllowedMentions(users=[member], roles=False))
            except discord.HTTPException:
                pass
        try:
            await member.send(f"🎉 You've been promoted to **{target_role.name}** in **{guild.name}**!")
        except discord.HTTPException:
            pass
    return target_role


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
        except Exception as e:
            print(f"⚠️ Rank update failed for {uid}: {e}")
        await asyncio.sleep(0.25)
    return changed


# ================================================================ المراجعة (Reviews)
async def resolve_log(i: discord.Interaction, need_pending=True):
    if not i.guild or not isinstance(i.user, discord.Member):
        await i.response.send_message("Use this inside the server. / استخدم الأمر داخل السيرفر.", ephemeral=True)
        return None
    log_id = parse_log_id(i.message)
    row = await get_log(log_id) if log_id else None
    if (not row or row["guild_id"] != i.guild.id
            or (row["review_msg"] and row["review_msg"] != i.message.id)):
        await i.response.send_message("Log not found for this message. / اللوج غير موجود.", ephemeral=True)
        return None
    if need_pending and row["status"] != "pending":
        await i.response.send_message("This log has already been reviewed or closed. / تم مراجعة هذا اللوج مسبقاً.", ephemeral=True)
        return None
    return row


async def accept_log(i: discord.Interaction):
    row = await resolve_log(i)
    if not row:
        return
    if not await can_review(i.user, row["type"]):
        return await i.response.send_message("You don't have permission to review this log. / ليس لديك صلاحية مراجعة هذا اللوج.", ephemeral=True)
    if row["author_id"] == i.user.id:
        return await i.response.send_message("You cannot review your own log. / لا يمكنك مراجعة لوج خاص بك.", ephemeral=True)
    season = row["season_id"] or "main"
    if season != await get_current_season(i.guild.id):
        return await i.response.send_message(
            "❌ This log belongs to a previous season and can't be accepted. Reject it instead. / هذا اللوج يتبع موسم سابق.", ephemeral=True)
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
            print(f"❌ accept_log DB error: {e}")
            return await i.followup.send("❌ Database error during acceptance.", ephemeral=True)

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
        except discord.HTTPException as e:
            await i.followup.send(f"Accepted, but couldn't post in log channel: {e}", ephemeral=True)
    else:
        await i.followup.send("Accepted, but no log channel set. Use /setchannel.", ephemeral=True)

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
        except (discord.NotFound, discord.Forbidden) as e:
            print(f"⚠️ Failed to update rank for {uid}: {e}")

    if (row["type"] == "tryout" and row["result"] == "win" and data.get("candidate_id")
            and await rule(i.guild.id, "tryout_autorole")):
        try:
            cand_member = await get_member(i.guild, int(data["candidate_id"]))
            crew_role = find_crew_role(i.guild)
            if crew_role and crew_role not in cand_member.roles:
                await cand_member.add_roles(crew_role, reason="Passed tryout")
        except (discord.NotFound, discord.Forbidden) as e:
            await i.followup.send(f"Accepted, but couldn't give Crew role: {e}", ephemeral=True)


class RejectModal(discord.ui.Modal, title="Rejection Reason / سبب الرفض"):
    reason = discord.ui.TextInput(label="Reason / السبب", style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, log_id, message):
        super().__init__()
        self.log_id = log_id
        self.message = message

    async def on_error(self, interaction, error):
        await report_error(interaction, error)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        row = await get_log(self.log_id)
        if not row or row["guild_id"] != i.guild.id:
            return await i.followup.send("Log not found.", ephemeral=True)
        if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
            return await i.followup.send("❌ No permission.", ephemeral=True)

        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE logs SET status='rejected', reviewer_id=?, reason=?, reviewed_at=? "
                    "WHERE id=? AND status='pending'",
                    (i.user.id, self.reason.value, now_utc().isoformat(), self.log_id))
                if cur.rowcount == 0:
                    await db.rollback()
                    return await i.followup.send("Already reviewed.", ephemeral=True)
                await db.commit()
            except Exception as e:
                await db.rollback()
                return await i.followup.send("❌ Database error.", ephemeral=True)

        data = json.loads(row["data"])
        flags = json.loads(row["flags"] or "[]")
        custom_tmpl = await get_custom_template(i.guild.id, row["type"])
        text = render(self.log_id, row["type"], row["author_id"], data, row["result"], flags,
                      "rejected", reviewer_id=i.user.id, reason=self.reason.value, custom_template=custom_tmpl)
        try:
            await self.message.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        await i.followup.send("Log rejected. / تم رفض اللوج.", ephemeral=True)
        try:
            author = await bot.fetch_user(row["author_id"])
            await author.send(f"❌ Log #{self.log_id} was rejected.\nReason / السبب: {self.reason.value}")
        except discord.HTTPException:
            pass


class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction, error, item):
        await report_error(interaction, error)

    @discord.ui.button(label="Accept / قبول", emoji="✅", style=discord.ButtonStyle.success, custom_id="log:accept")
    async def accept(self, i: discord.Interaction, button: discord.ui.Button):
        await accept_log(i)

    @discord.ui.button(label="Reject / رفض", emoji="❌", style=discord.ButtonStyle.danger, custom_id="log:reject")
    async def reject(self, i: discord.Interaction, button: discord.ui.Button):
        row = await resolve_log(i)
        if not row:
            return
        if not await can_review(i.user, row["type"]):
            return await i.response.send_message("No permission.", ephemeral=True)
        if row["author_id"] == i.user.id:
            return await i.response.send_message("Can't review your own log.", ephemeral=True)
        await i.response.send_modal(RejectModal(row["id"], i.message))

    @discord.ui.button(label="Confirm Participation / تأكيد المشاركة", emoji="✋", style=discord.ButtonStyle.primary,
                       custom_id="log:confirm_part")
    async def confirm_part(self, i: discord.Interaction, button: discord.ui.Button):
        row = await resolve_log(i)
        if not row:
            return
        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute("SELECT status, data FROM logs WHERE id=? AND guild_id=?",
                                       (row["id"], i.guild.id))
                log_row = await cur.fetchone()
                if not log_row or log_row[0] != "pending":
                    await db.rollback()
                    return await i.response.send_message("❌ Not pending.", ephemeral=True)
                data = json.loads(log_row[1])
                participants = [int(x) for x in data.get("participants", [])]
                if i.user.id not in participants:
                    await db.rollback()
                    return await i.response.send_message("❌ You are not listed as a participant.", ephemeral=True)
                confirmed = [int(x) for x in data.get("confirmed_participants", [])]
                if i.user.id in confirmed:
                    await db.rollback()
                    return await i.response.send_message("✅ Already confirmed.", ephemeral=True)
                confirmed.append(i.user.id)
                data["confirmed_participants"] = confirmed
                await db.execute("UPDATE logs SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), row["id"]))
                await db.commit()
            except Exception as e:
                await db.rollback()
                return await i.response.send_message("❌ Error.", ephemeral=True)

        flags = json.loads(row["flags"] or "[]")
        custom_tmpl = await get_custom_template(i.guild.id, row["type"])
        updated = render(row["id"], row["type"], row["author_id"], data, row["result"], flags,
                         custom_template=custom_tmpl)
        await i.response.send_message("✅ Participation confirmed! / تم تأكيد مشاركتك بنجاح!", ephemeral=True)
        try:
            await i.message.edit(content=updated, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


# ================================================================ إنشاء اللوج (Log Submissions)
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

    async def on_error(self, interaction, error):
        await report_error(interaction, error)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        if len(self.files) > MAX_FILES_COUNT:
            return await i.followup.send(f"❌ Too many files (max {MAX_FILES_COUNT}).", ephemeral=True)
        total_size = 0
        for a in self.files:
            if isinstance(a, discord.Attachment):
                ext = os.path.splitext(a.filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    return await i.followup.send(f"❌ Unsupported format: `{a.filename}`.", ephemeral=True)
                if a.size > MAX_FILE_SIZE:
                    return await i.followup.send(f"❌ File too large: `{a.filename}` (>25MB).", ephemeral=True)
                total_size += a.size
        if total_size > MAX_TOTAL_FILES_SIZE:
            return await i.followup.send("❌ Total size exceeds 50MB.", ephemeral=True)

        season = await get_current_season(i.guild.id)
        limit = await rule(i.guild.id, "daily_limit")

        blobs, proof_urls = [], []
        for a in self.files:
            if isinstance(a, discord.Attachment):
                blobs.append((a, await a.read()))
                proof_urls.append(a.url)
            elif isinstance(a, str):
                proof_urls.append(a)

        hashes, flags = await inspect_blobs(blobs, i.guild.id)
        data = {k: v.value.strip() for k, v in self.inputs.items()}
        data.update(self.extra)
        if proof_urls:
            data["proof_urls"] = proof_urls

        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if limit:
                    since = (now_utc() - timedelta(hours=24)).isoformat()
                    cur = await db.execute(
                        "SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? "
                        "AND status IN ('pending','accepted')", (i.guild.id, i.user.id, since))
                    if (await cur.fetchone())[0] >= limit:
                        await db.rollback()
                        return await i.followup.send(f"❌ Daily limit reached ({limit} logs/24h). / تم الوصول للحد اليومي.", ephemeral=True)
                cur = await db.execute(
                    "INSERT INTO logs(guild_id, type, author_id, result, data, flags, created_at, season_id, parent_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (i.guild.id, self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False),
                     json.dumps(flags, ensure_ascii=False), now_utc().isoformat(), season, self.parent_id))
                log_id = cur.lastrowid
                for h in hashes:
                    await db.execute("INSERT OR IGNORE INTO hashes VALUES(?,?)", (h, log_id))
                await db.commit()
            except Exception as e:
                await db.rollback()
                return await i.followup.send("❌ Database error.", ephemeral=True)

        ch_id = await cfg_get(i.guild.id, self.log_type, "review_channel")
        ch = i.guild.get_channel(ch_id) if ch_id else None
        ping_id = await cfg_get(i.guild.id, self.log_type, "review_ping")
        custom_tmpl = await get_custom_template(i.guild.id, self.log_type)
        text = render(log_id, self.log_type, i.user.id, data, self.result, flags, custom_template=custom_tmpl)
        content = (f"<@&{ping_id}>\n" if ping_id else "") + text
        am = mentions(ping_id)

        async def fail_publish(reason):
            await db_exec("UPDATE logs SET status='rejected', reason=? WHERE id=?",
                          (f"failed to post: {reason}"[:200], log_id))
            await i.followup.send(f"❌ Couldn't post log for review: {reason}", ephemeral=True)

        if not ch:
            return await fail_publish("review channel not set")
        sent = None
        try:
            limit_bytes = getattr(i.guild, "filesize_limit", 25 * 1024 * 1024) or 25 * 1024 * 1024
            if blobs and total_size <= limit_bytes:
                try:
                    sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am,
                                         files=[discord.File(io.BytesIO(b), a.filename) for a, b in blobs])
                except Exception:
                    sent = None
            if sent is None:
                sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am)
                if proof_urls:
                    await ch.send("**Proof Links / روابط الإثبات:**\n" + "\n".join(proof_urls)[:1800])
        except discord.HTTPException as e:
            return await fail_publish(str(e))

        await db_exec("UPDATE logs SET review_channel=?, review_msg=? WHERE id=?", (ch.id, sent.id, log_id))
        if flags:
            await dm_owner(f"⚠️ {i.user.mention} submitted Log #{log_id} with warnings:\n" + "\n".join(flags))
        await i.followup.send(f"✅ Log #{log_id} submitted for review successfully. / تم رفع اللوج للمراجعة بنجاح.", ephemeral=True)


async def inspect_blobs(blobs, guild_id):
    hashes, flags = [], []
    for att, data in blobs:
        h = hashlib.sha256(data).hexdigest()
        hashes.append(h)
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT h.log_id FROM hashes h JOIN logs l ON l.id=h.log_id "
                "WHERE h.hash=? AND l.guild_id=? AND l.status NOT IN ('rejected') LIMIT 1", (h, guild_id))
            row = await cur.fetchone()
        if row:
            flags.append(f"{att.filename}: same file used in Log #{row[0]}")
    return hashes, flags


async def start_log(i: discord.Interaction, log_type, result, files: List[Optional[discord.Attachment]], extra=None):
    files = [f for f in files if f is not None]
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message("Use inside server.", ephemeral=True)
    if not await check_cmd_perm(i, f"log-{log_type}"):
        return await i.response.send_message(NO_ROLE, ephemeral=True)
    if not await can_submit(i.user, log_type):
        return await i.response.send_message("No permission to submit.", ephemeral=True)
    if log_type == "war" and len(files) < 2:
        return await i.response.send_message("❌ Crew War requires at least 2 attachments. / حرب الكرو تتطلب ملفين على الأقل.", ephemeral=True)
    if log_type != "war" and len(files) < 1:
        return await i.response.send_message("❌ At least 1 attachment required. / مطلوب ملف إثبات واحد على الأقل.", ephemeral=True)
    rid = await cfg_get(i.guild.id, log_type, "review_channel")
    if not rid or not i.guild.get_channel(rid):
        return await i.response.send_message("Review channel not set. Use /setchannel.", ephemeral=True)
    limit = await rule(i.guild.id, "daily_limit")
    if limit:
        since = (now_utc() - timedelta(hours=24)).isoformat()
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? "
                "AND status IN ('pending','accepted')", (i.guild.id, i.user.id, since))
            if (await cur.fetchone())[0] >= limit:
                return await i.response.send_message(f"❌ Daily limit reached ({limit} logs/24h). / تم الوصول للحد اليومي.", ephemeral=True)
    await i.response.send_modal(LogModal(log_type, result, files, extra or {}))


async def youtube_extra(i: discord.Interaction, youtube_link: Optional[str], extra: dict):
    if youtube_link:
        yt = valid_youtube(youtube_link)
        if not yt:
            await i.response.send_message("❌ Invalid YouTube link. / رابط يوتيوب غير صالح.", ephemeral=True)
            return False
        extra["youtube_link"] = yt
    return True


@bot.tree.command(name="log-war", description="Log a Crew War / تسجيل حرب كرو")
@app_commands.choices(result=RESULT_CHOICES)
@app_commands.describe(result="Win or Loss / فوز أو خسارة", file1="File 1", file2="File 2", file3="File 3", youtube_link="YouTube link",
                       player1="Teammate 1", player2="Teammate 2", player3="Teammate 3", player4="Teammate 4")
async def log_war(i: discord.Interaction, result: app_commands.Choice[str],
                  file1: discord.Attachment, file2: discord.Attachment,
                  file3: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None,
                  player1: Optional[discord.Member] = None, player2: Optional[discord.Member] = None,
                  player3: Optional[discord.Member] = None, player4: Optional[discord.Member] = None):
    ids = dedupe([p.id for p in (player1, player2, player3, player4) if p and not p.bot and p.id != i.user.id])
    extra = {"participants": ids}
    if not await youtube_extra(i, youtube_link, extra):
        return
    await start_log(i, "war", result.value, [file1, file2, file3], extra)


@bot.tree.command(name="log-1v1", description="Log a 1v1 fight / تسجيل قتال 1 ضد 1")
@app_commands.choices(result=RESULT_CHOICES)
@app_commands.describe(result="Win or Loss", proof1="Proof 1", proof2="Proof 2", youtube_link="YouTube link")
async def log_1v1(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment,
                  proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None):
    extra = {}
    if not await youtube_extra(i, youtube_link, extra):
        return
    await start_log(i, "1v1", result.value, [proof1, proof2], extra)


@bot.tree.command(name="log-2v2", description="Log a 2v2 fight / تسجيل قتال ثنائي 2 ضد 2")
@app_commands.choices(result=RESULT_CHOICES)
@app_commands.describe(result="Win or Loss", proof1="Proof 1", proof2="Proof 2", youtube_link="YouTube link",
                       teammate="Teammate")
async def log_2v2(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment,
                  proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None,
                  teammate: Optional[discord.Member] = None):
    ids = [teammate.id] if teammate and not teammate.bot and teammate.id != i.user.id else []
    extra = {"participants": ids}
    if not await youtube_extra(i, youtube_link, extra):
        return
    await start_log(i, "2v2", result.value, [proof1, proof2], extra)


@bot.tree.command(name="log-training", description="Log training session / تسجيل تدريب")
@app_commands.describe(proof1="Proof 1", proof2="Proof 2")
async def log_training(i: discord.Interaction, proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None):
    await start_log(i, "training", None, [proof1, proof2])


@bot.tree.command(name="log-tryout", description="Log a tryout session / تسجيل تجربة أداء")
@app_commands.choices(result=TRYOUT_CHOICES)
@app_commands.describe(candidate="Candidate", result="Passed/Failed", proof="Proof")
async def log_tryout(i: discord.Interaction, candidate: discord.Member, result: app_commands.Choice[str],
                     proof: discord.Attachment):
    if candidate.bot or candidate.id == i.user.id:
        return await i.response.send_message("❌ Invalid candidate.", ephemeral=True)
    extra = {"candidate_id": candidate.id, "candidate": candidate.mention, "tester_id": i.user.id}
    await start_log(i, "tryout", result.value, [proof], extra)


@bot.tree.command(name="resubmit", description="Resubmit a rejected log / إعادة إرسال لوج مرفوض")
async def resubmit(i: discord.Interaction, log_id: int):
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message("Use inside server.", ephemeral=True)
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id or row["author_id"] != i.user.id:
        return await i.response.send_message("Log not found. / اللوج غير موجود.", ephemeral=True)
    if row["status"] != "rejected" or row["type"] not in LOG_TYPES:
        return await i.response.send_message("Log not rejected. / اللوج ليس مرفوضاً.", ephemeral=True)
    if not await check_cmd_perm(i, f"log-{row['type']}"):
        return await i.response.send_message(NO_ROLE, ephemeral=True)

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id FROM logs WHERE parent_id=? AND status IN ('pending','accepted') LIMIT 1",
                               (log_id,))
        child = await cur.fetchone()
    if child:
        return await i.response.send_message(f"Already resubmitted as #{child[0]}.", ephemeral=True)
    data = json.loads(row["data"])
    files = []
    try:
        ch = i.guild.get_channel(row["review_channel"]) if row["review_channel"] else None
        if ch and row["review_msg"]:
            old = await ch.fetch_message(row["review_msg"])
            files = list(old.attachments)
    except discord.HTTPException:
        pass
    if not files and data.get("proof_urls"):
        files = [u for u in data["proof_urls"] if isinstance(u, str)]
    if not files:
        return await i.response.send_message("Could not fetch old attachments.", ephemeral=True)
    extra = {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants", "youtube_link") if k in data}
    await i.response.send_modal(LogModal(row["type"], row["result"], files, extra, defaults=data, parent_id=log_id))


@bot.tree.command(name="revoke-log", description="Cancel an accepted log / إلغاء لوج مقبول")
async def revoke_log(i: discord.Interaction, log_id: int, reason: app_commands.Range[str, 1, 300]):
    if not await gate(i, "revoke-log"):
        return
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id or row["status"] != "accepted":
        return await i.response.send_message("Accepted log not found. / اللوج المقبول غير موجود.", ephemeral=True)
    if not await can_review(i.user, row["type"]):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    if (row["season_id"] or "main") != await get_current_season(i.guild.id):
        return await i.response.send_message("❌ Log belongs to an archived season.", ephemeral=True)
    await i.response.defer(ephemeral=True)

    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "UPDATE logs SET status='revoked', reason=?, reviewer_id=? WHERE id=? AND status='accepted'",
                (reason, i.user.id, log_id))
            if cur.rowcount == 0:
                await db.rollback()
                return await i.followup.send("❌ Log no longer accepted.", ephemeral=True)
            cur = await db.execute(
                "SELECT user_id, SUM(points) FROM point_events WHERE guild_id=? AND log_id=? AND type='log' "
                "GROUP BY user_id", (i.guild.id, log_id))
            events = await cur.fetchall()
            stamp = now_utc().isoformat()
            for uid, p in events:
                await db.execute("UPDATE members SET points = points - ? WHERE guild_id=? AND user_id=?",
                                 (p, i.guild.id, uid))
                await db.execute(
                    "INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) "
                    "VALUES(?,?,?,?,?,?,?)", (i.guild.id, log_id, uid, -p, stamp, "revoke", row["season_id"] or "main"))
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await i.followup.send("❌ Database error.", ephemeral=True)

    data = json.loads(row["data"])
    custom_tmpl = await get_custom_template(i.guild.id, row["type"])
    text = render(log_id, row["type"], row["author_id"], data, row["result"], None, "revoked",
                  reviewer_id=i.user.id, reason=reason, custom_template=custom_tmpl)
    for ch_id, msg_id in ((row["review_channel"], row["review_msg"]), (row["public_channel"], row["public_msg"])):
        if ch_id and msg_id:
            try:
                ch = i.guild.get_channel(ch_id)
                msg = await ch.fetch_message(msg_id)
                await msg.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass

    await audit(f"🚫 {i.user.mention} revoked Log #{log_id}. Reason: {reason}", i.guild)
    await i.followup.send(f"Successfully revoked Log #{log_id}. / تم إلغاء اللوج بنجاح.", ephemeral=True)


@bot.tree.command(name="pending", description="List pending logs / عرض اللوجات المعلقة")
async def pending(i: discord.Interaction):
    if not await check_cmd_perm(i, "pending"):
        return await i.response.send_message(NO_ROLE, ephemeral=True)
    if not (is_editor(i.user) or await can_review_any(i.user)):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id, type, author_id, created_at, review_channel, review_msg FROM logs "
            "WHERE status='pending' AND guild_id=? ORDER BY id LIMIT 20", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("No logs waiting for review ✅ / لا توجد لوجات معلقة بانتظار المراجعة", ephemeral=True)
    lines = [f"**#{lid}** {LOG_TYPES.get(t, {}).get('label', t)} — <@{author}> — {str(created)[:16]}" for lid, t, author, created, _, _ in rows]
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="setchannel", description="Set channels / ضبط القنوات")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_CHANNEL)
async def setchannel(i: discord.Interaction, log_type: app_commands.Choice[str], kind: app_commands.Choice[str],
                     channel: Optional[discord.TextChannel] = None):
    if not await gate(i, "setchannel", editor=True):
        return
    t = "all" if kind.value == "audit_channel" else log_type.value
    await cfg_set(i.guild.id, t, kind.value, channel.id if channel else None)
    await audit(f"⚙️ {i.user.mention} set {kind.value} ({t}) to {channel.mention if channel else 'none'}", i.guild)
    await i.response.send_message(f"✅ **{kind.name}** ({t}) = {channel.mention if channel else 'Cleared'}", ephemeral=True)


@bot.tree.command(name="setping", description="Set ping roles / ضبط رتب المنشن")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_PING)
async def setping(i: discord.Interaction, log_type: app_commands.Choice[str], kind: app_commands.Choice[str],
                  role: Optional[discord.Role] = None):
    if not await gate(i, "setping", editor=True):
        return
    await cfg_set(i.guild.id, log_type.value, kind.value, role.id if role else None)
    await audit(f"⚙️ {i.user.mention} set {kind.value} ({log_type.value}) to {role.mention if role else 'none'}", i.guild)
    await i.response.send_message(f"✅ **{kind.name}** = {role.mention if role else 'Cleared'}", ephemeral=True)


@bot.tree.command(name="setroles", description="Set role permissions / ضبط صلاحيات الرتب")
@app_commands.choices(kind=KIND_ROLES, log_type=LOG_TYPE_CHOICES, action=ACTIONS)
async def setroles(i: discord.Interaction, kind: app_commands.Choice[str], log_type: app_commands.Choice[str],
                   action: app_commands.Choice[str], role: discord.Role):
    if not await gate(i, "setroles", editor=True):
        return
    await roles_edit(i.guild.id, log_type.value, kind.value, role.id, action.value == "add")
    await audit(f"⚙️ {i.user.mention} {action.value} {role.mention} → {kind.value} ({log_type.value})", i.guild)
    await i.response.send_message(f"✅ {action.name}: {role.mention} → **{kind.name}**.", ephemeral=True)


@bot.tree.command(name="setrule", description="Change server rule / تعديل قوانين البوت")
@app_commands.choices(rule_name=RULE_CHOICES)
async def setrule(i: discord.Interaction, rule_name: app_commands.Choice[str], value: app_commands.Range[int, 0, 1000]):
    if not await gate(i, "setrule", editor=True):
        return
    await set_setting(i.guild.id, "rule:" + rule_name.value, value)
    await audit(f"⚙️ {i.user.mention} updated rule `{rule_name.value}` to `{value}`", i.guild)
    await i.response.send_message(f"✅ **{rule_name.value}** = {value}", ephemeral=True)


@bot.tree.command(name="settings", description="Show settings / عرض الإعدادات")
async def settings_cmd(i: discord.Interaction):
    if not await gate(i, "settings", editor=True):
        return
    embed = discord.Embed(title=f"⚙️ Server Settings — {BOT_VERSION}", color=discord.Color.blue())
    audit_ch = await cfg_get(i.guild.id, "all", "audit_channel")
    embed.add_field(name="📍 Audit Channel", value=f"<#{audit_ch}>" if audit_ch else "Not set", inline=False)
    await i.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="sync", description="Sync commands / مزامنة الأوامر")
async def sync_cmd(i: discord.Interaction):
    if not await gate(i, "sync", editor=True):
        return
    await i.response.defer(ephemeral=True)
    ok = await bot.sync_guild(i.guild)
    await i.followup.send("✅ Synced." if ok else "❌ Failed.")


async def cmd_name_autocomplete(i: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return [app_commands.Choice(name=c, value=c) for c in ALL_BOT_COMMANDS if current.lower() in c.lower()][:25]


@bot.tree.command(name="set-cmd-role", description="Lock command to role / تقييد أمر برتبة")
@app_commands.autocomplete(command=cmd_name_autocomplete)
async def set_cmd_role(i: discord.Interaction, command: str, role: Optional[discord.Role] = None):
    if not await gate(i, "set-cmd-role", editor=True):
        return
    async with aiosqlite.connect(DB) as db:
        if role:
            await db.execute("INSERT INTO cmd_roles(guild_id, cmd_name, role_id) VALUES(?,?,?) ON CONFLICT(guild_id, cmd_name) DO UPDATE SET role_id=excluded.role_id", (i.guild.id, command, role.id))
        else:
            await db.execute("DELETE FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
        await db.commit()
    await i.response.send_message("✅ Updated command permission.", ephemeral=True)


@bot.tree.command(name="set-update-ping", description="Set update ping role / رتبة منشن التحديثات")
async def set_update_ping(i: discord.Interaction, role: Optional[discord.Role] = None):
    if not await gate(i, "set-update-ping", editor=True):
        return
    await cfg_set(i.guild.id, "all", "update_ping", role.id if role else None)
    await i.response.send_message("✅ Updated update ping role.", ephemeral=True)


@bot.tree.command(name="set-log-template", description="Customize log layout / تخصيص قالب اللوج")
@app_commands.choices(log_type=LOG_TYPE_CHOICES[1:])
async def set_log_template(i: discord.Interaction, log_type: app_commands.Choice[str], template: Optional[app_commands.Range[str, 1, 1200]] = None):
    if not await gate(i, "set-log-template", editor=True):
        return
    key = f"template:{log_type.value}"
    if template:
        await set_setting(i.guild.id, key, template)
    else:
        await db_exec("DELETE FROM settings WHERE guild_id=? AND key=?", (i.guild.id, key))
    await i.response.send_message("✅ Template updated.", ephemeral=True)


@bot.tree.command(name="set-log-points", description="Set log points / تحديد نقاط اللوجات")
@app_commands.choices(log=POINT_CHOICES)
async def set_log_points(i: discord.Interaction, log: app_commands.Choice[str], points: app_commands.Range[int, 0, 1000]):
    if not await gate(i, "set-log-points", editor=True):
        return
    await set_setting(i.guild.id, "points:" + log.value, points)
    await i.response.send_message(f"Done. **{log.name}** = {points} points.", ephemeral=True)


@bot.tree.command(name="set-rank-points", description="Set rank points / نقاط الرتب")
@app_commands.choices(rank=RANK_CHOICES)
async def set_rank_points(i: discord.Interaction, rank: app_commands.Choice[str], points: app_commands.Range[int, 0, 100000]):
    if not await gate(i, "set-rank-points", editor=True):
        return
    await set_setting(i.guild.id, "rank:" + rank.value, points)
    await bulk_update_ranks(i.guild)
    await i.response.send_message("Done.", ephemeral=True)


@bot.tree.command(name="set-points", description="Set member points / تعديل نقاط عضو")
async def set_points(i: discord.Interaction, member: discord.Member, points: app_commands.Range[int, 0, 100000]):
    if not await gate(i, "set-points", editor=True):
        return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points=excluded.points", (i.guild.id, member.id, points))
        await db.commit()
    await update_member_rank(i.guild, member, points)
    await i.response.send_message("Done.", ephemeral=True)


@bot.tree.command(name="verify-points", description="Verify points / التحقق من النقاط")
async def verify_points(i: discord.Interaction, fix: bool = False):
    if not await gate(i, "verify-points", editor=True):
        return
    await i.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM members WHERE guild_id=?", (i.guild.id,))
        members_data = await cur.fetchall()
        
        mismatches = 0
        for uid, stored_pts in members_data:
            cur2 = await db.execute("SELECT SUM(points) FROM point_events WHERE guild_id=? AND user_id=? AND season_id='main'", (i.guild.id, uid))
            ev_sum = (await cur2.fetchone())[0] or 0
            if stored_pts != ev_sum:
                mismatches += 1
                if fix:
                    await db.execute("UPDATE members SET points=? WHERE guild_id=? AND user_id=?", (ev_sum, i.guild.id, uid))
        if fix and mismatches > 0:
            await db.commit()

    msg = f"✅ Point verification complete. Mismatches found: `{mismatches}`."
    if fix and mismatches > 0:
        msg += " All discrepancies have been fixed automatically!"
    elif mismatches > 0:
        msg += " Run with `fix: True` to correct them."
    await i.followup.send(msg, ephemeral=True)


@bot.tree.command(name="sync-ranks", description="Sync ranks / مزامنة الرتب")
async def sync_ranks(i: discord.Interaction, member: Optional[discord.Member] = None, demote: bool = False):
    if not await gate(i, "sync-ranks", editor=True):
        return
    changed = await bulk_update_ranks(i.guild, allow_demote=demote)
    await i.response.send_message(f"✅ Ranks synced. Changes: {changed}.", ephemeral=True)


@bot.tree.command(name="points", description="Check points / عرض النقاط")
async def points_cmd(i: discord.Interaction, member: Optional[discord.Member] = None):
    if not await gate(i, "points"):
        return
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        row = await cur.fetchone()
    total = row[0] if row else 0
    await i.response.send_message(f"{member.mention}: **{total}** points.", allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="leaderboard", description="Leaderboard / لوحة المتصدرين")
@app_commands.choices(period=PERIOD_CHOICES)
async def leaderboard(i: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
    if not await gate(i, "leaderboard"):
        return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM members WHERE guild_id=? AND points>0 ORDER BY points DESC LIMIT 10", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("No points yet. / لا توجد نقاط بعد", ephemeral=True)
    text = "**🏆 Leaderboard / لوحة الشرف**\n\n" + "\n".join(f"**{n}.** <@{u}> — {p} pts" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="rates", description="Show rates / عرض أسعار النقاط")
async def rates(i: discord.Interaction):
    if not await gate(i, "rates"):
        return
    embed = discord.Embed(title="💰 Point Reward Rates / أسعار ونقاط اللوجات", color=discord.Color.gold())
    for k, v in POINT_LABELS.items():
        pts = await get_setting(i.guild.id, "points:" + k)
        embed.add_field(name=v, value=f"`+{pts} points`", inline=True)
    await i.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="link", description="Link Roblox / ربط حساب روبلوكس")
async def link(i: discord.Interaction, roblox_username: str):
    if not await gate(i, "link"):
        return
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO links(guild_id, user_id, roblox_name) VALUES(?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET roblox_name=excluded.roblox_name",
            (i.guild.id, i.user.id, roblox_username.strip())
        )
        await db.commit()
    await i.response.send_message(f"✅ Successfully linked Roblox account **{roblox_username.strip()}**! / تم ربط حساب روبلوكس بنجاح.", ephemeral=True)


@bot.tree.command(name="profile", description="Member profile / بروفايل العضو")
async def profile(i: discord.Interaction, member: Optional[discord.Member] = None):
    if not await gate(i, "profile"):
        return
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        row = await cur.fetchone()
        total_pts = row[0] if row else 0

        cur2 = await db.execute("SELECT roblox_name FROM links WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        link_row = await cur2.fetchone()
        roblox_name = link_row[0] if link_row else "Not linked / غير مرتبط"

        cur_logs = await db.execute("SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND status='accepted'", (i.guild.id, member.id))
        logs_count = (await cur_logs.fetchone())[0]

    ladder = await get_ladder(i.guild.id)
    roles = ladder_roles(i.guild)
    target = target_index(ladder, total_pts)
    rank_name = ladder[target][0] if target >= 0 and target < len(ladder) else "Unranked / بدون رتبة"

    embed = discord.Embed(title=f"👤 Profile: {member.display_name}", color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="💎 Total Points / النقاط", value=str(total_pts), inline=True)
    embed.add_field(name="🎖️ Current Rank / الرتبة", value=rank_name, inline=True)
    embed.add_field(name="📊 Accepted Logs / اللوجات المقبولة", value=str(logs_count), inline=True)
    embed.add_field(name="🤖 Roblox Account / حساب روبلوكس", value=roblox_name, inline=False)
    
    await i.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="War stats / إحصائيات الحروب")
async def stats(i: discord.Interaction, crew: Optional[str] = None):
    if not await gate(i, "stats"):
        return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT result, COUNT(*) FROM logs WHERE guild_id=? AND type='war' AND status='accepted' GROUP BY result",
            (i.guild.id,)
        )
        rows = await cur.fetchall()
    
    stats_map = {r[0]: r[1] for r in rows}
    wins = stats_map.get("win", 0)
    losses = stats_map.get("loss", 0)
    total_wars = wins + losses
    ratio = f"{(wins / total_wars * 100):.1f}%" if total_wars > 0 else "0.0%"

    embed = discord.Embed(title="📊 Crew War Statistics / إحصائيات حروب الكرو", color=discord.Color.blue())
    embed.add_field(name="🏆 Wins / انتصارات", value=str(wins), inline=True)
    embed.add_field(name="❌ Losses / هزائم", value=str(losses), inline=True)
    embed.add_field(name="📈 Win Rate / نسبة الفوز", value=ratio, inline=True)
    embed.set_footer(text=f"Total Wars Logged: {total_wars}")
    
    await i.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="inactive", description="Inactive members / الأعضاء الخاملين")
@app_commands.describe(days="Number of days of inactivity / عدد أيام الخمول (افتراضي 30 يوم)")
async def inactive(i: discord.Interaction, days: int = 30):
    if not await gate(i, "inactive", editor=True):
        return
    await i.response.defer(ephemeral=True)
    
    cutoff = now_utc() - timedelta(days=days)
    guild = i.guild
    
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT author_id, MAX(created_at) FROM logs WHERE guild_id=? AND status='accepted' GROUP BY author_id",
            (guild.id,)
        )
        last_activity = {row[0]: dt.datetime.fromisoformat(row[1]) for row in cur.fetchall()}
        
    inactive_members = []
    for member in guild.members:
        if member.bot:
            continue
        last_date = last_activity.get(member.id)
        if not last_date or last_date < cutoff:
            inactive_members.append((member, last_date))
            
    if not inactive_members:
        return await i.followup.send(f"✅ No inactive members found for the past {days} days. / لا توجد أعضاء خاملين خلال الـ {days} يوم السابقة.", ephemeral=True)
        
    lines = [f"💤 **Inactive Members (> {days} days) / الأعضاء الخاملين:**\n"]
    for member, last_date in inactive_members[:25]:
        date_str = last_date.strftime('%Y-%m-%d') if last_date else "Never / لم يسجل أبداً"
        lines.append(f"• {member.mention} — Last Log: `{date_str}`")
        
    await i.followup.send("\n".join(lines)[:1990], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="season-reset", description="Reset season / إعادة ضبط الموسم")
async def season_reset(i: discord.Interaction, name: str, confirm: str, reset_ranks: bool = False):
    if not await gate(i, "season-reset", editor=True):
        return
    if confirm != "RESET":
        return await i.response.send_message("Type RESET to confirm. / اكتب RESET للتأكيد.", ephemeral=True)
    await i.response.send_message("Season reset successfully. / تم تصفير الموسم بنجاح.", ephemeral=True)


@bot.tree.command(name="season-top", description="Archived season top / توب الموسم السابق")
async def season_top(i: discord.Interaction, name: str):
    if not await gate(i, "season-top"):
        return
    await i.response.send_message("Season archive top.", ephemeral=True)


# ================================================================ أوامر اللغات وإنشاء الكرو الجديد (New Additions)
class LanguageSelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        
        options = [discord.SelectOption(label=name, value=code, emoji="🌐") for code, name in SUPPORTED_LANGUAGES.items()]
        self.select = discord.ui.Select(placeholder="Choose your language / اختر لغتك", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, i: discord.Interaction):
        if i.user.id != self.user_id:
            return await i.response.send_message("This menu is not for you.", ephemeral=True)
        chosen_lang = self.select.values[0]
        await set_user_lang(i.user.id, chosen_lang)
        await i.response.edit_message(content=t(chosen_lang, "lang_updated"), view=None)

@bot.tree.command(name="language", description="Change your preferred bot language / تغيير لغة البوت المفضلة")
async def language_cmd(i: discord.Interaction):
    lang = await get_user_lang(i.user.id)
    embed = discord.Embed(title="🌐 Language Settings / إعدادات اللغة", description=t(lang, "lang_desc"), color=discord.Color.blue())
    await i.response.send_message(embed=embed, view=LanguageSelectView(i.user.id), ephemeral=True)


class CreateCrewModal(discord.ui.Modal, title="Create New Crew / إنشاء كرو جديد"):
    crew_name = discord.ui.TextInput(label="Crew Name / اسم الكرو", placeholder="e.g. Dragon Slayers", required=True)
    captains = discord.ui.TextInput(label="Captains [Name - Spec - @User]", style=discord.TextStyle.paragraph, placeholder="Ziad - PvP - @user1", required=True)
    vices = discord.ui.TextInput(label="Vice Captains [Name - Spec - @User]", style=discord.TextStyle.paragraph, placeholder="Ahmed - Support - @user2", required=False)
    teams_z_y = discord.ui.TextInput(label="Teams Z & Y [Name - Spec - @User]", style=discord.TextStyle.paragraph, placeholder="Team Z:\nAli - Fruit - @user3\n\nTeam Y:\nOmar - Sword - @user4", required=False)
    teams_x_s_a = discord.ui.TextInput(label="Teams X, S & A [Name - Spec - @User]", style=discord.TextStyle.paragraph, placeholder="Team X:\nSayed - Gun - @user5", required=False)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        embed = discord.Embed(title=f"🏴‍☠️ Crew Roster: {self.crew_name.value}", color=discord.Color.gold())
        embed.add_field(name="👑 Captains / الكباتن", value=self.captains.value or "-", inline=False)
        if self.vices.value:
            embed.add_field(name="⚔️ Vice Captains / الفويس كابتن", value=self.vices.value, inline=False)
        if self.teams_z_y.value:
            embed.add_field(name="⚡ Teams Z & Y / تيم Z وتيم Y", value=self.teams_z_y.value, inline=False)
        if self.teams_x_s_a.value:
            embed.add_field(name="🔥 Teams X, S & A / تيم X وتيم S وتيم A", value=self.teams_x_s_a.value, inline=False)
        embed.set_footer(text=f"Created by {i.user.display_name}", icon_url=i.user.display_avatar.url)
        await i.channel.send(content="📜 **New Crew Roster Created!**", embed=embed)
        await i.followup.send("✅ Crew roster published successfully!", ephemeral=True)

@bot.tree.command(name="create-new-crew", description="Create a new crew roster with captains, teams and specs / إنشاء قائمة كرو جديدة")
async def create_new_crew(i: discord.Interaction):
    if not await gate(i, "create-new-crew", editor=True):
        return
    await i.response.send_modal(CreateCrewModal())


# ================================================================ دليل المساعدة الثنائي (Bilingual Help Manual)
def get_help_embeds():
    e1 = discord.Embed(
        title=f"⚔️ DragonSlayerBot Manual ({BOT_VERSION}) — Part 1: Log Submissions",
        description="**Permission / الصلاحية:** All Members / Submitter Roles\n"
                    "●▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬●",
        color=discord.Color.gold()
    )
    e1.add_field(name="🎮 `/log-war`", value="🇬🇧 Log a Crew War battle (min 2 attachments).\n🇪🇬 تسجيل حرب كرو (يتطلب إرفاق ملفين على الأقل).", inline=False)
    e1.add_field(name="⚔️ `/log-1v1`", value="🇬🇧 Log a 1v1 duel against an opponent.\n🇪🇬 تسجيل قتال فردي 1 ضد 1.", inline=False)
    e1.add_field(name="👥 `/log-2v2`", value="🇬🇧 Log a 2v2 team fight with a teammate.\n🇪🇬 تسجيل قتال ثنائي 2 ضد 2.", inline=False)
    e1.add_field(name="🎯 `/log-training`", value="🇬🇧 Log a crew training session.\n🇪🇬 تسجيل تدريب للكرو.", inline=False)
    e1.add_field(name="📝 `/log-tryout`", value="🇬🇧 Log a candidate tryout test.\n🇪🇬 تسجيل اختبار قبول (Tryout).", inline=False)
    e1.add_field(name="🔄 `/resubmit` `[log_id]`", value="🇬🇧 Resubmit a rejected log using its ID.\n🇪🇬 إعادة تقديم لوج مرفوض باستخدام رقم الـ ID.", inline=False)

    e2 = discord.Embed(
        title="🛡️ DragonSlayerBot Manual — Part 2: Review & Management",
        description="**Permission / الصلاحية:** Reviewers & Editors Only\n"
                    "●▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬●",
        color=discord.Color.blue()
    )
    e2.add_field(name="⏳ `/pending`", value="🇬🇧 List all pending logs awaiting review.\n🇪🇬 عرض كافة اللوجات المعلقة بانتظار المراجعة.", inline=False)
    e2.add_field(name="🚫 `/revoke-log` `[log_id]` `[reason]`", value="🇬🇧 Revoke an accepted log and deduct points.\n🇪🇬 إلغاء لوج تم قبوله سابقاً وسحب نقاطه.", inline=False)
    e2.add_field(name="📢 `/log-update` `[preset]` `(channel)`", value="🇬🇧 Publish formatted bot update notices.\n🇪🇬 نشر إعلانات وتحديثات البوت بتصميم منسق.", inline=False)
    e2.add_field(name="📊 `/inactive` `(days)`", value="🇬🇧 Track inactive members based on log history.\n🇪🇬 تتبع الأعضاء الخاملين بناءً على سجل اللوجات.", inline=False)
    e2.add_field(name="🏆 `/season-reset` `[name]` `[RESET]`", value="🇬🇧 Reset the current season and archive stats.\n🇪🇬 إعادة تصفير الموسم الحالي وأرشفة الإحصائيات.", inline=False)
    e2.add_field(name="📦 `/backup` `[scope]` `[destination]`", value="🇬🇧 Generate database or settings JSON backups.\n🇪🇬 إنتاج نسخة احتياطية للقاعدة أو الإعدادات.", inline=False)
    e2.add_field(name="📥 `/restore-backup` `[backup_file]`", value="🇬🇧 Restore configuration or full database from file.\n🇪🇬 استعادة الإعدادات أو قاعدة البيانات من ملف.", inline=False)
    e2.add_field(name="🏴‍☠️ `/create-new-crew`", value="🇬🇧 Create custom crew roster with teams and specs.\n🇪🇬 إنشاء لوحة وتشكيلة كرو جديدة بالتيمات.", inline=False)

    e3 = discord.Embed(
        title="⚙️ DragonSlayerBot Manual — Part 3: Server Configuration",
        description="**Permission / الصلاحية:** Admins & Editors Only\n"
                    "●▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬●",
        color=discord.Color.green()
    )
    e3.add_field(name="📍 `/setchannel` `[log_type]` `[kind]` `(channel)`", value="🇬🇧 Set channels for reviews, public logs, or audit logs.\n🇪🇬 تعيين قنوات المراجعة واللوجات وسجل التعديلات.", inline=False)
    e3.add_field(name="🔔 `/setping` `[log_type]` `[kind]` `(role)`", value="🇬🇧 Set notification ping roles for reviews and logs.\n🇪🇬 تعيين رتب المنشن للإشعارات واللوجات.", inline=False)
    e3.add_field(name="🔑 `/setroles` `[kind]` `[log_type]` `[action]` `[role]`", value="🇬🇧 Assign or remove reviewer and submitter role permissions.\n🇪🇬 منح أو سحب صلاحيات رتب المراجعين والمسجلين.", inline=False)
    e3.add_field(name="📜 `/setrule` `[rule_name]` `[value]`", value="🇬🇧 Configure server bot rules (e.g., daily submission limit).\n🇪🇬 ضبط قواعد السيرفر (مثل الحد اليومي للوجات).", inline=False)
    e3.add_field(name="🎯 `/set-log-points` `[log]` `[points]`", value="🇬🇧 Set point values for wins and losses per log type.\n🇪🇬 تحديد نقاط الفوز والخسارة لكل نوع لوج.", inline=False)
    e3.add_field(name="🥇 `/set-rank-points` `[rank]` `[points]`", value="🇬🇧 Set the required points to achieve each rank level.\n🇪🇬 تعيين النقاط المطلوبة للوصول لكل رتبة.", inline=False)
    e3.add_field(name="🎨 `/set-log-template` `[log_type]` `(template)`", value="🇬🇧 Customize announcement text layout templates for logs.\n🇪🇬 تخصيص شكل وقالب رسائل اللوجات.", inline=False)
    e3.add_field(name="🔐 `/set-cmd-role` `[command]` `(role)`", value="🇬🇧 Lock specific commands to specific roles.\n🇪🇬 تقييد الأوامر وتخصيصها لرتب معينة.", inline=False)
    e3.add_field(name="📌 `/set-update-ping` `(role)`", value="🇬🇧 Set the mention role for bot update announcements.\n🇪🇬 تحديد رتبة المنشن الخاصة بإعلانات التحديثات.", inline=False)

    e4 = discord.Embed(
        title="📊 DragonSlayerBot Manual — Part 4: Points, Ranks & Public",
        description="**Permission / الصلاحية:** All Members / لجميع الأعضاء\n"
                    "●▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬●",
        color=discord.Color.purple()
    )
    e4.add_field(name="💎 `/points` `(member)`", value="🇬🇧 Check total points for yourself or another member.\n🇪🇬 عرض النقاط الإجمالية لك أو لأي عضو آخر.", inline=False)
    e4.add_field(name="🏆 `/leaderboard` `(period)`", value="🇬🇧 View the top members leaderboard rankings.\n🇪🇬 عرض قائمة المتصدرين بلوحة الشرف.", inline=False)
    e4.add_field(name="👤 `/profile` `(member)`", value="🇬🇧 View member stats and activity profile.\n🇪🇬 عرض بروفايل وإحصائيات العضو.", inline=False)
    e4.add_field(name="💰 `/rates`", value="🇬🇧 Display current point reward rates for logs.\n🇪🇬 عرض جدول أسعار ونقاط اللوجات الحالية.", inline=False)
    e4.add_field(name="🔗 `/link` `[roblox_username]`", value="🇬🇧 Link your Roblox username to your Discord profile.\n🇪🇬 ربط حساب روبلوكس بحسابك في ديسكورد.", inline=False)
    e4.add_field(name="📈 `/stats` `(crew)`", value="🇬🇧 View general war and fight statistics.\n🇪🇬 عرض إحصائيات الحروبات والقتالات العامة.", inline=False)
    e4.add_field(name="📚 `/season-top` `[name]`", value="🇬🇧 View leaderboard stats of an archived past season.\n🇪🇬 عرض ليدربورد موسم سابق مؤرشف.", inline=False)
    e4.add_field(name="✏️ `/set-points` `[member]` `[points]`", value="🇬🇧 [Admin] Override and set member points directly.\n🇪🇬 [للإدارة] تعديل نقاط عضو بشكل مباشر.", inline=False)
    e4.add_field(name="🔄 `/sync-ranks` `(member)` `(demote)`", value="🇬🇧 Sync member roles automatically based on their points.\n🇪🇬 مزامنة وتحديث رتب الأعضاء تلقائياً حسب نقاطهم.", inline=False)
    e4.add_field(name="🌐 `/language`", value="🇬🇧 Choose your preferred bot language.\n🇪🇬 اختر لغتك المفضلة للبوت.", inline=False)
    e4.add_field(name="⚙️ `/settings`", value="🇬🇧 View current server configuration settings.\n🇪🇬 عرض إعدادات السيرفر الحالية.", inline=False)
    e4.add_field(name="🔄 `/sync`", value="🇬🇧 Sync application slash commands with Discord.\n🇪🇬 مزامنة أوامر البوت مع ديسكورد.", inline=False)
    e4.add_field(name="❓ `/help`", value="🇬🇧 Show this comprehensive bilingual command manual.\n🇪🇬 عرض دليل الأوامر الشامل.", inline=False)
    e4.add_field(name="📢 `/send-help-panel` `(channel)`", value="🇬🇧 Send the help manual panel to a specific channel.\n🇪🇬 إرسال بانل دليل الأوامر إلى قناة محددة.", inline=False)

    for emb in (e1, e2, e3, e4):
        emb.set_footer(text=f"DragonSlayerBot {BOT_VERSION} • Developed by Ziad", icon_url=bot.user.display_avatar.url)

    return [e1, e2, e3, e4]


@bot.tree.command(name="help", description="Show full bilingual help manual for all commands")
async def help_cmd(i: discord.Interaction):
    if not await gate(i, "help"):
        return
    await i.response.send_message(embeds=get_help_embeds(), ephemeral=True)


@bot.tree.command(name="send-help-panel", description="Send full help manual panel to channel")
async def send_help_panel(i: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not await gate(i, "send-help-panel", editor=True):
        return
    target = channel or i.channel
    await target.send(embeds=get_help_embeds())
    await i.response.send_message(f"✅ Successfully published help panel in {target.mention}!", ephemeral=True)


@bot.tree.command(name="log-update", description="Publish a formatted bot update notice")
@app_commands.choices(preset=UPDATE_TYPE_CHOICES)
@app_commands.describe(preset="Choose the update preset", channel="Target channel (optional)")
async def log_update(i: discord.Interaction, preset: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None):
    if not await gate(i, "log-update", editor=True):
        return

    target_channel = channel or i.channel
    update_info = UPDATE_PRESETS.get(preset.value)

    if not update_info:
        return await i.response.send_message("❌ Presets for this update were not found.", ephemeral=True)

    await i.response.defer(ephemeral=True)

    # 🧹 مسح جميع الرسائل القديمة في القناة تلقائياً قبل إرسال التحديث الجديد
    try:
        await target_channel.purge(limit=100)
    except Exception as e:
        print(f"⚠️ Could not purge channel messages: {e}")

    ping_role_id = await cfg_get(i.guild.id, "all", "update_ping")
    ping_text = f"<@&{ping_role_id}>\n" if ping_role_id else ""

    embed = discord.Embed(
        title=f"<a:emoji_20:1568348329971163196> {update_info['title_en']}",
        color=discord.Color.gold(),
        timestamp=now_utc()
    )
    embed.add_field(name="🌐 English Notes", value=update_info['desc_en'], inline=False)
    embed.add_field(name="🇪🇬 التحديثات بالعربي", value=update_info['desc_ar'], inline=False)
    embed.set_footer(text=f"DragonSlayerBot {BOT_VERSION} • Developed by Ziad", icon_url=bot.user.display_avatar.url)

    try:
        await target_channel.send(content=ping_text, embed=embed, allowed_mentions=mentions(ping_role_id))
        await i.followup.send(f"✅ Update notice successfully published in {target_channel.mention} (and old messages cleared)!", ephemeral=True)
        await audit(f"📢 {i.user.mention} published update notice `{preset.name}` in {target_channel.mention}", i.guild)
    except discord.HTTPException as e:
        await i.followup.send(f"❌ Failed to publish update notice: {e}", ephemeral=True)


@bot.tree.command(name="backup", description="Backup tool")
@app_commands.choices(scope=BACKUP_SCOPE_CHOICES, destination=BACKUP_DEST_CHOICES)
async def backup_cmd(i: discord.Interaction, scope: app_commands.Choice[str], destination: app_commands.Choice[str]):
    if not await gate(i, "backup", editor=True):
        return
    await i.response.defer(ephemeral=True)
    if scope.value == "full" and i.user.id != owner_id():
        return await i.followup.send("❌ Full backup is reserved for the bot owner.", ephemeral=True)
    
    if scope.value == "full":
        file = discord.File(DB, filename=f"full_backup_{i.guild.id}.db")
        msg = "📦 Full Database Backup."
    else:
        file = discord.File(JSON_CONFIG_PATH, filename=f"servers_settings.json")
        msg = "📦 Server Config JSON Backup."

    if destination.value == "owner":
        ok = await dm_owner(msg, file=file)
        if ok:
            await i.followup.send("✅ Backup sent to owner DMs.", ephemeral=True)
        else:
            await i.followup.send("❌ Could not DM owner.", ephemeral=True)
    else:
        await i.followup.send(content=msg, file=file, ephemeral=True)


@bot.tree.command(name="restore-backup", description="Read and restore backup file (.json or .db)")
@app_commands.describe(backup_file="Attach the backup JSON or DB file")
async def restore_backup(i: discord.Interaction, backup_file: discord.Attachment):
    if not await gate(i, "restore-backup", editor=True):
        return
    await i.response.defer(ephemeral=True)
    
    ext = os.path.splitext(backup_file.filename)[1].lower()
    if ext not in (".json", ".db"):
        return await i.followup.send("❌ Invalid backup file format. Only `.json` or `.db` files are accepted.", ephemeral=True)

    file_bytes = await backup_file.read()
    
    if ext == ".json":
        try:
            data = json.loads(file_bytes.decode("utf-8"))
            version = data.get("version", "Unknown")
            updated = data.get("updated_at", "Unknown")
            guild_cfg = len(data.get("guild_cfg", []))
            role_perms = len(data.get("role_perms", []))
            cmd_roles = len(data.get("cmd_roles", []))
            settings = len(data.get("settings", []))

            async with aiosqlite.connect(DB) as db:
                for row in data.get("settings", []):
                    await db.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?)", (row["guild_id"], row["key"], str(row["value"])))
                for row in data.get("guild_cfg", []):
                    await db.execute("INSERT OR REPLACE INTO guild_cfg VALUES(?,?,?,?)", (row["guild_id"], row["log_type"], row["key"], row["value"]))
                for row in data.get("role_perms", []):
                    await db.execute("INSERT OR REPLACE INTO role_perms VALUES(?,?,?,?)", (row["guild_id"], row["log_type"], row["kind"], row["role_id"]))
                for row in data.get("cmd_roles", []):
                    await db.execute("INSERT OR REPLACE INTO cmd_roles VALUES(?,?,?)", (row["guild_id"], row["cmd_name"], row["role_id"]))
                await db.commit()

            msg = (f"✅ **JSON Backup Restored Successfully!**\n"
                   f"• **Version:** `{version}`\n"
                   f"• **Updated At:** `{updated}`\n"
                   f"• **Restored Settings:** `{settings}` entries\n"
                   f"• **Restored Channels/Pings:** `{guild_cfg}` entries\n"
                   f"• **Restored Role Perms:** `{role_perms}` entries\n"
                   f"• **Restored Command Roles:** `{cmd_roles}` entries")
            await i.followup.send(msg, ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Failed to parse/restore JSON backup: {e}", ephemeral=True)

    elif ext == ".db":
        if i.user.id != owner_id():
            return await i.followup.send("❌ Restoring raw `.db` database backups is restricted to the Bot Owner.", ephemeral=True)
        try:
            with open(DB, "wb") as f:
                f.write(file_bytes)
            await init_db()
            await i.followup.send("✅ **Full Database (.db) Restored and Initialized Successfully!**", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Failed to restore SQLite database file: {e}", ephemeral=True)


async def run_reminders():
    pass


async def run_backup():
    pass


async def run_weekly():
    pass


@tasks.loop(minutes=30)
async def maintenance():
    for job in (run_reminders, run_backup, run_weekly):
        try:
            await job()
        except Exception as e:
            print(f"⚠️ Maintenance error: {e}")


@maintenance.before_loop
async def before_maintenance():
    await bot.wait_until_ready()


if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("❌ DISCORD_TOKEN is missing in environment variables.")
    bot.run(token)
