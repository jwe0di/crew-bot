import io
import asyncio
import datetime as dt
import hashlib
import json
import os
import tempfile
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from dotenv import load_dotenv

import config

load_dotenv()
DB = "crewbot.db"

RESULT_LABEL = {"win": "✅ Win", "loss": "❌ Loss"}
RESULT_CHOICES = [
    app_commands.Choice(name="Win", value="win"),
    app_commands.Choice(name="Loss", value="loss"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS members(user_id INTEGER PRIMARY KEY, points INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT, author_id INTEGER, result TEXT, data TEXT, flags TEXT,
    status TEXT DEFAULT 'pending', points INTEGER DEFAULT 0,
    reviewer_id INTEGER, reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS hashes(hash TEXT, log_id INTEGER, PRIMARY KEY(hash, log_id));
"""

class CrewBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await init_db()
        self.add_view(ReviewView())
        if config.GUILD_ID:
            guild = discord.Object(config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        print("Bot logged in as", self.user)
        if not check_pending_logs.is_running():
            check_pending_logs.start()

bot = CrewBot()

def is_higher_than_bot(member: discord.Member, guild: discord.Guild) -> bool:
    if member.guild_permissions.administrator:
        return True
    bot_member = guild.me
    if not bot_member:
        return False
    return member.top_role > bot_member.top_role

def has_role(member, names) -> bool:
    return any(
        n.lower() in r.name.lower()
        for r in getattr(member, "roles", [])
        for n in names
    )

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(SCHEMA)
        for k, v in config.DEFAULT_POINTS.items():
            await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", ("points:" + k, v))
        for name, p in config.LADDER:
            await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", ("rank:" + name, p))
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
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()

async def get_ladder():
    return [(n, await get_setting("rank:" + n)) for n, _ in config.LADDER]

async def get_log(log_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM logs WHERE id=?", (log_id,))
        return await cur.fetchone()

async def audit(text):
    ch = bot.get_channel(config.AUDIT_CHANNEL_ID)
    if ch:
        await ch.send(text, allowed_mentions=discord.AllowedMentions.none())

async def dm_owner(text):
    try:
        user = await bot.fetch_user(config.OWNER_ID)
        await user.send(text)
    except (discord.HTTPException, discord.NotFound):
        pass

async def get_member(guild, user_id):
    return guild.get_member(user_id) or await guild.fetch_member(user_id)

def points_key(log_type, result):
    return ("points:" + log_type + "_" + result) if log_type in ("war", "outside") else ("points:" + log_type)

def build_stylized_log(log_id, log_type, author_mention, data, result, flags=None, status="⏳ Pending", points=None, reviewer_mention=None, reason=None):
    title_label = config.LOG_TYPES[log_type]["label"]
    divider = "<:EU1:1449430079611600936> ●▬▬▬▬▬▬▬▬▬▬▬☭  <:Grok2:1513556052036030584> ☭▬▬▬▬▬▬▬▬▬▬▬● <:EU1:1449430079611600936>"
    
    msg = "<:celestial_angel:1455574559062360198>  **" + title_label + "**  <:celestial_angel:1455574559062360198>\n\n"
    msg += divider + "\n\n"
    msg += "<:Whitemoon:1458484853640138784> **Logged by:** " + author_mention + "\n"
    
    if log_type == "war":
        msg += "<:Whitemoon:1458484853640138784> **Dragon Slayer's Fighters:**\n"
        msg += "<:f_arrows:1364977170140893315>  " + str(data.get('my_team', author_mention)) + "\n\n"
        msg += divider + "\n\n"
        msg += "<:Whitemoon:1458484853640138784> **" + str(data.get('crew', 'Enemy Crew')) + " Fighters:**\n"
        msg += "<:f_arrows:1364977170140893315>  " + str(data.get('their_team', '-')) + "\n\n"
    elif log_type == "outside":
        msg += "<:Whitemoon:1458484853640138784> **Dragon Slayer's Fighter:**\n"
        msg += "<:f_arrows:1364977170140893315>  " + str(data.get('my_fighter', author_mention)) + "\n\n"
        msg += divider + "\n\n"
        msg += "<:Whitemoon:1458484853640138784> **Opponent Fighter:**\n"
        msg += "<:f_arrows:1364977170140893315>  " + str(data.get('opponent', '-')) + "\n\n"
    elif log_type == "internal":
        msg += "<:Whitemoon:1458484853640138784> **Internal Fighters:**\n"
        msg += "<:f_arrows:1364977170140893315>  " + str(data.get('players', '-')) + "\n\n"
    elif log_type == "tryout":
        msg += "<:Whitemoon:1458484853640138784> **Tryout Candidate:**\n"
        msg += "<:f_arrows:1364977170140893315>  " + str(data.get('candidate', '-')) + "\n\n"
        if reviewer_mention and status != "⏳ Pending":
            msg += "<:Whitemoon:1458484853640138784> **Tested & Accepted by:** " + reviewer_mention + "\n\n"

    msg += divider + "\n\n"
    if result:
        msg += "      <:WhiteCrown:1458484851614416968> **Winner:** " + RESULT_LABEL.get(result, result) + "\n"
    msg += "      • **Score:** " + str(data.get('score', '-')) + "\n"
    if data.get('notes'):
        msg += "      • **Notes:** " + str(data.get('notes')) + "\n"
        
    msg += "\n**Status:** " + status
    if points is not None:
        msg += " | **Points:** +" + str(points)
    if reviewer_mention and log_type != "tryout":
        msg += " | **Reviewed by:** " + str(reviewer_mention)
    elif reviewer_mention and log_type == "tryout" and status == "⏳ Pending":
        pass
    if reason:
        msg += "\n**Reason:** " + str(reason)
    if flags:
        msg += "\n⚠️ **Warnings:** " + " ".join(flags)
        
    msg += "\n\n`Log #" + str(log_id) + "`"
    return msg

def log_id_from_msg(message: discord.Message) -> int:
    last_line = message.content.strip().splitlines()[-1]
    return int(last_line.split("#")[1].replace("`", ""))

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

    ch = bot.get_channel(config.LOG_CHANNEL_ID)
    if ch:
        await ch.send("🎉 " + member.mention + " has been promoted to " + roles[target].mention + "!")
    return roles[target]

async def accept_log(i: discord.Interaction):
    if not isinstance(i.user, discord.Member) or not is_higher_than_bot(i.user, i.guild):
        return await i.response.send_message("You don't have permission (Must have a higher role than the bot).", ephemeral=True)
    log_id = log_id_from_msg(i.message)
    row = await get_log(log_id)
    if row["author_id"] == i.user.id and row["type"] != "tryout":
        return await i.response.send_message("You cannot review your own log.", ephemeral=True)
    await i.response.defer()

    pts = await get_setting(points_key(row["type"], row["result"])) if row["type"] in ("war", "outside", "internal", "tryout") else 0
    if pts is None:
        pts = config.DEFAULT_POINTS.get(row["type"], 0)

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "UPDATE logs SET status='accepted', points=?, reviewer_id=? "
            "WHERE id=? AND status='pending'", (pts, i.user.id, log_id))
        if cur.rowcount == 0:
            await db.commit()
            return await i.followup.send("This log has already been reviewed.", ephemeral=True)
        
        target_user_id = row["author_id"]
        await db.execute(
            "INSERT INTO members(user_id, points) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET points = points + ?",
            (target_user_id, pts, pts))
        cur = await db.execute("SELECT points FROM members WHERE user_id=?", (target_user_id,))
        total = (await cur.fetchone())[0]
        await db.commit()

    data = json.loads(row["data"])
    flags = json.loads(row["flags"] or "[]")
    author_text = "<@" + str(row['author_id']) + ">"

    log_ch = bot.get_channel(config.LOG_CHANNEL_ID)
    if log_ch:
        public_msg = build_stylized_log(log_id, row["type"], author_text, data, row["result"], None,
                                        status="✅ Accepted", points=pts, reviewer_mention=i.user.mention)
        try:
            files = [await a.to_file() for a in i.message.attachments]
        except discord.HTTPException:
            files = []
        await log_ch.send(content=public_msg, files=files)

    reviewed_msg = build_stylized_log(log_id, row["type"], author_text, data, row["result"], flags,
                                       status="✅ Accepted", points=pts, reviewer_mention=i.user.mention)
    await i.message.edit(content=reviewed_msg, view=None)

    try:
        member = await get_member(i.guild, target_user_id)
        if member:
            await apply_promotion(i.guild, member, total)
    except (discord.NotFound, discord.Forbidden) as e:
        await i.followup.send("Log accepted, but promotion failed: " + str(e), ephemeral=True)

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
        rejected_msg = build_stylized_log(self.log_id, row["type"], "<@" + str(row['author_id']) + ">", data, row["result"],
                                          flags, status="❌ Rejected", reviewer_mention=i.user.mention,
                                          reason=self.reason.value)
        await self.message.edit(content=rejected_msg, view=None)
        await i.response.send_message("Log rejected.", ephemeral=True)
        try:
            author = await bot.fetch_user(row["author_id"])
            await author.send("❌ Log #" + str(self.log_id) + " was rejected.\nReason: " + self.reason.value)
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
        if not isinstance(i.user, discord.Member) or not is_higher_than_bot(i.user, i.guild):
            return await i.response.send_message("You don't have permission (Must have a higher role than the bot).", ephemeral=True)
        log_id = log_id_from_msg(i.message)
        row = await get_log(log_id)
        if row["author_id"] == i.user.id and row["type"] != "tryout":
            return await i.response.send_message("You cannot review your own log.", ephemeral=True)
        await i.response.send_modal(RejectModal(log_id, i.message))

class LogModal(discord.ui.Modal):
    def __init__(self, log_type, result, files):
        super().__init__(title=config.LOG_TYPES[log_type]["label"][:45])
        self.log_type, self.result, self.files = log_type, result, files
        self.inputs = {}
        for key, label, long in config.LOG_TYPES[log_type]["fields"]:
            ti = discord.ui.TextInput(
                label=label[:45], max_length=500,
                style=discord.TextStyle.paragraph if long else discord.TextStyle.short)
            self.add_item(ti)
            self.inputs[key] = ti

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        blobs = [(a, await a.read()) for a in self.files]
        hashes = [hashlib.sha256(b).hexdigest() for _, b in blobs]
        flags = []
        for att, h in zip(self.files, hashes):
            async with aiosqlite.connect(DB) as db:
                cur = await db.execute(
                    "SELECT h.log_id FROM hashes h JOIN logs l ON l.id=h.log_id "
                    "WHERE h.hash=? AND l.status!='rejected' LIMIT 1", (h,))
                row = await cur.fetchone()
            if row:
                flags.append(att.filename + ": Same file was used in Log #" + str(row[0]))

        data = {k: v.value for k, v in self.inputs.items()}

        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "INSERT INTO logs(type, author_id, result, data, flags, created_at) VALUES(?,?,?,?,?,?)",
                (self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False),
                 json.dumps(flags, ensure_ascii=False), dt.datetime.utcnow().isoformat()))
            log_id = cur.lastrowid
            for h in hashes:
                await db.execute("INSERT OR IGNORE INTO hashes VALUES(?,?)", (h, log_id))
            await db.commit()

        msg_content = build_stylized_log(log_id, self.log_type, i.user.mention, data, self.result, flags)
        ch = bot.get_channel(config.REVIEW_CHANNEL_ID)
        try:
            await ch.send(content=msg_content, view=ReviewView(),
                          files=[discord.File(io.BytesIO(b), a.filename) for a, b in blobs])
        except discord.HTTPException:
            msg_content += "\n\n**Attachments:**\n" + "\n".join(a.url for a, _ in blobs)
            await ch.send(content=msg_content, view=ReviewView())

        if flags:
            await dm_owner("⚠️ " + i.user.mention + " (" + str(i.user) + ") submitted Log #" + str(log_id) + " with warnings:\n" + "\n".join(flags))
        await i.followup.send("✅ Log #" + str(log_id) + " submitted for review.", ephemeral=True)

async def start_log(i: discord.Interaction, log_type, result, files):
    files = [f for f in files if f]
    if log_type == "war" and not any((f.content_type or "").startswith("video") for f in files):
        return await i.response.send_message("War log requires at least one video attachment.", ephemeral=True)
    if log_type == "tryout" and not has_role(i.user, config.TRYOUTER_ROLES):
        return await i.response.send_message("Only Tryout Masters can submit tryout logs.", ephemeral=True)
    await i.response.send_modal(LogModal(log_type, result, files))

@bot.tree.command(name="log-war", description="Log a Crew War (Video required)")
@app_commands.choices(result=RESULT_CHOICES)
async def log_war(i: discord.Interaction, result: app_commands.Choice[str],
                  video1: discord.Attachment, video2: Optional[discord.Attachment] = None,
                  video3: Optional[discord.Attachment] = None):
    await start_log(i, "war", result.value, [video1, video2, video3])

@bot.tree.command(name="log-outside", description="Log a 1v1 fight against an outside player/famous player")
@app_commands.choices(result=RESULT_CHOICES)
async def log_outside(i: discord.Interaction, result: app_commands.Choice[str],
                      proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None):
    await start_log(i, "outside", result.value, [proof1, proof2])

@bot.tree.command(name="log-internal", description="Log an internal fight between crew members")
async def log_internal(i: discord.Interaction, proof1: discord.Attachment,
                       proof2: Optional[discord.Attachment] = None):
    await start_log(i, "internal", None, [proof1, proof2])

@bot.tree.command(name="log-tryout", description="Log a crew tryout result")
async def log_tryout(i: discord.Interaction, proof: discord.Attachment):
    await start_log(i, "tryout", None, [proof])

def is_editor(i: discord.Interaction) -> bool:
    if isinstance(i.user, discord.Member) and is_higher_than_bot(i.user, i.guild):
        return True
    return has_role(i.user, config.EDITOR_ROLES)

NO_PERM = "You don't have permission to modify points."
POINT_CHOICES = [app_commands.Choice(name=l, value=k) for k, l in config.POINT_LABELS.items()]
RANK_CHOICES = [app_commands.Choice(name=n, value=n) for n, _ in config.LADDER]

@bot.tree.command(name="set-log-points", description="Set points for a specific log type")
@app_commands.choices(log=POINT_CHOICES)
async def set_log_points(i: discord.Interaction, log: app_commands.Choice[str],
                         points: app_commands.Range[int, 0, 1000]):
    if not is_editor(i):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    old = await get_setting("points:" + log.value)
    await set_setting("points:" + log.value, points)
    await audit("✏️ " + i.user.mention + " changed points for **" + log.name + "** from " + str(old) + " to " + str(points))
    await i.response.send_message("Done. **" + log.name + "** = " + str(points) + " points.", ephemeral=True)

@bot.tree.command(name="set-rank-points", description="Set required points for a rank")
@app_commands.choices(rank=RANK_CHOICES)
async def set_rank_points(i: discord.Interaction, rank: app_commands.Choice[str],
                          points: app_commands.Range[int, 0, 100000]):
    if not is_editor(i):
        return await i.response.send_message(NO_PERM, ephemeral=True)
    old = await get_setting("rank:" + rank.value)
    await set_setting("rank:" + rank.value, points)
    await audit("✏️ " + i.user.mention + " changed points for rank **" + rank.name + "** from " + str(old) + " to " + str(points))
    await i.response.send_message("Done. **" + rank.name + "** requires " + str(points) + " points.", ephemeral=True)

@bot.tree.command(name="set-points", description="Set points balance for a member")
async def set_points(i: discord.Interaction, member: discord.Member,
                     points: app_commands.Range[int, 0, 100000]):
    if not is_editor(i):
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
    await audit("✏️ " + i.user.mention + " changed points balance for " + member.mention + " from " + str(old) + " to " + str(points))
    await apply_promotion(i.guild, member, points)
    await i.followup.send("Done. " + member.display_name + "'s points = " + str(points) + ".", ephemeral=True)

@bot.tree.command(name="points", description="Check your points or another member's points")
async def points_cmd(i: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE user_id=?", (member.id,))
        row = await cur.fetchone()
    total = row[0] if row else 0
    nxt = next(((n, p) for n, p in await get_ladder() if p > total), None)
    msg = member.mention + ": **" + str(total) + "** points."
    if nxt:
        msg += "\nNext rank: **" + str(nxt[0]) + "** (Needs " + str(nxt[1] - total) + " more points)."
    else:
        msg += "\nReached the highest auto-promotion rank!"
    await i.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())

# Leaderboard Pagination View and Command matching the requested screenshot style
class LeaderboardPaginator(discord.ui.View):
    def __init__(self, rows, user_id):
        super().__init__(timeout=180)
        self.rows = rows
        self.user_id = user_id
        self.page = 0
        self.per_page = 10
        self.max_pages = max(1, (len(rows) + self.per_page - 1) // self.per_page)
        self.update_buttons()

    def update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.max_pages - 1

    def build_embed(self):
        embed = discord.Embed(color=0x2b2d31)
        start = self.page * self.per_page
        end = start + self.per_page
        current_rows = self.rows[start:end]

        desc = f"🏆 **Leaderboard (Page {self.page + 1}/{self.max_pages})**\n\n"
        if not current_rows:
            desc += "No points recorded yet."
        else:
            for idx, (uid, pts) in enumerate(current_rows, start + 1):
                desc += f"`{idx}.` <@{uid}> — **{pts} points**\n"

        # Find user position
        user_rank = "Unranked"
        for idx, (uid, pts) in enumerate(self.rows, 1):
            if uid == self.user_id:
                user_rank = f"#{idx}"
                break

        embed.description = desc
        embed.set_footer(text=f"Your Position: {user_rank}")
        return embed

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, custom_id="lb:prev")
    async def prev_btn(self, i: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            self.update_buttons()
            await i.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await i.response.defer()

    @discord.ui.button(label="Go To Page", style=discord.ButtonStyle.secondary, custom_id="lb:goto", disabled=True)
    async def goto_btn(self, i: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, custom_id="lb:next")
    async def next_btn(self, i: discord.Interaction, button: discord.ui.Button):
        if self.page < self.max_pages - 1:
            self.page += 1
            self.update_buttons()
            await i.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await i.response.defer()

@bot.tree.command(name="leaderboard", description="View top members leaderboard with pagination")
async def leaderboard(i: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, points FROM members ORDER BY points DESC")
        rows = await cur.fetchall()
    
    if not rows:
        return await i.response.send_message("No points recorded yet.", ephemeral=True)
    
    view = LeaderboardPaginator(rows, i.user.id)
    await i.response.send_message(embed=view.build_embed(), view=view)

@bot.tree.command(name="rates", description="View current point rates and rank requirements")
async def rates(i: discord.Instruction if hasattr(discord, 'Instruction') else discord.Interaction):
    lines = ["**Log Points:**"]
    for k, label in config.POINT_LABELS.items():
        lines.append("- " + label + ": " + str(await get_setting("points:" + k)))
    lines.append("\n**Rank Requirements:**")
    for n, p in await get_ladder():
        lines.append("- " + n + ": " + p if isinstance(p, str) else "- " + n + ": " + str(p))
    await i.response.send_message("\n".join(lines), ephemeral=True)

# Background task to send warning for logs pending > 3 days
from discord.ext import tasks

@tasks.loop(hours=24)
async def check_pending_logs():
    await bot.wait_until_ready()
    try:
        async with aiosqlite.connect(DB) as db:
            db.row_factory = aiosqlite.Row
            three_days_ago = (dt.datetime.utcnow() - dt.timedelta(days=3)).isoformat()
            cur = await db.execute("SELECT * FROM logs WHERE status='pending' AND created_at <= ?", (three_days_ago,))
            rows = await cur.fetchall()
        
        if rows:
            review_ch = bot.get_channel(config.REVIEW_CHANNEL_ID)
            if review_ch:
                for row in rows:
                    await review_ch.send("⚠️ **WARNING:** Log #" + str(row["id"]) + " has been pending for more than 3 days! Reviewers please check it.")
    except Exception as e:
        print("Error in background task:", e)

if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])
