import asyncio
import datetime as dt
import hashlib
import io
import json
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import urllib.parse
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional
from urllib.parse import urlparse

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

try:
    import config
except ImportError:
    config = None

load_dotenv()

BOT_VERSION = "v9.0.0 (Ultimate AI + Gaming)"
DB = os.getenv("DB_PATH", "crewbot.db")
JSON_CONFIG_PATH = os.getenv("JSON_CONFIG_PATH") or os.path.join(os.path.dirname(DB) or ".", "servers_settings.json")
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

try:
    if os.path.exists(DB):
        os.chmod(DB, 0o600)
except (OSError, NotImplementedError):
    pass

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".mkv"}
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_TOTAL_FILES_SIZE = 30 * 1024 * 1024
MAX_FILES_COUNT = 5
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}

# ================================================================ AI Config
AI_PROVIDER = os.getenv("AI_PROVIDER", "groq").lower()
AI_API_KEY = os.getenv("AI_API_KEY", "")

_PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"),
    "groq_vision": ("https://api.groq.com/openai/v1/chat/completions", "llama-3.2-90b-vision-preview"),
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    "deepseek": ("https://api.deepseek.com/chat/completions", "deepseek-chat"),
}

LANG_NAMES = {
    "ar": "Arabic (العربية)", "hi": "Hindi (हिन्दी)", "en": "English",
    "de": "German (Deutsch)", "it": "Italian (Italiano)", "fr": "French (Français)",
    "es": "Spanish (Español)", "ru": "Russian (Русский)", "tr": "Turkish (Türkçe)",
    "ur": "Urdu (اردو)", "fa": "Persian (فارسی)",
}

def detect_language(text: str) -> str:
    if not text: return "en"
    if re.search(r"[\u0600-\u06FF\u0750-\u077F]", text): return "ar"
    if re.search(r"[\u0900-\u097F]", text): return "hi"
    if re.search(r"[\u0400-\u04FF]", text): return "ru"
    if re.search(r"[\u0590-\u05FF]", text): return "he"
    if re.search(r"[\u4E00-\u9FFF]", text): return "zh"
    if re.search(r"[\u3040-\u30FF]", text): return "ja"
    if re.search(r"[\uAC00-\uD7AF]", text): return "ko"
    return "en"

def _build_system_prompt(lang: str, is_admin: bool) -> str:
    lang_name = LANG_NAMES.get(lang, "English")
    admin_status = "You HAVE full admin permissions. You can execute administrative tools without asking for confirmation." if is_admin else "You DO NOT have admin permissions. You can only chat and provide information. If the user asks you to perform an administrative action (like creating a role, muting, etc.), politely refuse and tell them they need the required role."
    
    return f"""You are the Ultimate AI Assistant inside a Discord gaming server (Dragon Slayers).

🌐 LANGUAGE RULE (CRITICAL):
The user's message is in **{lang_name}**.
You MUST reply in **{lang_name}** — no exceptions.

RULES:
- Keep replies short (2-5 lines max unless asked for details).
- Use tools to perform server actions. Never claim success unless a tool returned success.
- {admin_status}
- Use the `search_web` tool if you need real-time information or don't know the answer.
- If the user sends an image and asks about it, analyze it carefully.
"""

_T_DEFS = [
    ("get_member_info", "Get member info (points, rank, streak, logs)", {"member_query": "string (required)"}, False),
    ("get_server_info", "Get server stats", {}, False),
    ("list_channels", "List server channels", {}, False),
    ("list_roles", "List server roles with member counts", {}, False),
    ("search_web", "Search the internet for real-time information (DuckDuckGo)", {"query": "string (required)"}, False),
    ("create_role", "Create a role", {"name": "string (required)", "color_hex": "string (optional)", "hoist": "boolean (optional)"}, True),
    ("edit_role", "Edit role name/color", {"role_query": "string (required)", "new_name": "string (optional)", "new_color_hex": "string (optional)"}, True),
    ("delete_role", "Delete a role (DESTRUCTIVE)", {"role_query": "string (required)"}, True),
    ("create_channel", "Create a channel", {"name": "string (required)", "channel_type": "string (optional)", "category_query": "string (optional)", "is_private": "boolean (optional)"}, True),
    ("delete_channel", "Delete a channel (DESTRUCTIVE)", {"channel_query": "string (required)"}, True),
    ("give_role", "Give a role to a member", {"member_query": "string (required)", "role_query": "string (required)"}, True),
    ("remove_role", "Remove a role from a member", {"member_query": "string (required)", "role_query": "string (required)"}, True),
    ("timeout_member", "Timeout a member (Mute)", {"member_query": "string (required)", "duration_minutes": "integer (required)", "reason": "string (optional)"}, True),
    ("kick_member", "Kick a member from the server", {"member_query": "string (required)", "reason": "string (optional)"}, True),
    ("purge_messages", "Delete messages in the current channel", {"amount": "integer (required)"}, True),
    ("send_channel_message", "Send a message in a channel", {"channel_query": "string (required)", "message_text": "string (required)"}, True),
]

TOOLS, TOOL_META = [], {}
for _n, _d, _p, _a in _T_DEFS:
    _props, _req = {}, []
    for _k, _v in _p.items():
        if "boolean" in _v: _props[_k] = {"type": "boolean", "description": _v}
        elif "integer" in _v: _props[_k] = {"type": "integer", "description": _v}
        else: _props[_k] = {"type": "string", "description": _v}
        if "required" in _v: _req.append(_k)
    TOOLS.append({"type": "function", "function": {"name": _n, "description": _d, "parameters": {"type": "object", "properties": _props, "required": _req}}})
    TOOL_META[_n] = {"requires_admin": _a}

def run_dummy_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"DragonSlayerBot {BOT_VERSION} is online!".encode())
        def log_message(self, format, *args): pass
    port = int(os.environ.get("PORT", 8080))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

REQUIRED_TYPES = ("war", "1v1", "2v2", "training", "tryout")
_DEFAULT_LOG_TYPES = {
    "war": {"label": "Crew War Log", "fields": [("my_team", "Who fought for Dragon Slayers", True, True), ("crew", "Enemy crew name", False, True), ("their_team", "Who fought for the enemy crew", True, True), ("score", "Score (e.g. 5-3)", False, True), ("notes", "Notes (optional)", True, False)]},
    "1v1": {"label": "1v1 Fight Log", "fields": [("my_fighter", "Who fought for us", False, True), ("opponent", "Who we fought against", False, True), ("score", "Score (e.g. 5-3)", False, True), ("notes", "Notes (optional)", True, False)]},
    "2v2": {"label": "2v2 Fight Log", "fields": [("my_team", "Our team", False, True), ("their_team", "Enemy team", False, True), ("score", "Score (e.g. 5-3)", False, True), ("notes", "Notes (optional)", True, False)]},
    "training": {"label": "Training Log", "fields": [("participants", "Who trained", True, True), ("score", "Result / summary (optional)", False, False), ("notes", "Notes (optional)", True, False)]},
    "tryout": {"label": "Tryout Log", "fields": [("score", "Tryout score / rating", False, True), ("roblox", "Candidate Roblox username (optional)", False, False), ("notes", "Notes (optional)", True, False)]},
}

def _load_log_types():
    cfg = getattr(config, "LOG_TYPES", None) if config else None
    if isinstance(cfg, dict) and all(t in cfg and isinstance(cfg[t].get("fields"), list) and all(len(f) == 4 for f in cfg[t]["fields"]) for t in REQUIRED_TYPES):
        return {t: cfg[t] for t in REQUIRED_TYPES}
    return _DEFAULT_LOG_TYPES

LOG_TYPES = _load_log_types()
DEFAULT_POINTS = {k: int(getattr(config, "DEFAULT_POINTS", {}).get(k, v)) for k, v in {"war_win": 10, "war_loss": 5, "1v1_win": 8, "1v1_loss": 4, "2v2_win": 6, "2v2_loss": 3, "training": 3, "tryout": 5}.items()}
POINT_LABELS = {k: str(getattr(config, "POINT_LABELS", {}).get(k, v)) for k, v in {"war_win": "War - Win", "war_loss": "War - Loss", "1v1_win": "1v1 - Win", "1v1_loss": "1v1 - Loss", "2v2_win": "2v2 - Win", "2v2_loss": "2v2 - Loss", "training": "Training", "tryout": "Tryout"}.items()}
LADDER = [(str(n), int(p)) for n, p in (getattr(config, "LADDER", None) or [("Lvl. 20", 20), ("Lvl. 40", 60), ("Lvl. 60", 130), ("Lvl. 80", 220), ("Lvl. 100", 350)])]
EDITOR_ROLES = list(getattr(config, "EDITOR_ROLES", ["The Admiral", "Empyrean Monarch", "Commander"]))
REVIEWER_ROLES = list(getattr(config, "REVIEWER_ROLES", ["The Admiral", "Empyrean Monarch", "Commander", "Astral Apotheosis"]))
TRYOUTER_ROLES = list(getattr(config, "TRYOUTER_ROLES", ["Tryout Master"]))
CREW_ROLE_KEYWORD = getattr(config, "CREW_ROLE_KEYWORD", "Crew")

RULE_DEFAULTS = {"daily_limit": 5, "reminder_hours": 48, "backup_hours": 24, "weekly_post": 1, "war_participants": 1, "tryout_autorole": 1}
RULE_DEFAULTS.update(getattr(config, "RULE_DEFAULTS", {}) or {})
RULE_LABELS = {"daily_limit": "Max logs per member per 24h", "reminder_hours": "Remind reviewers after N hours", "backup_hours": "Auto backup every N hours", "weekly_post": "Weekly leaderboard post", "war_participants": "War points to confirmed teammates", "tryout_autorole": "Give Crew role on passed tryout"}

RESULT_LABEL = {"win": "✅ Win", "loss": "❌ Loss"}
TRYOUT_LABEL = {"win": "✅ Passed", "loss": "❌ Failed"}
STATUS_LABEL = {"pending": "⏳ Pending", "accepted": "✅ Accepted", "rejected": "❌ Rejected", "revoked": "🚫 Revoked"}
RESULT_CHOICES = [app_commands.Choice(name="Win / فوز", value="win"), app_commands.Choice(name="Loss / خسارة", value="loss")]
TRYOUT_CHOICES = [app_commands.Choice(name="Passed / نجاح", value="win"), app_commands.Choice(name="Failed / فشل", value="loss")]
LOG_TYPE_CHOICES = [app_commands.Choice(name="All types", value="all")] + [app_commands.Choice(name=v["label"][:100], value=k) for k, v in LOG_TYPES.items()]
KIND_CHANNEL = [app_commands.Choice(name="Review channel", value="review_channel"), app_commands.Choice(name="Log channel", value="log_channel"), app_commands.Choice(name="Audit channel", value="audit_channel")]
KIND_PING = [app_commands.Choice(name="Review ping", value="review_ping"), app_commands.Choice(name="Log ping", value="log_ping")]
KIND_ROLES = [app_commands.Choice(name="Reviewers", value="review"), app_commands.Choice(name="Submitters", value="submit")]
ACTIONS = [app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")]
PERIOD_CHOICES = [app_commands.Choice(name="All time", value="all"), app_commands.Choice(name="This season", value="season"), app_commands.Choice(name="This week", value="week")]
RULE_CHOICES = [app_commands.Choice(name=f"{l[:80]} / {k}", value=k) for k, l in RULE_LABELS.items()]
POINT_CHOICES = [app_commands.Choice(name=f"{l} / {k}", value=k) for k, l in POINT_LABELS.items()]
RANK_CHOICES = [app_commands.Choice(name=n, value=n) for n, _ in LADDER]
BACKUP_SCOPE_CHOICES = [app_commands.Choice(name="Guild Activity", value="today"), app_commands.Choice(name="Full Database", value="full")]
BACKUP_DEST_CHOICES = [app_commands.Choice(name="Here in chat", value="chat"), app_commands.Choice(name="Bot Owner DM", value="owner")]

COLOR_CHOICES = [app_commands.Choice(name="🔴 Red", value="FF0000"), app_commands.Choice(name="🟢 Green", value="00FF00"), app_commands.Choice(name="🔵 Blue", value="0000FF"), app_commands.Choice(name="🟣 Purple", value="800080"), app_commands.Choice(name="🟡 Yellow", value="FFFF00")]

UPDATE_PRESETS = {"bot_patch": {"label": f"🤖 Release notes {BOT_VERSION}", "title_en": f"Update {BOT_VERSION}", "title_ar": f"تحديث {BOT_VERSION}", "desc_en": "• AI Vision & Search\n• Reaction Roles\n• Tournaments\n• AI Memory", "desc_ar": "• رؤية وبحث AI\n• رتب تفاعلية\n• بطولات\n• ذاكرة للـ AI"}}
UPDATE_TYPE_CHOICES = [app_commands.Choice(name=v["label"], value=k) for k, v in UPDATE_PRESETS.items()]

ALL_BOT_COMMANDS = [
    "log-war", "log-1v1", "log-2v2", "log-training", "log-tryout", "resubmit", "edit-log", "log-update", "set-log-template", "points", "leaderboard", "profile",
    "stats", "rates", "link", "pending", "revoke-log", "inactive", "season-reset", "season-top", "backup", "setchannel", "setping",
    "setroles", "setrule", "set-log-points", "set-rank-points", "set-points", "sync-ranks", "verify-points", "settings", "sync", "set-cmd-perms",
    "help", "send-help-panel", "restore-backup", "my-logs", "log-info", "reviewer-stats", "create-new-crew", "crews-list", "crews-delete-all",
    "streak", "shop", "buy", "achievements", "claim-achievement", "quests", "event", "start-vote", "add-shop-item", "add-achievement", "add-quest", "customize-msg",
    "create-custom-role", "h2h", "lfg", "daily", "setup-tickets", "set-bio", "embed", "set-temp-voice", "create-temp-voice", "set-notification", "purge", "set-lfg-roles", "set-top-rewards",
    "grant-temp-admin", "revoke-temp-admin", "set-log-emojis", "show-log-emojis", "reset-log-emojis",
    "ai", "ai-reset", "ai-help", "set-ai-default", "set-ai-user", "set-ai-admin", "set-ai-mention-roles", "transfer", "add-auto-reply", "setup-reaction-role", "tournament-create", "tournament-start", "tournament-set-winner"
]

TEXTS = {
    "no_perm": "❌ No permission / مش معاك صلاحية.", "no_role": "❌ Missing role / مش معاك الرتبة.", "server_only": "❌ Server only.",
    "db_err": "❌ Database error.", "cooldown": "⏳ Wait {time}s.", "log_not_found": "❌ Log not found.", "log_already_reviewed": "⚠️ Already reviewed.",
    "no_review_perm": "❌ No review permission.", "no_self_review": "❌ Cannot self-review.", "old_season_log": "❌ Old season log.", "log_rejected_ok": "✅ Log rejected.",
    "not_tagged": "❌ Not a participant.", "already_confirmed": "✅ Already confirmed.", "part_confirmed": "✅ Participation confirmed.",
    "daily_limit_hit": "❌ Daily limit hit.", "log_submitted": "✅ Log `#{id}` submitted.", "no_submit_perm": "❌ No submit permission.",
    "war_min_files": "❌ War requires 2 files.", "min_one_file": "❌ Attach 1 file min.", "no_review_ch": "❌ Review channel not set.", "bad_yt": "❌ Bad YouTube link.",
    "bad_candidate": "❌ Invalid candidate.", "too_many_files": "❌ Too many files.", "bad_file_ext": "❌ Bad file ext.", "file_too_big": "❌ File too big.",
    "total_too_big": "❌ Total size too big.", "points_msg": "💰 {member}: **{total}** pts.", "no_points_yet": "ℹ️ No points yet.",
    "lb_week": "📊 Weekly LB", "lb_season": "🏆 Season LB", "lb_all": "📈 All-Time LB", "no_pending": "✅ No pending logs.", "no_my_logs": "ℹ️ No logs yet.",
    "sync_ok": "✅ Synced.", "sync_fail": "❌ Sync failed.", "crew_bad_members": "❌ Invalid members.", "no_crews": "⚠️ No crews.",
    "crew_created": "✅ Crew **{name}** created!", "crew_exists": "❌ Name taken.", "crew_bad_name": "❌ Name must be 2-40 chars.", "crew_bad_owner": "❌ Owner must be real.",
    "shop_title": "🏪 Shop", "shop_desc": "Buy with `/buy`", "shop_empty": "⚠️ Shop is empty.", "shop_bought": "✅ Bought **{item}** for {price} pts.",
    "shop_no_points": "❌ Need **{price}**, have **{points}**.", "streak_title": "🔥 Streak", "streak_desc": "Current: **{current}** | Max: **{max}**",
    "achieve_title": "🏆 Achievement!", "achieve_desc": "Congrats {user}! Unlocked **{name}**!\nClaim: `/claim-achievement`", "quest_title": "🎯 Quests",
    "event_title": "📅 Event: {title}", "event_desc": "**Time:** {time}\nConfirm attendance!", "event_joined": "✅ Joined!", "event_left": "❌ Left.",
    "role_created": "✅ Role {role} created for {member}!", "role_created_free": "✅ Role {role} created (Free)!", "achieve_claimed": "✅ Claimed **{name}**!",
    "achieve_already_claimed": "⚠️ Already claimed."
}

def tr(key: str, **kw) -> str:
    text = TEXTS.get(key, key)
    return text.format(**kw) if kw else text

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id, key));
CREATE TABLE IF NOT EXISTS members(guild_id INTEGER, user_id INTEGER, points INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS guild_channels(guild_id INTEGER PRIMARY KEY, review_channel INTEGER, log_channel INTEGER, audit_channel INTEGER);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, type TEXT NOT NULL, author_id INTEGER NOT NULL, result TEXT, data TEXT, flags TEXT,
    status TEXT DEFAULT 'pending', points INTEGER DEFAULT 0, reviewer_id INTEGER, reason TEXT, created_at TEXT NOT NULL, review_channel INTEGER, review_msg INTEGER,
    public_channel INTEGER, public_msg INTEGER, reviewed_at TEXT, reminded INTEGER DEFAULT 0, parent_id INTEGER, season_id TEXT DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS hashes(hash TEXT, log_id INTEGER, PRIMARY KEY(hash, log_id));
CREATE TABLE IF NOT EXISTS guild_cfg(guild_id INTEGER, log_type TEXT, key TEXT, value INTEGER, PRIMARY KEY(guild_id, log_type, key));
CREATE TABLE IF NOT EXISTS role_perms(guild_id INTEGER, log_type TEXT, kind TEXT, role_id INTEGER, PRIMARY KEY(guild_id, log_type, kind, role_id));
CREATE TABLE IF NOT EXISTS point_events(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, log_id INTEGER, user_id INTEGER NOT NULL, points INTEGER NOT NULL, created_at TEXT NOT NULL, type TEXT DEFAULT 'log', season_id TEXT DEFAULT 'main');
CREATE TABLE IF NOT EXISTS links(guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, roblox_id INTEGER, roblox_name TEXT, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS season_archive(season TEXT, guild_id INTEGER, user_id INTEGER, points INTEGER, PRIMARY KEY(season, guild_id, user_id));
CREATE TABLE IF NOT EXISTS cmd_roles(guild_id INTEGER, cmd_name TEXT, role_id INTEGER, PRIMARY KEY(guild_id, cmd_name));
CREATE TABLE IF NOT EXISTS cmd_users(guild_id INTEGER, cmd_name TEXT, user_id INTEGER, PRIMARY KEY(guild_id, cmd_name, user_id));
CREATE TABLE IF NOT EXISTS crews(guild_id INTEGER, crew_name TEXT, owner_id INTEGER, captains_data TEXT, created_at TEXT, PRIMARY KEY(guild_id, crew_name));
CREATE TABLE IF NOT EXISTS custom_settings(guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id, key));
CREATE TABLE IF NOT EXISTS custom_messages(guild_id INTEGER, key TEXT, title TEXT, description TEXT, color TEXT, PRIMARY KEY(guild_id, key));
CREATE TABLE IF NOT EXISTS custom_roles(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role_id INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS temp_voice_rooms(guild_id INTEGER NOT NULL, channel_id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS streaks(guild_id INTEGER, user_id INTEGER, current_streak INTEGER DEFAULT 0, max_streak INTEGER DEFAULT 0, last_log_date TEXT, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS shop_items(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, price INTEGER, role_id INTEGER, description TEXT);
CREATE TABLE IF NOT EXISTS achievements(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, description TEXT, required_points INTEGER, reward_points INTEGER DEFAULT 0, role_id INTEGER);
CREATE TABLE IF NOT EXISTS user_achievements(guild_id INTEGER, user_id INTEGER, achievement_id INTEGER, claimed INTEGER DEFAULT 0, PRIMARY KEY(guild_id, user_id, achievement_id));
CREATE TABLE IF NOT EXISTS quests(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, description TEXT, target_type TEXT, target_amount INTEGER, reward_points INTEGER, role_id INTEGER, is_auto INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER, message_id INTEGER, title TEXT, time TEXT, attendees TEXT);
CREATE TABLE IF NOT EXISTS tickets(guild_id INTEGER, user_id INTEGER, channel_id INTEGER, status TEXT DEFAULT 'open', PRIMARY KEY(guild_id, user_id, channel_id));
CREATE TABLE IF NOT EXISTS daily_bonus(guild_id INTEGER, user_id INTEGER, last_claimed TEXT, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS bios(guild_id INTEGER, user_id INTEGER, bio TEXT, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS notif_settings(guild_id INTEGER, event_type TEXT, mode TEXT, target_id INTEGER, PRIMARY KEY(guild_id, event_type));
CREATE TABLE IF NOT EXISTS lfg_allowed_roles(guild_id INTEGER, role_id INTEGER, PRIMARY KEY(guild_id, role_id));
CREATE TABLE IF NOT EXISTS votes(guild_id INTEGER, message_id INTEGER, voter_id INTEGER, target_id INTEGER, PRIMARY KEY(guild_id, message_id, voter_id));
CREATE TABLE IF NOT EXISTS temp_perms(guild_id INTEGER, user_id INTEGER, expires_at TEXT, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS log_emojis(guild_id INTEGER, slot TEXT, emoji TEXT, PRIMARY KEY(guild_id, slot));
CREATE TABLE IF NOT EXISTS ai_settings(guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id, key));
CREATE TABLE IF NOT EXISTS ai_allow(guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS ai_deny(guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS ai_admin_users(guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS ai_memory(guild_id INTEGER, user_id INTEGER, history TEXT, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS ai_mention_roles(guild_id INTEGER, role_id INTEGER, PRIMARY KEY(guild_id, role_id));
CREATE TABLE IF NOT EXISTS auto_replies(guild_id INTEGER, trigger_word TEXT, reply TEXT, PRIMARY KEY(guild_id, trigger_word));
CREATE TABLE IF NOT EXISTS reaction_roles(guild_id INTEGER, message_id INTEGER, emoji TEXT, role_id INTEGER, PRIMARY KEY(guild_id, message_id, emoji));
CREATE TABLE IF NOT EXISTS tournaments(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, max_players INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS tournament_players(tournament_id INTEGER, user_id INTEGER, PRIMARY KEY(tournament_id, user_id));
CREATE TABLE IF NOT EXISTS tournament_matches(tournament_id INTEGER, round INTEGER, p1_id INTEGER, p2_id INTEGER, winner_id INTEGER);
"""

LEGACY_COPY = {}
LOG_COLUMNS = [("guild_id", "INTEGER NOT NULL DEFAULT 0"), ("review_channel", "INTEGER"), ("review_msg", "INTEGER"), ("public_channel", "INTEGER"), ("public_msg", "INTEGER"), ("reviewed_at", "TEXT"), ("reminded", "INTEGER DEFAULT 0"), ("parent_id", "INTEGER"), ("season_id", "TEXT DEFAULT 'main'")]
INDEXES = ""

LOG_ID_RE = re.compile(r"`Log #(\d+)`")
LOG_ID_FALLBACK_RE = re.compile(r"Log #(\d+)")
TEMPLATE_VAR_RE = re.compile(r"\{(\w{1,32})\}")

def now_utc(): return dt.datetime.now(timezone.utc)

def owner_id():
    try: return int(os.getenv("OWNER_ID") or getattr(config, "OWNER_ID", 0) or 0)
    except (ValueError, TypeError): return 0

_ALLOWED_TABLES = {t for t in SCHEMA.split("CREATE TABLE IF NOT EXISTS ")[1:] for t in [t.split("(")[0].strip()]}

async def table_columns(db, table):
    if table not in _ALLOWED_TABLES: raise ValueError(f"Invalid table: {table}")
    cur = await db.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in await cur.fetchall()]

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=5000;")
        await db.executescript(SCHEMA)
        await db.commit()
def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()

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
            for name, sql in (("guild_cfg", "SELECT * FROM guild_cfg"), ("role_perms", "SELECT * FROM role_perms"), ("cmd_roles", "SELECT * FROM cmd_roles"), ("settings", "SELECT guild_id, key, value FROM settings WHERE key NOT LIKE 'state:%'")):
                cur = await db.execute(sql)
                out[name] = [dict(r) for r in await cur.fetchall()]
        out.update({"version": BOT_VERSION, "description": "Configuration snapshot only.", "updated_at": now_utc().isoformat()})
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
        try: return int(v)
        except (TypeError, ValueError): return v
    if key.startswith("points:"): return DEFAULT_POINTS.get(key[len("points:"):], 0)
    if key.startswith("rank:"): return next((p for name, p in LADDER if name == key[len("rank:"):]), 0)
    if key.startswith("rule:"): return RULE_DEFAULTS.get(key[len("rule:"):], 0)
    return None

async def set_setting(guild_id: int, key: str, value):
    await db_exec("INSERT INTO settings(guild_id, key, value) VALUES(?,?,?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (guild_id, str(key), str(value)))
    if not str(key).startswith("state:"):
        await save_config_snapshot_json()

async def rule(guild_id: int, name: str) -> int:
    v = await get_setting(guild_id, "rule:" + name)
    try: return int(v)
    except (TypeError, ValueError): return int(RULE_DEFAULTS.get(name, 0))

async def get_current_season(guild_id: int) -> str:
    s = await raw_setting(guild_id, "state:current_season")
    return str(s) if s else "main"

async def cfg_get(guild_id, log_type, key):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM guild_cfg WHERE guild_id=? AND key=? AND log_type IN (?, 'all') ORDER BY (log_type='all') LIMIT 1", (guild_id, key, log_type))
        row = await cur.fetchone()
    return row[0] if row and row[0] is not None else None

async def cfg_set(guild_id, log_type, key, value):
    async with aiosqlite.connect(DB) as db:
        if value is None:
            await db.execute("DELETE FROM guild_cfg WHERE guild_id=? AND log_type=? AND key=?", (guild_id, log_type, key))
        else:
            await db.execute("INSERT INTO guild_cfg VALUES(?,?,?,?) ON CONFLICT(guild_id, log_type, key) DO UPDATE SET value=excluded.value", (guild_id, log_type, key, value))
        await db.commit()
    await save_config_snapshot_json()

async def roles_get(guild_id, log_type, kind):
    async with aiosqlite.connect(DB) as db:
        for t in (log_type, "all"):
            cur = await db.execute("SELECT role_id FROM role_perms WHERE guild_id=? AND log_type=? AND kind=?", (guild_id, t, kind))
            ids = [r[0] for r in await cur.fetchall()]
            if ids: return ids
    return []

async def roles_edit(guild_id, log_type, kind, role_id, add: bool):
    async with aiosqlite.connect(DB) as db:
        if add: await db.execute("INSERT OR IGNORE INTO role_perms VALUES(?,?,?,?)", (guild_id, log_type, kind, role_id))
        else: await db.execute("DELETE FROM role_perms WHERE guild_id=? AND log_type=? AND kind=? AND role_id=?", (guild_id, log_type, kind, role_id))
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

async def points_for_key(guild_id, key) -> int:
    v = await get_setting(guild_id, "points:" + key)
    try: return int(v)
    except (TypeError, ValueError): return DEFAULT_POINTS.get(key, 0)

async def points_for(guild_id: int, log_type, result) -> int:
    key = f"{log_type}_{result}" if log_type in ("war", "1v1", "2v2") else log_type
    return await points_for_key(guild_id, key)

async def recipients_for(row, data):
    if row["type"] == "tryout": return [int(data.get("tester_id") or row["author_id"])]
    ids = [row["author_id"]]
    if row["type"] in ("war", "2v2") and await rule(row["guild_id"], "war_participants"):
        confirmed = {int(x) for x in data.get("confirmed_participants", [])}
        for x in data.get("participants", []):
            x = int(x)
            if x not in ids and x in confirmed: ids.append(x)
    return ids

async def get_custom_msg(guild_id, key, default_title, default_desc, default_color=discord.Color.blue()):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT title, description, color FROM custom_messages WHERE guild_id=? AND key=?", (guild_id, key))
        row = await cur.fetchone()
    if row:
        try: color = int(row[2].replace("#", ""), 16) if row[2] else default_color
        except ValueError: color = default_color
        return discord.Embed(title=row[0][:256], description=row[1][:4000], color=color)
    return discord.Embed(title=default_title[:256], description=default_desc[:4000], color=default_color)

async def get_custom_val(guild_id, key, default):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM custom_settings WHERE guild_id=? AND key=?", (guild_id, key))
        row = await cur.fetchone()
    return row[0] if row else default

_EMOJI_CACHE, _EMOJI_CACHE_TS = {}, {}
MSG_LIMIT = 1900

async def get_guild_emojis(guild_id: int) -> dict:
    now = time.time()
    if guild_id in _EMOJI_CACHE and now - _EMOJI_CACHE_TS.get(guild_id, 0) < 300: return _EMOJI_CACHE[guild_id]
    emojis = {}
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT slot, emoji FROM log_emojis WHERE guild_id=?", (guild_id,))
        for slot, emoji in await cur.fetchall(): emojis[slot] = emoji
    _EMOJI_CACHE[guild_id] = emojis
    _EMOJI_CACHE_TS[guild_id] = now
    return emojis

def invalidate_emoji_cache(guild_id: int):
    _EMOJI_CACHE.pop(guild_id, None)
    _EMOJI_CACHE_TS.pop(guild_id, None)

def build_divider(emojis: dict) -> str:
    left, right = emojis.get("divider_left", ""), emojis.get("divider_right", "")
    core = "●▬▬▬▬▬▬▬▬▬▬▬☭   ☭▬▬▬▬▬▬▬▬▬▬▬●"
    if left and right: return f"{left}   {core}   {right}"
    elif left: return f"{left}   {core}"
    elif right: return f"{core}   {right}"
    return core

async def send_notification(guild, event_type, title, desc, color=discord.Color.blue()):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT mode, target_id FROM notif_settings WHERE guild_id=? AND event_type=?", (guild.id, event_type))
        row = await cur.fetchone()
    if not row: return
    mode, target_id = row
    embed = await get_custom_msg(guild.id, f"notif_{event_type}", title, desc, color)
    if mode == "channel" and target_id:
        ch = guild.get_channel(target_id)
        if ch:
            try: await ch.send(embed=embed)
            except discord.HTTPException: pass
    elif mode == "dm" and target_id:
        role = guild.get_role(target_id)
        if role and len(role.members) <= 50:
            for member in role.members:
                if member.bot: continue
                try: await member.send(embed=embed)
                except discord.HTTPException: pass
                await asyncio.sleep(0.5)

def has_role(member, names) -> bool:
    normalized = {str(n).strip().casefold() for n in names if str(n).strip()}
    return any(r.name.strip().casefold() in normalized for r in getattr(member, "roles", []))

def is_admin(member) -> bool:
    if not isinstance(member, discord.Member): return False
    if owner_id() and member.id == owner_id(): return True
    return member.guild_permissions.administrator

TEMP_ADMIN_BLOCKED_COMMANDS = {
    "grant-temp-admin", "revoke-temp-admin", "backup", "restore-backup",
    "set-cmd-perms", "setchannel", "setrule", "settings", "purge", "sync",
    "set-ai-default", "set-ai-user", "set-ai-admin",
}

async def has_temp_admin(member) -> bool:
    if not member.guild: return False
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT expires_at FROM temp_perms WHERE guild_id=? AND user_id=?", (member.guild.id, member.id))
        row = await cur.fetchone()
        if not row: return False
        try:
            if now_utc() > dt.datetime.fromisoformat(row[0]):
                await db.execute("DELETE FROM temp_perms WHERE guild_id=? AND user_id=?", (member.guild.id, member.id))
                await db.commit()
                return False
            return True
        except ValueError: return False

async def is_editor(member) -> bool:
    return is_admin(member) or await has_temp_admin(member) or has_role(member, EDITOR_ROLES)

async def is_editor_for_command(member, cmd_name: str) -> bool:
    if is_admin(member): return True
    if await has_temp_admin(member): return cmd_name not in TEMP_ADMIN_BLOCKED_COMMANDS
    return has_role(member, EDITOR_ROLES)

async def can_review(member, log_type) -> bool:
    if is_admin(member) or await has_temp_admin(member): return True
    ids = await roles_get(member.guild.id, log_type, "review")
    if ids: return any(r.id in ids for r in member.roles)
    return has_role(member, REVIEWER_ROLES)

async def can_review_any(member) -> bool:
    for t in LOG_TYPES:
        if await can_review(member, t): return True
    return False

async def can_submit(member, log_type) -> bool:
    if is_admin(member) or await has_temp_admin(member): return True
    ids = await roles_get(member.guild.id, log_type, "submit")
    if ids: return any(r.id in ids for r in member.roles)
    if log_type == "tryout": return has_role(member, TRYOUTER_ROLES)
    return True

async def check_cmd_perm(i: discord.Interaction, cmd_name: str) -> bool:
    if not i.guild or not isinstance(i.user, discord.Member): return False
    if is_admin(i.user) or await has_temp_admin(i.user): return True
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT role_id FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, cmd_name))
        role_row = await cur.fetchone()
        cur = await db.execute("SELECT user_id FROM cmd_users WHERE guild_id=? AND cmd_name=?", (i.guild.id, cmd_name))
        user_rows = await cur.fetchall()
    allowed_users = [r[0] for r in user_rows]
    if not allowed_users and not role_row: return True
    if i.user.id in allowed_users: return True
    if role_row: return any(r.id == role_row[0] for r in i.user.roles)
    return False

async def gate(i: discord.Interaction, cmd_name: str, editor=False) -> bool:
    if not i.guild or not isinstance(i.user, discord.Member):
        await i.response.send_message(tr("server_only"), ephemeral=True)
        return False
    if not await check_cmd_perm(i, cmd_name):
        await i.response.send_message(tr("no_role"), ephemeral=True)
        return False
    if editor and not await is_editor_for_command(i.user, cmd_name):
        await i.response.send_message(tr("no_perm"), ephemeral=True)
        return False
    return True

async def _ensure_members(guild):
    if getattr(guild, "chunked", False): return
    try:
        async for _ in guild.fetch_members(limit=None): pass
    except Exception as e: print(f"⚠️ Error fetching members: {e}")

async def audit(text, guild):
    ch_id = await cfg_get(guild.id, "all", "audit_channel")
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        try: await ch.send(text[:1990], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException: pass

async def dm_owner(text, file=None):
    if not owner_id(): return False
    try:
        user = await bot.fetch_user(owner_id())
        if file: await user.send(text, file=file)
        else: await user.send(text)
        return True
    except Exception as e:
        print(f"⚠️ Failed to DM owner: {e}")
        return False

async def get_member(guild, user_id):
    return guild.get_member(user_id) or await guild.fetch_member(user_id)

def parse_log_id(message: discord.Message):
    for pattern in (LOG_ID_RE, LOG_ID_FALLBACK_RE):
        m = pattern.search(message.content or "")
        if m: return int(m.group(1))
    return None

def mentions(role_id=None, user_ids=None):
    return discord.AllowedMentions(roles=[discord.Object(id=role_id)] if role_id else False, users=[discord.Object(id=u) for u in user_ids] if user_ids else False, everyone=False)

def find_crew_role(guild):
    kw = CREW_ROLE_KEYWORD.lower()
    return discord.utils.find(lambda r: r.name.lower().rstrip().endswith(kw), guild.roles)

def ladder_roles(guild):
    result = []
    for name, _ in LADDER:
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        result.append(next((r for r in guild.roles if pattern.search(r.name)), None))
    return result

def valid_youtube(url: str):
    url = (url or "").strip()
    if not url or len(url) > 200 or any(c.isspace() for c in url): return None
    try: p = urlparse(url)
    except ValueError: return None
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() not in YT_HOSTS: return None
    return url

def dedupe(seq):
    out = []
    for x in seq:
        if x not in out: out.append(x)
    return out

async def report_error(i: discord.Interaction, error):
    import traceback
    original = getattr(error, "original", error)
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = tr("cooldown", time=f"{error.retry_after:.1f}")
        try:
            if i.response.is_done(): await i.followup.send(msg, ephemeral=True)
            else: await i.response.send_message(msg, ephemeral=True)
        except discord.HTTPException: pass
        return
    tb = "".join(traceback.format_exception(type(original), original, original.__traceback__))
    print(f"❌ Error in command: {type(original).__name__}: {original}")
    print(tb)
    error_type = type(original).__name__
    error_msg = str(original).strip() or "(no message)"
    is_privileged = False
    try:
        if i.guild and isinstance(i.user, discord.Member): is_privileged = is_admin(i.user)
    except Exception: pass
    if is_privileged:
        tb_short = tb[-1200:] if len(tb) > 1200 else tb
        msg = f"❌ **Error / خطأ**\n**Type:** `{error_type}`\n**Message:** {error_msg[:300]}\n**Traceback:**\n```py\n{tb_short}\n```"
    else:
        msg = f"❌ **Error:** `{error_type}`\n**Details:** {error_msg[:500]}\n*Please report to admin.*"
    try:
        if i.response.is_done(): await i.followup.send(msg[:1990], ephemeral=True)
        else: await i.response.send_message(msg[:1990], ephemeral=True)
    except discord.HTTPException: pass

class CrewBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._ready_once = False

    async def setup_hook(self):
        await init_db()
        self.add_view(ReviewView())
        self.add_view(TicketView())
        self.add_view(CloseTicketView())
        self.add_view(EventView())
        self.add_view(VoteView())
        try: await self.http.bulk_upsert_global_commands(self.application_id, [])
        except Exception as e: print(f"⚠️ Could not clear global commands: {e}")
        print("✅ Setup complete. Commands will sync per-guild.")

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
            for g in self.guilds: await self.sync_guild(g)
        if not maintenance.is_running(): maintenance.start()

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)
        try:
            target = guild.system_channel or next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
            if target: await target.send(f"👋 Thanks for adding **DragonSlayerBot**!\nUse `/help` for a full manual.")
        except Exception as e: print(f"⚠️ Welcome message failed: {e}")

bot = CrewBot()

@bot.tree.error
async def on_app_command_error(i: discord.Interaction, error):
    await report_error(i, error)

def clip(v, n=350):
    v = str(v) if v not in (None, "") else "-"
    return v if len(v) <= n else v[: n - 1] + "…"

DEFAULT_TEMPLATES = {
    "war": "{MOON} **Logged by:** {author}\n{MOON} **Dragon Slayer's Fighters:**\n{ARROW}  {my_team}\n\n{DIVIDER}\n\n{MOON} **{crew} Fighters:**\n{ARROW}  {their_team}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{participants_block}{flags_block}",
    "1v1": "{MOON} **Logged by:** {author}\n{MOON} **Dragon Slayer's Fighter:**\n{ARROW}  {my_fighter}\n\n{DIVIDER}\n\n{MOON} **Opponent:**\n{ARROW}  {opponent}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{flags_block}",
    "2v2": "{MOON} **Logged by:** {author}\n{MOON} **Dragon Slayer's Team:**\n{ARROW}  {my_team}\n\n{DIVIDER}\n\n{MOON} **Enemy Team:**\n{ARROW}  {their_team}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{participants_block}{flags_block}",
    "training": "{MOON} **Logged by:** {author}\n{MOON} **Training Participants:**\n{ARROW}  {participants}\n\n{DIVIDER}\n\n      • **Score:** {score}\n{notes_line}{youtube_line}{flags_block}",
    "tryout": "{MOON} **Tested by:** {author}\n{MOON} **Tryout Candidate:**\n{ARROW}  {candidate} {roblox}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{flags_block}"
}

async def get_custom_template(guild_id, log_type):
    t = await raw_setting(guild_id, f"template:{log_type}")
    return t or DEFAULT_TEMPLATES.get(log_type, "")

def apply_template(template: str, values: dict) -> str:
    result = TEMPLATE_VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)
    cleaned = []
    for line in result.split("\n"):
        if line.startswith(" ") and line.strip(): line = line[1:]
        cleaned.append(line)
    return "\n".join(cleaned)

def render(log_id, log_type, author_id, data, result, flags=None, status="pending", points=None, reviewer_id=None, reason=None, pts_to=None, custom_template=None, guild_emojis=None):
    guild_emojis = guild_emojis or {}
    label = LOG_TYPES.get(log_type, {}).get("label", log_type)
    d = lambda k, default="-": clip(data.get(k, default))
    participants = dedupe([int(x) for x in data.get("participants", [])]) if isinstance(data.get("participants"), list) else []
    confirmed = {int(x) for x in data.get("confirmed_participants", [])}
    template = custom_template or DEFAULT_TEMPLATES.get(log_type, "")
    labels = TRYOUT_LABEL if log_type == "tryout" else RESULT_LABEL
    divider = build_divider(guild_emojis)
    values = {
        "log_id": log_id, "author": f"<@{author_id}>", "score": d("score"), "notes": d("notes"),
        "youtube_link": data.get("youtube_link", "-"), "my_team": d("my_team"), "their_team": d("their_team"),
        "crew": d("crew"), "my_fighter": d("my_fighter"), "opponent": d("opponent"),
        "participants": d("participants") if not participants else " ".join(f"<@{p}>" for p in participants),
        "candidate": data.get("candidate", "-"), "roblox": f"({clip(data['roblox'], 40)})" if data.get("roblox") else "",
        "result": labels.get(result, result) if result else "-",
        "ANGEL": guild_emojis.get("angel", ""), "MOON": guild_emojis.get("moon", ""), "ARROW": guild_emojis.get("arrow", ""),
        "CROWN": guild_emojis.get("crown", ""), "DIVIDER": divider,
        "notes_line": f"      • **Notes:** {d('notes')}\n" if data.get("notes") else "",
        "youtube_line": f"      • **YouTube Video:** <{data['youtube_link']}>\n" if data.get("youtube_link") else "",
        "participants_block": f"\n**Tagged Teammates Verification:**\n" + "".join(f"• <@{p}> — **{'✅ Confirmed' if p in confirmed else '⏳ Unconfirmed'}**\n" for p in participants[:8]) if log_type in ("war", "2v2") and participants else "",
        "flags_block": f"\n⚠️ **Warnings:**\n" + "\n".join("• " + clip(f, 120) for f in flags[:5]) if flags else ""
    }
    header = f"{guild_emojis.get('angel', '')}  **{label}**  {guild_emojis.get('angel', '')}  🆔 `Log #{log_id}`\n\n{divider}\n\n"
    body = header + apply_template(clip(template, 1200), values) + "\n\n" + divider + "\n"
    footer = f"\n**Status:** {STATUS_LABEL[status]}"
    if points is not None:
        footer += f" | **Points:** +{points}"
        if pts_to: footer += " → " + " ".join(f"<@{u}>" for u in list(pts_to)[:6])
    if reviewer_id and status != "pending":
        who = {"accepted": "Accepted by", "rejected": "Rejected by", "revoked": "Revoked by"}[status]
        footer += f"\n**{who}:** <@{reviewer_id}>"
    if reason: footer += f"\n**Reason:** {clip(reason, 300)}"
    if len(body) + len(footer) > MSG_LIMIT: body = body[: max(200, MSG_LIMIT - len(footer) - 1)] + "…"
    return (body + footer)[:MSG_LIMIT + 100]

def progress_bar(total, ladder, width=10):
    nxt = next(((n, p) for n, p in ladder if p > total), None)
    if not nxt: return "█" * width + " MAX"
    prev = max([p for _, p in ladder if p <= total], default=0)
    filled = max(0, min(width, int((total - prev) / max(1, nxt[1] - prev) * width)))
    return f"{'█' * filled}{'░' * (width - filled)} {total}/{nxt[1]} → {nxt[0]}"

def target_index(ladder, total):
    target = -1
    for idx, (_, pts) in enumerate(ladder):
        if total >= pts: target = idx
    return target

async def update_member_rank(guild, member, total, allow_demote=False):
    ladder = await get_ladder(guild.id)
    roles = ladder_roles(guild)
    target = target_index(ladder, total)
    held = [idx for idx, r in enumerate(roles) if r and r in member.roles]
    current = max(held) if held else -1
    if allow_demote:
        if target == current and len(held) <= 1: return None
    elif target <= current: return None
    target_role = roles[target] if 0 <= target < len(roles) else None
    if target >= 0 and target_role is None:
        await audit(f"⚠️ Role for **{ladder[target][0]}** not found for {member.mention}.", guild)
        return None
    to_add = [target_role] if target_role and target_role not in member.roles else []
    to_remove = [roles[idx] for idx in held if roles[idx] != target_role]
    try:
        if to_add: await member.add_roles(*to_add, reason="Rank update")
        if to_remove: await member.remove_roles(*to_remove, reason="Rank update")
    except discord.Forbidden: return None
    except discord.HTTPException: return None
    if target_role and target > current:
        ch_id = await cfg_get(guild.id, "all", "log_channel")
        ch = guild.get_channel(ch_id) if ch_id else None
        if ch:
            try: await ch.send(f"🎉 {member.mention} promoted to {target_role.mention}!", allowed_mentions=discord.AllowedMentions(users=[member], roles=False))
            except discord.HTTPException: pass
        try: await member.send(f"🎉 Promoted to **{target_role.name}** in **{guild.name}**!")
        except discord.HTTPException: pass
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
        if not member: continue
        try:
            if await update_member_rank(guild, member, pts, allow_demote): changed += 1
        except Exception as e: print(f"⚠️ Rank update failed for {uid}: {e}")
        await asyncio.sleep(0.25)
    return changed
# ================================================================ إنشاء اللوج (Modals)
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
                style=discord.TextStyle.paragraph if long else discord.TextStyle.short
            )
            self.add_item(ti)
            self.inputs[key] = ti

    async def on_error(self, interaction, error):
        await report_error(interaction, error)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        
        if len(self.files) > MAX_FILES_COUNT:
            return await i.followup.send(tr("too_many_files", max=MAX_FILES_COUNT), ephemeral=True)
            
        total_size = 0
        for a in self.files:
            if isinstance(a, discord.Attachment):
                ext = os.path.splitext(a.filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    return await i.followup.send(tr("bad_file_ext", name=a.filename), ephemeral=True)
                if a.size > MAX_FILE_SIZE:
                    return await i.followup.send(tr("file_too_big", name=a.filename), ephemeral=True)
                total_size += a.size
                
        if total_size > MAX_TOTAL_FILES_SIZE:
            return await i.followup.send(tr("total_too_big"), ephemeral=True)

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
                        "SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? AND status IN ('pending','accepted')",
                        (i.guild.id, i.user.id, since)
                    )
                    if (await cur.fetchone())[0] >= limit:
                        await db.rollback()
                        return await i.followup.send(tr("daily_limit_hit", limit=limit), ephemeral=True)
                        
                cur = await db.execute(
                    "INSERT INTO logs(guild_id, type, author_id, result, data, flags, created_at, season_id, parent_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (i.guild.id, self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False), json.dumps(flags, ensure_ascii=False), now_utc().isoformat(), season, self.parent_id)
                )
                log_id = cur.lastrowid
                for h in hashes:
                    await db.execute("INSERT OR IGNORE INTO hashes VALUES(?,?)", (h, log_id))
                await db.commit()
            except Exception as e:
                await db.rollback()
                print(f"❌ submit DB error: {e}")
                return await i.followup.send(tr("db_err"), ephemeral=True)

        ch_id = await cfg_get(i.guild.id, self.log_type, "review_channel")
        ch = i.guild.get_channel(ch_id) if ch_id else None
        ping_id = await cfg_get(i.guild.id, self.log_type, "review_ping")
        
        custom_tmpl = await get_custom_template(i.guild.id, self.log_type)
        guild_emojis = await get_guild_emojis(i.guild.id)
        text = render(log_id, self.log_type, i.user.id, data, self.result, flags, custom_template=custom_tmpl, guild_emojis=guild_emojis)
        content = (f"<@&{ping_id}>\n" if ping_id else "") + text
        am = mentions(ping_id)

        async def fail_publish(reason):
            await db_exec("UPDATE logs SET status='rejected', reason=? WHERE id=?", (f"failed to post: {reason}"[:200], log_id))
            await i.followup.send(f"❌ Couldn't post the log for review ({reason}). Check /setchannel.", ephemeral=True)

        if not ch:
            return await fail_publish("review channel not set")
            
        sent = None
        try:
            limit_bytes = getattr(i.guild, "filesize_limit", 25 * 1024 * 1024) or 25 * 1024 * 1024
            if blobs and total_size <= limit_bytes:
                try:
                    sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am, files=[discord.File(io.BytesIO(b), a.filename) for a, b in blobs])
                except discord.Forbidden: raise
                except discord.HTTPException: sent = None
            if sent is None:
                sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am)
                if proof_urls:
                    await ch.send("**Attached Proof Links:**\n" + "\n".join(proof_urls)[:1800], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            return await fail_publish(str(e)[:150])

        await db_exec("UPDATE logs SET review_channel=?, review_msg=? WHERE id=?", (ch.id, sent.id, log_id))
        if flags:
            await dm_owner(f"⚠️ {i.user.mention} submitted Log #{log_id} with warnings:\n" + "\n".join(flags))
        await i.followup.send(tr("log_submitted", id=log_id), ephemeral=True)

class EditLogModal(discord.ui.Modal):
    def __init__(self, log_id, log_type, result, files, extra, defaults=None):
        super().__init__(title=f"Edit Log #{log_id} / تعديل اللوج")
        self.log_id = log_id
        self.log_type = log_type
        self.result = result
        self.files = files
        self.extra = extra
        self.inputs = {}
        defaults = defaults or {}
        for key, label, long, required in LOG_TYPES[log_type]["fields"]:
            max_len = 350 if long else 100
            dv = defaults.get(key)
            dv = str(dv)[:max_len] if isinstance(dv, (str, int)) and str(dv) else None
            ti = discord.ui.TextInput(
                label=label[:45], required=required, max_length=max_len, default=dv,
                style=discord.TextStyle.paragraph if long else discord.TextStyle.short
            )
            self.add_item(ti)
            self.inputs[key] = ti

    async def on_error(self, interaction, error):
        await report_error(interaction, error)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        
        row = await get_log(self.log_id)
        if not row or row["guild_id"] != i.guild.id or row["author_id"] != i.user.id:
            return await i.followup.send(tr("log_not_found"), ephemeral=True)
        
        if row["status"] != "pending":
            return await i.followup.send("❌ Only pending logs can be edited.", ephemeral=True)
        
        new_data = {k: v.value.strip() for k, v in self.inputs.items()}
        new_data.update(self.extra)
        
        old_data = json.loads(row["data"] or "{}")
        if old_data.get("proof_urls"):
            new_data["proof_urls"] = old_data["proof_urls"]
        
        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE logs SET data=? WHERE id=? AND status='pending' AND author_id=?",
                    (json.dumps(new_data, ensure_ascii=False), self.log_id, i.user.id)
                )
                if cur.rowcount == 0:
                    await db.rollback()
                    return await i.followup.send(tr("log_already_reviewed"), ephemeral=True)
                await db.commit()
            except Exception as e:
                await db.rollback()
                print(f"❌ edit-log DB error: {e}")
                return await i.followup.send(tr("db_err"), ephemeral=True)
        
        try:
            ch = i.guild.get_channel(row["review_channel"]) if row["review_channel"] else None
            if ch and row["review_msg"]:
                msg = await ch.fetch_message(row["review_msg"])
                custom_tmpl = await get_custom_template(i.guild.id, row["type"])
                guild_emojis = await get_guild_emojis(i.guild.id)
                flags = json.loads(row["flags"] or "[]")
                text = render(self.log_id, row["type"], row["author_id"], new_data, row["result"], flags, custom_template=custom_tmpl, guild_emojis=guild_emojis)
                await msg.edit(content=text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            print(f"⚠️ Could not update review message: {e}")
        
        await audit(f"✏️ {i.user.mention} edited Log #{self.log_id}", i.guild)
        await i.followup.send(f"✅ Log #{self.log_id} updated successfully.", ephemeral=True)

@bot.tree.command(name="edit-log", description="Edit a pending log's details / تعديل بيانات لوج")
@app_commands.checks.cooldown(1, 15.0)
async def edit_log(i: discord.Interaction, log_id: int):
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message(tr("server_only"), ephemeral=True)
    
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id or row["author_id"] != i.user.id:
        return await i.response.send_message(tr("log_not_found"), ephemeral=True)
    if row["status"] != "pending":
        return await i.response.send_message("❌ Only pending logs can be edited.", ephemeral=True)
    if row["type"] not in LOG_TYPES:
        return await i.response.send_message("❌ Invalid log type.", ephemeral=True)
    
    data = json.loads(row["data"] or "{}")
    files = []
    try:
        ch = i.guild.get_channel(row["review_channel"]) if row["review_channel"] else None
        if ch and row["review_msg"]:
            old = await ch.fetch_message(row["review_msg"])
            files = list(old.attachments)
    except discord.HTTPException: pass
    
    if not files and data.get("proof_urls"):
        files = [u for u in data["proof_urls"] if isinstance(u, str)]
    
    extra = {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants", "youtube_link") if k in data}
    await i.response.send_modal(EditLogModal(log_id, row["type"], row["result"], files, extra, defaults=data))

async def inspect_blobs(blobs, guild_id):
    hashes, flags = [], []
    for att, data in blobs:
        h = hashlib.sha256(data).hexdigest()
        hashes.append(h)
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT h.log_id FROM hashes h JOIN logs l ON l.id=h.log_id WHERE h.hash=? AND l.guild_id=? AND l.status NOT IN ('rejected') LIMIT 1",
                (h, guild_id)
            )
            row = await cur.fetchone()
        if row:
            flags.append(f"{att.filename}: same file was used in Log #{row[0]}")
    return hashes, flags

async def check_anti_cheat(i: discord.Interaction, log_type):
    if is_admin(i.user) or await has_temp_admin(i.user): return True
    if (now_utc() - i.user.created_at).days < 7:
        await i.response.send_message("⚠️ **Log Rejected:** Your account is too new.", ephemeral=True)
        return False
    since = (now_utc() - timedelta(hours=1)).isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? AND status IN ('pending', 'accepted')", (i.guild.id, i.user.id, since))
        if (await cur.fetchone())[0] >= 5:
            await i.response.send_message("⚠️ **Log Rejected:** Too fast! Please wait.", ephemeral=True)
            return False
    return True

async def start_log(i: discord.Interaction, log_type, result, files: List[Optional[discord.Attachment]], extra=None):
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message(tr("server_only"), ephemeral=True)
    if not await check_anti_cheat(i, log_type): return
    files = [f for f in files if f is not None]
    if not await check_cmd_perm(i, f"log-{log_type}"): return await i.response.send_message(tr("no_role"), ephemeral=True)
    if not await can_submit(i.user, log_type): return await i.response.send_message(tr("no_submit_perm"), ephemeral=True)
    if log_type == "war" and len(files) < 2: return await i.response.send_message(tr("war_min_files"), ephemeral=True)
    if log_type != "war" and len(files) < 1: return await i.response.send_message(tr("min_one_file"), ephemeral=True)
    rid = await cfg_get(i.guild.id, log_type, "review_channel")
    if not rid or not i.guild.get_channel(rid): return await i.response.send_message(tr("no_review_ch"), ephemeral=True)
    limit = await rule(i.guild.id, "daily_limit")
    if limit:
        since = (now_utc() - timedelta(hours=24)).isoformat()
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? AND status IN ('pending','accepted')", (i.guild.id, i.user.id, since))
            if (await cur.fetchone())[0] >= limit:
                return await i.response.send_message(tr("daily_limit_hit", limit=limit), ephemeral=True)
    await i.response.send_modal(LogModal(log_type, result, files, extra or {}))

async def youtube_extra(i: discord.Interaction, youtube_link: Optional[str], extra: dict):
    if youtube_link:
        yt = valid_youtube(youtube_link)
        if not yt:
            await i.response.send_message(tr("bad_yt"), ephemeral=True)
            return False
        extra["youtube_link"] = yt
    return True

# ================================================================ أوامر اللوجات
@bot.tree.command(name="log-war", description="Log a Crew War (min 2 attachments)")
@app_commands.checks.cooldown(1, 60.0)
@app_commands.choices(result=RESULT_CHOICES)
async def log_war(i: discord.Interaction, result: app_commands.Choice[str], file1: discord.Attachment, file2: discord.Attachment, file3: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None, player1: Optional[discord.Member] = None, player2: Optional[discord.Member] = None, player3: Optional[discord.Member] = None, player4: Optional[discord.Member] = None):
    ids = dedupe([p.id for p in (player1, player2, player3, player4) if p and not p.bot and p.id != i.user.id])
    extra = {"participants": ids}
    if not await youtube_extra(i, youtube_link, extra): return
    await start_log(i, "war", result.value, [file1, file2, file3], extra)

@bot.tree.command(name="log-1v1", description="Log a 1v1 fight")
@app_commands.checks.cooldown(1, 30.0)
@app_commands.choices(result=RESULT_CHOICES)
async def log_1v1(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None):
    extra = {}
    if not await youtube_extra(i, youtube_link, extra): return
    await start_log(i, "1v1", result.value, [proof1, proof2], extra)

@bot.tree.command(name="log-2v2", description="Log a 2v2 fight")
@app_commands.checks.cooldown(1, 30.0)
@app_commands.choices(result=RESULT_CHOICES)
async def log_2v2(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None, teammate: Optional[discord.Member] = None):
    ids = [teammate.id] if teammate and not teammate.bot and teammate.id != i.user.id else []
    extra = {"participants": ids}
    if not await youtube_extra(i, youtube_link, extra): return
    await start_log(i, "2v2", result.value, [proof1, proof2], extra)

@bot.tree.command(name="log-training", description="Log training")
@app_commands.checks.cooldown(1, 30.0)
async def log_training(i: discord.Interaction, proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None):
    await start_log(i, "training", None, [proof1, proof2])

@bot.tree.command(name="log-tryout", description="Log a tryout")
@app_commands.checks.cooldown(1, 60.0)
@app_commands.choices(result=TRYOUT_CHOICES)
async def log_tryout(i: discord.Interaction, candidate: discord.Member, result: app_commands.Choice[str], proof: discord.Attachment):
    if candidate.bot or candidate.id == i.user.id: return await i.response.send_message(tr("bad_candidate"), ephemeral=True)
    extra = {"candidate_id": candidate.id, "candidate": candidate.mention, "tester_id": i.user.id}
    await start_log(i, "tryout", result.value, [proof], extra)

@bot.tree.command(name="resubmit", description="Fix and resend a rejected log")
@app_commands.checks.cooldown(1, 15.0)
async def resubmit(i: discord.Interaction, log_id: int):
    if not i.guild or not isinstance(i.user, discord.Member): return await i.response.send_message(tr("server_only"), ephemeral=True)
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id or row["author_id"] != i.user.id: return await i.response.send_message(tr("log_not_found"), ephemeral=True)
    if row["status"] != "rejected" or row["type"] not in LOG_TYPES: return await i.response.send_message("❌ This log is not rejected.", ephemeral=True)
    if not await check_cmd_perm(i, f"log-{row['type']}"): return await i.response.send_message(tr("no_role"), ephemeral=True)
    if not await can_submit(i.user, row["type"]): return await i.response.send_message(tr("no_submit_perm"), ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id FROM logs WHERE parent_id=? AND status IN ('pending','accepted') LIMIT 1", (log_id,))
        child = await cur.fetchone()
    if child: return await i.response.send_message(f"This log was already resubmitted as #{child[0]}.", ephemeral=True)
    data = json.loads(row["data"] or "{}")
    files = []
    try:
        ch = i.guild.get_channel(row["review_channel"]) if row["review_channel"] else None
        if ch and row["review_msg"]:
            old = await ch.fetch_message(row["review_msg"])
            files = list(old.attachments)
    except discord.HTTPException: pass
    if not files and data.get("proof_urls"): files = [u for u in data["proof_urls"] if isinstance(u, str)]
    if not files: return await i.response.send_message("Could not fetch the old attachments.", ephemeral=True)
    extra = {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants", "youtube_link") if k in data}
    await i.response.send_modal(LogModal(row["type"], row["result"], files, extra, defaults=data, parent_id=log_id))

@bot.tree.command(name="revoke-log", description="Cancel an accepted log")
@app_commands.checks.cooldown(1, 10.0)
async def revoke_log(i: discord.Interaction, log_id: int, reason: app_commands.Range[str, 1, 300]):
    if not await gate(i, "revoke-log"): return
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id or row["status"] != "accepted": return await i.response.send_message(tr("log_not_found"), ephemeral=True)
    if not await can_review(i.user, row["type"]): return await i.response.send_message(tr("no_perm"), ephemeral=True)
    if (row["season_id"] or "main") != await get_current_season(i.guild.id): return await i.response.send_message(tr("old_season_log"), ephemeral=True)
    await i.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("UPDATE logs SET status='revoked', reason=?, reviewer_id=? WHERE id=? AND status='accepted'", (reason, i.user.id, log_id))
            if cur.rowcount == 0:
                await db.rollback()
                return await i.followup.send(tr("log_already_reviewed"), ephemeral=True)
            cur = await db.execute("SELECT user_id, SUM(points) FROM point_events WHERE guild_id=? AND log_id=? AND type='log' GROUP BY user_id", (i.guild.id, log_id))
            events = await cur.fetchall()
            stamp = now_utc().isoformat()
            for uid, p in events:
                await db.execute("UPDATE members SET points = points - ? WHERE guild_id=? AND user_id=?", (p, i.guild.id, uid))
                await db.execute("INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?,?)", (i.guild.id, log_id, uid, -p, stamp, "revoke", row["season_id"] or "main"))
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await i.followup.send(tr("db_err"), ephemeral=True)
    data = json.loads(row["data"] or "{}")
    custom_tmpl = await get_custom_template(i.guild.id, row["type"])
    guild_emojis = await get_guild_emojis(i.guild.id)
    text = render(log_id, row["type"], row["author_id"], data, row["result"], None, "revoked", reviewer_id=i.user.id, reason=reason, custom_template=custom_tmpl, guild_emojis=guild_emojis)
    for ch_id, msg_id in ((row["review_channel"], row["review_msg"]), (row["public_channel"], row["public_msg"])):
        if ch_id and msg_id:
            try:
                ch = i.guild.get_channel(ch_id)
                msg = await ch.fetch_message(msg_id)
                await msg.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, AttributeError): pass
    await audit(f"🚫 {i.user.mention} revoked Log #{log_id}. Reason: {reason}", i.guild)
    await i.followup.send(f"✅ Successfully revoked Log #{log_id}.", ephemeral=True)

# ================================================================ المراجعة والأزرار
async def resolve_log(i: discord.Interaction, need_pending=True):
    if not i.guild or not isinstance(i.user, discord.Member):
        await i.response.send_message(tr("server_only"), ephemeral=True)
        return None
    log_id = parse_log_id(i.message)
    row = await get_log(log_id) if log_id else None
    if (not row or row["guild_id"] != i.guild.id or (row["review_msg"] and row["review_msg"] != i.message.id)):
        await i.response.send_message(tr("log_not_found"), ephemeral=True)
        return None
    if need_pending and row["status"] != "pending":
        await i.response.send_message(tr("log_already_reviewed"), ephemeral=True)
        return None
    return row

async def accept_log(i: discord.Interaction):
    row = await resolve_log(i)
    if not row: return
    if not await can_review(i.user, row["type"]): return await i.response.send_message(tr("no_review_perm"), ephemeral=True)
    if row["author_id"] == i.user.id: return await i.response.send_message(tr("no_self_review"), ephemeral=True)
    season = row["season_id"] or "main"
    if season != await get_current_season(i.guild.id): return await i.response.send_message(tr("old_season_log"), ephemeral=True)
    await i.response.defer()
    log_id = row["id"]
    data = json.loads(row["data"] or "{}")
    flags = json.loads(row["flags"] or "[]")
    pts = await points_for(i.guild.id, row["type"], row["result"])
    recips = await recipients_for(row, data)
    stamp = now_utc().isoformat()
    totals = {}
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("UPDATE logs SET status='accepted', points=?, reviewer_id=?, reviewed_at=? WHERE id=? AND status='pending'", (pts, i.user.id, stamp, log_id))
            if cur.rowcount == 0:
                await db.rollback()
                return await i.followup.send(tr("log_already_reviewed"), ephemeral=True)
            for uid in recips:
                await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (i.guild.id, uid, pts, pts))
                await db.execute("INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?,?)", (i.guild.id, log_id, uid, pts, stamp, "log", season))
                cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, uid))
                totals[uid] = (await cur.fetchone())[0]
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await i.followup.send(tr("db_err"), ephemeral=True)
    custom_tmpl = await get_custom_template(i.guild.id, row["type"])
    guild_emojis = await get_guild_emojis(i.guild.id)
    pub_id = await cfg_get(i.guild.id, row["type"], "log_channel")
    log_ch = i.guild.get_channel(pub_id) if pub_id else None
    if log_ch:
        ping_id = await cfg_get(i.guild.id, row["type"], "log_ping")
        text = render(log_id, row["type"], row["author_id"], data, row["result"], None, "accepted", pts, i.user.id, pts_to=recips, custom_template=custom_tmpl, guild_emojis=guild_emojis)
        content = (f"<@&{ping_id}>\n" if ping_id else "") + text
        cand = [int(data["candidate_id"])] if row["type"] == "tryout" and data.get("candidate_id") else None
        try: files = [await a.to_file() for a in i.message.attachments]
        except discord.HTTPException: files = []
        try:
            sent = await log_ch.send(content=content, files=files, allowed_mentions=mentions(ping_id, cand))
            await db_exec("UPDATE logs SET public_channel=?, public_msg=? WHERE id=?", (log_ch.id, sent.id, log_id))
        except discord.HTTPException as e: await i.followup.send(f"Accepted, but couldn't post in log channel: {e}", ephemeral=True)
    else: await i.followup.send("Accepted, but no log channel is set.", ephemeral=True)
    reviewed = render(log_id, row["type"], row["author_id"], data, row["result"], flags, "accepted", pts, i.user.id, pts_to=recips, custom_template=custom_tmpl, guild_emojis=guild_emojis)
    try: await i.message.edit(content=reviewed, view=None, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException: pass
    for uid, total in totals.items():
        try:
            member = await get_member(i.guild, uid)
            await update_member_rank(i.guild, member, total)
            current_streak, max_streak = await update_streak(i.guild.id, uid)
            streak_points = int(await get_custom_val(i.guild.id, "streak_reward_points", 0))
            if streak_points > 0:
                await db_exec("UPDATE members SET points = points + ? WHERE guild_id=? AND user_id=?", (streak_points, i.guild.id, uid))
                await db_exec("INSERT INTO point_events(guild_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?)", (i.guild.id, uid, streak_points, now_utc().isoformat(), "streak", season))
            await check_achievements(i.guild, uid, total)
        except (discord.NotFound, discord.Forbidden): pass
    if (row["type"] == "tryout" and row["result"] == "win" and data.get("candidate_id") and await rule(i.guild.id, "tryout_autorole")):
        try:
            cand_member = await get_member(i.guild, int(data["candidate_id"]))
            crew_role = find_crew_role(i.guild)
            if crew_role and crew_role not in cand_member.roles: await cand_member.add_roles(crew_role, reason="Passed tryout")
        except (discord.NotFound, discord.Forbidden) as e: await i.followup.send(f"Accepted, but couldn't give Crew role: {e}", ephemeral=True)

class RejectModal(discord.ui.Modal, title="Rejection Reason"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=300)
    def __init__(self, log_id, message):
        super().__init__()
        self.log_id, self.message = log_id, message
    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        row = await get_log(self.log_id)
        if not row or row["guild_id"] != i.guild.id: return await i.followup.send(tr("log_not_found"), ephemeral=True)
        if not await can_review(i.user, row["type"]): return await i.followup.send(tr("no_review_perm"), ephemeral=True)
        if row["author_id"] == i.user.id: return await i.followup.send(tr("no_self_review"), ephemeral=True)
        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute("UPDATE logs SET status='rejected', reviewer_id=?, reason=?, reviewed_at=? WHERE id=? AND status='pending'", (i.user.id, self.reason.value, now_utc().isoformat(), self.log_id))
                if cur.rowcount == 0:
                    await db.rollback()
                    return await i.followup.send(tr("log_already_reviewed"), ephemeral=True)
                await db.commit()
            except Exception as e:
                await db.rollback()
                return await i.followup.send(tr("db_err"), ephemeral=True)
        data, flags = json.loads(row["data"] or "{}"), json.loads(row["flags"] or "[]")
        custom_tmpl, guild_emojis = await get_custom_template(i.guild.id, row["type"]), await get_guild_emojis(i.guild.id)
        text = render(self.log_id, row["type"], row["author_id"], data, row["result"], flags, "rejected", reviewer_id=i.user.id, reason=self.reason.value, custom_template=custom_tmpl, guild_emojis=guild_emojis)
        try: await self.message.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException: pass
        await i.followup.send(tr("log_rejected_ok"), ephemeral=True)
        try:
            author = await bot.fetch_user(row["author_id"])
            await author.send(f"❌ Log #{self.log_id} was rejected.\nReason: {self.reason.value}\n\nUse `/resubmit log_id:{self.log_id}`")
        except discord.HTTPException: pass

class ReviewView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    async def on_error(self, interaction, error, item): await report_error(interaction, error)
    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, custom_id="log:accept")
    async def accept(self, i: discord.Interaction, button: discord.ui.Button): await accept_log(i)
    @discord.ui.button(label="Reject", emoji="❌", style=discord.ButtonStyle.danger, custom_id="log:reject")
    async def reject(self, i: discord.Interaction, button: discord.ui.Button):
        row = await resolve_log(i)
        if not row: return
        if not await can_review(i.user, row["type"]): return await i.response.send_message(tr("no_review_perm"), ephemeral=True)
        if row["author_id"] == i.user.id: return await i.response.send_message(tr("no_self_review"), ephemeral=True)
        await i.response.send_modal(RejectModal(row["id"], i.message))
    @discord.ui.button(label="Confirm Part", emoji="✋", style=discord.ButtonStyle.primary, custom_id="log:confirm_part")
    async def confirm_part(self, i: discord.Interaction, button: discord.ui.Button):
        row = await resolve_log(i)
        if not row: return
        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute("SELECT status, data FROM logs WHERE id=? AND guild_id=?", (row["id"], i.guild.id))
                log_row = await cur.fetchone()
                if not log_row or log_row[0] != "pending":
                    await db.rollback()
                    return await i.response.send_message(tr("log_already_reviewed"), ephemeral=True)
                data = json.loads(log_row[1] or "{}")
                participants = [int(x) for x in data.get("participants", [])]
                if i.user.id not in participants:
                    await db.rollback()
                    return await i.response.send_message(tr("not_tagged"), ephemeral=True)
                confirmed = [int(x) for x in data.get("confirmed_participants", [])]
                if i.user.id in confirmed:
                    await db.rollback()
                    return await i.response.send_message(tr("already_confirmed"), ephemeral=True)
                confirmed.append(i.user.id)
                data["confirmed_participants"] = confirmed
                await db.execute("UPDATE logs SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), row["id"]))
                await db.commit()
            except Exception as e:
                await db.rollback()
                return await i.response.send_message(tr("db_err"), ephemeral=True)
        flags = json.loads(row["flags"] or "[]")
        custom_tmpl, guild_emojis = await get_custom_template(i.guild.id, row["type"]), await get_guild_emojis(i.guild.id)
        updated = render(row["id"], row["type"], row["author_id"], data, row["result"], flags, custom_template=custom_tmpl, guild_emojis=guild_emojis)
        await i.response.send_message(tr("part_confirmed"), ephemeral=True)
        try: await i.message.edit(content=updated, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException: pass

class PendingPaginator(discord.ui.View):
    def __init__(self, items, guild_id, owner_user_id):
        super().__init__(timeout=180)
        self.items, self.guild_id, self.owner_user_id = items, guild_id, owner_user_id
        self.page, self.per_page = 0, 15
    def max_pages(self): return max(1, (len(self.items) - 1) // self.per_page + 1)
    def get_page_text(self):
        chunk = self.items[self.page * self.per_page:(self.page + 1) * self.per_page]
        lines = [f"**#{lid}** {LOG_TYPES.get(t, {}).get('label', t)} — <@{author}> — {str(created)[:16]}{f' https://discord.com/channels/{self.guild_id}/{ch_id}/{msg_id}' if ch_id and msg_id else ''}" for lid, t, author, created, ch_id, msg_id in chunk]
        return f"⏳ **Pending logs** — {len(self.items)} total — page {self.page + 1}/{self.max_pages()}\n\n" + "\n".join(lines)
    async def interaction_check(self, i: discord.Interaction) -> bool: return i.user.id == self.owner_user_id
    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, i: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await i.response.edit_message(content=self.get_page_text()[:1990], view=self)
    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, i: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_pages() - 1, self.page + 1)
        await i.response.edit_message(content=self.get_page_text()[:1990], view=self)

@bot.tree.command(name="pending", description="List logs waiting for review")
@app_commands.checks.cooldown(1, 10.0)
async def pending(i: discord.Interaction):
    if not await check_cmd_perm(i, "pending"): return await i.response.send_message(tr("no_role"), ephemeral=True)
    if not (await is_editor(i.user) or await can_review_any(i.user)): return await i.response.send_message(tr("no_perm"), ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id, type, author_id, created_at, review_channel, review_msg FROM logs WHERE status='pending' AND guild_id=? ORDER BY id DESC LIMIT 200", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows: return await i.response.send_message(tr("no_pending"), ephemeral=True)
    if len(rows) <= 15:
        lines = [f"**#{lid}** {LOG_TYPES.get(t, {}).get('label', t)} — <@{author}> — {str(created)[:16]}{f' https://discord.com/channels/{i.guild.id}/{ch_id}/{msg_id}' if ch_id and msg_id else ''}" for lid, t, author, created, ch_id, msg_id in rows]
        await i.response.send_message("\n".join(lines)[:1990], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        view = PendingPaginator(rows, i.guild.id, i.user.id)
        await i.response.send_message(view.get_page_text()[:1990], view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
# ================================================================ الإدارة المؤقتة (Temp Admin)
@bot.tree.command(name="grant-temp-admin", description="Grant temporary admin permissions")
@app_commands.checks.cooldown(1, 10.0)
async def grant_temp_admin(i: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 1440]):
    if not is_admin(i.user) and i.user.id != owner_id():
        return await i.response.send_message(tr("no_perm"), ephemeral=True)
    if member.bot:
        return await i.response.send_message("❌ Cannot give temp admin to a bot.", ephemeral=True)
    expires_at = (now_utc() + timedelta(minutes=minutes)).isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO temp_perms(guild_id, user_id, expires_at) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET expires_at=excluded.expires_at", (i.guild.id, member.id, expires_at))
        await db.commit()
    await audit(f"👑 {i.user.mention} granted Temp Admin to {member.mention} for {minutes} minutes.", i.guild)
    await i.response.send_message(f"✅ Granted temporary admin to {member.mention} for **{minutes}** minutes.", ephemeral=True)

@bot.tree.command(name="revoke-temp-admin", description="Revoke temporary admin permissions")
@app_commands.checks.cooldown(1, 10.0)
async def revoke_temp_admin(i: discord.Interaction, member: discord.Member):
    if not is_admin(i.user) and i.user.id != owner_id():
        return await i.response.send_message(tr("no_perm"), ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM temp_perms WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        if not await cur.fetchone():
            return await i.response.send_message("❌ This user doesn't have temp admin.", ephemeral=True)
        await db.execute("DELETE FROM temp_perms WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        await db.commit()
    await audit(f"🚫 {i.user.mention} revoked Temp Admin from {member.mention}.", i.guild)
    await i.response.send_message(f"✅ Revoked temporary admin from {member.mention}.", ephemeral=True)

# ================================================================ أنظمة الستريك والإنجازات
async def update_streak(guild_id, user_id):
    today, yesterday = now_utc().date().isoformat(), (now_utc().date() - timedelta(days=1)).isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT current_streak, max_streak, last_log_date FROM streaks WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = await cur.fetchone()
        if not row:
            await db.execute("INSERT INTO streaks(guild_id, user_id, current_streak, max_streak, last_log_date) VALUES(?,?,1,1,?)", (guild_id, user_id, today))
            await db.commit()
            return 1, 1
        current, max_s, last_date = row
        if last_date == today: return current, max_s
        elif last_date == yesterday:
            current += 1; max_s = max(max_s, current)
            await db.execute("UPDATE streaks SET current_streak=?, max_streak=?, last_log_date=? WHERE guild_id=? AND user_id=?", (current, max_s, today, guild_id, user_id))
            await db.commit()
            return current, max_s
        else:
            await db.execute("UPDATE streaks SET current_streak=1, last_log_date=? WHERE guild_id=? AND user_id=?", (today, guild_id, user_id))
            await db.commit()
            return 1, max_s

async def check_achievements(guild, user_id, current_points):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id, name FROM achievements WHERE guild_id=? AND required_points<=?", (guild.id, current_points))
        for ach_id, name in await cur.fetchall():
            cur2 = await db.execute("SELECT 1 FROM user_achievements WHERE guild_id=? AND user_id=? AND achievement_id=?", (guild.id, user_id, ach_id))
            if not await cur2.fetchone():
                await db.execute("INSERT INTO user_achievements(guild_id, user_id, achievement_id, claimed) VALUES(?,?,?,0)", (guild.id, user_id, ach_id))
                await db.commit()
                member = guild.get_member(user_id)
                if member:
                    embed = await get_custom_msg(guild.id, "achieve_msg", tr("achieve_title"), tr("achieve_desc", user=member.mention, name=name), discord.Color.purple())
                    try: await member.send(embed=embed)
                    except discord.HTTPException: pass

# ================================================================ الإعدادات والتحكم
@bot.tree.command(name="setchannel", description="Set channels")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_CHANNEL)
async def setchannel(i: discord.Interaction, log_type: app_commands.Choice[str], kind: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None):
    if not await gate(i, "setchannel", editor=True): return
    t = "all" if kind.value in ("audit_channel", "update_channel") else log_type.value
    await cfg_set(i.guild.id, t, kind.value, channel.id if channel else None)
    warn = ""
    if channel:
        perms = channel.permissions_for(i.guild.me)
        missing = [n for n, ok in (("Send Messages", perms.send_messages), ("Attach Files", perms.attach_files), ("Embed Links", perms.embed_links)) if not ok]
        if missing: warn = "\n⚠️ Missing bot permissions: " + ", ".join(missing)
    await audit(f"⚙️ {i.user.mention} set {kind.value} ({t}) to {channel.mention if channel else 'none'}", i.guild)
    await i.response.send_message(f"✅ **{kind.name}** ({t}) = {channel.mention if channel else 'Cleared'}{warn}", ephemeral=True)

@bot.tree.command(name="setping", description="Set ping roles")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_PING)
async def setping(i: discord.Interaction, log_type: app_commands.Choice[str], kind: app_commands.Choice[str], role: Optional[discord.Role] = None):
    if not await gate(i, "setping", editor=True): return
    await cfg_set(i.guild.id, log_type.value, kind.value, role.id if role else None)
    await audit(f"⚙️ {i.user.mention} set {kind.value} ({log_type.value}) to {role.mention if role else 'none'}", i.guild)
    await i.response.send_message(f"✅ **{kind.name}** ({log_type.name}) = {role.mention if role else 'Cleared'}", ephemeral=True)

@bot.tree.command(name="setroles", description="Set role permissions")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(kind=KIND_ROLES, log_type=LOG_TYPE_CHOICES, action=ACTIONS)
async def setroles(i: discord.Interaction, kind: app_commands.Choice[str], log_type: app_commands.Choice[str], action: app_commands.Choice[str], role: discord.Role):
    if not await gate(i, "setroles", editor=True): return
    await roles_edit(i.guild.id, log_type.value, kind.value, role.id, action.value == "add")
    await audit(f"⚙️ {i.user.mention} {action.value} {role.mention} → {kind.value} ({log_type.value})", i.guild)
    await i.response.send_message(f"✅ {action.name}: {role.mention} → **{kind.name}** ({log_type.name}).", ephemeral=True)

@bot.tree.command(name="setrule", description="Change a server rule")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(rule_name=RULE_CHOICES)
async def setrule(i: discord.Interaction, rule_name: app_commands.Choice[str], value: app_commands.Range[int, 0, 1000]):
    if not await gate(i, "setrule", editor=True): return
    await set_setting(i.guild.id, "rule:" + rule_name.value, value)
    await audit(f"⚙️ {i.user.mention} updated rule `{rule_name.value}` to `{value}`", i.guild)
    await i.response.send_message(f"✅ **{rule_name.value}** = {value}", ephemeral=True)

@bot.tree.command(name="settings", description="Show server settings")
@app_commands.checks.cooldown(1, 5.0)
async def settings_cmd(i: discord.Interaction):
    if not await gate(i, "settings", editor=True): return
    g = i.guild.id
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT cmd_name, role_id FROM cmd_roles WHERE guild_id=?", (g,))
        cmd_rows = await cur.fetchall()
    embed = discord.Embed(title=f"⚙️ Server Settings — {BOT_VERSION}", color=discord.Color.blue(), timestamp=now_utc())
    audit_ch = await cfg_get(g, "all", "audit_channel")
    embed.add_field(name="📍 Audit Channel", value=f"<#{audit_ch}>" if audit_ch else "Not set", inline=False)
    ch_text = ""
    for t in LOG_TYPES:
        rv, lg = await cfg_get(g, t, "review_channel"), await cfg_get(g, t, "log_channel")
        ch_text += f"**{LOG_TYPES[t]['label']}**\nReview: {f'<#{rv}>' if rv else 'Not set'} | Log: {f'<#{lg}>' if lg else 'Not set'}\n"
    embed.add_field(name="📺 Channels", value=ch_text[:1000] or "None", inline=False)
    rule_lines = [f"• `{k}`: {await rule(g, k)}" for k in RULE_LABELS]
    embed.add_field(name="📜 Rules", value="\n".join(rule_lines)[:1000], inline=False)
    if cmd_rows: embed.add_field(name="🔒 Command Locks", value="\n".join(f"• `/{c}` → <@&{r}>" for c, r in cmd_rows)[:1000], inline=False)
    await i.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="sync", description="Sync commands")
@app_commands.checks.cooldown(1, 30.0)
async def sync_cmd(i: discord.Interaction):
    if not await gate(i, "sync", editor=True): return
    await i.response.defer(ephemeral=True)
    ok = await bot.sync_guild(i.guild)
    await i.followup.send(tr("sync_ok") if ok else tr("sync_fail"))

async def cmd_name_autocomplete(i: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return [app_commands.Choice(name=c, value=c) for c in ALL_BOT_COMMANDS if current.lower() in c.lower()][:25]

@bot.tree.command(name="set-cmd-perms", description="Control command permissions")
@app_commands.autocomplete(command=cmd_name_autocomplete)
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(action=[
    app_commands.Choice(name="Add Role", value="add_role"), app_commands.Choice(name="Remove Role", value="remove_role"),
    app_commands.Choice(name="Add User", value="add_user"), app_commands.Choice(name="Remove User", value="remove_user"),
    app_commands.Choice(name="Clear All", value="clear")
])
async def set_cmd_perms(i: discord.Interaction, command: str, action: app_commands.Choice[str], role: Optional[discord.Role] = None, member: Optional[discord.Member] = None):
    if not await gate(i, "set-cmd-perms", editor=True): return
    if command not in ALL_BOT_COMMANDS: return await i.response.send_message("❌ Invalid command.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        if action.value == "add_role" and role:
            await db.execute("INSERT INTO cmd_roles(guild_id, cmd_name, role_id) VALUES(?,?,?) ON CONFLICT(guild_id, cmd_name) DO UPDATE SET role_id=excluded.role_id", (i.guild.id, command, role.id))
            msg = f"🔒 `/{command}` locked to role {role.mention}."
        elif action.value == "remove_role":
            await db.execute("DELETE FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
            msg = f"🔓 `/{command}` role lock removed."
        elif action.value == "add_user" and member:
            await db.execute("INSERT OR IGNORE INTO cmd_users(guild_id, cmd_name, user_id) VALUES(?,?,?)", (i.guild.id, command, member.id))
            msg = f"🔒 `/{command}` locked to user {member.mention}."
        elif action.value == "remove_user" and member:
            await db.execute("DELETE FROM cmd_users WHERE guild_id=? AND cmd_name=? AND user_id=?", (i.guild.id, command, member.id))
            msg = f"🔓 `/{command}` user lock removed."
        elif action.value == "clear":
            await db.execute("DELETE FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
            await db.execute("DELETE FROM cmd_users WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
            msg = f"🔓 `/{command}` is now fully open."
        else: return await i.response.send_message("❌ Provide a role or member.", ephemeral=True)
        await db.commit()
    await save_config_snapshot_json()
    await audit(f"⚙️ {i.user.mention} updated permissions for `/{command}`", i.guild)
    await i.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="set-update-ping", description="Set role pinged on updates")
@app_commands.checks.cooldown(1, 5.0)
async def set_update_ping(i: discord.Interaction, role: Optional[discord.Role] = None):
    if not await gate(i, "set-update-ping", editor=True): return
    await cfg_set(i.guild.id, "all", "update_ping", role.id if role else None)
    await audit(f"⚙️ {i.user.mention} set update ping to {role.mention if role else 'None'}", i.guild)
    await i.response.send_message(f"✅ Update ping role set to {role.mention}." if role else "🗑️ Update ping cleared.", ephemeral=True)

@bot.tree.command(name="set-log-template", description="Customize a log layout")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(log_type=LOG_TYPE_CHOICES[1:])
async def set_log_template(i: discord.Interaction, log_type: app_commands.Choice[str], template: Optional[app_commands.Range[str, 1, 1200]] = None):
    if not await gate(i, "set-log-template", editor=True): return
    key = f"template:{log_type.value}"
    if template:
        await set_setting(i.guild.id, key, template)
        await audit(f"🎨 {i.user.mention} updated the log template for **{log_type.name}**", i.guild)
        await i.response.send_message(f"✅ Template updated for **{log_type.name}**.", ephemeral=True)
    else:
        current_template = await get_custom_template(i.guild.id, log_type.value)
        msg = f"📝 **Current Template for {log_type.name}:**\n```\n{current_template}\n```\n*Use variables like `{{MOON}}`, `{{ARROW}}`, `{{CROWN}}`, `{{DIVIDER}}`, `{{author}}`, `{{score}}`, `{{result}}`.*"
        await i.response.send_message(msg[:1990], ephemeral=True)

# ================================================================ إدارة الإيموجيات المخصصة
EMOJI_SLOT_CHOICES = [app_commands.Choice(name="👼 Angel", value="angel"), app_commands.Choice(name="🌙 Moon", value="moon"), app_commands.Choice(name="➡️ Arrow", value="arrow"), app_commands.Choice(name="👑 Crown", value="crown"), app_commands.Choice(name="◀️ Divider Left", value="divider_left"), app_commands.Choice(name="▶️ Divider Right", value="divider_right")]

@bot.tree.command(name="set-log-emojis", description="Set custom emojis for logs")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(slot=EMOJI_SLOT_CHOICES)
async def set_log_emojis(i: discord.Interaction, slot: app_commands.Choice[str], emoji: Optional[str] = None):
    if not await gate(i, "set-log-emojis", editor=True): return
    if emoji is None or not emoji.strip():
        async with aiosqlite.connect(DB) as db:
            await db.execute("DELETE FROM log_emojis WHERE guild_id=? AND slot=?", (i.guild.id, slot.value))
            await db.commit()
        invalidate_emoji_cache(i.guild.id)
        return await i.response.send_message(f"🗑️ Cleared **{slot.name}**.", ephemeral=True)
    emoji = emoji.strip()
    if len(emoji) > 150: return await i.response.send_message("❌ Emoji is too long.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO log_emojis(guild_id, slot, emoji) VALUES(?,?,?) ON CONFLICT(guild_id, slot) DO UPDATE SET emoji=excluded.emoji", (i.guild.id, slot.value, emoji))
        await db.commit()
    invalidate_emoji_cache(i.guild.id)
    await audit(f"🎨 {i.user.mention} set log emoji `{slot.value}` to {emoji}", i.guild)
    await i.response.send_message(f"✅ **{slot.name}** → {emoji}", ephemeral=True)

@bot.tree.command(name="show-log-emojis", description="Show current log emojis")
@app_commands.checks.cooldown(1, 5.0)
async def show_log_emojis(i: discord.Interaction):
    if not await gate(i, "show-log-emojis"): return
    emojis = await get_guild_emojis(i.guild.id)
    embed = discord.Embed(title="🎨 Current Log Emojis", color=discord.Color.blue())
    slot_names = {"angel": "👼 Angel", "moon": "🌙 Moon", "arrow": "➡️ Arrow", "crown": "👑 Crown", "divider_left": "◀️ Left", "divider_right": "▶️ Right"}
    for slot, name in slot_names.items(): embed.add_field(name=name, value=emojis.get(slot, "— (none)"), inline=True)
    await i.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="reset-log-emojis", description="Reset all log emojis")
@app_commands.checks.cooldown(1, 10.0)
async def reset_log_emojis(i: discord.Interaction):
    if not await gate(i, "reset-log-emojis", editor=True): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM log_emojis WHERE guild_id=?", (i.guild.id,))
        await db.commit()
    invalidate_emoji_cache(i.guild.id)
    await audit(f"🗑️ {i.user.mention} cleared all log emojis", i.guild)
    await i.response.send_message("🗑️ All custom log emojis cleared.", ephemeral=True)

# ================================================================ النقط والترتيب
@bot.tree.command(name="set-log-points", description="Set log points")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(log=POINT_CHOICES)
async def set_log_points(i: discord.Interaction, log: app_commands.Choice[str], points: app_commands.Range[int, 0, 1000]):
    if not await gate(i, "set-log-points", editor=True): return
    await set_setting(i.guild.id, "points:" + log.value, points)
    await audit(f"⚙️ {i.user.mention} set log points for **{log.name}** to **{points}**", i.guild)
    await i.response.send_message(f"✅ **{log.name}** = {points} points (new logs only).", ephemeral=True)

@bot.tree.command(name="set-rank-points", description="Set rank threshold")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(rank=RANK_CHOICES)
async def set_rank_points(i: discord.Interaction, rank: app_commands.Choice[str], points: app_commands.Range[int, 0, 100000]):
    if not await gate(i, "set-rank-points", editor=True): return
    ladder = await get_ladder(i.guild.id)
    idx = next(k for k, (n, _) in enumerate(ladder) if n == rank.value)
    prev_pts = ladder[idx - 1][1] if idx > 0 else 0
    next_pts = ladder[idx + 1][1] if idx < len(ladder) - 1 else None
    if (idx > 0 and points <= prev_pts) or (next_pts is not None and points >= next_pts):
        return await i.response.send_message(f"❌ Number must be > {prev_pts} and < {next_pts if next_pts is not None else '∞'}.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    await set_setting(i.guild.id, "rank:" + rank.value, points)
    changed = await bulk_update_ranks(i.guild)
    await audit(f"⚙️ {i.user.mention} set rank **{rank.name}** to **{points}** ({changed} promotions)", i.guild)
    await i.followup.send(f"✅ **{rank.name}** = {points} points. Promotions applied: {changed}.", ephemeral=True)

@bot.tree.command(name="set-points", description="Set member points")
@app_commands.checks.cooldown(1, 5.0)
async def set_points(i: discord.Interaction, member: discord.Member, points: app_commands.Range[int, 0, 100000]):
    if not await gate(i, "set-points", editor=True): return
    await i.response.defer(ephemeral=True)
    season = await get_current_season(i.guild.id)
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
            row = await cur.fetchone()
            delta = points - (row[0] if row else 0)
            await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points=excluded.points", (i.guild.id, member.id, points))
            if delta != 0: await db.execute("INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?,?)", (i.guild.id, None, member.id, delta, now_utc().isoformat(), "manual", season))
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await i.followup.send(tr("db_err"), ephemeral=True)
    await update_member_rank(i.guild, member, points)
    await audit(f"⚙️ {i.user.mention} set points of {member.mention} to **{points}** (Δ {delta:+d})", i.guild)
    note = "\n⚠️ Member holds a rank above their points. Use `/sync-ranks demote:True`." if await rank_is_above_points(i.guild, member, points) else ""
    await i.followup.send(f"✅ {member.display_name} = {points} points.{note}", ephemeral=True)

async def ledger_mismatches(guild_id: int):
    season = await get_current_season(guild_id)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, SUM(points) FROM point_events WHERE guild_id=? AND season_id=? GROUP BY user_id", (guild_id, season))
        ledger = {u: p for u, p in await cur.fetchall()}
        cur = await db.execute("SELECT user_id, points FROM members WHERE guild_id=?", (guild_id,))
        cached = {u: p for u, p in await cur.fetchall()}
    return [(u, cached.get(u, 0), ledger.get(u, 0)) for u in sorted(set(ledger) | set(cached)) if cached.get(u, 0) != ledger.get(u, 0)]

@bot.tree.command(name="verify-points", description="Check balances against ledger")
@app_commands.checks.cooldown(1, 30.0)
async def verify_points(i: discord.Interaction, fix: bool = False, confirm: Optional[str] = None):
    if not await gate(i, "verify-points", editor=True): return
    await i.response.defer(ephemeral=True)
    bad = await ledger_mismatches(i.guild.id)
    if not bad: return await i.followup.send("✅ All balances match the points ledger.", ephemeral=True)
    if fix:
        if len(bad) > 5 and confirm != "FIX": return await i.followup.send(f"⚠️ Found **{len(bad)}** mismatches. Add `confirm:FIX` to repair.", ephemeral=True)
        async with aiosqlite.connect(DB) as db:
            for uid, _, led in bad: await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points=excluded.points", (i.guild.id, uid, led))
            await db.commit()
        await audit(f"🛠️ {i.user.mention} repaired {len(bad)} balances from the ledger", i.guild)
    lines = [f"<@{u}>: cached {c} / ledger {l}" for u, c, l in bad[:15]]
    await i.followup.send(f"{'🛠️ Fixed' if fix else '⚠️ Found'} {len(bad)} mismatch(es):\n" + "\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name="sync-ranks", description="Re-check ranks from points")
@app_commands.checks.cooldown(1, 60.0)
async def sync_ranks(i: discord.Interaction, member: Optional[discord.Member] = None, demote: bool = False):
    if not await gate(i, "sync-ranks", editor=True): return
    await i.response.defer(ephemeral=True)
    if member:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
            row = await cur.fetchone()
        changed = 1 if await update_member_rank(i.guild, member, row[0] if row else 0, demote) else 0
    else: changed = await bulk_update_ranks(i.guild, allow_demote=demote)
    await audit(f"⚙️ {i.user.mention} ran sync-ranks (demote={demote}), changes: {changed}", i.guild)
    await i.followup.send(f"✅ Done. Rank changes: {changed}.", ephemeral=True)

# ================================================================ المواسم والنسخ الاحتياطي
@bot.tree.command(name="season-reset", description="Archive points & start new season")
@app_commands.checks.cooldown(1, 60.0)
async def season_reset(i: discord.Interaction, name: app_commands.Range[str, 1, 50], confirm: str, reset_ranks: bool = False):
    if not await gate(i, "season-reset", editor=True): return
    if confirm != "RESET": return await i.response.send_message("❌ Type RESET to proceed.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    name = name.strip()
    new_season_id = f"season_{uuid.uuid4().hex[:6]}"
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT 1 FROM season_archive WHERE season=? AND guild_id=? LIMIT 1", (name, i.guild.id))
            if await cur.fetchone():
                await db.rollback()
                return await i.followup.send(f"❌ Season name **{name}** already exists.", ephemeral=True)
            await db.execute("INSERT INTO season_archive SELECT ?, guild_id, user_id, points FROM members WHERE guild_id=? AND points>0", (name, i.guild.id))
            await db.execute("UPDATE members SET points=0 WHERE guild_id=?", (i.guild.id,))
            await db.execute("UPDATE logs SET status='rejected', reason='Season ended before review', reviewed_at=? WHERE guild_id=? AND status='pending'", (now_utc().isoformat(), i.guild.id))
            await db.execute("INSERT INTO settings(guild_id, key, value) VALUES(?, 'state:current_season', ?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (i.guild.id, new_season_id))
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await i.followup.send(tr("db_err"), ephemeral=True)
    stripped = 0
    if reset_ranks:
        await _ensure_members(i.guild)
        lroles = [r for r in ladder_roles(i.guild) if r]
        for m in i.guild.members:
            held = [r for r in lroles if r in m.roles]
            if held and not m.bot:
                try:
                    await m.remove_roles(*held, reason="Season reset")
                    stripped += 1
                except discord.HTTPException: pass
                await asyncio.sleep(0.3)
    await audit(f"🎉 {i.user.mention} started season **{name}** (`{new_season_id}`).", i.guild)
    await i.followup.send(f"✅ Season **{name}** started. Points archived and reset." + (f" Ranks removed from {stripped} members." if reset_ranks else ""), ephemeral=True)

@bot.tree.command(name="season-top", description="Top 10 of an archived season")
@app_commands.checks.cooldown(1, 10.0)
async def season_top(i: discord.Interaction, name: app_commands.Range[str, 1, 50]):
    if not await gate(i, "season-top"): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM season_archive WHERE season=? AND guild_id=? ORDER BY points DESC LIMIT 10", (name.strip(), i.guild.id))
        rows = await cur.fetchall()
    if not rows: return await i.response.send_message("ℹ️ Season archive not found.", ephemeral=True)
    text = f"🏆 **Archived Season: {name}**\n" + "\n".join(f"**{n}.** <@{u}> — {p}" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())

def _backup_file(path):
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(path)
    try: src.backup(dst)
    finally:
        dst.close()
        src.close()

@bot.tree.command(name="backup", description="Secure backup tool")
@app_commands.checks.cooldown(1, 60.0)
@app_commands.choices(scope=BACKUP_SCOPE_CHOICES, destination=BACKUP_DEST_CHOICES)
async def backup_cmd(i: discord.Interaction, scope: app_commands.Choice[str], destination: app_commands.Choice[str]):
    if not await gate(i, "backup", editor=True): return
    if scope.value == "full":
        if not owner_id() or i.user.id != owner_id(): return await i.response.send_message("❌ Full DB backups are for owner only.", ephemeral=True)
        if destination.value != "owner": return await i.response.send_message("❌ Full DB backups can only be sent to owner DM.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    path = None
    try:
        suffix = uuid.uuid4().hex[:8]
        stamp = now_utc().strftime("%Y%m%d-%H%M%S")
        if scope.value == "full":
            filename = f"crewbot-full-backup-{stamp}-{suffix}.db"
            path = os.path.join(tempfile.gettempdir(), filename)
            await asyncio.to_thread(_backup_file, path)
            payload = await asyncio.to_thread(_read_bytes, path)
        else:
            today = now_utc().strftime("%Y-%m-%d")
            filename = f"crewbot-guild-{i.guild.id}-{today}-{suffix}.json"
            async with aiosqlite.connect(DB) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM logs WHERE guild_id=? AND created_at >= ?", (i.guild.id, today))
                logs_today = [dict(r) for r in await cur.fetchall()]
                cur = await db.execute("SELECT * FROM point_events WHERE guild_id=? AND created_at >= ?", (i.guild.id, today))
                events_today = [dict(r) for r in await cur.fetchall()]
            payload = json.dumps({"version": BOT_VERSION, "guild_id": i.guild.id, "date": today, "logs": logs_today, "point_events": events_today, "exported_at": now_utc().isoformat()}, indent=2, ensure_ascii=False).encode("utf-8")
        file_obj = discord.File(io.BytesIO(payload), filename=filename)
        if destination.value == "owner":
            ok = await dm_owner(f"🗄️ Backup (`{scope.name}`) from **{i.guild.name}**:", file=file_obj)
            await audit(f"🗄️ {i.user.mention} exported a backup (`{scope.value}`) to owner DM.", i.guild)
            await i.followup.send("✅ Backup sent to owner's DM." if ok else "❌ Couldn't DM the owner.", ephemeral=True)
        else:
            await audit(f"🗄️ {i.user.mention} exported a guild backup (`{scope.value}`) in chat.", i.guild)
            await i.followup.send(f"🗄️ Backup ({scope.name}):", file=file_obj, ephemeral=True)
    except Exception as e:
        print(f"❌ Backup error: {e}")
        await i.followup.send("❌ Backup failed.", ephemeral=True)
    finally:
        if path and os.path.exists(path):
            try: os.unlink(path)
            except OSError: pass
# ================================================================ AI Context & Core Logic
class AIContext:
    def __init__(self, guild, user, can_use_tools, channel=None):
        self.guild = guild
        self.user = user
        self.can_use_tools = can_use_tools
        self.channel = channel

async def _ai_can_sensitive(ctx):
    if is_admin(ctx.user): return True
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM ai_admin_users WHERE guild_id=? AND user_id=?", (ctx.guild.id, ctx.user.id))
        return bool(await cur.fetchone())

async def _web_search(query: str) -> str:
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as r:
                if r.status != 200: return f"❌ Search failed (HTTP {r.status})."
                html = await r.text()
                import re as _re
                results = _re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>', html)
                snippets = _re.findall(r'<a class="result__snippet"[^>]*>(.*?)</a>', html)
                out = []
                for i, (link, title) in enumerate(results[:4]):
                    title = _re.sub(r'<.*?>', '', title)
                    snippet = _re.sub(r'<.*?>', '', snippets[i]) if i < len(snippets) else ""
                    out.append(f"[{title}]({link})\n{snippet}")
                return "\n\n".join(out) if out else "No results found."
    except Exception as e:
        return f"❌ Search error: {e}"

async def _exec_ai_tool(ctx, name, args):
    if not ctx.can_use_tools: return "❌ عذراً، ليس لديك الرتبة المطلوبة لتوجيه أوامر إدارية للبوت. يمكنك الدردشة فقط."
    if TOOL_META.get(name, {}).get("requires_admin") and not await _ai_can_sensitive(ctx):
        return "❌ DENIED: You are not authorized for sensitive commands."
    g = ctx.guild
    try:
        if name == "search_web": return await _web_search(args.get("query", ""))
        if name == "get_member_info":
            m = _find_member(g, args.get("member_query", ""))
            if not m: return "❌ Member not found."
            season = await get_current_season(g.id)
            async with aiosqlite.connect(DB) as db:
                cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (g.id, m.id))
                pr = await cur.fetchone()
                cur = await db.execute("SELECT current_streak,max_streak FROM streaks WHERE guild_id=? AND user_id=?", (g.id, m.id))
                sr = await cur.fetchone()
            pts = pr[0] if pr else 0
            st = f"{sr[0]}d (max {sr[1]}d)" if sr else "0d"
            rank = next((r.name for r in ladder_roles(g) if r and r in m.roles), "—")
            return f"👤 **{m.display_name}** (`{m.id}`)\n• Points: **{pts}**\n• Rank: {rank}\n• Streak: {st}"
        if name == "get_server_info": return f"📊 **{g.name}**\n• Members: {g.member_count}\n• Roles: {len(g.roles)}\n• Channels: {len(g.channels)}"
        if name == "list_channels":
            cats, no_cat = {}, []
            for c in g.channels:
                if isinstance(c, discord.CategoryChannel): cats[c.name] = []
            for c in g.channels:
                if isinstance(c, discord.CategoryChannel): continue
                line = f"  • #{c.name}" if isinstance(c, discord.TextChannel) else f"  • 🔊 {c.name}"
                if c.category and c.category.name in cats: cats[c.category.name].append(line)
                else: no_cat.append(line)
            parts = [f"**{k}**\n" + "\n".join(v) for k, v in cats.items() if v]
            if no_cat: parts.append("**Uncategorized**\n" + "\n".join(no_cat))
            return "\n\n".join(parts)[:1900] or "No channels."
        if name == "list_roles":
            rs = sorted([r for r in g.roles if not r.is_default()], key=lambda x: -x.position)[:30]
            return "\n".join(f"• **{r.name}** — {len(r.members)} members" for r in rs) or "No roles."
        if name == "create_role":
            r = await g.create_role(name=args["name"], color=_hex_color(args.get("color_hex")), reason=f"AI by {ctx.user}")
            return f"✅ Created role **{r.mention}**"
        if name == "edit_role":
            r = _find_role(g, args.get("role_query", ""))
            if not r: return "❌ Role not found."
            kw = {"reason": f"AI by {ctx.user}"}
            if args.get("new_name"): kw["name"] = args["new_name"]
            if args.get("new_color_hex"): kw["color"] = _hex_color(args["new_color_hex"])
            await r.edit(**kw)
            return f"✅ Updated role **{r.name}**"
        if name == "delete_role":
            r = _find_role(g, args.get("role_query", ""))
            if not r: return "❌ Role not found."
            if r.is_default() or r.managed or r.position >= g.me.top_role.position: return "❌ Cannot delete."
            n = r.name
            await r.delete(reason=f"AI by {ctx.user}")
            return f"✅ Deleted role **{n}**"
        if name == "create_channel":
            ct = args.get("channel_type", "text")
            cat = _find_channel(g, args.get("category_query", "")) if args.get("category_query") else None
            kw = {"name": args["name"], "category": cat, "reason": f"AI by {ctx.user}"}
            ch = await (g.create_voice_channel(**kw) if ct == "voice" else g.create_text_channel(**kw))
            return f"✅ Created {ct} channel {ch.mention}"
        if name == "delete_channel":
            c = _find_channel(g, args.get("channel_query", ""))
            if not c: return "❌ Channel not found."
            n = c.name
            await c.delete(reason=f"AI by {ctx.user}")
            return f"✅ Deleted channel **{n}**"
        if name == "give_role":
            m = _find_member(g, args.get("member_query", ""))
            r = _find_role(g, args.get("role_query", ""))
            if not m or not r: return "❌ Member or role not found."
            await m.add_roles(r, reason=f"AI by {ctx.user}")
            return f"✅ Gave **{r.name}** to **{m.display_name}**"
        if name == "remove_role":
            m = _find_member(g, args.get("member_query", ""))
            r = _find_role(g, args.get("role_query", ""))
            if not m or not r: return "❌ Member or role not found."
            await m.remove_roles(r, reason=f"AI by {ctx.user}")
            return f"✅ Removed **{r.name}** from **{m.display_name}**"
        if name == "timeout_member":
            m = _find_member(g, args.get("member_query", ""))
            if not m: return "❌ Member not found."
            mins = int(args.get("duration_minutes", 10))
            try:
                await m.timeout(timedelta(minutes=mins), reason=args.get("reason", "AI Timeout"))
                return f"✅ Muted **{m.display_name}** for {mins} minutes."
            except discord.Forbidden: return "❌ Cannot timeout (bot role too low)."
        if name == "kick_member":
            m = _find_member(g, args.get("member_query", ""))
            if not m: return "❌ Member not found."
            try:
                await m.kick(reason=args.get("reason", "AI Kick"))
                return f"✅ Kicked **{m.display_name}**."
            except discord.Forbidden: return "❌ Cannot kick (bot role too low)."
        if name == "purge_messages":
            amount = int(args.get("amount", 10))
            if not ctx.channel: return "❌ Cannot purge here."
            try:
                await ctx.channel.purge(limit=min(amount, 100))
                return f"✅ Deleted {amount} messages."
            except discord.Forbidden: return "❌ Missing Manage Messages permission."
        if name == "send_channel_message":
            c = _find_channel(g, args.get("channel_query", ""))
            if not isinstance(c, discord.TextChannel): return "❌ Text channel not found."
            await c.send(args.get("message_text", ""))
            return f"✅ Sent message in {c.mention}"
        return f"✅ {name} executed."
    except discord.Forbidden: return "❌ Forbidden: missing Discord permission."
    except Exception as e: return f"❌ {type(e).__name__}: {str(e)[:200]}"

async def _ai_call(messages, image_url=None, is_vision=False):
    if not AI_API_KEY: return {"error": "AI_API_KEY missing."}
    prov_key = "groq_vision" if is_vision else AI_PROVIDER
    if prov_key not in _PROVIDERS: return {"error": f"Unknown provider: {prov_key}"}
    url, model = _PROVIDERS[prov_key]
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    
    if is_vision and image_url:
        last_msg = messages[-1]
        messages[-1] = {
            "role": "user",
            "content": [
                {"type": "text", "text": last_msg["content"]},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]
        }
    
    payload = {"model": model, "messages": messages, "tools": TOOLS if not is_vision else [], "tool_choice": "auto" if not is_vision else "none", "temperature": 0.6, "max_tokens": 800}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as s:
            async with s.post(url, headers=headers, json=payload) as r:
                if r.status != 200: return {"error": f"HTTP {r.status}: {(await r.text())[:300]}"}
                data = await r.json()
                return {"message": data["choices"][0]["message"]}
    except asyncio.TimeoutError: return {"error": "Timeout — try again."}
    except Exception as e: return {"error": f"{type(e).__name__}: {str(e)[:200]}"}

async def _run_ai(ctx, text, image_url=None):
    key = (ctx.guild.id, ctx.user.id)
    lang = detect_language(text)
    
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT history FROM ai_memory WHERE guild_id=? AND user_id=?", (ctx.guild.id, ctx.user.id))
        row = await cur.fetchone()
    
    history = json.loads(row[0]) if row else [{"role": "system", "content": _build_system_prompt(lang, ctx.can_use_tools)}]
    
    if not any(lang in str(h.get("content", "")) for h in history if h["role"] == "system"):
        history = [{"role": "system", "content": _build_system_prompt(lang, ctx.can_use_tools)}]

    history.append({"role": "user", "content": text})
    if len(history) > 20: history = [history[0]] + history[-18:]

    for _ in range(4):
        res = await _ai_call(history, image_url=image_url, is_vision=bool(image_url))
        if "error" in res: return f"❌ **AI Error:**\n```\n{res['error']}\n```"
            
        msg = res["message"]
        tcs = msg.get("tool_calls") or []
        
        if not tcs:
            history.append({"role": "assistant", "content": msg.get("content", "")})
            async with aiosqlite.connect(DB) as db:
                await db.execute("INSERT INTO ai_memory(guild_id, user_id, history) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET history=excluded.history", (ctx.guild.id, ctx.user.id, json.dumps(history, ensure_ascii=False)))
                await db.commit()
            return msg.get("content", "") or "(no reply)"

        history.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": tcs})
        for tc in tcs:
            fn = tc["function"]
            try: args = json.loads(fn.get("arguments") or "{}")
            except Exception: args = {}
            out = await _exec_ai_tool(ctx, fn["name"], args)
            history.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": out[:1900]})

    return "⚠️ Tool loop stopped."

# ================================================================ AI Commands & Mentions
@bot.tree.command(name="ai", description="Chat with AI in your language / كلم المساعد الذكي بلغتك")
async def ai_cmd(i: discord.Interaction, message: str):
    if not i.guild: return await i.response.send_message("❌ Server only.", ephemeral=True)
    if not AI_API_KEY: return await i.response.send_message("❌ AI not configured.", ephemeral=True)
    await i.response.defer()
    ctx = AIContext(i.guild, i.user, await _ai_can_sensitive(i), i.channel)
    reply = await _run_ai(ctx, message)
    for j in range(0, min(len(reply), 5700), 1900): await i.followup.send(reply[j:j+1900])

@bot.tree.command(name="ai-reset", description="Clear your AI memory / امسح ذاكرة المحادثة")
async def ai_reset(i: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM ai_memory WHERE guild_id=? AND user_id=?", (i.guild_id, i.user.id))
        await db.commit()
    await i.response.send_message("✅ Memory cleared / تم مسح الذاكرة.", ephemeral=True)

@bot.tree.command(name="set-ai-mention-roles", description="تحديد الرتبة المسموح لها بإعطاء أوامر للبوت بالمنشن")
@app_commands.choices(action=[app_commands.Choice(name="Add Role", value="add"), app_commands.Choice(name="Remove Role", value="remove"), app_commands.Choice(name="Clear", value="clear")])
async def set_ai_mention_roles(i: discord.Interaction, action: app_commands.Choice[str], role: Optional[discord.Role] = None):
    if not is_admin(i.user): return await i.response.send_message("❌ Admin only.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        if action.value == "add" and role:
            await db.execute("INSERT OR IGNORE INTO ai_mention_roles(guild_id, role_id) VALUES(?,?)", (i.guild.id, role.id))
            msg = f"✅ الرتبة {role.mention} أصبحت مسموح لها بإعطاء أوامر للبوت."
        elif action.value == "remove" and role:
            await db.execute("DELETE FROM ai_mention_roles WHERE guild_id=? AND role_id=?", (i.guild.id, role.id))
            msg = f"🗑️ تم إزالة الرتبة {role.mention}."
        elif action.value == "clear":
            await db.execute("DELETE FROM ai_mention_roles WHERE guild_id=?", (i.guild.id,))
            msg = "🗑️ تم مسح كل الرتب."
        else: return await i.response.send_message("❌ Provide a role.", ephemeral=True)
        await db.commit()
    await i.response.send_message(msg, ephemeral=True)

@bot.event
async def on_message(message):
    if message.author.bot: return
    if not message.guild: return
    
    content_lower = message.content.lower()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT reply FROM auto_replies WHERE guild_id=? AND trigger_word=?", (message.guild.id, content_lower))
        auto_row = await cur.fetchone()
        if auto_row:
            await message.reply(auto_row[0])
            return

    if bot.user in message.mentions:
        content = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if not content and not message.attachments: return await message.reply("أهلاً بك! كيف يمكنني مساعدتك؟ 👋")

        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT role_id FROM ai_mention_roles WHERE guild_id=?", (message.guild.id,))
            allowed_roles = [r[0] for r in await cur.fetchall()]
            
        is_allowed = is_admin(message.author) or any(r.id in allowed_roles for r in message.author.roles)
        ctx = AIContext(message.guild, message.author, is_allowed, message.channel)
        
        image_url = None
        if message.attachments:
            for att in message.attachments:
                if att.content_type and "image" in att.content_type:
                    image_url = att.url
                    break
        
        async with message.channel.typing():
            reply = await _run_ai(ctx, content or "حلل الصورة دي", image_url=image_url)
            for j in range(0, min(len(reply), 5700), 1900):
                await message.reply(reply[j:j+1900])
    await bot.process_commands(message)

@bot.tree.command(name="add-auto-reply", description="إضافة رد تلقائي لكلمة معينة")
async def add_auto_reply(i: discord.Interaction, trigger_word: str, reply: str):
    if not await gate(i, "add-auto-reply", editor=True): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO auto_replies(guild_id, trigger_word, reply) VALUES(?,?,?) ON CONFLICT(guild_id, trigger_word) DO UPDATE SET reply=excluded.reply", (i.guild.id, trigger_word.lower(), reply))
        await db.commit()
    await i.response.send_message(f"✅ البوت هيرد بـ `{reply}` لما حد يقول `{trigger_word}`.", ephemeral=True)

# ================================================================ الاقتصاد (بدون كازينو)
@bot.tree.command(name="transfer", description="تحويل نقاط لعضو تاني")
async def transfer_cmd(i: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 100000]):
    if member.id == i.user.id: return await i.response.send_message("❌ متحولش لنفسك يا نجم.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
        sender_pts = (await cur.fetchone() or [0])[0]
        if sender_pts < amount:
            await db.rollback()
            return await i.response.send_message("❌ رصيدك مش كفاية.", ephemeral=True)
        await db.execute("UPDATE members SET points = points - ? WHERE guild_id=? AND user_id=?", (amount, i.guild.id, i.user.id))
        await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (i.guild.id, member.id, amount, amount))
        stamp = now_utc().isoformat()
        season = await get_current_season(i.guild.id)
        await db.execute("INSERT INTO point_events(guild_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?)", (i.guild.id, i.user.id, -amount, stamp, "transfer_out", season))
        await db.execute("INSERT INTO point_events(guild_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?)", (i.guild.id, member.id, amount, stamp, "transfer_in", season))
        await db.commit()
    await i.response.send_message(f"✅ تم تحويل **{amount}** نقطة لـ {member.mention} بنجاح!", ephemeral=True)

# ================================================================ الرتب التفاعلية (Reaction Roles)
@bot.tree.command(name="setup-reaction-role", description="إنشاء لوحة رتب تفاعلية (أزرار تفاعل)")
async def setup_reaction_role(i: discord.Interaction, title: str, description: str, emoji1: str, role1: discord.Role, emoji2: Optional[str] = None, role2: Optional[discord.Role] = None):
    if not await gate(i, "setup-reaction-role", editor=True): return
    embed = discord.Embed(title=title, description=description, color=discord.Color.blue())
    msg = await i.channel.send(embed=embed)
    async with aiosqlite.connect(DB) as db:
        await msg.add_reaction(emoji1)
        await db.execute("INSERT OR REPLACE INTO reaction_roles VALUES(?,?,?,?)", (i.guild.id, msg.id, emoji1, role1.id))
        if emoji2 and role2:
            await msg.add_reaction(emoji2)
            await db.execute("INSERT OR REPLACE INTO reaction_roles VALUES(?,?,?,?)", (i.guild.id, msg.id, emoji2, role2.id))
        await db.commit()
    await i.response.send_message("✅ تم إنشاء لوحة الرتب التفاعلية بنجاح!", ephemeral=True)

@bot.event
async def on_raw_reaction_add(payload):
    if payload.guild_id is None or payload.user_id == bot.user.id: return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT role_id FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?", (payload.guild_id, payload.message_id, str(payload.emoji)))
        row = await cur.fetchone()
        if row:
            guild = bot.get_guild(payload.guild_id)
            role, member = guild.get_role(row[0]), guild.get_member(payload.user_id)
            if role and member:
                try: await member.add_roles(role, reason="Reaction Role")
                except discord.Forbidden: pass

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.guild_id is None: return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT role_id FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?", (payload.guild_id, payload.message_id, str(payload.emoji)))
        row = await cur.fetchone()
        if row:
            guild = bot.get_guild(payload.guild_id)
            role, member = guild.get_role(row[0]), guild.get_member(payload.user_id)
            if role and member:
                try: await member.remove_roles(role, reason="Reaction Role Removed")
                except discord.Forbidden: pass

# ================================================================ البطولات (Tournaments)
class TournamentJoinView(discord.ui.View):
    def __init__(self, tournament_id):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id

    @discord.ui.button(label="تسجيل الدخول / Join", style=discord.ButtonStyle.success, emoji="⚔️")
    async def join_btn(self, i: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT status, max_players FROM tournaments WHERE id=?", (self.tournament_id,))
            tour = await cur.fetchone()
            if not tour or tour[0] != "open":
                return await i.response.send_message("❌ البطولة دي اتقفلت خلاص أو مش موجودة.", ephemeral=True)
            cur = await db.execute("SELECT 1 FROM tournament_players WHERE tournament_id=? AND user_id=?", (self.tournament_id, i.user.id))
            if await cur.fetchone():
                return await i.response.send_message("❌ إنت مسجل فيها خلاص يا بطل!", ephemeral=True)
            await db.execute("INSERT INTO tournament_players(tournament_id, user_id) VALUES(?,?)", (self.tournament_id, i.user.id))
            await db.commit()
            cur = await db.execute("SELECT COUNT(*) FROM tournament_players WHERE tournament_id=?", (self.tournament_id,))
            count = (await cur.fetchone())[0]
        await i.response.send_message(f"✅ تم تسجيلك في البطولة! (المشاركين: {count}/{tour[1]})", ephemeral=True)

@bot.tree.command(name="tournament-create", description="إنشاء بطولة جديدة")
async def tournament_create(i: discord.Interaction, name: str, max_players: app_commands.Range[int, 2, 32]):
    if not await gate(i, "tournament-create", editor=True): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("INSERT INTO tournaments(guild_id, name, max_players, status) VALUES(?,?,?,?)", (i.guild.id, name, max_players, "open"))
        tour_id = cur.lastrowid
        await db.commit()
    embed = discord.Embed(title=f"🏆 بطولة: {name}", description=f"المطلوب: **{max_players}** لاعب.\nاضغط على الزر عشان تسجل!", color=discord.Color.gold())
    await i.channel.send(embed=embed, view=TournamentJoinView(tour_id))
    await i.response.send_message("✅ تم إنشاء البطولة!", ephemeral=True)

@bot.tree.command(name="tournament-start", description="بدء البطولة وتوزيع المباريات")
async def tournament_start(i: discord.Interaction, tournament_id: int):
    if not await gate(i, "tournament-start", editor=True): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT status FROM tournaments WHERE id=? AND guild_id=?", (tournament_id, i.guild.id))
        tour = await cur.fetchone()
        if not tour or tour[0] != "open": return await i.response.send_message("❌ البطولة مش موجودة أو بدأت خلاص.", ephemeral=True)
        cur = await db.execute("SELECT user_id FROM tournament_players WHERE tournament_id=?", (tournament_id,))
        players = [r[0] for r in await cur.fetchall()]
        if len(players) < 2: return await i.response.send_message("❌ محتاج على الأقل لاعبين!", ephemeral=True)
        await db.execute("UPDATE tournaments SET status='ongoing' WHERE id=?", (tournament_id,))
        random.shuffle(players)
        matches = []
        for j in range(0, len(players) - 1, 2):
            p1, p2 = players[j], players[j+1]
            await db.execute("INSERT INTO tournament_matches(tournament_id, round, p1_id, p2_id) VALUES(?,?,?,?)", (tournament_id, 1, p1, p2))
            matches.append((p1, p2))
        await db.commit()
    matches_text = "\n".join([f"⚔️ <@{p1}> ضد <@{p2}>" for p1, p2 in matches])
    await i.channel.send(embed=discord.Embed(title=f"🏆 بدء بطولة #{tournament_id}", description=f"**مباريات الدور الأول:**\n\n{matches_text}", color=discord.Color.red()))
    await i.response.send_message("✅ تم بدء البطولة!", ephemeral=True)

@bot.tree.command(name="tournament-set-winner", description="تحديد الفايز في مباراة")
async def tournament_set_winner(i: discord.Interaction, tournament_id: int, winner: discord.Member, loser: discord.Member):
    if not await gate(i, "tournament-set-winner", editor=True): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("UPDATE tournament_matches SET winner_id=? WHERE tournament_id=? AND ((p1_id=? AND p2_id=?) OR (p1_id=? AND p2_id=?)) AND winner_id IS NULL", (winner.id, tournament_id, winner.id, loser.id, loser.id, winner.id))
        if cur.rowcount == 0: return await i.response.send_message("❌ المباراة دي مش موجودة أو اتحدد الفايز فيها.", ephemeral=True)
        await db.commit()
    await i.response.send_message(f"✅ تم تسجيل فوز {winner.mention} على {loser.mention}!", ephemeral=True)
# ================================================================ الأوامر العامة (Points, Profile, Leaderboard)
@bot.tree.command(name="points", description="Check your points / عرض رصيدك")
@app_commands.checks.cooldown(1, 5.0)
async def points_cmd(i: discord.Interaction, member: Optional[discord.Member] = None):
    if not await gate(i, "points"): return
    target = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, target.id))
        row = await cur.fetchone()
    pts = row[0] if row else 0
    ladder = await get_ladder(i.guild.id)
    bar = progress_bar(pts, ladder)
    embed = discord.Embed(title=f"💰 {target.display_name}'s Balance", description=f"**Points:** {pts}\n**Progress:** {bar}", color=discord.Color.gold())
    await i.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="View the top members / قائمة المتصدرين")
@app_commands.checks.cooldown(1, 10.0)
@app_commands.choices(period=PERIOD_CHOICES)
async def leaderboard(i: discord.Interaction, period: app_commands.Choice[str] = None):
    if not await gate(i, "leaderboard"): return
    await i.response.defer()
    p = period.value if period else "all"
    season = await get_current_season(i.guild.id)
    
    async with aiosqlite.connect(DB) as db:
        if p == "week":
            since = (now_utc() - timedelta(days=7)).isoformat()
            cur = await db.execute("SELECT user_id, SUM(points) FROM point_events WHERE guild_id=? AND created_at>=? GROUP BY user_id ORDER BY SUM(points) DESC LIMIT 10", (i.guild.id, since))
        elif p == "season":
            cur = await db.execute("SELECT user_id, SUM(points) FROM point_events WHERE guild_id=? AND season_id=? GROUP BY user_id ORDER BY SUM(points) DESC LIMIT 10", (i.guild.id, season))
        else:
            cur = await db.execute("SELECT user_id, points FROM members WHERE guild_id=? ORDER BY points DESC LIMIT 10", (i.guild.id,))
        rows = await cur.fetchall()
        
    if not rows: return await i.followup.send("ℹ️ No data yet.")
    title = tr("lb_week") if p == "week" else tr("lb_season") if p == "season" else tr("lb_all")
    text = "\n".join(f"**{n}.** <@{u}> — {pts} pts" for n, (u, pts) in enumerate(rows, 1))
    await i.followup.send(embed=discord.Embed(title=title, description=text, color=discord.Color.gold()))

@bot.tree.command(name="profile", description="View member profile / عرض الملف الشخصي")
@app_commands.checks.cooldown(1, 5.0)
async def profile(i: discord.Interaction, member: Optional[discord.Member] = None):
    if not await gate(i, "profile"): return
    target = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, target.id))
        pts_row = await cur.fetchone()
        cur = await db.execute("SELECT roblox_name FROM links WHERE guild_id=? AND user_id=?", (i.guild.id, target.id))
        rob_row = await cur.fetchone()
        cur = await db.execute("SELECT bio FROM bios WHERE guild_id=? AND user_id=?", (i.guild.id, target.id))
        bio_row = await cur.fetchone()
        
    pts = pts_row[0] if pts_row else 0
    roblox = rob_row[0] if rob_row else "Not linked"
    bio = bio_row[0] if bio_row else "No bio set."
    
    embed = discord.Embed(title=f"👤 {target.display_name}'s Profile", color=target.color)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Points", value=f"💰 {pts}", inline=True)
    embed.add_field(name="Roblox", value=f"🎮 {roblox}", inline=True)
    embed.add_field(name="Bio", value=bio, inline=False)
    await i.response.send_message(embed=embed)

@bot.tree.command(name="rates", description="View points and ranks config / الأسعار والرتب")
@app_commands.checks.cooldown(1, 5.0)
async def rates_cmd(i: discord.Interaction):
    if not await gate(i, "rates"): return
    ladder = await get_ladder(i.guild.id)
    pts_text = "\n".join(f"• `{k}`: {await points_for_key(i.guild.id, k)} pts" for k in POINT_LABELS)
    rank_text = "\n".join(f"• **{n}**: {p} pts" for n, p in ladder)
    embed = discord.Embed(title="📊 Rates & Ranks / الأسعار والرتب", color=discord.Color.blue())
    embed.add_field(name="Points / النقاط", value=pts_text[:1024], inline=False)
    embed.add_field(name="Ranks / الرتب", value=rank_text[:1024], inline=False)
    await i.response.send_message(embed=embed)

@bot.tree.command(name="link", description="Link your Roblox username / ربط حساب روبلوكس")
@app_commands.checks.cooldown(1, 10.0)
async def link_cmd(i: discord.Interaction, roblox_name: str):
    if not await gate(i, "link"): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO links(guild_id, user_id, roblox_name) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET roblox_name=excluded.roblox_name", (i.guild.id, i.user.id, roblox_name))
        await db.commit()
    await i.response.send_message(f"✅ Linked your Roblox username to **{roblox_name}**.", ephemeral=True)

@bot.tree.command(name="set-bio", description="Set your profile bio / كتابة سيرتك الذاتية")
@app_commands.checks.cooldown(1, 10.0)
async def set_bio(i: discord.Interaction, text: str):
    if not await gate(i, "set-bio"): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO bios(guild_id, user_id, bio) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET bio=excluded.bio", (i.guild.id, i.user.id, text[:200]))
        await db.commit()
    await i.response.send_message("✅ Bio updated successfully.", ephemeral=True)

@bot.tree.command(name="stats", description="War stats / إحصائيات الحروب")
@app_commands.checks.cooldown(1, 5.0)
async def stats_cmd(i: discord.Interaction):
    if not await gate(i, "stats"): return
    await i.response.defer()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT data, result FROM logs WHERE guild_id=? AND type='war' AND status='accepted'", (i.guild.id,))
        rows = await cur.fetchall()
    wins, losses = {}, {}
    for data_str, result in rows:
        try:
            crew = json.loads(data_str or "{}").get("crew", "Unknown")
            if result == "win": wins[crew] = wins.get(crew, 0) + 1
            elif result == "loss": losses[crew] = losses.get(crew, 0) + 1
        except: pass
    if not wins and not losses: return await i.followup.send("ℹ️ No war data available.")
    embed = discord.Embed(title="⚔️ War Stats by Crew", color=discord.Color.red())
    all_crews = set(wins.keys()).union(set(losses.keys()))
    for c in all_crews:
        embed.add_field(name=c, value=f"✅ Wins: {wins.get(c, 0)}\n❌ Losses: {losses.get(c, 0)}", inline=True)
    await i.followup.send(embed=embed)

@bot.tree.command(name="my-logs", description="View your recent logs / عرض لوجاتك")
@app_commands.checks.cooldown(1, 5.0)
async def my_logs(i: discord.Interaction):
    if not await gate(i, "my-logs"): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id, type, status, created_at FROM logs WHERE guild_id=? AND author_id=? ORDER BY id DESC LIMIT 10", (i.guild.id, i.user.id))
        rows = await cur.fetchall()
    if not rows: return await i.response.send_message(tr("no_my_logs"), ephemeral=True)
    lines = [f"**#{lid}** — {LOG_TYPES.get(t, {}).get('label', t)} — {STATUS_LABEL.get(s, s)}" for lid, t, s, _ in rows]
    await i.response.send_message("📜 **Your Recent Logs:**\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="log-info", description="View details of a log / تفاصيل لوج")
@app_commands.checks.cooldown(1, 5.0)
async def log_info(i: discord.Interaction, log_id: int):
    if not await gate(i, "log-info"): return
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id: return await i.response.send_message(tr("log_not_found"), ephemeral=True)
    data = json.loads(row["data"] or "{}")
    embed = discord.Embed(title=f"Log #{log_id} — {LOG_TYPES.get(row['type'], {}).get('label', row['type'])}", color=discord.Color.blue())
    embed.add_field(name="Author", value=f"<@{row['author_id']}>", inline=True)
    embed.add_field(name="Status", value=STATUS_LABEL.get(row["status"], row["status"]), inline=True)
    if row["result"]: embed.add_field(name="Result", value=row["result"].capitalize(), inline=True)
    if row["points"]: embed.add_field(name="Points", value=str(row["points"]), inline=True)
    if data.get("score"): embed.add_field(name="Score", value=data["score"], inline=True)
    await i.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="reviewer-stats", description="Reviewer stats / إحصائيات المراجعين")
@app_commands.checks.cooldown(1, 10.0)
async def reviewer_stats(i: discord.Interaction):
    if not await gate(i, "reviewer-stats", editor=True): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT reviewer_id, COUNT(*) FROM logs WHERE guild_id=? AND reviewer_id IS NOT NULL AND status IN ('accepted', 'rejected') GROUP BY reviewer_id ORDER BY COUNT(*) DESC", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows: return await i.response.send_message("ℹ️ No reviews recorded yet.", ephemeral=True)
    text = "\n".join(f"<@{r}> — {c} reviews" for r, c in rows[:15])
    await i.response.send_message(embed=discord.Embed(title="📊 Reviewer Stats", description=text, color=discord.Color.blue()))

@bot.tree.command(name="inactive", description="List inactive members / الأعضاء غير النشطين")
@app_commands.checks.cooldown(1, 10.0)
async def inactive_cmd(i: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30):
    if not await gate(i, "inactive", editor=True): return
    await i.response.defer()
    since = (now_utc() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id FROM members WHERE guild_id=? AND user_id NOT IN (SELECT author_id FROM logs WHERE guild_id=? AND created_at>=?)", (i.guild.id, i.guild.id, since))
        rows = await cur.fetchall()
    if not rows: return await i.followup.send(f"✅ No inactive members in the last {days} days.")
    text = "\n".join(f"<@{r[0]}>" for r in rows[:30])
    if len(rows) > 30: text += f"\n...and {len(rows)-30} more."
    await i.followup.send(f"💤 **Inactive members (>{days} days):**\n{text}", allowed_mentions=discord.AllowedMentions.none())

# ================================================================ أوامر الطواقم (Crews)
@bot.tree.command(name="crews-list", description="View all crews / عرض الطواقم")
@app_commands.checks.cooldown(1, 5.0)
async def crews_list(i: discord.Interaction):
    if not await gate(i, "crews-list"): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT crew_name, owner_id FROM crews WHERE guild_id=?", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows: return await i.response.send_message(tr("no_crews"), ephemeral=True)
    text = "\n".join(f"🛡️ **{name}** — Owner: <@{owner}>" for name, owner in rows)
    await i.response.send_message(embed=discord.Embed(title="🎖️ Registered Crews", description=text[:2000], color=discord.Color.blue()))

@bot.tree.command(name="create-new-crew", description="Create a new crew / إنشاء طاقم جديد")
@app_commands.checks.cooldown(1, 10.0)
async def create_new_crew(i: discord.Interaction, name: str, owner: discord.Member, captain1: Optional[discord.Member] = None, captain2: Optional[discord.Member] = None):
    if not await gate(i, "create-new-crew", editor=True): return
    name = name.strip()[:40]
    if len(name) < 2: return await i.response.send_message(tr("crew_bad_name"), ephemeral=True)
    if owner.bot: return await i.response.send_message(tr("crew_bad_owner"), ephemeral=True)
    captains = [c.id for c in (captain1, captain2) if c and not c.bot]
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM crews WHERE guild_id=? AND LOWER(crew_name)=LOWER(?)", (i.guild.id, name))
        if await cur.fetchone(): return await i.response.send_message(tr("crew_exists"), ephemeral=True)
        await db.execute("INSERT INTO crews(guild_id, crew_name, owner_id, captains_data, created_at) VALUES(?,?,?,?,?)", (i.guild.id, name, owner.id, json.dumps(captains), now_utc().isoformat()))
        await db.commit()
    await i.response.send_message(tr("crew_created", name=name), ephemeral=True)

@bot.tree.command(name="crews-delete-all", description="Delete all crews / حذف كل الطواقم")
@app_commands.checks.cooldown(1, 10.0)
async def crews_delete_all(i: discord.Interaction, confirm: str):
    if not await gate(i, "crews-delete-all", editor=True): return
    if confirm != "DELETE": return await i.response.send_message("❌ Type `DELETE` to confirm.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM crews WHERE guild_id=?", (i.guild.id,))
        await db.commit()
    await i.response.send_message("🗑️ All crews deleted.", ephemeral=True)

# ================================================================ المساعدة (Help)
HELP_DATA = [
    ("📜 Part 1 — Logging Activity", discord.Color.gold(), [("/log-war", "Log a Crew War"), ("/log-1v1", "Log a 1v1 fight"), ("/log-2v2", "Log a 2v2 fight"), ("/log-training", "Log training"), ("/log-tryout", "Log a tryout"), ("/edit-log", "Edit a pending log"), ("/resubmit", "Fix and resend a rejected log")]),
    ("🛡️ Part 2 — Review & Moderation", discord.Color.blue(), [("/pending", "List pending logs"), ("/revoke-log", "Cancel an accepted log"), ("/reviewer-stats", "Reviewer stats"), ("/inactive", "Inactive members"), ("/season-reset", "Start new season"), ("/verify-points", "Check balances")]),
    ("⚙️ Part 3 — Configuration", discord.Color.green(), [("/setchannel", "Set channels"), ("/setping", "Set ping roles"), ("/setroles", "Set role permissions"), ("/setrule", "Configure server rules"), ("/set-log-points", "Set log points"), ("/set-rank-points", "Set rank points"), ("/sync-ranks", "Re-check ranks"), ("/set-cmd-perms", "Lock commands"), ("/set-log-emojis", "Set custom emojis")]),
    ("🏆 Part 4 — Points & Public", discord.Color.purple(), [("/points", "Check your points"), ("/leaderboard", "Top members"), ("/profile", "View profile"), ("/rates", "View rates"), ("/link", "Link Roblox"), ("/stats", "War stats"), ("/help", "Show this manual")]),
    ("🎮 Part 5 — Systems & Games", discord.Color.teal(), [("/streak", "Check streak"), ("/shop", "Point shop"), ("/buy", "Buy item"), ("/achievements", "View achievements"), ("/quests", "Daily quests"), ("/event", "Create event"), ("/h2h", "Head-to-head stats"), ("/lfg", "Looking for group"), ("/daily", "Daily bonus"), ("/setup-tickets", "Setup tickets"), ("/create-temp-voice", "Temp voice channel"), ("/ai", "Chat with AI")])
]

def get_help_embeds():
    embeds = []
    for title, color, items in HELP_DATA:
        e = discord.Embed(title=f"{title} ({BOT_VERSION})", color=color)
        for cmd, desc in items: e.add_field(name=f"`{cmd}`", value=desc, inline=False)
        e.set_footer(text="DragonSlayerBot • Ultimate Edition")
        embeds.append(e)
    return embeds

@bot.tree.command(name="help", description="Show the bot manual / عرض دليل الأوامر")
@app_commands.checks.cooldown(1, 5.0)
async def help_cmd(i: discord.Interaction):
    if not await gate(i, "help"): return
    await i.response.defer(ephemeral=True)
    for e in get_help_embeds()[:5]: await i.followup.send(embed=e, ephemeral=True)

@bot.tree.command(name="send-help-panel", description="Post the manual in a channel / إرسال الشرح")
@app_commands.checks.cooldown(1, 10.0)
async def send_help_panel(i: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not await gate(i, "send-help-panel", editor=True): return
    target = channel or i.channel
    try:
        for e in get_help_embeds(): await target.send(embed=e)
        await i.response.send_message(f"✅ Posted the manual to {target.mention}.", ephemeral=True)
    except discord.HTTPException as e:
        await i.response.send_message(f"❌ Failed: {e}", ephemeral=True)

# ================================================================ الفعاليات والتصويت (Events & Vote)
class EventView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="✅", style=discord.ButtonStyle.success, custom_id="event_join")
    async def join(self, i: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT attendees FROM events WHERE message_id=?", (i.message.id,))
            row = await cur.fetchone()
            if row:
                attendees = json.loads(row[0] or "[]")
                if i.user.id not in attendees:
                    attendees.append(i.user.id)
                    await db.execute("UPDATE events SET attendees=? WHERE message_id=?", (json.dumps(attendees), i.message.id))
                    await db.commit()
        await i.response.send_message(tr("event_joined"), ephemeral=True)
    @discord.ui.button(label="❌", style=discord.ButtonStyle.danger, custom_id="event_leave")
    async def leave(self, i: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT attendees FROM events WHERE message_id=?", (i.message.id,))
            row = await cur.fetchone()
            if row:
                attendees = json.loads(row[0] or "[]")
                if i.user.id in attendees:
                    attendees.remove(i.user.id)
                    await db.execute("UPDATE events SET attendees=? WHERE message_id=?", (json.dumps(attendees), i.message.id))
                    await db.commit()
        await i.response.send_message(tr("event_left"), ephemeral=True)

@bot.tree.command(name="event", description="Create an event / إنشاء فعالية")
@app_commands.checks.cooldown(1, 30.0)
async def event_cmd(i: discord.Interaction, title: str, time_str: str):
    if not await gate(i, "event", editor=True): return
    ch_id = await cfg_get(i.guild.id, "all", "log_channel")
    ch = i.guild.get_channel(ch_id) if ch_id else i.channel
    embed = await get_custom_msg(i.guild.id, "event_msg", tr("event_title", title=title), tr("event_desc", time=time_str), discord.Color.gold())
    msg = await ch.send(embed=embed, view=EventView())
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO events(guild_id, channel_id, message_id, title, time, attendees) VALUES(?,?,?,?,?,?)", (i.guild.id, ch.id, msg.id, title, time_str, "[]"))
        await db.commit()
    await i.response.send_message(f"✅ Event created in {ch.mention}.", ephemeral=True)

class VoteView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="صوت / Vote", style=discord.ButtonStyle.primary, custom_id="vote_btn")
    async def vote(self, i: discord.Interaction, button: discord.ui.Button):
        await i.response.send_modal(VoteModal(i.message.id))

class VoteModal(discord.ui.Modal, title="التصويت / Voting"):
    player = discord.ui.TextInput(label="اسم اللاعب أو منشنه / Name or mention", required=True)
    def __init__(self, message_id):
        super().__init__()
        self.message_id = message_id
    async def on_submit(self, i: discord.Interaction):
        target_id = None
        if self.player.value.isdigit(): target_id = int(self.player.value)
        else:
            match = re.search(r"<@!?(\d+)>", self.player.value)
            if match: target_id = int(match.group(1))
            else:
                member = discord.utils.find(lambda m: m.name.lower() == self.player.value.lower() or m.display_name.lower() == self.player.value.lower(), i.guild.members)
                if member: target_id = member.id
        if not target_id: return await i.response.send_message("❌ Member not found.", ephemeral=True)
        async with aiosqlite.connect(DB) as db:
            await db.execute("INSERT OR REPLACE INTO votes(guild_id, message_id, voter_id, target_id) VALUES(?,?,?,?)", (i.guild.id, self.message_id, i.user.id, target_id))
            await db.commit()
        await i.response.send_message(f"✅ Vote registered for <@{target_id}>.", ephemeral=True)

@bot.tree.command(name="start-vote", description="Start MVP vote / بدء تصويت")
@app_commands.checks.cooldown(1, 60.0)
async def start_vote(i: discord.Interaction, reason: str = "أفضل لاعب هذا الأسبوع"):
    if not await gate(i, "start-vote", editor=True): return
    embed = discord.Embed(title="🏆 Voting / تصويت", description=f"**{reason}**\n\nWho is the best player?", color=discord.Color.blue())
    await i.channel.send(embed=embed, view=VoteView())
    await i.response.send_message("✅ Voting started.", ephemeral=True)

# ================================================================ التذاكر (Tickets)
class TicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Open Ticket / فتح تذكرة", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="ticket_open")
    async def open_ticket(self, i: discord.Interaction, button: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        guild = i.guild
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'", (guild.id, i.user.id))
            if await cur.fetchone(): return await i.followup.send("❌ You already have an open ticket.", ephemeral=True)
        overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False), i.user: discord.PermissionOverwrite(read_messages=True, send_messages=True), guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
        for role_name in EDITOR_ROLES:
            role = discord.utils.get(guild.roles, name=role_name)
            if role: overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        try:
            channel = await guild.create_text_channel(name=f"ticket-{i.user.name}", overwrites=overwrites, category=None)
            async with aiosqlite.connect(DB) as db:
                await db.execute("INSERT INTO tickets(guild_id, user_id, channel_id, status) VALUES(?,?,?,?)", (guild.id, i.user.id, channel.id, "open"))
                await db.commit()
            await channel.send(f"Welcome {i.user.mention}! A staff member will be with you shortly.", view=CloseTicketView())
            await i.followup.send(f"✅ Ticket created: {channel.mention}", ephemeral=True)
        except discord.Forbidden: await i.followup.send("❌ I don't have permission to create channels.", ephemeral=True)

class CloseTicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Close Ticket / إغلاق", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket_close")
    async def close_ticket(self, i: discord.Interaction, button: discord.ui.Button):
        await i.response.send_message("🔒 Closing ticket...", ephemeral=True)
        log_ch_id = await cfg_get(i.guild.id, "all", "audit_channel")
        target_ch = i.guild.get_channel(log_ch_id) if log_ch_id else None
        messages = []
        async for msg in i.channel.history(limit=500): messages.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M')}] {msg.author.name}: {msg.content}")
        transcript = "\n".join(reversed(messages)).encode("utf-8")
        if target_ch:
            try: await target_ch.send(f"🎫 Transcript for {i.channel.name}:", file=discord.File(io.BytesIO(transcript), filename=f"transcript-{i.channel.name}.txt"))
            except Exception: pass
        async with aiosqlite.connect(DB) as db:
            await db.execute("UPDATE tickets SET status='closed' WHERE channel_id=?", (i.channel.id,))
            await db.commit()
        await i.channel.delete()

@bot.tree.command(name="setup-tickets", description="Setup support tickets / إنشاء لوحة التذاكر")
@app_commands.checks.cooldown(1, 10.0)
async def setup_tickets(i: discord.Interaction):
    if not await gate(i, "setup-tickets", editor=True): return
    embed = discord.Embed(title="🎫 Support Tickets", description="Click to open a ticket / اضغط لفتح تذكرة", color=discord.Color.blue())
    await i.channel.send(embed=embed, view=TicketView())
    await i.response.send_message("✅ Setup complete.", ephemeral=True)

# ================================================================ الغرف الصوتية (Temp Voice)
@bot.tree.command(name="create-temp-voice", description="Create temp voice / غرفة صوتية مؤقتة")
@app_commands.checks.cooldown(1, 60.0)
async def create_temp_voice(i: discord.Interaction, name: Optional[str] = None):
    if not await gate(i, "create-temp-voice"): return
    await i.response.defer(ephemeral=True)
    guild = i.guild
    channel_name = name or f"🔊 Room: {i.user.display_name}"
    overwrites = {guild.default_role: discord.PermissionOverwrite(connect=True), i.user: discord.PermissionOverwrite(manage_channels=True, connect=True)}
    try:
        channel = await guild.create_voice_channel(name=channel_name[:100], overwrites=overwrites)
        await i.followup.send(f"✅ Temp voice created: {channel.mention}", ephemeral=True)
    except discord.Forbidden: await i.followup.send("❌ No permission to create voice channels.", ephemeral=True)

@bot.tree.command(name="set-temp-voice", description="Set temp voice trigger / قناة الغرف المؤقتة")
@app_commands.checks.cooldown(1, 5.0)
async def set_temp_voice(i: discord.Interaction, channel: discord.VoiceChannel):
    if not await gate(i, "set-temp-voice", editor=True): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO custom_settings(guild_id, key, value) VALUES(?,?,?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (i.guild.id, "temp_voice_trigger", str(channel.id)))
        await db.commit()
    await i.response.send_message(f"✅ Set {channel.mention} as trigger.", ephemeral=True)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    guild = member.guild
    if after.channel:
        trigger_id = await get_custom_val(guild.id, "temp_voice_trigger", "0")
        if str(after.channel.id) == trigger_id:
            try:
                new_ch = await guild.create_voice_channel(name=f"🔊 Room: {member.display_name}", category=after.channel.category)
                async with aiosqlite.connect(DB) as db:
                    await db.execute("INSERT INTO temp_voice_rooms(guild_id,channel_id,owner_id,created_at) VALUES(?,?,?,?)", (guild.id, new_ch.id, member.id, now_utc().isoformat()))
                    await db.commit()
                await new_ch.set_permissions(member, connect=True, manage_channels=True)
                await member.move_to(new_ch)
            except discord.HTTPException: pass
    if before.channel and before.channel != after.channel:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT channel_id FROM temp_voice_rooms WHERE guild_id=? AND channel_id=?", (guild.id, before.channel.id))
            managed = await cur.fetchone()
        if managed and len(before.channel.members) == 0:
            try: await before.channel.delete(reason="Temp voice empty")
            except discord.HTTPException: return
            async with aiosqlite.connect(DB) as db:
                await db.execute("DELETE FROM temp_voice_rooms WHERE guild_id=? AND channel_id=?", (guild.id, before.channel.id))
                await db.commit()

# ================================================================ أدوات إضافية (Purge, Embed, Custom Roles)
@bot.tree.command(name="purge", description="Purge messages / مسح رسائل")
@app_commands.checks.cooldown(1, 10.0)
async def purge_cmd(i: discord.Interaction, amount: app_commands.Range[int, 1, 100], member: Optional[discord.Member] = None):
    if not await gate(i, "purge", editor=True): return
    protected = [await cfg_get(i.guild.id, "all", "review_channel"), await cfg_get(i.guild.id, "all", "log_channel")]
    if i.channel.id in protected: return await i.response.send_message("❌ Cannot purge here.", ephemeral=True)
    if not i.channel.permissions_for(i.guild.me).manage_messages: return await i.response.send_message("❌ Missing Manage Messages permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    def check(m): return m.author.id == member.id if member else True
    try:
        deleted = await i.channel.purge(limit=amount if not member else 100, check=check, bulk=True)
        await i.followup.send(f"✅ Deleted {len(deleted)} messages.", ephemeral=True)
    except discord.HTTPException as e: await i.followup.send(f"❌ Failed: {e}", ephemeral=True)

@bot.tree.command(name="embed", description="Embed builder / صانع الرسائل")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(color=COLOR_CHOICES)
async def embed_builder(i: discord.Interaction, title: str, description: str, color: Optional[app_commands.Choice[str]] = None):
    if not await gate(i, "embed", editor=True): return
    ec = discord.Color.blue()
    if color:
        try: ec = int(color.value, 16)
        except ValueError: pass
    embed = discord.Embed(title=title[:256], description=description[:4000], color=ec)
    await i.channel.send(embed=embed)
    await i.response.send_message("✅ Sent.", ephemeral=True)

@bot.tree.command(name="create-custom-role", description="Create custom role / رتبة مخصصة")
@app_commands.checks.cooldown(1, 60.0)
@app_commands.choices(color=COLOR_CHOICES)
async def create_custom_role(i: discord.Interaction, name: app_commands.Range[str, 2, 32], color: app_commands.Choice[str]):
    if not await gate(i, "create-custom-role"): return
    clean = name.strip()
    forbidden = ["admin", "mod", "commander", "admiral", "owner", "lvl", "staff", "empyrean", "apotheosis"]
    if not clean or any(w in clean.casefold() for w in forbidden): return await i.response.send_message("❌ Invalid name.", ephemeral=True)
    is_free = is_admin(i.user) or await has_temp_admin(i.user)
    price = 0 if is_free else int(await get_custom_val(i.guild.id, "custom_role_price", 500))
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT COUNT(*) FROM custom_roles WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
        if (await cur.fetchone())[0] >= 3: return await i.response.send_message("❌ Max 3 custom roles.", ephemeral=True)
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
        points = (await cur.fetchone() or [0])[0]
    if not is_free and points < price: return await i.response.send_message(tr("shop_no_points", price=price, points=points), ephemeral=True)
    try: role = await i.guild.create_role(name=clean, color=discord.Color(int(color.value, 16)), reason="Custom Role")
    except Exception: return await i.response.send_message("❌ Could not create role.", ephemeral=True)
    try:
        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            if not is_free:
                cur = await db.execute("UPDATE members SET points=points-? WHERE guild_id=? AND user_id=? AND points>=?", (price, i.guild.id, i.user.id, price))
                if cur.rowcount != 1:
                    await db.rollback()
                    await role.delete()
                    return await i.response.send_message("❌ Insufficient balance.", ephemeral=True)
                await db.execute("INSERT INTO point_events(guild_id,user_id,points,created_at,type,season_id) VALUES(?,?,?,?,?,?)", (i.guild.id, i.user.id, -price, now_utc().isoformat(), "shop", await get_current_season(i.guild.id)))
            await db.execute("INSERT INTO custom_roles(guild_id,user_id,role_id,created_at) VALUES(?,?,?,?)", (i.guild.id, i.user.id, role.id, now_utc().isoformat()))
            await db.commit()
    except Exception:
        try: await role.delete()
        except Exception: pass
        return await i.response.send_message("❌ Purchase failed.", ephemeral=True)
    try: await i.user.add_roles(role, reason="Custom Role")
    except (discord.Forbidden, discord.HTTPException):
        async with aiosqlite.connect(DB) as db:
            await db.execute("DELETE FROM custom_roles WHERE role_id=? AND guild_id=?", (role.id, i.guild.id))
            if not is_free:
                await db.execute("UPDATE members SET points=points+? WHERE guild_id=? AND user_id=?", (price, i.guild.id, i.user.id))
                await db.execute("INSERT INTO point_events(guild_id,user_id,points,created_at,type,season_id) VALUES(?,?,?,?,?,?)", (i.guild.id, i.user.id, price, now_utc().isoformat(), "shop_refund", await get_current_season(i.guild.id)))
            await db.commit()
        try: await role.delete()
        except Exception: pass
        return await i.response.send_message("❌ Role assignment failed; refunded.", ephemeral=True)
    await i.response.send_message(tr("role_created_free" if is_free else "role_created", role=role.mention, member=i.user.mention), ephemeral=True)

# ================================================================ المهام التلقائية (Background Tasks)
async def fix_db_columns():
    async with aiosqlite.connect(DB) as db:
        try:
            cur = await db.execute("PRAGMA table_info(quests)")
            cols = [r[1] for r in await cur.fetchall()]
            if "is_auto" not in cols:
                await db.execute("ALTER TABLE quests ADD COLUMN is_auto INTEGER DEFAULT 0")
            await db.commit()
        except Exception as e: print(f"⚠️ DB Fix error: {e}")

async def run_reminders():
    now = now_utc()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, type, guild_id, created_at, review_channel, review_msg FROM logs WHERE status='pending' AND COALESCE(reminded,0)=0 AND guild_id!=0")
        rows = await cur.fetchall()
    for l in rows:
        try:
            guild = bot.get_guild(l["guild_id"])
            if not guild: continue
            hours = await rule(guild.id, "reminder_hours")
            if hours <= 0: continue
            created = dt.datetime.fromisoformat(l["created_at"])
            if (now - created).total_seconds() < hours * 3600: continue
            ch_id = l["review_channel"] or await cfg_get(guild.id, l["type"], "review_channel")
            ch = guild.get_channel(ch_id) if ch_id else None
            if ch:
                ping_id = await cfg_get(guild.id, l["type"], "review_ping")
                text = f"{f'<@&{ping_id}> ' if ping_id else ''}⏰ Log `#{l['id']}` has been waiting for review for over {hours}h."
                try:
                    if l["review_msg"]:
                        msg = await ch.fetch_message(l["review_msg"])
                        await msg.reply(text, allowed_mentions=mentions(ping_id))
                    else: await ch.send(text, allowed_mentions=mentions(ping_id))
                except discord.HTTPException: pass
            await db_exec("UPDATE logs SET reminded=1 WHERE id=?", (l["id"],))
        except Exception as e: print(f"⚠️ Reminder error for log {l['id']}: {e}")

async def run_daily_quests():
    now = now_utc()
    if now.hour != 0 or now.minute > 30: return
    today_str = now.date().isoformat()
    for guild in bot.guilds:
        try:
            last_gen = await raw_setting(guild.id, "state:last_quest_gen")
            if last_gen == today_str: continue
            async with aiosqlite.connect(DB) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await db.execute("DELETE FROM quests WHERE guild_id=? AND is_auto=1", (guild.id,))
                    pool = [("سجل فايت 1v1 النهاردة", 15), ("شارك في حرب كرو", 30), ("اعمل تدريب", 10), ("ادخل فعالية", 20), ("تفاعل في الشات", 5)]
                    random_quests = random.sample(pool, 3) if len(pool) >= 3 else pool
                    for desc, pts in random_quests:
                        await db.execute("INSERT INTO quests(guild_id, description, target_type, target_amount, reward_points, role_id, is_auto) VALUES(?,?,?,?,?,?,?)", (guild.id, desc, "log", 1, pts, None, 1))
                    await db.commit()
                except Exception as e:
                    await db.rollback()
                    continue
            await set_setting(guild.id, "state:last_quest_gen", today_str)
            await send_notification(guild, "quests", "🎯 مهام جديدة!", "تم توليد مهام يومية جديدة! استخدم `/quests` لمشاهدتها.", discord.Color.green())
        except Exception as e: print(f"⚠️ Auto quest generation failed for guild {guild.id}: {e}")

async def run_backup():
    if not owner_id(): return
    now = now_utc()
    due = []
    for guild in bot.guilds:
        try:
            hours = await rule(guild.id, "backup_hours")
            if hours <= 0: continue
            cycle = hours * 3600
            last = await raw_setting(guild.id, "state:last_backup")
            if last:
                try:
                    if (now - dt.datetime.fromisoformat(last)).total_seconds() < cycle: continue
                except ValueError: pass
            due.append(guild)
        except Exception as e: pass
    if not due: return
    for guild in due:
        try: await set_setting(guild.id, "state:last_backup", now.isoformat())
        except Exception: pass
    path = os.path.join(tempfile.gettempdir(), f"crewbot-auto-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.db")
    try:
        await asyncio.to_thread(_backup_file, path)
        payload = await asyncio.to_thread(_read_bytes, path)
        names = ", ".join(f"**{g.name}**" for g in due[:5])
        extra = f" (+{len(due) - 5} more)" if len(due) > 5 else ""
        await dm_owner(f"🗄️ **Scheduled backup** — due for: {names}{extra}", file=discord.File(io.BytesIO(payload), filename=os.path.basename(path)))
    finally:
        if os.path.exists(path):
            try: os.unlink(path)
            except OSError: pass

async def run_weekly():
    now = now_utc()
    if now.weekday() != 6: return
    week_key = now.strftime("%Y-W%U")
    for guild in bot.guilds:
        try:
            if await rule(guild.id, "weekly_post") != 1: continue
            if await raw_setting(guild.id, "state:last_weekly_post") == week_key: continue
            ch_id = await cfg_get(guild.id, "all", "log_channel")
            ch = guild.get_channel(ch_id) if ch_id else None
            if not ch: continue
            season = await get_current_season(guild.id)
            async with aiosqlite.connect(DB) as db:
                cur = await db.execute("SELECT user_id, SUM(points) AS total FROM point_events WHERE guild_id=? AND season_id=? AND created_at >= ? AND type NOT IN ('shop', 'revoke') GROUP BY user_id HAVING total>0 ORDER BY total DESC LIMIT 10", (guild.id, season, (now - timedelta(days=7)).isoformat()))
                rows = await cur.fetchall()
            if rows:
                text = f"🏆 **Weekly Top 10 ({week_key})**\n\n" + "\n".join(f"**{n}.** <@{u}> — {p} pts" for n, (u, p) in enumerate(rows, 1))
                await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
                for idx, (uid, _) in enumerate(rows[:3], 1):
                    reward_pts = int(await get_custom_val(guild.id, f"top{idx}_reward_points", 0))
                    reward_role_id = await get_custom_val(guild.id, f"top{idx}_reward_role", None)
                    if reward_pts > 0:
                        async with aiosqlite.connect(DB) as db:
                            await db.execute("BEGIN IMMEDIATE")
                            try:
                                await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (guild.id, uid, reward_pts, reward_pts))
                                await db.execute("INSERT INTO point_events(guild_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?)", (guild.id, uid, reward_pts, now.isoformat(), "weekly_top", season))
                                await db.commit()
                            except Exception:
                                await db.rollback()
                    if reward_role_id:
                        role = guild.get_role(int(reward_role_id))
                        member = guild.get_member(uid)
                        if role and member:
                            try: await member.add_roles(role, reason="Weekly Top Reward")
                            except discord.Forbidden: pass
            await set_setting(guild.id, "state:last_weekly_post", week_key)
        except Exception as e: print(f"⚠️ Weekly leaderboard error: {e}")

@tasks.loop(minutes=30)
async def maintenance():
    for job in (run_reminders, run_daily_quests, run_backup, run_weekly):
        try: await job()
        except Exception as e: print(f"⚠️ Maintenance job {job.__name__} failed: {e}")

@maintenance.before_loop
async def before_maintenance():
    await bot.wait_until_ready()

# ================================================================ التشغيل النهائي
if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("❌ DISCORD_TOKEN is missing in environment variables.")
    bot.run(token)
    