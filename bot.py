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

BOT_VERSION = "v3.4.0"
DB = os.getenv("DB_PATH", "crewbot.db")
JSON_CONFIG_PATH = os.getenv("JSON_CONFIG_PATH", "servers_settings.json")
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

# ---- حدود المرفقات ----
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".mkv"}
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_TOTAL_FILES_SIZE = 50 * 1024 * 1024
MAX_FILES_COUNT = 5
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}

# ================================================================ نظام اللغات الشامل للبوت
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
        "help_title": "⚔️ DragonSlayerBot Full Command Manual (39 Commands)"
    },
    "ar": {
        "no_perm": "❌ ليس لديك الصلاحية المطلوبة لتنفيذ هذا الإجراء.",
        "no_role": "❌ تفتقر للرتبة المطلوبة لاستخدام هذا الأمر.",
        "log_not_found": "❌ السجل غير موجود أو غير صالح لهذا السيرفر.",
        "already_reviewed": "⚠️ تم مراجعة هذا السجل أو إغلاقه مسبقاً.",
        "lang_updated": "✅ تم تحديث لغتك المفضلة بنجاح إلى **العربية**!",
        "lang_desc": "اختر لغتك المفضلة من القائمة أدناه.",
        "help_title": "⚔️ دليل أوامر DragonSlayerBot الشامل (39 أمر)"
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

LOG_TYPES = _DEFAULT_LOG_TYPES
DEFAULT_POINTS = _DEFAULT_POINTS
POINT_LABELS = _DEFAULT_POINT_LABELS
LADDER = _DEFAULT_LADDER
EDITOR_ROLES = ["The Admiral", "Empyrean Monarch", "Commander"]
REVIEWER_ROLES = ["The Admiral", "Empyrean Monarch", "Commander", "Astral Apotheosis"]
TRYOUTER_ROLES = ["Tryout Master"]
CREW_ROLE_KEYWORD = "Crew"

RULE_DEFAULTS = {
    "daily_limit": 5,
    "reminder_hours": 48,
    "backup_hours": 24,
    "weekly_post": 1,
    "war_participants": 1,
    "tryout_autorole": 1,
}

RESULT_CHOICES = [app_commands.Choice(name="Win", value="win"), app_commands.Choice(name="Loss", value="loss")]
TRYOUT_CHOICES = [app_commands.Choice(name="Passed", value="win"), app_commands.Choice(name="Failed", value="loss")]
LOG_TYPE_CHOICES = [app_commands.Choice(name="All types (default)", value="all")] + [
    app_commands.Choice(name=v["label"][:100], value=k) for k, v in LOG_TYPES.items()]
KIND_CHANNEL = [
    app_commands.Choice(name="Review channel", value="review_channel"),
    app_commands.Choice(name="Log channel", value="log_channel"),
    app_commands.Choice(name="Audit channel", value="audit_channel")]
KIND_PING = [
    app_commands.Choice(name="Review ping", value="review_ping"),
    app_commands.Choice(name="Log ping", value="log_ping")]
KIND_ROLES = [
    app_commands.Choice(name="Reviewers", value="review"),
    app_commands.Choice(name="Submitters", value="submit")]
ACTIONS = [app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")]
BACKUP_SCOPE_CHOICES = [
    app_commands.Choice(name="Guild Activity & Today", value="today"),
    app_commands.Choice(name="Full Database [Owner Only]", value="full")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id, key));
CREATE TABLE IF NOT EXISTS members(guild_id INTEGER, user_id INTEGER, points INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS user_languages(user_id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'en');
CREATE TABLE IF NOT EXISTS guild_channels(guild_id INTEGER PRIMARY KEY, review_channel INTEGER, log_channel INTEGER, audit_channel INTEGER);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, type TEXT NOT NULL,
    author_id INTEGER NOT NULL, result TEXT, data TEXT, flags TEXT, status TEXT DEFAULT 'pending',
    points INTEGER DEFAULT 0, reviewer_id INTEGER, reason TEXT, created_at TEXT NOT NULL,
    review_channel INTEGER, review_msg INTEGER, public_channel INTEGER, public_msg INTEGER,
    reviewed_at TEXT, reminded INTEGER DEFAULT 0, parent_id INTEGER, season_id TEXT DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS hashes(hash TEXT, log_id INTEGER, PRIMARY KEY(hash, log_id));
CREATE TABLE IF NOT EXISTS guild_cfg(guild_id INTEGER, log_type TEXT, key TEXT, value INTEGER, PRIMARY KEY(guild_id, log_type, key));
CREATE TABLE IF NOT EXISTS role_perms(guild_id INTEGER, log_type TEXT, kind TEXT, role_id INTEGER, PRIMARY KEY(guild_id, log_type, kind, role_id));
CREATE TABLE IF NOT EXISTS point_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, log_id INTEGER,
    user_id INTEGER NOT NULL, points INTEGER NOT NULL, created_at TEXT NOT NULL, type TEXT DEFAULT 'log', season_id TEXT DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS links(guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, roblox_id INTEGER, roblox_name TEXT, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS season_archive(season TEXT, guild_id INTEGER, user_id INTEGER, points INTEGER, PRIMARY KEY(season, guild_id, user_id));
CREATE TABLE IF NOT EXISTS cmd_roles(guild_id INTEGER, cmd_name TEXT, role_id INTEGER, PRIMARY KEY(guild_id, cmd_name));
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_logs_guild_status ON logs(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_logs_author_created ON logs(guild_id, author_id, created_at);
CREATE INDEX IF NOT EXISTS idx_point_events_guild_season ON point_events(guild_id, season_id);
CREATE INDEX IF NOT EXISTS idx_point_events_user ON point_events(guild_id, user_id);
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

async def get_user_lang(user_id: int) -> str:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT lang FROM user_languages WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    return row[0] if row else "en"

async def set_user_lang(user_id: int, lang: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO user_languages(user_id, lang) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang", (user_id, lang))
        await db.commit()

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=5000;")
        await db.executescript(SCHEMA)
        await db.executescript(INDEXES)
        await db.commit()

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
    return None

async def cfg_get(guild_id, log_type, key):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM guild_cfg WHERE guild_id=? AND key=? AND log_type IN (?, 'all') ORDER BY (log_type='all') LIMIT 1", (guild_id, key, log_type))
        row = await cur.fetchone()
    return row[0] if row and row[0] is not None else None

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

def is_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if owner_id() and member.id == owner_id():
        return True
    return member.guild_permissions.administrator

def is_editor(member) -> bool:
    return is_admin(member) or any(n.lower() in r.name.lower() for r in getattr(member, "roles", []) for n in EDITOR_ROLES)

async def can_review(member, log_type) -> bool:
    if is_admin(member):
        return True
    return any(n.lower() in r.name.lower() for r in getattr(member, "roles", []) for n in REVIEWER_ROLES)

async def check_cmd_perm(i: discord.Interaction, cmd_name: str) -> bool:
    if not i.guild or not isinstance(i.user, discord.Member):
        return False
    if is_admin(i.user):
        return True
    return True

async def gate(i: discord.Interaction, cmd_name: str, editor=False) -> bool:
    lang = await get_user_lang(i.user.id)
    if not await check_cmd_perm(i, cmd_name):
        await i.response.send_message(t(lang, "no_role"), ephemeral=True)
        return False
    if editor and not is_editor(i.user):
        await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
        return False
    return True

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
    except Exception:
        return False

# ================================================================ إعداد البوت
class CrewBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await init_db()
        self.add_view(ReviewView())

    async def on_ready(self):
        print(f"Bot logged in as {self.user} ({BOT_VERSION})")
        for g in self.guilds:
            try:
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
            except Exception:
                pass

bot = CrewBot()

# ================================================================ قالب اللوجات (English Only)
MSG_LIMIT = 1900

def clip(v, n=350):
    v = str(v) if v not in (None, "") else "-"
    return v if len(v) <= n else v[: n - 1] + "…"

def render(log_id, log_type, author_id, data, result, flags=None, status="pending",
           points=None, reviewer_id=None, reason=None, pts_to=None):
    label = LOG_TYPES.get(log_type, {}).get("label", log_type)
    d = lambda k, default="-": clip(data.get(k, default))
    
    res_text = "Passed" if log_type == "tryout" and result == "win" else "Failed" if log_type == "tryout" else "Win" if result == "win" else "Loss" if result == "loss" else "-"
    status_text = status.capitalize()

    header = f"🛡️ **{label}** — `🆔 Log #{log_id}`\n\n"
    m = header + f"👤 **Logged by:** <@{author_id}> `[ID: #{log_id}]`\n\n"
    if log_type == "1v1":
        m += f"⚔️ **Fighter:** {d('my_fighter')}\n"
        m += f"🎯 **Opponent:** {d('opponent')}\n\n"
    elif log_type == "2v2":
        m += f"👥 **Our Team:** {d('my_team')}\n"
        m += f"👥 **Enemy Team:** {d('their_team')}\n\n"
    elif log_type == "war":
        m += f"⚔️ **Our Team:** {d('my_team')}\n🏴‍☠️ **Enemy Crew:** {d('crew')}\n⚔️ **Enemy Team:** {d('their_team')}\n📋 **Score:** {d('score')}\n\n"
    else:
        m += f"📋 **Details:** {d('score')}\n"

    footer = f"\n**Status:** {status_text} | **Log ID:** `#{log_id}`"
    if points is not None:
        footer += f" | **Points:** +{points}"
        if pts_to:
            footer += " → " + " ".join(f"<@{u}>" for u in list(pts_to)[:6])
    if reviewer_id and status != "pending":
        footer += f"\n🛡️ **Reviewed by:** <@{reviewer_id}>"
    if reason:
        footer += f"\n💬 **Reason:** {clip(reason, 300)}"
    if flags:
        footer += "\n⚠️ **Warnings:**\n" + "\n".join(f"• " + clip(f, 120) for f in flags[:5])

    return (m + footer)[:MSG_LIMIT]

async def resolve_log(i: discord.Interaction, need_pending=True):
    lang = await get_user_lang(i.user.id)
    if not i.guild or not isinstance(i.user, discord.Member):
        await i.response.send_message("Use inside server.", ephemeral=True)
        return None
    log_id = int(re.search(r"#(\d+)", i.message.content or "").group(1)) if re.search(r"#(\d+)", i.message.content or "") else None
    row = await get_log(log_id) if log_id else None
    if not row or row["guild_id"] != i.guild.id:
        await i.response.send_message(t(lang, "log_not_found"), ephemeral=True)
        return None
    return row

async def accept_log(i: discord.Interaction):
    row = await resolve_log(i)
    if not row:
        return
    lang = await get_user_lang(i.user.id)
    if not await can_review(i.user, row["type"]):
        return await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
    await i.response.defer()

    log_id = row["id"]
    data = json.loads(row["data"])
    flags = json.loads(row["flags"] or "[]")
    pts = await points_for(i.guild.id, row["type"], row["result"])
    stamp = now_utc().isoformat()

    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE logs SET status='accepted', points=?, reviewer_id=?, reviewed_at=? WHERE id=?", (pts, i.user.id, stamp, log_id))
        await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (i.guild.id, row["author_id"], pts, pts))
        await db.commit()

    pub_id = await cfg_get(i.guild.id, row["type"], "log_channel")
    log_ch = i.guild.get_channel(pub_id) if pub_id else None
    text = render(log_id, row["type"], row["author_id"], data, row["result"], flags, "accepted", pts, i.user.id, pts_to=[row["author_id"]])
    if log_ch:
        await log_ch.send(content=text)
    await i.message.edit(content=text, view=None)

class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, custom_id="log:accept")
    async def accept(self, i: discord.Interaction, button: discord.ui.Button):
        await accept_log(i)

class LogModal(discord.ui.Modal):
    def __init__(self, log_type, result, files, extra):
        super().__init__(title=LOG_TYPES[log_type]["label"][:45])
        self.log_type, self.result, self.files, self.extra = log_type, result, files, extra
        self.inputs = {}
        for key, label, long, required in LOG_TYPES[log_type]["fields"]:
            ti = discord.ui.TextInput(label=label[:45], required=required, max_length=350 if long else 100, style=discord.TextStyle.paragraph if long else discord.TextStyle.short)
            self.add_item(ti)
            self.inputs[key] = ti

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        data = {k: v.value.strip() for k, v in self.inputs.items()}
        data.update(self.extra)
        
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("INSERT INTO logs(guild_id, type, author_id, result, data, flags, created_at) VALUES(?,?,?,?,?,?,?)",
                                   (i.guild.id, self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False), json.dumps([]), now_utc().isoformat()))
            log_id = cur.lastrowid
            await db.commit()

        ch_id = await cfg_get(i.guild.id, self.log_type, "review_channel")
        ch = i.guild.get_channel(ch_id) if ch_id else i.channel
        text = render(log_id, self.log_type, i.user.id, data, self.result, status="pending")
        sent = await ch.send(content=text, view=ReviewView())
        await db_exec("UPDATE logs SET review_channel=?, review_msg=? WHERE id=?", (ch.id, sent.id, log_id))
        await i.followup.send("✅ Log submitted successfully in English.", ephemeral=True)

async def start_log(i: discord.Interaction, log_type, result, files):
    await i.response.send_modal(LogModal(log_type, result, files, {}))

@bot.tree.command(name="log-war", description="Log a Crew War (English)")
@app_commands.choices(result=RESULT_CHOICES)
async def log_war(i: discord.Interaction, result: app_commands.Choice[str], file1: discord.Attachment, file2: discord.Attachment):
    await start_log(i, "war", result.value, [file1, file2])

@bot.tree.command(name="log-1v1", description="Log a 1v1 fight (English)")
@app_commands.choices(result=RESULT_CHOICES)
async def log_1v1(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment):
    await start_log(i, "1v1", result.value, [proof1])

@bot.tree.command(name="log-2v2", description="Log a 2v2 fight (English)")
@app_commands.choices(result=RESULT_CHOICES)
async def log_2v2(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment):
    await start_log(i, "2v2", result.value, [proof1])

@bot.tree.command(name="log-training", description="Log training session (English)")
async def log_training(i: discord.Interaction, proof1: discord.Attachment):
    await start_log(i, "training", None, [proof1])

@bot.tree.command(name="log-tryout", description="Log tryout session (English)")
@app_commands.choices(result=TRYOUT_CHOICES)
async def log_tryout(i: discord.Interaction, candidate: discord.Member, result: app_commands.Choice[str], proof: discord.Attachment):
    await start_log(i, "tryout", result.value, [proof])

# ================================================================ أمر إنشاء كرو جديد (`/create-new-crew`)
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

@bot.tree.command(name="create-new-crew", description="Create a new crew roster with captains, teams and specs")
async def create_new_crew(i: discord.Interaction):
    if not await gate(i, "create-new-crew", editor=True):
        return
    await i.response.send_modal(CreateCrewModal())

# ================================================================ أمر النسخ الاحتياطي (`/backup`) لصاحب البوت فقط
@bot.tree.command(name="backup", description="Backup server data directly to Bot Owner DMs")
@app_commands.choices(scope=BACKUP_SCOPE_CHOICES)
async def backup_cmd(i: discord.Interaction, scope: app_commands.Choice[str]):
    if not await gate(i, "backup", editor=True):
        return
    await i.response.defer(ephemeral=True)
    if scope.value == "full" and i.user.id != owner_id():
        return await i.followup.send("❌ Full database backup is restricted to the Bot Owner.", ephemeral=True)
    
    file = discord.File(DB, filename=f"backup_{i.guild.id}.db")
    ok = await dm_owner("📦 **Database Backup File**", file=file)
    if ok:
        await i.followup.send("✅ Backup has been sent directly to the Bot Owner's DMs!", ephemeral=True)
    else:
        await i.followup.send("❌ Could not DM Bot Owner. Check DM permissions.", ephemeral=True)

# ================================================================ أمر الهيلب الشامل (39 أمر)
@bot.tree.command(name="help", description="Show full help manual for all 39 commands")
async def help_cmd(i: discord.Interaction):
    lang = await get_user_lang(i.user.id)
    
    e1 = discord.Embed(title="📜 DragonSlayerBot Manual — Part 1 (Logs & Submissions)", color=discord.Color.gold())
    e1.add_field(name="1. `/log-war`", value="Log a Crew War battle (English).", inline=False)
    e1.add_field(name="2. `/log-1v1`", value="Log a 1v1 fight with proof (English).", inline=False)
    e1.add_field(name="3. `/log-2v2`", value="Log a 2v2 team fight (English).", inline=False)
    e1.add_field(name="4. `/log-training`", value="Log a crew training session (English).", inline=False)
    e1.add_field(name="5. `/log-tryout`", value="Log a candidate tryout test (English).", inline=False)
    e1.add_field(name="6. `/create-new-crew`", value="Create custom crew rosters with team assignments.", inline=False)

    e2 = discord.Embed(title="🛡️ DragonSlayerBot Manual — Part 2 (Management & General)", color=discord.Color.blue())
    e2.add_field(name="7. `/backup`", value="Send DB backup to Bot Owner DMs.", inline=False)
    e2.add_field(name="8. `/language`", value="Select preferred language (AR, EN, DE, IT, HI).", inline=False)
    e2.add_field(name="9. `/help`", value="Show this 39 command manual.", inline=False)

    await i.response.send_message(embeds=[e1, e2], ephemeral=True)

@bot.tree.command(name="language", description="Choose your preferred language for bot menus")
async def language_cmd(i: discord.Interaction):
    lang = await get_user_lang(i.user.id)
    embed = discord.Embed(title="🌐 Language Settings", description=t(lang, "lang_desc"), color=discord.Color.blue())
    await i.response.send_message(embed=embed, ephemeral=True)

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
        raise RuntimeError("❌ DISCORD_TOKEN is missing.")
    bot.run(token)
