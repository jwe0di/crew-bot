"""ai_chat.py — AI Assistant for DragonSlayerBot (auto language matching)"""
import os
import json
import re
import asyncio
import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

AI_PROVIDER = os.getenv("AI_PROVIDER", "groq").lower()
AI_API_KEY = os.getenv("AI_API_KEY", "")

_PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"),
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    "deepseek": ("https://api.deepseek.com/chat/completions", "deepseek-chat"),
    "together": ("https://api.together.xyz/v1/chat/completions", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
}

LANG_NAMES = {
    "ar": "Arabic (العربية)",
    "hi": "Hindi (हिन्दी)",
    "en": "English",
    "de": "German (Deutsch)",
    "it": "Italian (Italiano)",
    "fr": "French (Français)",
    "es": "Spanish (Español)",
    "ru": "Russian (Русский)",
    "tr": "Turkish (Türkçe)",
    "ur": "Urdu (اردو)",
    "fa": "Persian (فارسی)",
}

def detect_language(text: str) -> str:
    """Detect language from text using unicode blocks."""
    if not text:
        return "en"
    if re.search(r"[\u0600-\u06FF\u0750-\u077F]", text):
        return "ar"
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    if re.search(r"[\u0400-\u04FF]", text):
        return "ru"
    if re.search(r"[\u0590-\u05FF]", text):
        return "he"
    if re.search(r"[\u4E00-\u9FFF]", text):
        return "zh"
    if re.search(r"[\u3040-\u30FF]", text):
        return "ja"
    if re.search(r"[\uAC00-\uD7AF]", text):
        return "ko"
    if re.search(r"[\u0E00-\u0E7F]", text):
        return "th"
    return "en"

def _build_system_prompt(lang: str) -> str:
    lang_name = LANG_NAMES.get(lang, "English")
    return f"""You are the AI assistant inside a Discord gaming server.

🌐 LANGUAGE RULE (CRITICAL):
The user's message is in **{lang_name}**.
You MUST reply in **{lang_name}** — no exceptions.
If the user switches language, reply in their new language.
Never reply in English unless the user used English.

RULES:
- Keep replies short (2-5 lines max unless asked for details).
- Use tools to perform server actions. Never claim success unless a tool returned success.
- Sensitive actions (create/edit/delete roles, channels, mass role assignment) require permission; if the user lacks it, politely refuse.
- You can play simple games (riddles, guessing) when asked.
- If you don't know something, say so honestly.
"""

_T_DEFS = [
    ("get_member_info", "Get member info (points, rank, streak, logs)", {"member_query": "string (required)"}, False),
    ("get_server_info", "Get server stats", {}, False),
    ("list_channels", "List server channels", {}, False),
    ("list_roles", "List server roles with member counts", {}, False),
    ("get_channel_info", "Channel details", {"channel_query": "string (required)"}, False),
    ("create_role", "Create a role", {"name": "string (required)", "color_hex": "string (optional)", "hoist": "boolean (optional)"}, True),
    ("edit_role", "Edit role name/color", {"role_query": "string (required)", "new_name": "string (optional)", "new_color_hex": "string (optional)"}, True),
    ("delete_role", "Delete a role (DESTRUCTIVE)", {"role_query": "string (required)"}, True),
    ("create_channel", "Create a channel", {"name": "string (required)", "channel_type": "string (optional)", "category_query": "string (optional)", "is_private": "boolean (optional)", "allowed_roles": "array (optional)"}, True),
    ("delete_channel", "Delete a channel (DESTRUCTIVE)", {"channel_query": "string (required)"}, True),
    ("give_role", "Give a role to a member", {"member_query": "string (required)", "role_query": "string (required)"}, True),
    ("remove_role", "Remove a role from a member", {"member_query": "string (required)", "role_query": "string (required)"}, True),
    ("bulk_give_role", "Give a role to all members that have another role (DESTRUCTIVE)", {"source_role_query": "string (required)", "target_role_query": "string (required)"}, True),
    ("send_channel_message", "Send a message in a channel", {"channel_query": "string (required)", "message_text": "string (required)"}, True),
]

TOOLS, TOOL_META = [], {}
for _n, _d, _p, _a in _T_DEFS:
    _props, _req = {}, []
    for _k, _v in _p.items():
        if "array" in _v:
            _props[_k] = {"type": "array", "items": {"type": "string"}, "description": _v}
        elif "boolean" in _v:
            _props[_k] = {"type": "boolean", "description": _v}
        else:
            _props[_k] = {"type": "string", "description": _v}
        if "required" in _v:
            _req.append(_k)
    TOOLS.append({"type": "function", "function": {"name": _n, "description": _d, "parameters": {"type": "object", "properties": _props, "required": _req}}})
    TOOL_META[_n] = {"requires_admin": _a}


class AICog(commands.Cog):
    def __init__(self, bot, db_path, is_admin_fn, ladder_roles_fn, get_season_fn):
        self.bot = bot
        self.db_path = db_path
        self.is_admin = is_admin_fn
        self.ladder_roles = ladder_roles_fn
        self.get_current_season = get_season_fn
        self._sessions = {}

    async def _get_setting(self, gid, key, default=None):
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT value FROM ai_settings WHERE guild_id=? AND key=?", (gid, key))
            row = await cur.fetchone()
        return row[0] if row else default

    async def _set_setting(self, gid, key, value):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO ai_settings(guild_id,key,value) VALUES(?,?,?) ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value", (gid, key, str(value)))
            await db.commit()

    async def _can_chat(self, i):
        default = await self._get_setting(i.guild_id, "chat_default", "1")
        async with aiosqlite.connect(self.db_path) as db:
            if default == "1":
                cur = await db.execute("SELECT 1 FROM ai_deny WHERE guild_id=? AND user_id=?", (i.guild_id, i.user.id))
                return not await cur.fetchone()
            cur = await db.execute("SELECT 1 FROM ai_allow WHERE guild_id=? AND user_id=?", (i.guild_id, i.user.id))
            return bool(await cur.fetchone()) or self.is_admin(i.user)

    async def _can_sensitive(self, i):
        if self.is_admin(i.user):
            return True
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT 1 FROM ai_admin_users WHERE guild_id=? AND user_id=?", (i.guild_id, i.user.id))
            return bool(await cur.fetchone())

    async def _call_ai(self, messages):
        if not AI_API_KEY:
            return {"error": "AI_API_KEY missing in Railway Variables."}
        if AI_PROVIDER not in _PROVIDERS:
            return {"error": f"Unknown AI_PROVIDER. Use: {', '.join(_PROVIDERS)}"}
        url, model = _PROVIDERS[AI_PROVIDER]
        headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto", "temperature": 0.6, "max_tokens": 800}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as s:
                async with s.post(url, headers=headers, json=payload) as r:
                    if r.status != 200:
                        return {"error": f"HTTP {r.status}: {(await r.text())[:300]}"}
                    data = await r.json()
                    return {"message": data["choices"][0]["message"]}
        except asyncio.TimeoutError:
            return {"error": "Timeout — try again."}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    def _find_member(self, g, q):
        q = str(q).strip().lstrip("<@!").rstrip(">")
        if q.isdigit():
            return g.get_member(int(q))
        for m in g.members:
            if m.name.lower() == q.lower() or m.display_name.lower() == q.lower():
                return m
        for m in g.members:
            if q.lower() in m.name.lower() or q.lower() in m.display_name.lower():
                return m
        return None

    def _find_role(self, g, q):
        q = str(q).strip().lstrip("<@&").rstrip(">")
        if q.isdigit():
            return g.get_role(int(q))
        for r in g.roles:
            if r.name.lower() == q.lower():
                return r
        for r in g.roles:
            if q.lower() in r.name.lower():
                return r
        return None

    def _find_channel(self, g, q):
        q = str(q).strip().lstrip("<#").rstrip(">")
        if q.isdigit():
            return g.get_channel(int(q))
        for c in g.channels:
            if c.name.lower() == q.lower():
                return c
        for c in g.channels:
            if q.lower() in c.name.lower():
                return c
        return None

    def _color(self, hx):
        if not hx:
            return discord.Color.default()
        try:
            return discord.Color(int(str(hx).lstrip("#"), 16))
        except Exception:
            return discord.Color.default()

    async def _exec_tool(self, i, name, args):
        if TOOL_META.get(name, {}).get("requires_admin") and not await self._can_sensitive(i):
            return "❌ DENIED: You are not authorized for sensitive commands."
        g = i.guild
        try:
            if name == "get_member_info":
                m = self._find_member(g, args.get("member_query", ""))
                if not m: return "❌ Member not found."
                season = await self.get_current_season(g.id)
                async with aiosqlite.connect(self.db_path) as db:
                    cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (g.id, m.id))
                    pr = await cur.fetchone()
                    cur = await db.execute("SELECT current_streak,max_streak FROM streaks WHERE guild_id=? AND user_id=?", (g.id, m.id))
                    sr = await cur.fetchone()
                    cur = await db.execute("SELECT l.type,COUNT(*) FROM point_events e JOIN logs l ON l.id=e.log_id WHERE e.guild_id=? AND e.user_id=? AND e.type='log' AND l.status='accepted' GROUP BY l.type", (g.id, m.id))
                    lr = await cur.fetchall()
                pts = pr[0] if pr else 0
                st = f"{sr[0]}d (max {sr[1]}d)" if sr else "0d"
                total = sum(r[1] for r in lr) if lr else 0
                bd = ", ".join(f"{r[0]}:{r[1]}" for r in lr) or "no logs"
                rank = next((r.name for r in self.ladder_roles(g) if r and r in m.roles), "—")
                return (f"👤 **{m.display_name}** (`{m.id}`)\n"
                        f"• Points: **{pts}** (season {season})\n"
                        f"• Rank: {rank}\n"
                        f"• Streak: {st}\n"
                        f"• Logs: {total} ({bd})\n"
                        f"• Joined: {m.joined_at.strftime('%Y-%m-%d') if m.joined_at else '?'}\n"
                        f"• Top role: {m.top_role.name}")

            if name == "get_server_info":
                return (f"📊 **{g.name}**\n"
                        f"• Members: {g.member_count}\n"
                        f"• Roles: {len(g.roles)}\n"
                        f"• Channels: {len(g.channels)} (text {len(g.text_channels)} / voice {len(g.voice_channels)})\n"
                        f"• Boost L{g.premium_tier} ({g.premium_subscription_count})\n"
                        f"• Owner: <@{g.owner_id}>")

            if name == "list_channels":
                cats, no_cat = {}, []
                for c in g.channels:
                    if isinstance(c, discord.CategoryChannel):
                        cats[c.name] = []
                for c in g.channels:
                    if isinstance(c, discord.CategoryChannel):
                        continue
                    line = f"  • #{c.name}" if isinstance(c, discord.TextChannel) else f"  • 🔊 {c.name}"
                    if c.category and c.category.name in cats:
                        cats[c.category.name].append(line)
                    else:
                        no_cat.append(line)
                parts = [f"**{k}**\n" + "\n".join(v) for k, v in cats.items() if v]
                if no_cat:
                    parts.append("**Uncategorized**\n" + "\n".join(no_cat))
                return "\n\n".join(parts)[:1900] or "No channels."

            if name == "list_roles":
                rs = sorted([r for r in g.roles if not r.is_default()], key=lambda x: -x.position)[:30]
                return "\n".join(f"• **{r.name}** — {len(r.members)} members — {r.color}" for r in rs) or "No roles."

            if name == "get_channel_info":
                c = self._find_channel(g, args.get("channel_query", ""))
                if not c: return "❌ Channel not found."
                return (f"📺 **#{c.name}**\n"
                        f"• Type: {type(c).__name__}\n"
                        f"• Category: {c.category.name if c.category else 'none'}\n"
                        f"• Overwrites: {len(c.overwrites)}")

            if name == "create_role":
                r = await g.create_role(
                    name=args["name"],
                    color=self._color(args.get("color_hex")),
                    hoist=bool(args.get("hoist")),
                    reason=f"AI by {i.user}"
                )
                return f"✅ Created role **{r.mention}**"

            if name == "edit_role":
                r = self._find_role(g, args.get("role_query", ""))
                if not r: return "❌ Role not found."
                kw = {"reason": f"AI by {i.user}"}
                if args.get("new_name"): kw["name"] = args["new_name"]
                if args.get("new_color_hex"): kw["color"] = self._color(args["new_color_hex"])
                await r.edit(**kw)
                return f"✅ Updated role **{r.name}**"

            if name == "delete_role":
                r = self._find_role(g, args.get("role_query", ""))
                if not r: return "❌ Role not found."
                if r.is_default() or r.managed or r.position >= g.me.top_role.position:
                    return "❌ Cannot delete (default/managed/above bot)."
                n = r.name
                await r.delete(reason=f"AI by {i.user}")
                return f"✅ Deleted role **{n}**"

            if name == "create_channel":
                ct = args.get("channel_type", "text")
                cat = None
                if args.get("category_query"):
                    c = self._find_channel(g, args["category_query"])
                    if isinstance(c, discord.CategoryChannel):
                        cat = c
                ow = {}
                if args.get("is_private"):
                    ow[g.default_role] = discord.PermissionOverwrite(read_messages=False)
                    for rq in args.get("allowed_roles", []) or []:
                        rr = self._find_role(g, rq)
                        if rr:
                            ow[rr] = discord.PermissionOverwrite(read_messages=True)
                    ow[g.me] = discord.PermissionOverwrite(read_messages=True)
                kw = {"name": args["name"], "category": cat, "overwrites": ow, "reason": f"AI by {i.user}"}
                ch = await (g.create_voice_channel(**kw) if ct == "voice" else g.create_text_channel(**kw))
                return f"✅ Created {ct} channel {ch.mention}"

            if name == "delete_channel":
                c = self._find_channel(g, args.get("channel_query", ""))
                if not c: return "❌ Channel not found."
                n = c.name
                await c.delete(reason=f"AI by {i.user}")
                return f"✅ Deleted channel **{n}**"

            if name == "give_role":
                m = self._find_member(g, args.get("member_query", ""))
                r = self._find_role(g, args.get("role_query", ""))
                if not m or not r: return "❌ Member or role not found."
                await m.add_roles(r, reason=f"AI by {i.user}")
                return f"✅ Gave **{r.name}** to **{m.display_name}**"

            if name == "remove_role":
                m = self._find_member(g, args.get("member_query", ""))
                r = self._find_role(g, args.get("role_query", ""))
                if not m or not r: return "❌ Member or role not found."
                await m.remove_roles(r, reason=f"AI by {i.user}")
                return f"✅ Removed **{r.name}** from **{m.display_name}**"

            if name == "bulk_give_role":
                src = self._find_role(g, args.get("source_role_query", ""))
                tgt = self._find_role(g, args.get("target_role_query", ""))
                if not src or not tgt: return "❌ Source or target role not found."
                cnt = 0
                for m in src.members:
                    if m.bot: continue
                    try:
                        await m.add_roles(tgt, reason=f"AI bulk by {i.user}")
                        cnt += 1
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass
                return f"✅ Gave **{tgt.name}** to {cnt} members with **{src.name}**"

            if name == "send_channel_message":
                c = self._find_channel(g, args.get("channel_query", ""))
                if not isinstance(c, discord.TextChannel): return "❌ Text channel not found."
                await c.send(args.get("message_text", ""))
                return f"✅ Sent message in {c.mention}"

            return f"✅ {name} executed."
        except discord.Forbidden:
            return "❌ Forbidden: missing Discord permission."
        except Exception as e:
            return f"❌ {type(e).__name__}: {str(e)[:200]}"

    async def _run_ai(self, i, text):
        key = (i.guild_id, i.user.id)
        lang = detect_language(text)
        # Build session: if new or language changed, reset system prompt
        cached = self._sessions.get(key)
        if not cached or cached.get("lang") != lang:
            cached = {"lang": lang, "history": [{"role": "system", "content": _build_system_prompt(lang)}]}
        hist = cached["history"]
        hist.append({"role": "user", "content": text})
        if len(hist) > 20:
            hist = [hist[0]] + hist[-18:]

        for _ in range(4):
            res = await self._call_ai(hist)
            if "error" in res:
                cached["history"] = hist
                self._sessions[key] = cached
                return f"❌ **AI Error:**\n```\n{res['error']}\n```"

            msg = res["message"]
            tcs = msg.get("tool_calls") or []
            if not tcs:
                hist.append({"role": "assistant", "content": msg.get("content", "")})
                cached["history"] = hist
                self._sessions[key] = cached
                return msg.get("content", "") or "(no reply)"

            hist.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": tcs})
            for tc in tcs:
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                out = await self._exec_tool(i, fn["name"], args)
                hist.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": out[:1900]})

        cached["history"] = hist
        self._sessions[key] = cached
        return "⚠️ Tool loop stopped."

    # -------------------- Commands --------------------
    @app_commands.command(name="ai", description="Chat with AI in your language / كلم المساعد الذكي بلغتك")
    @app_commands.describe(message="Your message in any language / رسالتك بأي لغة")
    async def ai_cmd(self, i: discord.Interaction, message: str):
        if not i.guild:
            return await i.response.send_message("❌ Server only.", ephemeral=True)
        if not await self._can_chat(i):
            return await i.response.send_message("❌ AI is disabled for you here.", ephemeral=True)
        if not AI_API_KEY:
            return await i.response.send_message("❌ AI not configured. Admin must set `AI_API_KEY`.", ephemeral=True)
        await i.response.defer()
        reply = await self._run_ai(i, message)
        for j in range(0, min(len(reply), 5700), 1900):
            await i.followup.send(reply[j:j+1900])

    @app_commands.command(name="ai-reset", description="Clear your AI memory / امسح ذاكرة المحادثة")
    async def ai_reset(self, i: discord.Interaction):
        self._sessions.pop((i.guild_id, i.user.id), None)
        await i.response.send_message("✅ Memory cleared / تم مسح الذاكرة.", ephemeral=True)

    @app_commands.command(name="set-ai-default", description="Default AI access / الوضع الافتراضي للـ AI")
    @app_commands.choices(state=[
        app_commands.Choice(name="On / مفعّل للكل", value="1"),
        app_commands.Choice(name="Off / موقوف (للمسموحين فقط)", value="0"),
    ])
    async def set_ai_default(self, i: discord.Interaction, state: app_commands.Choice[str]):
        if not self.is_admin(i.user):
            return await i.response.send_message("❌ Admin only.", ephemeral=True)
        await self._set_setting(i.guild_id, "chat_default", state.value)
        await i.response.send_message(f"✅ Default = **{state.name}**", ephemeral=True)

    @app_commands.command(name="set-ai-user", description="Allow / deny a member / سماح أو منع عضو")
    @app_commands.choices(action=[
        app_commands.Choice(name="Allow / سماح", value="allow"),
        app_commands.Choice(name="Deny / منع", value="deny"),
        app_commands.Choice(name="Reset / إلغاء", value="reset"),
    ])
    async def set_ai_user(self, i: discord.Interaction, member: discord.Member, action: app_commands.Choice[str]):
        if not self.is_admin(i.user):
            return await i.response.send_message("❌ Admin only.", ephemeral=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM ai_allow WHERE guild_id=? AND user_id=?", (i.guild_id, member.id))
            await db.execute("DELETE FROM ai_deny WHERE guild_id=? AND user_id=?", (i.guild_id, member.id))
            if action.value == "allow":
                await db.execute("INSERT INTO ai_allow VALUES(?,?)", (i.guild_id, member.id))
            elif action.value == "deny":
                await db.execute("INSERT INTO ai_deny VALUES(?,?)", (i.guild_id, member.id))
            await db.commit()
        await i.response.send_message(f"✅ {action.name} → {member.mention}", ephemeral=True)

    @app_commands.command(name="set-ai-admin", description="Allow / deny sensitive AI commands / السماح بالأوامر الخطيرة")
    @app_commands.choices(action=[
        app_commands.Choice(name="Grant / منح", value="grant"),
        app_commands.Choice(name="Revoke / سحب", value="revoke"),
    ])
    async def set_ai_admin(self, i: discord.Interaction, member: discord.Member, action: app_commands.Choice[str]):
        if not self.is_admin(i.user):
            return await i.response.send_message("❌ Admin only.", ephemeral=True)
        async with aiosqlite.connect(self.db_path) as db:
            if action.value == "grant":
                await db.execute("INSERT OR IGNORE INTO ai_admin_users VALUES(?,?)", (i.guild_id, member.id))
            else:
                await db.execute("DELETE FROM ai_admin_users WHERE guild_id=? AND user_id=?", (i.guild_id, member.id))
            await db.commit()
        await i.response.send_message(f"✅ {action.name} → {member.mention}", ephemeral=True)

    @app_commands.command(name="ai-help", description="What can the AI do? / إمكانيات المساعد")
    async def ai_help(self, i: discord.Interaction):
        await i.response.send_message(
            "🤖 **AI Assistant**\n\n"
            "`/ai message:...` — كلم المساعد (يرد بنفس لغتك)\n"
            "`/ai-reset` — امسح المحادثة\n\n"
            "**أمثلة:**\n"
            "• `/ai مين فلان؟`\n"
            "• `/ai اعمل رول Test بلون أحمر`\n"
            "• `/ai اعمل قناة خاصة للأدمن بس`\n"
            "• `/ai قولي إحصائيات السيرفر`\n"
            "• `/ai العب معايا لغز`\n\n"
            "🌐 **الرد بنفس اللغة:** لو كتبت عربي → عربي، Hindi → Hindi، English → English.",
            ephemeral=True
        )


async def init_ai_tables(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS ai_settings(guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id,key))")
        await db.execute("CREATE TABLE IF NOT EXISTS ai_allow(guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id,user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS ai_deny(guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id,user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS ai_admin_users(guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id,user_id))")
        await db.commit()