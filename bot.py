import asyncio
from contextlib import asynccontextmanager
import datetime as dt
import hashlib
import io
import json
import os
import re
from pathlib import Path
import sqlite3
import tempfile
import threading
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import config

# Fail early with a clear message if config.py is missing required settings.
_REQUIRED_CONFIG = (
    "DEFAULT_POINTS", "EDITOR_ROLES", "LADDER", "LOG_TYPES",
    "MAX_VIDEO_AGE_DAYS", "POINT_LABELS", "REVIEWER_ROLES", "TRYOUTER_ROLES",
)
_missing_config = [name for name in _REQUIRED_CONFIG if not hasattr(config, name)]
if _missing_config:
    raise RuntimeError(
        "config.py is missing required variables: " + ", ".join(_missing_config)
    )

load_dotenv()

# مسار الداتابيز. على Railway حط Volume واكتب DB_PATH=/data/crewbot.db في Variables
_default_data_dir = Path(__file__).resolve().parent / "data"
_default_data_dir.mkdir(parents=True, exist_ok=True)
if os.getenv("RAILWAY_ENVIRONMENT") and not os.getenv("DB_PATH"):
    raise RuntimeError("Persistent storage required: set DB_PATH=/data/dragonslayerbot.sqlite3 and mount a Railway Volume at /data.")
DB = os.path.abspath(os.getenv("DB_PATH", str(_default_data_dir / "dragonslayerbot.sqlite3")))
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

@asynccontextmanager
async def db_connect():
    """Yield a configured SQLite connection and always close it."""
    db = await aiosqlite.connect(DB, timeout=30)
    try:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=30000")
        yield db
    finally:
        await db.close()


# --- سيرفر ويب مصغر لـ Railway ---
def run_dummy_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"DragonSlayerBot is online!")

        def log_message(self, format, *args):
            pass

    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


threading.Thread(target=run_dummy_server, daemon=True).start()
# ---------------------------------

# ---- قواعد قابلة للتعديل بـ /setrule (القيم هنا افتراضية) ----
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS members(user_id INTEGER PRIMARY KEY, points INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS guild_channels(guild_id INTEGER PRIMARY KEY, review_channel INTEGER, log_channel INTEGER, audit_channel INTEGER);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT, author_id INTEGER, result TEXT, data TEXT, flags TEXT,
    status TEXT DEFAULT 'pending', points INTEGER DEFAULT 0,
    reviewer_id INTEGER, reason TEXT, created_at TEXT
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
    log_id INTEGER, user_id INTEGER, points INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS links(user_id INTEGER PRIMARY KEY, roblox_id INTEGER, roblox_name TEXT);
CREATE TABLE IF NOT EXISTS season_archive(season TEXT, user_id INTEGER, points INTEGER);
CREATE TABLE IF NOT EXISTS guild_settings(
    guild_id INTEGER NOT NULL, key TEXT NOT NULL, value INTEGER NOT NULL,
    PRIMARY KEY(guild_id, key)
);
CREATE TABLE IF NOT EXISTS guild_members(
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, points INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS guild_point_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, log_id INTEGER,
    user_id INTEGER NOT NULL, points INTEGER NOT NULL, created_at TEXT NOT NULL,
    season_id INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(log_id) REFERENCES logs(id)
);
CREATE INDEX IF NOT EXISTS idx_guild_events_guild_user_date ON guild_point_events(guild_id, user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_guild_events_log ON guild_point_events(guild_id, log_id);
CREATE TABLE IF NOT EXISTS guild_season_archive(
    guild_id INTEGER NOT NULL, season TEXT NOT NULL, user_id INTEGER NOT NULL, points INTEGER NOT NULL,
    PRIMARY KEY(guild_id, season, user_id)
);
CREATE TABLE IF NOT EXISTS guild_state(
    guild_id INTEGER NOT NULL, key TEXT NOT NULL, value INTEGER NOT NULL,
    PRIMARY KEY(guild_id, key)
);
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_points_review(
    user_id INTEGER PRIMARY KEY, old_points INTEGER NOT NULL, note TEXT NOT NULL
);
"""

MIGRATIONS = [
    ("logs", "guild_id", "INTEGER"),
    ("logs", "review_channel", "INTEGER"),
    ("logs", "review_msg", "INTEGER"),
    ("logs", "public_channel", "INTEGER"),
    ("logs", "public_msg", "INTEGER"),
    ("logs", "reviewed_at", "TEXT"),
    ("logs", "reminded", "INTEGER DEFAULT 0"),
    ("logs", "season_id", "INTEGER NOT NULL DEFAULT 1"),
]

CHANNEL_FALLBACK = {}
LOG_ID_RE = re.compile(r"Log #(\d+)")


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
        # مسح أي أوامر "عامة" قديمة عشان متتكررش مع أوامر السيرفر
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
        print(f"Bot logged in as {self.user}")
        if not self._synced:
            self._synced = True
            for g in self.guilds:
                await self.sync_guild(g)
        if not maintenance.is_running():
            maintenance.start()

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)


bot = CrewBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    """Centralized, user-friendly handling for slash-command errors."""
    original = getattr(error, "original", error)

    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"الأمر عليه انتظار. جرّب تاني بعد {error.retry_after:.1f} ثانية."
    elif isinstance(error, app_commands.MissingPermissions):
        message = "معندكش الصلاحيات المطلوبة لاستخدام الأمر."
    elif isinstance(error, app_commands.CheckFailure):
        message = "مش مسموح لك تستخدم الأمر ده في السياق الحالي."
    elif isinstance(error, app_commands.TransformerError):
        message = "فيه قيمة مدخلة غير صحيحة. راجع الاختيارات وحاول تاني."
    elif isinstance(original, discord.Forbidden):
        message = "البوت مش معاه الصلاحيات الكافية لتنفيذ الطلب."
    else:
        message = "حصل خطأ غير متوقع أثناء تنفيذ الأمر. جرّب تاني، ولو اتكرر بلّغ إدارة السيرفر."
        print(f"[APP COMMAND ERROR] {type(original).__name__}: {original!r}")

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException as send_error:
        print(f"[APP COMMAND ERROR] Could not send error response: {send_error!r}")


# ================================================================ DB helpers
def now_utc():
    return dt.datetime.now(timezone.utc)


def now_ts():
    return int(now_utc().timestamp())


async def init_db():
    async with db_connect() as db:
        await db.execute("PRAGMA journal_mode=WAL")

        # Older deployments may have a three-column `settings` table.
        # CREATE TABLE IF NOT EXISTS does not change an existing table, so
        # preserve the old table and recreate the two-column schema expected
        # by this version of the bot.
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'")
        settings_exists = await cur.fetchone()
        legacy_settings_table = None
        if settings_exists:
            cur = await db.execute("PRAGMA table_info(settings)")
            settings_columns = [row[1] for row in await cur.fetchall()]
            if settings_columns != ["key", "value"]:
                legacy_settings_table = "settings_legacy_backup"
                suffix = 1
                while True:
                    cur = await db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (legacy_settings_table,),
                    )
                    if not await cur.fetchone():
                        break
                    suffix += 1
                    legacy_settings_table = f"settings_legacy_backup_{suffix}"
                await db.execute(f"ALTER TABLE settings RENAME TO {legacy_settings_table}")

        await db.executescript(SCHEMA)

        # Preserve legacy key/value settings when those columns exist.
        # The full old table remains available as settings_legacy_backup*.
        if legacy_settings_table:
            cur = await db.execute(f"PRAGMA table_info({legacy_settings_table})")
            legacy_columns = [row[1] for row in await cur.fetchall()]
            if "key" in legacy_columns and "value" in legacy_columns:
                await db.execute(
                    f"INSERT OR IGNORE INTO settings(key, value) "
                    f"SELECT key, value FROM {legacy_settings_table}"
                )
        for table, col, decl in MIGRATIONS:
            cur = await db.execute(f"PRAGMA table_info({table})")
            if col not in [r[1] for r in await cur.fetchall()]:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        cur = await db.execute("PRAGMA table_info(guild_point_events)")
        if "season_id" not in [r[1] for r in await cur.fetchall()]:
            await db.execute("ALTER TABLE guild_point_events ADD COLUMN season_id INTEGER NOT NULL DEFAULT 1")
        for k, v in config.DEFAULT_POINTS.items():
            await db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", ("points:" + k, v))
        for name, p in config.LADDER:
            await db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", ("rank:" + name, p))
        # One-time tenant migration after legacy columns have been added.
        cur = await db.execute("SELECT 1 FROM schema_migrations WHERE version=1")
        migrated = await cur.fetchone()
        if not migrated:
            await db.execute("""
                INSERT OR IGNORE INTO guild_point_events(guild_id, log_id, user_id, points, created_at)
                SELECT l.guild_id, e.log_id, e.user_id, e.points, e.created_at
                FROM point_events e JOIN logs l ON l.id=e.log_id
                WHERE l.guild_id IS NOT NULL
            """)
            await db.execute("""
                INSERT OR IGNORE INTO guild_members(guild_id, user_id, points)
                SELECT guild_id, user_id, SUM(points)
                FROM guild_point_events GROUP BY guild_id, user_id
            """)
            await db.execute("""
                INSERT OR IGNORE INTO legacy_points_review(user_id, old_points, note)
                SELECT m.user_id, m.points,
                       'Legacy shared balance retained for manual reconciliation; per-server events were migrated separately.'
                FROM members m
            """)
            # Copy legacy global overrides to each known server once, then all future edits are isolated.
            cur = await db.execute("SELECT DISTINCT guild_id FROM logs WHERE guild_id IS NOT NULL")
            known_guilds = {r[0] for r in await cur.fetchall()}
            for table in ("guild_cfg", "role_perms", "guild_channels"):
                cur = await db.execute(f"SELECT DISTINCT guild_id FROM {table}")
                known_guilds.update(r[0] for r in await cur.fetchall() if r[0] is not None)
            known_guilds.update(g.id for g in bot.guilds)
            cur = await db.execute("SELECT key, value FROM settings")
            legacy_settings = await cur.fetchall()
            for gid in known_guilds:
                for key, value in legacy_settings:
                    if not key.startswith("state:"):
                        await db.execute("INSERT OR IGNORE INTO guild_settings(guild_id,key,value) VALUES(?,?,?)",
                                         (gid, key, value))
            await db.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                             (now_utc().isoformat(),))
        await db.execute("CREATE INDEX IF NOT EXISTS idx_logs_guild_status_created ON logs(guild_id,status,created_at)")
        # ترحيل القنوات من النظام القديم
        cur = await db.execute("SELECT guild_id, review_channel, log_channel, audit_channel FROM guild_channels")
        for g, r, l, a in await cur.fetchall():
            for key, v in (("review_channel", r), ("log_channel", l), ("audit_channel", a)):
                if v:
                    await db.execute("INSERT OR IGNORE INTO guild_cfg VALUES(?,?,?,?)", (g, "all", key, v))
        await db.commit()


async def active_season_id(guild_id):
    value = await get_setting("active_season_id", guild_id)
    return int(value or 1)


async def db_exec(sql, params=()):
    async with db_connect() as db:
        await db.execute(sql, params)
        await db.commit()


async def get_setting(key, guild_id=None):
    """Read only this guild override; global settings are reserved for bot-wide state."""
    async with db_connect() as db:
        if guild_id is not None:
            cur = await db.execute("SELECT value FROM guild_settings WHERE guild_id=? AND key=?",
                                   (guild_id, key))
            row = await cur.fetchone()
            return row[0] if row else None
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
    return row[0] if row else None


async def set_setting(key, value, guild_id=None):
    if guild_id is None:
        await db_exec("INSERT INTO settings(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        return
    async with db_connect() as db:
        await db.execute("INSERT INTO guild_settings(guild_id,key,value) VALUES(?,?,?) "
                         "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
                         (guild_id, key, value))
        await db.commit()


async def rule(name, guild_id=None):
    v = await get_setting("rule:" + name, guild_id)
    return v if v is not None else RULE_DEFAULTS[name]

async def cfg_get(guild_id, log_type, key):
    """قيمة خاصة بنوع اللوج، وإلا القيمة العامة (all)، وإلا القيمة الاحتياطية من config."""
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT value FROM guild_cfg WHERE guild_id=? AND key=? AND log_type IN (?, 'all') "
            "ORDER BY (log_type='all') LIMIT 1", (guild_id, key, log_type))
        row = await cur.fetchone()
    return row[0] if row and row[0] else None


async def cfg_set(guild_id, log_type, key, value):
    async with db_connect() as db:
        if value is None:
            await db.execute("DELETE FROM guild_cfg WHERE guild_id=? AND log_type=? AND key=?",
                             (guild_id, log_type, key))
        else:
            await db.execute(
                "INSERT INTO guild_cfg VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id, log_type, key) DO UPDATE SET value=excluded.value",
                (guild_id, log_type, key, value))
        await db.commit()


async def roles_get(guild_id, log_type, kind):
    async with db_connect() as db:
        for t in (log_type, "all"):
            cur = await db.execute(
                "SELECT role_id FROM role_perms WHERE guild_id=? AND log_type=? AND kind=?",
                (guild_id, t, kind))
            ids = [r[0] for r in await cur.fetchall()]
            if ids:
                return ids
    return []


async def roles_edit(guild_id, log_type, kind, role_id, add: bool):
    async with db_connect() as db:
        if add:
            await db.execute("INSERT OR IGNORE INTO role_perms VALUES(?,?,?,?)",
                             (guild_id, log_type, kind, role_id))
        else:
            await db.execute(
                "DELETE FROM role_perms WHERE guild_id=? AND log_type=? AND kind=? AND role_id=?",
                (guild_id, log_type, kind, role_id))
        await db.commit()


async def get_ladder(guild_id=None):
    ladder = []
    for name, default_points in config.LADDER:
        points = await get_setting("rank:" + name, guild_id)
        ladder.append((name, default_points if points is None else points))
    return ladder


async def get_log(log_id, guild_id=None):
    async with db_connect() as db:
        db.row_factory = aiosqlite.Row
        if guild_id is None:
            cur = await db.execute("SELECT * FROM logs WHERE id=?", (log_id,))
        else:
            cur = await db.execute("SELECT * FROM logs WHERE id=? AND guild_id=?", (log_id, guild_id))
        return await cur.fetchone()


async def points_for(log_type, result, guild_id=None):
    key = f"{log_type}_{result}" if log_type in ("war", "outside") else log_type
    v = await get_setting("points:" + key, guild_id)
    return v if v is not None else config.DEFAULT_POINTS.get(key, 0)


async def recipients_for(row, data):
    """مين ياخد النقط: التراي أوت للـ tester، والحرب لكل اللاعبين المذكورين."""
    if row["type"] == "tryout":
        return [int(data.get("tester_id") or row["author_id"])]
    ids = [row["author_id"]]
    if row["type"] == "war" and await rule("war_participants", row["guild_id"]):
        for x in data.get("participants", []):
            if int(x) not in ids:
                ids.append(int(x))
    return ids


# ================================================================ permissions
def has_role(member, names) -> bool:
    wanted = {str(n).strip().casefold() for n in names}
    return any(r.name.strip().casefold() in wanted for r in getattr(member, "roles", []))


def is_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return bool(member.guild_permissions.administrator or member.guild_permissions.manage_guild)


def is_editor(member) -> bool:
    return isinstance(member, discord.Member) and (is_admin(member) or has_role(member, config.EDITOR_ROLES))


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
    return False


# ================================================================ misc helpers
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


async def dm_owner(text):
    if not owner_id():
        return
    try:
        user = await bot.fetch_user(owner_id())
        await user.send(text)
    except (discord.HTTPException, discord.NotFound):
        pass


async def get_member(guild, user_id):
    return guild.get_member(user_id) or await guild.fetch_member(user_id)


def parse_log_id(message: discord.Message):
    m = LOG_ID_RE.search(message.content or "")
    return int(m.group(1)) if m else None


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
    # Exact names avoid accidentally binding two ranks to similarly named roles.
    role_by_name = {r.name.strip().casefold(): r for r in guild.roles}
    return [role_by_name.get(n.strip().casefold()) for n, _ in config.LADDER]


# ================================================================ فحص الملفات
async def video_age_days(data: bytes) -> Optional[int]:
    """Return video age in days, or None when metadata cannot be read.

    ffprobe is optional: missing binaries, malformed metadata, and timeouts
    must never stop log submission.
    """
    path = None
    proc = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(data)
            path = f.name

        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_entries", "format_tags=creation_time", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return None

        metadata = json.loads(out or b"{}")
        created = metadata.get("format", {}).get("tags", {}).get("creation_time")
        if not created:
            return None

        created_at = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return max(0, (now_utc() - created_at).days)

    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
        return None
    except (FileNotFoundError, PermissionError, OSError, ValueError, TypeError):
        # ffprobe is not installed or the file has no usable metadata.
        return None
    finally:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


async def inspect_blobs(blobs, guild_id):
    hashes, flags = [], []
    for att, data in blobs:
        h = hashlib.sha256(data).hexdigest()
        hashes.append(h)
        async with db_connect() as db:
            cur = await db.execute(
                "SELECT h.log_id FROM hashes h JOIN logs l ON l.id=h.log_id "
                "WHERE h.hash=? AND l.guild_id=? AND l.status NOT IN ('rejected','delivery_failed') LIMIT 1", (h, guild_id))
            row = await cur.fetchone()
        if row:
            flags.append(f"{att.filename}: same file was used in Log #{row[0]}")
        if (att.content_type or "").startswith("video"):
            age = await video_age_days(data)
            if age is not None and age > config.MAX_VIDEO_AGE_DAYS:
                flags.append(f"{att.filename}: video is {age} days old")
    return hashes, flags


# ================================================================ شكل اللوج
DIVIDER = ("<:EU1:1449430079611600936> ●▬▬▬▬▬▬▬▬▬▬▬☭  <:Grok2:1513556052036030584> "
           "☭▬▬▬▬▬▬▬▬▬▬▬● <:EU1:1449430079611600936>")
ANGEL = "<:celestial_angel:1455574559062360198>"
MOON = "<:Whitemoon:1458484853640138784>"
ARROW = "<:f_arrows:1364977170140893315>"
CROWN = "<:WhiteCrown:1458484851614416968>"


def clip(v, n=350):
    v = str(v) if v not in (None, "") else "-"
    return v if len(v) <= n else v[: n - 1] + "…"


def render(log_id, log_type, author_id, data, result, flags=None, status="pending",
           points=None, reviewer_id=None, reason=None, pts_to=None):
    label = config.LOG_TYPES[log_type]["label"]
    d = lambda k, default="-": clip(data.get(k, default))  # noqa: E731
    m = f"{ANGEL}  **{label}**  {ANGEL}  `Log #{log_id}`\n\n{DIVIDER}\n\n"
    m += f"{MOON} **Logged by:** <@{author_id}>\n"

    if log_type == "war":
        m += f"{MOON} **Dragon Slayer's Fighters:**\n{ARROW}  {d('my_team')}\n\n{DIVIDER}\n\n"
        m += f"{MOON} **{clip(data.get('crew', 'Enemy Crew'), 60)} Fighters:**\n{ARROW}  {d('their_team')}\n\n"
    elif log_type == "outside":
        m += f"{MOON} **Dragon Slayer's Fighter:**\n{ARROW}  {d('my_fighter')}\n\n{DIVIDER}\n\n"
        m += f"{MOON} **Opponent:**\n{ARROW}  {d('opponent')}\n\n"
    elif log_type == "internal":
        m += f"{MOON} **Fighter 1:**\n{ARROW}  {d('p1')}\n\n{DIVIDER}\n\n"
        m += f"{MOON} **Fighter 2:**\n{ARROW}  {d('p2')}\n\n"
    elif log_type == "tryout":
        tester = data.get("tester_id") or author_id
        m += f"{MOON} **Tested by:** <@{tester}>\n"
        cand = data.get("candidate", "-")
        if data.get("roblox"):
            cand += f" ({clip(data['roblox'], 40)})"
        m += f"{MOON} **Tryout Candidate:**\n{ARROW}  {cand}\n\n"

    m += f"{DIVIDER}\n\n"
    if result:
        labels = TRYOUT_LABEL if log_type == "tryout" else RESULT_LABEL
        m += f"      {CROWN} **Result:** {labels.get(result, result)}\n"
    m += f"      • **Score:** {d('score')}\n"
    if data.get("notes"):
        m += f"      • **Notes:** {d('notes')}\n"

    m += f"\n**Status:** {STATUS_LABEL[status]}"
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


# ================================================================ الترقية
async def apply_promotion(guild, member, total):
    ladder = await get_ladder(guild.id)
    roles = ladder_roles(guild)
    target = max((idx for idx, (_, pts) in enumerate(ladder) if total >= pts), default=-1)
    current_roles = [r for r in roles if r and r in member.roles]
    current = max((roles.index(r) for r in current_roles), default=-1)
    if current == target and len(current_roles) <= 1:
        return roles[target] if target >= 0 else None

    target_role = roles[target] if target >= 0 else None
    if target >= 0 and target_role is None:
        return None
    if target_role is not None and target_role not in member.roles:
        await member.add_roles(target_role, reason="Automatic point rank recalculation")
    obsolete = [r for r in current_roles if r != target_role]
    if obsolete:
        await member.remove_roles(*obsolete, reason="Automatic point rank recalculation")

    ch_id = await cfg_get(guild.id, "all", "log_channel")
    ch = guild.get_channel(ch_id) if ch_id else None
    if target_role:
        message = f"🎉 {member.mention} has been promoted to {target_role.mention}!"
    elif current_roles:
        message = f"↘️ {member.mention}'s rank was recalculated after a points change."
    else:
        return None
    if ch:
        await ch.send(message, allowed_mentions=discord.AllowedMentions(users=[member], roles=False))
    try:
        rank_text = target_role.name if target_role else "no automatic rank"
        await member.send(f"Your rank in **{guild.name}** was recalculated: **{rank_text}**.")
    except discord.HTTPException:
        pass
    return target_role

# ================================================================ المراجعة
async def accept_log(i: discord.Interaction):
    log_id = parse_log_id(i.message)
    row = await get_log(log_id, i.guild.id if i.guild else None) if log_id else None
    if not row:
        return await i.response.send_message("Log not found in database.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
        return await i.response.send_message("You don't have permission to review this log.", ephemeral=True)
    if row["author_id"] == i.user.id:
        return await i.response.send_message("You cannot review your own log.", ephemeral=True)
    await i.response.defer()

    data = json.loads(row["data"])
    flags = json.loads(row["flags"] or "[]")
    pts = await points_for(row["type"], row["result"], i.guild.id)
    recips = await recipients_for(row, data)
    stamp = now_utc().isoformat()

    totals = {}
    async with db_connect() as db:
        cur = await db.execute(
            "UPDATE logs SET status='accepted', points=?, reviewer_id=?, reviewed_at=? "
            "WHERE id=? AND guild_id=? AND status='pending'", (pts, i.user.id, stamp, log_id, i.guild.id))
        if cur.rowcount == 0:
            await db.commit()
            return await i.followup.send("This log has already been reviewed.", ephemeral=True)
        for uid in recips:
            await db.execute(
                "INSERT INTO guild_members(guild_id, user_id, points) VALUES(?,?,?) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET points = points + excluded.points",
                (i.guild.id, uid, pts))
            await db.execute(
                "INSERT INTO guild_point_events(guild_id, log_id, user_id, points, created_at, season_id) VALUES(?,?,?,?,?,?)",
                (i.guild.id, log_id, uid, pts, stamp, row["season_id"] or 1))
            cur = await db.execute("SELECT points FROM guild_members WHERE guild_id=? AND user_id=?", (i.guild.id, uid))
            totals[uid] = (await cur.fetchone())[0]
        await db.commit()

    # نشر اللوج في القناة العامة
    pub_id = await cfg_get(i.guild.id, row["type"], "log_channel")
    log_ch = i.guild.get_channel(pub_id) if pub_id else None
    if log_ch:
        ping_id = await cfg_get(i.guild.id, row["type"], "log_ping")
        text = render(log_id, row["type"], row["author_id"], data, row["result"], None,
                      "accepted", pts, i.user.id, pts_to=recips)
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
                      "accepted", pts, i.user.id, pts_to=recips)
    await i.message.edit(content=reviewed, view=None, allowed_mentions=discord.AllowedMentions.none())

    # ترقيات
    for uid, total in totals.items():
        try:
            member = await get_member(i.guild, uid)
            await apply_promotion(i.guild, member, total)
        except (discord.NotFound, discord.Forbidden) as e:
            await i.followup.send(f"Log accepted, but promotion failed for <@{uid}>: {e}", ephemeral=True)

    # تراي أوت ناجح: رول Crew تلقائي
    if (row["type"] == "tryout" and row["result"] == "win" and data.get("candidate_id")
            and await rule("tryout_autorole", i.guild.id)):
        try:
            cand_member = await get_member(i.guild, int(data["candidate_id"]))
            crew_role = find_crew_role(i.guild)
            if crew_role and crew_role not in cand_member.roles:
                await cand_member.add_roles(crew_role, reason="Passed tryout")
        except (discord.NotFound, discord.Forbidden) as e:
            await i.followup.send(f"Accepted, but couldn't give the Crew role: {e}", ephemeral=True)


class RejectModal(discord.ui.Modal, title="Rejection Reason"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, log_id, message):
        super().__init__()
        self.log_id = log_id
        self.message = message

    async def on_submit(self, i: discord.Interaction):
        row = await get_log(self.log_id, i.guild.id if i.guild else None)
        if not row or not i.guild or not isinstance(i.user, discord.Member):
            return await i.response.send_message("Log not found in this server.", ephemeral=True)
        if not await can_review(i.user, row["type"]):
            return await i.response.send_message("You no longer have permission to review this log.", ephemeral=True)
        if row["author_id"] == i.user.id:
            return await i.response.send_message("You cannot review your own log.", ephemeral=True)
        async with db_connect() as db:
            cur = await db.execute(
                "UPDATE logs SET status='rejected', reviewer_id=?, reason=?, reviewed_at=? "
                "WHERE id=? AND guild_id=? AND status='pending'",
                (i.user.id, self.reason.value, now_utc().isoformat(), self.log_id, i.guild.id))
            await db.commit()
        if cur.rowcount == 0:
            return await i.response.send_message("This log has already been reviewed.", ephemeral=True)

        data = json.loads(row["data"])
        flags = json.loads(row["flags"] or "[]")
        text = render(self.log_id, row["type"], row["author_id"], data, row["result"], flags,
                      "rejected", reviewer_id=i.user.id, reason=self.reason.value)
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
        row = await get_log(log_id, i.guild.id if i.guild else None) if log_id else None
        if not row:
            return await i.response.send_message("Log not found in database.", ephemeral=True)
        if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
            return await i.response.send_message("You don't have permission to review this log.", ephemeral=True)
        if row["author_id"] == i.user.id:
            return await i.response.send_message("You cannot review your own log.", ephemeral=True)
        await i.response.send_modal(RejectModal(log_id, i.message))


# ================================================================ إنشاء اللوج
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
        blobs = [(a, await a.read()) for a in self.files]
        hashes, flags = await inspect_blobs(blobs, i.guild.id)
        data = {k: v.value.strip() for k, v in self.inputs.items()}
        data.update(self.extra)

        async with db_connect() as db:
            cur = await db.execute(
                "INSERT INTO logs(type, author_id, result, data, flags, created_at, guild_id, season_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False),
                 json.dumps(flags, ensure_ascii=False), now_utc().isoformat(), i.guild.id,
                 await active_season_id(i.guild.id)))
            log_id = cur.lastrowid
            for h in hashes:
                await db.execute("INSERT OR IGNORE INTO hashes VALUES(?,?)", (h, log_id))
            await db.commit()

        ch_id = await cfg_get(i.guild.id, self.log_type, "review_channel")
        ch = i.guild.get_channel(ch_id) if ch_id else None
        ping_id = await cfg_get(i.guild.id, self.log_type, "review_ping")
        text = render(log_id, self.log_type, i.user.id, data, self.result, flags)
        content = (f"<@&{ping_id}>\n" if ping_id else "") + text
        am = mentions(ping_id)

        async def fail(reason):
            await db_exec("UPDATE logs SET status='delivery_failed', reason=? WHERE id=? AND guild_id=?",
                          ("failed to post for review", log_id, i.guild.id))
            await i.followup.send(
                f"❌ مقدرتش أبعت اللوج لقناة المراجعة ({reason}). اتأكد إن الإدارة حددت القناة بـ /setchannel وإن البوت يقدر يكتب فيها.",
                ephemeral=True)

        if not ch:
            return await fail("القناة مش متحددة")
        sent = None
        try:
            try:
                sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am,
                                     files=[discord.File(io.BytesIO(b), a.filename) for a, b in blobs])
            except discord.Forbidden:
                raise
            except discord.HTTPException:
                # غالبًا الملفات أكبر من حد الرفع، نبعت روابط بدل الملفات
                sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am)
                await ch.send("**Attachments (links expire after a while):**\n" + "\n".join(a.url for a, _ in blobs),
                              allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            return await fail(str(e))

        await db_exec("UPDATE logs SET review_channel=?, review_msg=? WHERE id=?", (ch.id, sent.id, log_id))
        if flags:
            await dm_owner(f"⚠️ {i.user.mention} submitted Log #{log_id} with warnings:\n" + "\n".join(flags))
        await i.followup.send(f"✅ Log #{log_id} submitted for review.", ephemeral=True)


async def start_log(i: discord.Interaction, log_type, result, files, extra=None):
    files = [f for f in files if f]
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message("Use this command inside the server.", ephemeral=True)
    if not await can_submit(i.user, log_type):
        return await i.response.send_message("You don't have permission to submit this log type.", ephemeral=True)
    if log_type == "war" and not any((f.content_type or "").startswith("video") for f in files):
        return await i.response.send_message("War log requires at least one video attachment.", ephemeral=True)
    limit = await rule("daily_limit", i.guild.id)
    if limit:
        since = (now_utc() - timedelta(hours=24)).isoformat()
        async with db_connect() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? "
                "AND status IN ('pending','accepted')", (i.guild.id, i.user.id, since))
            n = (await cur.fetchone())[0]
        if n >= limit:
            return await i.response.send_message(
                f"وصلت للحد الأقصى ({limit} لوجات كل 24 ساعة). جرّب بعدين.", ephemeral=True)
    rid = await cfg_get(i.guild.id, log_type, "review_channel")
    if not rid or not i.guild.get_channel(rid):
        return await i.response.send_message(
            "قناة المراجعة مش متحددة لنوع اللوج ده. اطلب من الإدارة تستخدم /setchannel.", ephemeral=True)
    await i.response.send_modal(LogModal(log_type, result, files, extra or {}))


@bot.tree.command(name="log-war", description="Log a Crew War (video required)")
@app_commands.choices(result=RESULT_CHOICES)
@app_commands.describe(player1="لاعب معاك في الحرب (ياخد نقط)", player2="لاعب تاني", player3="لاعب تالت",
                       player4="لاعب رابع")
async def log_war(i: discord.Interaction, result: app_commands.Choice[str],
                  video1: discord.Attachment, video2: Optional[discord.Attachment] = None,
                  video3: Optional[discord.Attachment] = None,
                  player1: Optional[discord.Member] = None, player2: Optional[discord.Member] = None,
                  player3: Optional[discord.Member] = None, player4: Optional[discord.Member] = None):
    ids = []
    for p in (player1, player2, player3, player4):
        if p and not p.bot and p.id != i.user.id and p.id not in ids:
            ids.append(p.id)
    await start_log(i, "war", result.value, [video1, video2, video3], {"participants": ids})


@bot.tree.command(name="log-outside", description="Log a 1v1 against an outside/famous player")
@app_commands.choices(result=RESULT_CHOICES)
async def log_outside(i: discord.Interaction, result: app_commands.Choice[str],
                      proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None):
    await start_log(i, "outside", result.value, [proof1, proof2])


@bot.tree.command(name="log-internal", description="Log an internal fight between crew members")
async def log_internal(i: discord.Interaction, proof1: discord.Attachment,
                       proof2: Optional[discord.Attachment] = None):
    await start_log(i, "internal", None, [proof1, proof2])


@bot.tree.command(name="log-tryout", description="Log a tryout (mention the candidate)")
@app_commands.choices(result=TRYOUT_CHOICES)
@app_commands.describe(candidate="اللي اتعمله التراي أوت (منشن)", result="نجح ولا فشل",
                       proof="صورة الشات فيها السكور",
                       tester="اللي عمل التراي أوت وهياخد النقط (الافتراضي: أنت)")
async def log_tryout(i: discord.Interaction, candidate: discord.Member,
                     result: app_commands.Choice[str], proof: discord.Attachment,
                     tester: Optional[discord.Member] = None):
    tester = tester or i.user
    if tester.id != i.user.id and not await can_submit(tester, "tryout"):
        return await i.response.send_message("The selected tester is not configured as an authorized tryout submitter.", ephemeral=True)
    extra = {"candidate_id": candidate.id, "candidate": candidate.mention, "tester_id": tester.id}
    await start_log(i, "tryout", result.value, [proof], extra)


@bot.tree.command(name="resubmit", description="Fix and resend a rejected log")
async def resubmit(i: discord.Interaction, log_id: int):
    row = await get_log(log_id, i.guild.id if i.guild else None)
    if not row or row["author_id"] != i.user.id:
        return await i.response.send_message("مش لاقي لوج بالرقم ده ليك.", ephemeral=True)
    if row["status"] != "rejected" or row["type"] not in config.LOG_TYPES:
        return await i.response.send_message("اللوج ده مش مرفوض، فمينفعش يتعاد.", ephemeral=True)
    if not i.guild or not isinstance(i.user, discord.Member) or not await can_submit(i.user, row["type"]):
        return await i.response.send_message("You don't have permission to submit this log type.", ephemeral=True)
    files = []
    try:
        ch = i.guild.get_channel(row["review_channel"]) if row["review_channel"] else None
        if ch and row["review_msg"]:
            old = await ch.fetch_message(row["review_msg"])
            files = list(old.attachments)
    except discord.HTTPException:
        pass
    if not files:
        return await i.response.send_message(
            "مقدرتش أجيب الملفات القديمة. اعمل اللوج من الأول بالأمر العادي.", ephemeral=True)
    data = json.loads(row["data"])
    extra = {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants") if k in data}
    await i.response.send_modal(LogModal(row["type"], row["result"], files, extra, defaults=data))


# ================================================================ مراجعة: revoke / pending
@bot.tree.command(name="revoke-log", description="Cancel an accepted log and take its points back")
async def revoke_log(i: discord.Interaction, log_id: int, reason: str):
    row = await get_log(log_id, i.guild.id if i.guild else None)
    if not row or row["status"] != "accepted":
        return await i.response.send_message("مفيش لوج مقبول بالرقم ده.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
        return await i.response.send_message("❌ مش معاك صلاحية.", ephemeral=True)
    await i.response.defer(ephemeral=True)

    async with db_connect() as db:
        cur = await db.execute("SELECT user_id, points FROM guild_point_events WHERE guild_id=? AND log_id=?", (i.guild.id, log_id))
        events = await cur.fetchall()
        current_season = await active_season_id(i.guild.id)
        if int(row["season_id"] or 1) == current_season:
            for uid, p in events:
                await db.execute("UPDATE guild_members SET points = MAX(0, points - ?) WHERE guild_id=? AND user_id=?", (p, i.guild.id, uid))
        await db.execute("DELETE FROM guild_point_events WHERE guild_id=? AND log_id=?", (i.guild.id, log_id))
        await db.execute("UPDATE logs SET status='revoked', reason=?, reviewer_id=? WHERE id=? AND guild_id=?",
                         (reason, i.user.id, log_id, i.guild.id))
        await db.commit()

    if int(row["season_id"] or 1) == current_season:
        for uid, _ in events:
            try:
                member_obj = await get_member(i.guild, uid)
                async with db_connect() as db:
                    cur = await db.execute("SELECT points FROM guild_members WHERE guild_id=? AND user_id=?",
                                           (i.guild.id, uid))
                    balance_row = await cur.fetchone()
                await apply_promotion(i.guild, member_obj, balance_row[0] if balance_row else 0)
            except (discord.NotFound, discord.Forbidden):
                pass

    data = json.loads(row["data"])
    text = render(log_id, row["type"], row["author_id"], data, row["result"], None,
                  "revoked", reviewer_id=i.user.id, reason=reason)
    for ch_id, msg_id in ((row["review_channel"], row["review_msg"]),
                          (row["public_channel"], row["public_msg"])):
        if ch_id and msg_id:
            try:
                ch = i.guild.get_channel(ch_id)
                msg = await ch.fetch_message(msg_id)
                await msg.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, AttributeError):
                pass
    try:
        author = await bot.fetch_user(row["author_id"])
        await author.send(f"🚫 Log #{log_id} was revoked and its points were removed.\nReason: {reason}")
    except discord.HTTPException:
        pass
    await audit(f"🚫 {i.user.mention} revoked Log #{log_id} (points taken back from "
                f"{len(events)} member(s)). Reason: {reason}", i.guild)
    await i.followup.send(
        f"تم إلغاء اللوج #{log_id} وخصم نقطه. (الرتب مش بتتنزّل تلقائيًا، لو محتاج غيّرها بإيدك.)",
        ephemeral=True)


@bot.tree.command(name="pending", description="List logs waiting for review")
async def pending(i: discord.Interaction):
    if not isinstance(i.user, discord.Member) or not (is_editor(i.user) or await can_review_any(i.user)):
        return await i.response.send_message("❌ مش معاك صلاحية.", ephemeral=True)
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT id, type, author_id, created_at, review_channel, review_msg FROM logs "
            "WHERE status='pending' AND guild_id=? ORDER BY id LIMIT 20", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("مفيش لوجات مستنية مراجعة ✅", ephemeral=True)
    lines = []
    for lid, t, author, created, ch_id, msg_id in rows:
        link = f"https://discord.com/channels/{i.guild.id}/{ch_id}/{msg_id}" if ch_id and msg_id else ""
        lines.append(f"**#{lid}** {config.LOG_TYPES[t]['label']} — <@{author}> — {str(created)[:16]} {link}")
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())


# ================================================================ أوامر الإعدادات
NO_PERM = "❌ مش معاك صلاحية."


@bot.tree.command(name="setchannel", description="حدد قناة الريفيو/اللوج/الأوديت (سيب channel فاضي للمسح)")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_CHANNEL)
async def setchannel(i: discord.Interaction, log_type: app_commands.Choice[str],
                     kind: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    t = "all" if kind.value == "audit_channel" else log_type.value
    await cfg_set(i.guild.id, t, kind.value, channel.id if channel else None)
    if channel:
        perms = channel.permissions_for(i.guild.me)
        warn = "" if perms.send_messages else "\n⚠️ البوت مش معاه صلاحية الكتابة في القناة دي!"
        msg = f"✅ **{kind.name}** ({t}) = {channel.mention}{warn}"
        await audit(f"⚙️ {i.user.mention} set {kind.value} ({t}) to {channel.mention}", i.guild)
    else:
        msg = f"🗑️ اتمسح **{kind.name}** ({t})."
        await audit(f"⚙️ {i.user.mention} cleared {kind.value} ({t})", i.guild)
    await i.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="setping", description="حدد الرتبة اللي تتمنشن (سيب role فاضي للمسح)")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_PING)
async def setping(i: discord.Interaction, log_type: app_commands.Choice[str],
                  kind: app_commands.Choice[str], role: Optional[discord.Role] = None):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    await cfg_set(i.guild.id, log_type.value, kind.value, role.id if role else None)
    await audit(f"⚙️ {i.user.mention} set {kind.value} ({log_type.value}) to {role.mention if role else 'none'}", i.guild)
    msg = f"✅ **{kind.name}** ({log_type.name}) = {role.mention}" if role else f"🗑️ اتمسح **{kind.name}** ({log_type.name})."
    await i.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="setroles", description="حدد الرتب اللي تقبل/ترفض أو تسجّل كل نوع لوج")
@app_commands.choices(kind=KIND_ROLES, log_type=LOG_TYPE_CHOICES, action=ACTIONS)
async def setroles(i: discord.Interaction, kind: app_commands.Choice[str],
                   log_type: app_commands.Choice[str], action: app_commands.Choice[str], role: discord.Role):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    await roles_edit(i.guild.id, log_type.value, kind.value, role.id, action.value == "add")
    await audit(f"⚙️ {i.user.mention} {action.value} {role.mention} → {kind.value} ({log_type.value})", i.guild)
    verb = "اتضافت" if action.value == "add" else "اتشالت"
    await i.response.send_message(f"✅ {role.mention} {verb} من **{kind.name}** ({log_type.name}).", ephemeral=True)


@bot.tree.command(name="setrule", description="غيّر قاعدة من قواعد البوت (حد يومي، تذكير، باكاب...)")
@app_commands.choices(rule_name=RULE_CHOICES)
async def setrule(i: discord.Interaction, rule_name: app_commands.Choice[str],
                  value: app_commands.Range[int, 0, 1000]):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    old = await rule(rule_name.value, i.guild.id)
    await set_setting("rule:" + rule_name.value, value, i.guild.id)
    await audit(f"⚙️ {i.user.mention} changed rule **{rule_name.value}** from {old} to {value}", i.guild)
    await i.response.send_message(f"✅ **{rule_name.value}** = {value}", ephemeral=True)


@bot.tree.command(name="settings", description="اعرض كل إعدادات القنوات والمنشنات والرتب")
async def settings_cmd(i: discord.Interaction):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    g = i.guild.id
    async with db_connect() as db:
        cur = await db.execute("SELECT log_type, key, value FROM guild_cfg WHERE guild_id=?", (g,))
        cfg = {(t, k): v for t, k, v in await cur.fetchall()}
        cur = await db.execute("SELECT log_type, kind, role_id FROM role_perms WHERE guild_id=?", (g,))
        perms = {}
        for t, k, r in await cur.fetchall():
            perms.setdefault((t, k), []).append(r)

    def chn(t, k):
        v = cfg.get((t, k))
        return f"<#{v}>" if v else "—"

    def rol(t, k):
        v = cfg.get((t, k))
        return f"<@&{v}>" if v else "—"

    def rols(t, k):
        return " ".join(f"<@&{r}>" for r in perms.get((t, k), [])) or "—"

    lines = [f"**Audit:** {chn('all', 'audit_channel')}"]
    for t in ["all", *config.LOG_TYPES]:
        title = "Default (all)" if t == "all" else config.LOG_TYPES[t]["label"]
        lines.append(
            f"\n**{title}**\nReview: {chn(t, 'review_channel')} | Log: {chn(t, 'log_channel')}\n"
            f"Ping review: {rol(t, 'review_ping')} | Ping log: {rol(t, 'log_ping')}\n"
            f"Reviewers: {rols(t, 'review')}\nSubmitters: {rols(t, 'submit')}")
    lines.append("\n**Rules:**")
    for k in RULE_LABELS:
        lines.append(f"- {k}: {await rule(k, i.guild.id)}")
    lines.append("\n— = مش متحدد (بياخد من Default)")
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@bot.tree.command(name="sync", description="اعمل مزامنة للأوامر في السيرفر ده (لو أمر مش ظاهر)")
async def sync_cmd(i: discord.Interaction):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    await i.response.defer(ephemeral=True)
    ok = await bot.sync_guild(i.guild)
    await i.followup.send("✅ اتعملت مزامنة. اقفل ديسكورد وافتحه (أو Ctrl+R)." if ok else "❌ فشلت المزامنة، شوف الـ Logs.")


# ================================================================ النقط
POINT_CHOICES = [app_commands.Choice(name=l, value=k) for k, l in config.POINT_LABELS.items()]
RANK_CHOICES = [app_commands.Choice(name=n, value=n) for n, _ in config.LADDER]


@bot.tree.command(name="set-log-points", description="Set points for a log type")
@app_commands.choices(log=POINT_CHOICES)
async def set_log_points(i: discord.Interaction, log: app_commands.Choice[str],
                         points: app_commands.Range[int, 0, 1000]):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    old = await get_setting("points:" + log.value, i.guild.id)
    await set_setting("points:" + log.value, points, i.guild.id)
    await audit(f"✏️ {i.user.mention} changed points for **{log.name}** from {old} to {points}", i.guild)
    await i.response.send_message(f"Done. **{log.name}** = {points} points (new logs only).", ephemeral=True)


@bot.tree.command(name="set-rank-points", description="Set required points for a rank")
@app_commands.choices(rank=RANK_CHOICES)
async def set_rank_points(i: discord.Interaction, rank: app_commands.Choice[str],
                          points: app_commands.Range[int, 0, 100000]):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    old = await get_setting("rank:" + rank.value, i.guild.id)
    await set_setting("rank:" + rank.value, points, i.guild.id)
    await audit(f"✏️ {i.user.mention} changed rank **{rank.name}** from {old} to {points}", i.guild)
    await i.response.send_message(f"Done. **{rank.name}** requires {points} points.", ephemeral=True)


@bot.tree.command(name="set-points", description="Set a member's points balance")
async def set_points(i: discord.Interaction, member: discord.Member,
                     points: app_commands.Range[int, 0, 100000]):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    await i.response.defer(ephemeral=True)
    async with db_connect() as db:
        cur = await db.execute("SELECT points FROM guild_members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        row = await cur.fetchone()
        old = row[0] if row else 0
        await db.execute(
            "INSERT INTO guild_members(guild_id, user_id, points) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET points=excluded.points", (i.guild.id, member.id, points))
        delta = points - old
        if delta:
            await db.execute("INSERT INTO guild_point_events(guild_id, log_id, user_id, points, created_at, season_id) "
                             "VALUES(?,?,?,?,?,?)", (i.guild.id, None, member.id, delta, now_utc().isoformat(),
                                                        await active_season_id(i.guild.id)))
        await db.commit()
    await audit(f"✏️ {i.user.mention} changed {member.mention}'s points from {old} to {points}", i.guild)
    try:
        await apply_promotion(i.guild, member, points)
    except discord.Forbidden:
        pass
    await i.followup.send(f"Done. {member.display_name} = {points} points.", ephemeral=True)


@bot.tree.command(name="points", description="Check your points or someone else's")
async def points_cmd(i: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or i.user
    async with db_connect() as db:
        cur = await db.execute("SELECT points FROM guild_members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        row = await cur.fetchone()
    total = row[0] if row else 0
    nxt = next(((n, p) for n, p in await get_ladder(i.guild.id) if p > total), None)
    msg = f"{member.mention}: **{total}** points."
    msg += f"\nNext rank: **{nxt[0]}** ({nxt[1] - total} left)." if nxt else "\nMax auto rank reached. Further promotions are manual."
    await i.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())


async def top_events(guild_id, since_iso, types=None, limit=10):
    current_season = await active_season_id(guild_id)
    q = ("SELECT e.user_id, SUM(e.points) AS s FROM guild_point_events e "
         "JOIN logs l ON l.id=e.log_id WHERE e.guild_id=? AND e.season_id=? AND e.created_at>=?")
    params = [guild_id, current_season, since_iso]
    if types:
        q += " AND l.type IN (%s)" % ",".join("?" * len(types))
        params += list(types)
    q += " GROUP BY e.user_id ORDER BY s DESC LIMIT ?"
    params.append(limit)
    async with db_connect() as db:
        cur = await db.execute(q, params)
        return await cur.fetchall()


async def weekly_text(guild_id):
    since = (now_utc() - timedelta(days=7)).isoformat()
    sections = [("🏆 Top points", None, 10), ("⚔️ Wars", ["war"], 3),
                ("🥊 Fights", ["outside", "internal"], 3), ("🎯 Tryouts", ["tryout"], 3)]
    lines = ["**📅 Weekly leaderboard (last 7 days)**"]
    for title, types, n in sections:
        rows = await top_events(guild_id, since, types, n)
        if rows:
            lines.append(f"\n**{title}**")
            lines += [f"**{k}.** <@{u}> — {p}" for k, (u, p) in enumerate(rows, 1)]
    return "\n".join(lines)[:1990] if len(lines) > 1 else None


@bot.tree.command(name="leaderboard", description="Top members by points")
@app_commands.choices(period=PERIOD_CHOICES)
async def leaderboard(i: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
    if period and period.value == "week":
        text = await weekly_text(i.guild.id)
        if not text:
            return await i.response.send_message("No points this week yet.")
        return await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())
    async with db_connect() as db:
        cur = await db.execute("SELECT user_id, points FROM guild_members WHERE guild_id=? AND points>0 ORDER BY points DESC LIMIT 10", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("No points yet.")
    text = "\n".join(f"**{n}.** <@{u}> — {p}" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="rates", description="Show current log points, ranks and rules")
async def rates(i: discord.Interaction):
    lines = ["**Log points:**"]
    for k, label in config.POINT_LABELS.items():
        value = await get_setting("points:" + k, i.guild.id)
        if value is None:
            value = config.DEFAULT_POINTS.get(k, 0)
        lines.append(f"- {label}: {value}")
    lines.append("\n**Rank points:**")
    for n, p in await get_ladder(i.guild.id):
        lines.append(f"- {n}: {p}")
    lines.append("\n**Rules:**")
    for k in RULE_LABELS:
        lines.append(f"- {k}: {await rule(k, i.guild.id)}")
    await i.response.send_message("\n".join(lines), ephemeral=True)


# ================================================================ بروفايل وإحصائيات
async def roblox_lookup(username):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://users.roblox.com/v1/usernames/users",
                              json={"usernames": [username], "excludeBanned": False},
                              timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
        u = (data.get("data") or [None])[0]
        return (u["id"], u["name"]) if u else None
    except Exception:
        return "error"


@bot.tree.command(name="link", description="Link your Roblox username to your Discord account")
async def link(i: discord.Interaction, roblox_username: str):
    await i.response.defer(ephemeral=True)
    res = await roblox_lookup(roblox_username.strip())
    if res is None:
        return await i.followup.send("مفيش لاعب روبلوكس بالاسم ده.")
    if res == "error":
        rid, rname, note = 0, roblox_username.strip(), " (مقدرتش أتحقق من الاسم دلوقتي)"
    else:
        (rid, rname), note = res, ""
    await db_exec(
        "INSERT INTO links(user_id, roblox_id, roblox_name) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET roblox_id=excluded.roblox_id, roblox_name=excluded.roblox_name",
        (i.user.id, rid, rname))
    await i.followup.send(f"✅ اتربط حسابك بـ **{rname}**{note}.")


@bot.tree.command(name="profile", description="Member profile card")
async def profile(i: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or i.user
    async with db_connect() as db:
        cur = await db.execute("SELECT points FROM guild_members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        row = await cur.fetchone()
        cur = await db.execute(
            "SELECT l.type, l.result, COUNT(*) FROM guild_point_events e JOIN logs l ON l.id=e.log_id "
            "WHERE e.guild_id=? AND e.user_id=? GROUP BY l.type, l.result", (i.guild.id, member.id))
        stats_rows = await cur.fetchall()
        cur = await db.execute("SELECT roblox_name FROM links WHERE user_id=?", (member.id,))
        link_row = await cur.fetchone()
    total = row[0] if row else 0
    agg = {}
    for t, res, c in stats_rows:
        agg[(t, res)] = agg.get((t, res), 0) + c

    rank_name = "—"
    for (n, _), role in zip(config.LADDER, ladder_roles(i.guild)):
        if role and role in member.roles:
            rank_name = role.name
    nxt = next(((n, p) for n, p in await get_ladder(i.guild.id) if p > total), None)

    e = discord.Embed(title=member.display_name, color=discord.Color.blurple())
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Points", value=str(total))
    e.add_field(name="Rank", value=rank_name)
    e.add_field(name="Next rank", value=f"{nxt[0]} ({nxt[1] - total} left)" if nxt else "Max auto rank")
    e.add_field(name="⚔️ Wars", value=f"{agg.get(('war', 'win'), 0)}W - {agg.get(('war', 'loss'), 0)}L")
    e.add_field(name="🥊 Outside 1v1",
                value=f"{agg.get(('outside', 'win'), 0)}W - {agg.get(('outside', 'loss'), 0)}L")
    e.add_field(name="🤝 Internal", value=str(sum(c for (t, _), c in agg.items() if t == "internal")))
    e.add_field(name="🎯 Tryouts done", value=str(sum(c for (t, _), c in agg.items() if t == "tryout")))
    if link_row:
        e.add_field(name="Roblox", value=link_row[0])
    await i.response.send_message(embed=e)


@bot.tree.command(name="stats", description="War record against other crews")
async def stats(i: discord.Interaction, crew: Optional[str] = None):
    async with db_connect() as db:
        cur = await db.execute("SELECT data, result FROM logs WHERE guild_id=? AND type='war' AND status='accepted'", (i.guild.id,))
        rows = await cur.fetchall()
    agg = {}
    for data_s, res in rows:
        d = json.loads(data_s)
        name = (d.get("crew") or "?").strip()
        a = agg.setdefault(name.lower(), {"name": name, "w": 0, "l": 0, "scores": []})
        a["w" if res == "win" else "l"] += 1
        a["scores"].append(f"{d.get('score', '-')} ({'W' if res == 'win' else 'L'})")
    if not agg:
        return await i.response.send_message("لسه مفيش حروب متسجلة.")
    if crew:
        k = crew.strip().lower()
        found = [a for key, a in agg.items() if k in key]
        if not found:
            return await i.response.send_message("مفيش حروب ضد طاقم بالاسم ده.")
        lines = [f"**{a['name']}**: {a['w']}W - {a['l']}L\nLast: " + ", ".join(a["scores"][-5:]) for a in found[:5]]
        return await i.response.send_message("\n\n".join(lines)[:1990])
    tw = sum(a["w"] for a in agg.values())
    tl = sum(a["l"] for a in agg.values())
    top = sorted(agg.values(), key=lambda a: a["w"] + a["l"], reverse=True)[:10]
    lines = [f"**Overall:** {tw}W - {tl}L\n"]
    lines += [f"- {a['name']}: {a['w']}W - {a['l']}L" for a in top]
    await i.response.send_message("\n".join(lines)[:1990])


@bot.tree.command(name="inactive", description="Members with no accepted logs for N days")
async def inactive(i: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    await i.response.defer(ephemeral=True)
    if not i.guild.chunked:
        await i.guild.chunk()
    cutoff = (now_utc() - timedelta(days=days)).isoformat()
    async with db_connect() as db:
        cur = await db.execute("SELECT user_id, MAX(created_at) FROM guild_point_events WHERE guild_id=? GROUP BY user_id", (i.guild.id,))
        last = {u: t for u, t in await cur.fetchall()}
    crew_role = find_crew_role(i.guild)
    lroles = [r for r in ladder_roles(i.guild) if r]
    out = []
    for m in i.guild.members:
        if m.bot:
            continue
        if not ((crew_role and crew_role in m.roles) or any(r in m.roles for r in lroles)):
            continue
        t = last.get(m.id)
        if t is None or t < cutoff:
            out.append((t or "", m))
    out.sort(key=lambda x: x[0])
    if not out:
        return await i.followup.send("كله نشيط ✅")
    lines = [f"{m.mention} — {'never' if not t else t[:10]}" for t, m in out]
    header = f"**Inactive for {days}+ days: {len(lines)}**\n"
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > 1900:
            await i.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none())
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await i.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none())


# ================================================================ مواسم
@bot.tree.command(name="season-reset", description="Archive everyone's points and reset them to 0")
async def season_reset(i: discord.Interaction, name: str, confirm: str):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    if confirm != "RESET":
        return await i.response.send_message("اكتب RESET (بحروف كبيرة) في خانة confirm للتأكيد.", ephemeral=True)
    async with db_connect() as db:
        await db.execute("INSERT INTO guild_season_archive(guild_id,season,user_id,points) SELECT ?, ?, user_id, points FROM guild_members WHERE guild_id=? AND points>0 ON CONFLICT(guild_id,season,user_id) DO UPDATE SET points=excluded.points", (i.guild.id, name, i.guild.id))
        await db.execute("UPDATE guild_members SET points=0 WHERE guild_id=?", (i.guild.id,))
        next_season_id = await active_season_id(i.guild.id) + 1
        await db.execute("INSERT INTO guild_settings(guild_id,key,value) VALUES(?,?,?) "
                         "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
                         (i.guild.id, "active_season_id", next_season_id))
        await db.commit()
    await audit(f"🗄️ {i.user.mention} archived season **{name}** and reset all points", i.guild)
    # Rank roles follow the new zero balances as well.
    async with db_connect() as db:
        cur = await db.execute("SELECT user_id FROM guild_members WHERE guild_id=?", (i.guild.id,))
        member_ids = [r[0] for r in await cur.fetchall()]
    for user_id in member_ids:
        try:
            member_obj = await get_member(i.guild, user_id)
            await apply_promotion(i.guild, member_obj, 0)
        except (discord.NotFound, discord.Forbidden):
            pass
    await i.response.send_message(f"✅ اتأرشف موسم **{name}** وكل النقط رجعت 0 والرتب اتراجعت حسب الرصيد الجديد.", ephemeral=True)


@bot.tree.command(name="season-top", description="Top 10 of an archived season")
async def season_top(i: discord.Interaction, name: str):
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT user_id, points FROM guild_season_archive WHERE guild_id=? AND season=? ORDER BY points DESC LIMIT 10", (i.guild.id, name))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("مفيش موسم بالاسم ده.", ephemeral=True)
    text = f"**Season {name}**\n" + "\n".join(f"**{n}.** <@{u}> — {p}" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


# ================================================================ باكاب
def _backup_file(path):
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(path)
    src.backup(dst)
    dst.close()
    src.close()


async def make_backup():
    path = os.path.join(tempfile.gettempdir(), f"crewbot-backup-{now_utc():%Y%m%d-%H%M}.db")
    await asyncio.to_thread(_backup_file, path)
    return path


@bot.tree.command(name="backup", description="Send a copy of the database (ephemeral)")
async def backup_cmd(i: discord.Interaction):
    if not isinstance(i.user, discord.Member) or not (i.user.id == owner_id() or i.user.guild_permissions.administrator):
        return await i.response.send_message("❌ Backup is restricted to the bot owner and server administrators.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    path = await make_backup()
    try:
        await i.followup.send("🗄️ Backup", file=discord.File(path), ephemeral=True)
    finally:
        os.unlink(path)


# ================================================================ مهام تلقائية
async def run_reminders():
    hours_by_guild = {g.id: await rule("reminder_hours", g.id) for g in bot.guilds}
    if not any(hours_by_guild.values()):
        return
    hours = max((h for h in hours_by_guild.values() if h), default=0)
    if not hours:
        return
    cutoff = (now_utc() - timedelta(hours=hours)).isoformat()
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT id, type, guild_id, review_channel, review_msg FROM logs "
            "WHERE status='pending' AND COALESCE(reminded,0)=0 AND created_at<? AND review_msg IS NOT NULL",
            (cutoff,))
        rows = await cur.fetchall()
    for log_id, t, guild_id, ch_id, msg_id in rows:
        guild = bot.get_guild(guild_id) if guild_id else None
        if not guild or not hours_by_guild.get(guild.id):
            continue
        guild_cutoff = (now_utc() - timedelta(hours=hours_by_guild[guild.id])).isoformat()
        # A shared broad query is only a candidate scan; enforce each server's own threshold.
        async with db_connect() as db:
            cur = await db.execute("SELECT created_at FROM logs WHERE id=? AND guild_id=?", (log_id, guild.id))
            created_row = await cur.fetchone()
        if not created_row or created_row[0] >= guild_cutoff:
            continue
        ch = guild.get_channel(ch_id) if guild and ch_id else None
        if ch:
            ping = await cfg_get(guild.id, t, "review_ping")
            try:
                msg = await ch.fetch_message(msg_id)
                await msg.reply(f"{f'<@&{ping}> ' if ping else ''}⏰ Log #{log_id} has been waiting "
                                f"for review for more than {hours_by_guild[guild.id]}h.", allowed_mentions=mentions(ping))
            except discord.HTTPException:
                pass
        await db_exec("UPDATE logs SET reminded=1 WHERE id=?", (log_id,))


async def run_backup():
    hours = max([await rule("backup_hours", g.id) for g in bot.guilds] or [0])
    if not hours or not owner_id():
        return
    last = await get_setting("state:last_backup") or 0
    if now_ts() - last < hours * 3600:
        return
    await set_setting("state:last_backup", now_ts())
    path = await make_backup()
    try:
        owner = await bot.fetch_user(owner_id())
        await owner.send("🗄️ Automatic backup", file=discord.File(path))
    except discord.HTTPException:
        pass
    finally:
        os.unlink(path)


async def run_weekly():
    if not any([await rule("weekly_post", g.id) for g in bot.guilds]):
        return
    last = await get_setting("state:last_weekly")
    if last is None:
        await set_setting("state:last_weekly", now_ts())
        return
    if now_ts() - last < 7 * 86400:
        return
    await set_setting("state:last_weekly", now_ts())
    for guild in bot.guilds:
        if not await rule("weekly_post", guild.id):
            continue
        text = await weekly_text(guild.id)
        if not text:
            continue
        ch_id = await cfg_get(guild.id, "all", "log_channel")
        ch = guild.get_channel(ch_id) if ch_id else None
        if ch:
            try:
                await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

@tasks.loop(minutes=10)
async def maintenance():
    for job in (run_reminders, run_backup, run_weekly):
        try:
            await job()
        except Exception as e:
            print(f"⚠️ maintenance job {job.__name__} failed: {e}")


@maintenance.before_loop
async def before_maintenance():
    await bot.wait_until_ready()


if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])