import asyncio, datetime as dt, hashlib, io, json, os, random, re, sqlite3, tempfile, threading, time, uuid, urllib.parse, aiohttp, aiosqlite, discord
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional
from urllib.parse import urlparse
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

BOT_VERSION = "v9.5.0 (Ultimate Edition)"
DB = os.getenv("DB_PATH", "crewbot.db")
JSON_CONFIG_PATH = os.getenv("JSON_CONFIG_PATH") or "servers_settings.json"
AI_PROVIDER = os.getenv("AI_PROVIDER", "groq").lower()
AI_API_KEY = os.getenv("AI_API_KEY", "")

# تم تصليح موديل الذكاء الاصطناعي هنا
_PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "openai/gpt-oss-120b"),
    "groq_vision": ("https://api.groq.com/openai/v1/chat/completions", "llama-3.2-90b-vision-preview"),
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    "deepseek": ("https://api.deepseek.com/chat/completions", "deepseek-chat"),
}

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".mkv"}
MAX_FILE_SIZE, MAX_TOTAL_FILES_SIZE, MAX_FILES_COUNT = 25 * 1024 * 1024, 30 * 1024 * 1024, 5
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}

LANG_NAMES = {"ar": "Arabic", "hi": "Hindi", "en": "English", "ru": "Russian"}
def detect_language(text: str) -> str:
    if not text: return "en"
    if re.search(r"[\u0600-\u06FF\u0750-\u077F]", text): return "ar"
    if re.search(r"[\u0900-\u097F]", text): return "hi"
    if re.search(r"[\u0400-\u04FF]", text): return "ru"
    return "en"

def _build_system_prompt(lang: str, is_admin: bool) -> str:
    lang_name = LANG_NAMES.get(lang, "English")
    admin_status = "You HAVE full admin permissions. Execute tools without asking." if is_admin else "You DO NOT have admin permissions. Refuse administrative tasks."
    return f"You are the Ultimate AI Assistant inside Dragon Slayers Discord.\nLanguage: Reply in {lang_name}.\nRULES:\n- Keep replies short.\n- {admin_status}\n- Use `search_web` for real-time info."

_T_DEFS = [
    ("get_member_info", "Get member info", {"member_query": "string"}, False), ("get_server_info", "Get server stats", {}, False),
    ("list_channels", "List channels", {}, False), ("list_roles", "List roles", {}, False), ("search_web", "Search internet", {"query": "string"}, False),
    ("create_role", "Create role", {"name": "string", "color_hex": "string"}, True), ("edit_role", "Edit role", {"role_query": "string", "new_name": "string"}, True),
    ("delete_role", "Delete role", {"role_query": "string"}, True), ("create_channel", "Create channel", {"name": "string", "channel_type": "string"}, True),
    ("delete_channel", "Delete channel", {"channel_query": "string"}, True), ("give_role", "Give role", {"member_query": "string", "role_query": "string"}, True),
    ("remove_role", "Remove role", {"member_query": "string", "role_query": "string"}, True), ("timeout_member", "Timeout member", {"member_query": "string", "duration_minutes": "integer"}, True),
    ("kick_member", "Kick member", {"member_query": "string"}, True), ("purge_messages", "Delete messages", {"amount": "integer"}, True),
    ("send_channel_message", "Send message", {"channel_query": "string", "message_text": "string"}, True),
]
TOOLS, TOOL_META = [], {}
for _n, _d, _p, _a in _T_DEFS:
    _props, _req = {}, []
    for _k, _v in _p.items():
        _props[_k] = {"type": "integer" if "integer" in _v else "string", "description": _v}
        if "required" in _v or "string" in _v or "integer" in _v: _req.append(_k)
    TOOLS.append({"type": "function", "function": {"name": _n, "description": _d, "parameters": {"type": "object", "properties": _props, "required": _req}}})
    TOOL_META[_n] = {"requires_admin": _a}

def run_dummy_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(f"Bot {BOT_VERSION} Online".encode())
        def log_message(self, format, *args): pass
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), Handler).serve_forever()

LOG_TYPES = {
    "war": {"label": "Crew War Log", "fields": [("my_team", "Who fought for us", True, True), ("crew", "Enemy crew", False, True), ("their_team", "Enemy fighters", True, True), ("score", "Score", False, True), ("notes", "Notes", True, False)]},
    "1v1": {"label": "1v1 Fight Log", "fields": [("my_fighter", "Our fighter", False, True), ("opponent", "Opponent", False, True), ("score", "Score", False, True), ("notes", "Notes", True, False)]},
    "2v2": {"label": "2v2 Fight Log", "fields": [("my_team", "Our team", False, True), ("their_team", "Enemy team", False, True), ("score", "Score", False, True), ("notes", "Notes", True, False)]},
    "training": {"label": "Training Log", "fields": [("participants", "Who trained", True, True), ("score", "Result", False, False), ("notes", "Notes", True, False)]},
    "tryout": {"label": "Tryout Log", "fields": [("score", "Tryout score", False, True), ("roblox", "Roblox username", False, False), ("notes", "Notes", True, False)]}
}
DEFAULT_POINTS = {"war_win": 10, "war_loss": 5, "1v1_win": 8, "1v1_loss": 4, "2v2_win": 6, "2v2_loss": 3, "training": 3, "tryout": 5}
POINT_LABELS = {k: k.replace("_", " ").title() for k in DEFAULT_POINTS}
LADDER = [("Lvl. 20", 20), ("Lvl. 40", 60), ("Lvl. 60", 130), ("Lvl. 80", 220), ("Lvl. 100", 350)]
EDITOR_ROLES = ["The Admiral", "Empyrean Monarch", "Commander"]
REVIEWER_ROLES = ["The Admiral", "Empyrean Monarch", "Commander", "Astral Apotheosis"]
TRYOUTER_ROLES = ["Tryout Master"]
RULE_DEFAULTS = {"daily_limit": 5, "reminder_hours": 48, "backup_hours": 24, "weekly_post": 1, "war_participants": 1, "tryout_autorole": 1}
STATUS_LABEL = {"pending": "⏳ Pending", "accepted": "✅ Accepted", "rejected": "❌ Rejected", "revoked": "🚫 Revoked"}
RESULT_LABEL = {"win": "✅ Win", "loss": "❌ Loss"}
TRYOUT_LABEL = {"win": "✅ Passed", "loss": "❌ Failed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id, key));
CREATE TABLE IF NOT EXISTS members(guild_id INTEGER, user_id INTEGER, points INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, type TEXT, author_id INTEGER, result TEXT, data TEXT, flags TEXT, status TEXT, points INTEGER, reviewer_id INTEGER, reason TEXT, created_at TEXT, review_channel INTEGER, review_msg INTEGER, public_channel INTEGER, public_msg INTEGER, reviewed_at TEXT, reminded INTEGER, parent_id INTEGER, season_id TEXT);
CREATE TABLE IF NOT EXISTS hashes(hash TEXT, log_id INTEGER, PRIMARY KEY(hash, log_id));
CREATE TABLE IF NOT EXISTS guild_cfg(guild_id INTEGER, log_type TEXT, key TEXT, value INTEGER, PRIMARY KEY(guild_id, log_type, key));
CREATE TABLE IF NOT EXISTS role_perms(guild_id INTEGER, log_type TEXT, kind TEXT, role_id INTEGER, PRIMARY KEY(guild_id, log_type, kind, role_id));
CREATE TABLE IF NOT EXISTS point_events(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, log_id INTEGER, user_id INTEGER, points INTEGER, created_at TEXT, type TEXT, season_id TEXT);
CREATE TABLE IF NOT EXISTS links(guild_id INTEGER, user_id INTEGER, roblox_name TEXT, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS season_archive(season TEXT, guild_id INTEGER, user_id INTEGER, points INTEGER, PRIMARY KEY(season, guild_id, user_id));
CREATE TABLE IF NOT EXISTS cmd_roles(guild_id INTEGER, cmd_name TEXT, role_id INTEGER, PRIMARY KEY(guild_id, cmd_name));
CREATE TABLE IF NOT EXISTS cmd_users(guild_id INTEGER, cmd_name TEXT, user_id INTEGER, PRIMARY KEY(guild_id, cmd_name, user_id));
CREATE TABLE IF NOT EXISTS crews(guild_id INTEGER, crew_name TEXT, owner_id INTEGER, captains_data TEXT, created_at TEXT, PRIMARY KEY(guild_id, crew_name));
CREATE TABLE IF NOT EXISTS custom_settings(guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id, key));
CREATE TABLE IF NOT EXISTS custom_messages(guild_id INTEGER, key TEXT, title TEXT, description TEXT, color TEXT, PRIMARY KEY(guild_id, key));
CREATE TABLE IF NOT EXISTS custom_roles(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, role_id INTEGER UNIQUE, created_at TEXT);
CREATE TABLE IF NOT EXISTS temp_voice_rooms(guild_id INTEGER, channel_id INTEGER PRIMARY KEY, owner_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS streaks(guild_id INTEGER, user_id INTEGER, current_streak INTEGER, max_streak INTEGER, last_log_date TEXT, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS shop_items(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, price INTEGER, role_id INTEGER, description TEXT);
CREATE TABLE IF NOT EXISTS achievements(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, description TEXT, required_points INTEGER, reward_points INTEGER, role_id INTEGER);
CREATE TABLE IF NOT EXISTS user_achievements(guild_id INTEGER, user_id INTEGER, achievement_id INTEGER, claimed INTEGER, PRIMARY KEY(guild_id, user_id, achievement_id));
CREATE TABLE IF NOT EXISTS quests(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, description TEXT, target_type TEXT, target_amount INTEGER, reward_points INTEGER, role_id INTEGER, is_auto INTEGER);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER, message_id INTEGER, title TEXT, time TEXT, attendees TEXT);
CREATE TABLE IF NOT EXISTS tickets(guild_id INTEGER, user_id INTEGER, channel_id INTEGER, status TEXT, PRIMARY KEY(guild_id, user_id, channel_id));
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
LOG_ID_RE, LOG_ID_FALLBACK_RE, TEMPLATE_VAR_RE = re.compile(r"`Log #(\d+)`"), re.compile(r"Log #(\d+)"), re.compile(r"\{(\w{1,32})\}")

def now_utc(): return dt.datetime.now(timezone.utc)
def owner_id():
    try: return int(os.getenv("OWNER_ID", 0))
    except: return 0

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.executescript(SCHEMA)
        await db.commit()

async def db_exec(sql, params=()):
    async with aiosqlite.connect(DB) as db: await db.execute(sql, params); await db.commit()

async def raw_setting(guild_id: int, key: str):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
        row = await cur.fetchone()
    return row[0] if row else None

async def get_setting(guild_id: int, key: str):
    v = await raw_setting(guild_id, key)
    if v is not None:
        try: return int(v)
        except: return v
    if key.startswith("points:"): return DEFAULT_POINTS.get(key[len("points:"):], 0)
    if key.startswith("rank:"): return next((p for name, p in LADDER if name == key[len("rank:"):]), 0)
    if key.startswith("rule:"): return RULE_DEFAULTS.get(key[len("rule:"):], 0)
    return None

async def set_setting(guild_id: int, key: str, value):
    await db_exec("INSERT INTO settings(guild_id, key, value) VALUES(?,?,?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (guild_id, str(key), str(value)))

async def rule(guild_id: int, name: str) -> int:
    v = await get_setting(guild_id, "rule:" + name)
    try: return int(v)
    except: return int(RULE_DEFAULTS.get(name, 0))

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
        if value is None: await db.execute("DELETE FROM guild_cfg WHERE guild_id=? AND log_type=? AND key=?", (guild_id, log_type, key))
        else: await db.execute("INSERT INTO guild_cfg VALUES(?,?,?,?) ON CONFLICT(guild_id, log_type, key) DO UPDATE SET value=excluded.value", (guild_id, log_type, key, value))
        await db.commit()

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

async def get_ladder(guild_id: int):
    return [(n, await get_setting(guild_id, "rank:" + n)) for n, _ in LADDER]

async def get_log(log_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM logs WHERE id=?", (log_id,))
        return await cur.fetchone()

async def points_for(guild_id: int, log_type, result) -> int:
    key = f"{log_type}_{result}" if log_type in ("war", "1v1", "2v2") else log_type
    v = await get_setting(guild_id, "points:" + key)
    try: return int(v)
    except: return DEFAULT_POINTS.get(key, 0)

async def recipients_for(row, data):
    if row["type"] == "tryout": return [int(data.get("tester_id") or row["author_id"])]
    ids = [row["author_id"]]
    if row["type"] in ("war", "2v2") and await rule(row["guild_id"], "war_participants"):
        confirmed = {int(x) for x in data.get("confirmed_participants", [])}
        for x in data.get("participants", []):
            if int(x) not in ids and int(x) in confirmed: ids.append(int(x))
    return ids

async def get_custom_msg(guild_id, key, default_title, default_desc, default_color=discord.Color.blue()):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT title, description, color FROM custom_messages WHERE guild_id=? AND key=?", (guild_id, key))
        row = await cur.fetchone()
    if row:
        try: color = int(row[2].replace("#", ""), 16) if row[2] else default_color
        except: color = default_color
        return discord.Embed(title=row[0][:256], description=row[1][:4000], color=color)
    return discord.Embed(title=default_title[:256], description=default_desc[:4000], color=default_color)

async def get_custom_val(guild_id, key, default):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM custom_settings WHERE guild_id=? AND key=?", (guild_id, key))
        row = await cur.fetchone()
    return row[0] if row else default

_EMOJI_CACHE, _EMOJI_CACHE_TS, MSG_LIMIT = {}, {}, 1900
async def get_guild_emojis(guild_id: int) -> dict:
    now = time.time()
    if guild_id in _EMOJI_CACHE and now - _EMOJI_CACHE_TS.get(guild_id, 0) < 300: return _EMOJI_CACHE[guild_id]
    emojis = {}
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT slot, emoji FROM log_emojis WHERE guild_id=?", (guild_id,))
        for slot, emoji in await cur.fetchall(): emojis[slot] = emoji
    _EMOJI_CACHE[guild_id], _EMOJI_CACHE_TS[guild_id] = emojis, now
    return emojis

def invalidate_emoji_cache(guild_id: int): _EMOJI_CACHE.pop(guild_id, None); _EMOJI_CACHE_TS.pop(guild_id, None)

def build_divider(emojis: dict) -> str:
    left, right, core = emojis.get("divider_left", ""), emojis.get("divider_right", ""), "●▬▬▬▬▬▬▬▬▬▬▬☭   ☭▬▬▬▬▬▬▬▬▬▬▬●"
    return f"{left}   {core}   {right}" if left and right else f"{left}   {core}" if left else f"{core}   {right}" if right else core

def has_role(member, names) -> bool: return any(r.name.strip().casefold() in {str(n).strip().casefold() for n in names} for r in getattr(member, "roles", []))
def is_admin(member) -> bool: return isinstance(member, discord.Member) and (member.id == owner_id() or member.guild_permissions.administrator)

TEMP_ADMIN_BLOCKED_COMMANDS = {"grant-temp-admin", "revoke-temp-admin", "backup", "restore-backup", "set-cmd-perms", "setchannel", "setrule", "settings", "purge", "sync", "set-ai-default", "set-ai-user", "set-ai-admin"}

async def has_temp_admin(member) -> bool:
    if not member.guild: return False
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT expires_at FROM temp_perms WHERE guild_id=? AND user_id=?", (member.guild.id, member.id))
        row = await cur.fetchone()
        if not row: return False
        try:
            if now_utc() > dt.datetime.fromisoformat(row[0]): await db.execute("DELETE FROM temp_perms WHERE guild_id=? AND user_id=?", (member.guild.id, member.id)); await db.commit(); return False
            return True
        except: return False

async def is_editor(member) -> bool: return is_admin(member) or await has_temp_admin(member) or has_role(member, EDITOR_ROLES)
async def is_editor_for_command(member, cmd_name: str) -> bool: return is_admin(member) or (await has_temp_admin(member) and cmd_name not in TEMP_ADMIN_BLOCKED_COMMANDS) or has_role(member, EDITOR_ROLES)

async def can_review(member, log_type) -> bool:
    if is_admin(member) or await has_temp_admin(member): return True
    ids = await roles_get(member.guild.id, log_type, "review")
    return any(r.id in ids for r in member.roles) if ids else has_role(member, REVIEWER_ROLES)

async def can_submit(member, log_type) -> bool:
    if is_admin(member) or await has_temp_admin(member): return True
    ids = await roles_get(member.guild.id, log_type, "submit")
    return any(r.id in ids for r in member.roles) if ids else has_role(member, TRYOUTER_ROLES) if log_type == "tryout" else True

async def check_cmd_perm(i: discord.Interaction, cmd_name: str) -> bool:
    if not i.guild or not isinstance(i.user, discord.Member): return False
    if is_admin(i.user) or await has_temp_admin(i.user): return True
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT role_id FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, cmd_name))
        role_row = await cur.fetchone()
        cur = await db.execute("SELECT user_id FROM cmd_users WHERE guild_id=? AND cmd_name=?", (i.guild.id, cmd_name))
        allowed_users = [r[0] for r in await cur.fetchall()]
    if not allowed_users and not role_row: return True
    if i.user.id in allowed_users: return True
    return any(r.id == role_row[0] for r in i.user.roles) if role_row else False

async def gate(i: discord.Interaction, cmd_name: str, editor=False) -> bool:
    if not i.guild or not isinstance(i.user, discord.Member): await i.response.send_message("❌ Server only.", ephemeral=True); return False
    if not await check_cmd_perm(i, cmd_name): await i.response.send_message("❌ No permission.", ephemeral=True); return False
    if editor and not await is_editor_for_command(i.user, cmd_name): await i.response.send_message("❌ Admin only.", ephemeral=True); return False
    return True

async def _ensure_members(guild):
    if not getattr(guild, "chunked", False):
        try:
            async for _ in guild.fetch_members(limit=None): pass
        except: pass

async def audit(text, guild):
    ch_id = await cfg_get(guild.id, "all", "audit_channel")
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        try: await ch.send(text[:1990], allowed_mentions=discord.AllowedMentions.none())
        except: pass

async def dm_owner(text, file=None):
    if not owner_id(): return False
    try:
        user = await bot.fetch_user(owner_id())
        if file: await user.send(text, file=file)
        else: await user.send(text)
        return True
    except: return False

async def get_member(guild, user_id): return guild.get_member(user_id) or await guild.fetch_member(user_id)

def parse_log_id(message: discord.Message):
    for p in (LOG_ID_RE, LOG_ID_FALLBACK_RE):
        m = p.search(message.content or "")
        if m: return int(m.group(1))
    return None

def mentions(role_id=None, user_ids=None): return discord.AllowedMentions(roles=[discord.Object(id=role_id)] if role_id else False, users=[discord.Object(id=u) for u in user_ids] if user_ids else False, everyone=False)
def find_crew_role(guild): return discord.utils.find(lambda r: r.name.lower().rstrip().endswith("crew"), guild.roles)
def ladder_roles(guild):
    return [next((r for r in guild.roles if re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE).search(r.name)), None) for name, _ in LADDER]
def dedupe(seq): return list(dict.fromkeys(seq))

async def report_error(i: discord.Interaction, error):
    if isinstance(error, app_commands.CommandOnCooldown):
        try: await i.response.send_message(f"⏳ Wait {error.retry_after:.1f}s.", ephemeral=True)
        except: pass
        return
    print(f"❌ Error: {error}")
    try:
        msg = f"❌ **Error:** `{type(error).__name__}`\n{str(error)[:500]}"
        if i.response.is_done(): await i.followup.send(msg, ephemeral=True)
        else: await i.response.send_message(msg, ephemeral=True)
    except: pass

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
        self.add_view(ReviewView()); self.add_view(TicketView()); self.add_view(CloseTicketView()); self.add_view(EventView()); self.add_view(VoteView())
        try: await self.http.bulk_upsert_global_commands(self.application_id, [])
        except: pass
    async def sync_guild(self, guild):
        try: self.tree.copy_global_to(guild=guild); await self.tree.sync(guild=guild); return True
        except: return False
    async def on_ready(self):
        print(f"Bot logged in as {self.user} (v{BOT_VERSION})")
        if not self._ready_once:
            self._ready_once = True
            for g in self.guilds: await self.sync_guild(g)
        if not maintenance.is_running(): maintenance.start()
    async def on_guild_join(self, guild):
        await self.sync_guild(guild)
        try:
            target = guild.system_channel or next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
            if target: await target.send("👋 Thanks for adding **DragonSlayerBot**!\nUse `/help` for a full manual.")
        except: pass

bot = CrewBot()
@bot.tree.error
async def on_app_command_error(i: discord.Interaction, error): await report_error(i, error)

def clip(v, n=350): return (str(v) if v not in (None, "") else "-")[:n]

DEFAULT_TEMPLATES = {
    "war": "{MOON} **Logged by:** {author}\n{MOON} **Fighters:**\n{ARROW}  {my_team}\n\n{DIVIDER}\n\n{MOON} **{crew} Fighters:**\n{ARROW}  {their_team}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{participants_block}{flags_block}",
    "1v1": "{MOON} **Logged by:** {author}\n{MOON} **Fighter:**\n{ARROW}  {my_fighter}\n\n{DIVIDER}\n\n{MOON} **Opponent:**\n{ARROW}  {opponent}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{flags_block}",
    "2v2": "{MOON} **Logged by:** {author}\n{MOON} **Team:**\n{ARROW}  {my_team}\n\n{DIVIDER}\n\n{MOON} **Enemy Team:**\n{ARROW}  {their_team}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{participants_block}{flags_block}",
    "training": "{MOON} **Logged by:** {author}\n{MOON} **Participants:**\n{ARROW}  {participants}\n\n{DIVIDER}\n\n      • **Score:** {score}\n{notes_line}{youtube_line}{flags_block}",
    "tryout": "{MOON} **Tested by:** {author}\n{MOON} **Candidate:**\n{ARROW}  {candidate} {roblox}\n\n{DIVIDER}\n\n{CROWN} **Result:** {result}\n      • **Score:** {score}\n{notes_line}{youtube_line}{flags_block}"
}

async def get_custom_template(guild_id, log_type): return await raw_setting(guild_id, f"template:{log_type}") or DEFAULT_TEMPLATES.get(log_type, "")
def apply_template(template: str, values: dict) -> str: return "\n".join(line[1:] if line.startswith(" ") and line.strip() else line for line in TEMPLATE_VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template).split("\n"))

def render(log_id, log_type, author_id, data, result, flags=None, status="pending", points=None, reviewer_id=None, reason=None, pts_to=None, custom_template=None, guild_emojis=None):
    guild_emojis, label = guild_emojis or {}, LOG_TYPES.get(log_type, {}).get("label", log_type)
    d = lambda k, default="-": clip(data.get(k, default))
    participants = dedupe([int(x) for x in data.get("participants", [])]) if isinstance(data.get("participants"), list) else []
    confirmed = {int(x) for x in data.get("confirmed_participants", [])}
    template, labels = custom_template or DEFAULT_TEMPLATES.get(log_type, ""), TRYOUT_LABEL if log_type == "tryout" else RESULT_LABEL
    values = {
        "log_id": log_id, "author": f"<@{author_id}>", "score": d("score"), "notes": d("notes"), "youtube_link": data.get("youtube_link", "-"), "my_team": d("my_team"), "their_team": d("their_team"), "crew": d("crew"), "my_fighter": d("my_fighter"), "opponent": d("opponent"),
        "participants": d("participants") if not participants else " ".join(f"<@{p}>" for p in participants), "candidate": data.get("candidate", "-"), "roblox": f"({clip(data['roblox'], 40)})" if data.get("roblox") else "",
        "result": labels.get(result, result) if result else "-", "ANGEL": guild_emojis.get("angel", ""), "MOON": guild_emojis.get("moon", ""), "ARROW": guild_emojis.get("arrow", ""), "CROWN": guild_emojis.get("crown", ""), "DIVIDER": build_divider(guild_emojis),
        "notes_line": f"      • **Notes:** {d('notes')}\n" if data.get("notes") else "", "youtube_line": f"      • **YouTube Video:** <{data['youtube_link']}>\n" if data.get("youtube_link") else "",
        "participants_block": f"\n**Tagged Teammates Verification:**\n" + "".join(f"• <@{p}> — **{'✅ Confirmed' if p in confirmed else '⏳ Unconfirmed'}**\n" for p in participants[:8]) if log_type in ("war", "2v2") and participants else "",
        "flags_block": f"\n⚠️ **Warnings:**\n" + "\n".join("• " + clip(f, 120) for f in flags[:5]) if flags else ""
    }
    header, divider = f"{guild_emojis.get('angel', '')}  **{label}**  {guild_emojis.get('angel', '')}  🆔 `Log #{log_id}`\n\n{build_divider(guild_emojis)}\n\n", build_divider(guild_emojis)
    body = header + apply_template(clip(template, 1200), values) + "\n\n" + divider + "\n"
    footer = f"\n**Status:** {STATUS_LABEL[status]}"
    if points is not None: footer += f" | **Points:** +{points}" + (" → " + " ".join(f"<@{u}>" for u in list(pts_to)[:6]) if pts_to else "")
    if reviewer_id and status != "pending": footer += f"\n**{ {'accepted': 'Accepted by', 'rejected': 'Rejected by', 'revoked': 'Revoked by'}[status] }:** <@{reviewer_id}>"
    if reason: footer += f"\n**Reason:** {clip(reason, 300)}"
    return (body[:max(200, MSG_LIMIT - len(footer) - 1)] + "…" if len(body) + len(footer) > MSG_LIMIT else body) + footer

def progress_bar(total, ladder, width=10):
    nxt = next(((n, p) for n, p in ladder if p > total), None)
    if not nxt: return "█" * width + " MAX"
    prev, filled = max([p for _, p in ladder if p <= total], default=0), max(0, min(width, int((total - max([p for _, p in ladder if p <= total], default=0)) / max(1, nxt[1] - max([p for _, p in ladder if p <= total], default=0)) * width)))
    return f"{'█' * filled}{'░' * (width - filled)} {total}/{nxt[1]} → {nxt[0]}"

def target_index(ladder, total):
    target = -1
    for idx, (_, pts) in enumerate(ladder):
        if total >= pts: target = idx
    return target

async def update_member_rank(guild, member, total, allow_demote=False):
    ladder, roles = await get_ladder(guild.id), ladder_roles(guild)
    target, held = target_index(ladder, total), [idx for idx, r in enumerate(ladder_roles(guild)) if r and r in member.roles]
    current = max(held) if held else -1
    if (allow_demote and target == current and len(held) <= 1) or (not allow_demote and target <= current): return None
    target_role = roles[target] if 0 <= target < len(roles) else None
    if target >= 0 and target_role is None: return None
    try:
        if target_role and target_role not in member.roles: await member.add_roles(target_role, reason="Rank update")
        to_remove = [roles[idx] for idx in held if roles[idx] != target_role]
        if to_remove: await member.remove_roles(*to_remove, reason="Rank update")
    except discord.Forbidden: return None
    if target_role and target > current:
        ch = guild.get_channel(await cfg_get(guild.id, "all", "log_channel")) if await cfg_get(guild.id, "all", "log_channel") else None
        if ch:
            try: await ch.send(f"🎉 {member.mention} promoted to {target_role.mention}!", allowed_mentions=discord.AllowedMentions(users=[member]))
            except: pass
        try: await member.send(f"🎉 Promoted to **{target_role.name}** in **{guild.name}**!")
        except: pass
    return target_role

async def rank_is_above_points(guild, member, total) -> bool:
    held = [idx for idx, r in enumerate(ladder_roles(guild)) if r and r in member.roles]
    return bool(held) and max(held) > target_index(await get_ladder(guild.id), total)

async def bulk_update_ranks(guild, allow_demote=False):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM members WHERE guild_id=?", (guild.id,))
        rows = await cur.fetchall()
    changed = 0
    for uid, pts in rows:
        member = guild.get_member(uid)
        if member and await update_member_rank(guild, member, pts, allow_demote): changed += 1
        await asyncio.sleep(0.25)
    return changed

# ================================================================ AI Core
class AIContext:
    def __init__(self, guild, user, can_use_tools, channel=None): self.guild, self.user, self.can_use_tools, self.channel = guild, user, can_use_tools, channel

async def _ai_can_sensitive(ctx):
    if is_admin(ctx.user): return True
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM ai_admin_users WHERE guild_id=? AND user_id=?", (ctx.guild.id, ctx.user.id))
        return bool(await cur.fetchone())

async def _web_search(query: str) -> str:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}", headers={"User-Agent": "Mozilla/5.0"}) as r:
                html = await r.text()
                results = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>', html)
                return "\n\n".join(f"[{re.sub(r'<.*?>', '', t)}]({l})" for l, t in results[:4]) or "No results."
    except Exception as e: return f"❌ Search error: {e}"

async def _exec_ai_tool(ctx, name, args):
    if not ctx.can_use_tools: return "❌ عذراً، ليس لديك الرتبة المطلوبة لتوجيه أوامر إدارية."
    if TOOL_META.get(name, {}).get("requires_admin") and not await _ai_can_sensitive(ctx): return "❌ DENIED."
    g, m_q, r_q, c_q = ctx.guild, args.get("member_query", ""), args.get("role_query", ""), args.get("channel_query", "")
    try:
        if name == "search_web": return await _web_search(args.get("query", ""))
        if name == "get_member_info":
            m = _find_member(g, m_q)
            if not m: return "❌ Member not found."
            async with aiosqlite.connect(DB) as db:
                cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (g.id, m.id))
                pts = (await cur.fetchone() or [0])[0]
            return f"👤 **{m.display_name}**\n• Points: {pts}"
        if name == "get_server_info": return f"📊 **{g.name}**\n• Members: {g.member_count}\n• Roles: {len(g.roles)}"
        if name == "create_role": return f"✅ Created **{(await g.create_role(name=args['name'], color=_hex_color(args.get('color_hex')))).mention}**"
        if name == "delete_role":
            r = _find_role(g, r_q)
            if r and not r.is_default() and not r.managed and r.position < g.me.top_role.position: await r.delete(); return f"✅ Deleted **{r.name}**"
            return "❌ Cannot delete."
        if name == "create_channel": return f"✅ Created {(await g.create_text_channel(name=args['name'])).mention}"
        if name == "delete_channel":
            c = _find_channel(g, c_q)
            if c: await c.delete(); return f"✅ Deleted **{c.name}**"
            return "❌ Channel not found."
        if name == "give_role":
            m, r = _find_member(g, m_q), _find_role(g, r_q)
            if m and r: await m.add_roles(r); return f"✅ Gave **{r.name}** to **{m.display_name}**"
            return "❌ Not found."
        if name == "timeout_member":
            m = _find_member(g, m_q)
            if m: await m.timeout(timedelta(minutes=int(args.get("duration_minutes", 10)))); return f"✅ Muted **{m.display_name}**"
            return "❌ Not found."
        if name == "kick_member":
            m = _find_member(g, m_q)
            if m: await m.kick(); return f"✅ Kicked **{m.display_name}**"
            return "❌ Not found."
        if name == "purge_messages":
            await ctx.channel.purge(limit=min(int(args.get("amount", 10)), 100)); return "✅ Purged."
        return f"✅ {name} executed."
    except Exception as e: return f"❌ {type(e).__name__}: {str(e)[:100]}"

def _find_member(g, q):
    q = str(q).strip().lstrip("<@!").rstrip(">")
    return g.get_member(int(q)) if q.isdigit() else next((m for m in g.members if q.lower() in [m.name.lower(), m.display_name.lower()]), None)
def _find_role(g, q):
    q = str(q).strip().lstrip("<@&").rstrip(">")
    return g.get_role(int(q)) if q.isdigit() else next((r for r in g.roles if q.lower() in r.name.lower()), None)
def _find_channel(g, q):
    q = str(q).strip().lstrip("<#").rstrip(">")
    return g.get_channel(int(q)) if q.isdigit() else next((c for c in g.channels if q.lower() in c.name.lower()), None)
def _hex_color(hx):
    try: return discord.Color(int(str(hx).lstrip("#"), 16))
    except: return discord.Color.default()

async def _ai_call(messages, image_url=None, is_vision=False):
    if not AI_API_KEY: return {"error": "AI_API_KEY missing."}
    url, model = _PROVIDERS["groq_vision" if is_vision else AI_PROVIDER]
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    if is_vision and image_url: messages[-1] = {"role": "user", "content": [{"type": "text", "text": messages[-1]["content"]}, {"type": "image_url", "image_url": {"url": image_url}}]}
    payload = {"model": model, "messages": messages, "tools": TOOLS if not is_vision else [], "tool_choice": "auto" if not is_vision else "none", "temperature": 0.6, "max_tokens": 4000}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as s:
            async with s.post(url, headers=headers, json=payload) as r:
                return {"message": (await r.json())["choices"][0]["message"]} if r.status == 200 else {"error": f"HTTP {r.status}: {(await r.text())[:200]}"}
    except Exception as e: return {"error": str(e)}

async def _run_ai(ctx, text, image_url=None):
    lang = detect_language(text)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT history FROM ai_memory WHERE guild_id=? AND user_id=?", (ctx.guild.id, ctx.user.id))
        row = await cur.fetchone()
    history = json.loads(row[0]) if row else [{"role": "system", "content": _build_system_prompt(lang, ctx.can_use_tools)}]
    if not any(lang in str(h.get("content", "")) for h in history if h["role"] == "system"): history = [{"role": "system", "content": _build_system_prompt(lang, ctx.can_use_tools)}]
    history.append({"role": "user", "content": text})
    if len(history) > 20: history = [history[0]] + history[-18:]
    for _ in range(4):
        res = await _ai_call(history, image_url, bool(image_url))
        if "error" in res: return f"❌ **AI Error:**\n```\n{res['error']}\n```"
        msg, tcs = res["message"], res["message"].get("tool_calls") or []
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
            except: args = {}
            history.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": (await _exec_ai_tool(ctx, fn["name"], args))[:1900]})
    return "⚠️ Tool loop stopped."

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT reply FROM auto_replies WHERE guild_id=? AND trigger_word=?", (message.guild.id, message.content.lower()))
        auto_row = await cur.fetchone()
        if auto_row: await message.reply(auto_row[0]); return
    if bot.user in message.mentions:
        content = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if not content and not message.attachments: return await message.reply("أهلاً بك! كيف يمكنني مساعدتك؟ 👋")
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT role_id FROM ai_mention_roles WHERE guild_id=?", (message.guild.id,))
            allowed = [r[0] for r in await cur.fetchall()]
        is_allowed = is_admin(message.author) or any(r.id in allowed for r in message.author.roles)
        ctx, image_url = AIContext(message.guild, message.author, is_allowed, message.channel), None
        if message.attachments:
            for att in message.attachments:
                if att.content_type and "image" in att.content_type: image_url = att.url; break
        async with message.channel.typing():
            reply = await _run_ai(ctx, content or "حلل الصورة دي", image_url)
            for j in range(0, min(len(reply), 5700), 1900): await message.reply(reply[j:j+1900])
    await bot.process_commands(message)

# ================================================================ Logging & Modals
class LogModal(discord.ui.Modal):
    def __init__(self, log_type, result, files, extra, defaults=None, parent_id=None):
        super().__init__(title=LOG_TYPES[log_type]["label"][:45])
        self.log_type, self.result, self.files, self.extra, self.parent_id, self.inputs = log_type, result, files, extra, parent_id, {}
        for key, label, long, required in LOG_TYPES[log_type]["fields"]:
            dv = (defaults or {}).get(key)
            ti = discord.ui.TextInput(label=label[:45], required=required, max_length=350 if long else 100, default=str(dv)[:350 if long else 100] if isinstance(dv, (str, int)) and str(dv) else None, style=discord.TextStyle.paragraph if long else discord.TextStyle.short)
            self.add_item(ti); self.inputs[key] = ti
    async def on_error(self, interaction, error): await report_error(interaction, error)
    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        if len(self.files) > MAX_FILES_COUNT: return await i.followup.send(f"❌ Too many files (max {MAX_FILES_COUNT}).", ephemeral=True)
        total_size = 0
        for a in self.files:
            if isinstance(a, discord.Attachment):
                if os.path.splitext(a.filename)[1].lower() not in ALLOWED_EXTENSIONS: return await i.followup.send(f"❌ Bad ext: {a.filename}", ephemeral=True)
                if a.size > MAX_FILE_SIZE: return await i.followup.send(f"❌ File too big: {a.filename}", ephemeral=True)
                total_size += a.size
        if total_size > MAX_TOTAL_FILES_SIZE: return await i.followup.send("❌ Total size too big.", ephemeral=True)
        season, limit = await get_current_season(i.guild.id), await rule(i.guild.id, "daily_limit")
        blobs, proof_urls = [(a, await a.read()) for a in self.files if isinstance(a, discord.Attachment)], [a.url if isinstance(a, discord.Attachment) else a for a in self.files]
        hashes, flags = await inspect_blobs(blobs, i.guild.id)
        data = {k: v.value.strip() for k, v in self.inputs.items()}; data.update(self.extra)
        if proof_urls: data["proof_urls"] = proof_urls
        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if limit and (await (await db.execute("SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? AND status IN ('pending','accepted')", (i.guild.id, i.user.id, (now_utc() - timedelta(hours=24)).isoformat()))).fetchone())[0] >= limit:
                    await db.rollback(); return await i.followup.send("❌ Daily limit hit.", ephemeral=True)
                cur = await db.execute("INSERT INTO logs(guild_id, type, author_id, result, data, flags, created_at, season_id, parent_id) VALUES(?,?,?,?,?,?,?,?,?)", (i.guild.id, self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False), json.dumps(flags, ensure_ascii=False), now_utc().isoformat(), season, self.parent_id))
                log_id = cur.lastrowid
                for h in hashes: await db.execute("INSERT OR IGNORE INTO hashes VALUES(?,?)", (h, log_id))
                await db.commit()
            except Exception as e: await db.rollback(); return await i.followup.send("❌ DB Error.", ephemeral=True)
        ch = i.guild.get_channel(await cfg_get(i.guild.id, self.log_type, "review_channel") or 0)
        if not ch: return await i.followup.send("❌ Review channel not set.", ephemeral=True)
        ping_id, custom_tmpl, emojis = await cfg_get(i.guild.id, self.log_type, "review_ping"), await get_custom_template(i.guild.id, self.log_type), await get_guild_emojis(i.guild.id)
        text = render(log_id, self.log_type, i.user.id, data, self.result, flags, custom_template=custom_tmpl, guild_emojis=emojis)
        try: sent = await ch.send(content=(f"<@&{ping_id}>\n" if ping_id else "") + text, view=ReviewView(), allowed_mentions=mentions(ping_id), files=[discord.File(io.BytesIO(b), a.filename) for a, b in blobs] if blobs else None)
        except discord.HTTPException: sent = await ch.send(content=(f"<@&{ping_id}>\n" if ping_id else "") + text, view=ReviewView(), allowed_mentions=mentions(ping_id))
        await db_exec("UPDATE logs SET review_channel=?, review_msg=? WHERE id=?", (ch.id, sent.id, log_id))
        await i.followup.send(f"✅ Log `#{log_id}` submitted.", ephemeral=True)

async def inspect_blobs(blobs, guild_id):
    hashes, flags = [], []
    for att, data in blobs:
        h = hashlib.sha256(data).hexdigest(); hashes.append(h)
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT h.log_id FROM hashes h JOIN logs l ON l.id=h.log_id WHERE h.hash=? AND l.guild_id=? AND l.status NOT IN ('rejected') LIMIT 1", (h, guild_id))
            if (row := await cur.fetchone()): flags.append(f"{att.filename}: same file in Log #{row[0]}")
    return hashes, flags

async def start_log(i: discord.Interaction, log_type, result, files, extra=None):
    if not await gate(i, f"log-{log_type}"): return
    if not await can_submit(i.user, log_type): return await i.response.send_message("❌ No submit permission.", ephemeral=True)
    files = [f for f in files if f is not None]
    if log_type == "war" and len(files) < 2: return await i.response.send_message("❌ War requires 2 files.", ephemeral=True)
    if log_type != "war" and len(files) < 1: return await i.response.send_message("❌ Attach at least 1 file.", ephemeral=True)
    if not await cfg_get(i.guild.id, log_type, "review_channel"): return await i.response.send_message("❌ Review channel not set.", ephemeral=True)
    if (now_utc() - i.user.created_at).days < 7 and not is_admin(i.user): return await i.response.send_message("❌ Account too new.", ephemeral=True)
    await i.response.send_modal(LogModal(log_type, result, files, extra or {}))

@bot.tree.command(name="log-war", description="Log a Crew War")
@app_commands.choices(result=[app_commands.Choice(name="Win", value="win"), app_commands.Choice(name="Loss", value="loss")])
async def log_war(i: discord.Interaction, result: app_commands.Choice[str], file1: discord.Attachment, file2: discord.Attachment, file3: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None, player1: Optional[discord.Member] = None, player2: Optional[discord.Member] = None, player3: Optional[discord.Member] = None):
    ids = [p.id for p in (player1, player2, player3) if p and not p.bot and p.id != i.user.id]
    extra = {"participants": ids}
    if youtube_link: extra["youtube_link"] = youtube_link
    await start_log(i, "war", result.value, [file1, file2, file3], extra)

@bot.tree.command(name="log-1v1", description="Log a 1v1 fight")
@app_commands.choices(result=[app_commands.Choice(name="Win", value="win"), app_commands.Choice(name="Loss", value="loss")])
async def log_1v1(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None):
    extra = {"youtube_link": youtube_link} if youtube_link else {}
    await start_log(i, "1v1", result.value, [proof1, proof2], extra)

@bot.tree.command(name="log-2v2", description="Log a 2v2 fight")
@app_commands.choices(result=[app_commands.Choice(name="Win", value="win"), app_commands.Choice(name="Loss", value="loss")])
async def log_2v2(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None, teammate: Optional[discord.Member] = None):
    extra = {"participants": [teammate.id] if teammate and not teammate.bot and teammate.id != i.user.id else []}
    if youtube_link: extra["youtube_link"] = youtube_link
    await start_log(i, "2v2", result.value, [proof1, proof2], extra)

@bot.tree.command(name="log-training", description="Log training")
async def log_training(i: discord.Interaction, proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None): await start_log(i, "training", None, [proof1, proof2])

@bot.tree.command(name="log-tryout", description="Log a tryout")
@app_commands.choices(result=[app_commands.Choice(name="Passed", value="win"), app_commands.Choice(name="Failed", value="loss")])
async def log_tryout(i: discord.Interaction, candidate: discord.Member, result: app_commands.Choice[str], proof: discord.Attachment):
    if candidate.bot or candidate.id == i.user.id: return await i.response.send_message("❌ Invalid candidate.", ephemeral=True)
    await start_log(i, "tryout", result.value, [proof], {"candidate_id": candidate.id, "candidate": candidate.mention, "tester_id": i.user.id})
# ================================================================ نظام المراجعة
class EditLogModal(discord.ui.Modal):
    def __init__(self, log_id, log_type, result, files, extra, defaults=None):
        super().__init__(title=f"Edit Log #{log_id}")
        self.log_id, self.log_type, self.result, self.files, self.extra, self.inputs = log_id, log_type, result, files, extra, {}
        for key, label, long, required in LOG_TYPES[log_type]["fields"]:
            dv = (defaults or {}).get(key)
            ti = discord.ui.TextInput(label=label[:45], required=required, max_length=350 if long else 100, default=str(dv) if isinstance(dv, (str, int)) and str(dv) else None, style=discord.TextStyle.paragraph if long else discord.TextStyle.short)
            self.add_item(ti); self.inputs[key] = ti
    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        row = await get_log(self.log_id)
        if not row or row["guild_id"] != i.guild.id or row["author_id"] != i.user.id or row["status"] != "pending": return await i.followup.send("❌ Cannot edit this log.", ephemeral=True)
        data = {k: v.value.strip() for k, v in self.inputs.items()}; data.update(self.extra)
        if old_urls := json.loads(row["data"] or "{}").get("proof_urls"): data["proof_urls"] = old_urls
        await db_exec("UPDATE logs SET data=? WHERE id=? AND status='pending'", (json.dumps(data), self.log_id))
        try:
            ch = i.guild.get_channel(row["review_channel"])
            msg = await ch.fetch_message(row["review_msg"])
            await msg.edit(content=render(self.log_id, row["type"], row["author_id"], data, row["result"], json.loads(row["flags"] or "[]"), custom_template=await get_custom_template(i.guild.id, row["type"]), guild_emojis=await get_guild_emojis(i.guild.id)))
        except: pass
        await i.followup.send("✅ Log updated.", ephemeral=True)

@bot.tree.command(name="edit-log", description="Edit a pending log")
async def edit_log(i: discord.Interaction, log_id: int):
    row = await get_log(log_id)
    if not row or row["author_id"] != i.user.id or row["status"] != "pending": return await i.response.send_message("❌ Log not found or not pending.", ephemeral=True)
    data, files = json.loads(row["data"] or "{}"), []
    try:
        if row["review_channel"] and row["review_msg"]: files = list((await i.guild.get_channel(row["review_channel"]).fetch_message(row["review_msg"])).attachments)
    except: pass
    if not files and data.get("proof_urls"): files = data["proof_urls"]
    await i.response.send_modal(EditLogModal(log_id, row["type"], row["result"], files, {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants", "youtube_link") if k in data}, data))

@bot.tree.command(name="resubmit", description="Resubmit a rejected log")
async def resubmit(i: discord.Interaction, log_id: int):
    row = await get_log(log_id)
    if not row or row["author_id"] != i.user.id or row["status"] != "rejected": return await i.response.send_message("❌ Log not found or not rejected.", ephemeral=True)
    data, files = json.loads(row["data"] or "{}"), []
    try:
        if row["review_channel"] and row["review_msg"]: files = list((await i.guild.get_channel(row["review_channel"]).fetch_message(row["review_msg"])).attachments)
    except: pass
    if not files and data.get("proof_urls"): files = data["proof_urls"]
    if not files: return await i.response.send_message("❌ Missing attachments.", ephemeral=True)
    await i.response.send_modal(LogModal(row["type"], row["result"], files, {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants", "youtube_link") if k in data}, data, log_id))

@bot.tree.command(name="revoke-log", description="Revoke an accepted log")
async def revoke_log(i: discord.Interaction, log_id: int, reason: str):
    if not await gate(i, "revoke-log", editor=True): return
    row = await get_log(log_id)
    if not row or row["status"] != "accepted": return await i.response.send_message("❌ Log not found or not accepted.", ephemeral=True)
    await db_exec("UPDATE logs SET status='revoked', reason=?, reviewer_id=? WHERE id=?", (reason, i.user.id, log_id))
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, SUM(points) FROM point_events WHERE guild_id=? AND log_id=? AND type='log' GROUP BY user_id", (i.guild.id, log_id))
        for uid, p in await cur.fetchall():
            await db.execute("UPDATE members SET points = points - ? WHERE guild_id=? AND user_id=?", (p, i.guild.id, uid))
            await db.execute("INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?,?)", (i.guild.id, log_id, uid, -p, now_utc().isoformat(), "revoke", row["season_id"]))
        await db.commit()
    await i.response.send_message(f"✅ Log #{log_id} revoked.", ephemeral=True)

async def resolve_log(i: discord.Interaction, need_pending=True):
    log_id = parse_log_id(i.message)
    row = await get_log(log_id) if log_id else None
    if not row or row["guild_id"] != i.guild.id or (need_pending and row["status"] != "pending"): return None
    return row

async def accept_log(i: discord.Interaction):
    row = await resolve_log(i)
    if not row or row["author_id"] == i.user.id or not await can_review(i.user, row["type"]): return await i.response.send_message("❌ Cannot review.", ephemeral=True)
    await i.response.defer()
    pts = await points_for(i.guild.id, row["type"], row["result"])
    recips = await recipients_for(row, json.loads(row["data"] or "{}"))
    stamp = now_utc().isoformat()
    data = json.loads(row["data"] or "{}")
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("UPDATE logs SET status='accepted', points=?, reviewer_id=?, reviewed_at=? WHERE id=? AND status='pending'", (pts, i.user.id, stamp, row["id"]))
            if cur.rowcount == 0: await db.rollback(); return await i.followup.send("❌ Already reviewed.", ephemeral=True)
            for uid in recips:
                await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (i.guild.id, uid, pts, pts))
                await db.execute("INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) VALUES(?,?,?,?,?,?,?)", (i.guild.id, row["id"], uid, pts, stamp, "log", row["season_id"]))
            await db.commit()
        except: await db.rollback(); return await i.followup.send("❌ DB Error.", ephemeral=True)
    pub_id = await cfg_get(i.guild.id, row["type"], "log_channel")
    if pub_id and (log_ch := i.guild.get_channel(pub_id)):
        ping = await cfg_get(i.guild.id, row["type"], "log_ping")
        text = render(row["id"], row["type"], row["author_id"], data, row["result"], None, "accepted", pts, i.user.id, pts_to=recips, custom_template=await get_custom_template(i.guild.id, row["type"]), guild_emojis=await get_guild_emojis(i.guild.id))
        try: await log_ch.send(content=(f"<@&{ping}>\n" if ping else "") + text, files=[await a.to_file() for a in i.message.attachments], allowed_mentions=mentions(ping))
        except: pass
    try:
        reviewed = render(row["id"], row["type"], row["author_id"], data, row["result"], None, "accepted", pts, i.user.id, pts_to=recips, custom_template=await get_custom_template(i.guild.id, row["type"]), guild_emojis=await get_guild_emojis(i.guild.id))
        await i.message.edit(content=reviewed, view=None)
    except: pass
    for uid in recips:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, uid))
            total = (await cur.fetchone() or [0])[0]
            if total and (member := i.guild.get_member(uid)): await update_member_rank(i.guild, member, total)
    await i.followup.send("✅ Accepted.", ephemeral=True)

class RejectModal(discord.ui.Modal, title="Reject Log"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)
    def __init__(self, log_id, msg): super().__init__(); self.log_id, self.message = log_id, msg
    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        await db_exec("UPDATE logs SET status='rejected', reason=?, reviewer_id=? WHERE id=? AND status='pending'", (self.reason.value, i.user.id, self.log_id))
        try: await self.message.edit(content="❌ **Rejected**\nReason: " + self.reason.value, view=None)
        except: pass
        await i.followup.send("✅ Rejected.", ephemeral=True)

class ReviewView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, custom_id="log:accept")
    async def accept(self, i: discord.Interaction, button: discord.ui.Button): await accept_log(i)
    @discord.ui.button(label="Reject", emoji="❌", style=discord.ButtonStyle.danger, custom_id="log:reject")
    async def reject(self, i: discord.Interaction, button: discord.ui.Button):
        row = await resolve_log(i)
        if row and row["author_id"] != i.user.id: await i.response.send_modal(RejectModal(row["id"], i.message))
        else: await i.response.send_message("❌ Cannot reject.", ephemeral=True)
    @discord.ui.button(label="Confirm", emoji="✋", style=discord.ButtonStyle.primary, custom_id="log:confirm_part")
    async def confirm(self, i: discord.Interaction, button: discord.ui.Button):
        row = await resolve_log(i)
        if not row: return
        data = json.loads(row["data"] or "{}")
        if i.user.id not in data.get("participants", []): return await i.response.send_message("❌ You are not tagged.", ephemeral=True)
        conf = data.get("confirmed_participants", [])
        if i.user.id in conf: return await i.response.send_message("❌ Already confirmed.", ephemeral=True)
        conf.append(i.user.id); data["confirmed_participants"] = conf
        await db_exec("UPDATE logs SET data=? WHERE id=?", (json.dumps(data), row["id"]))
        await i.response.send_message("✅ Confirmed.", ephemeral=True)

@bot.tree.command(name="pending", description="List pending logs")
async def pending(i: discord.Interaction):
    if not await gate(i, "pending", editor=True): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id, type, author_id, created_at FROM logs WHERE status='pending' AND guild_id=?", (i.guild.id,))
        rows = await cur.fetchall()
    await i.response.send_message("\n".join(f"**#{r[0]}** — {LOG_TYPES.get(r[1], {}).get('label', r[1])} — <@{r[2]}>" for r in rows[:15]) or "✅ No pending.", ephemeral=True)

# ================================================================ الإعدادات (Config)
@bot.tree.command(name="setchannel", description="Set review/log channels")
@app_commands.choices(kind=[app_commands.Choice(name="Review", value="review_channel"), app_commands.Choice(name="Log", value="log_channel"), app_commands.Choice(name="Audit", value="audit_channel")])
async def setchannel(i: discord.Interaction, kind: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None):
    if not await gate(i, "setchannel", editor=True): return
    await cfg_set(i.guild.id, "all", kind.value, channel.id if channel else None)
    await i.response.send_message(f"✅ {kind.name} channel set.", ephemeral=True)

@bot.tree.command(name="setrule", description="Change a rule")
@app_commands.choices(rule_name=[app_commands.Choice(name=k, value=k) for k in RULE_DEFAULTS])
async def setrule(i: discord.Interaction, rule_name: app_commands.Choice[str], value: int):
    if not await gate(i, "setrule", editor=True): return
    await set_setting(i.guild.id, f"rule:{rule_name.value}", value)
    await i.response.send_message(f"✅ Rule updated.", ephemeral=True)

@bot.tree.command(name="set-log-points", description="Set log points")
@app_commands.choices(log=[app_commands.Choice(name=k, value=k) for k in POINT_LABELS])
async def set_log_points(i: discord.Interaction, log: app_commands.Choice[str], points: app_commands.Range[int, 0, 1000]):
    if not await gate(i, "set-log-points", editor=True): return
    await set_setting(i.guild.id, "points:" + log.value, points)
    await i.response.send_message(f"✅ **{log.name}** = {points} points.", ephemeral=True)

@bot.tree.command(name="set-rank-points", description="Set rank threshold")
@app_commands.choices(rank=[app_commands.Choice(name=n, value=n) for n, _ in LADDER])
async def set_rank_points(i: discord.Interaction, rank: app_commands.Choice[str], points: app_commands.Range[int, 0, 100000]):
    if not await gate(i, "set-rank-points", editor=True): return
    await i.response.defer(ephemeral=True)
    await set_setting(i.guild.id, "rank:" + rank.value, points)
    changed = await bulk_update_ranks(i.guild)
    await i.followup.send(f"✅ **{rank.name}** = {points} points. Promotions applied: {changed}.", ephemeral=True)

@bot.tree.command(name="sync-ranks", description="Re-check ranks from points")
async def sync_ranks(i: discord.Interaction, member: Optional[discord.Member] = None, demote: bool = False):
    if not await gate(i, "sync-ranks", editor=True): return
    await i.response.defer(ephemeral=True)
    if member:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
            row = await cur.fetchone()
        changed = 1 if await update_member_rank(i.guild, member, row[0] if row else 0, demote) else 0
    else: changed = await bulk_update_ranks(i.guild, allow_demote=demote)
    await i.followup.send(f"✅ Done. Rank changes: {changed}.", ephemeral=True)

@bot.tree.command(name="settings", description="Show server settings")
async def settings_cmd(i: discord.Interaction):
    if not await gate(i, "settings", editor=True): return
    embed = discord.Embed(title=f"⚙️ Server Settings — {BOT_VERSION}", color=discord.Color.blue())
    embed.add_field(name="Rules", value="\n".join(f"• `{k}`: {await rule(i.guild.id, k)}" for k in RULE_DEFAULTS)[:1024], inline=False)
    await i.response.send_message(embed=embed, ephemeral=True)

# إصلاح دالة الإكمال التلقائي هنا
async def cmd_name_autocomplete(i: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return [app_commands.Choice(name=x, value=x) for x in ALL_BOT_COMMANDS if current.lower() in x.lower()][:25]

@bot.tree.command(name="set-cmd-perms", description="Control command permissions")
@app_commands.autocomplete(command=cmd_name_autocomplete)
@app_commands.choices(action=[app_commands.Choice(name="Add Role", value="add_role"), app_commands.Choice(name="Remove Role", value="remove_role"), app_commands.Choice(name="Add User", value="add_user"), app_commands.Choice(name="Remove User", value="remove_user"), app_commands.Choice(name="Clear All", value="clear")])
async def set_cmd_perms(i: discord.Interaction, command: str, action: app_commands.Choice[str], role: Optional[discord.Role] = None, member: Optional[discord.Member] = None):
    if not await gate(i, "set-cmd-perms", editor=True): return
    async with aiosqlite.connect(DB) as db:
        if action.value == "add_role" and role: await db.execute("INSERT INTO cmd_roles VALUES(?,?,?) ON CONFLICT(guild_id, cmd_name) DO UPDATE SET role_id=excluded.role_id", (i.guild.id, command, role.id))
        elif action.value == "remove_role": await db.execute("DELETE FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
        elif action.value == "add_user" and member: await db.execute("INSERT OR IGNORE INTO cmd_users VALUES(?,?,?)", (i.guild.id, command, member.id))
        elif action.value == "remove_user" and member: await db.execute("DELETE FROM cmd_users WHERE guild_id=? AND cmd_name=? AND user_id=?", (i.guild.id, command, member.id))
        elif action.value == "clear":
            await db.execute("DELETE FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
            await db.execute("DELETE FROM cmd_users WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
        await db.commit()
    await i.response.send_message(f"✅ Permissions updated for `/{command}`.", ephemeral=True)

@bot.tree.command(name="set-log-emojis", description="Set custom emojis")
@app_commands.choices(slot=[app_commands.Choice(name="👼 Angel", value="angel"), app_commands.Choice(name="🌙 Moon", value="moon"), app_commands.Choice(name="➡️ Arrow", value="arrow"), app_commands.Choice(name="👑 Crown", value="crown")])
async def set_log_emojis(i: discord.Interaction, slot: app_commands.Choice[str], emoji: Optional[str] = None):
    if not await gate(i, "set-log-emojis", editor=True): return
    async with aiosqlite.connect(DB) as db:
        if not emoji: await db.execute("DELETE FROM log_emojis WHERE guild_id=? AND slot=?", (i.guild.id, slot.value))
        else: await db.execute("INSERT INTO log_emojis VALUES(?,?,?) ON CONFLICT(guild_id, slot) DO UPDATE SET emoji=excluded.emoji", (i.guild.id, slot.value, emoji))
        await db.commit()
    invalidate_emoji_cache(i.guild.id)
    await i.response.send_message("✅ Emoji updated.", ephemeral=True)

# ================================================================ النقاط والاقتصاد
@bot.tree.command(name="set-points", description="Set member points")
async def set_points(i: discord.Interaction, member: discord.Member, points: int):
    if not await gate(i, "set-points", editor=True): return
    await db_exec("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points=excluded.points", (i.guild.id, member.id, points))
    await update_member_rank(i.guild, member, points)
    await i.response.send_message(f"✅ {member.mention} points set to {points}.", ephemeral=True)

@bot.tree.command(name="transfer", description="Transfer points")
async def transfer_cmd(i: discord.Interaction, member: discord.Member, amount: int):
    if member.id == i.user.id: return await i.response.send_message("❌ Cannot transfer to yourself.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("UPDATE members SET points = points - ? WHERE guild_id=? AND user_id=? AND points >= ?", (amount, i.guild.id, i.user.id, amount))
        if cur.rowcount == 0:
            await db.rollback()
            return await i.response.send_message("❌ Insufficient points.", ephemeral=True)
        await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (i.guild.id, member.id, amount, amount))
        await db.commit()
    await i.response.send_message(f"✅ Transferred {amount} points to {member.mention}.", ephemeral=True)

@bot.tree.command(name="daily", description="Claim daily bonus")
async def daily_cmd(i: discord.Interaction):
    today = now_utc().date().isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM daily_bonus WHERE guild_id=? AND user_id=? AND last_claimed=?", (i.guild.id, i.user.id, today))
        if await cur.fetchone(): return await i.response.send_message("❌ Already claimed today.", ephemeral=True)
        await db.execute("INSERT OR REPLACE INTO daily_bonus VALUES(?,?,?)", (i.guild.id, i.user.id, today))
        await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,10) ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + 10", (i.guild.id, i.user.id))
        await db.commit()
    await i.response.send_message("✅ Claimed 10 daily points!", ephemeral=True)

@bot.tree.command(name="points", description="Check points")
async def points_cmd(i: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, target.id))
        pts = (await cur.fetchone() or [0])[0]
    await i.response.send_message(embed=discord.Embed(title=f"💰 {target.display_name}", description=f"**Points:** {pts}"), ephemeral=True)

@bot.tree.command(name="leaderboard", description="Top members")
async def leaderboard(i: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM members WHERE guild_id=? ORDER BY points DESC LIMIT 10", (i.guild.id,))
        rows = await cur.fetchall()
    text = "\n".join(f"**{n}.** <@{u}> — {p} pts" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(embed=discord.Embed(title="🏆 Leaderboard", description=text), allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name="profile", description="View profile")
async def profile(i: discord.Interaction, member: Optional[discord.Member] = None):
    t = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, t.id))
        pts = (await cur.fetchone() or [0])[0]
    await i.response.send_message(embed=discord.Embed(title=f"👤 {t.display_name}", description=f"Points: {pts}").set_thumbnail(url=t.display_avatar.url))

@bot.tree.command(name="shop", description="View shop")
async def shop_cmd(i: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT name, price FROM shop_items WHERE guild_id=?", (i.guild.id,))
        items = await cur.fetchall()
    text = "\n".join(f"**{n}** — {p} pts" for n, p in items) or "Empty"
    await i.response.send_message(embed=discord.Embed(title="🏪 Shop", description=text))

@bot.tree.command(name="add-shop-item", description="Add shop item")
async def add_shop_item(i: discord.Interaction, name: str, price: int, role: discord.Role):
    if not await gate(i, "add-shop-item", editor=True): return
    await db_exec("INSERT INTO shop_items(guild_id, name, price, role_id) VALUES(?,?,?,?)", (i.guild.id, name, price, role.id))
    await i.response.send_message("✅ Added.", ephemeral=True)

# ================================================================ التذاكر والغرف الصوتية والفعاليات
class TicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Open", style=discord.ButtonStyle.primary, custom_id="ticket_open")
    async def open(self, i: discord.Interaction, button: discord.ui.Button):
        ch = await i.guild.create_text_channel(name=f"ticket-{i.user.name}", overwrites={i.guild.default_role: discord.PermissionOverwrite(read_messages=False), i.user: discord.PermissionOverwrite(read_messages=True)})
        await i.response.send_message(f"✅ {ch.mention}", ephemeral=True)

class CloseTicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close(self, i: discord.Interaction, button: discord.ui.Button):
        await i.response.send_message("🔒 Closing...", ephemeral=True)
        await i.channel.delete()

@bot.tree.command(name="setup-tickets", description="Setup tickets")
async def setup_tickets(i: discord.Interaction):
    if not await gate(i, "setup-tickets", editor=True): return
    await i.channel.send(embed=discord.Embed(title="🎫 Tickets", description="Click to open."), view=TicketView())
    await i.response.send_message("✅ Done.", ephemeral=True)

class EventView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="✅", style=discord.ButtonStyle.success, custom_id="event_join")
    async def join(self, i: discord.Interaction, button: discord.ui.Button): await i.response.send_message("✅ Joined.", ephemeral=True)

class VoteView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Vote", style=discord.ButtonStyle.primary, custom_id="vote_btn")
    async def vote(self, i: discord.Interaction, button: discord.ui.Button): await i.response.send_message("✅ Voted.", ephemeral=True)

@bot.tree.command(name="event", description="Create event")
async def event_cmd(i: discord.Interaction, title: str, time: str):
    if not await gate(i, "event", editor=True): return
    await i.channel.send(embed=discord.Embed(title=f"📅 {title}", description=f"Time: {time}"), view=EventView())
    await i.response.send_message("✅ Done.", ephemeral=True)

@bot.tree.command(name="start-vote", description="Start vote")
async def start_vote(i: discord.Interaction):
    if not await gate(i, "start-vote", editor=True): return
    await i.channel.send(embed=discord.Embed(title="🏆 Voting"), view=VoteView())
    await i.response.send_message("✅ Done.", ephemeral=True)

@bot.tree.command(name="set-temp-voice", description="Set temp voice trigger")
async def set_temp_voice(i: discord.Interaction, channel: discord.VoiceChannel):
    if not await gate(i, "set-temp-voice", editor=True): return
    await db_exec("INSERT INTO custom_settings(guild_id, key, value) VALUES(?,?,?) ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (i.guild.id, "temp_voice_trigger", str(channel.id)))
    await i.response.send_message("✅ Set.", ephemeral=True)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or not after.channel: return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM custom_settings WHERE guild_id=? AND key='temp_voice_trigger'", (member.guild.id,))
        row = await cur.fetchone()
    if row and str(after.channel.id) == row[0]:
        ch = await member.guild.create_voice_channel(name=f"🔊 {member.display_name}")
        await ch.set_permissions(member, manage_channels=True)
        await member.move_to(ch)

# ================================================================ البطولات والرتب التفاعلية
class TournamentJoinView(discord.ui.View):
    def __init__(self, tournament_id):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id
    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="⚔️")
    async def join_btn(self, i: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT status FROM tournaments WHERE id=?", (self.tournament_id,))
            tour = await cur.fetchone()
            if not tour or tour[0] != "open": return await i.response.send_message("❌ Closed.", ephemeral=True)
            await db.execute("INSERT OR IGNORE INTO tournament_players VALUES(?,?)", (self.tournament_id, i.user.id))
            await db.commit()
        await i.response.send_message("✅ Joined!", ephemeral=True)

@bot.tree.command(name="tournament-create", description="Create tournament")
async def tournament_create(i: discord.Interaction, name: str, max_players: int):
    if not await gate(i, "tournament-create", editor=True): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("INSERT INTO tournaments(guild_id, name, max_players, status) VALUES(?,?,?,?)", (i.guild.id, name, max_players, "open"))
        tour_id = cur.lastrowid
        await db.commit()
    await i.channel.send(embed=discord.Embed(title=f"🏆 {name}", description="Click to join!"), view=TournamentJoinView(tour_id))
    await i.response.send_message("✅ Created.", ephemeral=True)

@bot.tree.command(name="setup-reaction-role", description="Setup reaction role")
async def setup_reaction_role(i: discord.Interaction, title: str, description: str, emoji1: str, role1: discord.Role):
    if not await gate(i, "setup-reaction-role", editor=True): return
    msg = await i.channel.send(embed=discord.Embed(title=title, description=description))
    await msg.add_reaction(emoji1)
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR REPLACE INTO reaction_roles VALUES(?,?,?,?)", (i.guild.id, msg.id, emoji1, role1.id))
        await db.commit()
    await i.response.send_message("✅ Created.", ephemeral=True)

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
            try: await member.add_roles(role)
            except: pass

# ================================================================ أوامر الـ AI الإضافية
@bot.tree.command(name="ai-reset", description="Clear AI memory")
async def ai_reset(i: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM ai_memory WHERE guild_id=? AND user_id=?", (i.guild_id, i.user.id))
        await db.commit()
    await i.response.send_message("✅ Memory cleared.", ephemeral=True)

@bot.tree.command(name="set-ai-mention-roles", description="Set roles allowed to command AI")
@app_commands.choices(action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")])
async def set_ai_mention_roles(i: discord.Interaction, action: app_commands.Choice[str], role: discord.Role):
    if not is_admin(i.user): return await i.response.send_message("❌ Admin only.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        if action.value == "add": await db.execute("INSERT OR IGNORE INTO ai_mention_roles VALUES(?,?)", (i.guild.id, role.id))
        else: await db.execute("DELETE FROM ai_mention_roles WHERE guild_id=? AND role_id=?", (i.guild.id, role.id))
        await db.commit()
    await i.response.send_message(f"✅ {action.name}ed {role.mention}.", ephemeral=True)

@bot.tree.command(name="add-auto-reply", description="Add auto reply")
async def add_auto_reply(i: discord.Interaction, trigger: str, reply: str):
    if not await gate(i, "add-auto-reply", editor=True): return
    await db_exec("INSERT OR REPLACE INTO auto_replies VALUES(?,?,?)", (i.guild.id, trigger.lower(), reply))
    await i.response.send_message("✅ Auto-reply added.", ephemeral=True)
# ================================================================ المهام التلقائية (Background Tasks)
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

def _backup_file(path):
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(path)
    try: src.backup(dst)
    finally:
        dst.close()
        src.close()

def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()

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
        for cmd, desc in items: 
            e.add_field(name=f"`{cmd}`", value=desc, inline=False)
        e.set_footer(text="DragonSlayerBot • Ultimate Edition")
        embeds.append(e)
    return embeds

@bot.tree.command(name="help", description="Show the bot manual / عرض دليل الأوامر")
@app_commands.checks.cooldown(1, 5.0)
async def help_cmd(i: discord.Interaction):
    if not await gate(i, "help"): return
    await i.response.defer(ephemeral=True)
    for e in get_help_embeds()[:5]: 
        await i.followup.send(embed=e, ephemeral=True)

@bot.tree.command(name="send-help-panel", description="Post the manual in a channel / إرسال الشرح")
@app_commands.checks.cooldown(1, 10.0)
async def send_help_panel(i: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not await gate(i, "send-help-panel", editor=True): return
    target = channel or i.channel
    try:
        for e in get_help_embeds(): 
            await target.send(embed=e)
        await i.response.send_message(f"✅ Posted the manual to {target.mention}.", ephemeral=True)
    except discord.HTTPException as e:
        await i.response.send_message(f"❌ Failed: {e}", ephemeral=True)

# ================================================================ أوامر متفرقة (Streak, Achievements, Quests, LFG, H2H, Custom Roles)
@bot.tree.command(name="streak", description="Check your daily login streak")
async def streak_cmd(i: discord.Interaction):
    if not await gate(i, "streak"): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT current_streak, max_streak FROM streaks WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
        row = await cur.fetchone()
    current, max_s = (row[0], row[1]) if row else (0, 0)
    await i.response.send_message(embed=discord.Embed(title="🔥 Streak", description=f"Current: **{current}** | Max: **{max_s}**", color=discord.Color.orange()), ephemeral=True)

@bot.tree.command(name="add-achievement", description="Add achievement")
async def add_achievement(i: discord.Interaction, name: str, description: str, required_points: int, reward_points: int, role: Optional[discord.Role] = None):
    if not await gate(i, "add-achievement", editor=True): return
    await db_exec("INSERT INTO achievements(guild_id, name, description, required_points, reward_points, role_id) VALUES(?,?,?,?,?,?)", (i.guild.id, name, description, required_points, reward_points, role.id if role else None))
    await i.response.send_message(f"✅ Added achievement **{name}**.", ephemeral=True)

@bot.tree.command(name="achievements", description="View achievements")
async def achievements_cmd(i: discord.Interaction):
    if not await gate(i, "achievements"): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT name, description, required_points FROM achievements WHERE guild_id=?", (i.guild.id,))
        items = await cur.fetchall()
    if not items: return await i.response.send_message("⚠️ No achievements yet.", ephemeral=True)
    desc = "".join(f"🏅 **{name}** ({req} pts)\n┗ {d}\n\n" for name, d, req in items)
    await i.response.send_message(embed=discord.Embed(title="🏆 Achievements", description=desc, color=discord.Color.purple()), ephemeral=True)

@bot.tree.command(name="claim-achievement", description="Claim achievement")
async def claim_achievement(i: discord.Interaction):
    if not await gate(i, "claim-achievement"): return
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT a.id, a.name, a.reward_points, a.role_id FROM user_achievements ua JOIN achievements a ON a.id = ua.achievement_id WHERE ua.guild_id=? AND ua.user_id=? AND ua.claimed=0 LIMIT 1", (i.guild.id, i.user.id))
        row = await cur.fetchone()
        if not row:
            await db.rollback()
            return await i.response.send_message("⚠️ Already claimed or no achievements.", ephemeral=True)
        ach_id, name, reward_pts, reward_role = row
        await db.execute("UPDATE user_achievements SET claimed=1 WHERE guild_id=? AND user_id=? AND achievement_id=?", (i.guild.id, i.user.id, ach_id))
        if reward_pts > 0:
            await db.execute("INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (i.guild.id, i.user.id, reward_pts, reward_pts))
        await db.commit()
    if reward_role:
        role = i.guild.get_role(reward_role)
        if role:
            try: await i.user.add_roles(role, reason="Achievement Reward")
            except discord.Forbidden: pass
    await i.response.send_message(f"✅ Claimed **{name}**!", ephemeral=True)

@bot.tree.command(name="add-quest", description="Add daily quest")
async def add_quest(i: discord.Interaction, description: str, reward_points: int, role: Optional[discord.Role] = None):
    if not await gate(i, "add-quest", editor=True): return
    await db_exec("INSERT INTO quests(guild_id, description, target_type, target_amount, reward_points, role_id, is_auto) VALUES(?,?,?,?,?,?,0)", (i.guild.id, description, "log", 1, reward_points, role.id if role else None))
    await i.response.send_message(f"✅ Added quest.", ephemeral=True)

@bot.tree.command(name="quests", description="View daily quests")
async def quests_cmd(i: discord.Interaction):
    if not await gate(i, "quests"): return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id, description, reward_points FROM quests WHERE guild_id=?", (i.guild.id,))
        items = await cur.fetchall()
    if not items: return await i.response.send_message("⚠️ No quests yet.", ephemeral=True)
    desc = "".join(f"📜 {d} — Reward: {r} pts\n" for _id, d, r in items)
    await i.response.send_message(embed=discord.Embed(title="🎯 Daily Quests", description=desc, color=discord.Color.green()), ephemeral=True)

@bot.tree.command(name="h2h", description="Head-to-head stats")
async def h2h_cmd(i: discord.Interaction, member1: discord.Member, member2: discord.Member):
    if not await gate(i, "h2h"): return
    await i.response.defer()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT author_id, result, data FROM logs WHERE guild_id=? AND type='1v1' AND status='accepted'", (i.guild.id,))
        rows = await cur.fetchall()
    m1_wins, m2_wins = 0, 0
    for author_id, result, data_str in rows:
        data = json.loads(data_str or "{}")
        opp_text, fighter_text = str(data.get("opponent", "")), str(data.get("my_fighter", ""))
        if author_id == member1.id and member2.name.lower() in opp_text.lower():
            if result == "win": m1_wins += 1
            elif result == "loss": m2_wins += 1
        elif author_id == member2.id and member1.name.lower() in opp_text.lower():
            if result == "win": m2_wins += 1
            elif result == "loss": m1_wins += 1
    e = discord.Embed(title="⚔️ Head-to-Head", color=discord.Color.red())
    e.add_field(name=member1.display_name, value=f"{m1_wins} Wins")
    e.add_field(name=member2.display_name, value=f"{m2_wins} Wins")
    await i.followup.send(embed=e)

@bot.tree.command(name="set-lfg-roles", description="Set LFG roles")
async def set_lfg_roles(i: discord.Interaction, role: discord.Role):
    if not await gate(i, "set-lfg-roles", editor=True): return
    await db_exec("INSERT OR IGNORE INTO lfg_allowed_roles(guild_id, role_id) VALUES(?,?)", (i.guild.id, role.id))
    await i.response.send_message(f"✅ Allowed role {role.mention} for LFG.", ephemeral=True)

async def lfg_role_autocomplete(i: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT role_id FROM lfg_allowed_roles WHERE guild_id=?", (i.guild.id,))
        role_ids = [r[0] for r in await cur.fetchall()]
    roles = [i.guild.get_role(rid) for rid in role_ids if i.guild.get_role(rid)]
    return [app_commands.Choice(name=r.name, value=str(r.id)) for r in roles if current.lower() in r.name.lower()][:25]

@bot.tree.command(name="lfg", description="Looking for group")
@app_commands.choices(mode=[app_commands.Choice(name="Channel", value="channel"), app_commands.Choice(name="DM", value="dm")])
@app_commands.autocomplete(target_role=lfg_role_autocomplete)
async def lfg_cmd(i: discord.Interaction, mode: app_commands.Choice[str], target_role: str, note: str = "Looking for a team!"):
    if not await gate(i, "lfg"): return
    role = i.guild.get_role(int(target_role))
    if not role: return await i.response.send_message("❌ Role not found.", ephemeral=True)
    embed = discord.Embed(title="🔍 Looking for Group", description=f"**{note}**\n\nFrom: {i.user.mention}", color=discord.Color.blue())
    if mode.value == "channel":
        await i.channel.send(content=role.mention, embed=embed)
        await i.response.send_message("✅ Sent.", ephemeral=True)
    else:
        await i.response.defer(ephemeral=True)
        count = 0
        for member in [m for m in role.members if not m.bot and m.id != i.user.id][:200]:
            try: await member.send(embed=embed); count += 1
            except discord.HTTPException: pass
            await asyncio.sleep(1.0)
        await i.followup.send(f"✅ Sent to {count} members.", ephemeral=True)

@bot.tree.command(name="create-custom-role", description="Create custom role")
@app_commands.choices(color=[app_commands.Choice(name="🔴 Red", value="FF0000"), app_commands.Choice(name="🟢 Green", value="00FF00"), app_commands.Choice(name="🔵 Blue", value="0000FF"), app_commands.Choice(name="🟣 Purple", value="800080")])
async def create_custom_role(i: discord.Interaction, name: str, color: app_commands.Choice[str]):
    if not await gate(i, "create-custom-role"): return
    is_free = is_admin(i.user) or await has_temp_admin(i.user)
    price = 0 if is_free else int(await get_custom_val(i.guild.id, "custom_role_price", 500))
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
        points = (await cur.fetchone() or [0])[0]
    if not is_free and points < price: return await i.response.send_message(f"❌ Need **{price}** pts, you have **{points}**.", ephemeral=True)
    try: role = await i.guild.create_role(name=name, color=discord.Color(int(color.value, 16)), reason="Custom Role")
    except: return await i.response.send_message("❌ Error creating role.", ephemeral=True)
    if not is_free:
        await db_exec("UPDATE members SET points=points-? WHERE guild_id=? AND user_id=?", (price, i.guild.id, i.user.id))
    await i.user.add_roles(role)
    await i.response.send_message(f"✅ Role {role.mention} created!", ephemeral=True)

# ================================================================ التشغيل النهائي
if __name__ == "__main__":
    if not (token := os.getenv("DISCORD_TOKEN")):
        print("❌ DISCORD_TOKEN missing.")
    else:
        threading.Thread(target=run_dummy_server, daemon=True).start()
        bot.run(token)