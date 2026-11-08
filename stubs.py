"""
بدائل خفيفة للمكتبات (discord / aiosqlite / aiohttp / dotenv) عشان الاختبارات تشتغل حتى لو مش متسطبة.
لو المكتبات الحقيقية موجودة (pip install -r requirements.txt) بتتستخدم هي ومبيتحطش أي بديل.
"""
import copy
import enum
import sqlite3
import sys
import types


def install():
    _install_discord()
    _install_aiosqlite()
    _install_misc()


def _mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


def _install_discord():
    try:
        import discord  # noqa: F401
        return
    except ImportError:
        pass

    class HTTPException(Exception):
        def __init__(self, *a, **k):
            super().__init__(*a)

    class Forbidden(HTTPException):
        pass

    class NotFound(HTTPException):
        pass

    class Object:
        def __init__(self, id):
            self.id = id

    class AllowedMentions:
        def __init__(self, **kw):
            self.kw = kw

        @classmethod
        def none(cls):
            return cls(everyone=False, users=False, roles=False)

    class File:
        def __init__(self, fp=None, filename=None, **kw):
            self.fp, self.filename = fp, filename

    class Embed:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.fields = []

        def add_field(self, name, value, inline=True):
            self.fields.append((name, value))

        def set_footer(self, **kw):
            pass

        def set_thumbnail(self, **kw):
            pass

    class Color:
        gold = green = blue = blurple = orange = classmethod(lambda cls: 0)

    class Intents:
        members = False

        @classmethod
        def default(cls):
            return cls()

    class _Enum(enum.Enum):
        pass

    TextStyle = enum.Enum("TextStyle", "short paragraph")
    ButtonStyle = enum.Enum("ButtonStyle", "success danger primary secondary")

    class Attachment: ...
    class Member: ...
    class Role: ...
    class TextChannel: ...
    class Message: ...
    class Guild: ...
    class Interaction: ...
    class User: ...

    class Client:
        def __init__(self, *, intents=None, **kw):
            self.guilds = []
            self.http = types.SimpleNamespace()
            self.user = None
            self.application_id = 1

        def add_view(self, v):
            pass

        def get_guild(self, gid):
            return next((g for g in self.guilds if g.id == gid), None)

        async def fetch_user(self, uid):
            raise NotFound("no user")

        async def wait_until_ready(self):
            return None

    utils = types.SimpleNamespace(find=lambda pred, seq: next((x for x in seq if pred(x)), None))

    class _View:
        def __init__(self, *, timeout=180):
            self.timeout = timeout

    class _TextInput:
        def __init__(self, *, label="", required=True, max_length=None, default=None, style=None, **kw):
            self.label, self.required, self.max_length, self.default = label, required, max_length, default
            self._value = None

        @property
        def value(self):
            return self._value or self.default or ""

    class _Modal:
        def __init_subclass__(cls, **kw):
            cls._title = kw.get("title")

        def __init__(self, *, title=None, **kw):
            self.title = title or getattr(type(self), "_title", None)
            self.children = []
            for name in dir(type(self)):
                attr = getattr(type(self), name, None)
                if isinstance(attr, _TextInput):
                    setattr(self, name, copy.copy(attr))

        def add_item(self, item):
            self.children.append(item)

    def _button(**kw):
        return lambda fn: fn

    ui = _mod("discord.ui", View=_View, Modal=_Modal, TextInput=_TextInput, button=_button, Button=object)

    class Choice:
        def __init__(self, name, value):
            self.name, self.value = name, value

        def __class_getitem__(cls, item):
            return cls

    class Range:
        def __class_getitem__(cls, item):
            return item[0]

    class CommandTree:
        def __init__(self, client):
            self._cmds = []

        def command(self, *, name, description="..."):
            def deco(fn):
                self._cmds.append(types.SimpleNamespace(name=name, description=description, callback=fn))
                return fn
            return deco

        def error(self, fn):
            return fn

        def get_commands(self):
            return list(self._cmds)

        def copy_global_to(self, **kw):
            pass

        async def sync(self, **kw):
            return []

    ident = lambda **kw: (lambda fn: fn)  # noqa: E731
    app_commands = _mod("discord.app_commands", Choice=Choice, Range=Range, CommandTree=CommandTree,
                        choices=ident, describe=ident, autocomplete=ident)

    class _Loop:
        def __init__(self, coro):
            self.coro = coro
            self._running = False

        def is_running(self):
            return self._running

        def start(self):
            self._running = True

        def before_loop(self, fn):
            return fn

    tasks = _mod("discord.ext.tasks", loop=lambda **kw: (lambda fn: _Loop(fn)))
    ext = _mod("discord.ext", tasks=tasks)
    _mod("discord", HTTPException=HTTPException, Forbidden=Forbidden, NotFound=NotFound, Object=Object,
         AllowedMentions=AllowedMentions, File=File, Embed=Embed, Color=Color, Intents=Intents,
         TextStyle=TextStyle, ButtonStyle=ButtonStyle, Attachment=Attachment, Member=Member, Role=Role,
         TextChannel=TextChannel, Message=Message, Guild=Guild, Interaction=Interaction, User=User,
         Client=Client, utils=utils, ui=ui, app_commands=app_commands, ext=ext)


def _install_aiosqlite():
    try:
        import aiosqlite  # noqa: F401
        return
    except ImportError:
        pass

    class _Cursor:
        def __init__(self, cur):
            self._cur = cur

        @property
        def rowcount(self):
            return self._cur.rowcount

        @property
        def lastrowid(self):
            return self._cur.lastrowid

        async def fetchone(self):
            return self._cur.fetchone()

        async def fetchall(self):
            return self._cur.fetchall()

    class connect:
        def __init__(self, path, **kw):
            self._path = path
            self._conn = None

        async def __aenter__(self):
            self._conn = sqlite3.connect(self._path, timeout=5)
            return self

        async def __aexit__(self, *a):
            self._conn.close()

        @property
        def row_factory(self):
            return self._conn.row_factory

        @row_factory.setter
        def row_factory(self, v):
            self._conn.row_factory = v

        async def execute(self, sql, params=()):
            return _Cursor(self._conn.execute(sql, params))

        async def executescript(self, script):
            self._conn.executescript(script)

        async def commit(self):
            self._conn.commit()

        async def rollback(self):
            self._conn.rollback()

    _mod("aiosqlite", connect=connect, Row=sqlite3.Row)


def _install_misc():
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        _mod("aiohttp", ClientSession=object, ClientTimeout=lambda **kw: None)
    try:
        import dotenv  # noqa: F401
    except ImportError:
        _mod("dotenv", load_dotenv=lambda *a, **k: None)