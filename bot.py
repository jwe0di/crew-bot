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
from typing import Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import config

load_dotenv()

# مسار الداتابيز. على Railway حط Volume واكتب DB_PATH=/data/crewbot.db في Variables
DB = os.getenv("DB_PATH", "crewbot.db")
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)


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

# ---- قواعد قابلة للتعديل بـ /setrule ----
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
"""

MIGRATIONS = [
    ("logs", "guild_id", "INTEGER"),
    ("logs", "review_channel", "INTEGER"),
    ("logs", "review_msg", "INTEGER"),
    ("logs", "public_channel", "INTEGER"),
    ("logs", "public_msg", "INTEGER"),
    ("logs", "reviewed_at", "TEXT"),
    ("logs", "reminded", "INTEGER DEFAULT 0"),
]

CHANNEL_FALLBACK = {
    "review_channel": "REVIEW_CHANNEL_ID",
    "log_channel": "LOG_CHANNEL_ID",
    "audit_channel": "AUDIT_CHANNEL_ID",
}
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


def now_utc():
    return dt.datetime.now(timezone.utc)


def now_ts():
    return int(now_utc().timestamp())


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            cur = await db.execute(f"PRAGMA table_info({table})")
            if col not in [r[1] for r in await cur.fetchall()]:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        for k, v in config.DEFAULT_POINTS.items():
            await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", ("points:" + k, v))
        for name, p in config.LADDER:
            await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", ("rank:" + name, p))
        cur = await db.execute("SELECT guild_id, review_channel, log_channel, audit_channel FROM guild_channels")
        for g, r, l, a in await cur.fetchall():
            for key, v in (("review_channel", r), ("log_channel", l), ("audit_channel", a)):
                if v:
                    await db.execute("INSERT OR IGNORE INTO guild_cfg VALUES(?,?,?,?)", (g, "all", key, v))
        await db.commit()


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
    fb = CHANNEL_FALLBACK.get(key)
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


async def get_ladder():
    return [(n, await get_setting("rank:" + n)) for n, _ in config.LADDER]


async def get_log(log_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM logs WHERE id=?", (log_id,))
        return await cur.fetchone()


async def points_for(log_type, result):
    key = f"{log_type}_{result}" if log_type in ("war", "outside") else log_type
    v = await get_setting("points:" + key)
    return v if v is not None else config.DEFAULT_POINTS.get(key, 0)


async def recipients_for(row, data):
    if row["type"] == "tryout":
        return [int(data.get("tester_id") or row["author_id"])]
    ids = [row["author_id"]]
    if row["type"] == "war" and await rule("war_participants"):
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


def is_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    me = member.guild.me
    return bool(me) and member.top_role > me.top_role


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
    return [discord.utils.find(lambda r, n=n: n.lower() in r.name.lower(), guild.roles)
            for n, _ in config.LADDER]


async def video_age_days(data: bytes) -> Optional[int]:
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(data)
            path = f.name
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_entries", "format_tags=creation_time", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        created = json.loads(out or b"{}").get("format", {}).get("tags", {}).get("creation_time")
        if not created:
            return None
        created = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        return (now_utc() - created).days
    except (FileNotFoundError, ValueError):
        return None
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


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
        if (att.content_type or "").startswith("video"):
            age = await video_age_days(data)
            if age is not None and age > config.MAX_VIDEO_AGE_DAYS:
                flags.append(f"{att.filename}: video is {age} days old")
    return hashes, flags


# ================================================================ شكل اللوج (بالإنجليزية بالكامل)
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
        labels = {"win": "✅ Passed", "loss": "❌ Failed"} if log_type == "tryout" else RESULT_LABEL
        m += f"      {CROWN} **Result:** {labels.get(result, result)}\n"
    m += f"      • **Score:** {d('score')}\n"
    if data.get("notes"):
        m += f"      • **Notes:** {d('notes')}\n"

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


async def apply_promotion(guild, member, total):
    ladder = await get_ladder()
    roles = ladder_roles(guild)
    target = -1
    for idx, (_, pts) in enumerate(ladder):
        if total >= pts:
            target = idx
    if target < 0 or roles[target] is None:
        return None
    current = -1
    for idx, role in enumerate(roles):
        if role and role in member.roles:
            current = idx
    if current >= target:
        return None

    old = [r for r in roles if r and r in member.roles]
    await member.add_roles(roles[target], reason="Automatic point promotion")
    if old:
        await member.remove_roles(*old, reason="Automatic point promotion")

    ch_id = await cfg_get(guild.id, "all", "log_channel")
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        await ch.send(f"🎉 {member.mention} has been promoted to {roles[target].mention}!",
                      allowed_mentions=discord.AllowedMentions(users=[member], roles=False))
    try:
        await member.send(f"🎉 You've been promoted to **{roles[target].name}** in **{guild.name}**!")
    except discord.HTTPException:
        pass
    return roles[target]


async def accept_log(i: discord.Interaction):
    log_id = parse_log_id(i.message)
    row = await get_log(log_id) if log_id else None
    if not row:
        return await i.response.send_message("Log not found in database.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
        return await i.response.send_message("You don't have permission to review this log.", ephemeral=True)
    if row["author_id"] == i.user.id and row["type"] != "tryout":
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
                "INSERT INTO members(user_id, points) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET points = points + ?", (uid, pts, pts))
            await db.execute(
                "INSERT INTO point_events(log_id, user_id, points, created_at) VALUES(?,?,?,?)",
                (log_id, uid, pts, stamp))
            cur = await db.execute("SELECT points FROM members WHERE user_id=?", (uid,))
            totals[uid] = (await cur.fetchone())[0]
        await db.commit()

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

    for uid, total in totals.items():
        try:
            member = await get_member(i.guild, uid)
            await apply_promotion(i.guild, member, total)
        except (discord.NotFound, discord.Forbidden) as e:
            await i.followup.send(f"Log accepted, but promotion failed for <@{uid}>: {e}", ephemeral=True)

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
        row = await get_log(log_id) if log_id else None
        if not row:
            return await i.response.send_message("Log not found in database.", ephemeral=True)
        if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
            return await i.response.send_message("You don't have permission to review this log.", ephemeral=True)
        if row["author_id"] == i.user.id and row["type"] != "tryout":
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
                "INSERT INTO logs(type, author_id, result, data, flags, created_at, guild_id) "
                "VALUES(?,?,?,?,?,?,?)",
                (self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False),
                 json.dumps(flags, ensure_ascii=False), now_utc().isoformat(), i.guild.id))
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
            await db_exec("UPDATE logs SET status='rejected', reason=? WHERE id=?",
                          ("failed to post for review", log_id))
            await i.followup.send(
                f"❌ Failed to post log for review ({reason}). Make sure the review channel is set via /setchannel and the bot has permissions.",
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


async def start_log(i: discord.Interaction, log_type, result, files, extra=None):
    files = [f for f in files if f]
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message("Use this command inside the server.", ephemeral=True)
    if not await can_submit(i.user, log_type):
        return await i.response.send_message("You don't have permission to submit this log type.", ephemeral=True)
    if log_type == "war" and not any((f.content_type or "").startswith("video") for f in files):
        return await i.response.send_message("War log requires at least one video attachment.", ephemeral=True)
    limit = await rule("daily_limit")
    if limit:
        since = (now_utc() - timedelta(hours=24)).isoformat()
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM logs WHERE author_id=? AND created_at>=? "
                "AND status IN ('pending','accepted')", (i.user.id, since))
            n = (await cur.fetchone())[0]
        if n >= limit:
            return await i.response.send_message(
                f"You have reached the daily limit ({limit} logs per 24 hours). Try again later.", ephemeral=True)
    rid = await cfg_get(i.guild.id, log_type, "review_channel")
    if not rid or not i.guild.get_channel(rid):
        return await i.response.send_message(
            "Review channel is not set for this log type. Ask an admin to use /setchannel.", ephemeral=True)
    await i.response.send_modal(LogModal(log_type, result, files, extra or {}))


# ================================================================ الأوامر (بشرح ثنائي اللغة: عربي / إنجليزي)
@bot.tree.command(name="log-war", description="Log a Crew War (video required) | تسجيل حرب كرو")
@app_commands.choices(result=RESULT_CHOICES)
@app_commands.describe(player1="Crew member teammate | لاعب معاك في الطاقم", player2="Second teammate | لاعب ثاني",
                       player3="Third teammate | لاعب ثالث", player4="Fourth teammate | لاعب رابع")
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


@bot.tree.command(name="log-outside", description="Log a 1v1 against an outside/famous player | تسجيل فايت خارجي")
@app_commands.choices(result=RESULT_CHOICES)
async def log_outside(i: discord.Interaction, result: app_commands.Choice[str],
                      proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None):
    await start_log(i, "outside", result.value, [proof1, proof2])


@bot.tree.command(name="log-internal", description="Log an internal fight between crew members | تسجيل فايت داخلي")
async def log_internal(i: discord.Interaction, proof1: discord.Attachment,
                       proof2: Optional[discord.Attachment] = None):
    await start_log(i, "internal", None, [proof1, proof2])


@bot.tree.command(name="log-tryout", description="Log a tryout (mention candidate) | تسجيل تراي أوت")
@app_commands.choices(result=TRYOUT_CHOICES)
@app_commands.describe(candidate="Tryout candidate | المرشح للتراي أوت", result="Passed or Failed | النتيجة",
                       proof="Proof screenshot | صورة الدليل",
                       tester="Tester getting points (Default: you) | المسؤول عن الاختبار")
async def log_tryout(i: discord.Interaction, candidate: discord.Member,
                     result: app_commands.Choice[str], proof: discord.Attachment,
                     tester: Optional[discord.Member] = None):
    tester = tester or i.user
    extra = {"candidate_id": candidate.id, "candidate": candidate.mention, "tester_id": tester.id}
    await start_log(i, "tryout", result.value, [proof], extra)


@bot.tree.command(name="resubmit", description="Fix and resend a rejected log | إعادة إرسال لوج مرفوض")
async def resubmit(i: discord.Interaction, log_id: int):
    row = await get_log(log_id)
    if not row or row["author_id"] != i.user.id:
        return await i.response.send_message("Log not found or not yours.", ephemeral=True)
    if row["status"] != "rejected" or row["type"] not in config.LOG_TYPES:
        return await i.response.send_message("This log is not rejected.", ephemeral=True)
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
            "Could not fetch old attachments. Please create a new log.", ephemeral=True)
    data = json.loads(row["data"])
    extra = {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants") if k in data}
    await i.response.send_modal(LogModal(row["type"], row["result"], files, extra, defaults=data))


@bot.tree.command(name="revoke-log", description="Cancel an accepted log and take points back | إلغاء لوج مقبول وخصم نقاطه")
async def revoke_log(i: discord.Interaction, log_id: int, reason: str):
    row = await get_log(log_id)
    if not row or row["status"] != "accepted":
        return await i.response.send_message("Accepted log not found.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM point_events WHERE log_id=?", (log_id,))
        events = await cur.fetchall()
        for uid, p in events:
            await db.execute("UPDATE members SET points = MAX(0, points - ?) WHERE user_id=?", (p, uid))
        await db.execute("DELETE FROM point_events WHERE log_id=?", (log_id,))
        await db.execute("UPDATE logs SET status='revoked', reason=?, reviewer_id=? WHERE id=?",
                         (reason, i.user.id, log_id))
        await db.commit()

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
    await i.followup.send(f"Successfully revoked Log #{log_id} and deducted points.", ephemeral=True)


@bot.tree.command(name="pending", description="List logs waiting for review | عرض اللوجات المنتظرة للمراجعة")
async def pending(i: discord.Interaction):
    if not isinstance(i.user, discord.Member) or not (is_editor(i.user) or await can_review_any(i.user)):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id, type, author_id, created_at, review_channel, review_msg FROM logs "
            "WHERE status='pending' AND guild_id=? ORDER BY id LIMIT 20", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("No logs waiting for review ✅", ephemeral=True)
    lines = []
    for lid, t, author, created, ch_id, msg_id in rows:
        link = f"https://discord.com/channels/{i.guild.id}/{ch_id}/{msg_id}" if ch_id and msg_id else ""
        lines.append(f"**#{lid}** {config.LOG_TYPES[t]['label']} — <@{author}> — {str(created)[:16]} {link}")
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="setchannel", description="Set channels (review/log/audit) | تحديد قنوات المراجعة واللوج والأوديت")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_CHANNEL)
async def setchannel(i: discord.Interaction, log_type: app_commands.Choice[str],
                     kind: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    t = "all" if kind.value == "audit_channel" else log_type.value
    await cfg_set(i.guild.id, t, kind.value, channel.id if channel else None)
    if channel:
        perms = channel.permissions_for(i.guild.me)
        warn = "" if perms.send_messages else "\n⚠️ Bot does not have send message permissions here!"
        msg = f"✅ **{kind.name}** ({t}) = {channel.mention}{warn}"
        await audit(f"⚙️ {i.user.mention} set {kind.value} ({t}) to {channel.mention}", i.guild)
    else:
        msg = f"🗑️ Cleared **{kind.name}** ({t})."
        await audit(f"⚙️ {i.user.mention} cleared {kind.value} ({t})", i.guild)
    await i.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="setping", description="Set roles to ping | تحديد الرتب التي يتم منشنها")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_PING)
async def setping(i: discord.Interaction, log_type: app_commands.Choice[str],
                  kind: app_commands.Choice[str], role: Optional[discord.Role] = None):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    await cfg_set(i.guild.id, log_type.value, kind.value, role.id if role else None)
    await audit(f"⚙️ {i.user.mention} set {kind.value} ({log_type.value}) to {role.mention if role else 'none'}", i.guild)
    msg = f"✅ **{kind.name}** ({log_type.name}) = {role.mention}" if role else f"🗑️ Cleared **{kind.name}** ({log_type.name})."
    await i.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="setroles", description="Set role permissions for review/submit | تحديد صلاحيات الرتب")
@app_commands.choices(kind=KIND_ROLES, log_type=LOG_TYPE_CHOICES, action=ACTIONS)
async def setroles(i: discord.Interaction, kind: app_commands.Choice[str],
                   log_type: app_commands.Choice[str], action: app_commands.Choice[str], role: discord.Role):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    await roles_edit(i.guild.id, log_type.value, kind.value, role.id, action.value == "add")
    await audit(f"⚙️ {i.user.mention} {action.value} {role.mention} → {kind.value} ({log_type.value})", i.guild)
    verb = "added to" if action.value == "add" else "removed from"
    await i.response.send_message(f"✅ {role.mention} {verb} **{kind.name}** ({log_type.name}).", ephemeral=True)


@bot.tree.command(name="setrule", description="Change a bot rule (limits, reminders...) | تغيير قاعدة من قواعد البوت")
@app_commands.choices(rule_name=RULE_CHOICES)
async def setrule(i: discord.Interaction, rule_name: app_commands.Choice[str],
                  value: app_commands.Range[int, 0, 1000]):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    old = await rule(rule_name.value)
    await set_setting("rule:" + rule_name.value, value)
    await audit(f"⚙️ {i.user.mention} changed rule **{rule_name.value}** from {old} to {value}", i.guild)
    await i.response.send_message(f"✅ **{rule_name.value}** = {value}", ephemeral=True)


@bot.tree.command(name="settings", description="Show all settings and rules | عرض كافة الإعدادات الحالية والقواعد")
async def settings_cmd(i: discord.Interaction):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    g = i.guild.id
    async with aiosqlite.connect(DB) as db:
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
        lines.append(f"- {k}: {await rule(k)}")
    lines.append("\n— = Not set (inherits from Default)")
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@bot.tree.command(name="sync", description="Sync slash commands in this server | مزامنة الأوامر في السيرفر الحالي")
async def sync_cmd(i: discord.Interaction):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    ok = await bot.sync_guild(i.guild)
    await i.followup.send("✅ Synced successfully. Please restart Discord (Ctrl+R)." if ok else "❌ Sync failed, check logs.")


POINT_CHOICES = [app_commands.Choice(name=l, value=k) for k, l in config.POINT_LABELS.items()]
RANK_CHOICES = [app_commands.Choice(name=n, value=n) for n, _ in config.LADDER]


@bot.tree.command(name="set-log-points", description="Set points for a log type | تحديد نقاط نوع لوج معين")
@app_commands.choices(log=POINT_CHOICES)
async def set_log_points(i: discord.Interaction, log: app_commands.Choice[str],
                         points: app_commands.Range[int, 0, 1000]):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    old = await get_setting("points:" + log.value)
    await set_setting("points:" + log.value, points)
    await audit(f"✏️ {i.user.mention} changed points for **{log.name}** from {old} to {points}", i.guild)
    await i.response.send_message(f"Done. **{log.name}** = {points} points (new logs only).", ephemeral=True)


@bot.tree.command(name="set-rank-points", description="Set required points for a rank | تحديد نقاط رتبة معينة")
@app_commands.choices(rank=RANK_CHOICES)
async def set_rank_points(i: discord.Interaction, rank: app_commands.Choice[str],
                          points: app_commands.Range[int, 0, 100000]):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    old = await get_setting("rank:" + rank.value)
    await set_setting("rank:" + rank.value, points)
    await audit(f"✏️ {i.user.mention} changed rank **{rank.name}** from {old} to {points}", i.guild)
    await i.response.send_message(f"Done. **{rank.name}** requires {points} points.", ephemeral=True)


@bot.tree.command(name="set-points", description="Set a member's points balance | تعيين رصيد نقاط عضو يدوياً")
async def set_points(i: discord.Interaction, member: discord.Member,
                     points: app_commands.Range[int, 0, 100000]):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE user_id=?", (member.id,))
        row = await cur.fetchone()
        old = row[0] if row else 0
        await db.execute(
            "INSERT INTO members(user_id, points) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET points=excluded.points", (member.id, points))
        await db.commit()
    await audit(f"✏️ {i.user.mention} changed {member.mention}'s points from {old} to {points}", i.guild)
    try:
        await apply_promotion(i.guild, member, points)
    except discord.Forbidden:
        pass
    await i.followup.send(f"Done. {member.display_name} = {points} points.", ephemeral=True)


@bot.tree.command(name="points", description="Check points (yours or someone else's) | استعراض رصيد النقاط")
async def points_cmd(i: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE user_id=?", (member.id,))
        row = await cur.fetchone()
    total = row[0] if row else 0
    nxt = next(((n, p) for n, p in await get_ladder() if p > total), None)
    msg = f"{member.mention}: **{total}** points."
    msg += f"\nNext rank: **{nxt[0]}** ({nxt[1] - total} left)." if nxt else "\nMax auto rank reached."
    await i.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())


async def top_events(since_iso, types=None, limit=10):
    q = ("SELECT e.user_id, SUM(e.points) AS s FROM point_events e "
         "JOIN logs l ON l.id=e.log_id WHERE e.created_at>=?")
    params = [since_iso]
    if types:
        q += " AND l.type IN (%s)" % ",".join("?" * len(types))
        params += list(types)
    q += " GROUP BY e.user_id ORDER BY s DESC LIMIT ?"
    params.append(limit)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(q, params)
        return await cur.fetchall()


async def weekly_text():
    since = (now_utc() - timedelta(days=7)).isoformat()
    sections = [("🏆 Top points", None, 10), ("⚔️ Wars", ["war"], 3),
                ("🥊 Fights", ["outside", "internal"], 3), ("🎯 Tryouts", ["tryout"], 3)]
    lines = ["**📅 Weekly leaderboard (last 7 days)**"]
    for title, types, n in sections:
        rows = await top_events(since, types, n)
        if rows:
            lines.append(f"\n**{title}**")
            lines += [f"**{k}.** <@{u}> — {p}" for k, (u, p) in enumerate(rows, 1)]
    return "\n".join(lines)[:1990] if len(lines) > 1 else None


@bot.tree.command(name="leaderboard", description="Top members by points | قائمة المتصدرين بالنقاط")
@app_commands.choices(period=PERIOD_CHOICES)
async def leaderboard(i: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
    if period and period.value == "week":
        text = await weekly_text()
        if not text:
            return await i.response.send_message("No points this week yet.")
        return await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM members WHERE points>0 ORDER BY points DESC LIMIT 10")
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("No points yet.")
    text = "\n".join(f"**{n}.** <@{u}> — {p}" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="rates", description="Show log points and rank requirements | عرض معدلات النقاط والرتب")
async def rates(i: discord.Interaction):
    lines = ["**Log points:**"]
    for k, label in config.POINT_LABELS.items():
        lines.append(f"- {label}: {await get_setting('points:' + k)}")
    lines.append("\n**Rank points:**")
    for n, p in await get_ladder():
        lines.append(f"- {n}: {p}")
    lines.append("\n**Rules:**")
    for k in RULE_LABELS:
        lines.append(f"- {k}: {await rule(k)}")
    await i.response.send_message("\n".join(lines), ephemeral=True)


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


@bot.tree.command(name="link", description="Link your Roblox username | ربط حساب روبلوكس")
async def link(i: discord.Interaction, roblox_username: str):
    await i.response.defer(ephemeral=True)
    res = await roblox_lookup(roblox_username.strip())
    if res is None:
        return await i.followup.send("No Roblox user found with this name.")
    if res == "error":
        rid, rname, note = 0, roblox_username.strip(), " (Could not verify name right now)"
    else:
        (rid, rname), note = res, ""
    await db_exec(
        "INSERT INTO links(user_id, roblox_id, roblox_name) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET roblox_id=excluded.roblox_id, roblox_name=excluded.roblox_name",
        (i.user.id, rid, rname))
    await i.followup.send(f"✅ Linked your account to **{rname}**{note}.")


@bot.tree.command(name="profile", description="Member profile card | عرض البطاقة الشخصية للإحصائيات")
async def profile(i: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE user_id=?", (member.id,))
        row = await cur.fetchone()
        cur = await db.execute(
            "SELECT l.type, l.result, COUNT(*) FROM point_events e JOIN logs l ON l.id=e.log_id "
            "WHERE e.user_id=? GROUP BY l.type, l.result", (member.id,))
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
    nxt = next(((n, p) for n, p in await get_ladder() if p > total), None)

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


@bot.tree.command(name="stats", description="War record against other crews | إحصائيات الحروب ضد الكروات الأخرى")
async def stats(i: discord.Interaction, crew: Optional[str] = None):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT data, result FROM logs WHERE type='war' AND status='accepted'")
        rows = await cur.fetchall()
    agg = {}
    for data_s, res in rows:
        d = json.loads(data_s)
        name = (d.get("crew") or "?").strip()
        a = agg.setdefault(name.lower(), {"name": name, "w": 0, "l": 0, "scores": []})
        a["w" if res == "win" else "l"] += 1
        a["scores"].append(f"{d.get('score', '-')} ({'W' if res == 'win' else 'L'})")
    if not agg:
        return await i.response.send_message("No wars logged yet.")
    if crew:
        k = crew.strip().lower()
        found = [a for key, a in agg.items() if k in key]
        if not found:
            return await i.response.send_message("No wars found against this crew.")
        lines = [f"**{a['name']}**: {a['w']}W - {a['l']}L\nLast: " + ", ".join(a["scores"][-5:]) for a in found[:5]]
        return await i.response.send_message("\n\n".join(lines)[:1990])
    tw = sum(a["w"] for a in agg.values())
    tl = sum(a["l"] for a in agg.values())
    top = sorted(agg.values(), key=lambda a: a["w"] + a["l"], reverse=True)[:10]
    lines = [f"**Overall:** {tw}W - {tl}L\n"]
    lines += [f"- {a['name']}: {a['w']}W - {a['l']}L" for a in top]
    await i.response.send_message("\n".join(lines)[:1990])


@bot.tree.command(name="inactive", description="Members with no accepted logs for N days | الأعضاء غير النشطين")
async def inactive(i: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    if not i.guild.chunked:
        await i.guild.chunk()
    cutoff = (now_utc() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, MAX(created_at) FROM point_events GROUP BY user_id")
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
        return await i.followup.send("Everyone is active ✅")
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


@bot.tree.command(name="season-reset", description="Archive and reset points for a new season | أرشفة وبدء موسم جديد")
async def season_reset(i: discord.Interaction, name: str, confirm: str):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    if confirm != "RESET":
        return await i.response.send_message("Type RESET in the confirm field to proceed.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO season_archive SELECT ?, user_id, points FROM members WHERE points>0", (name,))
        await db.execute("UPDATE members SET points=0")
        await db.commit()
    await audit(f"🗄️ {i.user.mention} archived season **{name}** and reset all points", i.guild)
    await i.response.send_message(f"✅ Archived season **{name}** and reset points to 0.", ephemeral=True)


@bot.tree.command(name="season-top", description="Top 10 of an archived season | متصدري موسم مؤرشف")
async def season_top(i: discord.Interaction, name: str):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT user_id, points FROM season_archive WHERE season=? ORDER BY points DESC LIMIT 10", (name,))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("Season not found.", ephemeral=True)
    text = f"**Season {name}**\n" + "\n".join(f"**{n}.** <@{u}> — {p}" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


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


@bot.tree.command(name="backup", description="Send a database backup file | إرسال نسخة احتياطية لقاعدة البيانات")
async def backup_cmd(i: discord.Interaction):
    if not is_editor(i.user):
        return await i.response.send_message("❌ No permission.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    path = await make_backup()
    try:
        await i.followup.send("🗄️ Backup", file=discord.File(path), ephemeral=True)
    finally:
        os.unlink(path)


async def run_reminders():
    hours = await rule("reminder_hours")
    if not hours:
        return
    cutoff = (now_utc() - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id, type, guild_id, review_channel, review_msg FROM logs "
            "WHERE status='pending' AND COALESCE(reminded,0)=0 AND created_at<? AND review_msg IS NOT NULL",
            (cutoff,))
        rows = await cur.fetchall()
    for log_id, t, guild_id, ch_id, msg_id in rows:
        guild = bot.get_guild(guild_id) if guild_id else None
        ch = guild.get_channel(ch_id) if guild and ch_id else None
        if ch:
            ping = await cfg_get(guild.id, t, "review_ping")
            try:
                msg = await ch.fetch_message(msg_id)
                await msg.reply(f"{f'<@&{ping}> ' if ping else ''}⏰ Log #{log_id} has been waiting "
                                f"for review for more than {hours}h.", allowed_mentions=mentions(ping))
            except discord.HTTPException:
                pass
        await db_exec("UPDATE logs SET reminded=1 WHERE id=?", (log_id,))


async def run_backup():
    hours = await rule("backup_hours")
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
    if not await rule("weekly_post"):
        return
    last = await get_setting("state:last_weekly")
    if last is None:
        await set_setting("state:last_weekly", now_ts())
        return
    if now_ts() - last < 7 * 86400:
        return
    await set_setting("state:last_weekly", now_ts())
    text = await weekly_text()
    if not text:
        return
    for g in bot.guilds:
        ch_id = await cfg_get(g.id, "all", "log_channel")
        ch = g.get_channel(ch_id) if ch_id else None
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