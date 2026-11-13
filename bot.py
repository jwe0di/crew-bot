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
import uuid
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

BOT_VERSION = "v8.0.1 (Security Hardened)"
DB = os.getenv("DB_PATH", "crewbot.db")
JSON_CONFIG_PATH = os.getenv("JSON_CONFIG_PATH") or os.path.join(os.path.dirname(DB) or ".", "servers_settings.json")
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

# [PATCH 2] حماية ملف الداتابيز
try:
    if os.path.exists(DB):
        os.chmod(DB, 0o600)
except (OSError, NotImplementedError):
    pass

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".mkv"}
MAX_FILE_SIZE = 25 * 1024 * 1024
# [PATCH 1] تم تقليل الحجم من 50MB لـ 30MB لمنع استهلاك الذاكرة
MAX_TOTAL_FILES_SIZE = 30 * 1024 * 1024
MAX_FILES_COUNT = 5
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}

def run_dummy_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"DragonSlayerBot {BOT_VERSION} is online!".encode())
        def log_message(self, format, *args): pass
    port = int(os.environ.get("PORT", 8080))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

# ================================================================ الإعدادات الأساسية
REQUIRED_TYPES = ("war", "1v1", "2v2", "training", "tryout")
_DEFAULT_LOG_TYPES = {
    "war": {"label": "Crew War Log", "fields": [("my_team", "Who fought for Dragon Slayers", True, True), ("crew", "Enemy crew name", False, True), ("their_team", "Who fought for the enemy crew", True, True), ("score", "Score (e.g. 5-3)", False, True), ("notes", "Notes (optional)", True, False)]},
    "1v1": {"label": "1v1 Fight Log", "fields": [("my_fighter", "Who fought for us", False, True), ("opponent", "Who we fought against", False, True), ("score", "Score (e.g. 5-3)", False, True), ("notes", "Notes (optional)", True, False)]},
    "2v2": {"label": "2v2 Fight Log", "fields": [("my_team", "Our team", False, True), ("their_team", "Enemy team", False, True), ("score", "Score (e.g. 5-3)", False, True), ("notes", "Notes (optional)", True, False)]},
    "training": {"label": "Training Log", "fields": [("participants", "Who trained", True, True), ("score", "Result / summary (optional)", False, False), ("notes", "Notes (optional)", True, False)]},
    "tryout": {"label": "Tryout Log", "fields": [("score", "Tryout score / rating", False, True), ("roblox", "Candidate Roblox username (optional)", False, False), ("notes", "Notes (optional)", True, False)]},
}

def _load_log_types():
    cfg = getattr(config, "LOG_TYPES", None)
    if isinstance(cfg, dict) and all(t in cfg and isinstance(cfg[t].get("fields"), list) and all(len(f) == 4 for f in cfg[t]["fields"]) for t in REQUIRED_TYPES):
        return {t: cfg[t] for t in REQUIRED_TYPES}
    return _DEFAULT_LOG_TYPES

LOG_TYPES = _load_log_types()
_cfg_points = getattr(config, "DEFAULT_POINTS", {}) or {}
DEFAULT_POINTS = {k: int(_cfg_points.get(k, v)) for k, v in {"war_win": 10, "war_loss": 5, "1v1_win": 8, "1v1_loss": 4, "2v2_win": 6, "2v2_loss": 3, "training": 3, "tryout": 5}.items()}
_cfg_labels = getattr(config, "POINT_LABELS", {}) or {}
POINT_LABELS = {k: str(_cfg_labels.get(k, v)) for k, v in {"war_win": "War - Win", "war_loss": "War - Loss", "1v1_win": "1v1 - Win", "1v1_loss": "1v1 - Loss", "2v2_win": "2v2 - Win", "2v2_loss": "2v2 - Loss", "training": "Training", "tryout": "Tryout"}.items()}
LADDER = [(str(n), int(p)) for n, p in (getattr(config, "LADDER", None) or [("Lvl. 20", 20), ("Lvl. 40", 60), ("Lvl. 60", 130), ("Lvl. 80", 220), ("Lvl. 100", 350)])]
EDITOR_ROLES = list(getattr(config, "EDITOR_ROLES", ["The Admiral", "Empyrean Monarch", "Commander"]))
REVIEWER_ROLES = list(getattr(config, "REVIEWER_ROLES", ["The Admiral", "Empyrean Monarch", "Commander", "Astral Apotheosis"]))
TRYOUTER_ROLES = list(getattr(config, "TRYOUTER_ROLES", ["Tryout Master"]))
CREW_ROLE_KEYWORD = getattr(config, "CREW_ROLE_KEYWORD", "Crew")

RULE_DEFAULTS = {"daily_limit": 5, "reminder_hours": 48, "backup_hours": 24, "weekly_post": 1, "war_participants": 1, "tryout_autorole": 1}
RULE_DEFAULTS.update(getattr(config, "RULE_DEFAULTS", {}) or {})
RULE_LABELS = {"daily_limit": "Max logs per member per 24h (0 = unlimited)", "reminder_hours": "Remind reviewers after N hours (0 = off)", "backup_hours": "Auto backup to owner every N hours (0 = off)", "weekly_post": "Weekly leaderboard post (1 = on, 0 = off)", "war_participants": "War points to confirmed teammates (1 = on, 0 = off)", "tryout_autorole": "Give Crew role on passed tryout (1 = on, 0 = off)"}

RESULT_LABEL = {"win": "✅ Win", "loss": "❌ Loss"}
TRYOUT_LABEL = {"win": "✅ Passed", "loss": "❌ Failed"}
STATUS_LABEL = {"pending": "⏳ Pending", "accepted": "✅ Accepted", "rejected": "❌ Rejected", "revoked": "🚫 Revoked"}
RESULT_CHOICES = [app_commands.Choice(name="Win / فوز", value="win"), app_commands.Choice(name="Loss / خسارة", value="loss")]
TRYOUT_CHOICES = [app_commands.Choice(name="Passed / نجاح", value="win"), app_commands.Choice(name="Failed / فشل", value="loss")]
LOG_TYPE_CHOICES = [app_commands.Choice(name="All types / كل الأنواع (default)", value="all")] + [app_commands.Choice(name=v["label"][:100], value=k) for k, v in LOG_TYPES.items()]
KIND_CHANNEL = [app_commands.Choice(name="Review channel / قناة المراجعة", value="review_channel"), app_commands.Choice(name="Log channel / قناة اللوجات", value="log_channel"), app_commands.Choice(name="Audit channel / قناة التدقيق", value="audit_channel"), app_commands.Choice(name="Update notices / قناة التحديثات", value="update_channel")]
KIND_PING = [app_commands.Choice(name="Review ping / منشن المراجعة", value="review_ping"), app_commands.Choice(name="Log ping / منشن اللوجات", value="log_ping")]
KIND_ROLES = [app_commands.Choice(name="Reviewers / المراجعين", value="review"), app_commands.Choice(name="Submitters / المسجلين", value="submit")]
ACTIONS = [app_commands.Choice(name="Add / إضافة", value="add"), app_commands.Choice(name="Remove / إزالة", value="remove")]
PERIOD_CHOICES = [app_commands.Choice(name="All time / كل الأوقات", value="all"), app_commands.Choice(name="This season / هذا الموسم", value="season"), app_commands.Choice(name="This week / هذا الأسبوع", value="week")]
RULE_CHOICES = [app_commands.Choice(name=f"{l[:80]} / {k}", value=k) for k, l in RULE_LABELS.items()]
POINT_CHOICES = [app_commands.Choice(name=f"{l} / {k}", value=k) for k, l in POINT_LABELS.items()]
RANK_CHOICES = [app_commands.Choice(name=n, value=n) for n, _ in LADDER]
BACKUP_SCOPE_CHOICES = [app_commands.Choice(name="Guild Activity / نشاط السيرفر (today)", value="today"), app_commands.Choice(name="Full Database / قاعدة البيانات (Owner only)", value="full")]
BACKUP_DEST_CHOICES = [app_commands.Choice(name="Here in chat / هنا في الشات", value="chat"), app_commands.Choice(name="Bot Owner DM / خاص المالك", value="owner")]

COLOR_CHOICES = [
    app_commands.Choice(name="🔴 Red / أحمر", value="FF0000"), app_commands.Choice(name="🟢 Green / أخضر", value="00FF00"),
    app_commands.Choice(name="🔵 Blue / أزرق", value="0000FF"), app_commands.Choice(name="🟣 Purple / بنفسجي", value="800080"),
    app_commands.Choice(name="🟡 Yellow / أصفر", value="FFFF00"), app_commands.Choice(name="🟠 Orange / برتقالي", value="FFA500"),
    app_commands.Choice(name="🩷 Pink / وردي", value="FFC0CB"), app_commands.Choice(name="⚫ Black / أسود", value="000000"),
    app_commands.Choice(name="⚪ White / أبيض", value="FFFFFF"), app_commands.Choice(name="🩵 Cyan / سماوي", value="00FFFF")
]

UPDATE_PRESETS = {
    "bot_patch": {
        "label": f"🤖 Release notes {BOT_VERSION}",
        "title_en": f"DragonSlayerBot {BOT_VERSION}",
        "title_ar": f"إصدار البوت {BOT_VERSION}",
        "desc_en": ("• Bilingual system (English / Arabic).\n• V8.0.1: Security hardening, Temp Admin protection, LFG abuse prevention."),
        "desc_ar": ("• نظام مزدوج اللغة (إنجليزي / عربي).\n• تحديث V8.0.1: تحسينات أمنية، حماية Temp Admin، حماية LFG من السبام."),
    }
}
UPDATE_TYPE_CHOICES = [app_commands.Choice(name=v["label"][:100], value=k) for k, v in UPDATE_PRESETS.items()]

ALL_BOT_COMMANDS = [
    "log-war", "log-1v1", "log-2v2", "log-training", "log-tryout", "resubmit", "log-update", "set-log-template", "points", "leaderboard", "profile",
    "stats", "rates", "link", "pending", "revoke-log", "inactive", "season-reset", "season-top", "backup", "setchannel", "setping",
    "setroles", "setrule", "set-log-points", "set-rank-points", "set-points", "sync-ranks", "verify-points", "settings", "sync", "set-cmd-perms", "set-update-ping",
    "help", "send-help-panel", "restore-backup", "my-logs", "log-info", "reviewer-stats", "create-new-crew", "crews-list", "crews-delete-all",
    "streak", "shop", "buy", "achievements", "claim-achievement", "quests", "event", "start-vote", "add-shop-item", "add-achievement", "add-quest", "customize-msg", "set-custom-reward",
    "create-custom-role", "h2h", "lfg", "daily", "setup-tickets", "set-bio", "embed", "set-temp-voice", "create-temp-voice", "set-notification", "purge", "set-lfg-roles", "set-top-rewards",
    "grant-temp-admin", "revoke-temp-admin"
]

# ================================================================ النصوص (Bilingual)
TEXTS = {
    "no_perm": "❌ No permission / مش معاك صلاحية.",
    "no_role": "❌ Missing required role / مش معاك الرتبة المطلوبة.",
    "server_only": "❌ Server only / جوه السيرفر فقط.",
    "unexpected_err": "❌ Unexpected error / حصل خطأ غير متوقع.",
    "db_err": "❌ Database error / حصل خطأ في قاعدة البيانات.",
    "cooldown": "⏳ Wait {time}s / استنى {time} ثانية.",
    "log_not_found": "❌ Log not found / اللوج غير موجود.",
    "log_already_reviewed": "⚠️ Already reviewed / اللوج ده اتراجع بالفعل.",
    "no_review_perm": "❌ No permission to review / معندكش صلاحية مراجعة النوع ده.",
    "no_self_review": "❌ Cannot review your own log / مينفعش تراجع اللوج بتاعك.",
    "old_season_log": "❌ Previous season log / اللوج ده تبع موسم قديم.",
    "log_rejected_ok": "✅ Log rejected / تم رفض اللوج.",
    "not_tagged": "❌ Not listed as participant / اسمك مش متضاف كمشارك.",
    "already_confirmed": "✅ Already confirmed / انت أكدت مشاركتك بالفعل.",
    "part_confirmed": "✅ Participation confirmed / تم تأكيد مشاركتك!",
    "daily_limit_hit": "❌ Daily limit reached ({limit} logs) / وصلت للحد اليومي ({limit} لوج).",
    "log_submitted": "✅ Log `#{id}` submitted / تم إرسال اللوج `#{id}` للمراجعة.",
    "no_submit_perm": "❌ No permission to submit / معندكش صلاحية تسجيل النوع ده.",
    "war_min_files": "❌ Requires 2 attachments / حرب الكرو بتحتاج **ملفين على الأقل**.",
    "min_one_file": "❌ Attach at least one file / لازم ترفق **ملف واحد على الأقل**.",
    "no_review_ch": "❌ Review channel not set / قناة المراجعة مش متحدة.",
    "bad_yt": "❌ Invalid YouTube link / رابط يوتيوب غير صالح.",
    "bad_candidate": "❌ Invalid candidate / المرشح لازم يكون عضو حقيقي تاني.",
    "too_many_files": "❌ Too many files (max {max}) / ملفات كتير جداً (الحد {max}).",
    "bad_file_ext": "❌ File `{name}` unsupported / صيغة الملف `{name}` غير مدعومة.",
    "file_too_big": "❌ File `{name}` > 25 MB / حجم الملف `{name}` أكبر من 25 ميجا.",
    "total_too_big": "❌ Total size exceeds 30 MB / إجمالي الحجم تخطى 30 ميجا.",
    "points_msg": "💰 {member}'s balance: **{total}** points / رصيد {member}: **{total}** نقطة.",
    "no_points_yet": "ℹ️ No points recorded yet / مفيش نقاط مسجلة لسه.",
    "lb_week": "📊 Weekly Leaderboard / متصدري الأسبوع",
    "lb_season": "🏆 Season Leaderboard / متصدرى الموسم",
    "lb_all": "📈 All-Time Leaderboard / متصدري كل الأوقات",
    "no_pending": "✅ No pending logs / مفيش لوجات منتظرة.",
    "no_my_logs": "ℹ️ No logs recorded yet / مفيش لوجات ليك لسه.",
    "sync_ok": "✅ Commands synced / تم مزامنة الأوامر.",
    "sync_fail": "❌ Sync failed / فشلت المزامنة.",
    "crew_bad_members": "❌ Invalid members / فيه عضو غير صحيح.",
    "no_crews": "⚠️ No crews / لا توجد طواقم.",
    "crews_prompt": "🎖️ **Available crews:**\nPick one: / **قائمة الطواقم:**\nاختر طاقماً:",
    "crew_created": "✅ Crew **{name}** created! / تم تأسيس الطاقم **{name}**!",
    "crew_exists": "❌ Name already taken / اسم الطاقم مستخدم.",
    "crew_not_found": "❌ Crew doesn't exist / الطاقم غير موجود.",
    "crew_bad_name": "❌ Name must be 2-40 chars / اسم الطاقم من 2-40 حرف.",
    "crew_bad_owner": "❌ Owner must be real / المالك لازم عضو حقيقي.",
    "crew_limit": "❌ Limit reached ({max}) / وصلت للحد الأقصى ({max}).",
    "select_placeholder": "📂 Pick a crew... / اختر طاقماً...",
    "none": "None / لا يوجد",
    "empty": "Empty / فارغ",
    "founded": "Founded / تاريخ التأسيس",
    "team_commanders": "Commanders / الكوماندرز",
    "browse_page": "Page {page} of {pages} / صفحة {page} من {pages}",
    "shop_title": "🏪 Point Shop / متجر النقاط",
    "shop_desc": "Buy custom roles with your points! Use `/buy` / اشتري رتب مخصصة بنقاطك! استخدم `/buy`",
    "shop_empty": "⚠️ Shop is empty / المتجر فاضي حالياً.",
    "shop_bought": "✅ Bought **{item}** for {price} points / مبروك! اشتريت **{item}** بـ {price} نقطة.",
    "shop_no_points": "❌ Need **{price}** points, you have **{points}** / محتاج **{price}** نقطة وعندك **{points}** بس.",
    "streak_title": "🔥 Your Streak / الستريك الخاص بك",
    "streak_desc": "You logged in for **{current}** days in a row!\nMax streak: **{max}** days. / أنت مسجل دخول لمدة **{current}** أيام متتالية!\nأعلى ستريك لك هو **{max}** أيام.",
    "achieve_title": "🏆 Achievement Unlocked! / إنجاز جديد!",
    "achieve_desc": "Congrats {user}! Unlocked **{name}**!\nClaim reward with `/claim-achievement` / مبروك {user}! فتحت إنجاز **{name}**!\nتقدر تستلم مكافأتك بـ `/claim-achievement`",
    "quest_title": "🎯 Daily Quests / المهام اليومية",
    "event_title": "📅 Event: {title} / فعالية: {title}",
    "event_desc": "**Time:** {time}\nClick buttons to confirm! / **الوقت:** {time}\nاضغط على الأزرار لتأكيد حضورك!",
    "event_joined": "✅ Attendance confirmed / تم تسجيل حضورك!",
    "event_left": "❌ Attendance cancelled / تم إلغاء حضورك.",
    "role_created": "✅ Custom role {role} created for {member}! / تم إنشاء الرتبة المخصصة {role} لـ {member}!",
    "role_created_free": "✅ Role {role} created for free (Admin/Owner)! / تم إنشاء الرتبة {role} مجاناً (أنت أدمن/مالك)!",
    "achieve_claimed": "✅ Successfully claimed reward for **{name}**! / تم استلام مكافأة إنجاز **{name}** بنجاح!",
    "achieve_already_claimed": "⚠️ Already claimed or no achievements / استلمت المكافأة قبل كده أو مفيش إنجازات."
}

def tr(key: str, **kw) -> str:
    text = TEXTS.get(key, key)
    return text.format(**kw) if kw else text

# ================================================================ قاعدة البيانات
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
CREATE TABLE IF NOT EXISTS crews(
    guild_id INTEGER, crew_name TEXT, owner_id INTEGER, captains_data TEXT, voice_captains_data TEXT,
    team_z_data TEXT, team_y_data TEXT, team_x_data TEXT, team_s_data TEXT, team_a_data TEXT,
    team_z_commanders_data TEXT, team_y_commanders_data TEXT, team_x_commanders_data TEXT, team_s_commanders_data TEXT, team_a_commanders_data TEXT,
    created_at TEXT, PRIMARY KEY(guild_id, crew_name)
);
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
"""

LEGACY_COPY = {
    "settings": "INSERT OR IGNORE INTO settings(guild_id,key,value) SELECT 0, key, CAST(value AS TEXT) FROM settings_legacy",
    "members": "INSERT OR IGNORE INTO members(guild_id,user_id,points) SELECT 0, user_id, points FROM members_legacy",
    "links": "INSERT OR IGNORE INTO links(guild_id,user_id,roblox_id,roblox_name) SELECT 0, user_id, roblox_id, roblox_name FROM links_legacy",
    "season_archive": "INSERT OR IGNORE INTO season_archive(season,guild_id,user_id,points) SELECT season, 0, user_id, points FROM season_archive_legacy",
    "point_events": "INSERT INTO point_events(guild_id,log_id,user_id,points,created_at,type,season_id) SELECT 0, log_id, user_id, points, created_at, 'log', 'main' FROM point_events_legacy",
}
LOG_COLUMNS = [("guild_id", "INTEGER NOT NULL DEFAULT 0"), ("review_channel", "INTEGER"), ("review_msg", "INTEGER"), ("public_channel", "INTEGER"), ("public_msg", "INTEGER"), ("reviewed_at", "TEXT"), ("reminded", "INTEGER DEFAULT 0"), ("parent_id", "INTEGER"), ("season_id", "TEXT DEFAULT 'main'")]
LEGACY_TYPE_MAP = {"outside": "1v1", "internal": "training"}
LEGACY_POINT_KEYS = {"points:outside_win": "points:1v1_win", "points:outside_loss": "points:1v1_loss", "points:internal": "points:training", "template:outside": "template:1v1", "template:internal": "template:training"}

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

# [PATCH 3] Whitelist لأسماء الجداول المسموح بها
_ALLOWED_TABLES = {
    "settings", "members", "guild_channels", "logs", "hashes",
    "guild_cfg", "role_perms", "point_events", "links",
    "season_archive", "cmd_roles", "cmd_users", "crews",
    "custom_settings", "custom_messages", "custom_roles",
    "temp_voice_rooms", "streaks", "shop_items", "achievements",
    "user_achievements", "quests", "events", "tickets",
    "daily_bonus", "bios", "notif_settings", "lfg_allowed_roles",
    "votes", "temp_perms",
    "settings_legacy", "members_legacy", "links_legacy",
    "season_archive_legacy", "point_events_legacy",
}

async def table_columns(db, table):
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Invalid table name: {table}")
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

        crew_column_migrations = {
            "guild_id": "INTEGER NOT NULL DEFAULT 0", "crew_name": "TEXT NOT NULL DEFAULT ''", "owner_id": "INTEGER NOT NULL DEFAULT 0",
            "captains_data": "TEXT NOT NULL DEFAULT '[]'", "voice_captains_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_z_data": "TEXT NOT NULL DEFAULT '[]'", "team_y_data": "TEXT NOT NULL DEFAULT '[]'", "team_x_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_s_data": "TEXT NOT NULL DEFAULT '[]'", "team_a_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_z_commanders_data": "TEXT NOT NULL DEFAULT '[]'", "team_y_commanders_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_x_commanders_data": "TEXT NOT NULL DEFAULT '[]'", "team_s_commanders_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_a_commanders_data": "TEXT NOT NULL DEFAULT '[]'", "created_at": "TEXT NOT NULL DEFAULT ''",
        }
        crew_cols = await table_columns(db, "crews")
        for col, definition in crew_column_migrations.items():
            if col not in crew_cols:
                await db.execute(f"ALTER TABLE crews ADD COLUMN {col} {definition}")

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
def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()

async def auto_restore_from_json():
    if not os.path.exists(JSON_CONFIG_PATH):
        return
    try:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT (SELECT COUNT(*) FROM settings) + (SELECT COUNT(*) FROM guild_cfg) + (SELECT COUNT(*) FROM role_perms) + (SELECT COUNT(*) FROM cmd_roles)")
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
                await db.execute("INSERT OR IGNORE INTO guild_cfg VALUES(?,?,?,?)", (r["guild_id"], r["log_type"], r["key"], r["value"]))
            for r in data.get("role_perms", []):
                await db.execute("INSERT OR IGNORE INTO role_perms VALUES(?,?,?,?)", (r["guild_id"], r["log_type"], r["kind"], r["role_id"]))
            for r in data.get("cmd_roles", []):
                await db.execute("INSERT OR IGNORE INTO cmd_roles VALUES(?,?,?)", (r["guild_id"], r["cmd_name"], r["role_id"]))
            await db.commit()
        print("✅ Settings restored from JSON.")
    except Exception as e:
        print(f"⚠️ Could not restore settings snapshot: {e}")

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
    await db_exec("INSERT INTO settings(guild_id, key, value) VALUES(?,?,?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (guild_id, str(key), str(value)))
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
            if ids:
                return ids
    return []

async def roles_edit(guild_id, log_type, kind, role_id, add: bool):
    async with aiosqlite.connect(DB) as db:
        if add:
            await db.execute("INSERT OR IGNORE INTO role_perms VALUES(?,?,?,?)", (guild_id, log_type, kind, role_id))
        else:
            await db.execute("DELETE FROM role_perms WHERE guild_id=? AND log_type=? AND kind=? AND role_id=?", (guild_id, log_type, kind, role_id))
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
    try:
        return int(v)
    except (TypeError, ValueError):
        return DEFAULT_POINTS.get(key, 0)

async def points_for(guild_id: int, log_type, result) -> int:
    key = f"{log_type}_{result}" if log_type in ("war", "1v1", "2v2") else log_type
    return await points_for_key(guild_id, key)

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

# ================================================================ التخصيص والقوالب
async def get_custom_msg(guild_id, key, default_title, default_desc, default_color=discord.Color.blue()):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT title, description, color FROM custom_messages WHERE guild_id=? AND key=?", (guild_id, key))
        row = await cur.fetchone()
    if row:
        try:
            color = int(row[2].replace("#", ""), 16) if row[2] else default_color
        except ValueError:
            color = default_color
        return discord.Embed(title=row[0][:256], description=row[1][:4000], color=color)
    return discord.Embed(title=default_title[:256], description=default_desc[:4000], color=default_color)

async def get_custom_val(guild_id, key, default):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM custom_settings WHERE guild_id=? AND key=?", (guild_id, key))
        row = await cur.fetchone()
    return row[0] if row else default

# ================================================================ الإشعارات الذكية
async def send_notification(guild, event_type, title, desc, color=discord.Color.blue()):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT mode, target_id FROM notif_settings WHERE guild_id=? AND event_type=?", (guild.id, event_type))
        row = await cur.fetchone()
    if not row:
        return
    mode, target_id = row
    embed = await get_custom_msg(guild.id, f"notif_{event_type}", title, desc, color)
    
    if mode == "channel" and target_id:
        ch = guild.get_channel(target_id)
        if ch:
            try:
                await ch.send(embed=embed)
            except discord.HTTPException:
                pass
    elif mode == "dm" and target_id:
        role = guild.get_role(target_id)
        if role and len(role.members) <= 50:
            for member in role.members:
                if member.bot:
                    continue
                try:
                    await member.send(embed=embed)
                except discord.HTTPException:
                    pass
                await asyncio.sleep(0.5)

# ================================================================ الصلاحيات (بما فيها الحماية الجديدة)
def has_role(member, names) -> bool:
    normalized = {str(n).strip().casefold() for n in names if str(n).strip()}
    return any(r.name.strip().casefold() in normalized for r in getattr(member, "roles", []))

def is_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if owner_id() and member.id == owner_id():
        return True
    return member.guild_permissions.administrator

# [PATCH 4] قائمة الأوامر اللي مش مسموح للـ Temp Admin يستخدمها
TEMP_ADMIN_BLOCKED_COMMANDS = {
    "grant-temp-admin",
    "revoke-temp-admin",
    "backup",
    "restore-backup",
    "set-cmd-perms",
    "setchannel",
    "setrule",
    "settings",
    "purge",
    "sync",
}

async def has_temp_admin(member) -> bool:
    if not member.guild:
        return False
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT expires_at FROM temp_perms WHERE guild_id=? AND user_id=?", (member.guild.id, member.id))
        row = await cur.fetchone()
        if not row:
            return False
        try:
            expires = dt.datetime.fromisoformat(row[0])
            if now_utc() > expires:
                await db.execute("DELETE FROM temp_perms WHERE guild_id=? AND user_id=?", (member.guild.id, member.id))
                await db.commit()
                return False
            return True
        except ValueError:
            return False

async def is_editor(member) -> bool:
    return is_admin(member) or await has_temp_admin(member) or has_role(member, EDITOR_ROLES)

# [PATCH 5] دالة جديدة عشان تحمي الـ Temp Admin من الأوامر الحساسة
async def is_editor_for_command(member, cmd_name: str) -> bool:
    if is_admin(member):
        return True
    if await has_temp_admin(member):
        return cmd_name not in TEMP_ADMIN_BLOCKED_COMMANDS
    return has_role(member, EDITOR_ROLES)

async def can_review(member, log_type) -> bool:
    if is_admin(member) or await has_temp_admin(member):
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
    if is_admin(member) or await has_temp_admin(member):
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
    if is_admin(i.user) or await has_temp_admin(i.user):
        return True
    
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT role_id FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, cmd_name))
        role_row = await cur.fetchone()
        cur = await db.execute("SELECT user_id FROM cmd_users WHERE guild_id=? AND cmd_name=?", (i.guild.id, cmd_name))
        user_rows = await cur.fetchall()
    
    allowed_users = [r[0] for r in user_rows]
    
    if not allowed_users and not role_row:
        return True
    if i.user.id in allowed_users:
        return True
    if role_row:
        return any(r.id == role_row[0] for r in i.user.roles)
        
    return False

# [PATCH 6] تحديث gate عشان يستخدم الحماية الجديدة
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

# ================================================================ مساعدات
async def _ensure_members(guild):
    if getattr(guild, "chunked", False):
        return
    try:
        async for _ in guild.fetch_members(limit=None):
            pass
    except Exception as e:
        print(f"⚠️ Error fetching members: {e}")

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
    except Exception as e:
        print(f"⚠️ Failed to DM owner: {e}")
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
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = tr("cooldown", time=f"{error.retry_after:.1f}")
        try:
            if i.response.is_done():
                await i.followup.send(msg, ephemeral=True)
            else:
                await i.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass
        return
        
    print(f"❌ Unhandled error: {type(error).__name__}: {error}")
    msg = tr("unexpected_err")
    try:
        if i.response.is_done():
            await i.followup.send(msg, ephemeral=True)
        else:
            await i.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass

# ================================================================ البوت
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
        self.add_view(TicketView())
        self.add_view(CloseTicketView())
        self.add_view(EventView())
        self.add_view(VoteView())
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
                try:
                    await check_and_notify_auto_update(g)
                except Exception as e:
                    print(f"⚠️ Update notice failed for {g.id}: {e}")
        if not maintenance.is_running():
            maintenance.start()

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)
        try:
            target = guild.system_channel or next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
            if target:
                await target.send(f"👋 Thanks for adding **DragonSlayerBot** to **{guild.name}**!\nUse `/help` for a full manual. Admins: run `/setchannel` to configure.")
        except Exception as e:
            print(f"⚠️ Welcome message failed: {e}")

bot = CrewBot()

@bot.tree.error
async def on_app_command_error(i: discord.Interaction, error):
    await report_error(i, error)

# ================================================================ شكل اللوج الثابت
_EMO = getattr(config, "EMOJIS", {}) or {}
ANGEL = _EMO.get("angel", "<:emoji_2:1568348048906649652>")
MOON = _EMO.get("bullet", "<a:emoji_15:1568348248522100757>")
ARROW = _EMO.get("arrow", "<a:emoji_8:1568348117236187237>")
CROWN = _EMO.get("crown", "<:emoji_11:1568348158495432754>")
_SPARK = _EMO.get("spark", "<a:emoji_6:1568348105781416008>")
DIVIDER = _EMO.get("divider", f"{_SPARK}   ●▬▬▬▬▬▬▬▬▬▬▬☭   ☭▬▬▬▬▬▬▬▬▬▬▬●   {_SPARK}")
MSG_LIMIT = 1900

DEFAULT_TEMPLATES = {
    "war": "{MOON} **Logged by:** {author}\n{MOON} **Dragon Slayer's Fighters:**\n{ARROW} {my_team}\n\n{DIVIDER}\n\n{MOON} **{crew} Fighters:**\n{ARROW} {their_team}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{participants_block}{flags_block}",
    "1v1": "{MOON} **Logged by:** {author}\n{MOON} **Dragon Slayer's Fighter:**\n{ARROW} {my_fighter}\n\n{DIVIDER}\n\n{MOON} **Opponent:**\n{ARROW} {opponent}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{flags_block}",
    "2v2": "{MOON} **Logged by:** {author}\n{MOON} **Dragon Slayer's Team:**\n{ARROW} {my_team}\n\n{DIVIDER}\n\n{MOON} **Enemy Team:**\n{ARROW} {their_team}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{participants_block}{flags_block}",
    "training": "{MOON} **Logged by:** {author}\n{MOON} **Training Participants:**\n{ARROW} {participants}\n\n{DIVIDER}\n\n      • **Score:** {score}\n{notes_line}{youtube_line}{flags_block}",
    "tryout": "{MOON} **Tested by:** {author}\n{MOON} **Tryout Candidate:**\n{ARROW} {candidate} {roblox}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{flags_block}"
}

def clip(v, n=350):
    v = str(v) if v not in (None, "") else "-"
    return v if len(v) <= n else v[: n - 1] + "…"

async def get_custom_template(guild_id, log_type):
    t = await raw_setting(guild_id, f"template:{log_type}")
    return t or DEFAULT_TEMPLATES.get(log_type, "")

def apply_template(template: str, values: dict) -> str:
    return TEMPLATE_VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)

def render(log_id, log_type, author_id, data, result, flags=None, status="pending", points=None, reviewer_id=None, reason=None, pts_to=None, custom_template=None):
    label = LOG_TYPES.get(log_type, {}).get("label", log_type)
    d = lambda k, default="-": clip(data.get(k, default))
    participants = dedupe([int(x) for x in data.get("participants", [])]) if isinstance(data.get("participants"), list) else []
    confirmed = {int(x) for x in data.get("confirmed_participants", [])}
    
    template = custom_template or DEFAULT_TEMPLATES.get(log_type, "")
    labels = TRYOUT_LABEL if log_type == "tryout" else RESULT_LABEL
    
    values = {
        "log_id": log_id, "author": f"<@{author_id}>", "score": d("score"), "notes": d("notes"),
        "youtube_link": data.get("youtube_link", "-"), "my_team": d("my_team"), "their_team": d("their_team"),
        "crew": d("crew"), "my_fighter": d("my_fighter"), "opponent": d("opponent"),
        "participants": d("participants") if not participants else " ".join(f"<@{p}>" for p in participants),
        "candidate": data.get("candidate", "-"), "roblox": f"({clip(data['roblox'], 40)})" if data.get("roblox") else "",
        "p1": d("p1"), "p2": d("p2"), "result": labels.get(result, result) if result else "-",
        "MOON": MOON, "ARROW": ARROW, "CROWN": CROWN, "DIVIDER": DIVIDER, "ANGEL": ANGEL,
        "notes_line": f"      • **Notes:** {d('notes')}\n" if data.get("notes") else "",
        "youtube_line": f"      • **YouTube Video:** <{data['youtube_link']}>\n" if data.get("youtube_link") else "",
        "participants_block": f"\n{MOON} **Tagged Teammates Verification:**\n" + "".join(f"• <@{p}> — **{'✅ Confirmed' if p in confirmed else '⏳ Unconfirmed'}**\n" for p in participants[:8]) if log_type in ("war", "2v2") and participants else "",
        "flags_block": f"\n⚠️ **Warnings:**\n" + "\n".join("• " + clip(f, 120) for f in flags[:5]) if flags else ""
    }

    header = f"{ANGEL}  **{label}**  {ANGEL}  🆔 `Log #{log_id}`\n\n{DIVIDER}\n\n"
    body = header + apply_template(clip(template, 1200), values) + "\n\n" + DIVIDER + "\n"
    
    footer = f"\n**Status:** {STATUS_LABEL[status]}"
    if points is not None:
        footer += f" | **Points:** +{points}"
        if pts_to: footer += " → " + " ".join(f"<@{u}>" for u in list(pts_to)[:6])
    if reviewer_id and status != "pending":
        who = {"accepted": "Accepted by", "rejected": "Rejected by", "revoked": "Revoked by"}[status]
        footer += f"\n{MOON} **{who}:** <@{reviewer_id}>"
    if reason: footer += f"\n**Reason:** {clip(reason, 300)}"
    
    if len(body) + len(footer) > MSG_LIMIT:
        body = body[: max(200, MSG_LIMIT - len(footer) - 1)] + "…"
    return (body + footer)[:MSG_LIMIT + 100]

# ================================================================ الرتب
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
        await audit(f"⚠️ Role for **{ladder[target][0]}** was not found in this server, rank update skipped for {member.mention}.", guild)
        return None

    to_add = [target_role] if target_role and target_role not in member.roles else []
    to_remove = [roles[idx] for idx in held if roles[idx] != target_role]
    try:
        if to_add: await member.add_roles(*to_add, reason="Rank update based on points")
        if to_remove: await member.remove_roles(*to_remove, reason="Rank update based on points")
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
                await ch.send(f"🎉 {member.mention} has been promoted to {target_role.mention}!", allowed_mentions=discord.AllowedMentions(users=[member], roles=False))
            except discord.HTTPException: pass
        try: await member.send(f"🎉 You've been promoted to **{target_role.name}** in **{guild.name}**!")
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
        except Exception as e:
            print(f"⚠️ Rank update failed for {uid}: {e}")
        await asyncio.sleep(0.25)
    return changed
# ================================================================ الإدارة المؤقتة (Temp Admin)
@bot.tree.command(name="grant-temp-admin", description="Grant temporary admin permissions / إعطاء صلاحيات إدارية مؤقتة")
@app_commands.checks.cooldown(1, 10.0)
@app_commands.describe(member="Member / العضو", minutes="Duration in minutes (1-1440) / المدة بالدقائق")
async def grant_temp_admin(i: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 1440]):
    if not is_admin(i.user) and i.user.id != owner_id():
        return await i.response.send_message(tr("no_perm"), ephemeral=True)
        
    if member.bot:
        return await i.response.send_message("❌ Cannot give temp admin to a bot. / لا يمكن إعطاء الصلاحية لبوت.", ephemeral=True)
        
    expires_at = (now_utc() + timedelta(minutes=minutes)).isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO temp_perms(guild_id, user_id, expires_at) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET expires_at=excluded.expires_at", (i.guild.id, member.id, expires_at))
        await db.commit()
        
    await audit(f"👑 {i.user.mention} granted Temp Admin to {member.mention} for {minutes} minutes.", i.guild)
    await i.response.send_message(f"✅ Granted temporary admin to {member.mention} for **{minutes}** minutes. / تم إعطاء صلاحيات مؤقتة لـ {member.mention} لمدة {minutes} دقيقة.", ephemeral=True)
    try: await member.send(f"👑 You have been granted **Temporary Admin** in **{i.guild.name}** for {minutes} minutes. / تم إعطاؤك صلاحيات إدارية مؤقتة في {i.guild.name} لمدة {minutes} دقيقة.")
    except discord.HTTPException: pass

@bot.tree.command(name="revoke-temp-admin", description="Revoke temporary admin permissions / سحب الصلاحيات المؤقتة")
@app_commands.checks.cooldown(1, 10.0)
async def revoke_temp_admin(i: discord.Interaction, member: discord.Member):
    if not is_admin(i.user) and i.user.id != owner_id():
        return await i.response.send_message(tr("no_perm"), ephemeral=True)
        
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM temp_perms WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        if not await cur.fetchone():
            return await i.response.send_message("❌ This user doesn't have temp admin. / هذا العضو ليس لديه صلاحيات مؤقتة.", ephemeral=True)
        await db.execute("DELETE FROM temp_perms WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        await db.commit()
        
    await audit(f"🚫 {i.user.mention} revoked Temp Admin from {member.mention}.", i.guild)
    await i.response.send_message(f"✅ Revoked temporary admin from {member.mention}. / تم سحب الصلاحيات المؤقتة من {member.mention}.", ephemeral=True)

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

# ================================================================ إنشاء اللوج
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
        text = render(log_id, self.log_type, i.user.id, data, self.result, flags, custom_template=custom_tmpl)
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
                except discord.Forbidden:
                    raise
                except discord.HTTPException:
                    sent = None
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
    if is_admin(i.user) or await has_temp_admin(i.user):
        return True

    if (now_utc() - i.user.created_at).days < 7:
        await i.response.send_message("⚠️ **Log Rejected:** Your account is too new (less than 7 days). / **تم رفض اللوج:** حسابك جديد جداً (أقل من 7 أيام).", ephemeral=True)
        return False
        
    since = (now_utc() - timedelta(hours=1)).isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? AND status IN ('pending', 'accepted')",
            (i.guild.id, i.user.id, since)
        )
        if (await cur.fetchone())[0] >= 5:
            await i.response.send_message("⚠️ **Log Rejected:** You are submitting logs too fast! Please wait. / **تم رفض اللوج:** أنت تسجل لوجات بسرعة كبيرة! انتظر قليلاً.", ephemeral=True)
            return False
    return True

async def start_log(i: discord.Interaction, log_type, result, files: List[Optional[discord.Attachment]], extra=None):
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message(tr("server_only"), ephemeral=True)
        
    if not await check_anti_cheat(i, log_type):
        return
        
    files = [f for f in files if f is not None]
    
    if not await check_cmd_perm(i, f"log-{log_type}"):
        return await i.response.send_message(tr("no_role"), ephemeral=True)
    if not await can_submit(i.user, log_type):
        return await i.response.send_message(tr("no_submit_perm"), ephemeral=True)
        
    if log_type == "war" and len(files) < 2:
        return await i.response.send_message(tr("war_min_files"), ephemeral=True)
    if log_type != "war" and len(files) < 1:
        return await i.response.send_message(tr("min_one_file"), ephemeral=True)
        
    rid = await cfg_get(i.guild.id, log_type, "review_channel")
    if not rid or not i.guild.get_channel(rid):
        return await i.response.send_message(tr("no_review_ch"), ephemeral=True)
        
    limit = await rule(i.guild.id, "daily_limit")
    if limit:
        since = (now_utc() - timedelta(hours=24)).isoformat()
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? AND status IN ('pending','accepted')", (i.guild.id, i.user.id, since))
            n = (await cur.fetchone())[0]
        if n >= limit:
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
@bot.tree.command(name="log-war", description="Log a Crew War (min 2 attachments) / تسجيل حرب كرو (ملفين على الأقل)")
@app_commands.checks.cooldown(1, 60.0)
@app_commands.choices(result=RESULT_CHOICES)
async def log_war(i: discord.Interaction, result: app_commands.Choice[str], file1: discord.Attachment, file2: discord.Attachment, file3: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None, player1: Optional[discord.Member] = None, player2: Optional[discord.Member] = None, player3: Optional[discord.Member] = None, player4: Optional[discord.Member] = None):
    ids = dedupe([p.id for p in (player1, player2, player3, player4) if p and not p.bot and p.id != i.user.id])
    extra = {"participants": ids}
    if not await youtube_extra(i, youtube_link, extra): return
    await start_log(i, "war", result.value, [file1, file2, file3], extra)

@bot.tree.command(name="log-1v1", description="Log a 1v1 fight / تسجيل فايت 1 ضد 1")
@app_commands.checks.cooldown(1, 30.0)
@app_commands.choices(result=RESULT_CHOICES)
async def log_1v1(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None):
    extra = {}
    if not await youtube_extra(i, youtube_link, extra): return
    await start_log(i, "1v1", result.value, [proof1, proof2], extra)

@bot.tree.command(name="log-2v2", description="Log a 2v2 fight / تسجيل فايت 2 ضد 2")
@app_commands.checks.cooldown(1, 30.0)
@app_commands.choices(result=RESULT_CHOICES)
async def log_2v2(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None, teammate: Optional[discord.Member] = None):
    ids = [teammate.id] if teammate and not teammate.bot and teammate.id != i.user.id else []
    extra = {"participants": ids}
    if not await youtube_extra(i, youtube_link, extra): return
    await start_log(i, "2v2", result.value, [proof1, proof2], extra)

@bot.tree.command(name="log-training", description="Log training / تسجيل تدريب")
@app_commands.checks.cooldown(1, 30.0)
async def log_training(i: discord.Interaction, proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None):
    await start_log(i, "training", None, [proof1, proof2])

@bot.tree.command(name="log-tryout", description="Log a tryout / تسجيل تراي أوت")
@app_commands.checks.cooldown(1, 60.0)
@app_commands.choices(result=TRYOUT_CHOICES)
async def log_tryout(i: discord.Interaction, candidate: discord.Member, result: app_commands.Choice[str], proof: discord.Attachment):
    if candidate.bot or candidate.id == i.user.id:
        return await i.response.send_message(tr("bad_candidate"), ephemeral=True)
    extra = {"candidate_id": candidate.id, "candidate": candidate.mention, "tester_id": i.user.id}
    await start_log(i, "tryout", result.value, [proof], extra)

@bot.tree.command(name="resubmit", description="Fix and resend a rejected log / إعادة إرسال لوج مرفوض")
@app_commands.checks.cooldown(1, 15.0)
async def resubmit(i: discord.Interaction, log_id: int):
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message(tr("server_only"), ephemeral=True)
    row = await get_log(log_id)
    
    if not row or row["guild_id"] != i.guild.id or row["author_id"] != i.user.id:
        return await i.response.send_message(tr("log_not_found"), ephemeral=True)
    if row["status"] != "rejected" or row["type"] not in LOG_TYPES:
        return await i.response.send_message("❌ This log is not rejected. / هذا اللوج ليس مرفوضاً.", ephemeral=True)
        
    if not await check_cmd_perm(i, f"log-{row['type']}"):
        return await i.response.send_message(tr("no_role"), ephemeral=True)
    if not await can_submit(i.user, row["type"]):
        return await i.response.send_message(tr("no_submit_perm"), ephemeral=True)

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id FROM logs WHERE parent_id=? AND status IN ('pending','accepted') LIMIT 1", (log_id,))
        child = await cur.fetchone()
    if child:
        return await i.response.send_message(f"This log was already resubmitted as #{child[0]}.", ephemeral=True)
        
    # [PATCH 9a] حماية من json.loads(None)
    data = json.loads(row["data"] or "{}")
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
        return await i.response.send_message("Could not fetch the old attachments. Please submit a new log. / لم يتم العثور على المرفقات القديمة.", ephemeral=True)
        
    extra = {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants", "youtube_link") if k in data}
    await i.response.send_modal(LogModal(row["type"], row["result"], files, extra, defaults=data, parent_id=log_id))

@bot.tree.command(name="revoke-log", description="Cancel an accepted log / إلغاء لوج مقبول")
@app_commands.checks.cooldown(1, 10.0)
async def revoke_log(i: discord.Interaction, log_id: int, reason: app_commands.Range[str, 1, 300]):
    if not await gate(i, "revoke-log"): return
    row = await get_log(log_id)
    
    if not row or row["guild_id"] != i.guild.id or row["status"] != "accepted":
        return await i.response.send_message(tr("log_not_found"), ephemeral=True)
    if not await can_review(i.user, row["type"]):
        return await i.response.send_message(tr("no_perm"), ephemeral=True)
    if (row["season_id"] or "main") != await get_current_season(i.guild.id):
        return await i.response.send_message(tr("old_season_log"), ephemeral=True)
        
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
            print(f"❌ revoke DB error: {e}")
            return await i.followup.send(tr("db_err"), ephemeral=True)

    # [PATCH 9a]
    data = json.loads(row["data"] or "{}")
    custom_tmpl = await get_custom_template(i.guild.id, row["type"])
    text = render(log_id, row["type"], row["author_id"], data, row["result"], None, "revoked", reviewer_id=i.user.id, reason=reason, custom_template=custom_tmpl)
    
    for ch_id, msg_id in ((row["review_channel"], row["review_msg"]), (row["public_channel"], row["public_msg"])):
        if ch_id and msg_id:
            try:
                ch = i.guild.get_channel(ch_id)
                msg = await ch.fetch_message(msg_id)
                await msg.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, AttributeError):
                pass

    warnings = []
    for uid, _ in events:
        member = i.guild.get_member(uid)
        if not member:
            continue
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, uid))
            res = await cur.fetchone()
        if await rank_is_above_points(i.guild, member, res[0] if res else 0):
            warnings.append(member.mention)
            
    await audit(f"🚫 {i.user.mention} revoked Log #{log_id}. Reason: {reason}", i.guild)
    msg = f"✅ Successfully revoked Log #{log_id}. / تم إلغاء اللوج بنجاح."
    if warnings:
        msg += ("\n⚠️ These members now hold a rank above their points: " + " ".join(warnings) + "\nUse `/sync-ranks demote:True` to fix.")
    await i.followup.send(msg, ephemeral=True)

# ================================================================ المراجعة
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
    if not row:
        return
    
    if not await can_review(i.user, row["type"]):
        return await i.response.send_message(tr("no_review_perm"), ephemeral=True)
    if row["author_id"] == i.user.id:
        return await i.response.send_message(tr("no_self_review"), ephemeral=True)
        
    season = row["season_id"] or "main"
    if season != await get_current_season(i.guild.id):
        return await i.response.send_message(tr("old_season_log"), ephemeral=True)
        
    await i.response.defer()
    log_id = row["id"]
    
    # [PATCH 9a]
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
            print(f"❌ accept_log DB error: {e}")
            return await i.followup.send(tr("db_err"), ephemeral=True)

    custom_tmpl = await get_custom_template(i.guild.id, row["type"])
    pub_id = await cfg_get(i.guild.id, row["type"], "log_channel")
    log_ch = i.guild.get_channel(pub_id) if pub_id else None
    
    if log_ch:
        ping_id = await cfg_get(i.guild.id, row["type"], "log_ping")
        text = render(log_id, row["type"], row["author_id"], data, row["result"], None, "accepted", pts, i.user.id, pts_to=recips, custom_template=custom_tmpl)
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
            await i.followup.send(f"Accepted, but couldn't post in the log channel: {e}", ephemeral=True)
    else:
        await i.followup.send("Accepted, but no log channel is set. Use /setchannel.", ephemeral=True)

    reviewed = render(log_id, row["type"], row["author_id"], data, row["result"], flags, "accepted", pts, i.user.id, pts_to=recips, custom_template=custom_tmpl)
    try:
        await i.message.edit(content=reviewed, view=None, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass

    for uid, total in totals.items():
        try:
            member = await get_member(i.guild, uid)
            await update_member_rank(i.guild, member, total)
            
            current_streak, max_streak = await update_streak(i.guild.id, uid)
            streak_points = int(await get_custom_val(i.guild.id, "streak_reward_points", 0))
            if streak_points > 0:
                await db_exec("UPDATE members SET points = points + ? WHERE guild_id=? AND user_id=?", (streak_points, i.guild.id, uid))
                await db_exec("INSERT INTO point_events(guild_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?)", (i.guild.id, uid, streak_points, now_utc().isoformat(), "streak", season))
            
            streak_role_id = await get_custom_val(i.guild.id, "streak_reward_role", None)
            if streak_role_id:
                role = i.guild.get_role(int(streak_role_id))
                if role and role not in member.roles:
                    try: await member.add_roles(role, reason="Streak Reward")
                    except discord.Forbidden: pass

            await check_achievements(i.guild, uid, total)
        except (discord.NotFound, discord.Forbidden) as e:
            print(f"⚠️ Failed to update rank for {uid}: {e}")

    if (row["type"] == "tryout" and row["result"] == "win" and data.get("candidate_id") and await rule(i.guild.id, "tryout_autorole")):
        try:
            cand_member = await get_member(i.guild, int(data["candidate_id"]))
            crew_role = find_crew_role(i.guild)
            if crew_role and crew_role not in cand_member.roles:
                await cand_member.add_roles(crew_role, reason="Passed tryout")
        except (discord.NotFound, discord.Forbidden) as e:
            await i.followup.send(f"Accepted, but couldn't give the Crew role: {e}", ephemeral=True)

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
            return await i.followup.send(tr("log_not_found"), ephemeral=True)
        if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
            return await i.followup.send(tr("no_review_perm"), ephemeral=True)
        if row["author_id"] == i.user.id:
            return await i.followup.send(tr("no_self_review"), ephemeral=True)

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
                print(f"❌ reject DB error: {e}")
                return await i.followup.send(tr("db_err"), ephemeral=True)

        # [PATCH 9a]
        data = json.loads(row["data"] or "{}")
        flags = json.loads(row["flags"] or "[]")
        custom_tmpl = await get_custom_template(i.guild.id, row["type"])
        text = render(self.log_id, row["type"], row["author_id"], data, row["result"], flags, "rejected", reviewer_id=i.user.id, reason=self.reason.value, custom_template=custom_tmpl)
        
        try:
            await self.message.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
            
        await i.followup.send(tr("log_rejected_ok"), ephemeral=True)
        try:
            author = await bot.fetch_user(row["author_id"])
            await author.send(f"❌ Log #{self.log_id} was rejected.\nReason: {self.reason.value}\n\nYou can fix it and send it again with `/resubmit log_id:{self.log_id}`")
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
            return await i.response.send_message(tr("no_review_perm"), ephemeral=True)
        if row["author_id"] == i.user.id:
            return await i.response.send_message(tr("no_self_review"), ephemeral=True)
        await i.response.send_modal(RejectModal(row["id"], i.message))
        
    @discord.ui.button(label="Confirm Participation / تأكيد المشاركة", emoji="✋", style=discord.ButtonStyle.primary, custom_id="log:confirm_part")
    async def confirm_part(self, i: discord.Interaction, button: discord.ui.Button):
        row = await resolve_log(i)
        if not row:
            return
        
        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute("SELECT status, data FROM logs WHERE id=? AND guild_id=?", (row["id"], i.guild.id))
                log_row = await cur.fetchone()
                if not log_row or log_row[0] != "pending":
                    await db.rollback()
                    return await i.response.send_message(tr("log_already_reviewed"), ephemeral=True)
                    
                # [PATCH 9b]
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
                print(f"❌ confirm DB error: {e}")
                return await i.response.send_message(tr("db_err"), ephemeral=True)

        flags = json.loads(row["flags"] or "[]")
        custom_tmpl = await get_custom_template(i.guild.id, row["type"])
        updated = render(row["id"], row["type"], row["author_id"], data, row["result"], flags, custom_template=custom_tmpl)
        await i.response.send_message(tr("part_confirmed"), ephemeral=True)
        try:
            await i.message.edit(content=updated, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

class PendingPaginator(discord.ui.View):
    def __init__(self, items, guild_id, owner_user_id):
        super().__init__(timeout=180)
        self.items, self.guild_id, self.owner_user_id = items, guild_id, owner_user_id
        self.page, self.per_page = 0, 15
        
    def max_pages(self):
        return max(1, (len(self.items) - 1) // self.per_page + 1)
        
    def get_page_text(self):
        chunk = self.items[self.page * self.per_page:(self.page + 1) * self.per_page]
        lines = [f"**#{lid}** {LOG_TYPES.get(t, {}).get('label', t)} — <@{author}> — {str(created)[:16]}{f' https://discord.com/channels/{self.guild_id}/{ch_id}/{msg_id}' if ch_id and msg_id else ''}" for lid, t, author, created, ch_id, msg_id in chunk]
        return f"⏳ **Pending logs / اللوجات المنتظرة** — {len(self.items)} total — page {self.page + 1}/{self.max_pages()}\n\n" + "\n".join(lines)
        
    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.owner_user_id
        
    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, i: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await i.response.edit_message(content=self.get_page_text()[:1990], view=self)
        
    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, i: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_pages() - 1, self.page + 1)
        await i.response.edit_message(content=self.get_page_text()[:1990], view=self)

@bot.tree.command(name="pending", description="List logs waiting for review / اللوجات المنتظرة")
@app_commands.checks.cooldown(1, 10.0)
async def pending(i: discord.Interaction):
    if not await check_cmd_perm(i, "pending"):
        return await i.response.send_message(tr("no_role"), ephemeral=True)
    if not (await is_editor(i.user) or await can_review_any(i.user)):
        return await i.response.send_message(tr("no_perm"), ephemeral=True)
        
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id, type, author_id, created_at, review_channel, review_msg FROM logs WHERE status='pending' AND guild_id=? ORDER BY id DESC LIMIT 200", (i.guild.id,))
        rows = await cur.fetchall()
        
    if not rows:
        return await i.response.send_message(tr("no_pending"), ephemeral=True)
        
    if len(rows) <= 15:
        lines = [f"**#{lid}** {LOG_TYPES.get(t, {}).get('label', t)} — <@{author}> — {str(created)[:16]}{f' https://discord.com/channels/{i.guild.id}/{ch_id}/{msg_id}' if ch_id and msg_id else ''}" for lid, t, author, created, ch_id, msg_id in rows]
        await i.response.send_message("\n".join(lines)[:1990], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        view = PendingPaginator(rows, i.guild.id, i.user.id)
        await i.response.send_message(view.get_page_text()[:1990], view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
# ================================================================ الإعدادات والتحكم
@bot.tree.command(name="setchannel", description="Set channels / تحديد القنوات")
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

@bot.tree.command(name="setping", description="Set ping roles / تحديد رتب المنشن")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_PING)
async def setping(i: discord.Interaction, log_type: app_commands.Choice[str], kind: app_commands.Choice[str], role: Optional[discord.Role] = None):
    if not await gate(i, "setping", editor=True): return
    await cfg_set(i.guild.id, log_type.value, kind.value, role.id if role else None)
    await audit(f"⚙️ {i.user.mention} set {kind.value} ({log_type.value}) to {role.mention if role else 'none'}", i.guild)
    await i.response.send_message(f"✅ **{kind.name}** ({log_type.name}) = {role.mention if role else 'Cleared'}", ephemeral=True)

@bot.tree.command(name="setroles", description="Set role permissions / صلاحيات الرتب")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(kind=KIND_ROLES, log_type=LOG_TYPE_CHOICES, action=ACTIONS)
async def setroles(i: discord.Interaction, kind: app_commands.Choice[str], log_type: app_commands.Choice[str], action: app_commands.Choice[str], role: discord.Role):
    if not await gate(i, "setroles", editor=True): return
    await roles_edit(i.guild.id, log_type.value, kind.value, role.id, action.value == "add")
    await audit(f"⚙️ {i.user.mention} {action.value} {role.mention} → {kind.value} ({log_type.value})", i.guild)
    await i.response.send_message(f"✅ {action.name}: {role.mention} → **{kind.name}** ({log_type.name}).", ephemeral=True)

@bot.tree.command(name="setrule", description="Change a server rule / تغيير قاعدة في السيرفر")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(rule_name=RULE_CHOICES)
async def setrule(i: discord.Interaction, rule_name: app_commands.Choice[str], value: app_commands.Range[int, 0, 1000]):
    if not await gate(i, "setrule", editor=True): return
    await set_setting(i.guild.id, "rule:" + rule_name.value, value)
    await audit(f"⚙️ {i.user.mention} updated rule `{rule_name.value}` to `{value}`", i.guild)
    await i.response.send_message(f"✅ **{rule_name.value}** = {value}", ephemeral=True)

@bot.tree.command(name="settings", description="Show server settings / عرض الإعدادات والقواعد")
@app_commands.checks.cooldown(1, 5.0)
async def settings_cmd(i: discord.Interaction):
    if not await gate(i, "settings", editor=True): return
    g = i.guild.id
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT cmd_name, role_id FROM cmd_roles WHERE guild_id=?", (g,))
        cmd_rows = await cur.fetchall()
    embed = discord.Embed(title=f"⚙️ Server Settings / إعدادات السيرفر — {BOT_VERSION}", color=discord.Color.blue(), timestamp=now_utc())
    audit_ch = await cfg_get(g, "all", "audit_channel")
    embed.add_field(name="📍 Audit Channel / قناة التدقيق", value=f"<#{audit_ch}>" if audit_ch else "Not set / غير محدد", inline=False)
    ch_text = ""
    for t in LOG_TYPES:
        rv, lg = await cfg_get(g, t, "review_channel"), await cfg_get(g, t, "log_channel")
        ch_text += f"**{LOG_TYPES[t]['label']}**\nReview: {f'<#{rv}>' if rv else 'Not set'} | Log: {f'<#{lg}>' if lg else 'Not set'}\n"
    embed.add_field(name="📺 Channels / القنوات", value=ch_text[:1000] or "None", inline=False)
    rule_lines = [f"• `{k}`: {await rule(g, k)}" for k in RULE_LABELS]
    embed.add_field(name="📜 Rules / القواعد", value="\n".join(rule_lines)[:1000], inline=False)
    if cmd_rows:
        embed.add_field(name="🔒 Command role locks / قفل الأوامر", value="\n".join(f"• `/{c}` → <@&{r}>" for c, r in cmd_rows)[:1000], inline=False)
    await i.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="sync", description="Sync commands / مزامنة الأوامر")
@app_commands.checks.cooldown(1, 30.0)
async def sync_cmd(i: discord.Interaction):
    if not await gate(i, "sync", editor=True): return
    await i.response.defer(ephemeral=True)
    ok = await bot.sync_guild(i.guild)
    await i.followup.send(tr("sync_ok") if ok else tr("sync_fail"))

async def cmd_name_autocomplete(i: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return [app_commands.Choice(name=c, value=c) for c in ALL_BOT_COMMANDS if current.lower() in c.lower()][:25]

@bot.tree.command(name="set-cmd-perms", description="Control command permissions / التحكم في صلاحيات الأوامر")
@app_commands.autocomplete(command=cmd_name_autocomplete)
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(action=[
    app_commands.Choice(name="Add Role / إضافة رتبة", value="add_role"),
    app_commands.Choice(name="Remove Role / إزالة رتبة", value="remove_role"),
    app_commands.Choice(name="Add User / إضافة عضو", value="add_user"),
    app_commands.Choice(name="Remove User / إزالة عضو", value="remove_user"),
    app_commands.Choice(name="Clear All / مسح الكل", value="clear")
])
async def set_cmd_perms(i: discord.Interaction, command: str, action: app_commands.Choice[str], role: Optional[discord.Role] = None, member: Optional[discord.Member] = None):
    if not await gate(i, "set-cmd-perms", editor=True): return
    if command not in ALL_BOT_COMMANDS:
        return await i.response.send_message("❌ Invalid command / أمر غير صالح.", ephemeral=True)
    
    async with aiosqlite.connect(DB) as db:
        if action.value == "add_role" and role:
            await db.execute("INSERT INTO cmd_roles(guild_id, cmd_name, role_id) VALUES(?,?,?) ON CONFLICT(guild_id, cmd_name) DO UPDATE SET role_id=excluded.role_id", (i.guild.id, command, role.id))
            msg = f"🔒 `/{command}` locked to role {role.mention}. / تم قفل الأمر لرتبة {role.mention}."
        elif action.value == "remove_role":
            await db.execute("DELETE FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
            msg = f"🔓 `/{command}` role lock removed. / تم إزالة قفل الرتبة."
        elif action.value == "add_user" and member:
            await db.execute("INSERT OR IGNORE INTO cmd_users(guild_id, cmd_name, user_id) VALUES(?,?,?)", (i.guild.id, command, member.id))
            msg = f"🔒 `/{command}` locked to user {member.mention}. / تم قفل الأمر للعضو {member.mention}."
        elif action.value == "remove_user" and member:
            await db.execute("DELETE FROM cmd_users WHERE guild_id=? AND cmd_name=? AND user_id=?", (i.guild.id, command, member.id))
            msg = f"🔓 `/{command}` user lock removed for {member.mention}. / تم إزالة قفل العضو."
        elif action.value == "clear":
            await db.execute("DELETE FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
            await db.execute("DELETE FROM cmd_users WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
            msg = f"🔓 `/{command}` is now fully open. / الأمر مفتوح الآن."
        else:
            return await i.response.send_message("❌ Please provide a role or member based on your choice. / يرجى تحديد رتبة أو عضو.", ephemeral=True)
        await db.commit()
    await save_config_snapshot_json()
    await audit(f"⚙️ {i.user.mention} updated permissions for `/{command}`", i.guild)
    await i.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="set-update-ping", description="Set role pinged on updates / رتبة منشن التحديثات")
@app_commands.checks.cooldown(1, 5.0)
async def set_update_ping(i: discord.Interaction, role: Optional[discord.Role] = None):
    if not await gate(i, "set-update-ping", editor=True): return
    await cfg_set(i.guild.id, "all", "update_ping", role.id if role else None)
    await audit(f"⚙️ {i.user.mention} set update ping role to {role.mention if role else 'None'}", i.guild)
    await i.response.send_message(f"✅ Update ping role set to {role.mention}." if role else "🗑️ Update ping role cleared.", ephemeral=True)

@bot.tree.command(name="set-log-template", description="Customize a log layout / تخصيص شكل اللوج")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(log_type=LOG_TYPE_CHOICES[1:])
async def set_log_template(i: discord.Interaction, log_type: app_commands.Choice[str], template: Optional[app_commands.Range[str, 1, 1200]] = None):
    if not await gate(i, "set-log-template", editor=True): return
    key = f"template:{log_type.value}"
    
    if template:
        await set_setting(i.guild.id, key, template)
        await audit(f"🎨 {i.user.mention} updated the log template for **{log_type.name}**", i.guild)
        await i.response.send_message(f"✅ Template updated for **{log_type.name}**. / تم تحديث قالب اللوج.", ephemeral=True)
    else:
        current_template = await get_custom_template(i.guild.id, log_type.value)
        msg = (
            f"📝 **Current Template for {log_type.name}:**\n"
            f"```\n{current_template}\n```\n"
            f"*Copy the code above, edit it, then use `/set-log-template` with your new template to save it.*\n"
            f"*(You can use variables like `{{MOON}}`, `{{ARROW}}`, `{{CROWN}}`, `{{DIVIDER}}`, `{{author}}`, `{{score}}`, `{{result}}`, etc.)*"
        )
        await i.response.send_message(msg[:1990], ephemeral=True)

# ================================================================ النقط والترتيب
@bot.tree.command(name="set-log-points", description="Set log points / تحديد نقاط اللوجات")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(log=POINT_CHOICES)
async def set_log_points(i: discord.Interaction, log: app_commands.Choice[str], points: app_commands.Range[int, 0, 1000]):
    if not await gate(i, "set-log-points", editor=True): return
    await set_setting(i.guild.id, "points:" + log.value, points)
    await audit(f"⚙️ {i.user.mention} set log points for **{log.name}** to **{points}**", i.guild)
    await i.response.send_message(f"✅ **{log.name}** = {points} points (new logs only). / تم تحديد النقاط.", ephemeral=True)

@bot.tree.command(name="set-rank-points", description="Set rank threshold / نقاط رتب السلم")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(rank=RANK_CHOICES)
async def set_rank_points(i: discord.Interaction, rank: app_commands.Choice[str], points: app_commands.Range[int, 0, 100000]):
    if not await gate(i, "set-rank-points", editor=True): return
    ladder = await get_ladder(i.guild.id)
    idx = next(k for k, (n, _) in enumerate(ladder) if n == rank.value)
    prev_pts = ladder[idx - 1][1] if idx > 0 else 0
    next_pts = ladder[idx + 1][1] if idx < len(ladder) - 1 else None
    if (idx > 0 and points <= prev_pts) or (next_pts is not None and points >= next_pts):
        return await i.response.send_message(f"❌ Number must be > {prev_pts} and < {next_pts if next_pts is not None else '∞'}. / الرقم غير صالح.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    await set_setting(i.guild.id, "rank:" + rank.value, points)
    changed = await bulk_update_ranks(i.guild)
    await audit(f"⚙️ {i.user.mention} set rank **{rank.name}** to **{points}** ({changed} promotions)", i.guild)
    await i.followup.send(f"✅ **{rank.name}** = {points} points. Promotions applied: {changed}. / تم التحديث.", ephemeral=True)

@bot.tree.command(name="set-points", description="Set member points / تحديد نقاط عضو")
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
            if delta != 0:
                await db.execute("INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?,?)", (i.guild.id, None, member.id, delta, now_utc().isoformat(), "manual", season))
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"❌ set-points DB error: {e}")
            return await i.followup.send(tr("db_err"), ephemeral=True)
    await update_member_rank(i.guild, member, points)
    await audit(f"⚙️ {i.user.mention} set points of {member.mention} to **{points}** (Δ {delta:+d})", i.guild)
    note = ""
    if await rank_is_above_points(i.guild, member, points):
        note = "\n⚠️ Member holds a rank above their points. Use `/sync-ranks demote:True` to adjust. / العضو رتبته أعلى من نقاطه."
    await i.followup.send(f"✅ {member.display_name} = {points} points.{note}", ephemeral=True)

async def ledger_mismatches(guild_id: int):
    season = await get_current_season(guild_id)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, SUM(points) FROM point_events WHERE guild_id=? AND season_id=? GROUP BY user_id", (guild_id, season))
        ledger = {u: p for u, p in await cur.fetchall()}
        cur = await db.execute("SELECT user_id, points FROM members WHERE guild_id=?", (guild_id,))
        cached = {u: p for u, p in await cur.fetchall()}
    return [(u, cached.get(u, 0), ledger.get(u, 0)) for u in sorted(set(ledger) | set(cached)) if cached.get(u, 0) != ledger.get(u, 0)]

@bot.tree.command(name="verify-points", description="Check balances against ledger / فحص الرصيد مقابل السجل")
@app_commands.checks.cooldown(1, 30.0)
@app_commands.describe(fix="Overwrite balances with ledger totals / تصحيح الأرصدة", confirm="Type FIX when repairing > 5 balances / اكتب FIX للتأكيد")
async def verify_points(i: discord.Interaction, fix: bool = False, confirm: Optional[str] = None):
    if not await gate(i, "verify-points", editor=True): return
    await i.response.defer(ephemeral=True)
    bad = await ledger_mismatches(i.guild.id)
    if not bad:
        return await i.followup.send("✅ All balances match the points ledger. / كل الأرصدة متطابقة.", ephemeral=True)
    if fix:
        if len(bad) > 5 and confirm != "FIX":
            return await i.followup.send(f"⚠️ Found **{len(bad)}** mismatches. To repair more than 5 at once, add `confirm:FIX`. / يرجى التأكيد.", ephemeral=True)
        async with aiosqlite.connect(DB) as db:
            for uid, _, led in bad:
                await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points=excluded.points", (i.guild.id, uid, led))
            await db.commit()
        await audit(f"🛠️ {i.user.mention} repaired {len(bad)} balances from the ledger", i.guild)
    lines = [f"<@{u}>: cached {c} / ledger {l}" for u, c, l in bad[:15]]
    await i.followup.send(f"{'🛠️ Fixed' if fix else '⚠️ Found'} {len(bad)} mismatch(es):\n" + "\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name="sync-ranks", description="Re-check ranks from points / إعادة فحص الرتب حسب النقاط")
@app_commands.checks.cooldown(1, 60.0)
@app_commands.describe(member="One member (empty = everyone) / عضو محدد", demote="Also REMOVE ranks above points / سحب الرتب الأعلى")
async def sync_ranks(i: discord.Interaction, member: Optional[discord.Member] = None, demote: bool = False):
    if not await gate(i, "sync-ranks", editor=True): return
    await i.response.defer(ephemeral=True)
    if member:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
            row = await cur.fetchone()
        changed = 1 if await update_member_rank(i.guild, member, row[0] if row else 0, demote) else 0
    else:
        changed = await bulk_update_ranks(i.guild, allow_demote=demote)
    await audit(f"⚙️ {i.user.mention} ran sync-ranks (demote={demote}), changes: {changed}", i.guild)
    await i.followup.send(f"✅ Done. Rank changes: {changed}. / تم التحديث.", ephemeral=True)

# ================================================================ المواسم والنسخ الاحتياطي
@bot.tree.command(name="season-reset", description="Archive points & start new season / بدء موسم جديد")
@app_commands.checks.cooldown(1, 60.0)
@app_commands.describe(name="Season name / اسم الموسم", confirm="Type RESET / اكتب RESET للتأكيد", reset_ranks="Also remove all Lvl roles / مسح الرتب")
async def season_reset(i: discord.Interaction, name: app_commands.Range[str, 1, 50], confirm: str, reset_ranks: bool = False):
    if not await gate(i, "season-reset", editor=True): return
    if confirm != "RESET":
        return await i.response.send_message("❌ Type RESET (capital letters) in `confirm` to proceed. / اكتب RESET للمتابعة.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    name = name.strip()
    new_season_id = f"season_{uuid.uuid4().hex[:6]}"
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT 1 FROM season_archive WHERE season=? AND guild_id=? LIMIT 1", (name, i.guild.id))
            if await cur.fetchone():
                await db.rollback()
                return await i.followup.send(f"❌ Season name **{name}** already exists. / اسم الموسم موجود بالفعل.", ephemeral=True)
            await db.execute("INSERT INTO season_archive SELECT ?, guild_id, user_id, points FROM members WHERE guild_id=? AND points>0", (name, i.guild.id))
            await db.execute("UPDATE members SET points=0 WHERE guild_id=?", (i.guild.id,))
            await db.execute("UPDATE logs SET status='rejected', reason='Season ended before review', reviewed_at=? WHERE guild_id=? AND status='pending'", (now_utc().isoformat(), i.guild.id))
            await db.execute("INSERT INTO settings(guild_id, key, value) VALUES(?, 'state:current_season', ?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (i.guild.id, new_season_id))
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"❌ season reset DB error: {e}")
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
                except discord.HTTPException:
                    pass
                await asyncio.sleep(0.3)
    await audit(f"🎉 {i.user.mention} started season **{name}** (`{new_season_id}`).", i.guild)
    await i.followup.send(f"✅ Season **{name}** started. Points archived and reset to 0." + (f" Ranks removed from {stripped} members." if reset_ranks else "") + " / تم بدء الموسم الجديد.", ephemeral=True)

@bot.tree.command(name="season-top", description="Top 10 of an archived season / متصدري موسم سابق")
@app_commands.checks.cooldown(1, 10.0)
async def season_top(i: discord.Interaction, name: app_commands.Range[str, 1, 50]):
    if not await gate(i, "season-top"): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM season_archive WHERE season=? AND guild_id=? ORDER BY points DESC LIMIT 10", (name.strip(), i.guild.id))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("ℹ️ Season archive not found. / أرشيف الموسم غير موجود.", ephemeral=True)
    text = f"🏆 **Archived Season: {name}**\n" + "\n".join(f"**{n}.** <@{u}> — {p}" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())

def _backup_file(path):
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

@bot.tree.command(name="backup", description="Secure backup tool / أداة النسخ الاحتياطي الآمنة")
@app_commands.checks.cooldown(1, 60.0)
@app_commands.choices(scope=BACKUP_SCOPE_CHOICES, destination=BACKUP_DEST_CHOICES)
async def backup_cmd(i: discord.Interaction, scope: app_commands.Choice[str], destination: app_commands.Choice[str]):
    if not await gate(i, "backup", editor=True): return
    if scope.value == "full":
        if not owner_id() or i.user.id != owner_id():
            return await i.response.send_message("❌ Full database backups are for the bot owner only. / هذا الأمر لصاحب البوت فقط.", ephemeral=True)
        if destination.value != "owner":
            return await i.response.send_message("❌ Full database backups can only be sent to the owner's DM. / يتم إرسالها لخاص المالك فقط.", ephemeral=True)
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
            ok = await dm_owner(f"🗄️ Backup (`{scope.name}`) from **{i.guild.name}** requested by {i.user.mention}:", file=file_obj)
            await audit(f"🗄️ {i.user.mention} exported a backup (`{scope.value}`) to the owner's DM.", i.guild)
            await i.followup.send("✅ Backup sent to the owner's DM. / تم إرسال النسخة لخاص المالك." if ok else "❌ Couldn't DM the owner. / فشل الإرسال.", ephemeral=True)
        else:
            await audit(f"🗄️ {i.user.mention} exported a guild backup (`{scope.value}`) in chat.", i.guild)
            await i.followup.send(f"🗄️ Backup ({scope.name}):", file=file_obj, ephemeral=True)
    except Exception as e:
        print(f"❌ Backup error: {e}")
        await i.followup.send("❌ Backup failed. / فشل النسخ الاحتياطي.", ephemeral=True)
    finally:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass

CFG_KEYS = {"review_channel", "log_channel", "audit_channel", "review_ping", "log_ping", "update_ping", "update_channel"}
SETTING_PREFIXES = ("points:", "rank:", "rule:", "template:")

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)

async def restore_settings_json(guild_id: int, raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("Not a valid JSON file. / الملف غير صالح.")
    if not isinstance(data, dict):
        raise ValueError("Unexpected file structure. / هيكل الملف غير متوقع.")
    file_version = data.get("version")
    if file_version and file_version != BOT_VERSION:
        print(f"⚠️ Restoring settings from version {file_version} into {BOT_VERSION} — schema may differ.")
    valid_types = set(LOG_TYPES) | {"all"}
    counts = {"settings": 0, "guild_cfg": 0, "role_perms": 0, "cmd_roles": 0, "skipped": 0}

    def mine(r):
        return isinstance(r, dict) and r.get("guild_id") == guild_id

    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for r in data.get("settings", []):
                k, v = (r.get("key"), r.get("value")) if isinstance(r, dict) else (None, None)
                if mine(r) and isinstance(k, str) and k.startswith(SETTING_PREFIXES) and len(k) <= 100 and v is not None and len(str(v)) <= 2000:
                    await db.execute("INSERT INTO settings(guild_id,key,value) VALUES(?,?,?) ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value", (guild_id, k, str(v)))
                    counts["settings"] += 1
                else:
                    counts["skipped"] += 1
            for r in data.get("guild_cfg", []):
                if mine(r) and r.get("log_type") in valid_types and r.get("key") in CFG_KEYS and _is_int(r.get("value")):
                    await db.execute("INSERT INTO guild_cfg VALUES(?,?,?,?) ON CONFLICT(guild_id,log_type,key) DO UPDATE SET value=excluded.value", (guild_id, r["log_type"], r["key"], r["value"]))
                    counts["guild_cfg"] += 1
                else:
                    counts["skipped"] += 1
            for r in data.get("role_perms", []):
                if mine(r) and r.get("log_type") in valid_types and r.get("kind") in ("review", "submit") and _is_int(r.get("role_id")):
                    await db.execute("INSERT OR IGNORE INTO role_perms VALUES(?,?,?,?)", (guild_id, r["log_type"], r["kind"], r["role_id"]))
                    counts["role_perms"] += 1
                else:
                    counts["skipped"] += 1
            for r in data.get("cmd_roles", []):
                if mine(r) and r.get("cmd_name") in ALL_BOT_COMMANDS and _is_int(r.get("role_id")):
                    await db.execute("INSERT INTO cmd_roles VALUES(?,?,?) ON CONFLICT(guild_id,cmd_name) DO UPDATE SET role_id=excluded.role_id", (guild_id, r["cmd_name"], r["role_id"]))
                    counts["cmd_roles"] += 1
                else:
                    counts["skipped"] += 1
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    await save_config_snapshot_json()
    return counts

def _validate_sqlite(path):
    con = sqlite3.connect(path)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()
    required = {
        "settings", "members", "logs", "hashes", "guild_cfg", "role_perms",
        "point_events", "cmd_roles", "cmd_users", "crews", "tickets",
        "shop_items", "achievements", "quests", "events"
    }
    return ok and required.issubset(tables)

def _restore_into_live(src_path):
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(DB)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

async def restore_database(raw: bytes):
    tmp = os.path.join(tempfile.gettempdir(), f"restore-{uuid.uuid4().hex[:8]}.db")
    safety = os.path.join(tempfile.gettempdir(), f"pre-restore-{uuid.uuid4().hex[:8]}.db")
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        if not await asyncio.to_thread(_validate_sqlite, tmp):
            raise ValueError("The file is not a valid DragonSlayerBot database. / الملف ليس قاعدة بيانات صالحة.")
        await asyncio.to_thread(_backup_file, safety)
        try:
            if not await asyncio.to_thread(_validate_sqlite, safety):
                raise RuntimeError("Could not create a valid pre-restore safety backup.")
            await asyncio.to_thread(_restore_into_live, tmp)
            await init_db()
            return True
        except Exception:
            if os.path.exists(safety) and await asyncio.to_thread(_validate_sqlite, safety):
                await asyncio.to_thread(_restore_into_live, safety)
                await init_db()
            raise
    finally:
        for path in (tmp, safety):
            if os.path.exists(path):
                os.unlink(path)

@bot.tree.command(name="restore-backup", description="Restore settings (.json) or database (.db) / استعادة نسخة احتياطية")
@app_commands.checks.cooldown(1, 60.0)
@app_commands.describe(backup_file="Backup file / الملف", confirm="Type RESTORE to restore .db / اكتب RESTORE")
async def restore_backup(i: discord.Interaction, backup_file: discord.Attachment, confirm: Optional[str] = None):
    if not await gate(i, "restore-backup", editor=True): return
    ext = os.path.splitext(backup_file.filename)[1].lower()
    if ext not in (".json", ".db"):
        return await i.response.send_message("❌ Only `.json` or `.db` files are accepted. / يرجى استخدام ملفات json أو db.", ephemeral=True)
    if ext == ".db":
        if not owner_id() or i.user.id != owner_id():
            return await i.response.send_message("❌ Restoring a database file is for the bot owner only. / هذا الأمر لصاحب البوت فقط.", ephemeral=True)
        if confirm != "RESTORE":
            return await i.response.send_message("⚠️ This REPLACES all data. Type `RESTORE` in `confirm`. / هذا سيستبدل كل البيانات.", ephemeral=True)
    if backup_file.size > (2 * 1024 * 1024 if ext == ".json" else MAX_FILE_SIZE):
        return await i.response.send_message("❌ File is too large. / الملف كبير جداً.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    raw = await backup_file.read()
    try:
        if ext == ".json":
            c = await restore_settings_json(i.guild.id, raw)
            await audit(f"📥 {i.user.mention} restored settings from a JSON backup ({c}).", i.guild)
            await i.followup.send(f"✅ Settings restored: {c['settings']} settings, {c['guild_cfg']} channels, {c['role_perms']} role perms, {c['cmd_roles']} cmd locks. / تم الاستعادة.", ephemeral=True)
        else:
            await restore_database(raw)
            await audit(f"📥 {i.user.mention} restored the full database from a .db backup.", i.guild)
            await i.followup.send("✅ Database restored and migrated. / تم استعادة قاعدة البيانات.", ephemeral=True)
    except ValueError as e:
        await i.followup.send(f"❌ {e}", ephemeral=True)
    except Exception as e:
        print(f"❌ Restore error: {e}")
        await i.followup.send("❌ Restore failed. / فشلت الاستعادة.", ephemeral=True)

# ================================================================ التحديثات (Updates)
async def post_update_notice(guild, target, preset_key="bot_patch", replace_previous=True):
    data = UPDATE_PRESETS[preset_key]
    ping_role_id = await cfg_get(guild.id, "all", "update_ping")
    embed = discord.Embed(title=f"📢 {data['title_en']} | {data['title_ar']}", color=discord.Color.gold(), timestamp=now_utc())
    embed.add_field(name="🇬🇧 English", value=data["desc_en"][:1000], inline=False)
    embed.add_field(name="🇸🇦 بالعربي", value=data["desc_ar"][:1000], inline=False)
    embed.set_footer(text=f"DragonSlayerBot {BOT_VERSION}")
    
    sent = await target.send(content=f"<@&{ping_role_id}>" if ping_role_id else None, embed=embed, allowed_mentions=mentions(ping_role_id))
    key = f"state:last_update:{target.id}"
    prev = await raw_setting(guild.id, key)
    await set_setting(guild.id, key, sent.id)
    
    if replace_previous and prev:
        try:
            old = await target.fetch_message(int(prev))
            if old.author.id == getattr(bot.user, "id", None):
                await old.delete()
        except (discord.HTTPException, ValueError, AttributeError):
            pass
    return sent

async def check_and_notify_auto_update(guild: discord.Guild):
    if await raw_setting(guild.id, "state:bot_version") == BOT_VERSION:
        return
    ch_id = (await cfg_get(guild.id, "all", "update_channel") or await cfg_get(guild.id, "all", "log_channel"))
    ch = guild.get_channel(ch_id) if ch_id else None
    if not ch:
        return
    try:
        await post_update_notice(guild, ch)
        await set_setting(guild.id, "state:bot_version", BOT_VERSION)
    except discord.HTTPException:
        pass

@bot.tree.command(name="log-update", description="Publish an update notice / نشر إشعار تحديث")
@app_commands.checks.cooldown(1, 10.0)
@app_commands.choices(preset=UPDATE_TYPE_CHOICES)
@app_commands.describe(preset="Update type / نوع التحديث", channel="Target channel / القناة", replace_previous="Delete previous notice / حذف الإشعار السابق")
async def log_update(i: discord.Interaction, preset: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None, replace_previous: bool = True):
    if not await gate(i, "log-update", editor=True): return
    target = channel
    if not target:
        ch_id = await cfg_get(i.guild.id, "all", "update_channel") or await cfg_get(i.guild.id, "all", "log_channel")
        target = i.guild.get_channel(ch_id) if ch_id else None
    if preset.value not in UPDATE_PRESETS or not target:
        return await i.response.send_message("❌ Set an update channel first with `/setchannel`. / يرجى تحديد قناة التحديثات أولاً.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    try:
        await post_update_notice(i.guild, target, preset.value, replace_previous)
        await audit(f"📢 {i.user.mention} posted update **{preset.name}** in {target.mention}", i.guild)
        await i.followup.send(f"✅ Update published to {target.mention}. / تم نشر التحديث.", ephemeral=True)
    except discord.HTTPException as e:
        await i.followup.send(f"❌ Failed to send the update: {e}", ephemeral=True)

# ================================================================ الإشعارات الذكية (مع patch 7)
@bot.tree.command(name="set-notification", description="Set where notifications go / تحديد مكان إرسال الإشعارات")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(event_type=[app_commands.Choice(name="Auto-Quests / المهام اليومية", value="quests"), app_commands.Choice(name="Daily Bonus / المكافأة اليومية", value="daily")], mode=[app_commands.Choice(name="Channel / قناة عامة", value="channel"), app_commands.Choice(name="DM / رسالة خاصة", value="dm")])
async def set_notification(i: discord.Interaction, event_type: app_commands.Choice[str], mode: app_commands.Choice[str], target: str):
    if not await gate(i, "set-notification", editor=True): return
    
    # [PATCH 7] try/except لحماية من input غير صالح
    target_id = 0
    try:
        if target.startswith("<#") and target.endswith(">"):
            target_id = int(target[2:-1])
        elif target.startswith("<@&") and target.endswith(">"):
            target_id = int(target[3:-1])
        elif target.isdigit():
            target_id = int(target)
        else:
            return await i.response.send_message("❌ Mention a channel/role, or use ID. / يرجى منشن القناة أو الرتبة.", ephemeral=True)
    except (ValueError, IndexError):
        return await i.response.send_message("❌ Invalid target format. / صيغة غير صالحة.", ephemeral=True)
    
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO notif_settings(guild_id, event_type, mode, target_id) VALUES(?,?,?,?) ON CONFLICT(guild_id, event_type) DO UPDATE SET mode=excluded.mode, target_id=excluded.target_id", (i.guild.id, event_type.value, mode.value, target_id))
        await db.commit()
    await i.response.send_message(f"✅ Notification **{event_type.name}** set to **{mode.name}**. / تم الضبط.", ephemeral=True)
# ================================================================ المساعدة (Help) - Bilingual
HELP_DATA = [
    ("📜 Part 1 — Logging Activity / تسجيل النشاط", discord.Color.gold(), [
        ("/log-war", "Log a Crew War (min 2 files) / تسجيل حرب كرو (ملفين على الأقل)."),
        ("/log-1v1", "Log a 1v1 fight / تسجيل فايت 1 ضد 1."),
        ("/log-2v2", "Log a 2v2 fight / تسجيل فايت 2 ضد 2."),
        ("/log-training", "Log a training session / تسجيل تدريب داخلي."),
        ("/log-tryout", "Log a tryout / تسجيل تراي أوت."),
        ("/resubmit", "Fix and resend a rejected log / إعادة إرسال لوج مرفوض."),
        ("/my-logs", "View your latest logs / عرض لوجاتك."),
        ("/log-info", "View details of one log / تفاصيل لوج برقمه."),
    ]),
    ("🛡️ Part 2 — Review & Moderation / المراجعة والإدارة", discord.Color.blue(), [
        ("/pending", "List logs waiting for review / اللوجات المنتظرة."),
        ("/revoke-log", "Cancel an accepted log / إلغاء لوج مقبول."),
        ("/reviewer-stats", "See how many logs each reviewer handled / إحصائيات المراجعين."),
        ("/inactive", "Crew members with no activity for N days / الأعضاء غير النشطين."),
        ("/season-reset", "Archive points and start a new season / بدء موسم جديد."),
        ("/season-top", "Top 10 of an archived season / أفضل 10 في موسم."),
        ("/verify-points", "Check balances against the points ledger / فحص الأرصدة."),
        ("/backup", "Export backup / نسخة احتياطية."),
        ("/restore-backup", "Restore settings (.json) or database (.db) / استعادة نسخة."),
    ]),
    ("⚙️ Part 3 — Configuration / الإعدادات", discord.Color.green(), [
        ("/setchannel", "Set review / log / audit / update channels / تحديد القنوات."),
        ("/setping", "Set roles to ping on review and public logs / تحديد المنشن."),
        ("/setroles", "Set who can review and submit each log type / صلاحيات الرتب."),
        ("/setrule", "Configure daily limits, reminders, etc / قوانين السيرفر."),
        ("/set-log-points", "Set points awarded per log type / نقاط اللوجات."),
        ("/set-rank-points", "Set points required for each rank / نقاط الرتب."),
        ("/set-points", "Manually set a member's point balance / تعديل نقاط عضو."),
        ("/sync-ranks", "Re-check ranks based on points / مزامنة الرتب."),
        ("/set-cmd-perms", "Lock any command to a specific role or user / قفل أمر برتبة أو عضو."),
        ("/grant-temp-admin", "Give temporary admin permissions / إعطاء صلاحيات مؤقتة."),
        ("/revoke-temp-admin", "Remove temporary admin permissions / سحب الصلاحيات المؤقتة."),
        ("/set-log-template", "Customize log layout / تخصيص تصميم اللوج."),
        ("/create-new-crew", "Create a crew with leaders and divisions / إنشاء طاقم."),
        ("/crews-delete-all", "Delete all registered crews / حذف الطواقم."),
        ("/settings", "Show current server settings / عرض الإعدادات."),
        ("/sync", "Re-sync slash commands / مزامنة الأوامر."),
        ("/send-help-panel", "Post this manual in a channel / نشر الدليل."),
    ]),
    ("🏆 Part 4 — Points & Public / النقاط والملف الشخصي", discord.Color.purple(), [
        ("/points", "Check your points and rank progress bar / رصيد النقاط."),
        ("/leaderboard", "Top members (all time, season, or this week) / المتصدرين."),
        ("/profile", "View member profile card / بطاقتك الشخصية."),
        ("/rates", "View points per log, ranks, and rules / الأسعار."),
        ("/link", "Link your Roblox username / ربط روبلوكس."),
        ("/stats", "War record against other crews / إحصائيات الحروب."),
        ("/crews-list", "Browse crews and their divisions / الطواقم."),
        ("/set-bio", "Set a bio for your profile / كتابة سيرتك الذاتية."),
        ("/help", "Show this manual / عرض دليل الأوامر."),
    ]),
    ("🎮 Part 5 — Systems & Games / الأنظمة والألعاب", discord.Color.teal(), [
        ("/streak", "Check your daily login streak / عرض الستريك."),
        ("/shop", "Point shop / متجر النقاط."),
        ("/buy", "Buy item from the shop / شراء منتج."),
        ("/achievements", "Achievements list / الإنجازات."),
        ("/claim-achievement", "Claim an unlocked achievement reward / استلام مكافأة إنجاز."),
        ("/quests", "Daily quests / المهام اليومية."),
        ("/event", "Create an event (Admin) / إنشاء فعالية."),
        ("/start-vote", "Start MVP vote (Admin) / بدء تصويت."),
        ("/h2h", "Head-to-head stats between two members / مواجهات مباشرة."),
        ("/lfg", "Looking for group (Channel or DM) / البحث عن فريق."),
        ("/daily", "Claim your daily bonus / المكافأة اليومية."),
        ("/setup-tickets", "Setup support tickets / إنشاء تذاكر."),
        ("/create-temp-voice", "Create a temporary voice channel / غرفة صوتية مؤقتة."),
        ("/purge", "Purge messages in a channel (Admin) / مسح الرسائل."),
        ("/embed", "Embed builder (Admin) / صانع الرسائل."),
        ("/customize-msg", "Customize bot messages (Admin) / تخصيص الرسائل."),
        ("/create-custom-role", "Create a custom color role (Members) / إنشاء رتبة مخصصة."),
        ("/set-notification", "Set where notifications go (Admin) / ضبط الإشعارات."),
        ("/set-lfg-roles", "Set allowed roles for LFG (Admin) / رتب البحث عن فريق."),
        ("/set-top-rewards", "Set weekly top 3 rewards (Admin) / جوائز المتصدرين."),
    ])
]

def get_help_embeds():
    embeds = []
    for title, color, items in HELP_DATA:
        e = discord.Embed(title=f"{title} ({BOT_VERSION})", color=color)
        for cmd, desc in items:
            e.add_field(name=f"`{cmd}`", value=desc, inline=False)
        e.set_footer(text="DragonSlayerBot • Bilingual")
        embeds.append(e)
    return embeds

@bot.tree.command(name="help", description="Show the bot manual / عرض دليل الأوامر")
@app_commands.checks.cooldown(1, 5.0)
async def help_cmd(i: discord.Interaction):
    if not await gate(i, "help"): return
    await i.response.defer(ephemeral=True)
    embeds = get_help_embeds()
    if len(embeds) <= 10:
        await i.followup.send(embeds=embeds, ephemeral=True)
    else:
        for e in embeds[:10]:
            await i.followup.send(embed=e, ephemeral=True)

@bot.tree.command(name="send-help-panel", description="Post the manual in a channel / إرسال الشرح في قناة")
@app_commands.checks.cooldown(1, 10.0)
async def send_help_panel(i: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not await gate(i, "send-help-panel", editor=True): return
    target = channel or i.channel
    try:
        for e in get_help_embeds(): await target.send(embed=e)
        await i.response.send_message(f"✅ Posted the manual to {target.mention}. / تم النشر.", ephemeral=True)
    except discord.HTTPException as e:
        await i.response.send_message(f"❌ Failed to send the manual: {e}", ephemeral=True)

# ================================================================ الأنظمة الإضافية
@bot.tree.command(name="customize-msg", description="Customize bot messages / تخصيص شكل أي رسالة في البوت")
@app_commands.checks.cooldown(1, 5.0)
async def customize_msg(i: discord.Interaction, key: str, title: str, desc: str, color: Optional[str] = None):
    if not await gate(i, "customize-msg", editor=True): return
    if len(title) > 256 or len(desc) > 4000:
        return await i.response.send_message("❌ Title or description is too long. / النص طويل جداً.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO custom_messages(guild_id, key, title, description, color) VALUES(?,?,?,?,?) ON CONFLICT(guild_id, key) DO UPDATE SET title=excluded.title, description=excluded.description, color=excluded.color", (i.guild.id, key, title, desc, color.replace("#", "") if color else None))
        await db.commit()
    await i.response.send_message(f"✅ Customized message `{key}` / تم تخصيص الرسالة.", ephemeral=True)

@bot.tree.command(name="set-custom-reward", description="Set custom rewards for any system / تحديد الرتبة والنقاط لأي نظام")
@app_commands.checks.cooldown(1, 5.0)
async def set_custom_reward(i: discord.Interaction, system_key: str, points: int, role: Optional[discord.Role] = None):
    if not await gate(i, "set-custom-reward", editor=True): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO custom_settings(guild_id, key, value) VALUES(?,?,?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (i.guild.id, f"{system_key}_points", str(points)))
        if role:
            await db.execute("INSERT INTO custom_settings(guild_id, key, value) VALUES(?,?,?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (i.guild.id, f"{system_key}_role", str(role.id)))
        await db.commit()
    await i.response.send_message(f"✅ Saved reward for `{system_key}`: {points} points" + (f" and {role.mention}" if role else "") + " / تم الحفظ.", ephemeral=True)

@bot.tree.command(name="streak", description="Check your daily login streak / عرض الستريك")
@app_commands.checks.cooldown(1, 5.0)
async def streak_cmd(i: discord.Interaction):
    if not await gate(i, "streak"): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT current_streak, max_streak FROM streaks WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
        row = await cur.fetchone()
    current, max_s = (row[0], row[1]) if row else (0, 0)
    embed = await get_custom_msg(i.guild.id, "streak_msg", tr("streak_title"), tr("streak_desc", current=current, max=max_s), discord.Color.orange())
    await i.response.send_message(embed=embed, ephemeral=True)

class ShopPaginator(discord.ui.View):
    def __init__(self, items, requester_id):
        super().__init__(timeout=120)
        self.items, self.requester_id = items, requester_id
        self.page, self.per_page = 0, 5
    def max_pages(self): return max(1, (len(self.items) - 1) // self.per_page + 1)
    def get_embed(self):
        chunk = self.items[self.page * self.per_page:(self.page + 1) * self.per_page]
        desc = tr("shop_desc") + "\n\n"
        for name, price, d in chunk: desc += f"🛒 **{name}** — {price} points\n┗ {d}\n\n"
        embed = discord.Embed(title=tr("shop_title"), description=desc, color=discord.Color.gold())
        embed.set_footer(text=tr("browse_page", page=self.page + 1, pages=self.max_pages()))
        return embed
    async def interaction_check(self, i: discord.Interaction) -> bool: return i.user.id == self.requester_id
    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, i: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await i.response.edit_message(embed=self.get_embed(), view=self)
    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, i: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_pages() - 1, self.page + 1)
        await i.response.edit_message(embed=self.get_embed(), view=self)

@bot.tree.command(name="add-shop-item", description="Add shop item / إضافة منتج للمتجر")
@app_commands.checks.cooldown(1, 5.0)
async def add_shop_item(i: discord.Interaction, name: str, price: int, role: discord.Role, description: str):
    if not await gate(i, "add-shop-item", editor=True): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO shop_items(guild_id, name, price, role_id, description) VALUES(?,?,?,?,?)", (i.guild.id, name, price, role.id, description))
        await db.commit()
    await i.response.send_message(f"✅ Added **{name}** to shop. / تم إضافة المنتج.", ephemeral=True)

@bot.tree.command(name="shop", description="View point shop / عرض متجر النقاط")
@app_commands.checks.cooldown(1, 5.0)
async def shop_cmd(i: discord.Interaction):
    if not await gate(i, "shop"): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT name, price, description FROM shop_items WHERE guild_id=?", (i.guild.id,))
        items = await cur.fetchall()
    if not items: return await i.response.send_message(tr("shop_empty"), ephemeral=True)
    view = ShopPaginator(items, i.user.id)
    await i.response.send_message(embed=view.get_embed(), view=view, ephemeral=True)

async def item_autocomplete(i: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT name FROM shop_items WHERE guild_id=? AND name LIKE ?", (i.guild.id, f"%{current}%"))
        rows = await cur.fetchall()
    return [app_commands.Choice(name=r[0], value=r[0]) for r in rows][:25]

@bot.tree.command(name="buy", description="Buy from shop / شراء منتج")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.autocomplete(item_name=item_autocomplete)
async def buy_cmd(i: discord.Interaction, item_name: str):
    if not await gate(i, "buy"): return
    await i.response.defer(ephemeral=True)
    
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id, price, role_id FROM shop_items WHERE guild_id=? AND name=?", (i.guild.id, item_name))
        item = await cur.fetchone()
        if not item: return await i.followup.send("❌ Item not found. / المنتج غير موجود.", ephemeral=True)
        item_id, price, role_id = item
        
        role = i.guild.get_role(role_id)
        if role and role in i.user.roles:
            return await i.followup.send("❌ You already own this role. / أنت تمتلك هذه الرتبة بالفعل.", ephemeral=True)

        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("UPDATE members SET points = points - ? WHERE guild_id=? AND user_id=? AND points >= ?", (price, i.guild.id, i.user.id, price))
            if cur.rowcount == 0:
                await db.rollback()
                cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
                points = (await cur.fetchone() or [0])[0]
                return await i.followup.send(tr("shop_no_points", price=price, points=points), ephemeral=True)
                
            await db.execute("INSERT INTO point_events(guild_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?)", (i.guild.id, i.user.id, -price, now_utc().isoformat(), "shop", await get_current_season(i.guild.id)))
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"❌ buy DB error: {e}")
            return await i.followup.send(tr("db_err"), ephemeral=True)
            
    if role:
        try: 
            await i.user.add_roles(role, reason="Shop Purchase")
        except discord.Forbidden: 
            async with aiosqlite.connect(DB) as db:
                await db.execute("UPDATE members SET points = points + ? WHERE guild_id=? AND user_id=?", (price, i.guild.id, i.user.id))
                await db.execute("INSERT INTO point_events(guild_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?)", (i.guild.id, i.user.id, price, now_utc().isoformat(), "refund", await get_current_season(i.guild.id)))
                await db.commit()
            return await i.followup.send("❌ Failed to give role. Points refunded. / فشل إعطاء الرتبة. تم استرجاع نقاطك.", ephemeral=True)
            
    await i.followup.send(tr("shop_bought", item=item_name, price=price), ephemeral=True)

@bot.tree.command(name="add-achievement", description="Add achievement / إضافة إنجاز جديد")
@app_commands.checks.cooldown(1, 5.0)
async def add_achievement(i: discord.Interaction, name: str, description: str, required_points: int, reward_points: int, role: Optional[discord.Role] = None):
    if not await gate(i, "add-achievement", editor=True): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO achievements(guild_id, name, description, required_points, reward_points, role_id) VALUES(?,?,?,?,?,?)", (i.guild.id, name, description, required_points, reward_points, role.id if role else None))
        await db.commit()
    await i.response.send_message(f"✅ Added achievement **{name}** (Reward: {reward_points} pts). / تم إضافة الإنجاز.", ephemeral=True)

@bot.tree.command(name="achievements", description="View achievements / عرض الإنجازات")
@app_commands.checks.cooldown(1, 5.0)
async def achievements_cmd(i: discord.Interaction):
    if not await gate(i, "achievements"): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT name, description, required_points FROM achievements WHERE guild_id=?", (i.guild.id,))
        items = await cur.fetchall()
    if not items: return await i.response.send_message("⚠️ No achievements yet. / لا توجد إنجازات حالياً.", ephemeral=True)
    desc = "".join(f"🏅 **{name}** ({req} pts)\n┗ {d}\n\n" for name, d, req in items)
    embed = await get_custom_msg(i.guild.id, "achieve_list_msg", "🏆 Achievements / الإنجازات", desc, discord.Color.purple())
    await i.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="claim-achievement", description="Claim achievement reward / استلام مكافأة إنجاز")
@app_commands.checks.cooldown(1, 10.0)
async def claim_achievement(i: discord.Interaction):
    if not await gate(i, "claim-achievement"): return
    
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT a.id, a.name, a.reward_points, a.role_id FROM user_achievements ua JOIN achievements a ON a.id = ua.achievement_id WHERE ua.guild_id=? AND ua.user_id=? AND ua.claimed=0 LIMIT 1", (i.guild.id, i.user.id))
            row = await cur.fetchone()
            if not row: 
                await db.rollback()
                return await i.response.send_message(tr("achieve_already_claimed"), ephemeral=True)
            
            ach_id, name, reward_pts, reward_role = row
            
            cur = await db.execute("UPDATE user_achievements SET claimed=1 WHERE guild_id=? AND user_id=? AND achievement_id=? AND claimed=0", (i.guild.id, i.user.id, ach_id))
            if cur.rowcount == 0:
                await db.rollback()
                return await i.response.send_message(tr("achieve_already_claimed"), ephemeral=True)
                
            if reward_pts > 0:
                await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (i.guild.id, i.user.id, reward_pts, reward_pts))
                await db.execute("INSERT INTO point_events(guild_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?)", (i.guild.id, i.user.id, reward_pts, now_utc().isoformat(), "achievement", await get_current_season(i.guild.id)))
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"❌ claim-achievement DB error: {e}")
            return await i.response.send_message(tr("db_err"), ephemeral=True)
        
    if reward_role:
        role = i.guild.get_role(reward_role)
        if role:
            try: await i.user.add_roles(role, reason="Achievement Reward")
            except discord.Forbidden: pass
            
    await i.response.send_message(tr("achieve_claimed", name=name), ephemeral=True)

@bot.tree.command(name="add-quest", description="Add daily quest / إضافة مهمة يومية")
@app_commands.checks.cooldown(1, 5.0)
async def add_quest(i: discord.Interaction, description: str, reward_points: int, role: Optional[discord.Role] = None):
    if not await gate(i, "add-quest", editor=True): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO quests(guild_id, description, target_type, target_amount, reward_points, role_id, is_auto) VALUES(?,?,?,?,?,?,0)", (i.guild.id, description, "log", 1, reward_points, role.id if role else None))
        await db.commit()
    await i.response.send_message(f"✅ Added daily quest: {description} / تم إضافة المهمة.", ephemeral=True)

@bot.tree.command(name="quests", description="View daily quests / عرض المهام اليومية")
@app_commands.checks.cooldown(1, 5.0)
async def quests_cmd(i: discord.Interaction):
    if not await gate(i, "quests"): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id, description, reward_points FROM quests WHERE guild_id=?", (i.guild.id,))
        items = await cur.fetchall()
    if not items: return await i.response.send_message("⚠️ No quests yet. / لا توجد مهام حالياً.", ephemeral=True)
    desc = "".join(f"📜 {d} — Reward: {r} pts\n" for _id, d, r in items)
    embed = await get_custom_msg(i.guild.id, "quest_msg", tr("quest_title"), desc, discord.Color.green())
    await i.response.send_message(embed=embed, ephemeral=True)

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

@bot.tree.command(name="event", description="Create an event / إنشاء فعالية جديدة")
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
    await i.response.send_message(f"✅ Event created in {ch.mention}. / تم إنشاء الفعالية.", ephemeral=True)

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
        if self.player.value.isdigit():
            target_id = int(self.player.value)
        else:
            match = re.search(r"<@!?(\d+)>", self.player.value)
            if match: target_id = int(match.group(1))
            else:
                member = discord.utils.find(lambda m: m.name.lower() == self.player.value.lower() or m.display_name.lower() == self.player.value.lower(), i.guild.members)
                if member: target_id = member.id
                
        if not target_id:
            return await i.response.send_message("❌ Member not found. / لم يتم العثور على العضو.", ephemeral=True)
            
        async with aiosqlite.connect(DB) as db:
            await db.execute("INSERT OR REPLACE INTO votes(guild_id, message_id, voter_id, target_id) VALUES(?,?,?,?)", (i.guild.id, self.message_id, i.user.id, target_id))
            await db.commit()
        await i.response.send_message(f"✅ Vote registered for <@{target_id}>. / تم تسجيل صوتك.", ephemeral=True)

@bot.tree.command(name="start-vote", description="Start MVP vote / بدء تصويت لأفضل لاعب")
@app_commands.checks.cooldown(1, 60.0)
async def start_vote(i: discord.Interaction, reason: str = "أفضل لاعب هذا الأسبوع / Best player this week"):
    if not await gate(i, "start-vote", editor=True): return
    embed = discord.Embed(title="🏆 Voting / تصويت", description=f"**{reason}**\n\nWho is the best player? / من هو أفضل لاعب؟", color=discord.Color.blue())
    msg = await i.channel.send(embed=embed, view=VoteView())
    await i.response.send_message("✅ Voting started. / تم بدء التصويت.", ephemeral=True)

@bot.tree.command(name="h2h", description="Head-to-head stats / إحصائيات المواجهات المباشرة")
@app_commands.checks.cooldown(1, 5.0)
async def h2h_cmd(i: discord.Interaction, member1: discord.Member, member2: discord.Member):
    if not await gate(i, "h2h"): return
    await i.response.defer()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT author_id, result, data FROM logs WHERE guild_id=? AND type='1v1' AND status='accepted'", (i.guild.id,))
        rows = await cur.fetchall()
        
    m1_wins, m2_wins = 0, 0
    for author_id, result, data_str in rows:
        data = json.loads(data_str or "{}")
        opp_text = str(data.get("opponent", ""))
        fighter_text = str(data.get("my_fighter", ""))
        
        m1_name, m2_name = member1.display_name.lower(), member2.display_name.lower()
        m1_username, m2_username = member1.name.lower(), member2.name.lower()
        
        is_m1_fighting = m1_name in fighter_text.lower() or m1_username in fighter_text.lower()
        is_m2_fighting = m2_name in fighter_text.lower() or m2_username in fighter_text.lower()
        is_m1_opp = m1_name in opp_text.lower() or m1_username in opp_text.lower()
        is_m2_opp = m2_name in opp_text.lower() or m2_username in opp_text.lower()

        if author_id == member1.id and is_m2_opp:
            if result == "win": m1_wins += 1
            elif result == "loss": m2_wins += 1
        elif author_id == member2.id and is_m1_opp:
            if result == "win": m2_wins += 1
            elif result == "loss": m1_wins += 1
        elif author_id not in (member1.id, member2.id):
            if is_m1_fighting and is_m2_opp:
                if result == "win": m1_wins += 1
                elif result == "loss": m2_wins += 1
            elif is_m2_fighting and is_m1_opp:
                if result == "win": m2_wins += 1
                elif result == "loss": m1_wins += 1

    e = discord.Embed(title="⚔️ Head-to-Head / مواجهات مباشرة", color=discord.Color.red())
    e.add_field(name=member1.display_name, value=f"{m1_wins} Wins")
    e.add_field(name=member2.display_name, value=f"{m2_wins} Wins")
    await i.followup.send(embed=e)

@bot.tree.command(name="set-lfg-roles", description="Set allowed roles for LFG / تحديد الرتب المسموح بها")
@app_commands.checks.cooldown(1, 5.0)
async def set_lfg_roles(i: discord.Interaction, role: discord.Role):
    if not await gate(i, "set-lfg-roles", editor=True): return
    if role.permissions.administrator or role.name.lower() in ("@everyone", "new member", "admin", "moderator"):
        return await i.response.send_message("❌ Cannot use this role for security reasons. / لا يمكنك استخدام هذه الرتبة لأغراض الأمان.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO lfg_allowed_roles(guild_id, role_id) VALUES(?,?)", (i.guild.id, role.id))
        await db.commit()
    await i.response.send_message(f"✅ Allowed role {role.mention} for LFG. / تم السماح بالرتبة.", ephemeral=True)

async def lfg_role_autocomplete(i: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT role_id FROM lfg_allowed_roles WHERE guild_id=?", (i.guild.id,))
        role_ids = [r[0] for r in await cur.fetchall()]
    roles = [i.guild.get_role(rid) for rid in role_ids if i.guild.get_role(rid)]
    return [app_commands.Choice(name=r.name, value=str(r.id)) for r in roles if current.lower() in r.name.lower()][:25]

# [PATCH 8] LFG مع cooldown 5 دقايق + تأخير ثانية + حد أقصى 200 عضو
@bot.tree.command(name="lfg", description="Looking for group / البحث عن فريق للعب")
@app_commands.checks.cooldown(1, 300.0)
@app_commands.choices(mode=[app_commands.Choice(name="Channel / قناة عامة", value="channel"), app_commands.Choice(name="DM / رسالة خاصة", value="dm")])
@app_commands.autocomplete(target_role=lfg_role_autocomplete)
async def lfg_cmd(i: discord.Interaction, mode: app_commands.Choice[str], target_role: str, note: str = "Looking for a team! / أبحث عن فريق للعب!"):
    if not await gate(i, "lfg"): return
    
    if mode.value == "dm" and not await is_editor(i.user):
        return await i.response.send_message("❌ DM broadcasting is for staff only. / إرسال رسائل جماعية في الخاص متاح للإدارة فقط.", ephemeral=True)
    
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM lfg_allowed_roles WHERE guild_id=? AND role_id=?", (i.guild.id, int(target_role)))
        if not await cur.fetchone():
            return await i.response.send_message("❌ This role is not allowed for LFG. / هذه الرتبة غير مسموح بها.", ephemeral=True)
            
    role = i.guild.get_role(int(target_role))
    if not role: return await i.response.send_message("❌ Role not found. / الرتبة غير موجودة.", ephemeral=True)
    
    embed = discord.Embed(title="🔍 Looking for Group / البحث عن فريق", description=f"**{note}**\n\nFrom / من: {i.user.mention}", color=discord.Color.blue())
    
    if mode.value == "channel":
        await i.channel.send(content=role.mention, embed=embed)
        await i.response.send_message("✅ Sent to channel. / تم الإرسال للقناة.", ephemeral=True)
    else:
        await i.response.defer(ephemeral=True)
        await _ensure_members(i.guild)
        
        count = 0
        failed = 0
        
        # حد أقصى 200 عضو لتجنب spam
        members_to_notify = [m for m in role.members if not m.bot and m.id != i.user.id][:200]
        
        for member in members_to_notify:
            try:
                await member.send(embed=embed)
                count += 1
            except discord.HTTPException:
                failed += 1
            
            # تأخير ثانية كاملة لتجنب rate limit
            await asyncio.sleep(1.0)
            
        await i.followup.send(f"✅ Sent to {count} members in DM. (Failed: {failed})\nتم الإرسال لـ {count} عضو في الخاص. (فشل إرسال لـ {failed})", ephemeral=True)

@bot.tree.command(name="daily", description="Claim daily bonus / الحصول على المكافأة اليومية")
@app_commands.checks.cooldown(1, 10.0)
async def daily_cmd(i: discord.Interaction):
    if not await gate(i, "daily"): return
    today = now_utc().date().isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT last_claimed FROM daily_bonus WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
        row = await cur.fetchone()
        if row and row[0] == today: return await i.response.send_message("❌ You already claimed your daily bonus today! / لقد استلمت مكافأتك اليومية بالفعل اليوم!", ephemeral=True)
        
        reward = int(await get_custom_val(i.guild.id, "daily_reward_points", 10))
        await db.execute("INSERT INTO daily_bonus(guild_id, user_id, last_claimed) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET last_claimed=excluded.last_claimed", (i.guild.id, i.user.id, today))
        await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (i.guild.id, i.user.id, reward, reward))
        await db.execute("INSERT INTO point_events(guild_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?)", (i.guild.id, i.user.id, reward, now_utc().isoformat(), "daily", await get_current_season(i.guild.id)))
        await db.commit()
    await i.response.send_message(f"✅ You claimed **{reward} points**! / لقد استلمت **{reward} نقطة**!", ephemeral=True)

class TicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Open Ticket / فتح تذكرة", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="ticket_open")
    async def open_ticket(self, i: discord.Interaction, button: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        guild = i.guild
        
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'", (guild.id, i.user.id))
            if await cur.fetchone():
                return await i.followup.send("❌ You already have an open ticket. / لديك تذكرة مفتوحة بالفعل.", ephemeral=True)

        overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False), i.user: discord.PermissionOverwrite(read_messages=True, send_messages=True), guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
        for role_name in EDITOR_ROLES:
            role = discord.utils.get(guild.roles, name=role_name)
            if role: overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            
        try:
            channel = await guild.create_text_channel(name=f"ticket-{i.user.name}", overwrites=overwrites, category=None)
            async with aiosqlite.connect(DB) as db:
                await db.execute("INSERT INTO tickets(guild_id, user_id, channel_id, status) VALUES(?,?,?,?)", (guild.id, i.user.id, channel.id, "open"))
                await db.commit()
            await channel.send(f"Welcome {i.user.mention}! A staff member will be with you shortly.\nUse the button below to close the ticket.", view=CloseTicketView())
            await i.followup.send(f"✅ Ticket created: {channel.mention}", ephemeral=True)
        except discord.Forbidden:
            await i.followup.send("❌ I don't have permission to create channels. / لا أملك صلاحية إنشاء القنوات.", ephemeral=True)

class CloseTicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Close Ticket / إغلاق", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket_close")
    async def close_ticket(self, i: discord.Interaction, button: discord.ui.Button):
        await i.response.send_message("🔒 Closing ticket and generating transcript... / جاري الإغلاق وحفظ المحادثة...", ephemeral=True)
        
        log_ch_id = await cfg_get(i.guild.id, "all", "audit_channel")
        target_ch = i.guild.get_channel(log_ch_id) if log_ch_id else None
        if not target_ch:
            return await i.response.send_message("❌ No audit channel set for transcripts. / لا توجد قناة تدقيق لحفظ النسخة.", ephemeral=True)

        messages = []
        async for msg in i.channel.history(limit=500): messages.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M')}] {msg.author.name}: {msg.content}")
        transcript = "\n".join(reversed(messages)).encode("utf-8")
        
        try:
            await target_ch.send(f"🎫 Ticket transcript for {i.channel.name}:", file=discord.File(io.BytesIO(transcript), filename=f"transcript-{i.channel.name}.txt"))
        except Exception as e:
            print(f"⚠️ Failed to send transcript: {e}")
            return await i.response.send_message("❌ Failed to send transcript. / فشل إرسال النسخة.", ephemeral=True)
            
        async with aiosqlite.connect(DB) as db:
            await db.execute("UPDATE tickets SET status='closed' WHERE channel_id=?", (i.channel.id,))
            await db.commit()
            
        await i.channel.delete()

@bot.tree.command(name="setup-tickets", description="Setup support tickets / إنشاء لوحة التذاكر")
@app_commands.checks.cooldown(1, 10.0)
async def setup_tickets(i: discord.Interaction):
    if not await gate(i, "setup-tickets", editor=True): return
    embed = discord.Embed(title="🎫 Support Tickets / تذاكر الدعم", description="Click the button below to open a private ticket with staff.\nاضغط على الزر لفتح تذكرة خاصة مع الإدارة.", color=discord.Color.blue())
    await i.channel.send(embed=embed, view=TicketView())
    await i.response.send_message("✅ Ticket panel setup. / تم إنشاء لوحة التذاكر.", ephemeral=True)

@bot.tree.command(name="purge", description="Purge messages / مسح رسائل في القناة")
@app_commands.checks.cooldown(1, 10.0)
async def purge_cmd(i: discord.Interaction, amount: app_commands.Range[int, 1, 100], member: Optional[discord.Member] = None):
    if not await gate(i, "purge", editor=True): return
    
    protected_channels = [await cfg_get(i.guild.id, "all", "review_channel"), await cfg_get(i.guild.id, "all", "log_channel")]
    if i.channel.id in protected_channels:
        return await i.response.send_message("❌ Cannot purge in log/review channels. / لا يمكن المسح في قنوات اللوجات أو المراجعة!", ephemeral=True)
        
    if not i.channel.permissions_for(i.guild.me).manage_messages: return await i.response.send_message("❌ I don't have `Manage Messages` permission. / لا أملك صلاحية إدارة الرسائل.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    def check(msg): return msg.author.id == member.id if member else True
    try:
        deleted = await i.channel.purge(limit=amount if not member else 100, check=check, bulk=True)
        await i.followup.send(f"✅ Deleted {len(deleted)} messages. / تم مسح {len(deleted)} رسالة.", ephemeral=True)
    except discord.HTTPException as e:
        await i.followup.send(f"❌ Failed: {e}", ephemeral=True)

@bot.tree.command(name="embed", description="Embed builder / صانع الرسائل الملونة")
@app_commands.checks.cooldown(1, 5.0)
@app_commands.choices(color=COLOR_CHOICES)
async def embed_builder(i: discord.Interaction, title: str, description: str, color: Optional[app_commands.Choice[str]] = None):
    if not await gate(i, "embed", editor=True): return
    embed_color = discord.Color.blue()
    if color:
        try: embed_color = int(color.value, 16)
        except ValueError: pass
    embed = discord.Embed(title=title[:256], description=description[:4000], color=embed_color)
    await i.channel.send(embed=embed)
    await i.response.send_message("✅ Embed sent. / تم إرسال الرسالة.", ephemeral=True)

@bot.tree.command(name="create-custom-role", description="Create custom role / إنشاء رتبة مخصصة")
@app_commands.checks.cooldown(1, 60.0)
@app_commands.choices(color=COLOR_CHOICES)
async def create_custom_role(i: discord.Interaction, name: app_commands.Range[str, 2, 32], color: app_commands.Choice[str]):
    if not await gate(i, "create-custom-role"):
        return
    if not isinstance(i.user, discord.Member) or not i.guild:
        return await i.response.send_message(tr("server_only"), ephemeral=True)

    clean_name = name.strip()
    forbidden = ["admin", "mod", "commander", "admiral", "owner", "lvl", "staff", "empyrean", "apotheosis"]
    if not clean_name or any(w in clean_name.casefold() for w in forbidden):
        return await i.response.send_message("❌ Name contains forbidden words. / الاسم يحتوي على كلمات محظورة.", ephemeral=True)

    is_free = is_admin(i.user) or await has_temp_admin(i.user)
    price = 0 if is_free else int(await get_custom_val(i.guild.id, "custom_role_price", 500))
    if price < 0 or price > 1_000_000_000:
        return await i.response.send_message("❌ Store price configuration is invalid. / إعداد سعر المتجر غير صالح.", ephemeral=True)

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT COUNT(*) FROM custom_roles WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
        if (await cur.fetchone())[0] >= 3:
            return await i.response.send_message("❌ Max 3 custom roles reached. / وصلت للحد الأقصى (3 رتب).", ephemeral=True)
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
        points = (await cur.fetchone() or [0])[0]
    if not is_free and points < price:
        return await i.response.send_message(tr("shop_no_points", price=price, points=points), ephemeral=True)

    try:
        role_color = int(color.value, 16)
        role = await i.guild.create_role(name=clean_name, color=discord.Color(role_color), reason="Custom Role Purchase")
    except (ValueError, discord.Forbidden, discord.HTTPException):
        return await i.response.send_message("❌ Could not create the role. Check the bot's Manage Roles permission and role hierarchy. / تعذر إنشاء الرتبة، راجع صلاحيات البوت وترتيب الرتب.", ephemeral=True)

    try:
        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            if not is_free:
                cur = await db.execute(
                    "UPDATE members SET points=points-? WHERE guild_id=? AND user_id=? AND points>=?",
                    (price, i.guild.id, i.user.id, price)
                )
                if cur.rowcount != 1:
                    await db.rollback()
                    await role.delete(reason="Custom role purchase rolled back")
                    return await i.response.send_message("❌ Insufficient balance. / رصيدك غير كافٍ.", ephemeral=True)
                await db.execute(
                    "INSERT INTO point_events(guild_id,user_id,points,created_at,type,season_id) VALUES(?,?,?,?,?,?)",
                    (i.guild.id, i.user.id, -price, now_utc().isoformat(), "shop", await get_current_season(i.guild.id))
                )
            await db.execute(
                "INSERT INTO custom_roles(guild_id,user_id,role_id,created_at) VALUES(?,?,?,?)",
                (i.guild.id, i.user.id, role.id, now_utc().isoformat())
            )
            await db.commit()
    except Exception:
        try:
            await role.delete(reason="Custom role purchase rolled back after database error")
        except discord.HTTPException:
            pass
        return await i.response.send_message("❌ Purchase failed; no charge was committed. / فشلت العملية ولم يتم اعتماد الخصم.", ephemeral=True)

    try:
        await i.user.add_roles(role, reason="Custom role purchase")
    except (discord.Forbidden, discord.HTTPException):
        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("DELETE FROM custom_roles WHERE role_id=? AND guild_id=?", (role.id, i.guild.id))
            if not is_free:
                await db.execute("UPDATE members SET points=points+? WHERE guild_id=? AND user_id=?", (price, i.guild.id, i.user.id))
                await db.execute(
                    "INSERT INTO point_events(guild_id,user_id,points,created_at,type,season_id) VALUES(?,?,?,?,?,?)",
                    (i.guild.id, i.user.id, price, now_utc().isoformat(), "shop_refund", await get_current_season(i.guild.id))
                )
            await db.commit()
        try:
            await role.delete(reason="Role assignment failed; purchase refunded")
        except discord.HTTPException:
            pass
        return await i.response.send_message("❌ Could not assign the role; the purchase was refunded. / تعذر منح الرتبة وتم رد النقاط.", ephemeral=True)

    await i.response.send_message(
        tr("role_created_free" if is_free else "role_created", role=role.mention, member=i.user.mention),
        ephemeral=True
    )

@bot.tree.command(name="create-temp-voice", description="Create temp voice / إنشاء غرفة صوتية مؤقتة")
@app_commands.checks.cooldown(1, 60.0)
async def create_temp_voice(i: discord.Interaction, name: Optional[str] = None):
    if not await gate(i, "create-temp-voice"): return
    await i.response.defer(ephemeral=True)
    guild = i.guild
    channel_name = name or f"🔊 Room: {i.user.display_name}"
    overwrites = {guild.default_role: discord.PermissionOverwrite(connect=True), i.user: discord.PermissionOverwrite(manage_channels=True, connect=True)}
    try:
        channel = await guild.create_voice_channel(name=channel_name[:100], overwrites=overwrites)
        await i.followup.send(f"✅ Temp voice created: {channel.mention} / تم إنشاء الغرفة الصوتية.", ephemeral=True)
    except discord.Forbidden:
        await i.followup.send("❌ No permission to create voice channels. / ليس لدي صلاحية إنشاء قنوات صوتية.", ephemeral=True)

@bot.tree.command(name="set-top-rewards", description="Set weekly top 3 rewards / تحديد جوائز المتصدرين")
@app_commands.checks.cooldown(1, 5.0)
async def set_top_rewards(i: discord.Interaction, rank: app_commands.Range[int, 1, 3], points: int, role: Optional[discord.Role] = None):
    if not await gate(i, "set-top-rewards", editor=True): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO custom_settings(guild_id, key, value) VALUES(?,?,?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (i.guild.id, f"top{rank}_reward_points", str(points)))
        if role:
            await db.execute("INSERT INTO custom_settings(guild_id, key, value) VALUES(?,?,?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (i.guild.id, f"top{rank}_reward_role", str(role.id)))
        await db.commit()
    await i.response.send_message(f"✅ Rank {rank} reward: {points} pts" + (f" and {role.mention}" if role else "") + " / تم التحديد.", ephemeral=True)

# ================================================================ الغرف الصوتية المؤقتة (Auto)
@bot.tree.command(name="set-temp-voice", description="Set temp voice trigger / تحديد قناة إنشاء الغرف الصوتية")
@app_commands.checks.cooldown(1, 5.0)
async def set_temp_voice(i: discord.Interaction, channel: discord.VoiceChannel):
    if not await gate(i, "set-temp-voice", editor=True): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO custom_settings(guild_id, key, value) VALUES(?,?,?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (i.guild.id, "temp_voice_trigger", str(channel.id)))
        await db.commit()
    await i.response.send_message(f"✅ Set {channel.mention} as temp voice trigger. / تم تحديد القناة.", ephemeral=True)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    guild = member.guild
    
    if after.channel:
        trigger_id_str = await get_custom_val(guild.id, "temp_voice_trigger", "0")
        if str(after.channel.id) == trigger_id_str:
            try:
                new_channel = await guild.create_voice_channel(name=f"🔊 Room: {member.display_name}", category=after.channel.category)
                async with aiosqlite.connect(DB) as db:
                    await db.execute(
                        "INSERT INTO temp_voice_rooms(guild_id,channel_id,owner_id,created_at) VALUES(?,?,?,?)",
                        (guild.id, new_channel.id, member.id, now_utc().isoformat())
                    )
                    await db.commit()
                await new_channel.set_permissions(member, connect=True, manage_channels=True)
                await member.move_to(new_channel)
            except discord.HTTPException: pass

    if before.channel and before.channel != after.channel:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT channel_id FROM temp_voice_rooms WHERE guild_id=? AND channel_id=?",
                (guild.id, before.channel.id)
            )
            is_managed_room = await cur.fetchone()
        if is_managed_room and len(before.channel.members) == 0:
            try:
                await before.channel.delete(reason="Temporary voice room became empty")
            except discord.HTTPException:
                return
            async with aiosqlite.connect(DB) as db:
                await db.execute("DELETE FROM temp_voice_rooms WHERE guild_id=? AND channel_id=?", (guild.id, before.channel.id))
                await db.commit()

# ================================================================ إصلاح الداتابيز للمهام
async def fix_db_columns():
    async with aiosqlite.connect(DB) as db:
        try:
            cur = await db.execute("PRAGMA table_info(quests)")
            cols = [r[1] for r in await cur.fetchall()]
            if "is_auto" not in cols:
                await db.execute("ALTER TABLE quests ADD COLUMN is_auto INTEGER DEFAULT 0")
            await db.commit()
        except Exception as e:
            print(f"⚠️ DB Fix error: {e}")

# ================================================================ المهام التلقائية (Background Tasks)
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
                    else:
                        await ch.send(text, allowed_mentions=mentions(ping_id))
                except discord.HTTPException: pass
            await db_exec("UPDATE logs SET reminded=1 WHERE id=?", (l["id"],))
        except Exception as e:
            print(f"⚠️ Reminder error for log {l['id']}: {e}")

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
                    print(f"❌ daily quests DB error: {e}")
                    continue
                    
            await set_setting(guild.id, "state:last_quest_gen", today_str)
            await send_notification(guild, "quests", "🎯 مهام جديدة!", "تم توليد مهام يومية جديدة! استخدم `/quests` لمشاهدتها.", discord.Color.green())
        except Exception as e:
            print(f"⚠️ Auto quest generation failed for guild {guild.id}: {e}")

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
        except Exception as e:
            print(f"⚠️ Auto backup check error: {e}")
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
        except Exception as e:
            print(f"⚠️ Weekly leaderboard error: {e}")

@tasks.loop(minutes=30)
async def maintenance():
    for job in (run_reminders, run_daily_quests, run_backup, run_weekly):
        try:
            await job()
        except Exception as e:
            print(f"⚠️ Maintenance job {job.__name__} failed: {e}")

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