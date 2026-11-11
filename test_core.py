"""
اختبارات تلقائية للوظائف الأساسية.
تشغيل:  python -m unittest discover -s tests -v      (أو: pytest -q)
مش محتاجة اتصال بديسكورد: بتستخدم قاعدة بيانات مؤقتة وكائنات تقليد (mocks).
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stubs  # noqa: E402

stubs.install()

_tmp_boot = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp_boot, "boot.db")
os.environ["JSON_CONFIG_PATH"] = os.path.join(_tmp_boot, "cfg.json")
os.environ["OWNER_ID"] = "999"

import aiosqlite  # noqa: E402
import discord  # noqa: E402
from discord import app_commands  # noqa: E402

import bot  # noqa: E402

MB = 1024 * 1024


def choice(value, name=None):
    return app_commands.Choice(name=name or value, value=value)


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    GID = 1

    async def asyncSetUp(self):
        d = tempfile.mkdtemp()
        bot.DB = os.path.join(d, "t.db")
        bot.JSON_CONFIG_PATH = os.path.join(d, "cfg.json")
        await bot.init_db()
        self.members = {}
        self.channels = {}
        self.lvl_roles = [self.role(n, 100 + k) for k, (n, _) in enumerate(bot.LADDER)]
        self.guild = self.make_guild(self.GID)
        self.review_ch = self.channel(600)
        self.log_ch = self.channel(700)
        await bot.cfg_set(self.GID, "all", "review_channel", 600)
        await bot.cfg_set(self.GID, "all", "log_channel", 700)

    # ---------- builders
    def role(self, name, rid):
        r = MagicMock(spec=discord.Role)
        r.name, r.id, r.mention = name, rid, f"<@&{rid}>"
        return r

    def channel(self, cid):
        c = MagicMock(spec=discord.TextChannel)
        c.id, c.mention = cid, f"<#{cid}>"
        self._msg_seq = getattr(self, "_msg_seq", 5000)

        async def send(*a, **k):
            self._msg_seq += 1
            return SimpleNamespace(id=self._msg_seq)

        c.send = AsyncMock(side_effect=send)
        c.fetch_message = AsyncMock()
        self.channels[cid] = c
        return c

    def make_guild(self, gid):
        g = MagicMock(spec=discord.Guild)
        g.id, g.name, g.chunked, g.filesize_limit = gid, "Guild", True, 25 * MB
        g.roles = list(self.lvl_roles)
        g.get_channel = lambda cid: self.channels.get(cid)
        g.get_member = lambda uid: self.members.get(uid)

        async def fetch_member(uid):
            if uid in self.members:
                return self.members[uid]
            raise discord.NotFound("no member")

        g.fetch_member = fetch_member
        g.me = SimpleNamespace(top_role=SimpleNamespace(position=99))
        g.members = []
        return g

    def member(self, uid, admin=False, roles=()):
        m = MagicMock(spec=discord.Member)
        m.id, m.bot, m.mention, m.display_name = uid, False, f"<@{uid}>", f"user{uid}"
        m.roles = list(roles)
        m.guild_permissions = SimpleNamespace(administrator=admin)
        m.guild = self.guild
        m.add_roles, m.remove_roles, m.send = AsyncMock(), AsyncMock(), AsyncMock()
        m.display_avatar = SimpleNamespace(url="http://x")
        self.members[uid] = m
        return m

    def interaction(self, user, message=None, guild=None):
        i = MagicMock(spec=discord.Interaction)
        i.user, i.guild, i.message = user, guild or self.guild, message
        i.response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(),
                                     edit_message=AsyncMock(), is_done=lambda: False)
        i.followup = SimpleNamespace(send=AsyncMock())
        return i

    def attachment(self, name="proof.png", data=b"img-1", size=None):
        a = MagicMock(spec=discord.Attachment)
        a.filename, a.size, a.url = name, size if size is not None else len(data), f"http://cdn/{name}"
        a.read = AsyncMock(return_value=data)
        a.content_type = "image/png"
        return a

    def message(self, mid, content, attachments=()):
        m = MagicMock(spec=discord.Message)
        m.id, m.content, m.attachments = mid, content, list(attachments)
        m.edit, m.reply = AsyncMock(), AsyncMock()
        return m

    # ---------- db helpers
    async def insert_log(self, type_="1v1", author=10, result="win", data=None, status="pending", season="main",
                         review_msg=555, review_channel=600, parent=None, gid=None, created=None):
        data = data if data is not None else {"my_fighter": "a", "opponent": "b", "score": "5-3"}
        created = created or bot.now_utc().isoformat()
        async with aiosqlite.connect(bot.DB) as db:
            cur = await db.execute(
                "INSERT INTO logs(guild_id,type,author_id,result,data,flags,status,created_at,season_id,"
                "review_channel,review_msg,parent_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid or self.GID, type_, author, result, json.dumps(data), "[]", status, created, season,
                 review_channel, review_msg, parent))
            await db.commit()
            return cur.lastrowid

    async def review_message(self, log_id, **kw):
        row = await bot.get_log(log_id)
        data = json.loads(row["data"])
        return self.message(row["review_msg"] or 555,
                            bot.render(log_id, row["type"], row["author_id"], data, row["result"]), **kw)

    async def balance(self, uid, gid=None):
        async with aiosqlite.connect(bot.DB) as db:
            cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (self.GID if gid is None else gid, uid))
            row = await cur.fetchone()
        return row[0] if row else None

    async def scalar(self, sql, params=()):
        async with aiosqlite.connect(bot.DB) as db:
            cur = await db.execute(sql, params)
            row = await cur.fetchone()
        return row[0] if row else None

    def last_text(self, mock):
        args, kwargs = mock.call_args
        return (args[0] if args else kwargs.get("content", "")) or ""


# =====================================================================  قاعدة البيانات والترحيل
class MigrationTests(BotTestCase):
    def build_legacy(self, path):
        c = sqlite3.connect(path)
        c.executescript("""
        CREATE TABLE settings(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE TABLE members(user_id INTEGER PRIMARY KEY, points INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE logs(id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, author_id INTEGER, result TEXT, data TEXT,
            flags TEXT, status TEXT DEFAULT 'pending', points INTEGER DEFAULT 0, reviewer_id INTEGER, reason TEXT,
            created_at TEXT);
        CREATE TABLE point_events(id INTEGER PRIMARY KEY AUTOINCREMENT, log_id INTEGER, user_id INTEGER,
            points INTEGER, created_at TEXT);
        CREATE TABLE links(user_id INTEGER PRIMARY KEY, roblox_id INTEGER, roblox_name TEXT);
        CREATE TABLE season_archive(season TEXT, user_id INTEGER, points INTEGER);
        CREATE TABLE guild_cfg(guild_id INTEGER, log_type TEXT, key TEXT, value INTEGER,
            PRIMARY KEY(guild_id, log_type, key));
        INSERT INTO settings VALUES('points:outside_win', 12);
        INSERT INTO members VALUES(111, 40);
        INSERT INTO members VALUES(222, 7);
        INSERT INTO logs(type,author_id,result,data,status,created_at) VALUES('outside',111,'win','{}','accepted','2026-01-01');
        INSERT INTO logs(type,author_id,result,data,status,created_at) VALUES('internal',222,NULL,'{}','pending','2026-01-02');
        INSERT INTO point_events(log_id,user_id,points,created_at) VALUES(1,111,8,'2026-01-01');
        INSERT INTO links VALUES(111, 5, 'Robby');
        INSERT INTO season_archive VALUES('old', 111, 30);
        INSERT INTO guild_cfg VALUES(1,'outside','review_channel',55);
        """)
        c.commit()
        c.close()

    async def test_legacy_db_migrates_without_data_loss(self):
        bot.DB = os.path.join(tempfile.mkdtemp(), "legacy.db")
        self.build_legacy(bot.DB)
        await bot.init_db()
        self.assertEqual(await self.balance(111, 0), 40)
        self.assertEqual(await self.balance(222, 0), 7)
        self.assertEqual(await self.scalar("SELECT type FROM logs WHERE id=1"), "1v1")
        self.assertEqual(await self.scalar("SELECT type FROM logs WHERE id=2"), "training")
        self.assertEqual(await self.scalar("SELECT value FROM settings WHERE key='points:1v1_win'"), "12")
        self.assertEqual(await self.scalar("SELECT roblox_name FROM links WHERE user_id=111"), "Robby")
        self.assertEqual(await self.scalar("SELECT points FROM season_archive WHERE season='old'"), 30)
        self.assertEqual(await self.scalar("SELECT value FROM guild_cfg WHERE log_type='1v1'"), 55)

    async def test_migration_is_idempotent(self):
        bot.DB = os.path.join(tempfile.mkdtemp(), "legacy.db")
        self.build_legacy(bot.DB)
        await bot.init_db()
        await bot.init_db()
        self.assertEqual(await self.balance(111, 0), 40)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM point_events WHERE type='opening'"), 2)

    async def test_migrated_balances_match_ledger(self):
        bot.DB = os.path.join(tempfile.mkdtemp(), "legacy.db")
        self.build_legacy(bot.DB)
        await bot.init_db()
        await bot.adopt_legacy_data(self.GID)
        self.assertEqual(await bot.ledger_mismatches(self.GID), [])
        self.assertEqual(await self.balance(111), 40)
        self.assertEqual(await self.scalar("SELECT guild_id FROM logs WHERE id=1"), self.GID)

    async def test_fresh_db_has_all_columns_and_indexes(self):
        cols = [r[1] for r in sqlite3.connect(bot.DB).execute("PRAGMA table_info(logs)")]
        for c in ("guild_id", "season_id", "parent_id", "review_msg", "public_msg", "reminded"):
            self.assertIn(c, cols)
        idx = [r[1] for r in sqlite3.connect(bot.DB).execute("PRAGMA index_list(point_events)")]
        self.assertIn("idx_point_events_season_user", idx)


# =====================================================================  دوال بحتة
class PureFunctionTests(unittest.TestCase):
    def test_youtube_validation(self):
        self.assertTrue(bot.valid_youtube("https://www.youtube.com/watch?v=abc123"))
        self.assertTrue(bot.valid_youtube("https://youtu.be/abc123"))
        for bad in ("https://evil.com/watch?v=1", "javascript:alert(1)", "https://youtube.com.evil.com/x",
                    "https://discord.gg/invite", "https://youtu.be/a b", "", "https://youtu.be/" + "a" * 300):
            self.assertIsNone(bot.valid_youtube(bad), bad)

    def test_template_substitution_is_safe(self):
        out = bot.apply_template("{score:>99999999} {author.__class__} {log_id} {missing}",
                                 {"score": "5-3", "author": "A", "log_id": 7})
        self.assertLess(len(out), 100)
        self.assertIn("7", out)
        self.assertIn("{missing}", out)
        self.assertIn("{author.__class__}", out)

    def test_render_truncation_keeps_header_and_status(self):
        data = {"my_team": "x" * 5000, "their_team": "y" * 5000, "crew": "c" * 500, "score": "1-0",
                "notes": "n" * 5000, "participants": list(range(1, 40))}
        text = bot.render(42, "war", 10, data, "win", flags=["f" * 400] * 9, status="rejected",
                          reviewer_id=5, reason="r" * 900)
        self.assertLessEqual(len(text), 2000)
        self.assertIn("`Log #42`", text)
        self.assertIn("Rejected", text)

    def test_render_handles_every_type_and_legacy_data(self):
        for t in bot.LOG_TYPES:
            self.assertIn("Log #1", bot.render(1, t, 10, {}, None))
        self.assertIn("a vs b", bot.render(1, "training", 10, {"p1": "a", "p2": "b"}, None))

    def test_log_id_parsing_ignores_user_text(self):
        msg = MagicMock(spec=discord.Message)
        msg.content = bot.render(7, "1v1", 1, {"notes": "Log #99 `Log #98`"}, "win")
        self.assertEqual(bot.parse_log_id(msg), 7)

    def test_target_index_and_dedupe(self):
        ladder = [("a", 20), ("b", 60), ("c", 130)]
        self.assertEqual([bot.target_index(ladder, p) for p in (0, 19, 20, 59, 60, 500)], [-1, -1, 0, 0, 1, 2])
        self.assertEqual(bot.dedupe([1, 2, 1, 3, 2]), [1, 2, 3])

    def test_registered_commands_are_valid(self):
        cmds = bot.bot.tree.get_commands()
        names = [c.name for c in cmds]
        self.assertLessEqual(len(cmds), 100)
        self.assertEqual(len(names), len(set(names)), "duplicate command names")
        for c in cmds:
            self.assertRegex(c.name, r"^[a-z0-9_-]{1,32}$")
            self.assertTrue(1 <= len(c.description) <= 100, f"{c.name}: description length {len(c.description)}")
        self.assertEqual(sorted(names), sorted(bot.ALL_BOT_COMMANDS),
                         "ALL_BOT_COMMANDS (used by /set-cmd-role) must match the real commands")


# =====================================================================  الصلاحيات
class PermissionTests(BotTestCase):
    async def test_command_role_lock(self):
        role = self.role("Special", 321)
        await bot.db_exec("INSERT INTO cmd_roles VALUES(?,?,?)", (self.GID, "points", role.id))
        plain, holder, admin = self.member(1), self.member(2, roles=[role]), self.member(3, admin=True)
        self.assertFalse(await bot.check_cmd_perm(self.interaction(plain), "points"))
        self.assertTrue(await bot.check_cmd_perm(self.interaction(holder), "points"))
        self.assertTrue(await bot.check_cmd_perm(self.interaction(admin), "points"))
        self.assertTrue(await bot.check_cmd_perm(self.interaction(plain), "rates"))

    async def test_editor_and_owner(self):
        commander = self.member(1, roles=[self.role("~DS | Commander", 5)])
        nobody = self.member(2)
        owner = self.member(999)
        self.assertTrue(bot.is_editor(commander))
        self.assertFalse(bot.is_editor(nobody))
        self.assertTrue(bot.is_admin(owner))

    async def test_reviewer_roles_configurable_per_type(self):
        rv = self.role("Judges", 77)
        await bot.roles_edit(self.GID, "tryout", "review", rv.id, True)
        judge, other = self.member(1, roles=[rv]), self.member(2, roles=[self.role("The Admiral", 8)])
        self.assertTrue(await bot.can_review(judge, "tryout"))
        self.assertFalse(await bot.can_review(other, "tryout"))
        self.assertTrue(await bot.can_review(other, "war"))

    async def test_tryout_submit_needs_tryouter_role(self):
        self.assertFalse(await bot.can_submit(self.member(1), "tryout"))
        self.assertTrue(await bot.can_submit(self.member(2, roles=[self.role("Tryout Master", 9)]), "tryout"))
        self.assertTrue(await bot.can_submit(self.member(3), "war"))


# =====================================================================  المراجعة والنقط (Ledger)
class AcceptRejectTests(BotTestCase):
    async def accept(self, log_id, reviewer, gid=None, msg=None):
        msg = msg or await self.review_message(log_id)
        i = self.interaction(reviewer, message=msg)
        await bot.accept_log(i)
        return i

    async def test_accept_awards_points_writes_ledger_and_posts_public_log(self):
        self.member(10)
        reviewer = self.member(20, admin=True)
        lid = await self.insert_log()
        i = await self.accept(lid, reviewer)
        self.assertEqual(await self.balance(10), 8)
        self.assertEqual(await self.scalar("SELECT status FROM logs WHERE id=?", (lid,)), "accepted")
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM point_events WHERE log_id=? AND type='log'", (lid,)), 1)
        self.log_ch.send.assert_awaited()
        self.assertEqual(await bot.ledger_mismatches(self.GID), [])
        i.response.defer.assert_awaited()

    async def test_cannot_review_own_log(self):
        author = self.member(10, admin=True)
        lid = await self.insert_log(author=10)
        i = await self.accept(lid, author)
        self.assertIn("own log", self.last_text(i.response.send_message))
        self.assertEqual(await self.scalar("SELECT status FROM logs WHERE id=?", (lid,)), "pending")

    async def test_non_reviewer_cannot_accept(self):
        self.member(10)
        lid = await self.insert_log()
        i = await self.accept(lid, self.member(30))
        self.assertIn("permission", self.last_text(i.response.send_message))
        self.assertIsNone(await self.balance(10))

    async def test_log_from_other_guild_is_rejected(self):
        self.member(10)
        reviewer = self.member(20, admin=True)
        lid = await self.insert_log(gid=2)
        i = await self.accept(lid, reviewer)
        self.assertIn("not found", self.last_text(i.response.send_message))
        self.assertIsNone(await self.balance(10))

    async def test_button_must_belong_to_the_logs_own_message(self):
        self.member(10)
        reviewer = self.member(20, admin=True)
        lid = await self.insert_log(review_msg=555)
        forged = self.message(12345, bot.render(lid, "1v1", 10, {}, "win"))
        i = await self.accept(lid, reviewer, msg=forged)
        self.assertIn("not found", self.last_text(i.response.send_message))
        self.assertIsNone(await self.balance(10))

    async def test_double_accept_only_counts_once(self):
        self.member(10)
        reviewer, reviewer2 = self.member(20, admin=True), self.member(21, admin=True)
        lid = await self.insert_log()
        await self.accept(lid, reviewer)
        i2 = await self.accept(lid, reviewer2)
        self.assertIn("already", self.last_text(i2.response.send_message))
        self.assertEqual(await self.balance(10), 8)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM point_events"), 1)

    async def test_log_from_closed_season_cannot_be_accepted(self):
        self.member(10)
        reviewer = self.member(20, admin=True)
        await bot.set_setting(self.GID, "state:current_season", "season_new")
        lid = await self.insert_log(season="main")
        i = await self.accept(lid, reviewer)
        self.assertIn("previous season", self.last_text(i.response.send_message))
        self.assertIsNone(await self.balance(10))

    async def test_war_points_go_only_to_confirmed_teammates(self):
        for u in (10, 20, 30):
            self.member(u)
        reviewer = self.member(40, admin=True)
        data = {"my_team": "a", "crew": "X", "their_team": "b", "score": "3-1",
                "participants": [20, 30], "confirmed_participants": [20]}
        lid = await self.insert_log(type_="war", data=data)
        await self.accept(lid, reviewer)
        self.assertEqual((await self.balance(10), await self.balance(20), await self.balance(30)), (10, 10, None))

    async def test_tryout_points_go_to_tester_and_pass_gives_crew_role(self):
        tester = self.member(10)
        cand = self.member(50)
        crew = self.role("~DS | Crew", 900)
        self.guild.roles.append(crew)
        reviewer = self.member(20, admin=True)
        data = {"score": "7/10", "candidate_id": 50, "candidate": "<@50>", "tester_id": 10}
        lid = await self.insert_log(type_="tryout", author=10, result="win", data=data)
        await self.accept(lid, reviewer)
        self.assertEqual(await self.balance(10), 5)
        self.assertIsNone(await self.balance(50))
        cand.add_roles.assert_awaited()
        self.assertIn(crew, cand.add_roles.await_args.args)
        tester.add_roles.assert_not_awaited()

    async def test_reject_requires_permission_and_blocks_second_decision(self):
        self.member(10)
        reviewer = self.member(20, admin=True)
        lid = await self.insert_log()
        msg = await self.review_message(lid)
        modal = bot.RejectModal(lid, msg)
        modal.reason._value = "blurry proof"
        i = self.interaction(reviewer, message=msg)
        await modal.on_submit(i)
        self.assertEqual(await self.scalar("SELECT status FROM logs WHERE id=?", (lid,)), "rejected")
        i2 = self.interaction(reviewer, message=msg)
        await modal.on_submit(i2)
        self.assertIn("already", self.last_text(i2.followup.send))

    async def test_confirm_participation_rules(self):
        self.member(10)
        for u in (20, 30):
            self.member(u)
        data = {"my_team": "a", "crew": "X", "their_team": "b", "score": "1-0", "participants": [20]}
        lid = await self.insert_log(type_="war", data=data)
        view = bot.ReviewView()
        stranger = self.interaction(self.member(30), message=await self.review_message(lid))
        await view.confirm_part(stranger, None)
        self.assertIn("not listed", self.last_text(stranger.response.send_message))
        tagged = self.interaction(self.member(20), message=await self.review_message(lid))
        await view.confirm_part(tagged, None)
        row = await bot.get_log(lid)
        self.assertEqual(json.loads(row["data"])["confirmed_participants"], [20])
        again = self.interaction(self.member(20), message=await self.review_message(lid))
        await view.confirm_part(again, None)
        self.assertIn("already", self.last_text(again.response.send_message))


# =====================================================================  الرتب
class RankTests(BotTestCase):
    def lvl(self, name):
        return next(r for r in self.lvl_roles if name in r.name)

    async def test_promotion_adds_new_role_and_removes_old(self):
        m = self.member(10, roles=[self.lvl("Lvl. 20")])
        role = await bot.update_member_rank(self.guild, m, 70)
        self.assertEqual(role, self.lvl("Lvl. 40"))
        self.assertIn(self.lvl("Lvl. 40"), m.add_roles.await_args.args)
        self.assertIn(self.lvl("Lvl. 20"), m.remove_roles.await_args.args)

    async def test_manual_higher_rank_is_never_removed_automatically(self):
        m = self.member(10, roles=[self.lvl("Lvl. 80")])
        self.assertIsNone(await bot.update_member_rank(self.guild, m, 10))
        m.remove_roles.assert_not_awaited()
        m.add_roles.assert_not_awaited()

    async def test_explicit_demote_removes_rank(self):
        m = self.member(10, roles=[self.lvl("Lvl. 80")])
        await bot.update_member_rank(self.guild, m, 10, allow_demote=True)
        m.remove_roles.assert_awaited()
        self.assertIn(self.lvl("Lvl. 80"), m.remove_roles.await_args.args)

    async def test_first_log_does_not_strip_existing_rank(self):
        m = self.member(10, roles=[self.lvl("Lvl. 60")])
        reviewer = self.member(20, admin=True)
        lid = await self.insert_log(author=10)
        i = self.interaction(reviewer, message=await self.review_message(lid))
        await bot.accept_log(i)
        m.remove_roles.assert_not_awaited()

    async def test_missing_rank_role_is_reported_not_crashing(self):
        self.guild.roles = []
        m = self.member(10)
        self.assertIsNone(await bot.update_member_rank(self.guild, m, 500))

    async def test_set_rank_points_keeps_ladder_ordered(self):
        admin = self.member(1, admin=True)
        i = self.interaction(admin)
        await bot.set_rank_points(i, choice("Lvl. 40"), 10)
        self.assertIn("❌", self.last_text(i.response.send_message))
        i2 = self.interaction(admin)
        await bot.set_rank_points(i2, choice("Lvl. 40"), 80)
        self.assertEqual(dict(await bot.get_ladder(self.GID))["Lvl. 40"], 80)


# =====================================================================  Revoke / Ledger / المواسم
class LedgerTests(BotTestCase):
    async def accepted_log(self, author=10):
        self.member(author)
        reviewer = self.member(20, admin=True)
        lid = await self.insert_log(author=author)
        await bot.accept_log(self.interaction(reviewer, message=await self.review_message(lid)))
        return lid, reviewer

    async def test_revoke_appends_reversal_and_keeps_history(self):
        lid, reviewer = await self.accepted_log()
        i = self.interaction(reviewer)
        await bot.revoke_log(i, lid, "cheating")
        self.assertEqual(await self.balance(10), 0)
        self.assertEqual(await self.scalar("SELECT status FROM logs WHERE id=?", (lid,)), "revoked")
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM point_events WHERE log_id=?", (lid,)), 2)
        self.assertEqual(await self.scalar("SELECT SUM(points) FROM point_events WHERE log_id=?", (lid,)), 0)
        self.assertEqual(await bot.ledger_mismatches(self.GID), [])

    async def test_revoke_twice_is_blocked(self):
        lid, reviewer = await self.accepted_log()
        await bot.revoke_log(self.interaction(reviewer), lid, "x")
        i2 = self.interaction(reviewer)
        await bot.revoke_log(i2, lid, "x")
        self.assertIn("not found", self.last_text(i2.response.send_message))
        self.assertEqual(await self.balance(10), 0)

    async def test_revoke_requires_review_permission(self):
        lid, _ = await self.accepted_log()
        i = self.interaction(self.member(77))
        await bot.revoke_log(i, lid, "x")
        self.assertEqual(await self.balance(10), 8)

    async def test_revoke_from_old_season_is_blocked(self):
        lid, reviewer = await self.accepted_log()
        await bot.set_setting(self.GID, "state:current_season", "season_new")
        i = self.interaction(reviewer)
        await bot.revoke_log(i, lid, "x")
        self.assertIn("archived season", self.last_text(i.response.send_message))
        self.assertEqual(await self.balance(10), 8)

    async def test_revoke_warns_when_rank_is_now_too_high(self):
        lid, reviewer = await self.accepted_log()
        self.members[10].roles = [self.lvl_roles[2]]  # Lvl. 60
        i = self.interaction(reviewer)
        await bot.revoke_log(i, lid, "x")
        self.assertIn("sync-ranks", self.last_text(i.followup.send))

    async def test_manual_points_are_ledgered_and_verified(self):
        admin = self.member(1, admin=True)
        m = self.member(10)
        await bot.set_points(self.interaction(admin), m, 30)
        await bot.set_points(self.interaction(admin), m, 12)
        self.assertEqual(await self.balance(10), 12)
        self.assertEqual(await self.scalar("SELECT SUM(points) FROM point_events WHERE user_id=10"), 12)
        self.assertEqual(await bot.ledger_mismatches(self.GID), [])

    async def test_verify_points_detects_and_fixes_drift(self):
        await self.accepted_log()
        await bot.db_exec("UPDATE members SET points=999 WHERE user_id=10")
        self.assertEqual(await bot.ledger_mismatches(self.GID), [(10, 999, 8)])
        admin = self.member(1, admin=True)
        await bot.verify_points(self.interaction(admin), True)
        self.assertEqual(await bot.ledger_mismatches(self.GID), [])
        self.assertEqual(await self.balance(10), 8)

    async def test_season_reset_archives_closes_pending_and_isolates_scores(self):
        await self.accepted_log()
        pending = await self.insert_log(author=11)
        admin = self.member(1, admin=True)
        i = self.interaction(admin)
        await bot.season_reset(i, "Season 1", "RESET", False)
        self.assertEqual(await self.balance(10), 0)
        self.assertEqual(await self.scalar("SELECT points FROM season_archive WHERE season='Season 1' AND user_id=10"), 8)
        self.assertEqual(await self.scalar("SELECT status FROM logs WHERE id=?", (pending,)), "rejected")
        self.assertNotEqual(await bot.get_current_season(self.GID), "main")
        self.assertEqual(await bot.ledger_mismatches(self.GID), [])
        i2 = self.interaction(admin)
        await bot.season_reset(i2, "Season 1", "RESET", False)
        self.assertIn("already exists", self.last_text(i2.followup.send))

    async def test_season_reset_needs_confirmation_and_editor(self):
        i = self.interaction(self.member(1, admin=True))
        await bot.season_reset(i, "S", "nope", False)
        self.assertIn("RESET", self.last_text(i.response.send_message))
        i2 = self.interaction(self.member(2))
        await bot.season_reset(i2, "S", "RESET", False)
        self.assertEqual(await bot.get_current_season(self.GID), "main")

    def setUp(self):
        super().setUp()


# =====================================================================  إنشاء اللوج / الملفات / Resubmit
class SubmissionTests(BotTestCase):
    def modal(self, log_type="1v1", result="win", files=None, parent=None):
        m = bot.LogModal(log_type, result, files if files is not None else [self.attachment()], {}, parent_id=parent)
        for key, ti in m.inputs.items():
            ti._value = "5-3" if key == "score" else "text"
        return m

    async def test_submit_creates_pending_log_with_message_and_hash(self):
        author = self.member(10)
        i = self.interaction(author)
        await self.modal().on_submit(i)
        self.assertEqual(await self.scalar("SELECT status FROM logs"), "pending")
        self.assertIsNotNone(await self.scalar("SELECT review_msg FROM logs"))
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM hashes"), 1)
        self.review_ch.send.assert_awaited()

    async def test_same_file_twice_is_flagged_and_owner_notified(self):
        author = self.member(10)
        with patch.object(bot, "dm_owner", AsyncMock(return_value=True)) as dm:
            await bot.set_setting(self.GID, "rule:daily_limit", 0)
            await self.modal().on_submit(self.interaction(author))
            await self.modal().on_submit(self.interaction(author))
            dm.assert_awaited_once()
        flags = await self.scalar("SELECT flags FROM logs WHERE id=2")
        self.assertIn("same file", flags)

    async def test_daily_limit_is_enforced(self):
        author = self.member(10)
        await bot.set_setting(self.GID, "rule:daily_limit", 1)
        await self.modal().on_submit(self.interaction(author))
        i2 = self.interaction(author)
        await self.modal(files=[self.attachment(data=b"img-2")]).on_submit(i2)
        self.assertIn("limit", self.last_text(i2.followup.send))
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM logs"), 1)

    async def test_bad_extension_and_oversize_are_refused(self):
        author = self.member(10)
        i = self.interaction(author)
        await self.modal(files=[self.attachment("virus.exe")]).on_submit(i)
        self.assertIn("unsupported", self.last_text(i.followup.send))
        i2 = self.interaction(author)
        await self.modal(files=[self.attachment("big.mp4", size=30 * MB)]).on_submit(i2)
        self.assertIn("25 MB", self.last_text(i2.followup.send))
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM logs"), 0)

    async def test_missing_review_channel_closes_the_log_instead_of_leaving_it_dangling(self):
        await bot.cfg_set(self.GID, "all", "review_channel", None)
        author = self.member(10)
        i = self.interaction(author)
        await self.modal().on_submit(i)
        self.assertEqual(await self.scalar("SELECT status FROM logs"), "rejected")
        self.assertIn("Couldn't post", self.last_text(i.followup.send))

    async def test_start_log_blocks_unconfigured_and_unauthorized(self):
        await bot.cfg_set(self.GID, "all", "review_channel", None)
        i = self.interaction(self.member(10))
        await bot.start_log(i, "1v1", "win", [self.attachment()])
        self.assertIn("not set", self.last_text(i.response.send_message))
        i2 = self.interaction(self.member(11))
        await bot.start_log(i2, "tryout", "win", [self.attachment()])
        self.assertIn("permission", self.last_text(i2.response.send_message))
        i3 = self.interaction(self.member(12))
        await bot.start_log(i3, "war", "win", [self.attachment()])
        self.assertIn("at least 2", self.last_text(i3.response.send_message))

    async def test_invalid_youtube_link_is_refused_before_any_work(self):
        i = self.interaction(self.member(10))
        extra = {}
        self.assertFalse(await bot.youtube_extra(i, "https://evil.example/x", extra))
        self.assertTrue(await bot.youtube_extra(self.interaction(self.member(10)), "https://youtu.be/abc", extra))
        self.assertEqual(extra["youtube_link"], "https://youtu.be/abc")

    async def test_tryout_candidate_must_be_someone_else(self):
        tester = self.member(10, roles=[self.role("Tryout Master", 9)])
        i = self.interaction(tester)
        await bot.log_tryout(i, tester, choice("win"), self.attachment())
        self.assertIn("❌", self.last_text(i.response.send_message))

    async def test_resubmit_ownership_status_and_single_child(self):
        author = self.member(10)
        att = self.attachment()
        old_msg = self.message(555, "x", attachments=[att])
        self.review_ch.fetch_message = AsyncMock(return_value=old_msg)
        rejected = await self.insert_log(status="rejected", author=10)

        stranger = self.interaction(self.member(11))
        await bot.resubmit(stranger, rejected)
        self.assertIn("not yours", self.last_text(stranger.response.send_message))

        pending = await self.insert_log(status="pending", author=10)
        notrej = self.interaction(author)
        await bot.resubmit(notrej, pending)
        self.assertIn("not rejected", self.last_text(notrej.response.send_message))

        ok = self.interaction(author)
        await bot.resubmit(ok, rejected)
        ok.response.send_modal.assert_awaited()
        modal = ok.response.send_modal.await_args.args[0]
        self.assertEqual(modal.parent_id, rejected)

        for key, ti in modal.inputs.items():
            ti._value = "5-3" if key == "score" else "text"
        await modal.on_submit(self.interaction(author))
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM logs WHERE parent_id=?", (rejected,)), 1)

        again = self.interaction(author)
        await bot.resubmit(again, rejected)
        self.assertIn("already resubmitted", self.last_text(again.response.send_message))


# =====================================================================  Backup
class BackupTests(BotTestCase):
    async def test_full_backup_is_owner_only_and_dm_only(self):
        full, dm = choice("full"), choice("owner")
        non_owner = self.interaction(self.member(5, admin=True))
        with patch.object(bot, "dm_owner", AsyncMock(return_value=True)) as dm_mock:
            await bot.backup_cmd(non_owner, full, dm)
            self.assertIn("owner only", self.last_text(non_owner.response.send_message))
            dm_mock.assert_not_awaited()

            owner_chat = self.interaction(self.member(999))
            await bot.backup_cmd(owner_chat, full, choice("chat"))
            self.assertIn("owner's DM", self.last_text(owner_chat.response.send_message))
            dm_mock.assert_not_awaited()

            owner = self.interaction(self.member(999))
            await bot.backup_cmd(owner, full, dm)
            dm_mock.assert_awaited_once()
            sent_file = dm_mock.await_args.kwargs["file"]
            self.assertTrue(sent_file.filename.startswith("crewbot-full-backup-"))
            data = sent_file.fp.getvalue()
            self.assertTrue(data.startswith(b"SQLite format 3"))

    async def test_guild_backup_contains_only_this_guild(self):
        await self.insert_log(gid=self.GID)
        await self.insert_log(gid=2)
        admin = self.interaction(self.member(5, admin=True))
        await bot.backup_cmd(admin, choice("today"), choice("chat"))
        sent = admin.followup.send.await_args.kwargs["file"]
        payload = json.loads(sent.fp.getvalue())
        self.assertEqual({row["guild_id"] for row in payload["logs"]}, {self.GID})

    async def test_non_editor_cannot_backup(self):
        i = self.interaction(self.member(6))
        await bot.backup_cmd(i, choice("today"), choice("chat"))
        self.assertIn("❌", self.last_text(i.response.send_message))


# =====================================================================  المهام التلقائية
class MaintenanceTests(BotTestCase):
    async def test_one_failing_job_does_not_stop_the_others(self):
        with patch.object(bot, "run_reminders", AsyncMock(side_effect=RuntimeError("boom"))), \
                patch.object(bot, "run_backup", AsyncMock()) as backup, \
                patch.object(bot, "run_weekly", AsyncMock()) as weekly:
            await bot.maintenance.coro()
            backup.assert_awaited_once()
            weekly.assert_awaited_once()

    async def test_reminder_is_sent_once_for_old_pending_logs(self):
        old = (bot.now_utc() - bot.timedelta(hours=60)).isoformat()
        lid = await self.insert_log(created=old)
        fresh = await self.insert_log(created=bot.now_utc().isoformat(), review_msg=556)
        review_msg = self.message(555, "x")
        self.review_ch.fetch_message = AsyncMock(return_value=review_msg)
        self.guild.id = self.GID
        bot.bot.guilds = [self.guild]
        await bot.run_reminders()
        review_msg.reply.assert_awaited_once()
        self.assertEqual(await self.scalar("SELECT reminded FROM logs WHERE id=?", (lid,)), 1)
        self.assertEqual(await self.scalar("SELECT COALESCE(reminded,0) FROM logs WHERE id=?", (fresh,)), 0)
        await bot.run_reminders()
        review_msg.reply.assert_awaited_once()

    async def test_reminders_can_be_disabled(self):
        await bot.set_setting(self.GID, "rule:reminder_hours", 0)
        await self.insert_log(created=(bot.now_utc() - bot.timedelta(days=9)).isoformat())
        review_msg = self.message(555, "x")
        self.review_ch.fetch_message = AsyncMock(return_value=review_msg)
        bot.bot.guilds = [self.guild]
        await bot.run_reminders()
        review_msg.reply.assert_not_awaited()

    async def test_auto_backup_runs_once_per_cycle_and_only_to_owner(self):
        bot.bot.guilds = [self.guild]
        with patch.object(bot, "dm_owner", AsyncMock(return_value=True)) as dm:
            await bot.run_backup()
            await bot.run_backup()
            dm.assert_awaited_once()

    async def test_auto_backup_failure_is_not_retried_every_cycle(self):
        bot.bot.guilds = [self.guild]
        with patch.object(bot, "dm_owner", AsyncMock(return_value=False)) as dm:
            await bot.run_backup()
            await bot.run_backup()
            dm.assert_awaited_once()


# =====================================================================  عرض / نقاط / إحصائيات
class ReadCommandsTests(BotTestCase):
    async def test_points_and_leaderboard_and_profile_do_not_crash(self):
        lid = None
        self.member(10)
        reviewer = self.member(20, admin=True)
        lid = await self.insert_log()
        await bot.accept_log(self.interaction(reviewer, message=await self.review_message(lid)))
        for cmd, args in ((bot.points_cmd, ()), (bot.profile, ()), (bot.rates, ()), (bot.stats, ())):
            i = self.interaction(self.members[10])
            await cmd(i, *args)
        i = self.interaction(self.members[10])
        await bot.leaderboard(i, choice("week"))
        self.assertIn("<@10>", self.last_text(i.response.send_message))
        i2 = self.interaction(self.members[10])
        await bot.leaderboard(i2, None)
        self.assertIn("8 pts", self.last_text(i2.response.send_message))

    async def test_settings_command_renders_effective_values(self):
        i = self.interaction(self.member(1, admin=True))
        await bot.settings_cmd(i)
        embed = i.response.send_message.await_args.kwargs["embed"]
        self.assertTrue(any("<#600>" in value for _, value in embed.fields))


if __name__ == "__main__":
    unittest.main()
