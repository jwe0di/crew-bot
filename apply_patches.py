#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_patches.py — يطبّق كل تعديلات bot.py تلقائياً
الاستخدام:
    python apply_patches.py            # تطبيق فعلي
    python apply_patches.py --dry-run  # معاينة فقط (لا تعدل)
"""
import os
import sys
import shutil
import py_compile
from datetime import datetime

BOT_FILE = "bot.py"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")

PATCHES = [
    # ============================================================
    # 1) إصلاح leaderboard — إضافة "This season" + "All time" عبر كل المواسم
    # ============================================================
    ("1. leaderboard — description",
     '@bot.tree.command(name="leaderboard", description="Top members (All time or Weekly) | المتصدرين")',
     '@bot.tree.command(name="leaderboard", description="Top members (All time / Season / Week) | المتصدرين")'),

    ("2. leaderboard — PERIOD_CHOICES (add season)",
     'PERIOD_CHOICES = [app_commands.Choice(name="All time", value="all"),\n                  app_commands.Choice(name="This week", value="week")]',
     'PERIOD_CHOICES = [\n    app_commands.Choice(name="All time", value="all"),\n    app_commands.Choice(name="This season", value="season"),\n    app_commands.Choice(name="This week", value="week"),\n]'),

    ("3. leaderboard — query branches (all/season/week)",
     '''        if p_val == "week":
            cur = await db.execute(
                "SELECT user_id, SUM(points) AS total FROM point_events WHERE guild_id=? AND season_id=? "
                "AND created_at >= ? GROUP BY user_id HAVING total>0 ORDER BY total DESC LIMIT 10",
                (i.guild.id, season, (now_utc() - timedelta(days=7)).isoformat()))
        else:
            cur = await db.execute(
                "SELECT user_id, points FROM members WHERE guild_id=? AND points>0 ORDER BY points DESC LIMIT 10",
                (i.guild.id,))
        rows = await cur.fetchall()''',
     '''        if p_val == "week":
            cur = await db.execute(
                "SELECT user_id, SUM(points) AS total FROM point_events WHERE guild_id=? AND season_id=? "
                "AND created_at >= ? GROUP BY user_id HAVING total>0 ORDER BY total DESC LIMIT 10",
                (i.guild.id, season, (now_utc() - timedelta(days=7)).isoformat()))
        elif p_val == "season":
            cur = await db.execute(
                "SELECT user_id, points FROM members WHERE guild_id=? AND points>0 ORDER BY points DESC LIMIT 10",
                (i.guild.id,))
        else:  # all-time = sum every point_event across every season
            cur = await db.execute(
                "SELECT user_id, SUM(points) AS total FROM point_events WHERE guild_id=? "
                "GROUP BY user_id HAVING total>0 ORDER BY total DESC LIMIT 10", (i.guild.id,))
        rows = await cur.fetchall()'''),

    ("4. leaderboard — title line",
     '    title = tr("lb_week", lang) if p_val == "week" else tr("lb_season", lang)',
     '    titles = {"week": tr("lb_week", lang), "season": tr("lb_season", lang), "all": tr("lb_all", lang)}\n    title = titles.get(p_val, tr("lb_all", lang))'),

    ("5. TEXTS — add lb_all",
     '''    "lb_season": {
        "ar": "🏆 متصدري الموسم",
        "en": "🏆 Season Leaderboard",
        "de": "🏆 Saison-Bestenliste",
        "it": "🏆 Classifica Stagionale",
        "hi": "🏆 सीज़न लीडरबोर्ड",
    },''',
     '''    "lb_season": {
        "ar": "🏆 متصدري الموسم",
        "en": "🏆 Season Leaderboard",
        "de": "🏆 Saison-Bestenliste",
        "it": "🏆 Classifica Stagionale",
        "hi": "🏆 सीज़न लीडरबोर्ड",
    },
    "lb_all": {
        "ar": "📈 متصدري كل الأوقات",
        "en": "📈 All-Time Leaderboard",
        "de": "📈 Allzeit-Bestenliste",
        "it": "📈 Classifica di sempre",
        "hi": "📈 सर्वकालिक लीडरबोर्ड",
    },'''),

    # ============================================================
    # 6) ladder_roles — word-boundary matching (يمنع خلط Lvl.20 مع Lvl.200)
    # ============================================================
    ("6. ladder_roles — regex word-boundary",
     'def ladder_roles(guild):\n    return [discord.utils.find(lambda r, n=n: n.lower() in r.name.lower(), guild.roles) for n, _ in LADDER]',
     '''def ladder_roles(guild):
    """Return one role per LADDER entry, using word-boundary matching so 'Lvl. 20'
    never accidentally matches 'Lvl. 200' or 'Lvl. 20 Test'."""
    result = []
    for name, _ in LADDER:
        pattern = re.compile(rf"\\b{re.escape(name)}\\b", re.IGNORECASE)
        result.append(next((r for r in guild.roles if pattern.search(r.name)), None))
    return result'''),

    # ============================================================
    # 7) check_and_notify_auto_update — fallback to log_channel
    # ============================================================
    ("7. auto-update fallback to log_channel",
     '''    ch_id = await cfg_get(guild.id, "all", "update_channel")
    ch = guild.get_channel(ch_id) if ch_id else None
    if not ch:
        return
    try:
        await post_update_notice(guild, ch)''',
     '''    ch_id = (await cfg_get(guild.id, "all", "update_channel")
             or await cfg_get(guild.id, "all", "log_channel"))
    ch = guild.get_channel(ch_id) if ch_id else None
    if not ch:
        return
    try:
        await post_update_notice(guild, ch)'''),

    # ============================================================
    # 8) run_backup — per-guild scheduling
    # ============================================================
    ("8. run_backup — per-guild scheduling",
     '''async def run_backup():
    if not owner_id():
        return
    hours = [h for h in [await rule(g.id, "backup_hours") for g in bot.guilds] if h > 0]
    if not hours:
        return
    cycle = min(hours) * 3600
    last = await raw_setting(0, "state:last_backup")
    now = now_utc()
    if last:
        try:
            if (now - dt.datetime.fromisoformat(last)).total_seconds() < cycle:
                return
        except ValueError:
            pass
    await set_setting(0, "state:last_backup", now.isoformat())
    path = os.path.join(tempfile.gettempdir(), f"crewbot-auto-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.db")
    try:
        await asyncio.to_thread(_backup_file, path)
        payload = await asyncio.to_thread(_read_bytes, path)
        await dm_owner("🗄️ **Scheduled backup** (full database):",
                       file=discord.File(io.BytesIO(payload), filename=os.path.basename(path)))
    finally:
        if os.path.exists(path):
            os.unlink(path)''',
     '''async def run_backup():
    if not owner_id():
        return
    now = now_utc()
    due = []
    for guild in bot.guilds:
        try:
            hours = await rule(guild.id, "backup_hours")
            if hours <= 0:
                continue
            cycle = hours * 3600
            last = await raw_setting(guild.id, "state:last_backup")
            if last:
                try:
                    if (now - dt.datetime.fromisoformat(last)).total_seconds() < cycle:
                        continue
                except ValueError:
                    pass
            due.append(guild)
        except Exception as e:
            print(f"⚠️ Auto backup check error in guild {guild.id}: {e}")
    if not due:
        return
    for guild in due:
        try:
            await set_setting(guild.id, "state:last_backup", now.isoformat())
        except Exception:
            pass
    path = os.path.join(tempfile.gettempdir(),
                        f"crewbot-auto-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.db")
    try:
        await asyncio.to_thread(_backup_file, path)
        payload = await asyncio.to_thread(_read_bytes, path)
        names = ", ".join(f"**{g.name}**" for g in due[:5])
        extra = f" (+{len(due) - 5} more)" if len(due) > 5 else ""
        await dm_owner(f"🗄️ **Scheduled backup** — due for: {names}{extra}",
                       file=discord.File(io.BytesIO(payload), filename=os.path.basename(path)))
    finally:
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass'''),

    # ============================================================
    # 9) verify_points — confirm when >5 mismatches
    # ============================================================
    ("9a. verify_points — add confirm param",
     '@app_commands.describe(fix="Overwrite balances with the ledger totals")\nasync def verify_points(i: discord.Interaction, fix: bool = False):',
     '@app_commands.describe(fix="Overwrite balances with the ledger totals",\n                       confirm="Type FIX when repairing more than 5 balances")\nasync def verify_points(i: discord.Interaction, fix: bool = False, confirm: Optional[str] = None):'),

    ("9b. verify_points — safety check",
     '''    if fix:
        async with aiosqlite.connect(DB) as db:
            for uid, _, led in bad:
                await db.execute(
                    "INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET points=excluded.points", (i.guild.id, uid, led))
            await db.commit()
        await audit(f"🛠️ {i.user.mention} repaired {len(bad)} balances from the ledger", i.guild)''',
     '''    if fix:
        if len(bad) > 5 and confirm != "FIX":
            return await i.followup.send(
                f"⚠️ Found **{len(bad)}** mismatches. To repair more than 5 at once, add `confirm:FIX`.",
                ephemeral=True)
        async with aiosqlite.connect(DB) as db:
            for uid, _, led in bad:
                await db.execute(
                    "INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET points=excluded.points", (i.guild.id, uid, led))
            await db.commit()
        await audit(f"🛠️ {i.user.mention} repaired {len(bad)} balances from the ledger", i.guild)'''),

    # ============================================================
    # 10) pending — pagination (PendingPaginator class + new body)
    # ============================================================
    ("10a. pending — add PendingPaginator class",
     '@bot.tree.command(name="pending", description="List logs waiting for review | اللوجات المنتظرة")',
     '''class PendingPaginator(discord.ui.View):
    def __init__(self, items, guild_id, owner_user_id):
        super().__init__(timeout=180)
        self.items = items
        self.guild_id = guild_id
        self.owner_user_id = owner_user_id
        self.page = 0
        self.per_page = 15

    def max_pages(self):
        return max(1, (len(self.items) - 1) // self.per_page + 1)

    def get_page_text(self):
        chunk = self.items[self.page * self.per_page:(self.page + 1) * self.per_page]
        lines = []
        for lid, t, author, created, ch_id, msg_id in chunk:
            link = f" https://discord.com/channels/{self.guild_id}/{ch_id}/{msg_id}" if ch_id and msg_id else ""
            lines.append(f"**#{lid}** {LOG_TYPES.get(t, {}).get('label', t)} — <@{author}> — {str(created)[:16]}{link}")
        header = (f"⏳ **Pending logs** — {len(self.items)} total — "
                  f"page {self.page + 1}/{self.max_pages()}\\n\\n")
        return header + "\\n".join(lines)

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


@bot.tree.command(name="pending", description="List logs waiting for review | اللوجات المنتظرة")'''),

    ("10b. pending — paginated body",
     '''    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id, type, author_id, created_at, review_channel, review_msg FROM logs "
            "WHERE status='pending' AND guild_id=? ORDER BY id LIMIT 20", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message(tr("no_pending", lang), ephemeral=True)
    lines = []
    for lid, t, author, created, ch_id, msg_id in rows:
        link = f" https://discord.com/channels/{i.guild.id}/{ch_id}/{msg_id}" if ch_id and msg_id else ""
        lines.append(f"**#{lid}** {LOG_TYPES.get(t, {}).get('label', t)} — <@{author}> — {str(created)[:16]}{link}")
    await i.response.send_message("\\n".join(lines)[:1990], ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())''',
     '''    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id, type, author_id, created_at, review_channel, review_msg FROM logs "
            "WHERE status='pending' AND guild_id=? ORDER BY id DESC LIMIT 200", (i.guild.id,))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message(tr("no_pending", lang), ephemeral=True)
    if len(rows) <= 15:
        lines = []
        for lid, t, author, created, ch_id, msg_id in rows:
            link = f" https://discord.com/channels/{i.guild.id}/{ch_id}/{msg_id}" if ch_id and msg_id else ""
            lines.append(f"**#{lid}** {LOG_TYPES.get(t, {}).get('label', t)} — <@{author}> — {str(created)[:16]}{link}")
        await i.response.send_message("\\n".join(lines)[:1990], ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())
    else:
        view = PendingPaginator(rows, i.guild.id, i.user.id)
        await i.response.send_message(view.get_page_text()[:1990], view=view, ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())'''),

    # ============================================================
    # 11) create-new-crew — voice_captains
    # ============================================================
    ("11a. create-new-crew — describe line",
     '    captains="Captain mentions/IDs",',
     '    captains="Captain mentions/IDs",\n    voice_captains="Voice captains mentions/IDs",'),

    ("11b. create-new-crew — signature",
     '        captains: Optional[app_commands.Range[str, 1, 1000]] = None,\n        team_z_members: Optional[app_commands.Range[str, 1, 1000]] = None,',
     '        captains: Optional[app_commands.Range[str, 1, 1000]] = None,\n        voice_captains: Optional[app_commands.Range[str, 1, 1000]] = None,\n        team_z_members: Optional[app_commands.Range[str, 1, 1000]] = None,'),

    ("11c. create-new-crew — raw_groups",
     '''    raw_groups = (captains, team_z_members, team_y_members, team_x_members, team_s_members, team_a_members,
                  team_z_commanders, team_y_commanders, team_x_commanders, team_s_commanders, team_a_commanders)''',
     '''    raw_groups = (captains, voice_captains, team_z_members, team_y_members, team_x_members,
                  team_s_members, team_a_members,
                  team_z_commanders, team_y_commanders, team_x_commanders,
                  team_s_commanders, team_a_commanders)'''),

    ("11d. create-new-crew — INSERT (voice_captains_data)",
     '''            await db.execute(
                "INSERT INTO crews(guild_id, crew_name, owner_id, captains_data, team_z_data, team_y_data, "
                "team_x_data, team_s_data, team_a_data, team_z_commanders_data, team_y_commanders_data, "
                "team_x_commanders_data, team_s_commanders_data, team_a_commanders_data, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (i.guild.id, crew_name, owner_member.id, *[json.dumps(x) for x in lists], now_utc().isoformat()))''',
     '''            await db.execute(
                "INSERT INTO crews(guild_id, crew_name, owner_id, captains_data, voice_captains_data, "
                "team_z_data, team_y_data, team_x_data, team_s_data, team_a_data, "
                "team_z_commanders_data, team_y_commanders_data, team_x_commanders_data, "
                "team_s_commanders_data, team_a_commanders_data, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (i.guild.id, crew_name, owner_member.id, *[json.dumps(x) for x in lists], now_utc().isoformat()))'''),

    # ============================================================
    # 12) CrewSelect.callback — عرض voice_captains
    # ============================================================
    ("12a. CrewSelect — SQL query",
     '''            cur = await db.execute(
                "SELECT owner_id, captains_data, team_z_data, team_y_data, team_x_data, team_s_data, team_a_data, "
                "created_at, team_z_commanders_data, team_y_commanders_data, team_x_commanders_data, "
                "team_s_commanders_data, team_a_commanders_data FROM crews WHERE guild_id=? AND crew_name=?",
                (i.guild.id, name))''',
     '''            cur = await db.execute(
                "SELECT owner_id, captains_data, voice_captains_data, "
                "team_z_data, team_y_data, team_x_data, team_s_data, team_a_data, "
                "created_at, team_z_commanders_data, team_y_commanders_data, "
                "team_x_commanders_data, team_s_commanders_data, team_a_commanders_data "
                "FROM crews WHERE guild_id=? AND crew_name=?", (i.guild.id, name))'''),

    ("12b. CrewSelect — row unpacking",
     '        owner, cap, *rest = row\n        teams, created_at, commanders = rest[:5], rest[5], rest[6:11]',
     '        owner, cap, vcap, tz, ty, tx, ts, ta, created_at, *commanders = row\n        teams = [tz, ty, tx, ts, ta]'),

    ("12c. CrewSelect — display voice_captains",
     '        e.add_field(name="⚔️ Captains", value=_mentions_or(_id_list(cap), none), inline=True)',
     '        e.add_field(name="⚔️ Captains", value=_mentions_or(_id_list(cap), none), inline=True)\n        e.add_field(name="🎙️ Voice Captains", value=_mentions_or(_id_list(vcap), none), inline=True)'),

    # ============================================================
    # 13) _mentions_or — 900→950 (حد Discord للـ embed field)
    # ============================================================
    ("13. _mentions_or — 900→950",
     '        if len(candidate) > 900:',
     '        if len(candidate) > 950:'),

    # ============================================================
    # 14) help_cmd — رسالة واحدة بـ embeds متعددة
    # ============================================================
    ("14. help_cmd — single message",
     '''    for e in get_help_embeds(lang):
        await i.followup.send(embed=e, ephemeral=True)''',
     '''    embeds = get_help_embeds(lang)
    if len(embeds) <= 10:
        await i.followup.send(embeds=embeds, ephemeral=True)
    else:
        for e in embeds[:10]:
            await i.followup.send(embed=e, ephemeral=True)'''),

    # ============================================================
    # 15) rates — استعلام مجمّع بدل N+1
    # ============================================================
    ("15. rates — consolidated query",
     '''    lines = [f"{flag} ⚙️ **Rates ({BOT_VERSION})**\\n\\n**Log points:**"]
    for k, label in POINT_LABELS.items():
        lines.append(f"- {label}: **{await points_for_key(i.guild.id, k)}** pts")''',
     '''    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT key, value FROM settings WHERE guild_id=? AND key LIKE 'points:%'", (i.guild.id,))
        stored_points = {}
        for key, value in await cur.fetchall():
            try:
                stored_points[key[len("points:"):]] = int(value)
            except (TypeError, ValueError):
                pass
    lines = [f"{flag} ⚙️ **Rates ({BOT_VERSION})**\\n\\n**Log points:**"]
    for k, label in POINT_LABELS.items():
        pts = stored_points.get(k, DEFAULT_POINTS.get(k, 0))
        lines.append(f"- {label}: **{pts}** pts")'''),

    # ============================================================
    # 16) restore_settings_json — version check
    # ============================================================
    ("16. restore_settings_json — version check",
     '''    if not isinstance(data, dict):
        raise ValueError("Unexpected file structure.")
    valid_types = set(LOG_TYPES) | {"all"}''',
     '''    if not isinstance(data, dict):
        raise ValueError("Unexpected file structure.")
    file_version = data.get("version")
    if file_version and file_version != BOT_VERSION:
        print(f"⚠️ Restoring settings from version {file_version} into {BOT_VERSION} — schema may differ.")
    valid_types = set(LOG_TYPES) | {"all"}'''),

    # ============================================================
    # 17) on_guild_join — رسالة ترحيب
    # ============================================================
    ("17. on_guild_join — welcome message",
     '''    async def on_guild_join(self, guild):
        await self.sync_guild(guild)''',
     '''    async def on_guild_join(self, guild):
        await self.sync_guild(guild)
        try:
            target = guild.system_channel or next(
                (c for c in guild.text_channels
                 if c.permissions_for(guild.me).send_messages), None)
            if target:
                await target.send(
                    f"👋 Thanks for adding **DragonSlayerBot** to **{guild.name}**!\\n"
                    f"Use `/help` for a full manual in 5 languages. "
                    f"Admins: run `/setchannel` to configure review & log channels.")
        except (discord.HTTPException, AttributeError):
            pass'''),

    # ============================================================
    # 18) _ensure_members helper + use in /inactive and /season-reset
    # ============================================================
    ("18a. _ensure_members helper",
     'async def audit(text, guild):',
     '''async def _ensure_members(guild):
    """Populate the member cache. Uses fetch_members (discord.py 2.x) with a safe fallback."""
    if getattr(guild, "chunked", False):
        return
    try:
        async for _ in guild.fetch_members(limit=None):
            pass
    except (discord.HTTPException, AttributeError, TypeError):
        pass


async def audit(text, guild):'''),

    ("18b. /inactive — use _ensure_members",
     '''    await i.response.defer(ephemeral=True)
    if not i.guild.chunked:
        await i.guild.chunk()
    cutoff = (now_utc() - timedelta(days=days)).isoformat()''',
     '''    await i.response.defer(ephemeral=True)
    await _ensure_members(i.guild)
    cutoff = (now_utc() - timedelta(days=days)).isoformat()'''),

    ("18c. /season-reset — use _ensure_members",
     '''    stripped = 0
    if reset_ranks:
        if not i.guild.chunked:
            await i.guild.chunk()
        lroles = [r for r in ladder_roles(i.guild) if r]''',
     '''    stripped = 0
    if reset_ranks:
        await _ensure_members(i.guild)
        lroles = [r for r in ladder_roles(i.guild) if r]'''),
]


def find_and_replace(content, old, new):
    count = content.count(old)
    if count == 1:
        return content.replace(old, new, 1), "replaced"
    if count == 0:
        if new and new in content:
            return content, "already"
        return content, "missing"
    return content, f"ambiguous({count})"


def main():
    dry_run = "--dry-run" in sys.argv

    if not os.path.exists(BOT_FILE):
        print(f"❌ لم أجد {BOT_FILE} في المجلد الحالي: {os.getcwd()}")
        sys.exit(1)

    with open(BOT_FILE, "r", encoding="utf-8") as f:
        original = f.read()

    backup_path = None
    if not dry_run:
        backup_path = f"{BOT_FILE}.backup-{STAMP}"
        shutil.copy2(BOT_FILE, backup_path)
        print(f"💾 نسخة احتياطية: {backup_path}\n")

    content = original
    results = []
    for name, old, new in PATCHES:
        content, status = find_and_replace(content, old, new)
        results.append((name, status))
        symbol = {"replaced": "✅", "already": "⏭️ ", "missing": "❌"}.get(status, "⚠️ ")
        print(f"{symbol} {name} — {status}")

    if dry_run:
        print("\n🔍 وضع المعاينة فقط — لم يتم تعديل الملف")
        return

    if content != original:
        with open(BOT_FILE, "w", encoding="utf-8") as f:
            f.write(content)

        try:
            py_compile.compile(BOT_FILE, doraise=True)
            print(f"\n✅ تم حفظ {BOT_FILE} — الصيغة سليمة (syntax OK)")
        except py_compile.PyCompileError as e:
            print("\n❌ خطأ في الصيغة بعد التعديل! استرجاع النسخة الاحتياطية...")
            shutil.copy2(backup_path, BOT_FILE)
            print(f"✅ تم الاسترجاع من {backup_path}")
            print(f"تفاصيل الخطأ:\n{e}")
            sys.exit(1)
    else:
        print("\n⏭️  لا تغييرات (كل التعديلات مطبقة مسبقاً أو لم تُطابق)")

    replaced = sum(1 for _, s in results if s == "replaced")
    already = sum(1 for _, s in results if s == "already")
    missing = sum(1 for _, s in results if s == "missing")
    ambiguous = sum(1 for _, s in results if s.startswith("ambiguous"))
    print(f"\n📊 ملخص: {replaced} مطبق، {already} مطبق مسبقاً، {missing} غير موجود، {ambiguous} غامض")

    if missing or ambiguous:
        print("\n⚠️ بعض التعديلات لم تُطبق. الملف لا يزال يعمل لكن التعديلات ناقصة.")
        print("انسخ التعديلات الفاشلة وأرسلها لي لإصلاحها.")


if __name__ == "__main__":
    main()