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
from typing import Optional, List

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import config

load_dotenv()

DB = os.getenv("DB_PATH", "crewbot.db")
JSON_CONFIG_PATH = os.getenv("JSON_CONFIG_PATH", "bot_config_snapshot.json")
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

BOT_VERSION = "v2.8.1"

# إعدادات الأمان للملفات المرفقة
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".mkv"}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB حد أقصى للملف الواحد
MAX_TOTAL_FILES_SIZE = 50 * 1024 * 1024  # 50 MB إجمالي الملفات في الطلب الواحد
MAX_FILES_COUNT = 5


# --- سيرفر ويب مصغر لـ Railway ---
def run_dummy_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"DragonSlayerBot v2.8.1 Production-Ready is online!")

        def log_message(self, format, *args):
            pass

    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


threading.Thread(target=run_dummy_server, daemon=True).start()

RULE_DEFAULTS = {
    "daily_limit": 5,
    "reminder_hours": 48,
    "backup_hours": 24,
    "weekly_post": 1,
    "war_participants": 1,
    "tryout_autorole": 1,
}
RULE_DEFAULTS.update(getattr(config, "RULE_DEFAULTS", {}))
RULE_LABELS = {
    "daily_limit": "Max logs per member per 24h (0 = unlimited)",
    "reminder_hours": "Remind reviewers after N hours (0 = off)",
    "backup_hours": "Auto backup every N hours (0 = off)",
    "weekly_post": "Weekly leaderboard post (1 = on, 0 = off)",
    "war_participants": "War points to all listed players (1 = on, 0 = off)",
    "tryout_autorole": "Give Crew role on passed tryout (1 = on, 0 = off)",
}
CREW_ROLE_KEYWORD = getattr(config, "CREW_ROLE_KEYWORD", "Crew")

RESULT_LABEL = {"win": "✅ Win", "loss": "❌ Loss"}
TRYOUT_LABEL = {"win": "✅ Passed", "loss": "❌ Failed"}
STATUS_LABEL = {"pending": "⏳ Pending", "accepted": "✅ Accepted",
                "rejected": "❌ Rejected", "revoked": "🚫 Revoked"}
RESULT_CHOICES = [
    app_commands.Choice(name="Win", value="win"),
    app_commands.Choice(name="Loss", value="loss"),
]
TRYOUT_CHOICES = [
    app_commands.Choice(name="Passed", value="win"),
    app_commands.Choice(name="Failed", value="loss"),
]
LOG_TYPE_CHOICES = [app_commands.Choice(name="All types (default)", value="all")] + [
    app_commands.Choice(name=v["label"], value=k) for k, v in config.LOG_TYPES.items()
]
KIND_CHANNEL = [
    app_commands.Choice(name="Review channel (قناة المراجعة)", value="review_channel"),
    app_commands.Choice(name="Log channel (قناة اللوج العامة)", value="log_channel"),
    app_commands.Choice(name="Audit channel (سجل التعديلات)", value="audit_channel"),
]
KIND_PING = [
    app_commands.Choice(name="Review ping (منشن في رسالة المراجعة)", value="review_ping"),
    app_commands.Choice(name="Log ping (منشن في اللوج العام)", value="log_ping"),
]
KIND_ROLES = [
    app_commands.Choice(name="Reviewers (مين يقبل/يرفض)", value="review"),
    app_commands.Choice(name="Submitters (مين يقدر يسجّل لوج)", value="submit"),
]
ACTIONS = [
    app_commands.Choice(name="Add (إضافة)", value="add"),
    app_commands.Choice(name="Remove (إزالة)", value="remove"),
]
PERIOD_CHOICES = [
    app_commands.Choice(name="All time", value="all"),
    app_commands.Choice(name="This week", value="week"),
]
RULE_CHOICES = [app_commands.Choice(name=l[:100], value=k) for k, l in RULE_LABELS.items()]

BACKUP_SCOPE_CHOICES = [
    app_commands.Choice(name="Guild Activity & Today (نشاط وسجلات السيرفر اليوم)", value="today"),
    app_commands.Choice(name="Full Database [Owner Only] (قاعدة البيانات كاملة - البوت بأكمله)", value="full"),
]
BACKUP_DEST_CHOICES = [
    app_commands.Choice(name="Here in chat (هنا في الشات)", value="chat"),
    app_commands.Choice(name="Bot Owner DM (إرسال للخاص لصاحب البوت)", value="owner"),
]

UPDATE_PRESETS = {
    "bot_patch": {
        "label": f"🤖 Bot Production Patch {BOT_VERSION} / تحديث الأمان والاستقرار",
        "title_en": f"DragonSlayerBot Production Release {BOT_VERSION}",
        "title_ar": f"إصدار الإنتاج المؤمّن للبوت {BOT_VERSION}",
        "desc_en": "• Full Database backups strictly restricted to Bot Owner and DM.\n• Season ID tied permanently to log creation time.\n• Atomic transactions implemented across all critical database workflows.\n• Comprehensive command permission checks applied to 100% of bot commands.\n• Attachment security validation and strict limits enforced.",
        "desc_ar": "• قصر النسخ الاحتياطي الكامل على مالك البوت وللخاص حصرياً.\n• ربط معرف الموسم بثبات بوقت إنشاء اللوج منعاً لتداخل النقاط.\n• استخدام المعاملات الذرية (Transactions) لكافة العمليات الحساسة.\n• فرض قيود الصلاحيات على كافة الأوامر دون استثناء.\n• إضافة فحص أمني دقيق لحجم وصيغ المرفقات والملفات."
    }
}

UPDATE_TYPE_CHOICES = [
    app_commands.Choice(name=v["label"], value=k) for k, v in UPDATE_PRESETS.items()
]

ALL_BOT_COMMANDS = [
    "log-war", "log-1v1", "log-2v2", "log-training", "log-tryout", "resubmit",
    "log-update", "set-log-template", "points", "leaderboard", "profile",
    "stats", "rates", "link", "pending", "revoke-log", "inactive",
    "season-reset", "season-top", "backup", "setchannel", "setping",
    "setroles", "setrule", "set-log-points", "set-rank-points", "set-points",
    "settings", "sync", "set-cmd-role", "set-update-ping", "help", "send-help-panel"
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    guild_id INTEGER,
    key TEXT,
    value TEXT,
    PRIMARY KEY(guild_id, key)
);
CREATE TABLE IF NOT EXISTS members(
    guild_id INTEGER, 
    user_id INTEGER, 
    points INTEGER NOT NULL DEFAULT 0, 
    PRIMARY KEY(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS guild_channels(
    guild_id INTEGER PRIMARY KEY, 
    review_channel INTEGER, 
    log_channel INTEGER, 
    audit_channel INTEGER
);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    type TEXT NOT NULL, 
    author_id INTEGER NOT NULL, 
    result TEXT, 
    data TEXT, 
    flags TEXT,
    status TEXT DEFAULT 'pending', 
    points INTEGER DEFAULT 0,
    reviewer_id INTEGER, 
    reason TEXT, 
    created_at TEXT NOT NULL,
    review_channel INTEGER, 
    review_msg INTEGER,
    public_channel INTEGER, 
    public_msg INTEGER,
    reviewed_at TEXT,
    reminded INTEGER DEFAULT 0,
    season_id TEXT DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS hashes(
    hash TEXT, 
    log_id INTEGER, 
    PRIMARY KEY(hash, log_id)
);
CREATE TABLE IF NOT EXISTS guild_cfg(
    guild_id INTEGER, 
    log_type TEXT, 
    key TEXT, 
    value INTEGER,
    PRIMARY KEY(guild_id, log_type, key)
);
CREATE TABLE IF NOT EXISTS role_perms(
    guild_id INTEGER, 
    log_type TEXT, 
    kind TEXT, 
    role_id INTEGER,
    PRIMARY KEY(guild_id, log_type, kind, role_id)
);
CREATE TABLE IF NOT EXISTS point_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    log_id INTEGER, 
    user_id INTEGER NOT NULL, 
    points INTEGER NOT NULL, 
    created_at TEXT NOT NULL,
    type TEXT DEFAULT 'log',
    season_id TEXT DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS links(
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, 
    roblox_id INTEGER, 
    roblox_name TEXT,
    PRIMARY KEY(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS season_archive(
    season TEXT, 
    guild_id INTEGER, 
    user_id INTEGER, 
    points INTEGER, 
    PRIMARY KEY(season, guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS cmd_roles(
    guild_id INTEGER, 
    cmd_name TEXT, 
    role_id INTEGER, 
    PRIMARY KEY(guild_id, cmd_name)
);
"""

LOG_ID_RE = re.compile(r"Log #(\d+)")


async def save_config_snapshot_json():
    try:
        async with aiosqlite.connect(DB) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM guild_cfg")
            guild_cfg = [dict(r) for r in await cur.fetchall()]
            cur = await db.execute("SELECT * FROM role_perms")
            role_perms = [dict(r) for r in await cur.fetchall()]
            cur = await db.execute("SELECT * FROM cmd_roles")
            cmd_roles = [dict(r) for r in await cur.fetchall()]
            cur = await db.execute("SELECT guild_id, key, value FROM settings")
            settings = [{"guild_id": r["guild_id"], "key": r["key"], "value": r["value"]} for r in await cur.fetchall()]
            
        data = {
            "version": BOT_VERSION,
            "description": "Configuration snapshot only.",
            "guild_cfg": guild_cfg,
            "role_perms": role_perms,
            "cmd_roles": cmd_roles,
            "settings": settings,
            "updated_at": dt.datetime.now(timezone.utc).isoformat()
        }
        with open(JSON_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Failed to save config snapshot JSON: {e}")


class CrewBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._synced = False

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
        if not self._synced:
            self._synced = True
            for g in self.guilds:
                await self.sync_guild(g)
                await check_and_notify_auto_update(g)
        if not maintenance.is_running():
            maintenance.start()

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)


bot = CrewBot()


def now_utc():
    return dt.datetime.now(timezone.utc)


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute("PRAGMA busy_timeout=5000;")
        
        # 1. إنشاء الجداول الأساسية
        await db.executescript(SCHEMA)
        
        # 2. فحص وإضافة العمود الناقص تلقائياً للجداول القديمة لمنع الكراش
        cur = await db.execute("PRAGMA table_info(logs);")
        columns = [row[1] for row in await cur.fetchall()]
        if "guild_id" not in columns:
            await db.execute("ALTER TABLE logs ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0;")

        # 3. إنشاء الفهارس بأمان
        indexes_script = """
        CREATE INDEX IF NOT EXISTS idx_logs_guild_status ON logs(guild_id, status);
        CREATE INDEX IF NOT EXISTS idx_logs_author_created ON logs(guild_id, author_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_logs_season ON logs(guild_id, season_id);
        CREATE INDEX IF NOT EXISTS idx_point_events_guild_season ON point_events(guild_id, season_id);
        CREATE INDEX IF NOT EXISTS idx_point_events_user ON point_events(guild_id, user_id);
        CREATE INDEX IF NOT EXISTS idx_point_events_created ON point_events(guild_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_hashes_hash ON hashes(hash);
        CREATE INDEX IF NOT EXISTS idx_cmd_roles_guild ON cmd_roles(guild_id);
        """
        await db.executescript(indexes_script)
        await db.commit()
        
    await save_config_snapshot_json()


async def db_exec(sql, params=()):
    async with aiosqlite.connect(DB) as db:
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute(sql, params)
        await db.commit()


async def get_setting(guild_id: int, key: str):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
        row = await cur.fetchone()
    if row is not None and row[0] is not None:
        try:
            return int(row[0])
        except ValueError:
            return row[0]
            
    if key.startswith("points:"):
        k = key.replace("points:", "")
        return config.DEFAULT_POINTS.get(k, 0)
    elif key.startswith("rank:"):
        n = key.replace("rank:", "")
        for name, pts in config.LADDER:
            if name == n:
                return pts
        return 0
    elif key.startswith("rule:"):
        r = key.replace("rule:", "")
        return RULE_DEFAULTS.get(r, 0)
    return None


async def set_setting(guild_id: int, key: str, value):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO settings(guild_id, key, value) VALUES(?,?,?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (guild_id, str(key), str(value)))
        await db.commit()
    await save_config_snapshot_json()


async def rule(guild_id: int, name: str):
    v = await get_setting(guild_id, "rule:" + name)
    return v if v is not None else RULE_DEFAULTS.get(name, 0)


async def get_current_season(guild_id: int) -> str:
    s = await get_setting(guild_id, "state:current_season")
    return str(s) if s else "main"


async def cfg_get(guild_id, log_type, key):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT value FROM guild_cfg WHERE guild_id=? AND key=? AND log_type IN (?, 'all') "
            "ORDER BY (log_type='all') LIMIT 1", (guild_id, key, log_type))
        row = await cur.fetchone()
    if row is not None and row[0] is not None:
        return row[0]
    return None


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
                "SELECT role_id FROM role_perms WHERE guild_id=? AND log_type=? AND kind=?",
                (guild_id, t, kind))
            ids = [r[0] for r in await cur.fetchall()]
            if ids:
                return ids
    return []


async def roles_edit(guild_id, log_type, kind, role_id, add: bool):
    async with aiosqlite.connect(DB) as db:
        if add:
            await db.execute("INSERT OR IGNORE INTO role_perms VALUES(?,?,?,?)",
                             (guild_id, log_type, kind, role_id))
        else:
            await db.execute(
                "DELETE FROM role_perms WHERE guild_id=? AND log_type=? AND kind=? AND role_id=?",
                (guild_id, log_type, kind, role_id))
        await db.commit()
    await save_config_snapshot_json()


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
    required_role_id = row[0]
    return any(r.id == required_role_id for r in i.user.roles)


async def get_ladder(guild_id: int):
    res = []
    for n, default_pts in config.LADDER:
        pts = await get_setting(guild_id, "rank:" + n)
        res.append((n, int(pts) if pts is not None else default_pts))
    return res


async def get_log(log_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM logs WHERE id=?", (log_id,))
        return await cur.fetchone()


async def points_for(guild_id: int, log_type, result):
    key = f"{log_type}_{result}" if log_type in ("war", "1v1", "2v2") else log_type
    v = await get_setting(guild_id, "points:" + key)
    return v if v is not None else config.DEFAULT_POINTS.get(key, 0)


async def recipients_for(row, data):
    if row["type"] == "tryout":
        return [int(data.get("tester_id") or row["author_id"])]
    ids = [row["author_id"]]
    if row["type"] in ("war", "2v2") and await rule(row["guild_id"], "war_participants"):
        confirmed = data.get("confirmed_participants", [])
        for x in data.get("participants", []):
            x_int = int(x)
            if x_int not in ids and x_int in confirmed:
                ids.append(x_int)
    return ids


def has_role(member, names) -> bool:
    return any(
        n.lower() in r.name.lower()
        for r in getattr(member, "roles", [])
        for n in names
    )


def is_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if owner_id() and member.id == owner_id():
        return True
    return member.guild_permissions.administrator


def is_editor(member) -> bool:
    return is_admin(member) or has_role(member, config.EDITOR_ROLES)


async def can_review(member, log_type) -> bool:
    if is_admin(member):
        return True
    ids = await roles_get(member.guild.id, log_type, "review")
    if ids:
        return any(r.id in ids for r in member.roles)
    return has_role(member, config.REVIEWER_ROLES)


async def can_review_any(member) -> bool:
    for t in config.LOG_TYPES:
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
        return has_role(member, config.TRYOUTER_ROLES)
    return True


def owner_id():
    return int(os.getenv("OWNER_ID") or getattr(config, "OWNER_ID", 0) or 0)


async def audit(text, guild):
    ch_id = await cfg_get(guild.id, "all", "audit_channel")
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        try:
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
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
    if message.content:
        m = LOG_ID_RE.search(message.content)
        if m:
            return int(m.group(1))
            
    for embed in message.embeds:
        text = f"{embed.title or ''} {embed.description or ''}"
        for field in embed.fields:
            text += f" {field.name} {field.value}"
        m = LOG_ID_RE.search(text)
        if m:
            return int(m.group(1))
            
    return None


def mentions(role_id=None, user_ids=None):
    return discord.AllowedMentions(
        roles=[discord.Object(id=role_id)] if role_id else False,
        users=[discord.Object(id=u) for u in user_ids] if user_ids else False,
        everyone=False,
    )


def find_crew_role(guild):
    kw = CREW_ROLE_KEYWORD.lower()
    return discord.utils.find(lambda r: r.name.lower().rstrip().endswith(kw), guild.roles)


def ladder_roles(guild):
    return [discord.utils.find(lambda r, n=n: n.lower() in r.name.lower(), guild.roles)
            for n, _ in config.LADDER]


async def inspect_blobs(blobs, guild_id):
    hashes, flags = [], []
    for att, data in blobs:
        h = hashlib.sha256(data).hexdigest()
        hashes.append(h)
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT h.log_id FROM hashes h JOIN logs l ON l.id=h.log_id "
                "WHERE h.hash=? AND l.guild_id=? AND l.status!='rejected' LIMIT 1", (h, guild_id))
            row = await cur.fetchone()
        if row:
            flags.append(f"{att.filename}: same file was used in Log #{row[0]} within this server")
    return hashes, flags


DIVIDER = ("<:EU1:1449430079611600936> ●▬▬▬▬▬▬▬▬▬▬▬☭  <:Grok2:1513556052036030584> "
           "☭▬▬▬▬▬▬▬▬▬▬▬● <:EU1:1449430079611600936>")
ANGEL = "<:celestial_angel:1455574559062360198>"
MOON = "<:Whitemoon:1458484853640138784>"
ARROW = "<:f_arrows:1364977170140893315>"
CROWN = "<:WhiteCrown:1458484851614416968>"


def clip(v, n=350):
    v = str(v) if v not in (None, "") else "-"
    return v if len(v) <= n else v[: n - 1] + "…"


async def get_custom_template(guild_id, log_type):
    return await get_setting(guild_id, f"template:{log_type}")


class SafeDict(dict):
    def __missing__(self, key):
        return f"{{{key}}}"


def render(log_id, log_type, author_id, data, result, flags=None, status="pending",
           points=None, reviewer_id=None, reason=None, pts_to=None, custom_template=None):
    label = config.LOG_TYPES[log_type]["label"]
    d = lambda k, default="-": clip(data.get(k, default))
    
    header = f"{ANGEL}  **{label}**  {ANGEL}  `Log #{log_id}`\n\n{DIVIDER}\n\n"
    
    if custom_template:
        try:
            format_vars = SafeDict({
                'log_id': log_id,
                'author': f"<@{author_id}>",
                'score': d('score'),
                'notes': d('notes'),
                'youtube_link': data.get('youtube_link', '-'),
                'my_team': d('my_team'),
                'their_team': d('their_team'),
                'crew': d('crew'),
                'my_fighter': d('my_fighter'),
                'opponent': d('opponent'),
                'participants': d('participants'),
                'candidate': data.get('candidate', '-'),
                'roblox': d('roblox')
            })
            rendered_text = custom_template.format_map(format_vars)
            m = header + rendered_text + "\n\n" + DIVIDER + "\n"
        except Exception:
            custom_template = None

    if not custom_template:
        m = header
        m += f"{MOON} **Logged by:** <@{author_id}>\n"

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
            m += f"{MOON} **Training Participants:**\n{ARROW}  {d('participants')}\n\n{DIVIDER}\n\n"
        elif log_type == "tryout":
            tester = data.get("tester_id") or author_id
            m += f"{MOON} **Tested by:** <@{tester}>\n"
            cand = data.get("candidate", "-")
            if data.get("roblox"):
                cand += f" ({clip(data['roblox'], 40)})"
            m += f"{MOON} **Tryout Candidate:**\n{ARROW}  {cand}\n\n"

        if log_type in ("war", "2v2") and data.get("participants"):
            p_list = data.get("participants", [])
            conf_list = data.get("confirmed_participants", [])
            m += f"{MOON} **Tagged Teammates Verification:**\n"
            for p_id in p_list:
                status_icon = "✅ Confirmed" if p_id in conf_list else "⏳ Unconfirmed"
                m += f"• <@{p_id}> — **{status_icon}**\n"
            m += "\n"

        m += f"{DIVIDER}\n\n"
        if result:
            labels = {"win": "✅ Passed", "loss": "❌ Failed"} if log_type == "tryout" else RESULT_LABEL
            m += f"      {CROWN} **Result:** {labels.get(result, result)}\n"
        m += f"      • **Score:** {d('score')}\n"
        if data.get("notes"):
            m += f"      • **Notes:** {d('notes')}\n"
        if data.get("youtube_link"):
            m += f"      • **YouTube Video:** {data.get('youtube_link')}\n"

    status_map = {"pending": "⏳ Pending", "accepted": "✅ Accepted", "rejected": "❌ Rejected", "revoked": "🚫 Revoked"}
    m += f"\n**Status:** {status_map[status]}"
    if points is not None:
        m += f" | **Points:** +{points}"
        if pts_to:
            m += " → " + " ".join(f"<@{u}>" for u in list(pts_to)[:6])
    if reviewer_id and status != "pending":
        who = {"accepted": "Accepted by", "rejected": "Rejected by", "revoked": "Revoked by"}[status]
        m += f"\n{MOON} **{who}:** <@{reviewer_id}>"
    if reason:
        m += f"\n**Reason:** {clip(reason, 300)}"
    if flags:
        m += "\n⚠️ **Warnings:**\n" + "\n".join("• " + clip(f, 120) for f in flags)
    return m[:1950]


async def update_member_rank(guild, member, total):
    ladder = await get_ladder(guild.id)
    roles = ladder_roles(guild)
    target = -1
    for idx, (_, pts) in enumerate(ladder):
        if total >= pts:
            target = idx
            
    current_roles = [r for idx, r in enumerate(roles) if r and r in member.roles]
    target_role = roles[target] if target >= 0 and target < len(roles) else None
    
    if target_role and target_role in member.roles and len(current_roles) == 1:
        return target_role
        
    roles_to_add = [target_role] if target_role and target_role not in member.roles else []
    roles_to_remove = [r for r in current_roles if r != target_role]
    
    if roles_to_add or roles_to_remove:
        try:
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="Rank update based on points")
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Rank update based on points")
            if target_role:
                ch_id = await cfg_get(guild.id, "all", "log_channel")
                ch = guild.get_channel(ch_id) if ch_id else None
                if ch:
                    await ch.send(f"🎉 {member.mention} has been updated to rank {target_role.mention}!",
                                  allowed_mentions=discord.AllowedMentions(users=[member], roles=False))
        except discord.Forbidden:
            err_msg = f"❌ [Role Permission Error] Lacking permissions to manage roles for {member.display_name} in '{guild.name}'."
            print(err_msg)
            await audit(err_msg, guild)
        except discord.HTTPException as e:
            err_msg = f"❌ [Discord API Error] Failed role update for {member.display_name}: {e}"
            print(err_msg)
            await audit(err_msg, guild)
    return target_role


async def bulk_update_ranks(guild: discord.Guild):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM members WHERE guild_id=?", (guild.id,))
        rows = await cur.fetchall()
    for uid, pts in rows:
        try:
            member = await get_member(guild, uid)
            if member:
                await update_member_rank(guild, member, pts)
        except Exception as e:
            print(f"⚠️ Failed to fetch member {uid} for bulk rank update: {e}")


async def accept_log(i: discord.Interaction):
    log_id = parse_log_id(i.message)
    row = await get_log(log_id) if log_id else None
    if not row:
        return await i.response.send_message("Log not found in database.", ephemeral=True)
    
    if row["guild_id"] != i.guild.id:
        return await i.response.send_message("❌ This log does not belong to this server.", ephemeral=True)

    if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
        return await i.response.send_message("You don't have permission to review this log.", ephemeral=True)
    
    if row["author_id"] == i.user.id:
        return await i.response.send_message("You cannot review your own log.", ephemeral=True)
        
    await i.response.defer()

    data = json.loads(row["data"])
    flags = json.loads(row["flags"] or "[]")
    pts = await points_for(i.guild.id, row["type"], row["result"])
    recips = await recipients_for(row, data)
    stamp = now_utc().isoformat()
    
    season = row["season_id"] or "main"

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
                    "INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?,?)",
                    (i.guild.id, log_id, uid, pts, stamp, "log", season))
                cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, uid))
                totals[uid] = (await cur.fetchone())[0]
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await i.followup.send(f"Database error during acceptance: {e}", ephemeral=True)

    pub_id = await cfg_get(i.guild.id, row["type"], "log_channel")
    log_ch = i.guild.get_channel(pub_id) if pub_id else None
    custom_tmpl = await get_custom_template(i.guild.id, row["type"])

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
            await db_exec("UPDATE logs SET public_channel=?, public_msg=? WHERE id=?",
                          (log_ch.id, sent.id, log_id))
        except discord.HTTPException as e:
            await i.followup.send(f"Accepted, but I couldn't post in the log channel: {e}", ephemeral=True)
    else:
        await i.followup.send("Accepted, but no log channel is set. Use /setchannel.", ephemeral=True)

    reviewed = render(log_id, row["type"], row["author_id"], data, row["result"], flags,
                      "accepted", pts, i.user.id, pts_to=recips, custom_template=custom_tmpl)
    await i.message.edit(content=reviewed, view=None, allowed_mentions=discord.AllowedMentions.none())

    for uid, total in totals.items():
        try:
            member = await get_member(i.guild, uid)
            await update_member_rank(i.guild, member, total)
        except (discord.NotFound, discord.Forbidden) as e:
            print(f"⚠️ Log accepted, but failed to update rank for <@{uid}>: {e}")

    if (row["type"] == "tryout" and row["result"] == "win" and data.get("candidate_id")
            and await rule(i.guild.id, "tryout_autorole")):
        try:
            cand_member = await get_member(i.guild, int(data["candidate_id"]))
            crew_role = find_crew_role(i.guild)
            if crew_role and crew_role not in cand_member.roles:
                await cand_member.add_roles(crew_role, reason="Passed tryout")
        except (discord.NotFound, discord.Forbidden) as e:
            print(f"⚠️ Accepted, but couldn't give Crew role: {e}")


class RejectModal(discord.ui.Modal, title="Rejection Reason"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, log_id, message):
        super().__init__()
        self.log_id = log_id
        self.message = message

    async def on_submit(self, i: discord.Interaction):
        row = await get_log(self.log_id)
        if not row or row["guild_id"] != i.guild.id:
            return await i.response.send_message("Log not found or not in this server.", ephemeral=True)
            
        if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
            return await i.response.send_message("❌ You no longer have permission to review this log.", ephemeral=True)

        if row["author_id"] == i.user.id:
            return await i.response.send_message("❌ You cannot review your own log.", ephemeral=True)

        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE logs SET status='rejected', reviewer_id=?, reason=?, reviewed_at=? "
                    "WHERE id=? AND status='pending'",
                    (i.user.id, self.reason.value, now_utc().isoformat(), self.log_id))
                if cur.rowcount == 0:
                    await db.rollback()
                    return await i.response.send_message("This log has already been reviewed or closed.", ephemeral=True)
                await db.commit()
            except Exception as e:
                await db.rollback()
                return await i.response.send_message(f"Database error: {e}", ephemeral=True)

        data = json.loads(row["data"])
        flags = json.loads(row["flags"] or "[]")
        custom_tmpl = await get_custom_template(i.guild.id, row["type"])
        text = render(self.log_id, row["type"], row["author_id"], data, row["result"], flags,
                      "rejected", reviewer_id=i.user.id, reason=self.reason.value, custom_template=custom_tmpl)
        await self.message.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
        await i.response.send_message("Log rejected.", ephemeral=True)
        try:
            author = await bot.fetch_user(row["author_id"])
            await author.send(
                f"❌ Log #{self.log_id} was rejected.\nReason: {self.reason.value}\n\n"
                f"You can fix it and send it again with `/resubmit log_id:{self.log_id}`")
        except discord.HTTPException:
            pass


class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, custom_id="log:accept")
    async def accept(self, i: discord.Interaction, button: discord.ui.Button):
        await accept_log(i)

    @discord.ui.button(label="Reject", emoji="❌", style=discord.ButtonStyle.danger, custom_id="log:reject")
    async def reject(self, i: discord.Interaction, button: discord.ui.Button):
        log_id = parse_log_id(i.message)
        row = await get_log(log_id) if log_id else None
        if not row or row["guild_id"] != i.guild.id:
            return await i.response.send_message("Log not found in database.", ephemeral=True)
        if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
            return await i.response.send_message("You don't have permission to review this log.", ephemeral=True)
        if row["author_id"] == i.user.id:
            return await i.response.send_message("You cannot review your own log.", ephemeral=True)
        await i.response.send_modal(RejectModal(log_id, i.message))

    @discord.ui.button(label="Confirm Participation", emoji="✋", style=discord.ButtonStyle.primary, custom_id="log:confirm_part")
    async def confirm_part(self, i: discord.Interaction, button: discord.ui.Button):
        log_id = parse_log_id(i.message)
        row = await get_log(log_id) if log_id else None
        if not row or row["guild_id"] != i.guild.id:
            return await i.response.send_message("Log not found.", ephemeral=True)

        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute("SELECT status, data FROM logs WHERE id=? AND guild_id=?", (log_id, i.guild.id))
                log_row = await cur.fetchone()
                if not log_row or log_row[0] != "pending":
                    await db.rollback()
                    return await i.response.send_message("❌ This log is no longer pending review or has been closed.", ephemeral=True)
                
                data = json.loads(log_row[1])
                participants = data.get("participants", [])

                if i.user.id not in participants:
                    await db.rollback()
                    return await i.response.send_message("❌ You are not listed as a tagged participant in this log.", ephemeral=True)

                confirmed = data.get("confirmed_participants", [])
                if i.user.id in confirmed:
                    await db.rollback()
                    return await i.response.send_message("✅ You have already confirmed your participation for this log.", ephemeral=True)

                confirmed.append(i.user.id)
                data["confirmed_participants"] = confirmed

                await db.execute("UPDATE logs SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), log_id))
                await db.commit()
            except Exception as e:
                await db.rollback()
                return await i.response.send_message(f"Database error: {e}", ephemeral=True)

        flags = json.loads(row["flags"] or "[]")
        custom_tmpl = await get_custom_template(i.guild.id, row["type"])
        updated_text = render(log_id, row["type"], row["author_id"], data, row["result"], flags, custom_template=custom_tmpl)

        try:
            await i.message.edit(content=updated_text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        await i.response.send_message("✅ Your participation in this match has been confirmed successfully!", ephemeral=True)


class LogModal(discord.ui.Modal):
    def __init__(self, log_type, result, files, extra, defaults=None):
        super().__init__(title=config.LOG_TYPES[log_type]["label"][:45])
        self.log_type, self.result, self.files, self.extra = log_type, result, files, extra
        self.inputs = {}
        defaults = defaults or {}
        for key, label, long, required in config.LOG_TYPES[log_type]["fields"]:
            ti = discord.ui.TextInput(
                label=label[:45], required=required,
                max_length=350 if long else 100,
                default=defaults.get(key) or None,
                style=discord.TextStyle.paragraph if long else discord.TextStyle.short)
            self.add_item(ti)
            self.inputs[key] = ti

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)

        if len(self.files) > MAX_FILES_COUNT:
            return await i.followup.send(f"❌ عدد الملفات المرفقة يتجاوز الحد المسموح ({MAX_FILES_COUNT} ملفات كحد أقصى).", ephemeral=True)

        total_size = 0
        for a in self.files:
            if hasattr(a, 'filename'):
                ext = os.path.splitext(a.filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    return await i.followup.send(f"❌ الملف `{a.filename}` بصيغة غير مسموحة.", ephemeral=True)
                if a.size > MAX_FILE_SIZE:
                    return await i.followup.send(f"❌ الملف `{a.filename}` يتجاوز الحد المسموح (25 ميجابايت).", ephemeral=True)
                total_size += a.size

        if total_size > MAX_TOTAL_FILES_SIZE:
            return await i.followup.send(f"❌ إجمالي حجم المرفقات يتجاوز 50 ميجابايت.", ephemeral=True)

        season = await get_current_season(i.guild.id)
        limit = await rule(i.guild.id, "daily_limit")
        
        blobs = []
        proof_urls = []
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
                    n = (await cur.fetchone())[0]
                    if n >= limit:
                        await db.rollback()
                        return await i.followup.send(
                            f"❌ Daily log limit reached ({limit} logs per 24 hours).", ephemeral=True)

                cur = await db.execute(
                    "INSERT INTO logs(guild_id, type, author_id, result, data, flags, created_at, season_id) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (i.guild.id, self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False),
                     json.dumps(flags, ensure_ascii=False), now_utc().isoformat(), season))
                log_id = cur.lastrowid
                for h in hashes:
                    await db.execute("INSERT OR IGNORE INTO hashes VALUES(?,?)", (h, log_id))
                await db.commit()
            except Exception as e:
                await db.rollback()
                return await i.followup.send(f"❌ Database error during submission: {e}", ephemeral=True)

        ch_id = await cfg_get(i.guild.id, self.log_type, "review_channel")
        ch = i.guild.get_channel(ch_id) if ch_id else None
        ping_id = await cfg_get(i.guild.id, self.log_type, "review_ping")
        
        custom_tmpl = await get_custom_template(i.guild.id, self.log_type)
        text = render(log_id, self.log_type, i.user.id, data, self.result, flags, custom_template=custom_tmpl)
        content = (f"<@&{ping_id}>\n" if ping_id else "") + text
        am = mentions(ping_id)

        async def fail_publish(reason):
            await db_exec("UPDATE logs SET status='rejected', reason=? WHERE id=?",
                          (f"failed to post for review: {reason}", log_id))
            await i.followup.send(
                f"❌ Failed to post log for review ({reason}). Check review channel configuration.",
                ephemeral=True)

        if not ch:
            return await fail_publish("Channel not set")
        sent = None
        try:
            if blobs:
                files_payload = [discord.File(io.BytesIO(b), a.filename) for a, b in blobs]
                sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am, files=files_payload)
            else:
                sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am)
                if proof_urls:
                    await ch.send("**Attached Proof Links:**\n" + "\n".join(proof_urls), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            return await fail_publish(str(e))

        await db_exec("UPDATE logs SET review_channel=?, review_msg=? WHERE id=?", (ch.id, sent.id, log_id))
        if flags:
            await dm_owner(f"⚠️ {i.user.mention} submitted Log #{log_id} with warnings:\n" + "\n".join(flags))
        await i.followup.send(f"✅ Log #{log_id} submitted for review.", ephemeral=True)


async def start_log(i: discord.Interaction, log_type, result, files: List[Optional[discord.Attachment]], extra=None):
    files = [f for f in files if f is not None]
    
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message("Use this command inside the server.", ephemeral=True)
    if not await check_cmd_perm(i, f"log-{log_type}"):
        return await i.response.send_message("❌ You don't have the required role to use this command.", ephemeral=True)
    if not await can_submit(i.user, log_type):
        return await i.response.send_message("You don't have permission to submit this log type.", ephemeral=True)
    
    if log_type == "war" and len(files) < 2:
        return await i.response.send_message("❌ Crew War requires **at least 2 attachments** (images or videos).", ephemeral=True)
    
    if log_type != "war" and len(files) < 1:
        return await i.response.send_message("❌ You must attach at least **one image or video** as proof.", ephemeral=True)
    
    rid = await cfg_get(i.guild.id, log_type, "review_channel")
    if not rid or not i.guild.get_channel(rid):
        return await i.response.send_message(
            "Review channel is not set for this log type. Ask an admin to use /setchannel.", ephemeral=True)
    await i.response.send_modal(LogModal(log_type, result, files, extra or {}))


def get_help_embeds():
    embed1 = discord.Embed(
        title=f"⚔️ DragonSlayerBot — Full Command Manual ({BOT_VERSION})",
        description="Welcome to DragonSlayerBot guide! Select categories below or read the full manual.",
        color=discord.Color.gold()
    )
    embed1.add_field(
        name="📜 1. Activity Logging Commands",
        value="• `/log-war`: Submit Crew War results (min 2 attachments).\n"
              "• `/log-1v1`: Submit 1v1 battle results.\n"
              "• `/log-2v2`: Submit 2v2 match with teammate.\n"
              "• `/log-training`: Submit internal training session.\n"
              "• `/log-tryout`: Submit new member evaluation.\n"
              "• `/resubmit`: Fix and resend rejected log.",
        inline=False
    )
    embed1.add_field(
        name="🎨 2. Updates & Customization",
        value="• `/log-update`: Publish system updates automatically with version notes.\n"
              "• `/set-log-template`: Customize log Embeds for each type.\n"
              "• `/set-update-ping`: Specify role to ping on updates.",
        inline=False
    )

    embed2 = discord.Embed(
        title="📊 Points, Stats & Admin Commands",
        color=discord.Color.blurple()
    )
    embed2.add_field(
        name="🏆 3. Points & Stats Commands",
        value="• `/points`: Check your balance & progress.\n"
              "• `/leaderboard`: View top members (All-time / Weekly).\n"
              "• `/profile`: Display complete stat card & Roblox account.\n"
              "• `/stats`: View overall war history against rival crews.\n"
              "• `/rates`: Check log points & ladder thresholds.\n"
              "• `/link`: Link your Roblox username.",
        inline=False
    )
    embed2.add_field(
        name="⚙️ 4. Admin & Security Setup",
        value="• `/backup`: Secure backup generator (Guild activity / Owner Full DB).\n"
              "• `/set-cmd-role`: Lock commands to specific roles.\n"
              "• `/setchannel`: Configure Review, Log & Audit channels.\n"
              "• `/setping`: Set ping roles for new logs.\n"
              "• `/setroles`: Assign review & submission permissions.\n"
              "• `/pending`: Review waiting logs.\n"
              "• `/revoke-log`: Cancel accepted log & retract points.",
        inline=False
    )
    return [embed1, embed2]


async def check_and_notify_auto_update(guild: discord.Guild):
    last_ver = await get_setting(guild.id, "state:bot_version")
    if last_ver != BOT_VERSION:
        ch_id = await cfg_get(guild.id, "all", "log_channel")
        ch = guild.get_channel(ch_id) if ch_id else None
        if ch:
            ping_role_id = await cfg_get(guild.id, "all", "update_ping")
            ping_text = f"<@&{ping_role_id}>\n" if ping_role_id else ""
            
            data = UPDATE_PRESETS["bot_patch"]
            embed = discord.Embed(
                title=f"🤖 System Production Update — Version {BOT_VERSION}",
                color=discord.Color.green(),
                timestamp=now_utc()
            )
            embed.add_field(name="English Patch Notes", value=data["desc_en"], inline=False)
            embed.add_field(name="تفاصيل التحديث بالعربية", value=data["desc_ar"], inline=False)
            embed.set_footer(text=f"DragonSlayerBot Security Engine | {BOT_VERSION}")
            try:
                await ch.send(content=ping_text, embed=embed, allowed_mentions=mentions(ping_role_id))
                await set_setting(guild.id, "state:bot_version", BOT_VERSION)
            except discord.HTTPException:
                pass


@bot.tree.command(name="help", description="Show full bot manual & command guide | عرض دليل الأوامر والشرح الكامل")
async def help_cmd(i: discord.Interaction):
    if not await check_cmd_perm(i, "help"):
        return await i.response.send_message("❌ You don't have the required role to use this command.", ephemeral=True)
    embeds = get_help_embeds()
    await i.response.send_message(embeds=embeds, ephemeral=True)


@bot.tree.command(name="send-help-panel", description="Send persistent help manual into a channel | إرسال رسالة الشرح الشامل في قناة محددة")
async def send_help_panel(i: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not await check_cmd_perm(i, "send-help-panel") or not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    target = channel or i.channel
    embeds = get_help_embeds()
    try:
        await target.send(embeds=embeds)
        await i.response.send_message(f"✅ Posted complete help manual to {target.mention}!", ephemeral=True)
    except discord.HTTPException as e:
        await i.response.send_message(f"❌ Failed to send help panel: {e}", ephemeral=True)


async def cmd_name_autocomplete(i: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=cmd, value=cmd)
        for cmd in ALL_BOT_COMMANDS
        if current.lower() in cmd.lower()
    ][:25]


@bot.tree.command(name="set-cmd-role", description="Lock a command to a specific role | تحديد رتبة مخصصة لاستخدام أمر معين")
@app_commands.autocomplete(command=cmd_name_autocomplete)
@app_commands.describe(command="Select command | اختر الأمر", role="Role required (Leave empty to reset) | الرتبة المطلوبة")
async def set_cmd_role(i: discord.Interaction, command: str, role: Optional[discord.Role] = None):
    if not await check_cmd_perm(i, "set-cmd-role") or not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    if command not in ALL_BOT_COMMANDS:
        return await i.response.send_message("❌ Invalid command name specified.", ephemeral=True)
    
    async with aiosqlite.connect(DB) as db:
        if role:
            await db.execute(
                "INSERT INTO cmd_roles(guild_id, cmd_name, role_id) VALUES(?,?,?) "
                "ON CONFLICT(guild_id, cmd_name) DO UPDATE SET role_id=excluded.role_id",
                (i.guild.id, command, role.id))
            msg = f"🔒 Command `/{command}` is now locked to role {role.mention}."
        else:
            await db.execute("DELETE FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
            msg = f"🔓 Command `/{command}` is now accessible to default permissions."
        await db.commit()
    await save_config_snapshot_json()
    await audit(f"⚙️ {i.user.mention} updated command permission for `/{command}`", i.guild)
    await i.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="set-update-ping", description="Set role to ping for bot updates and manuals | تحديد رتبة المنشن لنشرة التحديثات والشرح")
async def set_update_ping(i: discord.Interaction, role: Optional[discord.Role] = None):
    if not await check_cmd_perm(i, "set-update-ping") or not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    
    await cfg_set(i.guild.id, "all", "update_ping", role.id if role else None)
    await audit(f"⚙️ {i.user.mention} set update ping role to {role.mention if role else 'None'}", i.guild)
    msg = f"✅ Update ping role set to {role.mention}." if role else "🗑️ Update ping role cleared."
    await i.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="log-update", description="Publish system update & send manual on first run | إرسال لوج التحديث والتأكد من نشر الشرح")
@app_commands.choices(preset=UPDATE_TYPE_CHOICES)
@app_commands.describe(
    preset="Choose update type | اختر نوع التحديث التلقائي",
    channel="Target channel (Optional - Default: Log channel) | القناة المراد الإرسال إليها"
)
async def log_update(i: discord.Interaction, preset: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None):
    if not await check_cmd_perm(i, "log-update") or not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
        
    data = UPDATE_PRESETS.get(preset.value)
    if not data:
        return await i.response.send_message("Invalid update preset.", ephemeral=True)

    target_ch = channel
    if not target_ch:
        ch_id = await cfg_get(i.guild.id, "all", "log_channel")
        target_ch = i.guild.get_channel(ch_id) if ch_id else None

    if not target_ch:
        return await i.response.send_message("❌ Target channel is not specified or log channel is not set.", ephemeral=True)

    await i.response.defer(ephemeral=True)

    ping_role_id = await cfg_get(i.guild.id, "all", "update_ping")
    ping_text = f"<@&{ping_role_id}>\n" if ping_role_id else ""

    has_sent_manual = await get_setting(i.guild.id, "state:help_manual_sent")
    if not has_sent_manual:
        embeds = get_help_embeds()
        try:
            await target_ch.send(content=f"{ping_text}📖 **DragonSlayerBot Full System Manual & Setup ({BOT_VERSION})**", embeds=embeds, allowed_mentions=mentions(ping_role_id))
            await set_setting(i.guild.id, "state:help_manual_sent", 1)
        except discord.HTTPException:
            pass

    embed = discord.Embed(
        title=f"📢 {data['title_en']} | {data['title_ar']}",
        color=discord.Color.gold(),
        timestamp=now_utc()
    )
    embed.add_field(name="English Version", value=data["desc_en"], inline=False)
    embed.add_field(name="التفاصيل بالعربية", value=data["desc_ar"], inline=False)
    embed.set_footer(text=f"Published by {i.user.display_name} | Version {BOT_VERSION}", icon_url=i.user.display_avatar.url)

    try:
        await target_ch.send(content=ping_text, embed=embed, allowed_mentions=mentions(ping_role_id))
        await audit(f"📢 {i.user.mention} posted update **{preset.name}** (Ver {BOT_VERSION}) in {target_ch.mention}", i.guild)
        await i.followup.send(f"✅ Update preset **{preset.name}** (Version {BOT_VERSION}) published to {target_ch.mention}!", ephemeral=True)
    except discord.HTTPException as e:
        await i.followup.send(f"❌ Failed to send update message: {e}", ephemeral=True)


def _backup_file(path):
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(path)
    src.backup(dst)
    dst.close()
    src.close()


@bot.tree.command(name="backup", description="Secure backup tool (Guild activity / Owner Full DB) | أداة النسخ الاحتياطي الآمنة")
@app_commands.choices(scope=BACKUP_SCOPE_CHOICES, destination=BACKUP_DEST_CHOICES)
@app_commands.describe(
    scope="Choose backup scope | اختر نطاق النسخ الاحتياطي",
    destination="Choose where to send file | اختر مكان إرسال الملف"
)
async def backup_cmd(i: discord.Interaction, scope: app_commands.Choice[str], destination: app_commands.Choice[str]):
    if not await check_cmd_perm(i, "backup") or not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)

    if scope.value == "full":
        if not owner_id() or i.user.id != owner_id():
            return await i.response.send_message(
                "❌ **Security Error:** Full database backups can only be requested by the official bot owner.",
                ephemeral=True
            )
        if destination.value != "owner":
            return await i.response.send_message(
                "❌ **Security Error:** Full database backups are strictly prohibited from being sent to public chats.",
                ephemeral=True
            )

    await i.response.defer(ephemeral=True)

    path = None
    file_to_send = None

    try:
        unique_suffix = uuid.uuid4().hex[:8]
        timestamp_str = now_utc().strftime("%Y%m%d-%H%M%S-%f")

        if scope.value == "full":
            filename = f"crewbot-full-master-backup-{timestamp_str}-{unique_suffix}.db"
            path = os.path.join(tempfile.gettempdir(), filename)
            await asyncio.to_thread(_backup_file, path)
            file_to_send = discord.File(path, filename=filename)
        else:
            today_str = now_utc().strftime("%Y-%m-%d")
            filename = f"crewbot-guild-{i.guild.id}-activity-{today_str}-{unique_suffix}.json"
            
            async with aiosqlite.connect(DB) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM logs WHERE guild_id=? AND created_at >= ?", (i.guild.id, today_str))
                today_logs = [dict(r) for r in await cur.fetchall()]
                
                cur = await db.execute("SELECT * FROM point_events WHERE guild_id=? AND created_at >= ?", (i.guild.id, today_str))
                today_events = [dict(r) for r in await cur.fetchall()]

            data = {
                "version": BOT_VERSION,
                "guild_id": i.guild.id,
                "guild_name": i.guild.name,
                "date": today_str,
                "logs": today_logs,
                "point_events": today_events,
                "exported_at": now_utc().isoformat()
            }
            
            path = os.path.join(tempfile.gettempdir(), filename)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
                
            file_to_send = discord.File(path, filename=filename)

        if destination.value == "owner":
            success = await dm_owner(f"🗄️ **Secure Backup Report** (`{scope.name}`) from guild **{i.guild.name}** (`{i.guild.id}`) requested by {i.user.mention}:", file=file_to_send)
            if success:
                await audit(f"🗄️ {i.user.mention} successfully generated and exported secure backup (`{scope.name}`) to Bot Owner DM.", i.guild)
                await i.followup.send("✅ Secure backup file has been successfully sent to Bot Owner's DMs!", ephemeral=True)
            else:
                await i.followup.send("❌ Failed to send backup to owner's DM (Check OWNER_ID or DM settings).", ephemeral=True)
        else:
            await audit(f"🗄️ {i.user.mention} exported guild activity backup (`{scope.name}`) in chat.", i.guild)
            await i.followup.send(f"🗄️ **Guild Backup File ({scope.name}) — Ver {BOT_VERSION}:**", file=file_to_send, ephemeral=True)

    except Exception as e:
        await i.followup.send(f"❌ Backup error: `{str(e)[:200]}`", ephemeral=True)
    finally:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except Exception:
                pass


@bot.tree.command(name="set-log-template", description="Customize log format/template for a specific log type | تخصيص شكل اللوج لنوع معين")
@app_commands.choices(log_type=LOG_TYPE_CHOICES[1:])
@app_commands.describe(
    log_type="Choose log type | اختر نوع اللوج",
    template="Custom template design (Use variables like {author}, {score}, {my_fighter}, {opponent}, {notes}) | شكل التصميم الجديد"
)
async def set_log_template(i: discord.Interaction, log_type: app_commands.Choice[str], template: Optional[str] = None):
    if not await check_cmd_perm(i, "set-log-template") or not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    
    key = f"template:{log_type.value}"
    if template:
        await set_setting(i.guild.id, key, template)
        await audit(f"🎨 {i.user.mention} updated the log template for **{log_type.name}**", i.guild)
        await i.response.send_message(f"✅ Successfully updated the template design for **{log_type.name}**!", ephemeral=True)
    else:
        async with aiosqlite.connect(DB) as db:
            await db.execute("DELETE FROM settings WHERE guild_id=? AND key=?", (i.guild.id, key))
            await db.commit()
        await save_config_snapshot_json()
        await audit(f"🗑️ {i.user.mention} reset log template for **{log_type.name}** to default", i.guild)
        await i.response.send_message(f"🔄 Reset template for **{log_type.name}** back to default layout.", ephemeral=True)


@bot.tree.command(name="log-war", description="Log a Crew War (min 2 attachments: images/videos) | تسجيل حرب كرو")
@app_commands.choices(result=RESULT_CHOICES)
@app_commands.describe(
    result="Win or Loss | نتيجة الحرب", file1="First proof | الملف الأول", file2="Second proof | الملف الثاني",
    file3="Additional proof | ملف ثالث", youtube_link="YouTube link | رابط يوتيوب",
    player1="Teammate 1", player2="Teammate 2", player3="Teammate 3", player4="Teammate 4"
)
async def log_war(i: discord.Interaction, result: app_commands.Choice[str],
                  file1: discord.Attachment, file2: discord.Attachment,
                  file3: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None,
                  player1: Optional[discord.Member] = None, player2: Optional[discord.Member] = None,
                  player3: Optional[discord.Member] = None, player4: Optional[discord.Member] = None):
    ids = [p.id for p in (player1, player2, player3, player4) if p and not p.bot and p.id != i.user.id]
    extra = {"participants": ids}
    if youtube_link: extra["youtube_link"] = youtube_link
    await start_log(i, "war", result.value, [file1, file2, file3], extra)


@bot.tree.command(name="log-1v1", description="Log a 1v1 fight | تسجيل فايت 1v1")
@app_commands.choices(result=RESULT_CHOICES)
@app_commands.describe(result="Win or Loss", proof1="Proof 1", proof2="Proof 2", youtube_link="YouTube link")
async def log_1v1(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None):
    extra = {}
    if youtube_link: extra["youtube_link"] = youtube_link
    await start_log(i, "1v1", result.value, [proof1, proof2], extra)


@bot.tree.command(name="log-2v2", description="Log a 2v2 fight | تسجيل فايت 2v2")
@app_commands.choices(result=RESULT_CHOICES)
@app_commands.describe(result="Win or Loss", proof1="Proof 1", proof2="Proof 2", youtube_link="YouTube link", teammate="Teammate")
async def log_2v2(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None, teammate: Optional[discord.Member] = None):
    ids = [teammate.id] if teammate and not teammate.bot and teammate.id != i.user.id else []
    extra = {"participants": ids}
    if youtube_link: extra["youtube_link"] = youtube_link
    await start_log(i, "2v2", result.value, [proof1, proof2], extra)


@bot.tree.command(name="log-training", description="Log training | تسجيل تدريب داخلي")
@app_commands.describe(proof1="Proof 1", proof2="Proof 2")
async def log_training(i: discord.Interaction, proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None):
    await start_log(i, "training", None, [proof1, proof2])


@bot.tree.command(name="log-tryout", description="Log a tryout | تسجيل تراي أوت")
@app_commands.choices(result=TRYOUT_CHOICES)
@app_commands.describe(candidate="Candidate | المرشح", result="Passed/Failed | النتيجة", proof="Proof | دليل الإثبات")
async def log_tryout(i: discord.Interaction, candidate: discord.Member, result: app_commands.Choice[str], proof: discord.Attachment):
    extra = {"candidate_id": candidate.id, "candidate": candidate.mention, "tester_id": i.user.id}
    await start_log(i, "tryout", result.value, [proof], extra)


@bot.tree.command(name="resubmit", description="Fix and resend a rejected log | إعادة إرسال لوج مرفوض")
async def resubmit(i: discord.Interaction, log_id: int):
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id or row["author_id"] != i.user.id:
        return await i.response.send_message("Log not found or not yours.", ephemeral=True)
    if row["status"] != "rejected" or row["type"] not in config.LOG_TYPES:
        return await i.response.send_message("This log is not rejected.", ephemeral=True)
    
    if not await check_cmd_perm(i, f"log-{row['type']}"):
        return await i.response.send_message("❌ You don't have required role.", ephemeral=True)
    if not await can_submit(i.user, row["type"]):
        return await i.response.send_message("You don't have permission to submit this log type.", ephemeral=True)

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
        files = data["proof_urls"]

    if not files:
        return await i.response.send_message("Could not fetch old attachments. Please submit a new log.", ephemeral=True)
        
    extra = {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants", "youtube_link") if k in data}
    await i.response.send_modal(LogModal(row["type"], row["result"], files, extra, defaults=data))


@bot.tree.command(name="revoke-log", description="Cancel an accepted log | إلغاء لوج مقبول")
async def revoke_log(i: discord.Interaction, log_id: int, reason: str):
    if not await check_cmd_perm(i, "revoke-log"):
        return await i.response.send_message("❌ You don't have required role.", ephemeral=True)
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id or row["status"] != "accepted":
        return await i.response.send_message("Accepted log not found in this server.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)

    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT status FROM logs WHERE id=? AND guild_id=?", (log_id, i.guild.id))
            chk = await cur.fetchone()
            if not chk or chk[0] != "accepted":
                await db.rollback()
                return await i.followup.send("❌ Log is no longer accepted or already revoked.", ephemeral=True)

            cur = await db.execute("SELECT user_id, points FROM point_events WHERE guild_id=? AND log_id=?", (i.guild.id, log_id))
            events = await cur.fetchall()
            for uid, p in events:
                await db.execute("UPDATE members SET points = MAX(0, points - ?) WHERE guild_id=? AND user_id=?", (p, i.guild.id, uid))
            await db.execute("DELETE FROM point_events WHERE guild_id=? AND log_id=?", (i.guild.id, log_id))
            await db.execute("UPDATE logs SET status='revoked', reason=?, reviewer_id=? WHERE id=?", (reason, i.user.id, log_id))
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await i.followup.send(f"❌ Database error during revoke: {e}", ephemeral=True)

    data = json.loads(row["data"])
    custom_tmpl = await get_custom_template(i.guild.id, row["type"])
    text = render(log_id, row["type"], row["author_id"], data, row["result"], None, "revoked", reviewer_id=i.user.id, reason=reason, custom_template=custom_tmpl)
    for ch_id, msg_id in ((row["review_channel"], row["review_msg"]), (row["public_channel"], row["public_msg"])):
        if ch_id and msg_id:
            try:
                ch = i.guild.get_channel(ch_id)
                msg = await ch.fetch_message(msg_id)
                await msg.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, AttributeError): pass
                
    for uid, _ in events:
        try:
            member = await get_member(i.guild, uid)
            async with aiosqlite.connect(DB) as db:
                cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, uid))
                res = await cur.fetchone()
                total = res[0] if res else 0
            await update_member_rank(i.guild, member, total)
        except Exception as e:
            print(f"⚠️ Failed rank update after revoke for user {uid}: {e}")

    await audit(f"🚫 {i.user.mention} revoked Log #{log_id}. Reason: {reason}", i.guild)
    await i.followup.send(f"Successfully revoked Log #{log_id}.", ephemeral=True)


@bot.tree.command(name="pending", description="List logs waiting for review | اللوجات المنتظرة")
async def pending(i: discord.Interaction):
    if not await check_cmd_perm(i, "pending"):
        return await i.response.send_message("❌ You don't have required role.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not (is_editor(i.user) or await can_review_any(i.user)):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id, type, author_id, created_at FROM logs WHERE status='pending' AND guild_id=? ORDER BY id LIMIT 20", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows: return await i.response.send_message("No logs waiting for review ✅", ephemeral=True)
    lines = [f"**#{lid}** {config.LOG_TYPES[t]['label']} — <@{author}> — {str(created)[:16]}" for lid, t, author, created in rows]
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@bot.tree.command(name="setchannel", description="Set channels | تحديد القنوات")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_CHANNEL)
async def setchannel(i: discord.Interaction, log_type: app_commands.Choice[str], kind: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None):
    if not await check_cmd_perm(i, "setchannel") or not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    t = "all" if kind.value == "audit_channel" else log_type.value
    await cfg_set(i.guild.id, t, kind.value, channel.id if channel else None)
    await i.response.send_message(f"✅ **{kind.name}** ({t}) = {channel.mention if channel else 'Cleared'}", ephemeral=True)


@bot.tree.command(name="setping", description="Set ping roles | تحديد رتب المنشن")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_PING)
async def setping(i: discord.Interaction, log_type: app_commands.Choice[str], kind: app_commands.Choice[str], role: Optional[discord.Role] = None):
    if not await check_cmd_perm(i, "setping") or not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await cfg_set(i.guild.id, log_type.value, kind.value, role.id if role else None)
    await i.response.send_message(f"✅ **{kind.name}** ({log_type.name}) = {role.mention if role else 'Cleared'}", ephemeral=True)


@bot.tree.command(name="setroles", description="Set role permissions | صلاحيات الرتب")
@app_commands.choices(kind=KIND_ROLES, log_type=LOG_TYPE_CHOICES, action=ACTIONS)
async def setroles(i: discord.Interaction, kind: app_commands.Choice[str], log_type: app_commands.Choice[str], action: app_commands.Choice[str], role: discord.Role):
    if not await check_cmd_perm(i, "setroles") or not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await roles_edit(i.guild.id, log_type.value, kind.value, role.id, action.value == "add")
    await i.response.send_message(f"✅ Updated **{kind.name}** ({log_type.name}) for {role.mention}.", ephemeral=True)


@bot.tree.command(name="setrule", description="Change a server rule | تغيير قاعدة في السيرفر الحالي")
@app_commands.choices(rule_name=RULE_CHOICES)
async def setrule(i: discord.Interaction, rule_name: app_commands.Choice[str], value: app_commands.Range[int, 0, 1000]):
    if not await check_cmd_perm(i, "setrule") or not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await set_setting(i.guild.id, "rule:" + rule_name.value, value)
    await audit(f"⚙️ {i.user.mention} updated server rule `{rule_name.value}` to `{value}`", i.guild)
    await i.response.send_message(f"✅ Server rule **{rule_name.value}** = {value}", ephemeral=True)


@bot.tree.command(name="settings", description="Show complete server settings & rules | عرض الإعدادات والقواعد بالكامل")
async def settings_cmd(i: discord.Interaction):
    if not await check_cmd_perm(i, "settings") or not is_editor(i.user): 
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    
    g = i.guild.id
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT log_type, key, value FROM guild_cfg WHERE guild_id=?", (g,))
        rows = await cur.fetchall()
        cur = await db.execute("SELECT cmd_name, role_id FROM cmd_roles WHERE guild_id=?", (g,))
        cmd_rows = await cur.fetchall()
        
    cfg_map = {(t, k): val for t, k, val in rows}
    
    embed = discord.Embed(
        title=f"⚙️ Full Server Settings & Rules — Ver {BOT_VERSION}",
        color=discord.Color.blue(),
        timestamp=now_utc()
    )
    
    audit_ch = cfg_map.get(('all', 'audit_channel'))
    embed.add_field(name="📍 Audit Channel", value=f"<#{audit_ch}>" if audit_ch else "Not set", inline=False)
    
    ch_text = ""
    for t_key in config.LOG_TYPES:
        rev_ch = cfg_map.get((t_key, 'review_channel'))
        log_ch = cfg_map.get((t_key, 'log_channel'))
        rev_ping = cfg_map.get((t_key, 'review_ping'))
        log_ping = cfg_map.get((t_key, 'log_ping'))
        ch_text += f"• **{config.LOG_TYPES[t_key]['label']}**:\n"
        ch_text += f"  - Review: <#{rev_ch}> | Ping: <@&{rev_ping}>\n" if rev_ch else "  - Review: Not set\n"
        ch_text += f"  - Log: <#{log_ch}> | Ping: <@&{log_ping}>\n" if log_ch else "  - Log: Not set\n"
    embed.add_field(name="📺 Channels & Pings", value=ch_text or "None", inline=False)
    
    rule_text = ""
    for k in RULE_LABELS: 
        rule_text += f"• `{k}`: {await rule(g, k)}\n"
    embed.add_field(name="📜 Server Automations & Rules", value=rule_text, inline=False)
    
    if cmd_rows:
        cmd_text = "\n".join(f"• `/{c}` 🔒 <@&{r}>" for c, r in cmd_rows)
        embed.add_field(name="🔒 Command Role Restrictions", value=cmd_text, inline=False)
        
    await i.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="sync", description="Sync commands | مزامنة الأوامر")
async def sync_cmd(i: discord.Interaction):
    if not await check_cmd_perm(i, "sync") or not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    ok = await bot.sync_guild(i.guild)
    await i.followup.send("✅ Synced successfully." if ok else "❌ Sync failed.")


POINT_CHOICES = [app_commands.Choice(name=l, value=k) for k, l in config.POINT_LABELS.items()]
RANK_CHOICES = [app_commands.Choice(name=n, value=n) for n, _ in config.LADDER]


@bot.tree.command(name="set-log-points", description="Set log points for this server | تحديد نقاط اللوجات للسيرفر الحالي")
@app_commands.choices(log=POINT_CHOICES)
async def set_log_points(i: discord.Interaction, log: app_commands.Choice[str], points: app_commands.Range[int, 0, 1000]):
    if not await check_cmd_perm(i, "set-log-points") or not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await set_setting(i.guild.id, "points:" + log.value, points)
    await audit(f"⚙️ {i.user.mention} updated log points for **{log.name}** to **{points}**", i.guild)
    await i.response.send_message(f"Done. **{log.name}** = {points} points in this server.", ephemeral=True)


@bot.tree.command(name="set-rank-points", description="Set rank points threshold & sync ranks | تحديد نقاط رتب السلم وتحديث الرتب")
@app_commands.choices(rank=RANK_CHOICES)
async def set_rank_points(i: discord.Interaction, rank: app_commands.Choice[str], points: app_commands.Range[int, 0, 10000]:
    if not await check_cmd_perm(i, "set-rank-points") or not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    await set_setting(i.guild.id, "rank:" + rank.value, points)
    await bulk_update_ranks(i.guild)
    await audit(f"⚙️ {i.user.mention} updated rank threshold for **{rank.name}** to **{points}** and synced all member ranks", i.guild)
    await i.followup.send(f"Done. **{rank.name}** = {points} points in this server, and all member ranks have been re-evaluated.", ephemeral=True)


@bot.tree.command(name="set-points", description="Set member points (logged in audit events) | تحديد نقاط عضو وتوثيقها")
async def set_points(i: discord.Interaction, member: discord.Member, points: app_commands.Range[int, 0, 100000]):
    if not await check_cmd_perm(i, "set-points") or not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    season = await get_current_season(i.guild.id)
    
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
            row = await cur.fetchone()
            old_points = row[0] if row else 0
            delta = points - old_points
            
            await db.execute(
                "INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET points=excluded.points",
                (i.guild.id, member.id, points)
            )
            if delta != 0:
                await db.execute(
                    "INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?,?)",
                    (i.guild.id, None, member.id, delta, now_utc().isoformat(), "manual", season)
                )
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await i.followup.send(f"❌ Database error: {e}", ephemeral=True)
        
    try: 
        await update_member_rank(i.guild, member, points)
    except discord.Forbidden: 
        pass
        
    await audit(f"⚙️ {i.user.mention} manually set points for {member.mention} to **{points}** (Delta: `{delta:+d}`)", i.guild)
    await i.followup.send(f"Done. {member.display_name} = {points} points (Logged in point_events).", ephemeral=True)


@bot.tree.command(name="points", description="Check points | رصيد النقاط")
async def points_cmd(i: discord.Interaction, member: Optional[discord.Member] = None):
    if not await check_cmd_perm(i, "points"):
        return await i.response.send_message("❌ You don't have required role.", ephemeral=True)
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id,))
        row = await cur.fetchone()
    total = row[0] if row else 0
    ladder = await get_ladder(i.guild.id)
    nxt = next(((n, p) for n, p in ladder if p > total), None)
    msg = f"{member.mention}: **{total}** points."
    msg += f"\nNext rank: **{nxt[0]}** ({nxt[1] - total} left)." if nxt else "\nMax rank reached."
    await i.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="leaderboard", description="Top members (All time or Weekly) | المتصدرين")
@app_commands.choices(period=PERIOD_CHOICES)
async def leaderboard(i: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
    if not await check_cmd_perm(i, "leaderboard"):
        return await i.response.send_message("❌ You don't have required role.", ephemeral=True)
    
    p_val = period.value if period else "all"
    season = await get_current_season(i.guild.id)
    async with aiosqlite.connect(DB) as db:
        if p_val == "week":
            week_ago = (now_utc() - timedelta(days=7)).isoformat()
            cur = await db.execute(
                "SELECT user_id, SUM(points) as total FROM point_events "
                "WHERE guild_id=? AND season_id=? AND created_at >= ? GROUP BY user_id ORDER BY total DESC LIMIT 10",
                (i.guild.id, season, week_ago)
            )
        else:
            cur = await db.execute(
                "SELECT user_id, points FROM members WHERE guild_id=? AND points>0 ORDER BY points DESC LIMIT 10",
                (i.guild.id,)
            )
        rows = await cur.fetchall()
        
    if not rows: 
        return await i.response.send_message("No points recorded for this period yet.", ephemeral=True)
        
    title = f"📊 Weekly Leaderboard (Season: {season})" if p_val == "week" else f"🏆 Season Leaderboard ({season})"
    text = f"**{title}**\n\n" + "\n".join(f"**{n}.** <@{u}> — {p} pts" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="rates", description="Show independent server log points & ranks | أسعار النقاط والرتب المستقلة")
async def rates(i: discord.Interaction):
    if not await check_cmd_perm(i, "rates"):
        return await i.response.send_message("❌ You don't have required role.", ephemeral=True)
    lines = [f"⚙️ **Server Rates & Points (Ver {BOT_VERSION})**\n\n**Log points:**"]
    for k, label in config.POINT_LABELS.items():
        val = await get_setting(i.guild.id, 'points:' + k)
        lines.append(f"- {label}: **{val}** pts")
    lines.append("\n**Rank points thresholds:**")
    ladder = await get_ladder(i.guild.id)
    for n, p in ladder: 
        lines.append(f"- {n}: **{p}** pts")
    await i.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="link", description="Link Roblox username | ربط حساب روبلوكس")
async def link(i: discord.Interaction, roblox_username: str):
    if not await check_cmd_perm(i, "link"):
        return await i.response.send_message("❌ You don't have required role.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [roblox_username.strip()], "excludeBanned": False}) as r:
                if r.status != 200:
                    return await i.followup.send("❌ Error connecting to Roblox API.")
                data = await r.json()
        u = (data.get("data") or [None])[0]
        if not u: return await i.followup.send("No Roblox user found.")
        
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT INTO links(guild_id, user_id, roblox_id, roblox_name) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET roblox_id=excluded.roblox_id, roblox_name=excluded.roblox_name",
                (i.guild.id, i.user.id, u["id"], u["name"])
            )
            await db.commit()
        await i.followup.send(f"✅ Linked account to **{u['name']}** for this server.")
    except Exception:
        await i.followup.send("❌ Error connecting to Roblox API (Timeout or network issue).")


@bot.tree.command(name="profile", description="Member profile card | البطاقة الشخصية")
async def profile(i: discord.Interaction, member: Optional[discord.Member] = None):
    if not await check_cmd_perm(i, "profile"):
        return await i.response.send_message("❌ You don't have required role.", ephemeral=True)
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id,))
        row = await cur.fetchone()
        cur = await db.execute("SELECT roblox_name FROM links WHERE guild_id=? AND user_id=?", (i.guild.id, member.id,))
        link_row = await cur.fetchone()
    total = row[0] if row else 0
    e = discord.Embed(title=member.display_name, color=discord.Color.blurple())
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Points", value=str(total))
    if link_row: e.add_field(name="Roblox", value=link_row[0])
    await i.response.send_message(embed=e)


@bot.tree.command(name="stats", description="War stats against crews | إحصائيات الحروب ضد الكروات")
@app_commands.describe(crew="Filter by specific enemy crew name | تصفية حسب اسم الكرو المعادي")
async def stats(i: discord.Interaction, crew: Optional[str] = None):
    if not await check_cmd_perm(i, "stats"):
        return await i.response.send_message("❌ You don't have required role.", ephemeral=True)
    season = await get_current_season(i.guild.id)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT data, result FROM logs WHERE guild_id=? AND type='war' AND status='accepted' AND season_id=?", (i.guild.id, season))
        rows = await cur.fetchall()
        
    if not rows: 
        return await i.response.send_message("No accepted wars logged in current season yet.")
        
    total = 0
    wins = 0
    losses = 0
    target_crew = crew.strip().lower() if crew else None
    
    for r_data, r_res in rows:
        try:
            d = json.loads(r_data)
        except:
            d = {}
        c_name = str(d.get("crew", "")).strip().lower()
        if target_crew and target_crew not in c_name:
            continue
        total += 1
        if r_res == "win":
            wins += 1
        elif r_res == "loss":
            losses += 1
            
    msg = f"📊 **War Statistics (Season: {season})**" + (f" against **{crew}**" if crew else f" in **{i.guild.name}**") + "\n"
    msg += f"• **Total Wars:** {total}\n• **Wins:** {wins} ✅\n• **Losses:** {losses} ❌\n"
    if total > 0:
        wr = (wins / total) * 100
        msg += f"• **Win Rate:** {wr:.1f}%\n"
    await i.response.send_message(msg)


class InactivePaginator(discord.ui.View):
    def __init__(self, items, days):
        super().__init__(timeout=60)
        self.items = items
        self.days = days
        self.page = 0
        self.per_page = 15

    def get_page_embed(self):
        start = self.page * self.per_page
        end = start + self.per_page
        page_items = self.items[start:end]
        max_pages = (len(self.items) - 1) // self.per_page + 1
        
        embed = discord.Embed(
            title=f"⚠️ Inactive Members ({self.days}+ Days)",
            description=f"Total inactive found: **{len(self.items)}** members",
            color=discord.Color.orange()
        )
        lines = [f"• <@{uid}> — **{pts}** pts" for uid, pts in page_items]
        embed.add_field(name=f"Page {self.page + 1} of {max_pages}", value="\n".join(lines) or "None", inline=False)
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="⬅️")
    async def prev_page(self, i: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await i.response.edit_message(embed=self.get_page_embed(), view=self)
        else:
            await i.response.defer()

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="➡️")
    async def next_page(self, i: discord.Interaction, button: discord.ui.Button):
        max_pages = (len(self.items) - 1) // self.per_page + 1
        if self.page < max_pages - 1:
            self.page += 1
            await i.response.edit_message(embed=self.get_page_embed(), view=self)
        else:
            await i.response.defer()


@bot.tree.command(name="inactive", description="List inactive members who haven't logged activity | الأعضاء غير النشطين")
async def inactive(i: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30):
    if not await check_cmd_perm(i, "inactive") or not is_editor(i.user): 
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    
    cutoff = (now_utc() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT user_id, points FROM members WHERE guild_id=? AND user_id NOT IN "
            "(SELECT DISTINCT author_id FROM logs WHERE guild_id=? AND created_at >= ? AND status='accepted' "
            "UNION SELECT DISTINCT user_id FROM point_events WHERE guild_id=? AND created_at >= ?)",
            (i.guild.id, i.guild.id, cutoff, i.guild.id, cutoff)
        )
        rows = await cur.fetchall()
        
    if not rows:
        return await i.followup.send(f"✅ No inactive members found ({days}+ days).", ephemeral=True)
        
    paginator = InactivePaginator(rows, days)
    await i.followup.send(embed=paginator.get_page_embed(), view=paginator, ephemeral=True)


@bot.tree.command(name="season-reset", description="Reset points for new season & clear ranks | بدء موسم جديد وتصفير النقاط والرتب")
async def season_reset(i: discord.Interaction, name: str, confirm: str):
    if not await check_cmd_perm(i, "season-reset") or not is_editor(i.user): 
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    if confirm != "RESET": 
        return await i.response.send_message("Type RESET to confirm.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    
    name = name.strip()
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT 1 FROM season_archive WHERE season=? AND guild_id=? LIMIT 1", (name, i.guild.id))
            if await cur.fetchone():
                await db.rollback()
                return await i.followup.send(f"❌ Season name **'{name}'** already exists in archives! Choose a unique season name.", ephemeral=True)

            new_season_id = f"season_{uuid.uuid4().hex[:6]}"
            
            await db.execute("INSERT INTO season_archive SELECT ?, guild_id, user_id, points FROM members WHERE guild_id=? AND points>0", (name, i.guild.id))
            await db.execute("UPDATE members SET points=0 WHERE guild_id=?", (i.guild.id,))
            
            await db.execute(
                "INSERT INTO settings(guild_id, key, value) VALUES(?, 'state:current_season', ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                (i.guild.id, new_season_id)
            )
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await i.followup.send(f"❌ Season reset failed due to database error: {e}", ephemeral=True)
        
    await bulk_update_ranks(i.guild)
    await audit(f"🎉 {i.user.mention} started new season **{name}** (ID: `{new_season_id}`), archived old points, and reset member scores to 0.", i.guild)
    await i.followup.send(f"✅ Season **{name}** started successfully! Old points archived securely and scores reset to 0.", ephemeral=True)


@bot.tree.command(name="season-top", description="Top 10 of archived season | متصدري موسم سابق")
async def season_top(i: discord.Interaction, name: str):
    if not await check_cmd_perm(i, "season-top"):
        return await i.response.send_message("❌ You don't have required role.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM season_archive WHERE season=? AND guild_id=? ORDER BY points DESC LIMIT 10", (name, i.guild.id))
        rows = await cur.fetchall()
    if not rows: return await i.response.send_message("Season archive not found.")
    text = f"**Archived Season: {name}**\n" + "\n".join(f"**{n}.** <@{u}> — {p}" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


@tasks.loop(minutes=30)
async def maintenance():
    now = now_utc()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        
        # 1. Remind Reviewers
        try:
            cur = await db.execute("SELECT * FROM logs WHERE status='pending'")
            pending_logs = await cur.fetchall()
            for l in pending_logs:
                try:
                    g_id = l["guild_id"]
                    guild = bot.get_guild(g_id)
                    if not guild: continue
                    rem_hrs = await rule(g_id, "reminder_hours")
                    if rem_hrs > 0:
                        created = dt.datetime.fromisoformat(l["created_at"])
                        if (now - created).total_seconds() > (rem_hrs * 3600) and not l["reminded"]:
                            rev_ch_id = await cfg_get(g_id, l["type"], "review_channel")
                            rev_ch = guild.get_channel(rev_ch_id) if rev_ch_id else None
                            if rev_ch:
                                ping_id = await cfg_get(g_id, l["type"], "review_ping")
                                ping_str = f"<@&{ping_id}> " if ping_id else ""
                                await rev_ch.send(f"⏰ {ping_str}Reminder: Log `#{l['id']}` ({l['type']}) has been pending for over {rem_hrs} hours!")
                                await db.execute("UPDATE logs SET reminded=1 WHERE id=?", (l["id"],))
                                await db.commit()
                except Exception as ex:
                    print(f"⚠️ Error reminding log #{l['id']}: {ex}")
        except Exception as e:
            print(f"⚠️ Maintenance Task 1 error: {e}")

        # 2. Automated Scheduled Backups
        for guild in bot.guilds:
            try:
                backup_hrs = await rule(guild.id, "backup_hours")
                if backup_hrs > 0:
                    last_backup_str = await get_setting(guild.id, "state:last_backup")
                    should_backup = False
                    if not last_backup_str:
                        should_backup = True
                    else:
                        try:
                            last_b = dt.datetime.fromisoformat(str(last_backup_str))
                            if (now - last_b).total_seconds() > (backup_hrs * 3600):
                                should_backup = True
                        except:
                            should_backup = True
                    
                    if should_backup:
                        unique_suffix = uuid.uuid4().hex[:8]
                        filename = f"crewbot-auto-backup-{guild.id}-{now.strftime('%Y%m%d-%H%M%S')}-{unique_suffix}.db"
                        path = os.path.join(tempfile.gettempdir(), filename)
                        await asyncio.to_thread(_backup_file, path)
                        
                        success = await dm_owner(f"🗄️ **Scheduled Auto Backup** for **{guild.name}** (`{guild.id}`):", file=discord.File(path, filename=filename))
                        if success:
                            await set_setting(guild.id, "state:last_backup", now.isoformat())
                        
                        if os.path.exists(path):
                            os.unlink(path)
            except Exception as e:
                print(f"⚠️ Auto backup error in guild {guild.id}: {e}")

        # 3. Weekly Leaderboard Post
        for guild in bot.guilds:
            try:
                if await rule(guild.id, "weekly_post") == 1:
                    last_post_str = await get_setting(guild.id, "state:last_weekly_post")
                    week_key = now.strftime("%Y-W%U")
                    if last_post_str != week_key and now.weekday() == 6:
                        log_ch_id = await cfg_get(guild.id, "all", "log_channel")
                        log_ch = guild.get_channel(log_ch_id) if log_ch_id else None
                        if log_ch:
                            season = await get_current_season(guild.id)
                            cur_lb = await db.execute(
                                "SELECT user_id, SUM(points) as total FROM point_events "
                                "WHERE guild_id=? AND season_id=? AND created_at >= ? GROUP BY user_id ORDER BY total DESC LIMIT 10",
                                (guild.id, season, (now - timedelta(days=7)).isoformat())
                            )
                            lb_rows = await cur_lb.fetchall()
                            if lb_rows:
                                text = f"🏆 **Weekly Top 10 Leaderboard ({week_key})**\n\n" + "\n".join(f"**{n}.** <@{u}> — {p} pts" for n, (u, p) in enumerate(lb_rows, 1))
                                await log_ch.send(text)
                                await set_setting(guild.id, "state:last_weekly_post", week_key)
            except Exception as e:
                print(f"⚠️ Weekly leaderboard error in guild {guild.name}: {e}")


@maintenance.before_loop
async def before_maintenance(): 
    await bot.wait_until_ready()

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("❌ DISCORD_TOKEN is missing in environment variables.")
    bot.run(token)