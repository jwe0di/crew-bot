import asyncio
import datetime as dt
import hashlib
import io
import json
import os
import re
import tempfile
import threading
from datetime import timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from dotenv import load_dotenv

import configs as config

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

RESULT_LABEL = {"win": "✅ Win", "loss": "❌ Loss"}
RESULT_CHOICES = [
    app_commands.Choice(name="Win", value="win"),
    app_commands.Choice(name="Loss", value="loss"),
]
STATUS_LABEL = {"pending": "⏳ Pending", "accepted": "✅ Accepted", "rejected": "❌ Rejected"}

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
"""

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

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)


bot = CrewBot()


# ================================================================ DB helpers
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(SCHEMA)
        for k, v in config.DEFAULT_POINTS.items():
            await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", ("points:" + k, v))
        for name, p in config.LADDER:
            await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", ("rank:" + name, p))
        # ترحيل القنوات من النظام القديم
        cur = await db.execute("SELECT guild_id, review_channel, log_channel, audit_channel FROM guild_channels")
        for g, r, l, a in await cur.fetchall():
            for key, v in (("review_channel", r), ("log_channel", l), ("audit_channel", a)):
                if v:
                    await db.execute("INSERT OR IGNORE INTO guild_cfg VALUES(?,?,?,?)", (g, "all", key, v))
        await db.commit()


async def get_setting(key):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
    return row[0] if row else None


async def set_setting(key, value):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()


async def cfg_get(guild_id, log_type, key):
    """قيمة خاصة بنوع اللوج، وإلا القيمة العامة (all)، وإلا القيمة الاحتياطية من config."""
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


# ================================================================ permissions
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


async def can_submit(member, log_type) -> bool:
    if is_admin(member):
        return True
    ids = await roles_get(member.guild.id, log_type, "submit")
    if ids:
        return any(r.id in ids for r in member.roles)
    if log_type == "tryout":
        return has_role(member, config.TRYOUTER_ROLES)
    return True


# ================================================================ misc helpers
async def audit(text, guild):
    ch_id = await cfg_get(guild.id, "all", "audit_channel")
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        try:
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def dm_owner(text):
    owner_id = int(os.getenv("OWNER_ID") or getattr(config, "OWNER_ID", 0) or 0)
    if not owner_id:
        return
    try:
        user = await bot.fetch_user(owner_id)
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


# ================================================================ فحص الملفات
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
        return (dt.datetime.now(timezone.utc) - created).days
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
        m += f"      {CROWN} **Result:** {RESULT_LABEL.get(result, result)}\n"
    m += f"      • **Score:** {d('score')}\n"
    if data.get("notes"):
        m += f"      • **Notes:** {d('notes')}\n"

    m += f"\n**Status:** {STATUS_LABEL[status]}"
    if points is not None:
        m += f" | **Points:** +{points}"
        if pts_to:
            m += f" → <@{pts_to}>"
    if reviewer_id and status != "pending":
        who = "Accepted by" if status == "accepted" else "Rejected by"
        m += f"\n{MOON} **{who}:** <@{reviewer_id}>"
    if reason:
        m += f"\n**Reason:** {clip(reason, 300)}"
    if flags:
        m += "\n⚠️ **Warnings:**\n" + "\n".join("• " + clip(f, 120) for f in flags)
    return m[:1950]


# ================================================================ الترقية
async def apply_promotion(guild, member, total):
    ladder = await get_ladder()
    roles = [discord.utils.find(lambda r, n=n: n.lower() in r.name.lower(), guild.roles)
             for n, _ in ladder]
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
    return roles[target]


# ================================================================ المراجعة
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
    # في التراي أوت النقط بتروح للي عمل التراي أوت
    target_id = int(data["tester_id"]) if row["type"] == "tryout" and data.get("tester_id") else row["author_id"]

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "UPDATE logs SET status='accepted', points=?, reviewer_id=? "
            "WHERE id=? AND status='pending'", (pts, i.user.id, log_id))
        if cur.rowcount == 0:
            await db.commit()
            return await i.followup.send("This log has already been reviewed.", ephemeral=True)
        await db.execute(
            "INSERT INTO members(user_id, points) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET points = points + ?", (target_id, pts, pts))
        cur = await db.execute("SELECT points FROM members WHERE user_id=?", (target_id,))
        total = (await cur.fetchone())[0]
        await db.commit()

    # نشر اللوج في القناة العامة
    pub_id = await cfg_get(i.guild.id, row["type"], "log_channel")
    log_ch = i.guild.get_channel(pub_id) if pub_id else None
    if log_ch:
        ping_id = await cfg_get(i.guild.id, row["type"], "log_ping")
        text = render(log_id, row["type"], row["author_id"], data, row["result"], None,
                      "accepted", pts, i.user.id, pts_to=target_id)
        content = (f"<@&{ping_id}>\n" if ping_id else "") + text
        cand = [int(data["candidate_id"])] if row["type"] == "tryout" and data.get("candidate_id") else None
        try:
            files = [await a.to_file() for a in i.message.attachments]
        except discord.HTTPException:
            files = []
        try:
            await log_ch.send(content=content, files=files, allowed_mentions=mentions(ping_id, cand))
        except discord.HTTPException as e:
            await i.followup.send(f"Accepted, but I couldn't post in the log channel: {e}", ephemeral=True)
    else:
        await i.followup.send("Accepted, but no log channel is set. Use /setchannel.", ephemeral=True)

    reviewed = render(log_id, row["type"], row["author_id"], data, row["result"], flags,
                      "accepted", pts, i.user.id, pts_to=target_id)
    await i.message.edit(content=reviewed, view=None, allowed_mentions=discord.AllowedMentions.none())

    try:
        member = await get_member(i.guild, target_id)
        await apply_promotion(i.guild, member, total)
    except (discord.NotFound, discord.Forbidden) as e:
        await i.followup.send(f"Log accepted, but promotion failed: {e}", ephemeral=True)


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
                "UPDATE logs SET status='rejected', reviewer_id=?, reason=? "
                "WHERE id=? AND status='pending'", (i.user.id, self.reason.value, self.log_id))
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
            await author.send(f"❌ Log #{self.log_id} was rejected.\nReason: {self.reason.value}")
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


# ================================================================ إنشاء اللوج
class LogModal(discord.ui.Modal):
    def __init__(self, log_type, result, files, extra):
        super().__init__(title=config.LOG_TYPES[log_type]["label"][:45])
        self.log_type, self.result, self.files, self.extra = log_type, result, files, extra
        self.inputs = {}
        for key, label, long, required in config.LOG_TYPES[log_type]["fields"]:
            ti = discord.ui.TextInput(
                label=label[:45], required=required,
                max_length=350 if long else 100,
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
                "INSERT INTO logs(type, author_id, result, data, flags, created_at) VALUES(?,?,?,?,?,?)",
                (self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False),
                 json.dumps(flags, ensure_ascii=False), dt.datetime.now(timezone.utc).isoformat()))
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
            async with aiosqlite.connect(DB) as db:
                await db.execute("UPDATE logs SET status='rejected', reason=? WHERE id=?",
                                 ("failed to post for review", log_id))
                await db.commit()
            await i.followup.send(
                f"❌ مقدرتش أبعت اللوج لقناة المراجعة ({reason}). اتأكد إن الإدارة حددت القناة بـ /setchannel وإن البوت يقدر يكتب فيها.",
                ephemeral=True)

        if not ch:
            return await fail("القناة مش متحددة")
        try:
            try:
                await ch.send(content=content, view=ReviewView(), allowed_mentions=am,
                              files=[discord.File(io.BytesIO(b), a.filename) for a, b in blobs])
            except discord.Forbidden:
                raise
            except discord.HTTPException:
                # غالبًا الملفات أكبر من حد الرفع، نبعت روابط بدل الملفات
                await ch.send(content=content, view=ReviewView(), allowed_mentions=am)
                await ch.send("**Attachments (links expire after a while):**\n" + "\n".join(a.url for a, _ in blobs),
                              allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            return await fail(str(e))

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
    rid = await cfg_get(i.guild.id, log_type, "review_channel")
    if not rid or not i.guild.get_channel(rid):
        return await i.response.send_message(
            "قناة المراجعة مش متحددة لنوع اللوج ده. اطلب من الإدارة تستخدم /setchannel.", ephemeral=True)
    await i.response.send_modal(LogModal(log_type, result, files, extra or {}))


@bot.tree.command(name="log-war", description="Log a Crew War (video required)")
@app_commands.choices(result=RESULT_CHOICES)
async def log_war(i: discord.Interaction, result: app_commands.Choice[str],
                  video1: discord.Attachment, video2: Optional[discord.Attachment] = None,
                  video3: Optional[discord.Attachment] = None):
    await start_log(i, "war", result.value, [video1, video2, video3])


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
@app_commands.describe(candidate="اللي اتعمله التراي أوت (منشن)", proof="صورة الشات فيها السكور",
                       tester="اللي عمل التراي أوت وهياخد النقط (الافتراضي: أنت)")
async def log_tryout(i: discord.Interaction, candidate: discord.Member, proof: discord.Attachment,
                     tester: Optional[discord.Member] = None):
    tester = tester or i.user
    extra = {"candidate_id": candidate.id, "candidate": candidate.mention, "tester_id": tester.id}
    await start_log(i, "tryout", None, [proof], extra)


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


@bot.tree.command(name="settings", description="اعرض كل إعدادات القنوات والمنشنات والرتب")
async def settings_cmd(i: discord.Interaction):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
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
    old = await get_setting("points:" + log.value)
    await set_setting("points:" + log.value, points)
    await audit(f"✏️ {i.user.mention} changed points for **{log.name}** from {old} to {points}", i.guild)
    await i.response.send_message(f"Done. **{log.name}** = {points} points (new logs only).", ephemeral=True)


@bot.tree.command(name="set-rank-points", description="Set required points for a rank")
@app_commands.choices(rank=RANK_CHOICES)
async def set_rank_points(i: discord.Interaction, rank: app_commands.Choice[str],
                          points: app_commands.Range[int, 0, 100000]):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    old = await get_setting("rank:" + rank.value)
    await set_setting("rank:" + rank.value, points)
    await audit(f"✏️ {i.user.mention} changed rank **{rank.name}** from {old} to {points}", i.guild)
    await i.response.send_message(f"Done. **{rank.name}** requires {points} points.", ephemeral=True)


@bot.tree.command(name="set-points", description="Set a member's points balance")
async def set_points(i: discord.Interaction, member: discord.Member,
                     points: app_commands.Range[int, 0, 100000]):
    if not is_editor(i.user):
        return await i.response.send_message(NO_PERM, ephemeral=True)
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


@bot.tree.command(name="points", description="Check your points or someone else's")
async def points_cmd(i: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE user_id=?", (member.id,))
        row = await cur.fetchone()
    total = row[0] if row else 0
    nxt = next(((n, p) for n, p in await get_ladder() if p > total), None)
    msg = f"{member.mention}: **{total}** points."
    msg += f"\nNext rank: **{nxt[0]}** ({nxt[1] - total} left)." if nxt else "\nMax auto rank reached. Further promotions are manual."
    await i.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="leaderboard", description="Top 10 members by points")
async def leaderboard(i: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM members ORDER BY points DESC LIMIT 10")
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("No points yet.")
    text = "\n".join(f"**{n}.** <@{u}> — {p}" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="rates", description="Show current log points and rank requirements")
async def rates(i: discord.Interaction):
    lines = ["**Log points:**"]
    for k, label in config.POINT_LABELS.items():
        lines.append(f"- {label}: {await get_setting('points:' + k)}")
    lines.append("\n**Rank points:**")
    for n, p in await get_ladder():
        lines.append(f"- {n}: {p}")
    await i.response.send_message("\n".join(lines), ephemeral=True)


if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])