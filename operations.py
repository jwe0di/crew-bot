"""Operations, analytics, anti-raid, incident history and reversible bot actions.

This extension intentionally uses only Discord permissions already granted to the bot.
It never bypasses role hierarchy, accesses secrets, or executes arbitrary SQL/code.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from collections import defaultdict, deque
from typing import Any

import aiosqlite
import discord
from discord import app_commands

DB_PATH = os.getenv("DB_PATH", "crewbot.db")
UTC = dt.timezone.utc
JOIN_WINDOWS: dict[int, deque[float]] = defaultdict(deque)
RAID_UNTIL: dict[int, float] = {}
UNDO_SUPPRESS_UNTIL: dict[tuple[int, str, int], dt.datetime] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_settings (
 guild_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(guild_id,key)
);
CREATE TABLE IF NOT EXISTS ops_activity_daily (
 guild_id INTEGER NOT NULL, day TEXT NOT NULL,
 message_count INTEGER NOT NULL DEFAULT 0,
 join_count INTEGER NOT NULL DEFAULT 0,
 leave_count INTEGER NOT NULL DEFAULT 0,
 active_users INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(guild_id,day)
);
CREATE TABLE IF NOT EXISTS ops_activity_users (
 guild_id INTEGER NOT NULL, day TEXT NOT NULL, user_id INTEGER NOT NULL,
 PRIMARY KEY(guild_id,day,user_id)
);
CREATE TABLE IF NOT EXISTS ops_incidents (
 id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
 event_type TEXT NOT NULL, actor_id INTEGER, target_id INTEGER,
 summary TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ops_incidents_guild_time
 ON ops_incidents(guild_id,created_at);
CREATE TABLE IF NOT EXISTS ops_join_events (
 guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, joined_at TEXT NOT NULL,
 PRIMARY KEY(guild_id,user_id)
);
CREATE INDEX IF NOT EXISTS idx_ops_join_events_time ON ops_join_events(guild_id,joined_at);
CREATE TABLE IF NOT EXISTS ops_security_alert_state (
 guild_id INTEGER NOT NULL, rule TEXT NOT NULL, last_alert_at TEXT NOT NULL,
 PRIMARY KEY(guild_id,rule)
);
CREATE TABLE IF NOT EXISTS ops_nuke_quarantines (
 id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
 removed_roles TEXT NOT NULL, quarantine_role_id INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
 released_at TEXT, failure TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_nuke_quarantines_actor
 ON ops_nuke_quarantines(guild_id,actor_id,status,id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_nuke_active_actor
 ON ops_nuke_quarantines(guild_id,actor_id) WHERE status IN ('pending','quarantined','partial','releasing');
CREATE TABLE IF NOT EXISTS ops_undo_actions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
 actor_id INTEGER, action_type TEXT NOT NULL, target_id INTEGER,
 snapshot TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, undone_at TEXT,
 status TEXT NOT NULL DEFAULT 'pending', claimed_by INTEGER,
 claimed_at TEXT, failure TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_undo_guild ON ops_undo_actions(guild_id,undone,id);
CREATE TABLE IF NOT EXISTS ops_ticket_sla_notices (
 guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', claimed_at TEXT,
 notified_at TEXT, last_error TEXT,
 PRIMARY KEY(guild_id,channel_id)
);
CREATE INDEX IF NOT EXISTS idx_ops_ticket_sla_status ON ops_ticket_sla_notices(status,claimed_at);
"""


def _now() -> dt.datetime:
    return dt.datetime.now(UTC)


def _suppress_undo_record(guild_id: int, action_type: str, target_id: int, seconds: int = 15) -> None:
    UNDO_SUPPRESS_UNTIL[(guild_id, action_type, target_id)] = _now() + dt.timedelta(seconds=seconds)


def _undo_record_suppressed(guild_id: int, action_type: str, target_id: int) -> bool:
    key = (guild_id, action_type, target_id)
    until = UNDO_SUPPRESS_UNTIL.get(key)
    if until is None:
        return False
    if until <= _now():
        UNDO_SUPPRESS_UNTIL.pop(key, None)
        return False
    return True


def _day() -> str:
    return _now().date().isoformat()


def _is_admin(member: discord.Member) -> bool:
    return bool(member.guild_permissions.administrator or member.guild_permissions.manage_guild)


async def _db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def _setting(guild_id: int, key: str, default: str) -> str:
    async with await _db() as db:
        cur = await db.execute(
            "SELECT value FROM ops_settings WHERE guild_id=? AND key=?",
            (guild_id, key),
        )
        row = await cur.fetchone()
    return str(row[0]) if row else default


async def _set_setting(guild_id: int, key: str, value: str) -> None:
    async with await _db() as db:
        await db.execute(
            "INSERT INTO ops_settings(guild_id,key,value) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
            (guild_id, key, str(value)),
        )
        await db.commit()


async def _incident(guild_id: int, event_type: str, summary: str,
                    actor_id: int | None = None, target_id: int | None = None,
                    details: dict[str, Any] | None = None) -> None:
    async with await _db() as db:
        await db.execute(
            "INSERT INTO ops_incidents(guild_id,event_type,actor_id,target_id,summary,details,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (guild_id, event_type, actor_id, target_id, summary[:500],
             json.dumps(details or {}, ensure_ascii=False)[:8000], _now().isoformat()),
        )
        await db.commit()


async def _daily(guild_id: int, field: str, amount: int = 1) -> None:
    allowed = {"message_count", "join_count", "leave_count"}
    if field not in allowed:
        return
    async with await _db() as db:
        await db.execute(
            f"INSERT INTO ops_activity_daily(guild_id,day,{field}) VALUES(?,?,?) "
            f"ON CONFLICT(guild_id,day) DO UPDATE SET {field}={field}+excluded.{field}",
            (guild_id, _day(), amount),
        )
        await db.commit()


async def _mark_active(guild_id: int, user_id: int) -> None:
    async with await _db() as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO ops_activity_users(guild_id,day,user_id) VALUES(?,?,?)",
            (guild_id, _day(), user_id),
        )
        if cur.rowcount:
            await db.execute(
                "INSERT INTO ops_activity_daily(guild_id,day,active_users) VALUES(?,?,1) "
                "ON CONFLICT(guild_id,day) DO UPDATE SET active_users=active_users+1",
                (guild_id, _day()),
            )
        await db.commit()


async def _audit_actor(guild: discord.Guild, action: discord.AuditLogAction,
                       target_id: int) -> int | None:
    try:
        await asyncio.sleep(0.35)
        async for entry in guild.audit_logs(limit=6, action=action):
            if getattr(entry.target, "id", None) == target_id:
                age = (_now() - entry.created_at.astimezone(UTC)).total_seconds()
                if age <= 12:
                    return entry.user.id if entry.user else None
                break
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass
    return None


async def _record_undo(guild_id: int, actor_id: int | None, action_type: str,
                       target_id: int, snapshot: dict[str, Any]) -> None:
    if actor_id is None:
        return
    async with await _db() as db:
        await db.execute(
            "INSERT INTO ops_undo_actions(guild_id,actor_id,action_type,target_id,snapshot,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (guild_id, actor_id, action_type, target_id,
             json.dumps(snapshot, ensure_ascii=False), _now().isoformat()),
        )
        await db.commit()


def _role_snapshot(role: discord.Role) -> dict[str, Any]:
    return {
        "name": role.name, "permissions": role.permissions.value,
        "colour": role.colour.value, "hoist": role.hoist,
        "mentionable": role.mentionable, "position": role.position,
    }


def _channel_snapshot(channel: discord.abc.GuildChannel) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": channel.name, "type": str(channel.type),
        "position": channel.position,
        "category_id": channel.category_id,
        "overwrites": {
            str(target.id): {
                "kind": "role" if isinstance(target, discord.Role) else "member",
                "allow": permissions.pair()[0].value,
                "deny": permissions.pair()[1].value,
            }
            for target, permissions in channel.overwrites.items()
        },
    }
    if isinstance(channel, discord.TextChannel):
        data.update(topic=channel.topic, nsfw=channel.nsfw,
                    slowmode_delay=channel.slowmode_delay)
    elif isinstance(channel, discord.VoiceChannel):
        data.update(bitrate=channel.bitrate, user_limit=channel.user_limit)
    return data


async def _undo_last(interaction: discord.Interaction) -> str:
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
        return "â ÙØ­ØªØ§Ø¬ ØµÙØ§Ø­ÙØ© Administrator Ø£Ù Manage Server."

    # Claim one row atomically. A crashed process leaves a lease that can be
    # reclaimed after five minutes; two moderators cannot execute the same row.
    now = _now()
    async with await _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "UPDATE ops_undo_actions SET status='pending',claimed_by=NULL,claimed_at=NULL "
            "WHERE guild_id=? AND status='executing' AND claimed_at<? AND undone=0",
            (guild.id, (now-dt.timedelta(minutes=5)).isoformat()),
        )
        cur = await db.execute(
            "SELECT id,action_type,target_id,snapshot FROM ops_undo_actions "
            "WHERE guild_id=? AND undone=0 AND status='pending' ORDER BY id DESC LIMIT 1",
            (guild.id,),
        )
        row = await cur.fetchone()
        if not row:
            await db.commit()
            return "ÙÙÙØ´ Ø¥Ø¬Ø±Ø§Ø¡ ÙØªØ§Ø­ ÙÙØªØ±Ø§Ø¬Ø¹ Ø­Ø§ÙÙÙØ§. ÙÙÙÙ ÙÙÙÙ Ø¥Ø¬Ø±Ø§Ø¡ Ø¢Ø®Ø± ÙÙØ¯ Ø§ÙØªÙÙÙØ° Ø£Ù ÙÙÙØ´ Ø³Ø¬Ù ÙØ§Ø¨Ù ÙÙØ¹ÙØ³."
        action_id, action_type, target_id, raw = row
        cur = await db.execute(
            "UPDATE ops_undo_actions SET status='executing',claimed_by=?,claimed_at=?,failure=NULL "
            "WHERE id=? AND status='pending' AND undone=0",
            (interaction.user.id, now.isoformat(), action_id),
        )
        if cur.rowcount != 1:
            await db.rollback()
            return "Ø§ÙØ¥Ø¬Ø±Ø§Ø¡ Ø§ØªØ­Ø¬Ø² Ø¨ÙØ§Ø³Ø·Ø© Ø¹ÙÙÙØ© Ø£Ø®Ø±Ù. Ø­Ø¯ÙØ« Ø³Ø¬Ù Ø§ÙØªØ±Ø§Ø¬Ø¹ ÙØ¬Ø±Ø¨ ÙØ§Ø­ÙÙØ§."
        await db.commit()

    async def release(status: str, reason: str | None = None) -> None:
        async with await _db() as db:
            await db.execute(
                "UPDATE ops_undo_actions SET status=?,claimed_by=NULL,claimed_at=NULL,failure=? "
                "WHERE id=? AND status='executing' AND claimed_by=?",
                (status, (reason or "")[:500] or None, action_id, interaction.user.id),
            )
            await db.commit()

    try:
        snap = json.loads(raw)
        bot_user = getattr(interaction.client, "user", None)
        me = await guild.fetch_member(bot_user.id) if bot_user else None
        if me is None:
            await release("pending", "Bot member unavailable")
            return "ØªØ¹Ø°Ø± Ø§ÙØªØ±Ø§Ø¬Ø¹ ÙØ£Ù Ø¹Ø¶Ù Ø§ÙØ¨ÙØª ØºÙØ± ÙØªØ§Ø­ ÙÙ Ø§ÙØ³ÙØ±ÙØ±."
        if action_type == "role_create":
            roles = await guild.fetch_roles()
            role = discord.utils.get(roles, id=target_id)
            if not role:
                await release("conflict", "Created role no longer exists")
                return "Ø§ÙØ±ØªØ¨Ø© ÙØ´ ÙÙØ¬ÙØ¯Ø© Ø¨Ø§ÙÙØ¹ÙØ Ø³Ø¬Ù Ø§ÙØªØ±Ø§Ø¬Ø¹ Ø§ØªÙÙÙ ÙØªØ¹Ø§Ø±Ø¶ Ø¨Ø¯Ù ØªÙÙÙØ° Ø¥Ø¬Ø±Ø§Ø¡ ÙØ®ØªÙÙ."
            if _role_snapshot(role) != snap:
                await release("conflict", "Role changed after bot created it")
                return "Ø§ÙØ±ØªØ¨Ø© Ø§ØªØºÙØ±Øª Ø¨Ø¹Ø¯ Ø¥ÙØ´Ø§Ø¦ÙØ§. Ø£ÙÙÙØª Ø§ÙØªØ±Ø§Ø¬Ø¹ Ø¹Ø´Ø§Ù ÙØ§ ÙÙØ³Ø­Ø´ ØªØ¹Ø¯ÙÙÙØ§ Ø£Ø­Ø¯Ø«."
            if me.top_role <= role:
                await release("pending", "Bot role hierarchy prevents deletion")
                return "ØªØ±ØªÙØ¨ Ø±ØªØ¨ Ø§ÙØ¨ÙØª ÙØ§ ÙØ³ÙØ­ Ø¨Ø­Ø°Ù Ø§ÙØ±ØªØ¨Ø© Ø¯Ù."
            await role.delete(reason=f"Undo action #{action_id} requested by {interaction.user}")
        elif action_type == "channel_create":
            try:
                channel = await interaction.client.fetch_channel(target_id)
            except discord.NotFound:
                channel = None
            if not channel or getattr(getattr(channel, "guild", None), "id", None) != guild.id:
                await release("conflict", "Created channel no longer exists")
                return "Ø§ÙÙÙØ§Ø© ÙØ´ ÙÙØ¬ÙØ¯Ø© Ø¨Ø§ÙÙØ¹ÙØ Ø³Ø¬Ù Ø§ÙØªØ±Ø§Ø¬Ø¹ Ø§ØªÙÙÙ ÙØªØ¹Ø§Ø±Ø¶."
            if _channel_snapshot(channel) != snap:
                await release("conflict", "Channel changed after bot created it")
                return "Ø§ÙÙÙØ§Ø© Ø§ØªØºÙØ±Øª Ø¨Ø¹Ø¯ Ø¥ÙØ´Ø§Ø¦ÙØ§. Ø£ÙÙÙØª Ø§ÙØªØ±Ø§Ø¬Ø¹ ÙØ­ÙØ§ÙØ© Ø§ÙØªØ¹Ø¯ÙÙØ§Øª Ø§ÙØ£Ø­Ø¯Ø«."
            await channel.delete(reason=f"Undo action #{action_id} requested by {interaction.user}")
        elif action_type == "role_update":
            roles = await guild.fetch_roles()
            role = discord.utils.get(roles, id=target_id)
            if not role or me.top_role <= role:
                await release("pending", "Role missing or above bot hierarchy")
                return "Ø§ÙØ±ØªØ¨Ø© ØºÙØ± ÙÙØ¬ÙØ¯Ø© Ø£Ù Ø£Ø¹ÙÙ ÙÙ Ø±ØªØ¨Ø© Ø§ÙØ¨ÙØª."
            before = snap.get("before") if isinstance(snap, dict) else None
            after = snap.get("after") if isinstance(snap, dict) else None
            if not before or not after:
                await release("conflict", "Legacy undo record has no after-state fingerprint")
                return "Ø§ÙØ³Ø¬Ù ÙØ¯ÙÙ ÙÙØ§ ÙØ­ØªÙÙ Ø¹ÙÙ Ø¨ØµÙØ© Ø§ÙØ­Ø§ÙØ© Ø¨Ø¹Ø¯ Ø§ÙØªØ¹Ø¯ÙÙØ Ø£ÙÙÙØª Ø§ÙØªØ±Ø§Ø¬Ø¹ Ø§ÙØ¢ÙÙ ÙØªØ¬ÙØ¨ Ø§ÙÙØªØ§Ø¨Ø© ÙÙÙ ØªØºÙÙØ±Ø§Øª Ø£Ø­Ø¯Ø«."
            if _role_snapshot(role) != after:
                await release("conflict", "Role changed after recorded bot action")
                return "Ø§ÙØ±ØªØ¨Ø© Ø§ØªØºÙØ±Øª Ø¨Ø¹Ø¯ Ø§ÙØ¥Ø¬Ø±Ø§Ø¡ Ø§ÙÙØ³Ø¬Ù. Ø£ÙÙÙØª Ø§ÙØªØ±Ø§Ø¬Ø¹ Ø¨Ø¯Ù ÙØ§ Ø£Ø³ØªØ¨Ø¯Ù ØªØ¹Ø¯ÙÙÙØ§ Ø£Ø­Ø¯Ø«."
            _suppress_undo_record(guild.id, "role_update", role.id)
            await role.edit(
                name=before["name"], permissions=discord.Permissions(before["permissions"]),
                colour=discord.Colour(before["colour"]), hoist=before["hoist"],
                mentionable=before["mentionable"], position=before["position"],
                reason=f"Undo action #{action_id} requested by {interaction.user}",
            )
        elif action_type == "channel_update":
            try:
                channel = await interaction.client.fetch_channel(target_id)
            except discord.NotFound:
                channel = None
            if not channel or getattr(getattr(channel, "guild", None), "id", None) != guild.id:
                await release("pending", "Channel no longer exists")
                return "Ø§ÙÙÙØ§Ø© ÙØ´ ÙÙØ¬ÙØ¯Ø© Ø¨Ø§ÙÙØ¹Ù."
            before = snap.get("before") if isinstance(snap, dict) else None
            after = snap.get("after") if isinstance(snap, dict) else None
            if not before or not after:
                await release("conflict", "Legacy undo record has no after-state fingerprint")
                return "Ø§ÙØ³Ø¬Ù ÙØ¯ÙÙ ÙÙØ§ ÙØ­ØªÙÙ Ø¹ÙÙ Ø¨ØµÙØ© Ø§ÙØ­Ø§ÙØ© Ø¨Ø¹Ø¯ Ø§ÙØªØ¹Ø¯ÙÙØ Ø£ÙÙÙØª Ø§ÙØªØ±Ø§Ø¬Ø¹ Ø§ÙØ¢ÙÙ ÙØ­ÙØ§ÙØ© Ø§ÙØªØ¹Ø¯ÙÙØ§Øª Ø§ÙØ£Ø­Ø¯Ø«."
            if _channel_snapshot(channel) != after:
                await release("conflict", "Channel changed after recorded bot action")
                return "Ø§ÙÙÙØ§Ø© Ø§ØªØºÙØ±Øª Ø¨Ø¹Ø¯ Ø§ÙØ¥Ø¬Ø±Ø§Ø¡ Ø§ÙÙØ³Ø¬Ù. Ø£ÙÙÙØª Ø§ÙØªØ±Ø§Ø¬Ø¹ Ø¨Ø¯Ù ÙØ§ Ø£Ø³ØªØ¨Ø¯Ù ØªØ¹Ø¯ÙÙÙØ§ Ø£Ø­Ø¯Ø«."
            kwargs = {k: v for k, v in before.items() if k in {
                "name", "topic", "nsfw", "slowmode_delay", "bitrate", "user_limit", "position"
            }}
            before_category = before.get("category_id")
            kwargs["category"] = guild.get_channel(int(before_category)) if before_category else None
            restored_overwrites = {}
            for raw_target_id, permission_data in before.get("overwrites", {}).items():
                target_id_int = int(raw_target_id)
                target = guild.get_role(target_id_int) if permission_data.get("kind") == "role" else guild.get_member(target_id_int)
                if target is None and permission_data.get("kind") == "member":
                    try:
                        target = await guild.fetch_member(target_id_int)
                    except discord.NotFound:
                        target = None
                if target is None:
                    await release("conflict", f"Permission overwrite target {target_id_int} no longer exists")
                    return "ØªØ¹Ø°Ø± Ø§Ø³ØªØ¹Ø§Ø¯Ø© ØµÙØ§Ø­ÙØ§Øª Ø§ÙÙÙØ§Ø© ÙØ£Ù Ø£Ø­Ø¯ Ø§ÙØ£Ø¹Ø¶Ø§Ø¡ Ø£Ù Ø§ÙØ±ØªØ¨ Ø§ÙÙØ³ØªÙØ¯ÙØ© ÙÙ ÙØ¹Ø¯ ÙÙØ¬ÙØ¯ÙØ§."
                allow = discord.Permissions(int(permission_data.get("allow", 0)))
                deny = discord.Permissions(int(permission_data.get("deny", 0)))
                restored_overwrites[target] = discord.PermissionOverwrite.from_pair(allow, deny)
            kwargs["overwrites"] = restored_overwrites
            _suppress_undo_record(guild.id, "channel_update", channel.id)
            await channel.edit(reason=f"Undo action #{action_id} requested by {interaction.user}", **kwargs)
        else:
            await release("conflict", "Unsupported undo action type")
            return "ÙÙØ¹ Ø§ÙØ¥Ø¬Ø±Ø§Ø¡ Ø¯Ù ÙØ´ ÙØ¯Ø¹ÙÙ ÙÙØªØ±Ø§Ø¬Ø¹."
    except discord.Forbidden:
        await release("pending", "Discord permission denied")
        return "Discord Ø±ÙØ¶ Ø§ÙØ¹ÙÙÙØ©. Ø±Ø§Ø¬Ø¹ ØµÙØ§Ø­ÙØ§Øª Ø§ÙØ¨ÙØª ÙØªØ±ØªÙØ¨ Ø§ÙØ±ØªØ¨."
    except discord.HTTPException as exc:
        await release("pending", f"Discord HTTP error: {type(exc).__name__}")
        return f"ÙØ´Ù Ø§ÙØªØ±Ø§Ø¬Ø¹ Ø¨Ø³Ø¨Ø¨ Ø®Ø·Ø£ ÙÙ Discord: {type(exc).__name__}."
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        await release("conflict", f"Invalid undo snapshot: {type(exc).__name__}")
        return "Ø¨ÙØ§ÙØ§Øª Ø³Ø¬Ù Ø§ÙØªØ±Ø§Ø¬Ø¹ ØºÙØ± ØµØ§ÙØ­Ø©Ø Ø£ÙÙÙØª Ø§ÙØªÙÙÙØ° ÙØ­ÙØ§ÙØ© Ø§ÙØ³ÙØ±ÙØ±."
    except Exception as exc:
        await release("pending", f"Unexpected error: {type(exc).__name__}")
        return f"Ø­ØµÙ Ø®Ø·Ø£ ØºÙØ± ÙØªÙÙØ¹ Ø£Ø«ÙØ§Ø¡ Ø§ÙØªØ±Ø§Ø¬Ø¹: {type(exc).__name__}."

    async with await _db() as db:
        cur = await db.execute(
            "UPDATE ops_undo_actions SET undone=1,status='completed',undone_at=?,claimed_by=NULL,claimed_at=NULL "
            "WHERE id=? AND undone=0 AND status='executing' AND claimed_by=?",
            (_now().isoformat(), action_id, interaction.user.id),
        )
        await db.commit()
        if cur.rowcount != 1:
            await _incident(guild.id, "undo_state_conflict", f"Discord action was reversed but DB claim could not be finalized for undo #{action_id}", interaction.user.id, target_id)
            return "ØªÙ ØªÙÙÙØ° Ø§ÙØªØ±Ø§Ø¬Ø¹ ÙÙ DiscordØ ÙÙÙ ØªØ¹Ø°Ø± ØªØ«Ø¨ÙØª Ø­Ø§ÙØªÙ ÙÙ ÙØ§Ø¹Ø¯Ø© Ø§ÙØ¨ÙØ§ÙØ§Øª. Ø±Ø§Ø¬Ø¹ Ø³Ø¬Ù Ø§ÙØ­ÙØ§Ø¯Ø« ÙÙØ±ÙØ§."
    await _incident(guild.id, "undo", f"Undo action #{action_id} ({action_type})", interaction.user.id, target_id)
    return f"â ØªÙ Ø§ÙØªØ±Ø§Ø¬Ø¹ Ø¹Ù Ø§ÙØ¥Ø¬Ø±Ø§Ø¡ Ø§ÙÙØ³Ø¬Ù #{action_id}."


_NUKE_PERMISSION_FLAGS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "ban_members", "kick_members", "moderate_members",
)


def _role_is_privileged(role: discord.Role) -> bool:
    permissions = role.permissions
    return any(bool(getattr(permissions, flag, False)) for flag in _NUKE_PERMISSION_FLAGS)


async def _contain_nuke_actor(guild: discord.Guild, actor_id: int) -> str:
    """Opt-in containment: strip manageable privileged roles and apply a configured quarantine role.

    No bans or mass actions are performed. A durable role snapshot is written before the
    Discord edit so an administrator can explicitly release and restore the actor later.
    """
    if actor_id == guild.owner_id:
        return "Containment skipped: the guild owner cannot be modified."
    me = guild.me
    if me is not None and actor_id == me.id:
        return "Containment skipped: the bot cannot quarantine itself."
    if me is None or not me.guild_permissions.manage_roles:
        return "Containment unavailable: bot needs Manage Roles."
    try:
        quarantine_id = int(await _setting(guild.id, "anti_nuke_quarantine_role", "0"))
    except (TypeError, ValueError):
        quarantine_id = 0
    quarantine_role = guild.get_role(quarantine_id) if quarantine_id else None
    if (quarantine_role is None or quarantine_role.is_default() or quarantine_role.managed
            or _role_is_privileged(quarantine_role) or me.top_role <= quarantine_role):
        return "Containment unavailable: configure a manageable, non-privileged quarantine role below the bot."
    member = guild.get_member(actor_id)
    if member is None:
        try:
            member = await guild.fetch_member(actor_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return "Containment unavailable: actor is no longer accessible."
    privileged = [role for role in member.roles if role.id != guild.id and _role_is_privileged(role)]
    if not privileged:
        return "No privileged roles were found on the actor; no role changes were made."
    if any(role.managed or me.top_role <= role for role in privileged):
        return "Containment blocked: at least one privileged role is above the bot or managed."
    async with await _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT id FROM ops_nuke_quarantines WHERE guild_id=? AND actor_id=? AND status IN ('pending','quarantined','partial','releasing') ORDER BY id DESC LIMIT 1",
            (guild.id, actor_id),
        )
        if await cur.fetchone():
            await db.commit()
            return "Actor already has an active quarantine record."
        removed_ids = [role.id for role in privileged]
        cur = await db.execute(
            "INSERT INTO ops_nuke_quarantines(guild_id,actor_id,removed_roles,quarantine_role_id,status,created_at) VALUES(?,?,?,?,?,?)",
            (guild.id, actor_id, json.dumps(removed_ids), quarantine_role.id, "pending", _now().isoformat()),
        )
        record_id = cur.lastrowid
        await db.commit()
    retained = [role for role in member.roles if role.id != guild.id and role not in privileged and role.id != quarantine_role.id]
    try:
        await member.edit(roles=retained + [quarantine_role], reason="DragonSlayerBot Anti-Nuke containment after confirmed destructive-action burst")
    except (discord.Forbidden, discord.HTTPException) as exc:
        async with await _db() as db:
            await db.execute("UPDATE ops_nuke_quarantines SET status='failed',failure=? WHERE id=?", (type(exc).__name__, record_id))
            await db.commit()
        return f"Containment failed at Discord: {type(exc).__name__}. Review the incident immediately."
    async with await _db() as db:
        await db.execute("UPDATE ops_nuke_quarantines SET status='quarantined' WHERE id=? AND status='pending'", (record_id,))
        await db.commit()
    await _incident(guild.id, "anti_nuke_containment", "Privileged roles removed and quarantine role applied after destructive-action burst", target_id=actor_id,
                    details={"record_id": record_id, "removed_role_ids": removed_ids, "quarantine_role_id": quarantine_role.id})
    return f"Containment applied. Removed {len(privileged)} privileged role(s); record #{record_id} is available for reviewed release."


async def anti_nuke_release_cmd(interaction: discord.Interaction, member: discord.Member):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
        return await interaction.response.send_message("â ÙØ­ØªØ§Ø¬ ØµÙØ§Ø­ÙØ© Administrator Ø£Ù Manage Server.", ephemeral=True)
    async with await _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT id,removed_roles,quarantine_role_id,status FROM ops_nuke_quarantines WHERE guild_id=? AND actor_id=? AND status IN ('quarantined','pending','partial') ORDER BY id DESC LIMIT 1",
            (guild.id, member.id),
        )
        row = await cur.fetchone()
        if not row:
            await db.commit()
            return await interaction.response.send_message("ÙÙÙØ´ Ø³Ø¬Ù Ø¹Ø²Ù ÙØ´Ø· ÙØ§Ø¨Ù ÙÙÙØ±Ø§Ø¬Ø¹Ø© ÙÙØ¹Ø¶Ù Ø¯Ù.", ephemeral=True)
        record_id, raw_roles, quarantine_id, previous_status = row
        cur = await db.execute(
            "UPDATE ops_nuke_quarantines SET status='releasing' WHERE id=? AND status=?",
            (record_id, previous_status),
        )
        if cur.rowcount != 1:
            await db.commit()
            return await interaction.response.send_message("Ø­Ø§ÙØ© Ø³Ø¬Ù Ø§ÙØ¹Ø²Ù Ø§ØªØºÙØ±Øª Ø¨Ø§ÙØªØ²Ø§ÙÙØ Ø£Ø¹Ø¯ ÙØ±Ø§Ø¬Ø¹Ø© Ø§ÙØ­Ø§Ø¯Ø«.", ephemeral=True)
        await db.commit()
    async def release_claim():
        async with await _db() as db:
            await db.execute(
                "UPDATE ops_nuke_quarantines SET status=? WHERE id=? AND status='releasing'",
                (previous_status, record_id),
            )
            await db.commit()
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await release_claim()
        return await interaction.response.send_message("Ø§ÙØ¨ÙØª ÙØ­ØªØ§Ø¬ Manage Roles ÙØ§Ø³ØªØ¹Ø§Ø¯Ø© Ø§ÙØ±ØªØ¨.", ephemeral=True)
    restored = []
    missing = []
    try:
        role_ids = [int(value) for value in json.loads(raw_roles)]
    except (TypeError, ValueError, json.JSONDecodeError):
        await release_claim()
        return await interaction.response.send_message("Ø³Ø¬Ù Ø§ÙØ±ØªØ¨ ØºÙØ± ØµØ§ÙØ­Ø Ø£ÙÙÙØª Ø§ÙØ§Ø³ØªØ¹Ø§Ø¯Ø© ÙØ­ÙØ§ÙØ© Ø§ÙØµÙØ§Ø­ÙØ§Øª.", ephemeral=True)
    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role is None:
            missing.append(role_id)
        elif role.managed or me.top_role <= role:
            await release_claim()
            return await interaction.response.send_message(f"ÙØ§ ÙÙÙÙ Ø§Ø³ØªØ¹Ø§Ø¯Ø© Ø§ÙØ±ØªØ¨Ø© `{role_id}` Ø¨Ø³Ø¨Ø¨ ØªØ±ØªÙØ¨ Ø§ÙØ±ØªØ¨ Ø£Ù Ø¥Ø¯Ø§Ø±ØªÙØ§.", ephemeral=True)
        else:
            restored.append(role)
    quarantine = guild.get_role(int(quarantine_id))
    if quarantine and (quarantine.managed or me.top_role <= quarantine):
        await release_claim()
        return await interaction.response.send_message("ÙØ§ ÙÙÙÙ Ø¥Ø²Ø§ÙØ© Ø±ØªØ¨Ø© Ø§ÙØ¹Ø²Ù Ø¨Ø³Ø¨Ø¨ ØªØ±ØªÙØ¨ Ø§ÙØ±ØªØ¨.", ephemeral=True)
    roles = [role for role in member.roles if role.id not in {guild.id, int(quarantine_id)}]
    roles = list({role.id: role for role in roles + restored}.values())
    try:
        await member.edit(roles=roles, reason=f"Anti-Nuke quarantine release approved by {interaction.user}")
    except (discord.Forbidden, discord.HTTPException) as exc:
        await release_claim()
        return await interaction.response.send_message(f"ÙØ´ÙØª Ø§Ø³ØªØ¹Ø§Ø¯Ø© Ø§ÙØ±ØªØ¨: {type(exc).__name__}.", ephemeral=True)
    async with await _db() as db:
        await db.execute("UPDATE ops_nuke_quarantines SET status='released',released_at=? WHERE id=? AND status='releasing'", (_now().isoformat(), record_id))
        await db.commit()
    await _incident(guild.id, "anti_nuke_release", "Quarantined actor roles restored by administrator", interaction.user.id, member.id,
                    {"record_id": record_id, "missing_role_ids": missing})
    await interaction.response.send_message(f"ØªÙ ÙÙ Ø§ÙØ¹Ø²Ù ÙØ§Ø³ØªØ¹Ø§Ø¯Ø© {len(restored)} Ø±ØªØ¨Ø©. Ø§ÙØ±ØªØ¨ ØºÙØ± Ø§ÙÙÙØ¬ÙØ¯Ø©: {len(missing)}.", ephemeral=True)


async def anti_nuke_config_cmd(interaction: discord.Interaction, enabled: bool,
                               threshold: app_commands.Range[int, 2, 10],
                               window_seconds: app_commands.Range[int, 10, 180],
                               log_channel: discord.TextChannel,
                               quarantine_role: discord.Role):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
        return await interaction.response.send_message("â ÙØ­ØªØ§Ø¬ ØµÙØ§Ø­ÙØ© Administrator Ø£Ù Manage Server.", ephemeral=True)
    me = guild.me
    if (log_channel.guild.id != guild.id or me is None or not me.guild_permissions.manage_roles
            or not me.guild_permissions.view_audit_log or not me.guild_permissions.send_messages):
        return await interaction.response.send_message("Ø§ÙØ¨ÙØª ÙØ­ØªØ§Ø¬ Manage Roles ÙView Audit Log ÙSend MessagesØ ÙØ§ÙÙÙØ§Ø© ÙØ§Ø²Ù ØªÙÙÙ ÙÙ ÙÙØ³ Ø§ÙØ³ÙØ±ÙØ±.", ephemeral=True)
    if (quarantine_role.guild.id != guild.id or quarantine_role.is_default() or quarantine_role.managed
            or _role_is_privileged(quarantine_role) or me.top_role <= quarantine_role):
        return await interaction.response.send_message("Ø±ØªØ¨Ø© Ø§ÙØ¹Ø²Ù ÙØ§Ø²Ù ØªÙÙÙ ØºÙØ± Ø¥Ø¯Ø§Ø±ÙØ©Ø ØºÙØ± ÙÙØ¯Ø§Ø±Ø©Ø ÙØªØ­Øª Ø£Ø¹ÙÙ Ø±ØªØ¨Ø© ÙÙØ¨ÙØª.", ephemeral=True)
    perms = log_channel.permissions_for(me)
    if not perms.send_messages or not perms.embed_links:
        return await interaction.response.send_message("Ø§ÙØ¨ÙØª ÙØ­ØªØ§Ø¬ Send Messages ÙEmbed Links ÙÙ ÙÙØ§Ø© Ø§ÙØªÙØ¨ÙÙØ§Øª.", ephemeral=True)
    await _set_setting(guild.id, "anti_nuke_enabled", str(int(enabled)))
    await _set_setting(guild.id, "anti_nuke_threshold", str(threshold))
    await _set_setting(guild.id, "anti_nuke_window", str(window_seconds))
    await _set_setting(guild.id, "anti_nuke_log_channel", str(log_channel.id))
    await _set_setting(guild.id, "anti_nuke_quarantine_role", str(quarantine_role.id))
    await _incident(guild.id, "anti_nuke_config", f"Anti-Nuke {'enabled' if enabled else 'disabled'}; threshold={threshold}/{window_seconds}s", interaction.user.id, quarantine_role.id)
    await interaction.response.send_message(
        f"Anti-Nuke {'enabled' if enabled else 'disabled'}. It groups destructive actions by the same actor, then removes manageable privileged roles and applies {quarantine_role.mention}. Guild owner, bot hierarchy and managed roles are protected. Release is manual with `/anti-nuke-release`.", ephemeral=True)


async def _security_alert(guild: discord.Guild, rule: str, summary: str,
                          actor_id: int | None = None, target_id: int | None = None) -> bool:
    """Detect destructive bursts per actor and optionally apply configured role quarantine."""
    now = _now()
    anti_nuke_enabled = await _setting(guild.id, "anti_nuke_enabled", "0") == "1"
    try:
        threshold_key = "anti_nuke_threshold" if anti_nuke_enabled else "security_delete_threshold"
        window_key = "anti_nuke_window" if anti_nuke_enabled else "security_delete_window"
        threshold = max(2, min(20, int(await _setting(guild.id, threshold_key, "4"))))
        window = max(10, min(300, int(await _setting(guild.id, window_key, "60"))))
    except (TypeError, ValueError):
        threshold, window = 4, 60
    since = (now - dt.timedelta(seconds=window)).isoformat()
    event_types = ("role_delete", "channel_delete", "member_ban", "member_kick", "webhook_delete", "role_privilege_escalation")
    async with await _db() as db:
        placeholders = ",".join("?" for _ in event_types)
        query = f"SELECT COUNT(*) FROM ops_incidents WHERE guild_id=? AND event_type IN ({placeholders}) AND created_at>=?"
        params: tuple[Any, ...] = (guild.id, *event_types, since)
        if actor_id is not None:
            query += " AND actor_id=?"
            params += (actor_id,)
        cur = await db.execute(query, params)
        count = int((await cur.fetchone())[0] or 0)
        if count < threshold:
            return False
        rule_key = f"destructive_resource_burst:{actor_id or 'unknown'}"
        cur = await db.execute("SELECT last_alert_at FROM ops_security_alert_state WHERE guild_id=? AND rule=?", (guild.id, rule_key))
        row = await cur.fetchone()
        if row:
            try:
                last = dt.datetime.fromisoformat(str(row[0]))
                if (now - last).total_seconds() < window:
                    return False
            except ValueError:
                pass
        await db.execute(
            "INSERT INTO ops_security_alert_state(guild_id,rule,last_alert_at) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,rule) DO UPDATE SET last_alert_at=excluded.last_alert_at",
            (guild.id, rule_key, now.isoformat()),
        )
        await db.commit()
    containment = "Containment not attempted."
    if actor_id is not None and anti_nuke_enabled:
        containment = await _contain_nuke_actor(guild, actor_id)
    await _incident(guild.id, "security_alert", summary, actor_id, target_id,
                    {"rule": "destructive_resource_burst", "count": count,
                     "threshold": threshold, "window_seconds": window, "containment": containment})
    channel_key = "anti_nuke_log_channel" if anti_nuke_enabled else "security_log_channel"
    channel_id = int(await _setting(guild.id, channel_key, "0") or 0)
    channel = guild.get_channel(channel_id) if channel_id else None
    if isinstance(channel, discord.TextChannel):
        embed = discord.Embed(
            title="ð¡ï¸ Anti-Nuke | Suspicious Destructive Burst",
            description=(f"{summary}\n\nDetected **{count}** destructive actions by the same actor within **{window}s** "
                         f"(threshold: {threshold}).\nActor: {f'<@{actor_id}>' if actor_id else 'Unknown / audit log unavailable'}\n"
                         f"Containment: **{containment}**"),
            colour=discord.Colour.red(), timestamp=now,
        )
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
    return True


async def security_config_cmd(interaction: discord.Interaction,
                              log_channel: discord.TextChannel,
                              threshold: app_commands.Range[int, 2, 20] = 4,
                              window_seconds: app_commands.Range[int, 10, 300] = 60):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
        return await interaction.response.send_message("â ÙØ­ØªØ§Ø¬ ØµÙØ§Ø­ÙØ© Administrator Ø£Ù Manage Server.", ephemeral=True)
    me = guild.me
    if me is None or not me.guild_permissions.send_messages or not me.guild_permissions.embed_links:
        return await interaction.response.send_message("â Ø§ÙØ¨ÙØª ÙØ­ØªØ§Ø¬ Send Messages ÙEmbed Links ÙÙ ÙÙØ§Ø© Ø§ÙØªÙØ¨ÙÙØ§Øª.", ephemeral=True)
    if log_channel.guild.id != guild.id:
        return await interaction.response.send_message("â Ø§Ø®ØªØ§Ø± ÙÙØ§Ø© ÙÙ ÙÙØ³ Ø§ÙØ³ÙØ±ÙØ±.", ephemeral=True)
    perms = log_channel.permissions_for(me)
    if not perms.send_messages or not perms.embed_links:
        return await interaction.response.send_message("â Ø§ÙØ¨ÙØª ÙØ§ ÙØ³ØªØ·ÙØ¹ Ø¥Ø±Ø³Ø§Ù Ø§ÙØªÙØ¨ÙÙØ§Øª ÙÙ Ø§ÙÙÙØ§Ø© Ø§ÙÙØ®ØªØ§Ø±Ø©.", ephemeral=True)
    await _set_setting(guild.id, "security_log_channel", str(log_channel.id))
    await _set_setting(guild.id, "security_delete_threshold", str(threshold))
    await _set_setting(guild.id, "security_delete_window", str(window_seconds))
    await _incident(guild.id, "security_config", f"Security alert channel set to {log_channel.id}; threshold={threshold}/{window_seconds}s", interaction.user.id, log_channel.id)
    await interaction.response.send_message(
        f"â Security alerts configured in {log_channel.mention}. Alert after {threshold} role/channel deletions or member bans within {window_seconds}s. Detection is alert-only; review incidents before taking action.",
        ephemeral=True,
    )


DASHBOARD_PAGES = {
    "overview": ("Overview", "ð"),
    "analytics": ("Analytics", "ð"),
    "security": ("Security", "ð¡ï¸"),
    "incidents": ("Incidents", "ð§¾"),
    "undo": ("Undo history", "â©ï¸"),
    "health": ("Database health", "ð©º"),
    "tickets": ("Ticket queue", "ð«"),
    "system": ("System status", "âï¸"),
    "plugins": ("Extensions", "ð§©"),
    "analyst": ("AI insights", "ð§ "),
}


async def _dashboard_page_embed(guild: discord.Guild, page: str, bot: discord.Client | None = None) -> discord.Embed:
    """Build a read-only dashboard page from this guild's own records."""
    if page not in DASHBOARD_PAGES:
        page = "overview"
    title, emoji = DASHBOARD_PAGES[page]
    embed = discord.Embed(
        title=f"ð DragonSlayerBot | {title}",
        colour=discord.Colour.dark_purple(),
        timestamp=_now(),
    )
    if page == "overview":
        embed.description = "ÙØ±ÙØ² ØªØ­ÙÙ ÙÙØ­Ø¯ Ø¯Ø§Ø®Ù Discord. ÙÙ Ø¥Ø¬Ø±Ø§Ø¡ ÙØ®Ø¶Ø¹ ÙØµÙØ§Ø­ÙØ§Øª Discord ÙØªØ±ØªÙØ¨ Ø§ÙØ±ØªØ¨."
        embed.add_field(name="Server", value=f"Members: **{guild.member_count or len(guild.members):,}**\nChannels: **{len(guild.channels)}**\nRoles: **{max(0, len(guild.roles)-1)}**", inline=True)
        async with await _db() as db:
            raid = await db.execute("SELECT key,value FROM ops_settings WHERE guild_id=? AND key IN ('raid_enabled','raid_threshold','raid_window','raid_role','raid_until')", (guild.id,))
            raid_settings = {str(r[0]): str(r[1]) for r in await raid.fetchall()}
            sec = await db.execute("SELECT key,value FROM ops_settings WHERE guild_id=? AND key IN ('security_log_channel','security_delete_threshold','security_delete_window','anti_nuke_enabled','anti_nuke_threshold','anti_nuke_window','anti_nuke_quarantine_role','anti_nuke_log_channel')", (guild.id,))
            security_settings = {str(r[0]): str(r[1]) for r in await sec.fetchall()}
            count = await db.execute("SELECT COUNT(*) FROM ops_incidents WHERE guild_id=? AND created_at>=?", (guild.id, (_now()-dt.timedelta(days=7)).isoformat()))
            incident_count = int((await count.fetchone())[0])
        embed.add_field(name="Anti-Raid", value=f"Status: **{'Enabled' if raid_settings.get('raid_enabled') == '1' else 'Disabled'}**\nThreshold: **{raid_settings.get('raid_threshold', '4')} joins / {raid_settings.get('raid_window', '30')}s**", inline=True)
        embed.add_field(name="Security alerts", value=f"Channel: **{'Configured' if security_settings.get('security_log_channel', '0') != '0' else 'Not configured'}**\nAlert threshold: **{security_settings.get('security_delete_threshold', '4')} / {security_settings.get('security_delete_window', '60')}s**", inline=True)
        embed.add_field(name="Recent activity", value=f"Incidents in last 7 days: **{incident_count}**", inline=False)
        embed.set_footer(text="Choose a page from the menu. Dashboard controls are scoped to this server.")
    elif page == "analytics":
        async with await _db() as db:
            cur = await db.execute("SELECT message_count,join_count,leave_count,active_users FROM ops_activity_daily WHERE guild_id=? AND day=?", (guild.id, _day()))
            today = await cur.fetchone()
            cur = await db.execute("SELECT COALESCE(SUM(message_count),0),COALESCE(SUM(join_count),0),COALESCE(SUM(leave_count),0),COALESCE(SUM(active_users),0) FROM ops_activity_daily WHERE guild_id=? AND day>=date('now','-6 day')", (guild.id,))
            week = await cur.fetchone()
        t, w = tuple(today or (0, 0, 0, 0)), tuple(week or (0, 0, 0, 0))
        embed.add_field(name="Today", value=f"Messages: **{t[0]}**\nActive members: **{t[3]}**\nJoins: **{t[1]}** | Leaves: **{t[2]}**", inline=False)
        embed.add_field(name="Last 7 days", value=f"Messages: **{w[0]}**\nActive-user days: **{w[3]}**\nJoins: **{w[1]}** | Leaves: **{w[2]}**", inline=False)
        embed.set_footer(text="Tracking begins when the extension is enabled; historical activity is not fabricated.")
    elif page == "security":
        async with await _db() as db:
            cur = await db.execute("SELECT key,value FROM ops_settings WHERE guild_id=? AND key IN ('raid_enabled','raid_threshold','raid_window','raid_role','raid_until','security_log_channel','security_delete_threshold','security_delete_window','anti_nuke_enabled','anti_nuke_threshold','anti_nuke_window','anti_nuke_quarantine_role','anti_nuke_log_channel')", (guild.id,))
            settings = {str(r[0]): str(r[1]) for r in await cur.fetchall()}
        quarantine = settings.get("raid_role", "0")
        channel = settings.get("security_log_channel", "0")
        embed.description = "Ø­Ø§ÙØ© Ø£ÙØ¸ÙØ© Ø§ÙØ±ØµØ¯ Ø§ÙØ­Ø§ÙÙØ©. ÙØ°Ù Ø§ÙØµÙØ­Ø© ÙÙØ¹Ø±Ø¶Ø Ø§Ø³ØªØ®Ø¯Ù Ø£ÙØ§ÙØ± Ø§ÙØ¥Ø¹Ø¯Ø§Ø¯Ø§Øª ÙØªØºÙÙØ± Ø§ÙÙÙÙ."
        try:
            raid_active = float(settings.get("raid_until", "0")) > _now().timestamp()
        except (TypeError, ValueError):
            raid_active = False
        embed.add_field(name="Anti-Raid", value=f"Enabled: **{'Yes' if settings.get('raid_enabled') == '1' else 'No'}**\nActive quarantine window: **{'Yes' if raid_active else 'No'}**\nJoin threshold: **{settings.get('raid_threshold', '4')}**\nWindow: **{settings.get('raid_window', '30')} seconds**\nQuarantine role: **{f'<@&{quarantine}>' if quarantine != '0' else 'Not configured'}**", inline=False)
        nuke_role_id = settings.get("anti_nuke_quarantine_role", "0")
        nuke_channel_id = settings.get("anti_nuke_log_channel", "0")
        embed.add_field(name="Destructive-action alerts", value=f"Alert channel: **{f'<#{channel}>' if channel != '0' else 'Not configured'}**\nThreshold: **{settings.get('security_delete_threshold', '4')} events / {settings.get('security_delete_window', '60')} seconds**", inline=False)
        embed.add_field(name="Anti-Nuke containment", value=f"Enabled: **{'Yes' if settings.get('anti_nuke_enabled') == '1' else 'No'}**\nThreshold: **{settings.get('anti_nuke_threshold', '4')} actions / {settings.get('anti_nuke_window', '60')} seconds**\nAlert channel: **{f'<#{nuke_channel_id}>' if nuke_channel_id != '0' else 'Not configured'}**\nQuarantine role: **{f'<@&{nuke_role_id}>' if nuke_role_id != '0' else 'Not configured'}**\nRelease: manual administrator review", inline=False)
        embed.set_footer(text="Anti-Nuke is opt-in; role hierarchy and guild-owner safeguards are enforced.")
    elif page == "incidents":
        async with await _db() as db:
            cur = await db.execute("SELECT id,event_type,actor_id,target_id,summary,created_at FROM ops_incidents WHERE guild_id=? ORDER BY id DESC LIMIT 10", (guild.id,))
            rows = await cur.fetchall()
        if rows:
            embed.description = "\n\n".join(f"`#{r[0]}` **{str(r[1])[:50]}** Â· <t:{int(dt.datetime.fromisoformat(r[5]).timestamp())}:R>\nActor: {f'<@{r[2]}>' if r[2] else 'Unknown'} Â· Target: `{r[3] or 'N/A'}`\n{str(r[4])[:160]}" for r in rows)[:3900]
        else:
            embed.description = "No incidents recorded yet."
    elif page == "undo":
        async with await _db() as db:
            cur = await db.execute("SELECT id,action_type,target_id,created_at FROM ops_undo_actions WHERE guild_id=? AND undone=0 ORDER BY id DESC LIMIT 5", (guild.id,))
            rows = await cur.fetchall()
        embed.description = "Ø¢Ø®Ø± Ø§ÙØ¥Ø¬Ø±Ø§Ø¡Ø§Øª Ø§ÙÙØ¯Ø¹ÙÙØ© Ø§ÙØªÙ ÙÙ ÙØªÙ Ø§ÙØªØ±Ø§Ø¬Ø¹ Ø¹ÙÙØ§. Ø§ÙØªØ±Ø§Ø¬Ø¹ ÙØªØ·ÙØ¨ ØªØ£ÙÙØ¯ÙØ§ ÙÙÙØµÙÙØ§."
        embed.add_field(name="Pending undo records", value="\n".join(f"`#{r[0]}` **{str(r[1])[:40]}** Â· target `{r[2] or 'N/A'}` Â· <t:{int(dt.datetime.fromisoformat(r[3]).timestamp())}:R>" for r in rows) if rows else "No undoable actions.", inline=False)
    elif page == "health":
        try:
            async with await _db() as db:
                cur = await db.execute("PRAGMA quick_check")
                row = await cur.fetchone()
            state = str(row[0]) if row else "unknown"
        except Exception as exc:
            state = f"error: {type(exc).__name__}"
        embed.add_field(name="SQLite quick_check", value=f"**{state}**", inline=False)
        embed.set_footer(text="Read-only integrity check. This does not validate migrations, schema completeness, or live Discord connectivity.")
    elif page == "tickets":
        async with await _db() as db:
            cur = await db.execute(
                "SELECT user_id,channel_id,COALESCE(priority,'normal'),claimed_by,created_at,status "
                "FROM tickets WHERE guild_id=? AND status IN ('open','closing') "
                "ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,created_at ASC LIMIT 10",
                (guild.id,),
            )
            rows = await cur.fetchall()
        if not rows:
            embed.description = "ÙÙÙØ´ ØªØ°Ø§ÙØ± ÙÙØªÙØ­Ø© Ø­Ø§ÙÙÙØ§."
        else:
            now = _now()
            lines = []
            for user_id, channel_id, priority, claimed_by, created, ticket_status in rows:
                channel = guild.get_channel(int(channel_id))
                try:
                    opened = dt.datetime.fromisoformat(created) if created else now
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=UTC)
                    age = max(0, int((now-opened).total_seconds() // 3600))
                except (TypeError, ValueError):
                    age = 0
                target = channel.mention if channel else f"`#{channel_id}` (channel unavailable)"
                state_label = "CLOSING / retryable" if ticket_status == "closing" else "OPEN"
                lines.append(f"**{state_label} Â· {str(priority).upper()}** Â· {target} Â· owner <@{user_id}> Â· {f'<@{claimed_by}>' if claimed_by else 'unclaimed'} Â· **{age}h**")
            embed.description = "\n".join(lines)[:3900]
        embed.set_footer(text="Claim, priority and close controls remain available inside each ticket's native Discord staff controls.")
    elif page == "system":
        ready = bool(bot and bot.is_ready())
        latency = getattr(bot, "latency", None) if ready else None
        extensions = sorted(getattr(bot, "loaded_plugins", set())) if bot else []
        embed.add_field(name="Gateway", value=f"Ready: **{'Yes' if ready else 'No / unavailable'}**\nLatency: **{round(latency*1000, 1)} ms**" if latency is not None else f"Ready: **{'Yes' if ready else 'No / unavailable'}**\nLatency: **Unavailable**", inline=False)
        embed.add_field(name="Loaded extensions", value="\n".join(f"â¢ `{name}`" for name in extensions) if extensions else "No extension registry available.", inline=False)
        embed.set_footer(text="Process memory and host-level telemetry are not exposed by this build.")
    elif page == "plugins":
        extensions = sorted(getattr(bot, "loaded_plugins", set())) if bot else []
        embed.description = "Extensions loaded in this bot process. A loaded extension is not proof that every feature is healthy."
        embed.add_field(name="Loaded", value="\n".join(f"â¢ `{name}`" for name in extensions) if extensions else "No extensions are currently registered.", inline=False)
    elif page == "analyst":
        embed.description = "ØªØ­ÙÙÙ Ø§Ø®ØªÙØ§Ø±Ù ÙÙØ¤Ø´Ø±Ø§Øª ÙØ¬ÙØ¹Ø© ÙÙØ·. ÙØ§ ÙØªÙ Ø¥Ø±Ø³Ø§Ù ÙØ­ØªÙÙ Ø§ÙØ±Ø³Ø§Ø¦Ù Ø§ÙØ®Ø§ØµØ© Ø£Ù ÙÙÙØ§Øª Ø§ÙÙØ±ÙØ± Ø£Ù ÙÙØ§ØªÙØ­ API."
        embed.add_field(name="Data scope", value="Ø¢Ø®Ø± 7 Ø£ÙØ§Ù: Ø§ÙØ±Ø³Ø§Ø¦ÙØ Ø§ÙØ§ÙØ¶ÙØ§ÙØ§ØªØ Ø§ÙÙØºØ§Ø¯Ø±Ø§ØªØ ÙØ¹Ø¯Ø¯ Ø§ÙØ­ÙØ§Ø¯Ø« Ø­Ø³Ø¨ Ø§ÙÙÙØ¹.", inline=False)
        embed.set_footer(text="Use the AI insights button below. Requires a configured AI provider.")
    return embed


class DashboardPageSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=label, value=key, emoji=emoji, default=(key == "overview")) for key, (label, emoji) in DASHBOARD_PAGES.items()]
        super().__init__(placeholder="Navigate dashboard pagesâ¦", min_values=1, max_values=1, options=options, custom_id="dragon:admin_dashboard:page")

    async def callback(self, interaction: discord.Interaction):
        view: DashboardView = self.view  # type: ignore[assignment]
        if not await view.authorize(interaction):
            return
        page = self.values[0]
        view.current_page = page
        for option in self.options:
            option.default = option.value == page
        embed = await _dashboard_page_embed(interaction.guild, page, interaction.client)
        await interaction.response.edit_message(embed=embed, view=view)


class UndoConfirmView(discord.ui.View):
    def __init__(self, guild_id: int, actor_id: int):
        super().__init__(timeout=45)
        self.guild_id, self.actor_id = guild_id, actor_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.guild_id or interaction.user.id != self.actor_id:
            await interaction.response.send_message("â Ø§ÙØªØ£ÙÙØ¯ Ø¯Ù ÙØ´ ÙØ®ØµØµ ÙÙÙ Ø£Ù ÙÙØ³ÙØ±ÙØ± Ø¯Ù.", ephemeral=True)
            return False
        if not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
            await interaction.response.send_message("â ØµÙØ§Ø­ÙØ§Øª Ø§ÙØ¥Ø¯Ø§Ø±Ø© ÙÙ ØªØ¹Ø¯ ÙØªØ§Ø­Ø©.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm undo", style=discord.ButtonStyle.danger, emoji="â©ï¸")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        result = await _undo_last(interaction)
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(content=result, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="ØªÙ Ø¥ÙØºØ§Ø¡ Ø§ÙØªØ±Ø§Ø¬Ø¹.", view=self)


async def _send_dashboard_ai_insights(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    bot = interaction.client
    analyst = getattr(bot, "admin_analytics_ai", None)
    if guild is None or not callable(analyst):
        return await interaction.response.send_message("ØªØ­ÙÙÙ Ø§ÙØ°ÙØ§Ø¡ Ø§ÙØ§ØµØ·ÙØ§Ø¹Ù ØºÙØ± ÙØªØ§Ø­ Ø­Ø§ÙÙÙØ§. Ø±Ø§Ø¬Ø¹ Ø¥Ø¹Ø¯Ø§Ø¯ ÙØ²ÙØ¯ AI.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
        return await interaction.response.send_message("â ÙØ­ØªØ§Ø¬ ØµÙØ§Ø­ÙØ© Administrator Ø£Ù Manage Server.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        async with await _db() as db:
            cur = await db.execute(
                "SELECT COALESCE(SUM(message_count),0),COALESCE(SUM(join_count),0),COALESCE(SUM(leave_count),0),COALESCE(SUM(active_users),0) "
                "FROM ops_activity_daily WHERE guild_id=? AND day>=date('now','-6 day')",
                (guild.id,),
            )
            metrics = await cur.fetchone()
            cur = await db.execute(
                "SELECT event_type,COUNT(*) FROM ops_incidents WHERE guild_id=? AND created_at>=? GROUP BY event_type ORDER BY COUNT(*) DESC LIMIT 10",
                (guild.id, (_now()-dt.timedelta(days=7)).isoformat()),
            )
            incident_types = await cur.fetchall()
        metrics = tuple(metrics or (0, 0, 0, 0))
        payload = {
            "messages_7d": int(metrics[0]), "joins_7d": int(metrics[1]),
            "leaves_7d": int(metrics[2]), "active_user_days_7d": int(metrics[3]),
            "incident_types_7d": {str(row[0]): int(row[1]) for row in incident_types},
        }
        prompt = (
            "Analyze these aggregate seven-day server metrics only. Metrics: "
            + json.dumps(payload, ensure_ascii=False)
            + ". Give up to 4 concise evidence-based observations and practical recommendations. "
            "Do not invent missing context. Do not request private messages, credentials, tokens, or personal data."
        )
        answer = await analyst(prompt)
        if not answer:
            return await interaction.followup.send("ÙØ²ÙØ¯ AI ÙÙ ÙØ±Ø¬Ø¹ ØªØ­ÙÙÙÙØ§. Ø±Ø§Ø¬Ø¹ Ø§ÙÙÙØªØ§Ø­ ÙØ§ÙØ­ØµØ© ÙØ¥Ø¹Ø¯Ø§Ø¯Ø§Øª Ø§ÙÙØ²ÙØ¯.", ephemeral=True)
        embed = discord.Embed(title="ð§  AI Server Insights", description=str(answer)[:4000], colour=discord.Colour.blurple())
        embed.set_footer(text="Based only on aggregate counters and incident categories from the last 7 days.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"ØªØ¹Ø°Ø± Ø¥ÙØ´Ø§Ø¡ Ø§ÙØªØ­ÙÙÙ: {type(exc).__name__}.", ephemeral=True)


class AIBuilderModal(discord.ui.Modal, title="DragonSlayer AI Server Builder"):
    request = discord.ui.TextInput(
        label="Describe the server change or task",
        style=discord.TextStyle.paragraph,
        placeholder="Example: Create a support category with two ticket channels and set safe permissionsâ¦",
        min_length=5,
        max_length=2000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        bot = interaction.client
        if guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("â AI Server Builder works inside a server only.", ephemeral=True)
        required = ("ai_run", "ai_is_controller", "ai_consume_rate_limit", "ai_context_factory")
        if any(not callable(getattr(bot, name, None)) for name in required):
            return await interaction.response.send_message("â AI Server Builder is unavailable in this bot build.", ephemeral=True)
        try:
            context_factory = bot.ai_context_factory
            probe = context_factory(guild, interaction.user, channel=interaction.channel)
            if not await bot.ai_is_controller(probe):
                return await interaction.response.send_message("â You are not an authorized AI controller for this server.", ephemeral=True)
            allowed, retry_after = await bot.ai_consume_rate_limit(guild.id, interaction.user.id)
            if not allowed:
                return await interaction.response.send_message(f"â³ AI rate limit reached. Retry in {retry_after}s.", ephemeral=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            prompt = str(self.request.value).strip()
            ctx = context_factory(
                guild, interaction.user, can_use_tools=True, channel=interaction.channel,
                allow_memory_write=bool(getattr(bot, "ai_memory_write_intent", lambda value: False)(prompt)),
            )
            answer = await bot.ai_run(ctx, prompt)
            answer = str(answer or "AI returned no response.")
            for offset in range(0, len(answer), 1900):
                await interaction.followup.send(answer[offset:offset+1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            if interaction.response.is_done():
                await interaction.followup.send(f"AI Server Builder failed: {type(exc).__name__}.", ephemeral=True)
            else:
                await interaction.response.send_message(f"AI Server Builder failed: {type(exc).__name__}.", ephemeral=True)


class DashboardView(discord.ui.View):
    def __init__(self, owner_id: int | None = None, guild_id: int | None = None):
        super().__init__(timeout=600)
        self.owner_id, self.guild_id = owner_id, guild_id
        self.current_page = "overview"
        self.add_item(DashboardPageSelect())

    async def authorize(self, interaction: discord.Interaction) -> bool:
        if (interaction.guild is None or interaction.guild.id != self.guild_id
                or interaction.user.id != self.owner_id
                or not isinstance(interaction.user, discord.Member)
                or not _is_admin(interaction.user)):
            await interaction.response.send_message("â Ø§ÙÙÙØ­Ø© Ø¯Ù ÙØ±ØªØ¨Ø·Ø© Ø¨ÙØ§ØªØ­ÙØ§ ÙØ¨Ø§ÙØ³ÙØ±ÙØ± Ø§ÙØ£ØµÙÙØ ÙÙØ§Ø²Ù ØªÙØ¶Ù ÙØ¹Ø§Ù ØµÙØ§Ø­ÙØ© Ø§ÙØ¥Ø¯Ø§Ø±Ø©.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="ð", row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.authorize(interaction):
            return
        await interaction.response.edit_message(embed=await _dashboard_page_embed(interaction.guild, self.current_page, interaction.client), view=self)

    @discord.ui.button(label="Undo last action", style=discord.ButtonStyle.danger, emoji="â©ï¸", row=1)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.authorize(interaction):
            return
        await interaction.response.send_message("ØªØ£ÙÙØ¯ Ø§ÙØªØ±Ø§Ø¬Ø¹ Ø¹Ù Ø¢Ø®Ø± Ø¥Ø¬Ø±Ø§Ø¡ ÙØ¯Ø¹ÙÙ ÙÙØ³Ø¬Ù. Ø±Ø§Ø¬Ø¹ Ø³Ø¬Ù Ø§ÙØ¥Ø¬Ø±Ø§Ø¡Ø§Øª ÙØ¨Ù Ø§ÙØªØ£ÙÙØ¯.", view=UndoConfirmView(self.guild_id, self.owner_id), ephemeral=True)

    @discord.ui.button(label="AI insights", style=discord.ButtonStyle.secondary, emoji="ð§ ", row=1)
    async def ai_insights(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.authorize(interaction):
            return
        await _send_dashboard_ai_insights(interaction)

    @discord.ui.button(label="AI server builder", style=discord.ButtonStyle.success, emoji="ð ï¸", row=2)
    async def ai_server_builder(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.authorize(interaction):
            return
        await interaction.response.send_modal(AIBuilderModal())


async def _send_analytics(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    async with await _db() as db:
        cur = await db.execute(
            "SELECT message_count,join_count,leave_count,active_users FROM ops_activity_daily "
            "WHERE guild_id=? AND day=?", (guild.id, _day()),
        )
        today = await cur.fetchone()
        cur = await db.execute(
            "SELECT COALESCE(SUM(message_count),0),COALESCE(SUM(join_count),0),"
            "COALESCE(SUM(leave_count),0),COALESCE(SUM(active_users),0) "
            "FROM ops_activity_daily WHERE guild_id=? AND day>=date('now','-6 day')",
            (guild.id,),
        )
        week = await cur.fetchone()
        cur = await db.execute(
            "SELECT COUNT(*) FROM ops_incidents WHERE guild_id=? AND created_at>=?",
            (guild.id, (_now()-dt.timedelta(days=7)).isoformat()),
        )
        incidents = (await cur.fetchone())[0]
    t = tuple(today or (0, 0, 0, 0))
    w = tuple(week or (0, 0, 0, 0))
    embed = discord.Embed(title=f"ð {guild.name} | Server Analytics", colour=discord.Colour.blurple())
    embed.add_field(name="Members", value=f"{guild.member_count or len(guild.members):,}", inline=True)
    embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
    embed.add_field(name="Roles", value=str(max(0, len(guild.roles)-1)), inline=True)
    embed.add_field(name="Today", value=f"Messages: **{t[0]}**\nActive members: **{t[3]}**\nJoins: **{t[1]}** | Leaves: **{t[2]}**", inline=False)
    embed.add_field(name="Last 7 days", value=f"Messages: **{w[0]}**\nActive-user days: **{w[3]}**\nJoins: **{w[1]}** | Leaves: **{w[2]}**\nIncidents: **{incidents}**", inline=False)
    embed.set_footer(text="Activity tracking starts after this extension is enabled; no historical message data is fabricated.")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _send_incidents(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    async with await _db() as db:
        cur = await db.execute(
            "SELECT id,event_type,actor_id,target_id,summary,created_at FROM ops_incidents "
            "WHERE guild_id=? ORDER BY id DESC LIMIT 10", (guild.id,),
        )
        rows = await cur.fetchall()
    embed = discord.Embed(title="ð§¾ Recent Server Incidents", colour=discord.Colour.orange())
    if not rows:
        embed.description = "No incidents recorded yet."
    else:
        embed.description = "\n".join(
            f"`#{r[0]}` **{r[1]}** | <t:{int(dt.datetime.fromisoformat(r[5]).timestamp())}:R>\n"
            f"Actor: {f'<@{r[2]}>' if r[2] else 'Unknown'} | Target: `{r[3] or 'N/A'}`\n{r[4][:180]}"
            for r in rows
        )[:4000]
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _admin_dashboard(interaction: discord.Interaction) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
        return await interaction.response.send_message("â ÙØ­ØªØ§Ø¬ ØµÙØ§Ø­ÙØ© Administrator Ø£Ù Manage Server.", ephemeral=True)
    view = DashboardView(owner_id=interaction.user.id, guild_id=interaction.guild.id)
    embed = await _dashboard_page_embed(interaction.guild, "overview", interaction.client)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def dashboard_cmd(interaction: discord.Interaction):
    await _admin_dashboard(interaction)


async def analytics_cmd(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
        return await interaction.response.send_message("â ÙØ­ØªØ§Ø¬ ØµÙØ§Ø­ÙØ© Administrator Ø£Ù Manage Server.", ephemeral=True)
    await _send_analytics(interaction)


async def incidents_cmd(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
        return await interaction.response.send_message("â ÙØ­ØªØ§Ø¬ ØµÙØ§Ø­ÙØ© Administrator Ø£Ù Manage Server.", ephemeral=True)
    await _send_incidents(interaction)


async def anti_raid_config_cmd(interaction: discord.Interaction, enabled: bool,
                               threshold: app_commands.Range[int, 3, 30],
                               window_seconds: app_commands.Range[int, 10, 300],
                               quarantine_role: discord.Role | None = None):
    guild = interaction.guild
    if not guild or not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
        return await interaction.response.send_message("â ÙØ­ØªØ§Ø¬ ØµÙØ§Ø­ÙØ© Administrator Ø£Ù Manage Server.", ephemeral=True)
    if quarantine_role:
        if quarantine_role.is_default() or quarantine_role.managed or not guild.me or guild.me.top_role <= quarantine_role:
            return await interaction.response.send_message("â Ø±ØªØ¨Ø© Ø§ÙØ¹Ø²Ù ÙØ§Ø²Ù ØªÙÙÙ ÙØ§Ø¨ÙØ© ÙÙØ¥Ø¯Ø§Ø±Ø© ÙØªØ­Øª Ø£Ø¹ÙÙ Ø±ØªØ¨Ø© ÙÙØ¨ÙØª.", ephemeral=True)
        if not guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message("â Ø§ÙØ¨ÙØª ÙØ­ØªØ§Ø¬ Manage Roles.", ephemeral=True)
    await _set_setting(guild.id, "raid_enabled", str(int(enabled)))
    if not enabled:
        await _set_setting(guild.id, "raid_until", "0")
        RAID_UNTIL.pop(guild.id, None)
    await _set_setting(guild.id, "raid_threshold", str(threshold))
    await _set_setting(guild.id, "raid_window", str(window_seconds))
    await _set_setting(guild.id, "raid_role", str(quarantine_role.id if quarantine_role else 0))
    await _incident(guild.id, "anti_raid_config", f"Anti-raid {'enabled' if enabled else 'disabled'}; threshold={threshold}/{window_seconds}s", interaction.user.id, quarantine_role.id if quarantine_role else None)
    await interaction.response.send_message(
        f"â Anti-Raid {'enabled' if enabled else 'disabled'}. Threshold: **{threshold} joins / {window_seconds}s**. "
        f"Quarantine role: {quarantine_role.mention if quarantine_role else 'not configured'}.", ephemeral=True)


async def undo_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await _undo_last(interaction)
    await interaction.followup.send(result, ephemeral=True)


async def health_cmd(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
        return await interaction.response.send_message("â ÙØ­ØªØ§Ø¬ ØµÙØ§Ø­ÙØ© Administrator Ø£Ù Manage Server.", ephemeral=True)
    try:
        async with await _db() as db:
            cur = await db.execute("PRAGMA quick_check")
            result = await cur.fetchone()
        state = str(result[0]) if result else "unknown"
    except Exception as exc:
        state = f"error: {type(exc).__name__}"
    colour = discord.Colour.green() if state == "ok" else discord.Colour.red()
    embed = discord.Embed(title="ð©º Database Health", description=f"SQLite quick_check: **{state}**", colour=colour)
    embed.set_footer(text="Read-only check. No automatic database repair or destructive cleanup is performed.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


class OperationsExtension:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.ticket_sla_task: asyncio.Task | None = None

    async def ticket_sla_watchdog(self) -> None:
        """Send deduplicated Discord-native alerts for tickets open over 24h."""
        while not self.bot.is_closed():
            try:
                if not self.bot.is_ready():
                    await asyncio.sleep(30)
                    continue
                async with await _db() as db:
                    cur = await db.execute(
                        "SELECT guild_id,user_id,channel_id FROM tickets "
                        "WHERE status='open' AND created_at IS NOT NULL "
                        "AND julianday(created_at)<=julianday('now','-1 day')"
                    )
                    overdue = await cur.fetchall()
                for guild_id, user_id, channel_id in overdue:
                    guild = self.bot.get_guild(int(guild_id))
                    channel = guild.get_channel(int(channel_id)) if guild else None
                    if guild is None or not isinstance(channel, discord.TextChannel):
                        continue
                    async with await _db() as db:
                        cur = await db.execute(
                            "SELECT value FROM guild_cfg WHERE guild_id=? AND log_type='all' AND key='audit_channel'",
                            (guild.id,),
                        )
                        setting = await cur.fetchone()
                    audit = guild.get_channel(int(setting[0])) if setting and str(setting[0]).isdigit() else None
                    if not isinstance(audit, discord.TextChannel):
                        continue
                    now = _now()
                    async with await _db() as db:
                        await db.execute("BEGIN IMMEDIATE")
                        await db.execute(
                            "INSERT OR IGNORE INTO ops_ticket_sla_notices(guild_id,channel_id,status) VALUES(?,?,'pending')",
                            (guild.id, int(channel_id)),
                        )
                        cur = await db.execute(
                            "SELECT status FROM tickets WHERE guild_id=? AND channel_id=? AND status='open'",
                            (guild.id, int(channel_id)),
                        )
                        still_open = await cur.fetchone()
                        if not still_open:
                            await db.execute("UPDATE ops_ticket_sla_notices SET status='cancelled',claimed_at=NULL,last_error='Ticket is no longer open' WHERE guild_id=? AND channel_id=?", (guild.id, int(channel_id)))
                            await db.commit()
                            continue
                        cur = await db.execute(
                            "SELECT status,claimed_at FROM ops_ticket_sla_notices WHERE guild_id=? AND channel_id=?",
                            (guild.id, int(channel_id)),
                        )
                        state = await cur.fetchone()
                        if state and state[0] == "sent":
                            await db.commit()
                            continue
                        if state and state[0] == "sending" and state[1]:
                            try:
                                claimed = dt.datetime.fromisoformat(state[1])
                                if claimed.tzinfo is None:
                                    claimed = claimed.replace(tzinfo=UTC)
                                if claimed > now-dt.timedelta(minutes=5):
                                    await db.commit()
                                    continue
                            except ValueError:
                                pass
                        await db.execute(
                            "UPDATE ops_ticket_sla_notices SET status='sending',claimed_at=?,last_error=NULL WHERE guild_id=? AND channel_id=?",
                            (now.isoformat(), guild.id, int(channel_id)),
                        )
                        await db.commit()
                    try:
                        await audit.send(
                            f"â° Ticket SLA overdue: {channel.mention} Â· owner <@{int(user_id)}> Â· open for more than 24 hours.",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException as exc:
                        async with await _db() as db:
                            await db.execute(
                                "UPDATE ops_ticket_sla_notices SET status='pending',claimed_at=NULL,last_error=? WHERE guild_id=? AND channel_id=? AND status='sending'",
                                (type(exc).__name__, guild.id, int(channel_id)),
                            )
                            await db.commit()
                        continue
                    async with await _db() as db:
                        await db.execute(
                            "UPDATE ops_ticket_sla_notices SET status='sent',notified_at=?,claimed_at=NULL,last_error=NULL WHERE guild_id=? AND channel_id=? AND status='sending'",
                            (_now().isoformat(), guild.id, int(channel_id)),
                        )
                        await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"â ï¸ Native ticket SLA worker error: {type(exc).__name__}")
            await asyncio.sleep(900)

    async def shutdown(self) -> None:
        if self.ticket_sla_task and not self.ticket_sla_task.done():
            self.ticket_sla_task.cancel()
            try:
                await self.ticket_sla_task
            except asyncio.CancelledError:
                pass
        self.ticket_sla_task = None

    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        await _daily(message.guild.id, "message_count")
        await _mark_active(message.guild.id, message.author.id)

    async def on_member_join(self, member: discord.Member):
        await _daily(member.guild.id, "join_count")
        await _incident(member.guild.id, "member_join", f"{member} joined", target_id=member.id)
        enabled = await _setting(member.guild.id, "raid_enabled", "0") == "1"
        if not enabled:
            return
        threshold = max(3, min(30, int(await _setting(member.guild.id, "raid_threshold", "8"))))
        window = max(10, min(300, int(await _setting(member.guild.id, "raid_window", "30"))))
        now_dt = _now()
        now = now_dt.timestamp()
        cutoff = (now_dt - dt.timedelta(seconds=window)).isoformat()
        # Persist the rolling join window so a process restart does not erase
        # evidence of an ongoing join burst.
        async with await _db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO ops_join_events(guild_id,user_id,joined_at) VALUES(?,?,?)",
                (member.guild.id, member.id, now_dt.isoformat()),
            )
            await db.execute("DELETE FROM ops_join_events WHERE guild_id=? AND joined_at<?", (member.guild.id, cutoff))
            cur = await db.execute("SELECT joined_at FROM ops_join_events WHERE guild_id=? ORDER BY joined_at", (member.guild.id,))
            persisted = await cur.fetchall()
            await db.commit()
        queue = JOIN_WINDOWS[member.guild.id]
        queue.clear()
        for row in persisted:
            try:
                queue.append(dt.datetime.fromisoformat(str(row[0])).timestamp())
            except (TypeError, ValueError, OSError):
                continue
        while queue and now - queue[0] > window:
            queue.popleft()
        try:
            raid_until = float(await _setting(member.guild.id, "raid_until", "0"))
        except (TypeError, ValueError):
            raid_until = 0.0
        if len(queue) >= threshold:
            raid_until = max(raid_until, now + 600)
            RAID_UNTIL[member.guild.id] = raid_until
            await _set_setting(member.guild.id, "raid_until", str(raid_until))
            await _incident(member.guild.id, "raid_alert", f"Join burst detected: {len(queue)} joins in {window}s")
        elif raid_until > now:
            RAID_UNTIL[member.guild.id] = raid_until
        if raid_until > now:
            role_id = int(await _setting(member.guild.id, "raid_role", "0"))
            role = member.guild.get_role(role_id) if role_id else None
            me = member.guild.me
            if role and me and me.guild_permissions.manage_roles and me.top_role > role:
                try:
                    await member.add_roles(role, reason="Automatic anti-raid quarantine; join burst detected")
                    await _incident(member.guild.id, "raid_quarantine", "New member quarantined during raid window", target_id=member.id, details={"role_id": role.id})
                except discord.HTTPException:
                    await _incident(member.guild.id, "raid_quarantine_failed", "Could not quarantine new member; check bot permissions", target_id=member.id)

    async def on_member_remove(self, member: discord.Member):
        await _daily(member.guild.id, "leave_count")
        actor = await _audit_actor(member.guild, discord.AuditLogAction.kick, member.id)
        if actor is not None:
            await _incident(member.guild.id, "member_kick", f"Member kicked: {member}", actor, member.id)
            await _security_alert(member.guild, "destructive action burst", f"Member kicked: {member}", actor, member.id)
        else:
            await _incident(member.guild.id, "member_leave", f"{member} left", target_id=member.id)

    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        actor = await _audit_actor(guild, discord.AuditLogAction.ban, user.id)
        await _incident(guild.id, "member_ban", f"Member banned: {user}", actor, user.id)
        await _security_alert(guild, "destructive action burst", f"Member banned: {user}", actor, user.id)

    async def on_guild_role_create(self, role: discord.Role):
        actor = await _audit_actor(role.guild, discord.AuditLogAction.role_create, role.id)
        await _incident(role.guild.id, "role_create", f"Role created: {role.name}", actor, role.id, _role_snapshot(role))
        if actor == (self.bot.user.id if self.bot.user else None):
            await _record_undo(role.guild.id, actor, "role_create", role.id, _role_snapshot(role))

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        actor = await _audit_actor(after.guild, discord.AuditLogAction.role_update, after.id)
        snap = _role_snapshot(before)
        await _incident(after.guild.id, "role_update", f"Role updated: {before.name} â {after.name}", actor, after.id, {"before": snap, "after": _role_snapshot(after)})
        gained = [flag for flag in _NUKE_PERMISSION_FLAGS
                  if not bool(getattr(before.permissions, flag, False))
                  and bool(getattr(after.permissions, flag, False))]
        if gained:
            await _incident(after.guild.id, "role_privilege_escalation", f"Sensitive permissions added to role: {after.name}", actor, after.id, {"gained_permissions": gained})
            await _security_alert(after.guild, "privileged role permission escalation", f"Sensitive permissions added to role {after.name}: {', '.join(gained)}", actor, after.id)
        if actor == (self.bot.user.id if self.bot.user else None) and not _undo_record_suppressed(after.guild.id, "role_update", after.id):
            await _record_undo(after.guild.id, actor, "role_update", after.id, {"before": snap, "after": _role_snapshot(after)})

    async def on_guild_role_delete(self, role: discord.Role):
        actor = await _audit_actor(role.guild, discord.AuditLogAction.role_delete, role.id)
        await _incident(role.guild.id, "role_delete", f"Role deleted: {role.name}", actor, role.id, _role_snapshot(role))
        await _security_alert(role.guild, "role/channel deletion burst", f"Role deleted: {role.name}", actor, role.id)

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        actor = await _audit_actor(channel.guild, discord.AuditLogAction.channel_create, channel.id)
        snap = _channel_snapshot(channel)
        await _incident(channel.guild.id, "channel_create", f"Channel created: {channel.name}", actor, channel.id, snap)
        if actor == (self.bot.user.id if self.bot.user else None):
            await _record_undo(channel.guild.id, actor, "channel_create", channel.id, snap)

    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        actor = await _audit_actor(after.guild, discord.AuditLogAction.channel_update, after.id)
        snap = _channel_snapshot(before)
        await _incident(after.guild.id, "channel_update", f"Channel updated: {before.name} â {after.name}", actor, after.id, {"before": snap, "after": _channel_snapshot(after)})
        if actor == (self.bot.user.id if self.bot.user else None) and not _undo_record_suppressed(after.guild.id, "channel_update", after.id):
            await _record_undo(after.guild.id, actor, "channel_update", after.id, {"before": snap, "after": _channel_snapshot(after)})

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        actor = await _audit_actor(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
        await _incident(channel.guild.id, "channel_delete", f"Channel deleted: {channel.name}", actor, channel.id, _channel_snapshot(channel))
        await _security_alert(channel.guild, "role/channel deletion burst", f"Channel deleted: {channel.name}", actor, channel.id)

    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        # Discord emits one event for any webhook change. Only confirmed deletion
        # audit-log entries are considered destructive and counted for Anti-Nuke.
        try:
            async for entry in channel.guild.audit_logs(limit=8, action=discord.AuditLogAction.webhook_delete):
                age = (_now() - entry.created_at.astimezone(UTC)).total_seconds()
                if age > 12:
                    break
                target_id = getattr(entry.target, "id", None)
                actor_id = entry.user.id if entry.user else None
                cutoff = (_now() - dt.timedelta(seconds=12)).isoformat()
                async with await _db() as db:
                    cur = await db.execute("SELECT 1 FROM ops_incidents WHERE guild_id=? AND event_type='webhook_delete' AND target_id=? AND created_at>=? LIMIT 1", (channel.guild.id, target_id, cutoff))
                    if await cur.fetchone():
                        continue
                await _incident(channel.guild.id, "webhook_delete", "Webhook deleted", actor_id, target_id,
                                {"channel_id": channel.id})
                await _security_alert(channel.guild, "destructive action burst", "Webhook deleted", actor_id, target_id)
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            await _incident(channel.guild.id, "webhook_audit_unavailable", "Could not attribute webhook update from audit log", details={"channel_id": channel.id})


async def setup(bot: discord.Client):
    async with await _db() as db:
        await db.executescript(SCHEMA)
        cur = await db.execute("PRAGMA table_info(ops_undo_actions)")
        columns = {str(row[1]) for row in await cur.fetchall()}
        migrations = {
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "claimed_by": "INTEGER",
            "claimed_at": "TEXT",
            "failure": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in columns:
                await db.execute(f"ALTER TABLE ops_undo_actions ADD COLUMN {column} {definition}")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ops_undo_status ON ops_undo_actions(guild_id,status,id)")
        cur = await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dashboard_sla_notices'")
        legacy_sla_table = await cur.fetchone()
        if legacy_sla_table:
            await db.execute(
                "INSERT OR IGNORE INTO ops_ticket_sla_notices(guild_id,channel_id,status,notified_at) "
                "SELECT guild_id,channel_id,'sent',notified_at FROM dashboard_sla_notices"
            )
        await db.commit()
    ext = OperationsExtension(bot)
    bot.operations_extension = ext
    ext.ticket_sla_task = asyncio.create_task(ext.ticket_sla_watchdog(), name="dragon-ticket-sla-watchdog")
    bot.add_listener(ext.on_message, "on_message")
    bot.add_listener(ext.on_member_join, "on_member_join")
    bot.add_listener(ext.on_member_remove, "on_member_remove")
    bot.add_listener(ext.on_member_ban, "on_member_ban")
    bot.add_listener(ext.on_guild_role_create, "on_guild_role_create")
    bot.add_listener(ext.on_guild_role_update, "on_guild_role_update")
    bot.add_listener(ext.on_guild_role_delete, "on_guild_role_delete")
    bot.add_listener(ext.on_guild_channel_create, "on_guild_channel_create")
    bot.add_listener(ext.on_guild_channel_update, "on_guild_channel_update")
    bot.add_listener(ext.on_guild_channel_delete, "on_guild_channel_delete")
    bot.add_listener(ext.on_webhooks_update, "on_webhooks_update")

    bot.tree.add_command(app_commands.Command(name="admin-dashboard", description="Open the server admin dashboard", callback=dashboard_cmd))
    bot.tree.add_command(app_commands.Command(name="server-analytics", description="View server activity analytics", callback=analytics_cmd))
    bot.tree.add_command(app_commands.Command(name="incident-report", description="View recent server incidents", callback=incidents_cmd))
    bot.tree.add_command(app_commands.Command(name="anti-raid-config", description="Configure automatic raid detection", callback=anti_raid_config_cmd))
    bot.tree.add_command(app_commands.Command(name="security-config", description="Configure destructive-action security alerts", callback=security_config_cmd))
    bot.tree.add_command(app_commands.Command(name="anti-nuke-config", description="Configure opt-in destructive-action containment", callback=anti_nuke_config_cmd))
    bot.tree.add_command(app_commands.Command(name="anti-nuke-release", description="Review and release a quarantined moderator", callback=anti_nuke_release_cmd))
    bot.tree.add_command(app_commands.Command(name="undo-last-action", description="Undo the last supported bot action", callback=undo_cmd))
    bot.tree.add_command(app_commands.Command(name="database-health", description="Run a read-only database health check", callback=health_cmd))


async def teardown(bot: discord.Client):
    ext = getattr(bot, "operations_extension", None)
    if ext:
        await ext.shutdown()
        bot.operations_extension = None 