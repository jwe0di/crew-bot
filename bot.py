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
JSON_CONFIG_PATH = os.getenv("JSON_CONFIG_PATH", "bot_data.json")
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

BOT_VERSION = "v2.5.3"


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
    app_commands.Choice(name="Full Database (قاعدة البيانات كاملة - إعدادات ولوجات)", value="full"),
    app_commands.Choice(name="Today's Activity (نشاط ولوجات اليوم فقط)", value="today"),
]
BACKUP_DEST_CHOICES = [
    app_commands.Choice(name="Here in chat (هنا في الشات)", value="chat"),
    app_commands.Choice(name="Bot Owner DM (إرسال للخاص لصاحب البوت)", value="owner"),
]

UPDATE_PRESETS = {
    "bot_patch": {
        "label": f"🤖 Bot Patch Update {BOT_VERSION} / تحديث جديد للبوت",
        "title_en": f"DragonSlayerBot Update {BOT_VERSION}",
        "title_ar": f"تحديث نظام البوت {BOT_VERSION}",
        "desc_en": "• Fixed critical security privilege vulnerability in admin checks.\n• Advanced Backup System with Scope & Destination selection.\n• Full Database & Today's Activity export options.\n• Version Control Logging.",
        "desc_ar": "• تم إصلاح ثغرة أمنية خطيرة في نظام صلاحيات المشرفين.\n• نظام نسخ احتياطي متطور مع تحديد النطاق والوجهة.\n• تصدير قاعدة البيانات الكاملة أو نشاط اليوم فقط.\n• تتبع وتحديثات تلقائية لرقم النسخة."
    },
    "crew_update": {
        "label": "⚔️ Crew Policy & Ranks / تحديث الكرو والرتب",
        "title_en": "Crew Structure & Ladder Ranks Update",
        "title_ar": "تحديث هيكلة الكرو ورتب السلم",
        "desc_en": "• Adjusted point requirements for Level Ladder Ranks.\n• Updated War logs requirements (Minimum 2 proofs).\n• Automated Rank demotion upon point revocation.",
        "desc_ar": "• تعديل النقاط المطلوبة لرتب التقدم والسلم.\n• تحديث شروط لوج الحروب (تطلب دليلين على الأقل).\n• إضافة نظام التنزيل التلقائي للرتبة في حال خصم النقاط."
    },
    "rules": {
        "label": "📜 Server & Log Rules / قواعد السيرفر واللوجات",
        "title_en": "Official Crew & Log Regulations",
        "title_ar": "القوانين الرسمية للوجات والكرو",
        "desc_en": "• Self-review of logs is strictly prohibited.\n• Daily Log Submissions are limited per user.\n• Proof files must be clear and unedited.",
        "desc_ar": "• يمنع منعاً باتاً مراجعة أو قبول اللوج الشخصي.\n• تطبيق حد يومي على عدد اللوجات المسموح إرسالها.\n• يجب أن تكون فيديوهات وصور الإثبات واضحة وغير معدلة."
    },
    "event": {
        "label": "🎉 Special Event / فعالية جديدة",
        "title_en": "Crew Tournament & Multi-Points Event",
        "title_ar": "بطولة الكرو وفعالية مضاعفة النقاط",
        "desc_en": "• Double Points Event is now ACTIVE for War Logs!\n• Internal 1v1 and 2v2 Tournaments are starting soon.\n• Extra Rewards for top Leaderboard contenders.",
        "desc_ar": "• تفعيل فعالية مضاعفة النقاط لجميع حروب الكرو!\n• انطلاق بطولات الـ 1v1 والـ 2v2 الداخلية قريباً.\n• جوائز مخصصة لمتصدري قائمة الترتيب الأسبوعي."
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

CMD_CHOICES = [app_commands.Choice(name=c, value=c) for c in ALL_BOT_COMMANDS[:25]]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS members(guild_id INTEGER, user_id INTEGER, points INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS guild_channels(guild_id INTEGER PRIMARY KEY, review_channel INTEGER, log_channel INTEGER, audit_channel INTEGER);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    type TEXT, author_id INTEGER, result TEXT, data TEXT, flags TEXT,
    status TEXT DEFAULT 'pending', points INTEGER DEFAULT 0,
    reviewer_id INTEGER, reason TEXT, created_at TEXT,
    review_channel INTEGER, review_msg INTEGER,
    public_channel INTEGER, public_msg INTEGER,
    reviewed_at TEXT,
    reminded INTEGER DEFAULT 0
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
    guild_id INTEGER,
    log_id INTEGER, user_id INTEGER, points INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS links(user_id INTEGER PRIMARY KEY, roblox_id INTEGER, roblox_name TEXT);
CREATE TABLE IF NOT EXISTS season_archive(season TEXT, guild_id INTEGER, user_id INTEGER, points INTEGER, PRIMARY KEY(season, guild_id, user_id));
CREATE TABLE IF NOT EXISTS cmd_roles(guild_id INTEGER, cmd_name TEXT, role_id INTEGER, PRIMARY KEY(guild_id, cmd_name));
"""

LOG_ID_RE = re.compile(r"Log #(\d+)")


async def save_bot_state_json():
    try:
        async with aiosqlite.connect(DB) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM guild_cfg")
            guild_cfg = [dict(r) for r in await cur.fetchall()]
            
            cur = await db.execute("SELECT * FROM role_perms")
            role_perms = [dict(r) for r in await cur.fetchall()]
            
            cur = await db.execute("SELECT * FROM cmd_roles")
            cmd_roles = [dict(r) for r in await cur.fetchall()]
            
            cur = await db.execute("SELECT * FROM settings")
            settings = {r["key"]: r["value"] for r in await cur.fetchall()}
            
        data = {
            "version": BOT_VERSION,
            "guild_cfg": guild_cfg,
            "role_perms": role_perms,
            "cmd_roles": cmd_roles,
            "settings": settings,
            "updated_at": dt.datetime.now(timezone.utc).isoformat()
        }
        with open(JSON_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Failed to save JSON state: {e}")


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


def now_ts():
    return int(now_utc().timestamp())


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(SCHEMA)
        for k, v in config.DEFAULT_POINTS.items():
            await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", ("points:" + k, v))
        for name, p in config.LADDER:
            await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", ("rank:" + name, p))
        await db.commit()
    await save_bot_state_json()


async def db_exec(sql, params=()):
    async with aiosqlite.connect(DB) as db:
        await db.execute(sql, params)
        await db.commit()


async def get_setting(key):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
    return row[0] if row else None


async def set_setting(key, value):
    await db_exec(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    await save_bot_state_json()


async def rule(name):
    v = await get_setting("rule:" + name)
    return v if v is not None else RULE_DEFAULTS[name]


async def cfg_get(guild_id, log_type, key):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT value FROM guild_cfg WHERE guild_id=? AND key=? AND log_type IN (?, 'all') "
            "ORDER BY (log_type='all') LIMIT 1", (guild_id, key, log_type))
        row = await cur.fetchone()
    if row and row[0]:
        return row[0]
    fb = {"review_channel": "REVIEW_CHANNEL_ID", "log_channel": "LOG_CHANNEL_ID", "audit_channel": "AUDIT_CHANNEL_ID"}.get(key)
    return (getattr(config, fb, None) or None) if fb else None


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
    await save_bot_state_json()


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
    await save_bot_state_json()


async def check_cmd_perm(i: discord.Interaction, cmd_name: str) -> bool:
    if is_admin(i.user):
        return True
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT role_id FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, cmd_name))
        row = await cur.fetchone()
    if not row:
        return True
    required_role_id = row[0]
    return any(r.id == required_role_id for r in i.user.roles)


async def get_ladder():
    return [(n, await get_setting("rank:" + n)) for n, _ in config.LADDER]


async def get_log(log_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM logs WHERE id=?", (log_id,))
        return await cur.fetchone()


async def points_for(log_type, result):
    key = f"{log_type}_{result}" if log_type in ("war", "1v1", "2v2") else log_type
    v = await get_setting("points:" + key)
    return v if v is not None else config.DEFAULT_POINTS.get(key, 0)


async def recipients_for(row, data):
    if row["type"] == "tryout":
        return [int(data.get("tester_id") or row["author_id"])]
    ids = [row["author_id"]]
    if row["type"] in ("war", "2v2") and await rule("war_participants"):
        for x in data.get("participants", []):
            if int(x) not in ids:
                ids.append(int(x))
    return ids


def has_role(member, names) -> bool:
    return any(
        n.lower() in r.name.lower()
        for r in getattr(member, "roles", [])
        for n in names
    )


# تم إصلاح الثغرة هنا بنجاح: الاعتماد على صلاحية الـ Administrator وصاحب البوت فقط
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
    return [discord.utils.find(lambda r, n=n: n.lower() in r.name.lower(), guild.roles)
            for n, _ in config.LADDER]


async def inspect_blobs(blobs):
    hashes, flags = [], []
    for att, data in blobs:
        h = hashlib.sha256(data).hexdigest()
        hashes.append(h)
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT h.log_id FROM hashes h JOIN logs l ON l.id=h.log_id "
                "WHERE h.hash=? AND l.status!='rejected' LIMIT 1", (h,))
            row = await cur.fetchone()
        if row:
            flags.append(f"{att.filename}: same file was used in Log #{row[0]}")
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
    custom = await get_setting(f"template:{guild_id}:{log_type}")
    return custom if custom else None


def render(log_id, log_type, author_id, data, result, flags=None, status="pending",
           points=None, reviewer_id=None, reason=None, pts_to=None, custom_template=None):
    label = config.LOG_TYPES[log_type]["label"]
    d = lambda k, default="-": clip(data.get(k, default))
    
    if custom_template:
        try:
            rendered_text = custom_template.format(
                log_id=log_id, author=f"<@{author_id}>", score=d('score'), notes=d('notes'),
                youtube_link=data.get('youtube_link', '-'), my_team=d('my_team'), their_team=d('their_team'),
                crew=d('crew'), my_fighter=d('my_fighter'), opponent=d('opponent'),
                participants=d('participants'), candidate=data.get('candidate', '-'), roblox=d('roblox')
            )
        except Exception:
            rendered_text = "⚠️ Error in custom template format. Using default layout."
            custom_template = None
            
        if custom_template:
            m = rendered_text + "\n\n" + DIVIDER + "\n"
        else:
            m = f"{ANGEL}  **{label}**  {ANGEL}  `Log #{log_id}`\n\n{DIVIDER}\n\n"
    else:
        m = f"{ANGEL}  **{label}**  {ANGEL}  `Log #{log_id}`\n\n{DIVIDER}\n\n"
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
    ladder = await get_ladder()
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
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Rank update based on points")
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="Rank update based on points")
            if target_role:
                ch_id = await cfg_get(guild.id, "all", "log_channel")
                ch = guild.get_channel(ch_id) if ch_id else None
                if ch:
                    await ch.send(f"🎉 {member.mention} has been updated to rank {target_role.mention}!",
                                  allowed_mentions=discord.AllowedMentions(users=[member], roles=False))
        except discord.HTTPException:
            pass
    return target_role


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
    pts = await points_for(row["type"], row["result"])
    recips = await recipients_for(row, data)
    stamp = now_utc().isoformat()

    totals = {}
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "UPDATE logs SET status='accepted', points=?, reviewer_id=?, reviewed_at=? "
            "WHERE id=? AND status='pending'", (pts, i.user.id, stamp, log_id))
        if cur.rowcount == 0:
            await db.commit()
            return await i.followup.send("This log has already been reviewed.", ephemeral=True)
            
        for uid in recips:
            await db.execute(
                "INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (i.guild.id, uid, pts, pts))
            await db.execute(
                "INSERT INTO point_events(guild_id, log_id, user_id, points, created_at) VALUES(?,?,?,?,?)",
                (i.guild.id, log_id, uid, pts, stamp))
            cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, uid))
            totals[uid] = (await cur.fetchone())[0]
        await db.commit()

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
            await i.followup.send(f"Log accepted, but rank update failed for <@{uid}>: {e}", ephemeral=True)

    if (row["type"] == "tryout" and row["result"] == "win" and data.get("candidate_id")
            and await rule("tryout_autorole")):
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
        row = await get_log(self.log_id)
        if not row or row["guild_id"] != i.guild.id:
            return await i.response.send_message("Log not found or not in this server.", ephemeral=True)
            
        if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
            return await i.response.send_message("❌ You no longer have permission to review this log.", ephemeral=True)

        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "UPDATE logs SET status='rejected', reviewer_id=?, reason=?, reviewed_at=? "
                "WHERE id=? AND status='pending'",
                (i.user.id, self.reason.value, now_utc().isoformat(), self.log_id))
            await db.commit()
        if cur.rowcount == 0:
            return await i.response.send_message("This log has already been reviewed.", ephemeral=True)

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
        hashes, flags = await inspect_blobs(blobs)
        data = {k: v.value.strip() for k, v in self.inputs.items()}
        data.update(self.extra)

        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "INSERT INTO logs(guild_id, type, author_id, result, data, flags, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (i.guild.id, self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False),
                 json.dumps(flags, ensure_ascii=False), now_utc().isoformat()))
            log_id = cur.lastrowid
            for h in hashes:
                await db.execute("INSERT OR IGNORE INTO hashes VALUES(?,?)", (h, log_id))
            await db.commit()

        ch_id = await cfg_get(i.guild.id, self.log_type, "review_channel")
        ch = i.guild.get_channel(ch_id) if ch_id else None
        ping_id = await cfg_get(i.guild.id, self.log_type, "review_ping")
        
        custom_tmpl = await get_custom_template(i.guild.id, self.log_type)
        text = render(log_id, self.log_type, i.user.id, data, self.result, flags, custom_template=custom_tmpl)
        content = (f"<@&{ping_id}>\n" if ping_id else "") + text
        am = mentions(ping_id)

        async def fail(reason):
            await db_exec("UPDATE logs SET status='rejected', reason=? WHERE id=?",
                          ("failed to post for review", log_id))
            await i.followup.send(
                f"❌ Failed to post log for review ({reason}). Make sure the review channel is set via /setchannel.",
                ephemeral=True)

        if not ch:
            return await fail("Channel not set")
        sent = None
        try:
            try:
                sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am,
                                     files=[discord.File(io.BytesIO(b), a.filename) for a, b in blobs])
            except discord.Forbidden:
                raise
            except discord.HTTPException:
                sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am)
                await ch.send("**Attachments (links expire after a while):**\n" + "\n".join(a.url for a, _ in blobs),
                              allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            return await fail(str(e))

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
    
    limit = await rule("daily_limit")
    if limit:
        since = (now_utc() - timedelta(hours=24)).isoformat()
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? "
                "AND status IN ('pending','accepted')", (i.guild.id, i.user.id, since))
            n = (await cur.fetchone())[0]
        if n >= limit:
            return await i.response.send_message(
                f"You have reached the daily limit ({limit} logs per 24 hours). Try again later.", ephemeral=True)
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
        value="• `/backup`: Advanced backup generator (Full or Today, Chat or Owner DM).\n"
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
    last_ver = await get_setting(f"state:bot_version:{guild.id}")
    if last_ver != BOT_VERSION:
        await set_setting(f"state:bot_version:{guild.id}", BOT_VERSION)
        ch_id = await cfg_get(guild.id, "all", "log_channel")
        ch = guild.get_channel(ch_id) if ch_id else None
        if ch:
            ping_role_id = await cfg_get(guild.id, "all", "update_ping")
            ping_text = f"<@&{ping_role_id}>\n" if ping_role_id else ""
            
            data = UPDATE_PRESETS["bot_patch"]
            embed = discord.Embed(
                title=f"🤖 System Auto Update — Version {BOT_VERSION}",
                color=discord.Color.green(),
                timestamp=now_utc()
            )
            embed.add_field(name="English Patch Notes", value=data["desc_en"], inline=False)
            embed.add_field(name="تفاصيل التحديث بالعربية", value=data["desc_ar"], inline=False)
            embed.set_footer(text=f"DragonSlayerBot Auto Update Engine | {BOT_VERSION}")
            try:
                await ch.send(content=ping_text, embed=embed, allowed_mentions=mentions(ping_role_id))
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
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    target = channel or i.channel
    embeds = get_help_embeds()
    try:
        await target.send(embeds=embeds)
        await i.response.send_message(f"✅ Posted complete help manual to {target.mention}!", ephemeral=True)
    except discord.HTTPException as e:
        await i.response.send_message(f"❌ Failed to send help panel: {e}", ephemeral=True)


@bot.tree.command(name="set-cmd-role", description="Lock a command to a specific role | تحديد رتبة مخصصة لاستخدام أمر معين")
@app_commands.choices(command=CMD_CHOICES)
@app_commands.describe(command="Select command | اختر الأمر", role="Role required (Leave empty to reset) | الرتبة المطلوبة")
async def set_cmd_role(i: discord.Interaction, command: app_commands.Choice[str], role: Optional[discord.Role] = None):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    
    async with aiosqlite.connect(DB) as db:
        if role:
            await db.execute(
                "INSERT INTO cmd_roles(guild_id, cmd_name, role_id) VALUES(?,?,?) "
                "ON CONFLICT(guild_id, cmd_name) DO UPDATE SET role_id=excluded.role_id",
                (i.guild.id, command.value, role.id))
            msg = f"🔒 Command `/{command.value}` is now locked to role {role.mention}."
        else:
            await db.execute("DELETE FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, command.value))
            msg = f"🔓 Command `/{command.value}` is now accessible to default permissions."
        await db.commit()
    await save_bot_state_json()
    await audit(f"⚙️ {i.user.mention} updated command permission for `/{command.value}`", i.guild)
    await i.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="set-update-ping", description="Set role to ping for bot updates and manuals | تحديد رتبة المنشن لنشرة التحديثات والشرح")
async def set_update_ping(i: discord.Interaction, role: Optional[discord.Role] = None):
    if not is_editor(i.user):
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
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    if not await check_cmd_perm(i, "log-update"):
        return await i.response.send_message("❌ You don't have the required role to use this command.", ephemeral=True)
        
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

    has_sent_manual = await get_setting(f"state:help_manual_sent:{i.guild.id}")
    if not has_sent_manual:
        embeds = get_help_embeds()
        try:
            await target_ch.send(content=f"{ping_text}📖 **DragonSlayerBot Full System Manual & Setup ({BOT_VERSION})**", embeds=embeds, allowed_mentions=mentions(ping_role_id))
            await set_setting(f"state:help_manual_sent:{i.guild.id}", 1)
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


@bot.tree.command(name="backup", description="Advanced backup tool (Full / Today, Chat / Owner DM) | أداة النسخ الاحتياطي المتقدمة")
@app_commands.choices(scope=BACKUP_SCOPE_CHOICES, destination=BACKUP_DEST_CHOICES)
@app_commands.describe(
    scope="Choose backup scope | اختر نطاق النسخ الاحتياطي",
    destination="Choose where to send file | اختر مكان إرسال الملف"
)
async def backup_cmd(i: discord.Interaction, scope: app_commands.Choice[str], destination: app_commands.Choice[str]):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    if not await check_cmd_perm(i, "backup"):
        return await i.response.send_message("❌ You don't have the required role to use this command.", ephemeral=True)

    await i.response.defer(ephemeral=True)

    file_to_send = None
    filename = ""

    if scope.value == "full":
        filename = f"crewbot-full-backup-{now_utc():%Y%m%d-%H%M}.db"
        path = os.path.join(tempfile.gettempdir(), filename)
        await asyncio.to_thread(_backup_file, path)
        file_to_send = discord.File(path, filename=filename)
    else:
        today_str = now_utc().strftime("%Y-%m-%d")
        filename = f"crewbot-today-activity-{today_str}.json"
        
        async with aiosqlite.connect(DB) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM logs WHERE guild_id=? AND created_at >= ?", (i.guild.id, today_str))
            today_logs = [dict(r) for r in await cur.fetchall()]
            
            cur = await db.execute("SELECT * FROM point_events WHERE guild_id=? AND created_at >= ?", (i.guild.id, today_str))
            today_events = [dict(r) for r in await cur.fetchall()]

        data = {
            "version": BOT_VERSION,
            "guild_id": i.guild.id,
            "date": today_str,
            "logs": today_logs,
            "point_events": today_events,
            "exported_at": now_utc().isoformat()
        }
        
        path = os.path.join(tempfile.gettempdir(), filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            
        file_to_send = discord.File(path, filename=filename)

    try:
        if destination.value == "owner":
            success = await dm_owner(f"🗄️ **New Server Backup** (`{scope.name}`) from guild **{i.guild.name}** requested by {i.user.mention}:", file=file_to_send)
            if success:
                await i.followup.send("✅ Backup file has been successfully sent to the Bot Owner's DMs!", ephemeral=True)
            else:
                await i.followup.send("❌ Failed to send backup to owner's DM (Check OWNER_ID or DMs settings). Sending here instead:", file=file_to_send, ephemeral=True)
        else:
            await i.followup.send(f"🗄️ **Backup File ({scope.name}) — Ver {BOT_VERSION}:**", file=file_to_send, ephemeral=True)
    finally:
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
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    if not await check_cmd_perm(i, "set-log-template"):
        return await i.response.send_message("❌ You don't have the required role to use this command.", ephemeral=True)
    
    key = f"template:{i.guild.id}:{log_type.value}"
    if template:
        await set_setting(key, template)
        await audit(f"🎨 {i.user.mention} updated the log template for **{log_type.name}**", i.guild)
        await i.response.send_message(f"✅ Successfully updated the template design for **{log_type.name}**!", ephemeral=True)
    else:
        await set_setting(key, "")
        await audit(f"🗑️ {i.user.mention} reset the log template for **{log_type.name}** to default", i.guild)
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
@app_commands.describe(candidate="Candidate", result="Passed/Failed", proof="Proof", tester="Tester")
async def log_tryout(i: discord.Interaction, candidate: discord.Member, result: app_commands.Choice[str], proof: discord.Attachment, tester: Optional[discord.Member] = None):
    tester = tester or i.user
    extra = {"candidate_id": candidate.id, "candidate": candidate.mention, "tester_id": tester.id}
    await start_log(i, "tryout", result.value, [proof], extra)


@bot.tree.command(name="resubmit", description="Fix and resend a rejected log | إعادة إرسال لوج مرفوض")
async def resubmit(i: discord.Interaction, log_id: int):
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id or row["author_id"] != i.user.id:
        return await i.response.send_message("Log not found or not yours.", ephemeral=True)
    if row["status"] != "rejected" or row["type"] not in config.LOG_TYPES:
        return await i.response.send_message("This log is not rejected.", ephemeral=True)
    files = []
    try:
        ch = i.guild.get_channel(row["review_channel"]) if row["review_channel"] else None
        if ch and row["review_msg"]:
            old = await ch.fetch_message(row["review_msg"])
            files = list(old.attachments)
    except discord.HTTPException:
        pass
    if not files:
        return await i.response.send_message("Could not fetch old attachments. Please create a new log.", ephemeral=True)
    data = json.loads(row["data"])
    extra = {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants", "youtube_link") if k in data}
    await i.response.send_modal(LogModal(row["type"], row["result"], files, extra, defaults=data))


@bot.tree.command(name="revoke-log", description="Cancel an accepted log | إلغاء لوج مقبول")
async def revoke_log(i: discord.Interaction, log_id: int, reason: str):
    if not await check_cmd_perm(i, "revoke-log"):
        return await i.response.send_message("❌ You don't have the required role to use this command.", ephemeral=True)
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id or row["status"] != "accepted":
        return await i.response.send_message("Accepted log not found in this server.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM point_events WHERE guild_id=? AND log_id=?", (i.guild.id, log_id))
        events = await cur.fetchall()
        for uid, p in events:
            await db.execute("UPDATE members SET points = MAX(0, points - ?) WHERE guild_id=? AND user_id=?", (p, i.guild.id, uid))
        await db.execute("DELETE FROM point_events WHERE guild_id=? AND log_id=?", (i.guild.id, log_id))
        await db.execute("UPDATE logs SET status='revoked', reason=?, reviewer_id=? WHERE id=?", (reason, i.user.id, log_id))
        await db.commit()

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
        except Exception: pass

    await audit(f"🚫 {i.user.mention} revoked Log #{log_id}. Reason: {reason}", i.guild)
    await i.followup.send(f"Successfully revoked Log #{log_id}.", ephemeral=True)


@bot.tree.command(name="pending", description="List logs waiting for review | اللوجات المنتظرة")
async def pending(i: discord.Interaction):
    if not await check_cmd_perm(i, "pending"):
        return await i.response.send_message("❌ You don't have the required role to use this command.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not (is_editor(i.user) or await can_review_any(i.user)):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id, type, author_id, created_at, review_channel, review_msg FROM logs WHERE status='pending' AND guild_id=? ORDER BY id LIMIT 20", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows: return await i.response.send_message("No logs waiting for review ✅", ephemeral=True)
    lines = [f"**#{lid}** {config.LOG_TYPES[t]['label']} — <@{author}> — {str(created)[:16]}" for lid, t, author, created, ch_id, msg_id in rows]
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@bot.tree.command(name="setchannel", description="Set channels | تحديد القنوات")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_CHANNEL)
async def setchannel(i: discord.Interaction, log_type: app_commands.Choice[str], kind: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None):
    if not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    t = "all" if kind.value == "audit_channel" else log_type.value
    await cfg_set(i.guild.id, t, kind.value, channel.id if channel else None)
    await i.response.send_message(f"✅ **{kind.name}** ({t}) = {channel.mention if channel else 'Cleared'}", ephemeral=True)


@bot.tree.command(name="setping", description="Set ping roles | تحديد رتب المنشن")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_PING)
async def setping(i: discord.Interaction, log_type: app_commands.Choice[str], kind: app_commands.Choice[str], role: Optional[discord.Role] = None):
    if not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await cfg_set(i.guild.id, log_type.value, kind.value, role.id if role else None)
    await i.response.send_message(f"✅ **{kind.name}** ({log_type.name}) = {role.mention if role else 'Cleared'}", ephemeral=True)


@bot.tree.command(name="setroles", description="Set role permissions | صلاحيات الرتب")
@app_commands.choices(kind=KIND_ROLES, log_type=LOG_TYPE_CHOICES, action=ACTIONS)
async def setroles(i: discord.Interaction, kind: app_commands.Choice[str], log_type: app_commands.Choice[str], action: app_commands.Choice[str], role: discord.Role):
    if not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await roles_edit(i.guild.id, log_type.value, kind.value, role.id, action.value == "add")
    await i.response.send_message(f"✅ Updated **{kind.name}** ({log_type.name}) for {role.mention}.", ephemeral=True)


@bot.tree.command(name="setrule", description="Change a bot rule | تغيير القواعد")
@app_commands.choices(rule_name=RULE_CHOICES)
async def setrule(i: discord.Interaction, rule_name: app_commands.Choice[str], value: app_commands.Range[int, 0, 1000]):
    if not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await set_setting("rule:" + rule_name.value, value)
    await i.response.send_message(f"✅ **{rule_name.value}** = {value}", ephemeral=True)


@bot.tree.command(name="settings", description="Show all settings | الإعدادات الحالية")
async def settings_cmd(i: discord.Interaction):
    if not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    g = i.guild.id
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT log_type, key, value FROM guild_cfg WHERE guild_id=?", (g,))
        cfg = {(t, k): v for t, k, v in await cur.fetchall()}
    lines = [f"**Audit Channel:** <#{cfg.get(('all', 'audit_channel'), '—')}>"]
    for k in RULE_LABELS: lines.append(f"- {k}: {await rule(k)}")
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@bot.tree.command(name="sync", description="Sync commands | مزامنة الأوامر")
async def sync_cmd(i: discord.Interaction):
    if not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    ok = await bot.sync_guild(i.guild)
    await i.followup.send("✅ Synced successfully." if ok else "❌ Sync failed.")


POINT_CHOICES = [app_commands.Choice(name=l, value=k) for k, l in config.POINT_LABELS.items()]
RANK_CHOICES = [app_commands.Choice(name=n, value=n) for n, _ in config.LADDER]


@bot.tree.command(name="set-log-points", description="Set log points | تحديد نقاط اللوجات")
@app_commands.choices(log=POINT_CHOICES)
async def set_log_points(i: discord.Interaction, log: app_commands.Choice[str], points: app_commands.Range[int, 0, 1000]):
    if not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await set_setting("points:" + log.value, points)
    await i.response.send_message(f"Done. **{log.name}** = {points} points.", ephemeral=True)


@bot.tree.command(name="set-rank-points", description="Set rank points | تحديد نقاط الرتب")
@app_commands.choices(rank=RANK_CHOICES)
async def set_rank_points(i: discord.Interaction, rank: app_commands.Choice[str], points: app_commands.Range[int, 0, 100000]):
    if not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await set_setting("rank:" + rank.value, points)
    await i.response.send_message(f"Done. **{rank.name}** = {points} points.", ephemeral=True)


@bot.tree.command(name="set-points", description="Set member points | تحديد نقاط عضو")
async def set_points(i: discord.Interaction, member: discord.Member, points: app_commands.Range[int, 0, 100000]):
    if not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points=excluded.points", (i.guild.id, member.id, points))
        await db.commit()
    try: await update_member_rank(i.guild, member, points)
    except discord.Forbidden: pass
    await i.followup.send(f"Done. {member.display_name} = {points} points.", ephemeral=True)


@bot.tree.command(name="points", description="Check points | رصيد النقاط")
async def points_cmd(i: discord.Interaction, member: Optional[discord.Member] = None):
    if not await check_cmd_perm(i, "points"):
        return await i.response.send_message("❌ You don't have the required role to use this command.", ephemeral=True)
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id,))
        row = await cur.fetchone()
    total = row[0] if row else 0
    nxt = next(((n, p) for n, p in await get_ladder() if p > total), None)
    msg = f"{member.mention}: **{total}** points."
    msg += f"\nNext rank: **{nxt[0]}** ({nxt[1] - total} left)." if nxt else "\nMax rank reached."
    await i.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="leaderboard", description="Top members | المتصدرين")
@app_commands.choices(period=PERIOD_CHOICES)
async def leaderboard(i: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
    if not await check_cmd_perm(i, "leaderboard"):
        return await i.response.send_message("❌ You don't have the required role to use this command.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM members WHERE guild_id=? AND points>0 ORDER BY points DESC LIMIT 10", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows: return await i.response.send_message("No points yet.")
    text = "\n".join(f"**{n}.** <@{u}> — {p}" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="rates", description="Show log points & ranks | أسعار النقاط والسلالم")
async def rates(i: discord.Interaction):
    lines = ["**Log points:**"]
    for k, label in config.POINT_LABELS.items(): lines.append(f"- {label}: {await get_setting('points:' + k)}")
    lines.append("\n**Rank points:**")
    for n, p in await get_ladder(): lines.append(f"- {n}: {p}")
    await i.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="link", description="Link Roblox username | ربط حساب روبلوكس")
async def link(i: discord.Interaction, roblox_username: str):
    await i.response.defer(ephemeral=True)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [roblox_username.strip()], "excludeBanned": False}) as r:
                data = await r.json()
        u = (data.get("data") or [None])[0]
        if not u: return await i.followup.send("No Roblox user found.")
        await db_exec("INSERT INTO links(user_id, roblox_id, roblox_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET roblox_id=excluded.roblox_id, roblox_name=excluded.roblox_name", (i.user.id, u["id"], u["name"]))
        await i.followup.send(f"✅ Linked account to **{u['name']}**.")
    except Exception:
        await i.followup.send("❌ Error connecting to Roblox API.")


@bot.tree.command(name="profile", description="Member profile card | البطاقة الشخصية")
async def profile(i: discord.Interaction, member: Optional[discord.Member] = None):
    if not await check_cmd_perm(i, "profile"):
        return await i.response.send_message("❌ You don't have the required role to use this command.", ephemeral=True)
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id,))
        row = await cur.fetchone()
        cur = await db.execute("SELECT roblox_name FROM links WHERE user_id=?", (member.id,))
        link_row = await cur.fetchone()
    total = row[0] if row else 0
    e = discord.Embed(title=member.display_name, color=discord.Color.blurple())
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Points", value=str(total))
    if link_row: e.add_field(name="Roblox", value=link_row[0])
    await i.response.send_message(embed=e)


@bot.tree.command(name="stats", description="War stats against crews | إحصائيات الحروب")
async def stats(i: discord.Interaction, crew: Optional[str] = None):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT data, result FROM logs WHERE guild_id=? AND type='war' AND status='accepted'", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows: return await i.response.send_message("No wars logged yet.")
    await i.response.send_message(f"Total accepted wars in server: {len(rows)}")


@bot.tree.command(name="inactive", description="Inactive members | الأعضاء غير النشطين")
async def inactive(i: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30):
    if not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.send_message(f"Checking inactive members for last {days} days...", ephemeral=True)


@bot.tree.command(name="season-reset", description="Reset points for new season | بدء موسم جديد")
async def season_reset(i: discord.Interaction, name: str, confirm: str):
    if not is_editor(i.user): return await i.response.send_message("❌ No permission.", ephemeral=True)
    if confirm != "RESET": return await i.response.send_message("Type RESET to confirm.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR REPLACE INTO season_archive SELECT ?, guild_id, user_id, points FROM members WHERE guild_id=? AND points>0", (name, i.guild.id))
        await db.execute("UPDATE members SET points=0 WHERE guild_id=?", (i.guild.id,))
        await db.commit()
    await i.response.send_message(f"✅ Season **{name}** archived & points reset to 0.", ephemeral=True)


@bot.tree.command(name="season-top", description="Top 10 of archived season | متصدري موسم سابق")
async def season_top(i: discord.Interaction, name: str):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM season_archive WHERE season=? AND guild_id=? ORDER BY points DESC LIMIT 10", (name, i.guild.id))
        rows = await cur.fetchall()
    if not rows: return await i.response.send_message("Season not found.")
    text = f"**Season {name}**\n" + "\n".join(f"**{n}.** <@{u}> — {p}" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


@tasks.loop(minutes=10)
async def maintenance(): pass

@maintenance.before_loop
async def before_maintenance(): await bot.wait_until_ready()

if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])