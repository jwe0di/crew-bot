
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
import uuid
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
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

BOT_VERSION = "v3.8.0"
DB = os.getenv("DB_PATH", "crewbot.db")
JSON_CONFIG_PATH = os.getenv("JSON_CONFIG_PATH") or os.path.join(os.path.dirname(DB) or ".", "servers_settings.json")
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

# ---- حدود المرفقات ----
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".mkv"}
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_TOTAL_FILES_SIZE = 50 * 1024 * 1024
MAX_FILES_COUNT = 5
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


# --- سيرفر ويب مصغر لـ Railway ---
def run_dummy_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"DragonSlayerBot {BOT_VERSION} is online!".encode())

        def log_message(self, format, *args):
            pass

    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ================================================================ الإعدادات (config.py اختياري)
REQUIRED_TYPES = ("war", "1v1", "2v2", "training", "tryout")

_DEFAULT_LOG_TYPES = {
    "war": {"label": "Crew War Log", "fields": [
        ("my_team", "Who fought for Dragon Slayers", True, True),
        ("crew", "Enemy crew name", False, True),
        ("their_team", "Who fought for the enemy crew", True, True),
        ("score", "Score (e.g. 5-3)", False, True),
        ("notes", "Notes (optional)", True, False)]},
    "1v1": {"label": "1v1 Fight Log", "fields": [
        ("my_fighter", "Who fought for us", False, True),
        ("opponent", "Who we fought against", False, True),
        ("score", "Score (e.g. 5-3)", False, True),
        ("notes", "Notes (optional)", True, False)]},
    "2v2": {"label": "2v2 Fight Log", "fields": [
        ("my_team", "Our team", False, True),
        ("their_team", "Enemy team", False, True),
        ("score", "Score (e.g. 5-3)", False, True),
        ("notes", "Notes (optional)", True, False)]},
    "training": {"label": "Training Log", "fields": [
        ("participants", "Who trained", True, True),
        ("score", "Result / summary (optional)", False, False),
        ("notes", "Notes (optional)", True, False)]},
    "tryout": {"label": "Tryout Log", "fields": [
        ("score", "Tryout score / rating", False, True),
        ("roblox", "Candidate Roblox username (optional)", False, False),
        ("notes", "Notes (optional)", True, False)]},
}
_DEFAULT_POINTS = {"war_win": 10, "war_loss": 5, "1v1_win": 8, "1v1_loss": 4,
                   "2v2_win": 6, "2v2_loss": 3, "training": 3, "tryout": 5}
_DEFAULT_POINT_LABELS = {"war_win": "War - Win", "war_loss": "War - Loss",
                         "1v1_win": "1v1 - Win", "1v1_loss": "1v1 - Loss",
                         "2v2_win": "2v2 - Win", "2v2_loss": "2v2 - Loss",
                         "training": "Training", "tryout": "Tryout"}
_DEFAULT_LADDER = [("Lvl. 20", 20), ("Lvl. 40", 60), ("Lvl. 60", 130), ("Lvl. 80", 220), ("Lvl. 100", 350)]


def _load_log_types():
    cfg = getattr(config, "LOG_TYPES", None)
    if isinstance(cfg, dict) and all(
            t in cfg and isinstance(cfg[t].get("fields"), list) and all(len(f) == 4 for f in cfg[t]["fields"])
            for t in REQUIRED_TYPES):
        return {t: cfg[t] for t in REQUIRED_TYPES}
    print("⚠️ config.LOG_TYPES is missing the new log types; using built-in defaults")
    return _DEFAULT_LOG_TYPES


LOG_TYPES = _load_log_types()
_cfg_points = getattr(config, "DEFAULT_POINTS", {}) or {}
DEFAULT_POINTS = {k: int(_cfg_points.get(k, v)) for k, v in _DEFAULT_POINTS.items()}
_cfg_labels = getattr(config, "POINT_LABELS", {}) or {}
POINT_LABELS = {k: str(_cfg_labels.get(k, v)) for k, v in _DEFAULT_POINT_LABELS.items()}
LADDER = [(str(n), int(p)) for n, p in (getattr(config, "LADDER", None) or _DEFAULT_LADDER)]
EDITOR_ROLES = list(getattr(config, "EDITOR_ROLES", ["The Admiral", "Empyrean Monarch", "Commander"]))
REVIEWER_ROLES = list(getattr(config, "REVIEWER_ROLES", ["The Admiral", "Empyrean Monarch", "Commander", "Astral Apotheosis"]))
TRYOUTER_ROLES = list(getattr(config, "TRYOUTER_ROLES", ["Tryout Master"]))
CREW_ROLE_KEYWORD = getattr(config, "CREW_ROLE_KEYWORD", "Crew")

RULE_DEFAULTS = {
    "daily_limit": 5,
    "reminder_hours": 48,
    "backup_hours": 24,
    "weekly_post": 1,
    "war_participants": 1,
    "tryout_autorole": 1,
}
RULE_DEFAULTS.update(getattr(config, "RULE_DEFAULTS", {}) or {})
RULE_LABELS = {
    "daily_limit": "Max logs per member per 24h (0 = unlimited)",
    "reminder_hours": "Remind reviewers after N hours (0 = off)",
    "backup_hours": "Auto backup to owner every N hours (0 = off)",
    "weekly_post": "Weekly leaderboard post (1 = on, 0 = off)",
    "war_participants": "War points to confirmed teammates (1 = on, 0 = off)",
    "tryout_autorole": "Give Crew role on passed tryout (1 = on, 0 = off)",
}

RESULT_LABEL = {"win": "✅ Win", "loss": "❌ Loss"}
TRYOUT_LABEL = {"win": "✅ Passed", "loss": "❌ Failed"}
STATUS_LABEL = {"pending": "⏳ Pending", "accepted": "✅ Accepted",
                "rejected": "❌ Rejected", "revoked": "🚫 Revoked"}
RESULT_CHOICES = [app_commands.Choice(name="Win", value="win"),
                  app_commands.Choice(name="Loss", value="loss")]
TRYOUT_CHOICES = [app_commands.Choice(name="Passed", value="win"),
                  app_commands.Choice(name="Failed", value="loss")]
LOG_TYPE_CHOICES = [app_commands.Choice(name="All types (default)", value="all")] + [
    app_commands.Choice(name=v["label"][:100], value=k) for k, v in LOG_TYPES.items()]
KIND_CHANNEL = [
    app_commands.Choice(name="Review channel", value="review_channel"),
    app_commands.Choice(name="Log channel", value="log_channel"),
    app_commands.Choice(name="Audit channel", value="audit_channel"),
    app_commands.Choice(name="Update notices channel", value="update_channel")]
KIND_PING = [
    app_commands.Choice(name="Review ping", value="review_ping"),
    app_commands.Choice(name="Log ping", value="log_ping")]
KIND_ROLES = [
    app_commands.Choice(name="Reviewers", value="review"),
    app_commands.Choice(name="Submitters", value="submit")]
ACTIONS = [app_commands.Choice(name="Add", value="add"),
           app_commands.Choice(name="Remove", value="remove")]
PERIOD_CHOICES = [app_commands.Choice(name="All time", value="all"),
                  app_commands.Choice(name="This week", value="week")]
RULE_CHOICES = [app_commands.Choice(name=l[:100], value=k) for k, l in RULE_LABELS.items()]
POINT_CHOICES = [app_commands.Choice(name=l[:100], value=k) for k, l in POINT_LABELS.items()]
RANK_CHOICES = [app_commands.Choice(name=n, value=n) for n, _ in LADDER]
BACKUP_SCOPE_CHOICES = [
    app_commands.Choice(name="Guild Activity & Today", value="today"),
    app_commands.Choice(name="Full Database [Owner Only]", value="full")]
BACKUP_DEST_CHOICES = [
    app_commands.Choice(name="Here in chat", value="chat"),
    app_commands.Choice(name="Bot Owner DM", value="owner")]

UPDATE_PRESETS = {
    "bot_patch": {
        "label": f"🤖 Release notes {BOT_VERSION}",
        "title_en": f"DragonSlayerBot {BOT_VERSION}",
        "title_ar": f"إصدار البوت {BOT_VERSION}",
        "desc_en": ("• Per-user language selection via /language (Arabic, English, German, Italian, Hindi).\n"
                    "• Full localization for all commands, alerts, errors, and /help with flags (excluding combat/training/tryout logs).\n"
                    "• Full security hardening and SQLite WAL safety."),
        "desc_ar": ("• اختيار لغة مخصصة لكل عضو عبر /language (عربي، إنجليزي، ألماني، إيطالي، هندي).\n"
                    "• ترجمة شاملة لكل الأوامر والتنبيهات والأخطاء و /help مع الأعلام (باستثناء لوجات الحروب والفايتات).\n"
                    "• تقفيل كل الثغرات وتأمين قاعدة البيانات بالكامل."),
    }
}
UPDATE_TYPE_CHOICES = [app_commands.Choice(name=v["label"][:100], value=k) for k, v in UPDATE_PRESETS.items()]

ALL_BOT_COMMANDS = [
    "log-war", "log-1v1", "log-2v2", "log-training", "log-tryout", "resubmit",
    "log-update", "set-log-template", "points", "leaderboard", "profile",
    "stats", "rates", "link", "pending", "revoke-log", "inactive",
    "season-reset", "season-top", "backup", "setchannel", "setping",
    "setroles", "setrule", "set-log-points", "set-rank-points", "set-points",
    "sync-ranks", "verify-points", "settings", "sync", "set-cmd-role", "set-update-ping",
    "help", "send-help-panel", "restore-backup", "my-logs", "log-info", "reviewer-stats",
    "create-new-crew", "crews-list", "language", "crews-delete-all",
]

# ================================================================ نظام اللغات الشامل (Per-User + Guild Fallback)
LANGS = {
    "ar": ("العربية (Arabic)", "🇸🇦"),
    "en": ("English", "🇬🇧"),
    "de": ("Deutsch (German)", "🇩🇪"),
    "it": ("Italiano (Italian)", "🇮🇹"),
    "hi": ("हिन्दी (Hindi)", "🇮🇳"),
}
DEFAULT_LANG = "en"

TEXTS = {
    # أخطاء وصلاحيات عامة
    "no_perm": {
        "ar": "❌ مش معاك صلاحية لتنفيذ الأمر ده.",
        "en": "❌ You don't have permission to use this command.",
        "de": "❌ Du hast keine Berechtigung für diesen Befehl.",
        "it": "❌ Non hai i permessi per usare questo comando.",
        "hi": "❌ आपके पास इस कमांड का उपयोग करने की अनुमति नहीं है।",
    },
    "no_role": {
        "ar": "❌ مش معاك الرتبة المطلوبة لاستخدام الأمر ده.",
        "en": "❌ You don't have the required role to use this command.",
        "de": "❌ Dir fehlt die erforderliche Rolle für diesen Befehl.",
        "it": "❌ Non hai il ruolo richiesto per usare questo comando.",
        "hi": "❌ आपके पास इस कमांड के लिए आवश्यक भूमिका नहीं है।",
    },
    "server_only": {
        "ar": "❌ استخدم الأمر ده جوه السيرفر فقط.",
        "en": "❌ Use this command inside the server only.",
        "de": "❌ Verwende diesen Befehl nur auf dem Server.",
        "it": "❌ Usa questo comando solo all'interno del server.",
        "hi": "❌ इस कमांड का उपयोग केवल सर्वर के अंदर करें।",
    },
    "unexpected_err": {
        "ar": "❌ حصل خطأ غير متوقع. حاول تاني، ولو اتكرر بلّغ الإدارة.",
        "en": "❌ An unexpected error occurred. Please try again, or contact staff.",
        "de": "❌ Ein unerwarteter Fehler ist aufgetreten. Bitte versuche es erneut.",
        "it": "❌ Si è verificato un errore imprevisto. Riprova più tardi.",
        "hi": "❌ एक अनपेक्षित त्रुटि हुई। कृपया पुनः प्रयास करें।",
    },
    "db_err": {
        "ar": "❌ حصل خطأ في قاعدة البيانات. مفيش حاجة اتغيرت، حاول تاني.",
        "en": "❌ Database error. Nothing was changed, please try again.",
        "de": "❌ Datenbankfehler. Es wurde nichts geändert, bitte erneut versuchen.",
        "it": "❌ Errore del database. Nessuna modifica effettuata, riprova.",
        "hi": "❌ डेटाबेस त्रुटि। कुछ भी नहीं बदला गया, कृपया पुनः प्रयास करें।",
    },
    # أمر اللغة
    "language_prompt": {
        "ar": "🌐 **اختار لغتك المفضلة (هتتطبق عليك في كل أوامر وتنبيهات البوت):**",
        "en": "🌐 **Choose your preferred language (applies to all bot commands & alerts for you):**",
        "de": "🌐 **Wähle deine bevorzugte Sprache (gilt für alle Befehle & Hinweise für dich):**",
        "it": "🌐 **Scegli la tua lingua preferita (vale per tutti i comandi e gli avvisi per te):**",
        "hi": "🌐 **अपनी पसंदीदा भाषा चुनें (आपके लिए सभी कमांड और अलर्ट पर लागू होगी):**",
    },
    "lang_changed": {
        "ar": "✅ تم تغيير لغتك الشخصية إلى **العربية** بنجاح!",
        "en": "✅ Your personal language has been updated to **English**!",
        "de": "✅ Deine persönliche Sprache wurde auf **Deutsch** geändert!",
        "it": "✅ La tua lingua personale è stata impostata su **Italiano**!",
        "hi": "✅ आपकी व्यक्तिगत भाषा **हिन्दी** में बदल दी गई है!",
    },
    "invalid_lang": {
        "ar": "❌ لغة غير صالحة.",
        "en": "❌ Invalid language.",
        "de": "❌ Ungültige Sprache.",
        "it": "❌ Lingua non valida.",
        "hi": "❌ अमान्य भाषा।",
    },
    # المراجعة واللوجات (التنبيهات والأخطاء فقط، اللوجات نفسها مستثناة)
    "log_not_found": {
        "ar": "❌ اللوج غير موجود أو مش تبع السيرفر ده.",
        "en": "❌ Log not found for this message or server.",
        "de": "❌ Protokoll für diese Nachricht nicht gefunden.",
        "it": "❌ Log non trovato per questo messaggio.",
        "hi": "❌ इस संदेश के लिए लॉग नहीं मिला।",
    },
    "log_already_reviewed": {
        "ar": "⚠️ اللوج ده اتراجع أو اتقفل بالفعل.",
        "en": "⚠️ This log has already been reviewed or closed.",
        "de": "⚠️ Dieses Protokoll wurde bereits überprüft oder geschlossen.",
        "it": "⚠️ Questo log è già stato revisionato o chiuso.",
        "hi": "⚠️ यह लॉग पहले ही समीक्षा या बंद किया जा चुका है।",
    },
    "no_review_perm": {
        "ar": "❌ معندكش صلاحية مراجعة النوع ده من اللوجات.",
        "en": "❌ You don't have permission to review this log type.",
        "de": "❌ Du darfst diesen Protokolltyp nicht überprüfen.",
        "it": "❌ Non hai i permessi per revisionare questo tipo di log.",
        "hi": "❌ आपको इस लॉग प्रकार की समीक्षा करने की अनुमति नहीं है।",
    },
    "no_self_review": {
        "ar": "❌ مينفعش تراجع اللوج بتاعك انت.",
        "en": "❌ You cannot review your own log.",
        "de": "❌ Du kannst dein eigenes Protokoll nicht überprüfen.",
        "it": "❌ Non puoi revisionare il tuo stesso log.",
        "hi": "❌ आप अपने स्वयं के लॉग की समीक्षा नहीं कर सकते।",
    },
    "old_season_log": {
        "ar": "❌ اللوج ده تبع موسم قديم ومينفعش يتقبل. ارفضه بدلاً من كده.",
        "en": "❌ This log belongs to a previous season and can't be accepted. Reject it instead.",
        "de": "❌ Dieses Protokoll gehört zu einer alten Saison. Bitte lehne es ab.",
        "it": "❌ Questo log appartiene a una stagione precedente. Rifiutalo.",
        "hi": "❌ यह लॉग पिछले सीज़न का है और इसे स्वीकार नहीं किया जा सकता।",
    },
    "log_rejected_ok": {
        "ar": "✅ تم رفض اللوج.",
        "en": "✅ Log rejected.",
        "de": "✅ Protokoll abgelehnt.",
        "it": "✅ Log rifiutato.",
        "hi": "✅ लॉग अस्वीकार कर दिया गया।",
    },
    "not_tagged": {
        "ar": "❌ اسمك مش متضاف كمشارك في اللوج ده.",
        "en": "❌ You are not listed as a tagged participant in this log.",
        "de": "❌ Du bist in diesem Protokoll nicht als Teilnehmer markiert.",
        "it": "❌ Non sei elencato come partecipante in questo log.",
        "hi": "❌ आप इस लॉग में प्रतिभागी के रूप में सूचीबद्ध नहीं हैं।",
    },
    "already_confirmed": {
        "ar": "✅ انت أكدت مشاركتك بالفعل.",
        "en": "✅ You have already confirmed your participation.",
        "de": "✅ Du hast deine Teilnahme bereits bestätigt.",
        "it": "✅ Hai già confermato la tua partecipazione.",
        "hi": "✅ आपने पहले ही अपनी भागीदारी की पुष्टि कर दी है।",
    },
    "part_confirmed": {
        "ar": "✅ تم تأكيد مشاركتك بنجاح!",
        "en": "✅ Your participation has been confirmed!",
        "de": "✅ Deine Teilnahme wurde bestätigt!",
        "it": "✅ La tua partecipazione è stata confermata!",
        "hi": "✅ आपकी भागीदारी की पुष्टि हो गई है!",
    },
    "daily_limit_hit": {
        "ar": "❌ وصلت للحد اليومي للوجات ({limit} لوج كل 24 ساعة).",
        "en": "❌ Daily log limit reached ({limit} logs per 24 hours).",
        "de": "❌ Tageslimit erreicht ({limit} Protokolle pro 24 Stunden).",
        "it": "❌ Limite giornaliero raggiunto ({limit} log ogni 24 ore).",
        "hi": "❌ दैनिक लॉग सीमा पूरी हो गई है (24 घंटे में {limit} लॉग)।",
    },
    "log_submitted": {
        "ar": "✅ تم إرسال اللوج `#{id}` للمراجعة بنجاح.",
        "en": "✅ Log `#{id}` submitted for review.",
        "de": "✅ Protokoll `#{id}` zur Überprüfung eingereicht.",
        "it": "✅ Log `#{id}` inviato per la revisione.",
        "hi": "✅ लॉग `#{id}` समीक्षा के लिए सबमिट किया गया।",
    },
    "no_submit_perm": {
        "ar": "❌ معندكش صلاحية تسجيل النوع ده من اللوجات.",
        "en": "❌ You don't have permission to submit this log type.",
        "de": "❌ Du darfst diesen Protokolltyp nicht einreichen.",
        "it": "❌ Non hai i permessi per inviare questo tipo di log.",
        "hi": "❌ आपको इस लॉग प्रकार को सबमिट करने की अनुमति नहीं है।",
    },
    "war_min_files": {
        "ar": "❌ حرب الكرو بتحتاج **ملفين إثبات على الأقل**.",
        "en": "❌ Crew War requires **at least 2 attachments**.",
        "de": "❌ Crew War erfordert **mindestens 2 Anhänge**.",
        "it": "❌ Crew War richiede **almeno 2 allegati**.",
        "hi": "❌ क्रू वॉर के लिए **कम से कम 2 अटैचमेंट** आवश्यक हैं।",
    },
    "min_one_file": {
        "ar": "❌ لازم ترفق **صورة أو فيديو واحد على الأقل** كإثبات.",
        "en": "❌ You must attach at least **one image or video** as proof.",
        "de": "❌ Du musst mindestens **ein Bild oder Video** als Beweis anhängen.",
        "it": "❌ Devi allegare almeno **un'immagine o un video** come prova.",
        "hi": "❌ आपको प्रमाण के रूप में कम से कम **एक छवि या वीडियो** संलग्न करना होगा।",
    },
    "no_review_ch": {
        "ar": "❌ قناة المراجعة مش متحدة للنوع ده. اطلب من الإدارة استخدام `/setchannel`.",
        "en": "❌ Review channel is not set for this log type. Ask an admin to use `/setchannel`.",
        "de": "❌ Überprüfungskanal ist nicht festgelegt. Bitte einen Admin `/setchannel` zu nutzen.",
        "it": "❌ Canale di revisione non impostato. Chiedi a un admin di usare `/setchannel`.",
        "hi": "❌ इस लॉग प्रकार के लिए समीक्षा चैनल सेट नहीं है। एडमिन से `/setchannel` का उपयोग करने को कहें।",
    },
    "bad_yt": {
        "ar": "❌ رابط اليوتيوب غير صالح. لازم يكون من youtube.com أو youtu.be.",
        "en": "❌ Invalid YouTube link. Must be from youtube.com or youtu.be.",
        "de": "❌ Ungültiger YouTube-Link. Muss von youtube.com oder youtu.be sein.",
        "it": "❌ Link YouTube non valido. Deve essere di youtube.com o youtu.be.",
        "hi": "❌ अमान्य YouTube लिंक। यह youtube.com या youtu.be से होना चाहिए।",
    },
    "bad_candidate": {
        "ar": "❌ المرشح لازم يكون عضو حقيقي تاني (مش انت ومش بوت).",
        "en": "❌ Candidate must be another real member (not yourself or a bot).",
        "de": "❌ Kandidat muss ein anderes echtes Mitglied sein (nicht du oder ein Bot).",
        "it": "❌ Il candidato deve essere un altro membro reale (non tu o un bot).",
        "hi": "❌ उम्मीदवार कोई अन्य वास्तविक सदस्य होना चाहिए (आप या बॉट नहीं)।",
    },
    "too_many_files": {
        "ar": "❌ ملفات كتير جداً (الحد الأقصى {max}).",
        "en": "❌ Too many files (max {max}).",
        "de": "❌ Zu viele Dateien (max. {max}).",
        "it": "❌ Troppi file (max {max}).",
        "hi": "❌ बहुत अधिक फ़ाइलें (अधिकतम {max})।",
    },
    "bad_file_ext": {
        "ar": "❌ صيغة الملف `{name}` غير مدعومة.",
        "en": "❌ File `{name}` has an unsupported format.",
        "de": "❌ Datei `{name}` hat ein nicht unterstütztes Format.",
        "it": "❌ Il file `{name}` ha un formato non supportato.",
        "hi": "❌ फ़ाइल `{name}` का प्रारूप समर्थित नहीं है।",
    },
    "file_too_big": {
        "ar": "❌ حجم الملف `{name}` أكبر من 25 ميجا.",
        "en": "❌ File `{name}` is larger than 25 MB.",
        "de": "❌ Datei `{name}` ist größer als 25 MB.",
        "it": "❌ Il file `{name}` supera i 25 MB.",
        "hi": "❌ फ़ाइल `{name}` 25 MB से बड़ी है।",
    },
    "total_too_big": {
        "ar": "❌ إجمالي حجم المرفقات تخطى 50 ميجا.",
        "en": "❌ Total attachments exceed 50 MB.",
        "de": "❌ Gesamtgröße der Anhänge überschreitet 50 MB.",
        "it": "❌ La dimensione totale degli allegati supera i 50 MB.",
        "hi": "❌ कुल अटैचमेंट 50 MB से अधिक हैं।",
    },
    # أوامر النقط، الترتيب، البروفايل، الإحصائيات
    "points_msg": {
        "ar": "💰 رصيد {member}: **{total}** نقطة.",
        "en": "💰 {member}'s balance: **{total}** points.",
        "de": "💰 Kontostand von {member}: **{total}** Punkte.",
        "it": "💰 Saldo di {member}: **{total}** punti.",
        "hi": "💰 {member} का बैलेंस: **{total}** अंक।",
    },
    "no_points_yet": {
        "ar": "ℹ️ مفيش نقاط مسجلة للفترة دي لسه.",
        "en": "ℹ️ No points recorded for this period yet.",
        "de": "ℹ️ Für diesen Zeitraum wurden noch keine Punkte erfasst.",
        "it": "ℹ️ Nessun punto registrato per questo periodo.",
        "hi": "ℹ️ इस अवधि के लिए अभी तक कोई अंक दर्ज नहीं किए गए हैं।",
    },
    "lb_week": {
        "ar": "📊 متصدري الأسبوع",
        "en": "📊 Weekly Leaderboard",
        "de": "📊 Wöchentliche Bestenliste",
        "it": "📊 Classifica Settimanale",
        "hi": "📊 साप्ताहिक लीडरबोर्ड",
    },
    "lb_season": {
        "ar": "🏆 متصدري الموسم",
        "en": "🏆 Season Leaderboard",
        "de": "🏆 Saison-Bestenliste",
        "it": "🏆 Classifica Stagionale",
        "hi": "🏆 सीज़न लीडरबोर्ड",
    },
    "no_pending": {
        "ar": "✅ مفيش لوجات منتظرة المراجعة حالياً.",
        "en": "✅ No logs waiting for review right now.",
        "de": "✅ Derzeit warten keine Protokolle auf Überprüfung.",
        "it": "✅ Nessun log in attesa di revisione al momento.",
        "hi": "✅ अभी समीक्षा के लिए कोई लॉग प्रतीक्षारत नहीं है।",
    },
    "no_my_logs": {
        "ar": "ℹ️ مفيش لوجات مسجلة ليك لسه.",
        "en": "ℹ️ You have no logs recorded yet.",
        "de": "ℹ️ Du hast noch keine Protokolle eingereicht.",
        "it": "ℹ️ Non hai ancora registrato nessun log.",
        "hi": "ℹ️ आपके पास अभी तक कोई लॉग दर्ज नहीं है।",
    },
    "sync_ok": {
        "ar": "✅ تم تحديث ومزامنة الأوامر بنجاح.",
        "en": "✅ Commands synced successfully.",
        "de": "✅ Befehle erfolgreich synchronisiert.",
        "it": "✅ Comandi sincronizzati con successo.",
        "hi": "✅ कमांड सफलतापूर्वक सिंक हो गए।",
    },
    "sync_fail": {
        "ar": "❌ فشلت مزامنة الأوامر.",
        "en": "❌ Command sync failed.",
        "de": "❌ Befehlssynchronisierung fehlgeschlagen.",
        "it": "❌ Sincronizzazione comandi fallita.",
        "hi": "❌ कमांड सिंक विफल रहा।",
    },
    # الطواقم (Crews)
    "crew_bad_members": {
        "ar": "❌ فيه عضو غير صحيح أو مش موجود في السيرفر. استخدم منشن أو ID للأعضاء، وافصل بينهم بمسافة أو فاصلة.",
        "en": "❌ One or more members are invalid or not in this server. Use member mentions or IDs, separated by spaces or commas.",
        "de": "❌ Ein oder mehrere Mitglieder sind ungültig oder nicht auf diesem Server. Verwende Erwähnungen oder IDs.",
        "it": "❌ Uno o più membri non sono validi o non sono in questo server. Usa menzioni o ID.",
        "hi": "❌ एक या अधिक सदस्य अमान्य हैं या इस सर्वर में नहीं हैं। मेंशन या ID का उपयोग करें।",
    },
    "no_crews": {
        "ar": "⚠️ لا توجد أي طواقم مسجلة حالياً.",
        "en": "⚠️ No crews are registered yet.",
        "de": "⚠️ Es sind noch keine Crews registriert.",
        "it": "⚠️ Nessun crew è ancora registrato.",
        "hi": "⚠️ अभी तक कोई क्रू पंजीकृत नहीं है।",
    },
    "crews_prompt": {
        "ar": "🎖️ **قائمة الطواقم المتاحة:**\nاختر طاقماً لاستعراض الأقسام والقيادات:",
        "en": "🎖️ **Available crews:**\nPick a crew to see its divisions and leaders:",
        "de": "🎖️ **Verfügbare Crews:**\nWähle eine Crew, um Abteilungen und Anführer zu sehen:",
        "it": "🎖️ **Crew disponibili:**\nScegli un crew per vedere reparti e capi:",
        "hi": "🎖️ **उपलब्ध क्रू:**\nविभाग और नेता देखने के लिए एक क्रू चुनें:",
    },
    "crew_created": {
        "ar": "✅ تم تأسيس الطاقم **{name}** بنجاح!",
        "en": "✅ Crew **{name}** has been created!",
        "de": "✅ Die Crew **{name}** wurde erstellt!",
        "it": "✅ Il crew **{name}** è stato creato!",
        "hi": "✅ क्रू **{name}** बना दिया गया है!",
    },
    "crew_exists": {
        "ar": "❌ اسم الطاقم مستخدم مسبقاً!",
        "en": "❌ That crew name is already taken!",
        "de": "❌ Dieser Crew-Name ist bereits vergeben!",
        "it": "❌ Questo nome di crew è già in uso!",
        "hi": "❌ यह क्रू नाम पहले से उपयोग में है!",
    },
    "crew_not_found": {
        "ar": "❌ هذا الطاقم غير موجود.",
        "en": "❌ This crew doesn't exist.",
        "de": "❌ Diese Crew existiert nicht.",
        "it": "❌ Questo crew non esiste.",
        "hi": "❌ यह क्रू मौजूद नहीं है।",
    },
    "crew_bad_name": {
        "ar": "❌ اسم الطاقم لازم يكون من 2 إلى 40 حرف ومن غير @ أو < > أو `.",
        "en": "❌ The crew name must be 2–40 characters and can't contain @, < >, or `.",
        "de": "❌ Der Crew-Name muss 2–40 Zeichen lang sein und darf weder @, < > noch ` enthalten.",
        "it": "❌ Il nome del crew deve avere 2–40 caratteri e non può contenere @, < > o `.",
        "hi": "❌ क्रू का नाम 2–40 अक्षरों का होना चाहिए और उसमें @, < > या ` नहीं हो सकते।",
    },
    "crew_bad_owner": {
        "ar": "❌ مالك الطاقم لازم يكون عضو حقيقي (مش بوت).",
        "en": "❌ The crew owner must be a real member (not a bot).",
        "de": "❌ Der Crew-Besitzer muss ein echtes Mitglied sein (kein Bot).",
        "it": "❌ Il proprietario del crew deve essere un membro reale (non un bot).",
        "hi": "❌ क्रू का मालिक एक असली सदस्य होना चाहिए (बॉट नहीं)।",
    },
    "crew_limit": {
        "ar": "❌ وصلت للحد الأقصى للطواقم ({max}).",
        "en": "❌ You've reached the crew limit ({max}).",
        "de": "❌ Das Crew-Limit ({max}) ist erreicht.",
        "it": "❌ Hai raggiunto il limite di crew ({max}).",
        "hi": "❌ क्रू की अधिकतम सीमा ({max}) पूरी हो गई है।",
    },
    "select_placeholder": {
        "ar": "📂 اختر طاقماً لعرض أقسامه...",
        "en": "📂 Pick a crew to view its divisions...",
        "de": "📂 Wähle eine Crew, um ihre Abteilungen zu sehen...",
        "it": "📂 Scegli un crew per vedere i reparti...",
        "hi": "📂 विभाग देखने के लिए एक क्रू चुनें...",
    },
    "none": {"ar": "لا يوجد", "en": "None", "de": "Keine", "it": "Nessuno", "hi": "कोई नहीं"},
    "empty": {"ar": "فارغ", "en": "Empty", "de": "Leer", "it": "Vuoto", "hi": "खाली"},
    "founded": {"ar": "تاريخ التأسيس", "en": "Founded", "de": "Gegründet", "it": "Fondato", "hi": "स्थापना"},
    "team_commanders": {"ar": "الكوماندرز", "en": "Commanders", "de": "Kommandanten", "it": "Comandanti", "hi": "कमांडर"},
    "browse_page": {"ar": "صفحة {page} من {pages}", "en": "Page {page} of {pages}", "de": "Seite {page} von {pages}", "it": "Pagina {page} di {pages}", "hi": "पृष्ठ {page} / {pages}"},
}


def tr(key: str, lang: str, **kw) -> str:
    table = TEXTS.get(key, {})
    text = table.get(lang) or table.get("en") or key
    return text.format(**kw) if kw else text


async def user_lang(guild_id: int, user_id: Optional[int] = None) -> str:
    """يجلب لغة العضو الشخصية أولاً، ولو مش مختار بيرجع للغة السيرفر أو الافتراضية."""
    if user_id is not None:
        v = await raw_setting(guild_id, f"state:lang:{user_id}")
        if v in LANGS:
            return v
    v = await raw_setting(guild_id, "state:server_lang")
    if v in LANGS:
        return v
    return DEFAULT_LANG


# ================================================================ قاعدة البيانات الكاملة
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    guild_id INTEGER, key TEXT, value TEXT,
    PRIMARY KEY(guild_id, key)
);
CREATE TABLE IF NOT EXISTS members(
    guild_id INTEGER, user_id INTEGER, points INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS guild_channels(
    guild_id INTEGER PRIMARY KEY, review_channel INTEGER, log_channel INTEGER, audit_channel INTEGER
);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL,
    author_id INTEGER NOT NULL,
    result TEXT, data TEXT, flags TEXT,
    status TEXT DEFAULT 'pending',
    points INTEGER DEFAULT 0,
    reviewer_id INTEGER, reason TEXT,
    created_at TEXT NOT NULL,
    review_channel INTEGER, review_msg INTEGER,
    public_channel INTEGER, public_msg INTEGER,
    reviewed_at TEXT,
    reminded INTEGER DEFAULT 0,
    parent_id INTEGER,
    season_id TEXT DEFAULT 'main'
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
    guild_id INTEGER NOT NULL DEFAULT 0,
    log_id INTEGER,
    user_id INTEGER NOT NULL,
    points INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    type TEXT DEFAULT 'log',
    season_id TEXT DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS links(
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, roblox_id INTEGER, roblox_name TEXT,
    PRIMARY KEY(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS season_archive(
    season TEXT, guild_id INTEGER, user_id INTEGER, points INTEGER,
    PRIMARY KEY(season, guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS cmd_roles(
    guild_id INTEGER, cmd_name TEXT, role_id INTEGER,
    PRIMARY KEY(guild_id, cmd_name)
);
CREATE TABLE IF NOT EXISTS crews(
    guild_id INTEGER,
    crew_name TEXT,
    owner_id INTEGER,
    captains_data TEXT,
    voice_captains_data TEXT,
    team_z_data TEXT,
    team_y_data TEXT,
    team_x_data TEXT,
    team_s_data TEXT,
    team_a_data TEXT,
    team_z_voice_data TEXT,
    team_y_voice_data TEXT,
    team_x_voice_data TEXT,
    team_s_voice_data TEXT,
    team_a_voice_data TEXT,
    team_z_commanders_data TEXT,
    team_y_commanders_data TEXT,
    team_x_commanders_data TEXT,
    team_s_commanders_data TEXT,
    team_a_commanders_data TEXT,
    created_at TEXT,
    PRIMARY KEY(guild_id, crew_name)
);
"""

LEGACY_COPY = {
    "settings": "INSERT OR IGNORE INTO settings(guild_id,key,value) SELECT 0, key, CAST(value AS TEXT) FROM settings_legacy",
    "members": "INSERT OR IGNORE INTO members(guild_id,user_id,points) SELECT 0, user_id, points FROM members_legacy",
    "links": "INSERT OR IGNORE INTO links(guild_id,user_id,roblox_id,roblox_name) SELECT 0, user_id, roblox_id, roblox_name FROM links_legacy",
    "season_archive": "INSERT OR IGNORE INTO season_archive(season,guild_id,user_id,points) SELECT season, 0, user_id, points FROM season_archive_legacy",
    "point_events": "INSERT INTO point_events(guild_id,log_id,user_id,points,created_at,type,season_id) SELECT 0, log_id, user_id, points, created_at, 'log', 'main' FROM point_events_legacy",
}
LOG_COLUMNS = [
    ("guild_id", "INTEGER NOT NULL DEFAULT 0"), ("review_channel", "INTEGER"), ("review_msg", "INTEGER"),
    ("public_channel", "INTEGER"), ("public_msg", "INTEGER"), ("reviewed_at", "TEXT"),
    ("reminded", "INTEGER DEFAULT 0"), ("parent_id", "INTEGER"), ("season_id", "TEXT DEFAULT 'main'"),
]
LEGACY_TYPE_MAP = {"outside": "1v1", "internal": "training"}
LEGACY_POINT_KEYS = {"points:outside_win": "points:1v1_win", "points:outside_loss": "points:1v1_loss",
                     "points:internal": "points:training", "template:outside": "template:1v1",
                     "template:internal": "template:training"}

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


async def table_columns(db, table):
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
            "guild_id": "INTEGER NOT NULL DEFAULT 0", "crew_name": "TEXT NOT NULL DEFAULT ''",
            "owner_id": "INTEGER NOT NULL DEFAULT 0", "captains_data": "TEXT NOT NULL DEFAULT '[]'",
            "voice_captains_data": "TEXT NOT NULL DEFAULT '[]'", "team_z_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_y_data": "TEXT NOT NULL DEFAULT '[]'", "team_x_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_s_data": "TEXT NOT NULL DEFAULT '[]'", "team_a_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_z_voice_data": "TEXT NOT NULL DEFAULT '[]'", "team_y_voice_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_x_voice_data": "TEXT NOT NULL DEFAULT '[]'", "team_s_voice_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_a_voice_data": "TEXT NOT NULL DEFAULT '[]'", "team_z_commanders_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_y_commanders_data": "TEXT NOT NULL DEFAULT '[]'", "team_x_commanders_data": "TEXT NOT NULL DEFAULT '[]'",
            "team_s_commanders_data": "TEXT NOT NULL DEFAULT '[]'", "team_a_commanders_data": "TEXT NOT NULL DEFAULT '[]'",
            "created_at": "TEXT NOT NULL DEFAULT ''",
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
            cur = await db.execute("SELECT (SELECT COUNT(*) FROM settings) + (SELECT COUNT(*) FROM guild_cfg) "
                                   "+ (SELECT COUNT(*) FROM role_perms) + (SELECT COUNT(*) FROM cmd_roles)")
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
                await db.execute("INSERT OR IGNORE INTO guild_cfg VALUES(?,?,?,?)",
                                 (r["guild_id"], r["log_type"], r["key"], r["value"]))
            for r in data.get("role_perms", []):
                await db.execute("INSERT OR IGNORE INTO role_perms VALUES(?,?,?,?)",
                                 (r["guild_id"], r["log_type"], r["kind"], r["role_id"]))
            for r in data.get("cmd_roles", []):
                await db.execute("INSERT OR IGNORE INTO cmd_roles VALUES(?,?,?)", (r["guild_id"], r["cmd_name"], r["role_id"]))
            await db.commit()
        print("✅ Settings restored from the JSON snapshot (fresh database).")
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
            for name, sql in (("guild_cfg", "SELECT * FROM guild_cfg"), ("role_perms", "SELECT * FROM role_perms"),
                              ("cmd_roles", "SELECT * FROM cmd_roles"),
                              ("settings", "SELECT guild_id, key, value FROM settings WHERE key NOT LIKE 'state:%'")):
                cur = await db.execute(sql)
                out[name] = [dict(r) for r in await cur.fetchall()]
        out.update({"version": BOT_VERSION, "description": "Configuration snapshot only.",
                    "updated_at": now_utc().isoformat()})
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
    await db_exec(
        "INSERT INTO settings(guild_id, key, value) VALUES(?,?,?) "
        "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (guild_id, str(key), str(value)))
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
        cur = await db.execute(
            "SELECT value FROM guild_cfg WHERE guild_id=? AND key=? AND log_type IN (?, 'all') "
            "ORDER BY (log_type='all') LIMIT 1", (guild_id, key, log_type))
        row = await cur.fetchone()
    return row[0] if row and row[0] is not None else None


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
    await save_config_snapshot_json()


async def roles_get(guild_id, log_type, kind):
    async with aiosqlite.connect(DB) as db:
        for t in (log_type, "all"):
            cur = await db.execute(
                "SELECT role_id FROM role_perms WHERE guild_id=? AND log_type=? AND kind=?", (guild_id, t, kind))
            ids = [r[0] for r in await cur.fetchall()]
            if ids:
                return ids
    return []


async def roles_edit(guild_id, log_type, kind, role_id, add: bool):
    async with aiosqlite.connect(DB) as db:
        if add:
            await db.execute("INSERT OR IGNORE INTO role_perms VALUES(?,?,?,?)", (guild_id, log_type, kind, role_id))
        else:
            await db.execute("DELETE FROM role_perms WHERE guild_id=? AND log_type=? AND kind=? AND role_id=?",
                             (guild_id, log_type, kind, role_id))
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


async def points_for(guild_id: int, log_type, result) -> int:
    key = f"{log_type}_{result}" if log_type in ("war", "1v1", "2v2") else log_type
    v = await get_setting(guild_id, "points:" + key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return DEFAULT_POINTS.get(key, 0)


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


# ================================================================ الصلاحيات والحماية
def has_role(member, names) -> bool:
    return any(n.lower() in r.name.lower() for r in getattr(member, "roles", []) for n in names)


def is_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if owner_id() and member.id == owner_id():
        return True
    return member.guild_permissions.administrator


def is_editor(member) -> bool:
    return is_admin(member) or has_role(member, EDITOR_ROLES)


async def can_review(member, log_type) -> bool:
    if is_admin(member):
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
    if is_admin(member):
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
    if is_admin(i.user):
        return True
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT role_id FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, cmd_name))
        row = await cur.fetchone()
    if not row:
        return True
    return any(r.id == row[0] for r in i.user.roles)


async def gate(i: discord.Interaction, cmd_name: str, editor=False) -> bool:
    if not i.guild or not isinstance(i.user, discord.Member):
        await i.response.send_message(tr("server_only", DEFAULT_LANG), ephemeral=True)
        return False
    lang = await user_lang(i.guild.id, i.user.id)
    if not await check_cmd_perm(i, cmd_name):
        await i.response.send_message(tr("no_role", lang), ephemeral=True)
        return False
    if editor and not is_editor(i.user):
        await i.response.send_message(tr("no_perm", lang), ephemeral=True)
        return False
    return True


# ================================================================ مساعدات
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
    except (discord.HTTPException, discord.NotFound):
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
    return discord.AllowedMentions(
        roles=[discord.Object(id=role_id)] if role_id else False,
        users=[discord.Object(id=u) for u in user_ids] if user_ids else False,
        everyone=False)


def find_crew_role(guild):
    kw = CREW_ROLE_KEYWORD.lower()
    return discord.utils.find(lambda r: r.name.lower().rstrip().endswith(kw), guild.roles)


def ladder_roles(guild):
    return [discord.utils.find(lambda r, n=n: n.lower() in r.name.lower(), guild.roles) for n, _ in LADDER]


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
    print(f"❌ Unhandled error in interaction: {type(error).__name__}: {error}")
    lang = await user_lang(i.guild.id, i.user.id) if i.guild else DEFAULT_LANG
    msg = tr("unexpected_err", lang)
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


bot = CrewBot()


@bot.tree.error
async def on_app_command_error(i: discord.Interaction, error):
    await report_error(i, error)


# ================================================================ شكل اللوج الثابت (مستثنى تماماً كطلبك)
_EMO = getattr(config, "EMOJIS", {}) or {}
ANGEL = _EMO.get("angel", "<:emoji_2:1568348048906649652>")
MOON = _EMO.get("bullet", "<a:emoji_15:1568348248522100757>")
ARROW = _EMO.get("arrow", "<a:emoji_8:1568348117236187237>")
CROWN = _EMO.get("crown", "<:emoji_11:1568348158495432754>")
_SPARK = _EMO.get("spark", "<a:emoji_6:1568348105781416008>")
DIVIDER = _EMO.get("divider", f"{_SPARK}   ●▬▬▬▬▬▬▬▬▬▬▬☭   ☭▬▬▬▬▬▬▬▬▬▬▬●   {_SPARK}")
MSG_LIMIT = 1900


def clip(v, n=350):
    v = str(v) if v not in (None, "") else "-"
    return v if len(v) <= n else v[: n - 1] + "…"


async def get_custom_template(guild_id, log_type):
    t = await raw_setting(guild_id, f"template:{log_type}")
    return t or None


def apply_template(template: str, values: dict) -> str:
    return TEMPLATE_VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def render(log_id, log_type, author_id, data, result, flags=None, status="pending",
           points=None, reviewer_id=None, reason=None, pts_to=None, custom_template=None):
    label = LOG_TYPES.get(log_type, {}).get("label", log_type)
    d = lambda k, default="-": clip(data.get(k, default))
    participants = dedupe([int(x) for x in data.get("participants", [])]) if isinstance(data.get("participants"), list) else []
    confirmed = {int(x) for x in data.get("confirmed_participants", [])}

    header = f"{ANGEL}  **{label}**  {ANGEL}  🆔 `Log #{log_id}`\n\n{DIVIDER}\n\n"
    body = None
    if custom_template:
        values = {
            "log_id": log_id, "author": f"<@{author_id}>", "score": d("score"), "notes": d("notes"),
            "youtube_link": data.get("youtube_link", "-"), "my_team": d("my_team"),
            "their_team": d("their_team"), "crew": d("crew"), "my_fighter": d("my_fighter"),
            "opponent": d("opponent"), "participants": d("participants") if not participants else
            " ".join(f"<@{p}>" for p in participants), "candidate": data.get("candidate", "-"),
            "roblox": d("roblox"), "p1": d("p1"), "p2": d("p2"),
        }
        body = header + apply_template(clip(custom_template, 1200), values) + "\n\n" + DIVIDER + "\n"

    if body is None:
        m = header + f"{MOON} **Logged by / مسجّل بواسطة:** <@{author_id}>\n"
        if log_type == "war":
            m += f"{MOON} **Dragon Slayer's Fighters:**\n{ARROW}  {d('my_team')}\n\n{DIVIDER}\n\n"
            m += f"{MOON} **{clip(data.get('crew', 'Enemy Crew'), 60)} Fighters:**\n{ARROW}  {d('their_team')}\n\n"
        elif log_type == "1v1":
            m += f"{MOON} **Dragon Slayer's Fighter:**\n{ARROW}  {d('my_fighter')}\n\n{DIVIDER}\n\n"
            m += f"{MOON} **Opponent:**\n{ARROW}  {d('opponent')}\n\n"
        elif log_type == "2v2":
            m += f"{MOON} **Dragon Slayer's Team:**\n{ARROW}  {d('my_team')}\n\n{DIVIDER}\n\n"
            m += f"{MOON} **Enemy Team:**\n{ARROW}  {d('their_team')}\n\n"
        elif log_type == "training":
            people = data.get("participants") if not isinstance(data.get("participants"), list) else None
            if not people and (data.get("p1") or data.get("p2")):
                people = f"{data.get('p1', '-')} vs {data.get('p2', '-')}"
            m += f"{MOON} **Training Participants:**\n{ARROW}  {clip(people)}\n\n"
        elif log_type == "tryout":
            tester = data.get("tester_id") or author_id
            m += f"{MOON} **Tested by:** <@{tester}>\n"
            cand = data.get("candidate", "-")
            if data.get("roblox"):
                cand += f" ({clip(data['roblox'], 40)})"
            m += f"{MOON} **Tryout Candidate:**\n{ARROW}  {cand}\n\n"

        if log_type in ("war", "2v2") and participants:
            m += f"{MOON} **Tagged Teammates Verification:**\n"
            for p_id in participants[:8]:
                m += f"• <@{p_id}> — **{'✅ Confirmed' if p_id in confirmed else '⏳ Unconfirmed'}**\n"
            m += "\n"

        m += f"{DIVIDER}\n\n"
        if result:
            labels = TRYOUT_LABEL if log_type == "tryout" else RESULT_LABEL
            m += f"      {CROWN} **Result / النتيجة:** {labels.get(result, result)}\n"
        m += f"      • **Score / السكور:** {d('score')}\n"
        if data.get("notes"):
            m += f"      • **Notes:** {d('notes')}\n"
        if data.get("youtube_link"):
            m += f"      • **YouTube Video:** <{data['youtube_link']}>\n"
        body = m

    footer = f"\n**Status / الحالة:** {STATUS_LABEL[status]}"
    if points is not None:
        footer += f" | **Points / النقاط:** +{points}"
        if pts_to:
            footer += " → " + " ".join(f"<@{u}>" for u in list(pts_to)[:6])
    if reviewer_id and status != "pending":
        who = {"accepted": "Accepted by / مقبول بواسطة", "rejected": "Rejected by / مرفوض بواسطة", "revoked": "Revoked by / ملغي بواسطة"}[status]
        footer += f"\n{MOON} **{who}:** <@{reviewer_id}>"
    if reason:
        footer += f"\n**Reason / السبب:** {clip(reason, 300)}"
    if flags:
        footer += "\n⚠️ **Warnings / تحذيرات:**\n" + "\n".join("• " + clip(f, 120) for f in flags[:5])
    if len(body) + len(footer) > MSG_LIMIT:
        body = body[: max(200, MSG_LIMIT - len(footer) - 1)] + "…"
    return (body + footer)[:MSG_LIMIT + 100]


# ================================================================ الرتب (ترقية فقط تلقائيًا)
def progress_bar(total, ladder, width=10):
    nxt = next(((n, p) for n, p in ladder if p > total), None)
    if not nxt:
        return "█" * width + " MAX"
    prev = max([p for _, p in ladder if p <= total], default=0)
    filled = max(0, min(width, int((total - prev) / max(1, nxt[1] - prev) * width)))
    return f"{'█' * filled}{'░' * (width - filled)} {total}/{nxt[1]} → {nxt[0]}"


def target_index(ladder, total):
    target = -1
    for idx, (_, pts) in enumerate(ladder):
        if total >= pts:
            target = idx
    return target


async def update_member_rank(guild, member, total, allow_demote=False):
    ladder = await get_ladder(guild.id)
    roles = ladder_roles(guild)
    target = target_index(ladder, total)
    held = [idx for idx, r in enumerate(roles) if r and r in member.roles]
    current = max(held) if held else -1

    if allow_demote:
        if target == current and len(held) <= 1:
            return None
    elif target <= current:
        return None

    target_role = roles[target] if 0 <= target < len(roles) else None
    if target >= 0 and target_role is None:
        await audit(f"⚠️ Role for **{ladder[target][0]}** was not found in this server, rank update skipped for {member.mention}.", guild)
        return None

    to_add = [target_role] if target_role and target_role not in member.roles else []
    to_remove = [roles[idx] for idx in held if roles[idx] != target_role]
    try:
        if to_add:
            await member.add_roles(*to_add, reason="Rank update based on points")
        if to_remove:
            await member.remove_roles(*to_remove, reason="Rank update based on points")
    except discord.Forbidden:
        await audit(f"❌ [Role Permission Error] Can't manage roles for {member.mention}. Move the bot's role above the Lvl roles.", guild)
        return None
    except discord.HTTPException as e:
        await audit(f"❌ [Discord API Error] Rank update failed for {member.mention}: {e}", guild)
        return None

    if target_role and target > current:
        ch_id = await cfg_get(guild.id, "all", "log_channel")
        ch = guild.get_channel(ch_id) if ch_id else None
        if ch:
            try:
                await ch.send(f"🎉 {member.mention} has been promoted to {target_role.mention}!",
                              allowed_mentions=discord.AllowedMentions(users=[member], roles=False))
            except discord.HTTPException:
                pass
        try:
            await member.send(f"🎉 You've been promoted to **{target_role.name}** in **{guild.name}**!")
        except discord.HTTPException:
            pass
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
        if not member:
            continue
        try:
            if await update_member_rank(guild, member, pts, allow_demote):
                changed += 1
        except Exception as e:
            print(f"⚠️ Rank update failed for {uid}: {e}")
        await asyncio.sleep(0.25)
    return changed


# ================================================================ المراجعة
async def resolve_log(i: discord.Interaction, need_pending=True):
    if not i.guild or not isinstance(i.user, discord.Member):
        await i.response.send_message(tr("server_only", DEFAULT_LANG), ephemeral=True)
        return None
    lang = await user_lang(i.guild.id, i.user.id)
    log_id = parse_log_id(i.message)
    row = await get_log(log_id) if log_id else None
    if (not row or row["guild_id"] != i.guild.id
            or (row["review_msg"] and row["review_msg"] != i.message.id)):
        await i.response.send_message(tr("log_not_found", lang), ephemeral=True)
        return None
    if need_pending and row["status"] != "pending":
        await i.response.send_message(tr("log_already_reviewed", lang), ephemeral=True)
        return None
    return row


async def accept_log(i: discord.Interaction):
    row = await resolve_log(i)
    if not row:
        return
    lang = await user_lang(i.guild.id, i.user.id)
    if not await can_review(i.user, row["type"]):
        return await i.response.send_message(tr("no_review_perm", lang), ephemeral=True)
    if row["author_id"] == i.user.id:
        return await i.response.send_message(tr("no_self_review", lang), ephemeral=True)
    season = row["season_id"] or "main"
    if season != await get_current_season(i.guild.id):
        return await i.response.send_message(tr("old_season_log", lang), ephemeral=True)
    await i.response.defer()

    log_id = row["id"]
    data = json.loads(row["data"])
    flags = json.loads(row["flags"] or "[]")
    pts = await points_for(i.guild.id, row["type"], row["result"])
    recips = await recipients_for(row, data)
    stamp = now_utc().isoformat()
    totals = {}

    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "UPDATE logs SET status='accepted', points=?, reviewer_id=?, reviewed_at=? "
                "WHERE id=? AND status='pending'", (pts, i.user.id, stamp, log_id))
            if cur.rowcount == 0:
                await db.rollback()
                return await i.followup.send(tr("log_already_reviewed", lang), ephemeral=True)
            for uid in recips:
                await db.execute(
                    "INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + ?", (i.guild.id, uid, pts, pts))
                await db.execute(
                    "INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) "
                    "VALUES(?,?,?,?,?,?,?)", (i.guild.id, log_id, uid, pts, stamp, "log", season))
                cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, uid))
                totals[uid] = (await cur.fetchone())[0]
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"❌ accept_log DB error: {e}")
            return await i.followup.send(tr("db_err", lang), ephemeral=True)

    custom_tmpl = await get_custom_template(i.guild.id, row["type"])
    pub_id = await cfg_get(i.guild.id, row["type"], "log_channel")
    log_ch = i.guild.get_channel(pub_id) if pub_id else None
    if log_ch:
        ping_id = await cfg_get(i.guild.id, row["type"], "log_ping")
        text = render(log_id, row["type"], row["author_id"], data, row["result"], None,
                      "accepted", pts, i.user.id, pts_to=recips, custom_template=custom_tmpl)
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
            await i.followup.send(f"Accepted, but I couldn't post in the log channel: {e}", ephemeral=True)
    else:
        await i.followup.send("Accepted, but no log channel is set. Use /setchannel.", ephemeral=True)

    reviewed = render(log_id, row["type"], row["author_id"], data, row["result"], flags,
                      "accepted", pts, i.user.id, pts_to=recips, custom_template=custom_tmpl)
    try:
        await i.message.edit(content=reviewed, view=None, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass

    for uid, total in totals.items():
        try:
            member = await get_member(i.guild, uid)
            await update_member_rank(i.guild, member, total)
        except (discord.NotFound, discord.Forbidden) as e:
            print(f"⚠️ Log accepted, but failed to update rank for {uid}: {e}")

    if (row["type"] == "tryout" and row["result"] == "win" and data.get("candidate_id")
            and await rule(i.guild.id, "tryout_autorole")):
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

    async def on_error(self, interaction, error):
        await report_error(interaction, error)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        lang = await user_lang(i.guild.id, i.user.id)
        row = await get_log(self.log_id)
        if not row or row["guild_id"] != i.guild.id:
            return await i.followup.send(tr("log_not_found", lang), ephemeral=True)
        if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
            return await i.followup.send(tr("no_review_perm", lang), ephemeral=True)
        if row["author_id"] == i.user.id:
            return await i.followup.send(tr("no_self_review", lang), ephemeral=True)

        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE logs SET status='rejected', reviewer_id=?, reason=?, reviewed_at=? "
                    "WHERE id=? AND status='pending'",
                    (i.user.id, self.reason.value, now_utc().isoformat(), self.log_id))
                if cur.rowcount == 0:
                    await db.rollback()
                    return await i.followup.send(tr("log_already_reviewed", lang), ephemeral=True)
                await db.commit()
            except Exception as e:
                await db.rollback()
                print(f"❌ reject DB error: {e}")
                return await i.followup.send(tr("db_err", lang), ephemeral=True)

        data = json.loads(row["data"])
        flags = json.loads(row["flags"] or "[]")
        custom_tmpl = await get_custom_template(i.guild.id, row["type"])
        text = render(self.log_id, row["type"], row["author_id"], data, row["result"], flags,
                      "rejected", reviewer_id=i.user.id, reason=self.reason.value, custom_template=custom_tmpl)
        try:
            await self.message.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        await i.followup.send(tr("log_rejected_ok", lang), ephemeral=True)
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

    async def on_error(self, interaction, error, item):
        await report_error(interaction, error)

    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, custom_id="log:accept")
    async def accept(self, i: discord.Interaction, button: discord.ui.Button):
        await accept_log(i)

    @discord.ui.button(label="Reject", emoji="❌", style=discord.ButtonStyle.danger, custom_id="log:reject")
    async def reject(self, i: discord.Interaction, button: discord.ui.Button):
        row = await resolve_log(i)
        if not row:
            return
        lang = await user_lang(i.guild.id, i.user.id)
        if not await can_review(i.user, row["type"]):
            return await i.response.send_message(tr("no_review_perm", lang), ephemeral=True)
        if row["author_id"] == i.user.id:
            return await i.response.send_message(tr("no_self_review", lang), ephemeral=True)
        await i.response.send_modal(RejectModal(row["id"], i.message))

    @discord.ui.button(label="Confirm Participation", emoji="✋", style=discord.ButtonStyle.primary,
                       custom_id="log:confirm_part")
    async def confirm_part(self, i: discord.Interaction, button: discord.ui.Button):
        row = await resolve_log(i)
        if not row:
            return
        lang = await user_lang(i.guild.id, i.user.id)
        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute("SELECT status, data FROM logs WHERE id=? AND guild_id=?",
                                       (row["id"], i.guild.id))
                log_row = await cur.fetchone()
                if not log_row or log_row[0] != "pending":
                    await db.rollback()
                    return await i.response.send_message(tr("log_already_reviewed", lang), ephemeral=True)
                data = json.loads(log_row[1])
                participants = [int(x) for x in data.get("participants", [])]
                if i.user.id not in participants:
                    await db.rollback()
                    return await i.response.send_message(tr("not_tagged", lang), ephemeral=True)
                confirmed = [int(x) for x in data.get("confirmed_participants", [])]
                if i.user.id in confirmed:
                    await db.rollback()
                    return await i.response.send_message(tr("already_confirmed", lang), ephemeral=True)
                confirmed.append(i.user.id)
                data["confirmed_participants"] = confirmed
                await db.execute("UPDATE logs SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), row["id"]))
                await db.commit()
            except Exception as e:
                await db.rollback()
                print(f"❌ confirm DB error: {e}")
                return await i.response.send_message(tr("db_err", lang), ephemeral=True)

        flags = json.loads(row["flags"] or "[]")
        custom_tmpl = await get_custom_template(i.guild.id, row["type"])
        updated = render(row["id"], row["type"], row["author_id"], data, row["result"], flags,
                         custom_template=custom_tmpl)
        await i.response.send_message(tr("part_confirmed", lang), ephemeral=True)
        try:
            await i.message.edit(content=updated, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


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
                style=discord.TextStyle.paragraph if long else discord.TextStyle.short)
            self.add_item(ti)
            self.inputs[key] = ti

    async def on_error(self, interaction, error):
        await report_error(interaction, error)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        lang = await user_lang(i.guild.id, i.user.id)
        if len(self.files) > MAX_FILES_COUNT:
            return await i.followup.send(tr("too_many_files", lang, max=MAX_FILES_COUNT), ephemeral=True)
        total_size = 0
        for a in self.files:
            if isinstance(a, discord.Attachment):
                ext = os.path.splitext(a.filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    return await i.followup.send(tr("bad_file_ext", lang, name=a.filename), ephemeral=True)
                if a.size > MAX_FILE_SIZE:
                    return await i.followup.send(tr("file_too_big", lang, name=a.filename), ephemeral=True)
                total_size += a.size
        if total_size > MAX_TOTAL_FILES_SIZE:
            return await i.followup.send(tr("total_too_big", lang), ephemeral=True)

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
                        "SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? "
                        "AND status IN ('pending','accepted')", (i.guild.id, i.user.id, since))
                    if (await cur.fetchone())[0] >= limit:
                        await db.rollback()
                        return await i.followup.send(tr("daily_limit_hit", lang, limit=limit), ephemeral=True)
                cur = await db.execute(
                    "INSERT INTO logs(guild_id, type, author_id, result, data, flags, created_at, season_id, parent_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (i.guild.id, self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False),
                     json.dumps(flags, ensure_ascii=False), now_utc().isoformat(), season, self.parent_id))
                log_id = cur.lastrowid
                for h in hashes:
                    await db.execute("INSERT OR IGNORE INTO hashes VALUES(?,?)", (h, log_id))
                await db.commit()
            except Exception as e:
                await db.rollback()
                print(f"❌ submit DB error: {e}")
                return await i.followup.send(tr("db_err", lang), ephemeral=True)

        ch_id = await cfg_get(i.guild.id, self.log_type, "review_channel")
        ch = i.guild.get_channel(ch_id) if ch_id else None
        ping_id = await cfg_get(i.guild.id, self.log_type, "review_ping")
        custom_tmpl = await get_custom_template(i.guild.id, self.log_type)
        text = render(log_id, self.log_type, i.user.id, data, self.result, flags, custom_template=custom_tmpl)
        content = (f"<@&{ping_id}>\n" if ping_id else "") + text
        am = mentions(ping_id)

        async def fail_publish(reason):
            await db_exec("UPDATE logs SET status='rejected', reason=? WHERE id=?",
                          (f"failed to post for review: {reason}"[:200], log_id))
            await i.followup.send(
                f"❌ Couldn't post the log for review ({reason}). Ask an admin to check /setchannel and permissions.",
                ephemeral=True)

        if not ch:
            return await fail_publish("review channel not set")
        sent = None
        try:
            limit_bytes = getattr(i.guild, "filesize_limit", 25 * 1024 * 1024) or 25 * 1024 * 1024
            if blobs and total_size <= limit_bytes:
                try:
                    sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am,
                                         files=[discord.File(io.BytesIO(b), a.filename) for a, b in blobs])
                except discord.Forbidden:
                    raise
                except discord.HTTPException:
                    sent = None
            if sent is None:
                sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am)
                if proof_urls:
                    await ch.send("**Attached Proof Links (may expire):**\n" + "\n".join(proof_urls)[:1800],
                                  allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            return await fail_publish(str(e)[:150])

        await db_exec("UPDATE logs SET review_channel=?, review_msg=? WHERE id=?", (ch.id, sent.id, log_id))
        if flags:
            await dm_owner(f"⚠️ {i.user.mention} submitted Log #{log_id} with warnings:\n" + "\n".join(flags))
        await i.followup.send(tr("log_submitted", lang, id=log_id), ephemeral=True)


async def inspect_blobs(blobs, guild_id):
    hashes, flags = [], []
    for att, data in blobs:
        h = hashlib.sha256(data).hexdigest()
        hashes.append(h)
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT h.log_id FROM hashes h JOIN logs l ON l.id=h.log_id "
                "WHERE h.hash=? AND l.guild_id=? AND l.status NOT IN ('rejected') LIMIT 1", (h, guild_id))
            row = await cur.fetchone()
        if row:
            flags.append(f"{att.filename}: same file was used in Log #{row[0]}")
    return hashes, flags


async def start_log(i: discord.Interaction, log_type, result, files: List[Optional[discord.Attachment]], extra=None):
    files = [f for f in files if f is not None]
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message(tr("server_only", DEFAULT_LANG), ephemeral=True)
    lang = await user_lang(i.guild.id, i.user.id)
    if not await check_cmd_perm(i, f"log-{log_type}"):
        return await i.response.send_message(tr("no_role", lang), ephemeral=True)
    if not await can_submit(i.user, log_type):
        return await i.response.send_message(tr("no_submit_perm", lang), ephemeral=True)
    if log_type == "war" and len(files) < 2:
        return await i.response.send_message(tr("war_min_files", lang), ephemeral=True)
    if log_type != "war" and len(files) < 1:
        return await i.response.send_message(tr("min_one_file", lang), ephemeral=True)
    rid = await cfg_get(i.guild.id, log_type, "review_channel")
    if not rid or not i.guild.get_channel(rid):
        return await i.response.send_message(tr("no_review_ch", lang), ephemeral=True)
    limit = await rule(i.guild.id, "daily_limit")
    if limit:
        since = (now_utc() - timedelta(hours=24)).isoformat()
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND created_at>=? "
                "AND status IN ('pending','accepted')", (i.guild.id, i.user.id, since))
            n = (await cur.fetchone())[0]
        if n >= limit:
            return await i.response.send_message(tr("daily_limit_hit", lang, limit=limit), ephemeral=True)
    await i.response.send_modal(LogModal(log_type, result, files, extra or {}))


async def youtube_extra(i: discord.Interaction, youtube_link: Optional[str], extra: dict):
    if youtube_link:
        yt = valid_youtube(youtube_link)
        if not yt:
            lang = await user_lang(i.guild.id, i.user.id) if i.guild else DEFAULT_LANG
            await i.response.send_message(tr("bad_yt", lang), ephemeral=True)
            return False
        extra["youtube_link"] = yt
    return True


@bot.tree.command(name="log-war", description="Log a Crew War (min 2 attachments) | تسجيل حرب كرو")
@app_commands.choices(result=RESULT_CHOICES)
@app_commands.describe(result="Win or Loss", file1="First proof", file2="Second proof",
                       file3="Additional proof", youtube_link="YouTube link",
                       player1="Teammate 1", player2="Teammate 2", player3="Teammate 3", player4="Teammate 4")
async def log_war(i: discord.Interaction, result: app_commands.Choice[str],
                  file1: discord.Attachment, file2: discord.Attachment,
                  file3: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None,
                  player1: Optional[discord.Member] = None, player2: Optional[discord.Member] = None,
                  player3: Optional[discord.Member] = None, player4: Optional[discord.Member] = None):
    ids = dedupe([p.id for p in (player1, player2, player3, player4) if p and not p.bot and p.id != i.user.id])
    extra = {"participants": ids}
    if not await youtube_extra(i, youtube_link, extra):
        return
    await start_log(i, "war", result.value, [file1, file2, file3], extra)


@bot.tree.command(name="log-1v1", description="Log a 1v1 fight | تسجيل فايت 1v1")
@app_commands.choices(result=RESULT_CHOICES)
@app_commands.describe(result="Win or Loss", proof1="Proof 1", proof2="Proof 2", youtube_link="YouTube link")
async def log_1v1(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment,
                  proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None):
    extra = {}
    if not await youtube_extra(i, youtube_link, extra):
        return
    await start_log(i, "1v1", result.value, [proof1, proof2], extra)


@bot.tree.command(name="log-2v2", description="Log a 2v2 fight | تسجيل فايت 2v2")
@app_commands.choices(result=RESULT_CHOICES)
@app_commands.describe(result="Win or Loss", proof1="Proof 1", proof2="Proof 2", youtube_link="YouTube link",
                       teammate="Teammate")
async def log_2v2(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment,
                  proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None,
                  teammate: Optional[discord.Member] = None):
    ids = [teammate.id] if teammate and not teammate.bot and teammate.id != i.user.id else []
    extra = {"participants": ids}
    if not await youtube_extra(i, youtube_link, extra):
        return
    await start_log(i, "2v2", result.value, [proof1, proof2], extra)


@bot.tree.command(name="log-training", description="Log training | تسجيل تدريب داخلي")
@app_commands.describe(proof1="Proof 1", proof2="Proof 2")
async def log_training(i: discord.Interaction, proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None):
    await start_log(i, "training", None, [proof1, proof2])


@bot.tree.command(name="log-tryout", description="Log a tryout | تسجيل تراي أوت")
@app_commands.choices(result=TRYOUT_CHOICES)
@app_commands.describe(candidate="Candidate", result="Passed/Failed", proof="Proof")
async def log_tryout(i: discord.Interaction, candidate: discord.Member, result: app_commands.Choice[str],
                     proof: discord.Attachment):
    if candidate.bot or candidate.id == i.user.id:
        lang = await user_lang(i.guild.id, i.user.id) if i.guild else DEFAULT_LANG
        return await i.response.send_message(tr("bad_candidate", lang), ephemeral=True)
    extra = {"candidate_id": candidate.id, "candidate": candidate.mention, "tester_id": i.user.id}
    await start_log(i, "tryout", result.value, [proof], extra)

@bot.tree.command(name="resubmit", description="Fix and resend a rejected log | إعادة إرسال لوج مرفوض")
async def resubmit(i: discord.Interaction, log_id: int):
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message(tr("server_only", DEFAULT_LANG), ephemeral=True)
    lang = await user_lang(i.guild.id, i.user.id)
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id or row["author_id"] != i.user.id:
        return await i.response.send_message(tr("log_not_found", lang), ephemeral=True)
    if row["status"] != "rejected" or row["type"] not in LOG_TYPES:
        return await i.response.send_message("❌ This log is not rejected." if lang != "ar" else "❌ اللوج ده مش مرفوض.", ephemeral=True)
    if not await check_cmd_perm(i, f"log-{row['type']}"):
        return await i.response.send_message(tr("no_role", lang), ephemeral=True)
    if not await can_submit(i.user, row["type"]):
        return await i.response.send_message(tr("no_submit_perm", lang), ephemeral=True)

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id FROM logs WHERE parent_id=? AND status IN ('pending','accepted') LIMIT 1",
                               (log_id,))
        child = await cur.fetchone()
    if child:
        return await i.response.send_message(f"This log was already resubmitted as #{child[0]}.", ephemeral=True)
    data = json.loads(row["data"])
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
        return await i.response.send_message("Could not fetch the old attachments. Please submit a new log.", ephemeral=True)
    extra = {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants", "youtube_link") if k in data}
    await i.response.send_modal(LogModal(row["type"], row["result"], files, extra, defaults=data, parent_id=log_id))


@bot.tree.command(name="revoke-log", description="Cancel an accepted log | إلغاء لوج مقبول")
async def revoke_log(i: discord.Interaction, log_id: int, reason: app_commands.Range[str, 1, 300]):
    if not await gate(i, "revoke-log"):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id or row["status"] != "accepted":
        return await i.response.send_message(tr("log_not_found", lang), ephemeral=True)
    if not await can_review(i.user, row["type"]):
        return await i.response.send_message(tr("no_perm", lang), ephemeral=True)
    if (row["season_id"] or "main") != await get_current_season(i.guild.id):
        return await i.response.send_message(tr("old_season_log", lang), ephemeral=True)
    await i.response.defer(ephemeral=True)

    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "UPDATE logs SET status='revoked', reason=?, reviewer_id=? WHERE id=? AND status='accepted'",
                (reason, i.user.id, log_id))
            if cur.rowcount == 0:
                await db.rollback()
                return await i.followup.send(tr("log_already_reviewed", lang), ephemeral=True)
            cur = await db.execute(
                "SELECT user_id, SUM(points) FROM point_events WHERE guild_id=? AND log_id=? AND type='log' "
                "GROUP BY user_id", (i.guild.id, log_id))
            events = await cur.fetchall()
            stamp = now_utc().isoformat()
            for uid, p in events:
                await db.execute("UPDATE members SET points = points - ? WHERE guild_id=? AND user_id=?",
                                 (p, i.guild.id, uid))
                await db.execute(
                    "INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) "
                    "VALUES(?,?,?,?,?,?,?)", (i.guild.id, log_id, uid, -p, stamp, "revoke", row["season_id"] or "main"))
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"❌ revoke DB error: {e}")
            return await i.followup.send(tr("db_err", lang), ephemeral=True)

    data = json.loads(row["data"])
    custom_tmpl = await get_custom_template(i.guild.id, row["type"])
    text = render(log_id, row["type"], row["author_id"], data, row["result"], None, "revoked",
                  reviewer_id=i.user.id, reason=reason, custom_template=custom_tmpl)
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
    msg = f"✅ Successfully revoked Log #{log_id}." if lang != "ar" else f"✅ تم إلغاء اللوج #{log_id} وسحب النقاط بنجاح."
    if warnings:
        msg += ("\n⚠️ These members now hold a rank above their points: "
                + " ".join(warnings) + "\nUse `/sync-ranks demote:True` to fix.")
    await i.followup.send(msg, ephemeral=True)


@bot.tree.command(name="pending", description="List logs waiting for review | اللوجات المنتظرة")
async def pending(i: discord.Interaction):
    if not await check_cmd_perm(i, "pending"):
        return await i.response.send_message(tr("no_role", await user_lang(i.guild.id, i.user.id)), ephemeral=True)
    lang = await user_lang(i.guild.id, i.user.id)
    if not (is_editor(i.user) or await can_review_any(i.user)):
        return await i.response.send_message(tr("no_perm", lang), ephemeral=True)
    async with aiosqlite.connect(DB) as db:
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
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())


# ================================================================ الإعدادات
@bot.tree.command(name="setchannel", description="Set channels | تحديد القنوات")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_CHANNEL)
async def setchannel(i: discord.Interaction, log_type: app_commands.Choice[str], kind: app_commands.Choice[str],
                     channel: Optional[discord.TextChannel] = None):
    if not await gate(i, "setchannel", editor=True):
        return
    t = "all" if kind.value in ("audit_channel", "update_channel") else log_type.value
    await cfg_set(i.guild.id, t, kind.value, channel.id if channel else None)
    warn = ""
    if channel:
        perms = channel.permissions_for(i.guild.me)
        missing = [n for n, ok in (("Send Messages", perms.send_messages), ("Attach Files", perms.attach_files),
                                   ("Embed Links", perms.embed_links)) if not ok]
        if missing:
            warn = "\n⚠️ Missing bot permissions in this channel: " + ", ".join(missing)
    await audit(f"⚙️ {i.user.mention} set {kind.value} ({t}) to {channel.mention if channel else 'none'}", i.guild)
    await i.response.send_message(f"✅ **{kind.name}** ({t}) = {channel.mention if channel else 'Cleared'}{warn}",
                                  ephemeral=True)


@bot.tree.command(name="setping", description="Set ping roles | تحديد رتب المنشن")
@app_commands.choices(log_type=LOG_TYPE_CHOICES, kind=KIND_PING)
async def setping(i: discord.Interaction, log_type: app_commands.Choice[str], kind: app_commands.Choice[str],
                  role: Optional[discord.Role] = None):
    if not await gate(i, "setping", editor=True):
        return
    await cfg_set(i.guild.id, log_type.value, kind.value, role.id if role else None)
    await audit(f"⚙️ {i.user.mention} set {kind.value} ({log_type.value}) to {role.mention if role else 'none'}", i.guild)
    await i.response.send_message(f"✅ **{kind.name}** ({log_type.name}) = {role.mention if role else 'Cleared'}",
                                  ephemeral=True)


@bot.tree.command(name="setroles", description="Set role permissions | صلاحيات الرتب")
@app_commands.choices(kind=KIND_ROLES, log_type=LOG_TYPE_CHOICES, action=ACTIONS)
async def setroles(i: discord.Interaction, kind: app_commands.Choice[str], log_type: app_commands.Choice[str],
                   action: app_commands.Choice[str], role: discord.Role):
    if not await gate(i, "setroles", editor=True):
        return
    await roles_edit(i.guild.id, log_type.value, kind.value, role.id, action.value == "add")
    await audit(f"⚙️ {i.user.mention} {action.value} {role.mention} → {kind.value} ({log_type.value})", i.guild)
    await i.response.send_message(f"✅ {action.name}: {role.mention} → **{kind.name}** ({log_type.name}).", ephemeral=True)


@bot.tree.command(name="setrule", description="Change a server rule | تغيير قاعدة في السيرفر")
@app_commands.choices(rule_name=RULE_CHOICES)
async def setrule(i: discord.Interaction, rule_name: app_commands.Choice[str], value: app_commands.Range[int, 0, 1000]):
    if not await gate(i, "setrule", editor=True):
        return
    await set_setting(i.guild.id, "rule:" + rule_name.value, value)
    await audit(f"⚙️ {i.user.mention} updated rule `{rule_name.value}` to `{value}`", i.guild)
    await i.response.send_message(f"✅ **{rule_name.value}** = {value}", ephemeral=True)


@bot.tree.command(name="settings", description="Show server settings & rules | عرض الإعدادات والقواعد")
async def settings_cmd(i: discord.Interaction):
    if not await gate(i, "settings", editor=True):
        return
    g = i.guild.id
    lang = await user_lang(g, i.user.id)
    flag = LANGS.get(lang, ("", "🌐"))[1]
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT cmd_name, role_id FROM cmd_roles WHERE guild_id=?", (g,))
        cmd_rows = await cur.fetchall()
    embed = discord.Embed(title=f"{flag} ⚙️ Server Settings — {BOT_VERSION}", color=discord.Color.blue(), timestamp=now_utc())
    audit_ch = await cfg_get(g, "all", "audit_channel")
    embed.add_field(name="📍 Audit Channel", value=f"<#{audit_ch}>" if audit_ch else "Not set", inline=False)
    upd_ch = await cfg_get(g, "all", "update_channel")
    embed.add_field(name="📢 Update Channel", value=f"<#{upd_ch}>" if upd_ch else "Not set", inline=False)
    ch_text = ""
    for t in LOG_TYPES:
        rv = await cfg_get(g, t, "review_channel")
        lg = await cfg_get(g, t, "log_channel")
        rp = await cfg_get(g, t, "review_ping")
        lp = await cfg_get(g, t, "log_ping")
        rv_roles = await roles_get(g, t, "review")
        ch_text += (f"**{LOG_TYPES[t]['label']}**\n"
                    f"Review: {f'<#{rv}>' if rv else 'Not set'}{f' | Ping: <@&{rp}>' if rp else ''}\n"
                    f"Log: {f'<#{lg}>' if lg else 'Not set'}{f' | Ping: <@&{lp}>' if lp else ''}\n"
                    f"Reviewers: {' '.join(f'<@&{r}>' for r in rv_roles) if rv_roles else 'default'}\n")
    embed.add_field(name="📺 Channels, pings & reviewers", value=ch_text[:1000] or "None", inline=False)
    rule_lines = [f"• `{k}`: {await rule(g, k)}" for k in RULE_LABELS]
    embed.add_field(name="📜 Rules", value="\n".join(rule_lines)[:1000], inline=False)
    if cmd_rows:
        embed.add_field(name="🔒 Command role locks",
                        value="\n".join(f"• `/{c}` → <@&{r}>" for c, r in cmd_rows)[:1000], inline=False)
    await i.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="sync", description="Sync commands | مزامنة الأوامر")
async def sync_cmd(i: discord.Interaction):
    if not await gate(i, "sync", editor=True):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    await i.response.defer(ephemeral=True)
    ok = await bot.sync_guild(i.guild)
    await i.followup.send(tr("sync_ok", lang) if ok else tr("sync_fail", lang))


async def cmd_name_autocomplete(i: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return [app_commands.Choice(name=c, value=c) for c in ALL_BOT_COMMANDS if current.lower() in c.lower()][:25]


@bot.tree.command(name="set-cmd-role", description="Lock a command to a role | تحديد رتبة لاستخدام أمر")
@app_commands.autocomplete(command=cmd_name_autocomplete)
@app_commands.describe(command="Select command", role="Required role (empty = reset)")
async def set_cmd_role(i: discord.Interaction, command: str, role: Optional[discord.Role] = None):
    if not await gate(i, "set-cmd-role", editor=True):
        return
    if command not in ALL_BOT_COMMANDS:
        return await i.response.send_message("❌ Invalid command name.", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        if role:
            await db.execute(
                "INSERT INTO cmd_roles(guild_id, cmd_name, role_id) VALUES(?,?,?) "
                "ON CONFLICT(guild_id, cmd_name) DO UPDATE SET role_id=excluded.role_id", (i.guild.id, command, role.id))
            msg = f"🔒 `/{command}` is now locked to {role.mention}."
        else:
            await db.execute("DELETE FROM cmd_roles WHERE guild_id=? AND cmd_name=?", (i.guild.id, command))
            msg = f"🔓 `/{command}` is open to default permissions."
        await db.commit()
    await save_config_snapshot_json()
    await audit(f"⚙️ {i.user.mention} updated command permission for `/{command}`", i.guild)
    await i.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="set-update-ping", description="Set role pinged on updates | رتبة منشن التحديثات")
async def set_update_ping(i: discord.Interaction, role: Optional[discord.Role] = None):
    if not await gate(i, "set-update-ping", editor=True):
        return
    await cfg_set(i.guild.id, "all", "update_ping", role.id if role else None)
    await audit(f"⚙️ {i.user.mention} set update ping role to {role.mention if role else 'None'}", i.guild)
    await i.response.send_message(f"✅ Update ping role set to {role.mention}." if role else "🗑️ Update ping role cleared.",
                                  ephemeral=True)


@bot.tree.command(name="set-log-template", description="Customize a log layout | تخصيص شكل اللوج")
@app_commands.choices(log_type=LOG_TYPE_CHOICES[1:])
@app_commands.describe(log_type="Log type",
                       template="Use {author} {score} {my_fighter} {opponent} {my_team} {their_team} {crew} {notes} (empty = reset)")
async def set_log_template(i: discord.Interaction, log_type: app_commands.Choice[str],
                           template: Optional[app_commands.Range[str, 1, 1200]] = None):
    if not await gate(i, "set-log-template", editor=True):
        return
    key = f"template:{log_type.value}"
    if template:
        await set_setting(i.guild.id, key, template)
        await audit(f"🎨 {i.user.mention} updated the log template for **{log_type.name}**", i.guild)
        await i.response.send_message(f"✅ Template updated for **{log_type.name}**.", ephemeral=True)
    else:
        await db_exec("DELETE FROM settings WHERE guild_id=? AND key=?", (i.guild.id, key))
        await save_config_snapshot_json()
        await audit(f"🗑️ {i.user.mention} reset the log template for **{log_type.name}**", i.guild)
        await i.response.send_message(f"🔄 Template for **{log_type.name}** reset to default.", ephemeral=True)


# ================================================================ النقط
@bot.tree.command(name="set-log-points", description="Set log points for this server | نقاط اللوجات")
@app_commands.choices(log=POINT_CHOICES)
async def set_log_points(i: discord.Interaction, log: app_commands.Choice[str], points: app_commands.Range[int, 0, 1000]):
    if not await gate(i, "set-log-points", editor=True):
        return
    await set_setting(i.guild.id, "points:" + log.value, points)
    await audit(f"⚙️ {i.user.mention} set log points for **{log.name}** to **{points}**", i.guild)
    await i.response.send_message(f"✅ **{log.name}** = {points} points (new logs only).", ephemeral=True)


@bot.tree.command(name="set-rank-points", description="Set rank threshold | نقاط رتب السلم")
@app_commands.choices(rank=RANK_CHOICES)
async def set_rank_points(i: discord.Interaction, rank: app_commands.Choice[str],
                          points: app_commands.Range[int, 0, 100000]):
    if not await gate(i, "set-rank-points", editor=True):
        return
    ladder = await get_ladder(i.guild.id)
    idx = next(k for k, (n, _) in enumerate(ladder) if n == rank.value)
    prev_pts = ladder[idx - 1][1] if idx > 0 else 0
    next_pts = ladder[idx + 1][1] if idx < len(ladder) - 1 else None
    if (idx > 0 and points <= prev_pts) or (next_pts is not None and points >= next_pts):
        return await i.response.send_message(
            f"❌ Number must be > {prev_pts} and < {next_pts if next_pts is not None else '∞'}.",
            ephemeral=True)
    await i.response.defer(ephemeral=True)
    await set_setting(i.guild.id, "rank:" + rank.value, points)
    changed = await bulk_update_ranks(i.guild)
    await audit(f"⚙️ {i.user.mention} set rank **{rank.name}** to **{points}** ({changed} promotions)", i.guild)
    await i.followup.send(f"✅ **{rank.name}** = {points} points. Promotions applied: {changed}.", ephemeral=True)


@bot.tree.command(name="set-points", description="Set member points | تحديد نقاط عضو")
async def set_points(i: discord.Interaction, member: discord.Member, points: app_commands.Range[int, 0, 100000]):
    if not await gate(i, "set-points", editor=True):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    await i.response.defer(ephemeral=True)
    season = await get_current_season(i.guild.id)
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
            row = await cur.fetchone()
            delta = points - (row[0] if row else 0)
            await db.execute(
                "INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET points=excluded.points", (i.guild.id, member.id, points))
            if delta != 0:
                await db.execute(
                    "INSERT INTO point_events(guild_id, log_id, user_id, points, created_at, type, season_id) "
                    "VALUES(?,?,?,?,?,?,?)", (i.guild.id, None, member.id, delta, now_utc().isoformat(), "manual", season))
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"❌ set-points DB error: {e}")
            return await i.followup.send(tr("db_err", lang), ephemeral=True)
    await update_member_rank(i.guild, member, points)
    await audit(f"⚙️ {i.user.mention} set points of {member.mention} to **{points}** (Δ {delta:+d})", i.guild)
    note = ""
    if await rank_is_above_points(i.guild, member, points):
        note = "\n⚠️ Member holds a rank above their points. Use `/sync-ranks demote:True` to adjust."
    await i.followup.send(f"✅ {member.display_name} = {points} points.{note}", ephemeral=True)


async def ledger_mismatches(guild_id: int):
    season = await get_current_season(guild_id)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT user_id, SUM(points) FROM point_events WHERE guild_id=? AND season_id=? GROUP BY user_id",
            (guild_id, season))
        ledger = {u: p for u, p in await cur.fetchall()}
        cur = await db.execute("SELECT user_id, points FROM members WHERE guild_id=?", (guild_id,))
        cached = {u: p for u, p in await cur.fetchall()}
    return [(u, cached.get(u, 0), ledger.get(u, 0)) for u in sorted(set(ledger) | set(cached))
            if cached.get(u, 0) != ledger.get(u, 0)]


@bot.tree.command(name="verify-points", description="Check balances against the points ledger | فحص الرصيد مقابل السجل")
@app_commands.describe(fix="Overwrite balances with the ledger totals")
async def verify_points(i: discord.Interaction, fix: bool = False):
    if not await gate(i, "verify-points", editor=True):
        return
    await i.response.defer(ephemeral=True)
    bad = await ledger_mismatches(i.guild.id)
    if not bad:
        return await i.followup.send("✅ All balances match the points ledger.", ephemeral=True)
    if fix:
        async with aiosqlite.connect(DB) as db:
            for uid, _, led in bad:
                await db.execute(
                    "INSERT INTO members(guild_id, user_id, points) VALUES(?,?,?) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET points=excluded.points", (i.guild.id, uid, led))
            await db.commit()
        await audit(f"🛠️ {i.user.mention} repaired {len(bad)} balances from the ledger", i.guild)
    lines = [f"<@{u}>: cached {c} / ledger {l}" for u, c, l in bad[:15]]
    await i.followup.send(f"{'🛠️ Fixed' if fix else '⚠️ Found'} {len(bad)} mismatch(es):\n" + "\n".join(lines),
                          ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="sync-ranks", description="Re-check ranks from points | إعادة فحص الرتب حسب النقاط")
@app_commands.describe(member="One member (empty = everyone)", demote="Also REMOVE ranks above their points")
async def sync_ranks(i: discord.Interaction, member: Optional[discord.Member] = None, demote: bool = False):
    if not await gate(i, "sync-ranks", editor=True):
        return
    await i.response.defer(ephemeral=True)
    if member:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
            row = await cur.fetchone()
        changed = 1 if await update_member_rank(i.guild, member, row[0] if row else 0, demote) else 0
    else:
        changed = await bulk_update_ranks(i.guild, allow_demote=demote)
    await audit(f"⚙️ {i.user.mention} ran sync-ranks (demote={demote}), changes: {changed}", i.guild)
    await i.followup.send(f"✅ Done. Rank changes: {changed}.", ephemeral=True)


@bot.tree.command(name="points", description="Check points | رصيد النقاط")
async def points_cmd(i: discord.Interaction, member: Optional[discord.Member] = None):
    if not await gate(i, "points"):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    flag = LANGS.get(lang, ("", "🌐"))[1]
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        row = await cur.fetchone()
    total = row[0] if row else 0
    msg = f"{flag} " + tr("points_msg", lang, member=member.mention, total=total) + f"\n`{progress_bar(total, await get_ladder(i.guild.id))}`"
    await i.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="leaderboard", description="Top members (All time or Weekly) | المتصدرين")
@app_commands.choices(period=PERIOD_CHOICES)
async def leaderboard(i: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
    if not await gate(i, "leaderboard"):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    flag = LANGS.get(lang, ("", "🌐"))[1]
    p_val = period.value if period else "all"
    season = await get_current_season(i.guild.id)
    async with aiosqlite.connect(DB) as db:
        if p_val == "week":
            cur = await db.execute(
                "SELECT user_id, SUM(points) AS total FROM point_events WHERE guild_id=? AND season_id=? "
                "AND created_at >= ? GROUP BY user_id HAVING total>0 ORDER BY total DESC LIMIT 10",
                (i.guild.id, season, (now_utc() - timedelta(days=7)).isoformat()))
        else:
            cur = await db.execute(
                "SELECT user_id, points FROM members WHERE guild_id=? AND points>0 ORDER BY points DESC LIMIT 10",
                (i.guild.id,))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message(tr("no_points_yet", lang), ephemeral=True)
    title = tr("lb_week", lang) if p_val == "week" else tr("lb_season", lang)
    text = f"{flag} **{title}**\n\n" + "\n".join(f"**{n}.** <@{u}> — {p} pts" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


async def points_for_key(guild_id, key):
    v = await get_setting(guild_id, "points:" + key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return DEFAULT_POINTS.get(key, 0)


@bot.tree.command(name="rates", description="Show log points, ranks & rules | أسعار النقاط والرتب")
async def rates(i: discord.Interaction):
    if not await gate(i, "rates"):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    flag = LANGS.get(lang, ("", "🌐"))[1]
    lines = [f"{flag} ⚙️ **Rates ({BOT_VERSION})**\n\n**Log points:**"]
    for k, label in POINT_LABELS.items():
        lines.append(f"- {label}: **{await points_for_key(i.guild.id, k)}** pts")
    lines.append("\n**Rank thresholds:**")
    lines += [f"- {n}: **{p}** pts" for n, p in await get_ladder(i.guild.id)]
    lines.append("\n**Rules:**")
    lines += [f"- {k}: {await rule(i.guild.id, k)}" for k in RULE_LABELS]
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@bot.tree.command(name="link", description="Link Roblox username | ربط حساب روبلوكس")
async def link(i: discord.Interaction, roblox_username: app_commands.Range[str, 3, 30]):
    if not await gate(i, "link"):
        return
    await i.response.defer(ephemeral=True)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post("https://users.roblox.com/v1/usernames/users",
                              json={"usernames": [roblox_username.strip()], "excludeBanned": False}) as r:
                if r.status != 200:
                    return await i.followup.send("❌ Error connecting to Roblox API.")
                data = await r.json()
        u = (data.get("data") or [None])[0]
        if not u:
            return await i.followup.send("❌ No Roblox user found with that name.")
        await db_exec(
            "INSERT INTO links(guild_id, user_id, roblox_id, roblox_name) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET roblox_id=excluded.roblox_id, roblox_name=excluded.roblox_name",
            (i.guild.id, i.user.id, u["id"], u["name"]))
        await i.followup.send(f"✅ Linked to **{u['name']}**.")
    except Exception:
        await i.followup.send("❌ Couldn't reach Roblox right now. Try again later.")


@bot.tree.command(name="profile", description="Member profile card | البطاقة الشخصية")
async def profile(i: discord.Interaction, member: Optional[discord.Member] = None):
    if not await gate(i, "profile"):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    flag = LANGS.get(lang, ("", "🌐"))[1]
    member = member or i.user
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        row = await cur.fetchone()
        cur = await db.execute(
            "SELECT l.type, l.result, COUNT(*) FROM point_events e JOIN logs l ON l.id=e.log_id "
            "WHERE e.guild_id=? AND e.user_id=? AND e.type='log' AND l.status='accepted' "
            "GROUP BY l.type, l.result", (i.guild.id, member.id))
        stat_rows = await cur.fetchall()
        cur = await db.execute("SELECT roblox_name FROM links WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        link_row = await cur.fetchone()
    total = row[0] if row else 0
    agg = {(t, res): c for t, res, c in stat_rows}
    rank_name = "—"
    for role in ladder_roles(i.guild):
        if role and role in member.roles:
            rank_name = role.name
    nxt = next(((n, p) for n, p in await get_ladder(i.guild.id) if p > total), None)

    def wl(t):
        return f"{agg.get((t, 'win'), 0)}W - {agg.get((t, 'loss'), 0)}L"

    e = discord.Embed(title=f"{flag} {member.display_name}", color=discord.Color.blurple())
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Points", value=str(total))
    e.add_field(name="Rank", value=rank_name)
    e.add_field(name="Next rank", value=f"{nxt[0]} ({nxt[1] - total} left)" if nxt else "Max auto rank")
    e.add_field(name="Progress", value=f"`{progress_bar(total, await get_ladder(i.guild.id))}`", inline=False)
    e.add_field(name="⚔️ Wars", value=wl("war"))
    e.add_field(name="🥊 1v1", value=wl("1v1"))
    e.add_field(name="🤝 2v2", value=wl("2v2"))
    e.add_field(name="🏋️ Trainings", value=str(sum(c for (t, _), c in agg.items() if t == "training")))
    e.add_field(name="🎯 Tryouts done", value=str(sum(c for (t, _), c in agg.items() if t == "tryout")))
    if link_row:
        e.add_field(name="Roblox", value=link_row[0])
    await i.response.send_message(embed=e)


@bot.tree.command(name="stats", description="War stats against crews | إحصائيات الحروب")
@app_commands.describe(crew="Filter by enemy crew name")
async def stats(i: discord.Interaction, crew: Optional[str] = None):
    if not await gate(i, "stats"):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    flag = LANGS.get(lang, ("", "🌐"))[1]
    season = await get_current_season(i.guild.id)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT data, result FROM logs WHERE guild_id=? AND type='war' AND status='accepted' AND season_id=?",
            (i.guild.id, season))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("ℹ️ No accepted wars logged in the current season yet.")
    target = crew.strip().lower() if crew else None
    per_crew = {}
    wins = losses = 0
    for r_data, r_res in rows:
        try:
            d = json.loads(r_data)
        except ValueError:
            d = {}
        name = str(d.get("crew", "?")).strip()
        if target and target not in name.lower():
            continue
        a = per_crew.setdefault(name.lower(), {"name": name, "w": 0, "l": 0})
        if r_res == "win":
            wins += 1
            a["w"] += 1
        elif r_res == "loss":
            losses += 1
            a["l"] += 1
    total = wins + losses
    if not total:
        return await i.response.send_message("ℹ️ No wars found for that crew.")
    msg = f"{flag} 📊 **War Statistics**" + (f" against **{crew}**" if crew else "") + "\n"
    msg += f"• **Total:** {total} | **Wins:** {wins} ✅ | **Losses:** {losses} ❌ | **Win rate:** {wins / total * 100:.1f}%\n"
    if not target:
        top = sorted(per_crew.values(), key=lambda a: a["w"] + a["l"], reverse=True)[:5]
        msg += "\n**Most fought crews:**\n" + "\n".join(f"- {a['name']}: {a['w']}W - {a['l']}L" for a in top)
    await i.response.send_message(msg[:1990])


class InactivePaginator(discord.ui.View):
    def __init__(self, items, days, owner_user_id):
        super().__init__(timeout=120)
        self.items, self.days, self.page, self.per_page = items, days, 0, 15
        self.owner_user_id = owner_user_id

    def max_pages(self):
        return (len(self.items) - 1) // self.per_page + 1

    def get_page_embed(self):
        chunk = self.items[self.page * self.per_page:(self.page + 1) * self.per_page]
        embed = discord.Embed(title=f"⚠️ Inactive Members ({self.days}+ days)",
                              description=f"Total: **{len(self.items)}**", color=discord.Color.orange())
        embed.add_field(name=f"Page {self.page + 1} of {self.max_pages()}",
                        value="\n".join(f"• <@{uid}> — {last}" for uid, last in chunk) or "None", inline=False)
        return embed

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.owner_user_id

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="⬅️")
    async def prev_page(self, i: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await i.response.edit_message(embed=self.get_page_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="➡️")
    async def next_page(self, i: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_pages() - 1, self.page + 1)
        await i.response.edit_message(embed=self.get_page_embed(), view=self)


@bot.tree.command(name="inactive", description="Crew members with no activity for N days | الأعضاء غير النشطين")
async def inactive(i: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30):
    if not await gate(i, "inactive", editor=True):
        return
    await i.response.defer(ephemeral=True)
    if not i.guild.chunked:
        await i.guild.chunk()
    cutoff = (now_utc() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT e.user_id, MAX(e.created_at) FROM point_events e JOIN logs l ON l.id=e.log_id "
            "WHERE e.guild_id=? AND e.type='log' AND l.status='accepted' GROUP BY e.user_id",
            (i.guild.id,))
        last = {u: t for u, t in await cur.fetchall()}
    crew_role = find_crew_role(i.guild)
    lroles = [r for r in ladder_roles(i.guild) if r]
    out = []
    for m in i.guild.members:
        if m.bot or not ((crew_role and crew_role in m.roles) or any(r in m.roles for r in lroles)):
            continue
        t = last.get(m.id)
        if t is None or t < cutoff:
            out.append((t or "", m.id))
    out.sort()
    if not out:
        return await i.followup.send(f"✅ No inactive members ({days}+ days).", ephemeral=True)
    items = [(uid, t[:10] if t else "never") for t, uid in out]
    view = InactivePaginator(items, days, i.user.id)
    await i.followup.send(embed=view.get_page_embed(), view=view, ephemeral=True)


@bot.tree.command(name="season-reset", description="Archive points & start a new season | بدء موسم جديد")
@app_commands.describe(name="Season name", confirm="Type RESET", reset_ranks="Also remove all Lvl roles")
async def season_reset(i: discord.Interaction, name: app_commands.Range[str, 1, 50], confirm: str,
                       reset_ranks: bool = False):
    if not await gate(i, "season-reset", editor=True):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    if confirm != "RESET":
        return await i.response.send_message("❌ Type RESET (capital letters) in `confirm` to proceed.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    name = name.strip()
    new_season_id = f"season_{uuid.uuid4().hex[:6]}"
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT 1 FROM season_archive WHERE season=? AND guild_id=? LIMIT 1", (name, i.guild.id))
            if await cur.fetchone():
                await db.rollback()
                return await i.followup.send(f"❌ Season name **{name}** already exists.", ephemeral=True)
            await db.execute(
                "INSERT INTO season_archive SELECT ?, guild_id, user_id, points FROM members WHERE guild_id=? AND points>0",
                (name, i.guild.id))
            await db.execute("UPDATE members SET points=0 WHERE guild_id=?", (i.guild.id,))
            await db.execute("UPDATE logs SET status='rejected', reason='Season ended before review', reviewed_at=? "
                             "WHERE guild_id=? AND status='pending'", (now_utc().isoformat(), i.guild.id))
            await db.execute(
                "INSERT INTO settings(guild_id, key, value) VALUES(?, 'state:current_season', ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (i.guild.id, new_season_id))
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"❌ season reset DB error: {e}")
            return await i.followup.send(tr("db_err", lang), ephemeral=True)

    stripped = 0
    if reset_ranks:
        if not i.guild.chunked:
            await i.guild.chunk()
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
    await i.followup.send(f"✅ Season **{name}** started. Points archived and reset to 0."
                          + (f" Ranks removed from {stripped} members." if reset_ranks else ""), ephemeral=True)


@bot.tree.command(name="season-top", description="Top 10 of an archived season | متصدري موسم سابق")
async def season_top(i: discord.Interaction, name: app_commands.Range[str, 1, 50]):
    if not await gate(i, "season-top"):
        return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT user_id, points FROM season_archive WHERE season=? AND guild_id=? ORDER BY points DESC LIMIT 10",
            (name.strip(), i.guild.id))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message("ℹ️ Season archive not found.", ephemeral=True)
    text = f"🏆 **Archived Season: {name}**\n" + "\n".join(f"**{n}.** <@{u}> — {p}" for n, (u, p) in enumerate(rows, 1))
    await i.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


# ================================================================ الشرح والتحديثات (مترجم بالكامل + العلم)
HELP_DATA = {
    "ar": [
        ("📜 القسم 1 — تسجيل النشاط (اللوجات)", discord.Color.gold(), [
            ("/log-war", "تسجيل حرب كرو (ملفين على الأقل). منشن زملاءك لتأكيد المشاركة."),
            ("/log-1v1", "تسجيل فايت 1 ضد 1."),
            ("/log-2v2", "تسجيل فايت 2 ضد 2 مع زميل."),
            ("/log-training", "تسجيل تدريب داخلي."),
            ("/log-tryout", "تسجيل تراي أوت (منشن المرشح)."),
            ("/resubmit", "تعديل وإعادة إرسال لوج مرفوض بتاعك."),
            ("/my-logs", "عرض آخر لوجاتك وحالتها."),
            ("/log-info", "تفاصيل لوج محدد برقمه."),
        ]),
        ("🛡️ القسم 2 — المراجعة والإدارة", discord.Color.blue(), [
            ("/pending", "عرض اللوجات المنتظرة المراجعة."),
            ("/revoke-log", "إلغاء لوج مقبول وسحب نقطه."),
            ("/reviewer-stats", "إحصائيات وسرعة المراجعين."),
            ("/inactive", "عرض الأعضاء غير النشطين من N يوم."),
            ("/season-reset", "أرشفة النقاط وبدء موسم جديد."),
            ("/season-top", "أفضل 10 في موسم مؤرشف."),
            ("/verify-points", "فحص ومطابقة الأرصدة مع سجل النقاط."),
            ("/backup", "أخذ نسخة احتياطية (نشاط السيرفر أو الداتابيز كاملة للمالك)."),
            ("/restore-backup", "استعادة الإعدادات (.json) أو الداتابيز (.db)."),
        ]),
        ("⚙️ القسم 3 — الإعدادات والتحكم", discord.Color.green(), [
            ("/setchannel", "تحديد قنوات المراجعة واللوج والتدقيق والتحديثات."),
            ("/setping", "تحديد رتب المنشن في المراجعة واللوج."),
            ("/setroles", "تحديد رتب المراجعين والمسجلين."),
            ("/setrule", "تعديل قوانين السيرفر (الحد اليومي، التذكيرات، الباكاب...)."),
            ("/set-log-points", "تعديل نقاط كل نوع لوج."),
            ("/set-rank-points", "تعديل النقاط المطلوبة لكل رتبة."),
            ("/set-points", "تعديل رصيد عضو يدوياً."),
            ("/sync-ranks", "إعادة فحص الرتب حسب النقاط."),
            ("/set-log-template", "تخصيص تصميم اللوج."),
            ("/set-cmd-role", "قفل أمر معين على رتبة محددة."),
            ("/create-new-crew", "إنشاء طاقم جديد بقياداته وأقسامه Z/Y/X/S/A."),
            ("/crews-delete-all", "حذف كل الطواقم المسجلة في السيرفر."),
            ("/settings", "عرض الإعدادات الحالية."),
            ("/sync", "مزامنة أوامر السلاش."),
            ("/send-help-panel", "نشر لوحة الدليل في قناة عامة."),
        ]),
        ("🏆 القسم 4 — النقاط والأوامر العامة", discord.Color.purple(), [
            ("/points", "عرض نقاطك وشريط تقدم الرتبة."),
            ("/leaderboard", "عرض المتصدرين (الموسم أو الأسبوع)."),
            ("/profile", "عرض بطاقتك الشخصية وإحصائياتك."),
            ("/rates", "عرض أسعار النقاط والرتب والقواعد."),
            ("/link", "ربط حساب روبلوكس الخاص بك."),
            ("/stats", "سجل انتصارات وهزائم الحروب ضد الكروات."),
            ("/crews-list", "استعراض الطواقم وأقسامها."),
            ("/language", "اختيار لغتك المفضلة في البوت."),
            ("/help", "عرض دليل الأوامر ده بلغتك."),
        ]),
    ],
    "en": [
        ("📜 Part 1 — Logging Activity", discord.Color.gold(), [
            ("/log-war", "Log a Crew War (min 2 files). Tag teammates to confirm."),
            ("/log-1v1", "Log a 1v1 fight."),
            ("/log-2v2", "Log a 2v2 fight with a teammate."),
            ("/log-training", "Log a training session."),
            ("/log-tryout", "Log a tryout (mention the candidate)."),
            ("/resubmit", "Fix and resend one of your rejected logs."),
            ("/my-logs", "View your latest logs and their status."),
            ("/log-info", "View details of one log by ID."),
        ]),
        ("🛡️ Part 2 — Review & Moderation", discord.Color.blue(), [
            ("/pending", "List logs waiting for review."),
            ("/revoke-log", "Cancel an accepted log and revoke its points."),
            ("/reviewer-stats", "See how many logs each reviewer handled and speed."),
            ("/inactive", "Crew members with no activity for N days."),
            ("/season-reset", "Archive points and start a new season."),
            ("/season-top", "Top 10 of an archived season."),
            ("/verify-points", "Check balances against the points ledger."),
            ("/backup", "Export backup (guild activity or full DB for owner)."),
            ("/restore-backup", "Restore settings (.json) or database (.db)."),
        ]),
        ("⚙️ Part 3 — Configuration", discord.Color.green(), [
            ("/setchannel", "Set review / log / audit / update channels."),
            ("/setping", "Set roles to ping on review and public logs."),
            ("/setroles", "Set who can review and submit each log type."),
            ("/setrule", "Configure daily limits, reminders, backups, weekly post..."),
            ("/set-log-points", "Set points awarded per log type."),
            ("/set-rank-points", "Set points required for each rank."),
            ("/set-points", "Manually set a member's point balance."),
            ("/sync-ranks", "Re-check ranks based on points."),
            ("/set-log-template", "Customize log layout."),
            ("/set-cmd-role", "Lock a command to a specific role."),
            ("/create-new-crew", "Create a crew with leaders and divisions Z/Y/X/S/A."),
            ("/crews-delete-all", "Delete all registered crews in this server."),
            ("/settings", "Show current server settings."),
            ("/sync", "Re-sync slash commands."),
            ("/send-help-panel", "Post this manual in a channel."),
        ]),
        ("🏆 Part 4 — Points & Public", discord.Color.purple(), [
            ("/points", "Check your points and rank progress bar."),
            ("/leaderboard", "Top members (all time or this week)."),
            ("/profile", "View member profile card."),
            ("/rates", "View points per log, ranks, and rules."),
            ("/link", "Link your Roblox username."),
            ("/stats", "War record against other crews."),
            ("/crews-list", "Browse crews and their divisions."),
            ("/language", "Choose your preferred language."),
            ("/help", "Show this manual in your language."),
        ]),
    ],
    "de": [
        ("📜 Teil 1 — Aktivitäten protokollieren", discord.Color.gold(), [
            ("/log-war", "Crew War protokollieren (min. 2 Dateien)."),
            ("/log-1v1", "1v1-Kampf protokollieren."),
            ("/log-2v2", "2v2-Kampf protokollieren."),
            ("/log-training", "Trainingseinheit protokollieren."),
            ("/log-tryout", "Tryout protokollieren."),
            ("/resubmit", "Abgelehntes Protokoll korrigieren und erneut senden."),
            ("/my-logs", "Deine letzten Protokolle anzeigen."),
            ("/log-info", "Details eines Protokolls nach ID."),
        ]),
        ("🛡️ Teil 2 — Überprüfung & Moderation", discord.Color.blue(), [
            ("/pending", "Ausstehende Protokolle anzeigen."),
            ("/revoke-log", "Akzeptiertes Protokoll widerrufen."),
            ("/reviewer-stats", "Aktivität und Geschwindigkeit der Prüfer."),
            ("/inactive", "Inaktive Mitglieder seit N Tagen."),
            ("/season-reset", "Punkte archivieren und neue Saison starten."),
            ("/season-top", "Top 10 einer archivierten Saison."),
            ("/verify-points", "Kontostände mit dem Punktekonto abgleichen."),
            ("/backup", "Backup erstellen."),
            ("/restore-backup", "Backup wiederherstellen."),
        ]),
        ("⚙️ Teil 3 — Konfiguration", discord.Color.green(), [
            ("/setchannel", "Kanäle festlegen."),
            ("/setping", "Ping-Rollen festlegen."),
            ("/setroles", "Rollenberechtigungen festlegen."),
            ("/setrule", "Serverregeln ändern."),
            ("/set-log-points", "Punkte pro Protokolltyp festlegen."),
            ("/set-rank-points", "Punkte für Ränge festlegen."),
            ("/set-points", "Punkte eines Mitglieds setzen."),
            ("/sync-ranks", "Ränge synchronisieren."),
            ("/create-new-crew", "Neue Crew erstellen."),
            ("/settings", "Einstellungen anzeigen."),
            ("/sync", "Befehle synchronisieren."),
        ]),
        ("🏆 Teil 4 — Punkte & Öffentlich", discord.Color.purple(), [
            ("/points", "Deine Punkte und Rangfortschritt."),
            ("/leaderboard", "Bestenliste anzeigen."),
            ("/profile", "Profilkarte anzeigen."),
            ("/rates", "Punkte, Ränge & Regeln anzeigen."),
            ("/link", "Roblox-Benutzernamen verknüpfen."),
            ("/stats", "Kriegsstatistik gegen Crews."),
            ("/crews-list", "Crews durchsuchen."),
            ("/language", "Bevorzugte Sprache wählen."),
            ("/help", "Dieses Handbuch anzeigen."),
        ]),
    ],
    "it": [
        ("📜 Parte 1 — Registrazione Attività", discord.Color.gold(), [
            ("/log-war", "Registra una Crew War (min 2 file)."),
            ("/log-1v1", "Registra uno scontro 1v1."),
            ("/log-2v2", "Registra uno scontro 2v2."),
            ("/log-training", "Registra una sessione di allenamento."),
            ("/log-tryout", "Registra un tryout."),
            ("/resubmit", "Correggi e reinvia un log rifiutato."),
            ("/my-logs", "I tuoi ultimi log e il loro stato."),
            ("/log-info", "Dettagli di un log tramite ID."),
        ]),
        ("🛡️ Parte 2 — Revisione & Moderazione", discord.Color.blue(), [
            ("/pending", "Log in attesa di revisione."),
            ("/revoke-log", "Annulla un log accettato e revoca i punti."),
            ("/reviewer-stats", "Statistiche e velocità dei revisori."),
            ("/inactive", "Membri inattivi da N giorni."),
            ("/season-reset", "Archivia punti e inizia nuova stagione."),
            ("/season-top", "Top 10 di una stagione archiviata."),
            ("/verify-points", "Verifica saldi con il registro punti."),
            ("/backup", "Esporta backup."),
            ("/restore-backup", "Ripristina backup."),
        ]),
        ("⚙️ Parte 3 — Configurazione", discord.Color.green(), [
            ("/setchannel", "Imposta canali."),
            ("/setping", "Imposta ruoli da menzionare."),
            ("/setroles", "Imposta permessi ruoli."),
            ("/setrule", "Modifica regole del server."),
            ("/set-log-points", "Imposta punti per tipo di log."),
            ("/set-rank-points", "Imposta punti per grado."),
            ("/set-points", "Imposta saldo punti di un membro."),
            ("/sync-ranks", "Sincronizza gradi."),
            ("/create-new-crew", "Crea un nuovo crew."),
            ("/settings", "Mostra impostazioni."),
            ("/sync", "Sincronizza comandi."),
        ]),
        ("🏆 Parte 4 — Punti & Pubblico", discord.Color.purple(), [
            ("/points", "I tuoi punti e progresso grado."),
            ("/leaderboard", "Classifica migliori membri."),
            ("/profile", "Scheda profilo membro."),
            ("/rates", "Punti, gradi e regole."),
            ("/link", "Collega nome utente Roblox."),
            ("/stats", "Statistiche guerre contro crew."),
            ("/crews-list", "Sfoglia i crew."),
            ("/language", "Scegli la tua lingua preferita."),
            ("/help", "Mostra questo manuale."),
        ]),
    ],
    "hi": [
        ("📜 भाग 1 — गतिविधि लॉग करना", discord.Color.gold(), [
            ("/log-war", "क्रू वॉर लॉग करें (कम से कम 2 फ़ाइलें)।"),
            ("/log-1v1", "1v1 फाइट लॉग करें।"),
            ("/log-2v2", "2v2 फाइट लॉग करें।"),
            ("/log-training", "प्रशिक्षण सत्र लॉग करें।"),
            ("/log-tryout", "ट्रायआउट लॉग करें।"),
            ("/resubmit", "अस्वीकृत लॉग को सुधारें और पुनः भेजें।"),
            ("/my-logs", "आपके नवीनतम लॉग और उनकी स्थिति।"),
            ("/log-info", "ID द्वारा एक लॉग का विवरण।"),
        ]),
        ("🛡️ भाग 2 — समीक्षा और मॉडरेशन", discord.Color.blue(), [
            ("/pending", "समीक्षा की प्रतीक्षा कर रहे लॉग।"),
            ("/revoke-log", "स्वीकृत लॉग रद्द करें और अंक वापस लें।"),
            ("/reviewer-stats", "समीक्षकों की गतिविधि और गति।"),
            ("/inactive", "N दिनों से निष्क्रिय सदस्य।"),
            ("/season-reset", "अंक संग्रहीत करें और नया सीज़न शुरू करें।"),
            ("/season-top", "संग्रहीत सीज़न के शीर्ष 10।"),
            ("/verify-points", "अंक बहीखाते के विरुद्ध शेष राशि की जाँच करें।"),
            ("/backup", "बैकअप निर्यात करें।"),
            ("/restore-backup", "बैकअप पुनर्स्थापित करें।"),
        ]),
        ("⚙️ भाग 3 — कॉन्फ़िगरेशन", discord.Color.green(), [
            ("/setchannel", "चैनल सेट करें।"),
            ("/setping", "पिंग भूमिकाएँ सेट करें।"),
            ("/setroles", "भूमिका अनुमतियाँ सेट करें।"),
            ("/setrule", "सर्वर नियम बदलें।"),
            ("/set-log-points", "प्रति लॉग प्रकार अंक सेट करें।"),
            ("/set-rank-points", "प्रत्येक रैंक के लिए आवश्यक अंक सेट करें।"),
            ("/set-points", "सदस्य के अंक मैन्युअल रूप से सेट करें।"),
            ("/sync-ranks", "रैंक सिंक करें।"),
            ("/create-new-crew", "नया क्रू बनाएँ।"),
            ("/settings", "वर्तमान सेटिंग्स दिखाएँ।"),
            ("/sync", "स्लैश कमांड सिंक करें।"),
        ]),
        ("🏆 भाग 4 — अंक और सार्वजनिक", discord.Color.purple(), [
            ("/points", "आपके अंक और रैंक प्रगति।"),
            ("/leaderboard", "शीर्ष सदस्य लीडरबोर्ड।"),
            ("/profile", "सदस्य प्रोफ़ाइल कार्ड।"),
            ("/rates", "अंक दरें, रैंक और नियम।"),
            ("/link", "अपना Roblox उपयोगकर्ता नाम लिंक करें।"),
            ("/stats", "अन्य क्रू के खिलाफ युद्ध रिकॉर्ड।"),
            ("/crews-list", "क्रू और उनके विभाग ब्राउज़ करें।"),
            ("/language", "अपनी पसंदीदा भाषा चुनें।"),
            ("/help", "यह मैनुअल दिखाएँ।"),
        ]),
    ],
}


def get_help_embeds(lang: str):
    sections = HELP_DATA.get(lang) or HELP_DATA["en"]
    lang_name, flag = LANGS.get(lang, ("English", "🇬🇧"))
    embeds = []
    for title, color, items in sections:
        e = discord.Embed(title=f"{flag} {title}  ({BOT_VERSION})", color=color)
        for cmd, desc in items:
            e.add_field(name=f"`{cmd}`", value=f"{flag} {desc}", inline=False)
        e.set_footer(text=f"{flag} Language: {lang_name} • DragonSlayerBot")
        embeds.append(e)
    return embeds


async def post_update_notice(guild, target, preset_key="bot_patch", replace_previous=True):
    data = UPDATE_PRESETS[preset_key]
    ping_role_id = await cfg_get(guild.id, "all", "update_ping")
    embed = discord.Embed(title=f"📢 {data['title_en']} | {data['title_ar']}", color=discord.Color.gold(),
                          timestamp=now_utc())
    embed.add_field(name="🇬🇧 English", value=data["desc_en"][:1000], inline=False)
    embed.add_field(name="🇸🇦 بالعربي", value=data["desc_ar"][:1000], inline=False)
    embed.set_footer(text=f"DragonSlayerBot {BOT_VERSION}")
    sent = await target.send(content=f"<@&{ping_role_id}>" if ping_role_id else None, embed=embed,
                             allowed_mentions=mentions(ping_role_id))
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
    ch_id = await cfg_get(guild.id, "all", "update_channel")
    ch = guild.get_channel(ch_id) if ch_id else None
    if not ch:
        return
    try:
        await post_update_notice(guild, ch)
        await set_setting(guild.id, "state:bot_version", BOT_VERSION)
    except discord.HTTPException:
        pass


@bot.tree.command(name="help", description="Show the bot manual | عرض دليل الأوامر")
async def help_cmd(i: discord.Interaction):
    if not await gate(i, "help"):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    await i.response.defer(ephemeral=True)
    for e in get_help_embeds(lang):
        await i.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="send-help-panel", description="Post the manual in a channel | إرسال الشرح في قناة")
async def send_help_panel(i: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not await gate(i, "send-help-panel", editor=True):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    target = channel or i.channel
    try:
        for e in get_help_embeds(lang):
            await target.send(embed=e)
        await i.response.send_message(f"✅ Posted the manual to {target.mention}.", ephemeral=True)
    except (discord.HTTPException, AttributeError) as e:
        await i.response.send_message(f"❌ Failed to send the manual: {e}", ephemeral=True)


@bot.tree.command(name="log-update", description="Publish an update notice | نشر إشعار تحديث")
@app_commands.choices(preset=UPDATE_TYPE_CHOICES)
@app_commands.describe(preset="Update type", channel="Target channel", replace_previous="Delete previous notice")
async def log_update(i: discord.Interaction, preset: app_commands.Choice[str],
                     channel: Optional[discord.TextChannel] = None, replace_previous: bool = True):
    if not await gate(i, "log-update", editor=True):
        return
    target = channel
    if not target:
        ch_id = await cfg_get(i.guild.id, "all", "update_channel") or await cfg_get(i.guild.id, "all", "log_channel")
        target = i.guild.get_channel(ch_id) if ch_id else None
    if preset.value not in UPDATE_PRESETS or not target:
        return await i.response.send_message("❌ Set an update channel first with `/setchannel`.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    try:
        await post_update_notice(i.guild, target, preset.value, replace_previous)
        await audit(f"📢 {i.user.mention} posted update **{preset.name}** in {target.mention}", i.guild)
        await i.followup.send(f"✅ Update published to {target.mention}.", ephemeral=True)
    except discord.HTTPException as e:
        await i.followup.send(f"❌ Failed to send the update: {e}", ephemeral=True)


# ================================================================ باكاب واستعادة النسخ الاحتياطية
def _backup_file(path):
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


@bot.tree.command(name="backup", description="Secure backup tool | أداة النسخ الاحتياطي الآمنة")
@app_commands.choices(scope=BACKUP_SCOPE_CHOICES, destination=BACKUP_DEST_CHOICES)
@app_commands.describe(scope="Backup scope", destination="Where to send the file")
async def backup_cmd(i: discord.Interaction, scope: app_commands.Choice[str], destination: app_commands.Choice[str]):
    if not await gate(i, "backup", editor=True):
        return
    if scope.value == "full":
        if not owner_id() or i.user.id != owner_id():
            return await i.response.send_message("❌ Full database backups are for the bot owner only.", ephemeral=True)
        if destination.value != "owner":
            return await i.response.send_message("❌ Full database backups can only be sent to the owner's DM.", ephemeral=True)
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
            payload = json.dumps({"version": BOT_VERSION, "guild_id": i.guild.id, "date": today, "logs": logs_today,
                                  "point_events": events_today, "exported_at": now_utc().isoformat()},
                                 indent=2, ensure_ascii=False).encode("utf-8")

        file_obj = discord.File(io.BytesIO(payload), filename=filename)
        if destination.value == "owner":
            ok = await dm_owner(f"🗄️ Backup (`{scope.name}`) from **{i.guild.name}** requested by {i.user.mention}:", file=file_obj)
            await audit(f"🗄️ {i.user.mention} exported a backup (`{scope.value}`) to the owner's DM.", i.guild)
            await i.followup.send("✅ Backup sent to the owner's DM." if ok else "❌ Couldn't DM the owner.", ephemeral=True)
        else:
            await audit(f"🗄️ {i.user.mention} exported a guild backup (`{scope.value}`) in chat.", i.guild)
            await i.followup.send(f"🗄️ Backup ({scope.name}):", file=file_obj, ephemeral=True)
    except Exception as e:
        print(f"❌ Backup error: {e}")
        await i.followup.send("❌ Backup failed.", ephemeral=True)
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
        raise ValueError("Not a valid JSON file.")
    if not isinstance(data, dict):
        raise ValueError("Unexpected file structure.")
    valid_types = set(LOG_TYPES) | {"all"}
    counts = {"settings": 0, "guild_cfg": 0, "role_perms": 0, "cmd_roles": 0, "skipped": 0}

    def mine(r):
        return isinstance(r, dict) and r.get("guild_id") == guild_id

    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for r in data.get("settings", []):
                k, v = (r.get("key"), r.get("value")) if isinstance(r, dict) else (None, None)
                if mine(r) and isinstance(k, str) and k.startswith(SETTING_PREFIXES) and len(k) <= 100 \
                        and v is not None and len(str(v)) <= 2000:
                    await db.execute("INSERT INTO settings(guild_id,key,value) VALUES(?,?,?) "
                                     "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value", (guild_id, k, str(v)))
                    counts["settings"] += 1
                else:
                    counts["skipped"] += 1
            for r in data.get("guild_cfg", []):
                if mine(r) and r.get("log_type") in valid_types and r.get("key") in CFG_KEYS and _is_int(r.get("value")):
                    await db.execute("INSERT INTO guild_cfg VALUES(?,?,?,?) ON CONFLICT(guild_id,log_type,key) "
                                     "DO UPDATE SET value=excluded.value", (guild_id, r["log_type"], r["key"], r["value"]))
                    counts["guild_cfg"] += 1
                else:
                    counts["skipped"] += 1
            for r in data.get("role_perms", []):
                if mine(r) and r.get("log_type") in valid_types and r.get("kind") in ("review", "submit") \
                        and _is_int(r.get("role_id")):
                    await db.execute("INSERT OR IGNORE INTO role_perms VALUES(?,?,?,?)",
                                     (guild_id, r["log_type"], r["kind"], r["role_id"]))
                    counts["role_perms"] += 1
                else:
                    counts["skipped"] += 1
            for r in data.get("cmd_roles", []):
                if mine(r) and r.get("cmd_name") in ALL_BOT_COMMANDS and _is_int(r.get("role_id")):
                    await db.execute("INSERT INTO cmd_roles VALUES(?,?,?) ON CONFLICT(guild_id,cmd_name) "
                                     "DO UPDATE SET role_id=excluded.role_id", (guild_id, r["cmd_name"], r["role_id"]))
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
    return ok and {"logs", "members"} <= tables


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
            raise ValueError("The file is not a valid DragonSlayerBot database.")
        await asyncio.to_thread(_backup_file, safety)
        await asyncio.to_thread(_restore_into_live, tmp)
        await init_db()
        return True
    finally:
        for path in (tmp, safety):
            if os.path.exists(path):
                os.unlink(path)


@bot.tree.command(name="restore-backup", description="Restore settings (.json) or database (.db) | استعادة نسخة احتياطية")
@app_commands.describe(backup_file="Backup file", confirm="Type RESTORE to restore a .db file")
async def restore_backup(i: discord.Interaction, backup_file: discord.Attachment, confirm: Optional[str] = None):
    if not await gate(i, "restore-backup", editor=True):
        return
    ext = os.path.splitext(backup_file.filename)[1].lower()
    if ext not in (".json", ".db"):
        return await i.response.send_message("❌ Only `.json` or `.db` files are accepted.", ephemeral=True)
    if ext == ".db":
        if not owner_id() or i.user.id != owner_id():
            return await i.response.send_message("❌ Restoring a database file is for the bot owner only.", ephemeral=True)
        if confirm != "RESTORE":
            return await i.response.send_message("⚠️ This REPLACES all data. Type `RESTORE` in `confirm` to proceed.", ephemeral=True)
    if backup_file.size > (2 * 1024 * 1024 if ext == ".json" else MAX_FILE_SIZE):
        return await i.response.send_message("❌ File is too large.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    raw = await backup_file.read()
    try:
        if ext == ".json":
            c = await restore_settings_json(i.guild.id, raw)
            await audit(f"📥 {i.user.mention} restored settings from a JSON backup ({c}).", i.guild)
            await i.followup.send(
                f"✅ Settings restored: {c['settings']} settings, {c['guild_cfg']} channels/pings, "
                f"{c['role_perms']} role permissions, {c['cmd_roles']} command locks. Skipped: {c['skipped']}.", ephemeral=True)
        else:
            await restore_database(raw)
            await audit(f"📥 {i.user.mention} restored the full database from a .db backup.", i.guild)
            await i.followup.send("✅ Database restored and migrated.", ephemeral=True)
    except ValueError as e:
        await i.followup.send(f"❌ {e}", ephemeral=True)
    except Exception as e:
        print(f"❌ Restore error: {e}")
        await i.followup.send("❌ Restore failed.", ephemeral=True)


# ================================================================ أوامر إضافية
@bot.tree.command(name="my-logs", description="Your recent logs & status | لوجاتك الأخيرة وحالتها")
async def my_logs(i: discord.Interaction):
    if not await gate(i, "my-logs"):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id, type, status, points, created_at, reason FROM logs WHERE guild_id=? AND author_id=? "
            "ORDER BY id DESC LIMIT 10", (i.guild.id, i.user.id))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message(tr("no_my_logs", lang), ephemeral=True)
    lines = []
    for lid, t, status, pts, created, reason in rows:
        line = f"**#{lid}** {LOG_TYPES.get(t, {}).get('label', t)} — {STATUS_LABEL.get(status, status)}"
        if status == "accepted":
            line += f" (+{pts})"
        if status == "rejected" and reason:
            line += f" — {clip(reason, 60)} → `/resubmit log_id:{lid}`"
        lines.append(f"{line} — {str(created)[:10]}")
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@bot.tree.command(name="log-info", description="Details of a log | تفاصيل لوج")
async def log_info(i: discord.Interaction, log_id: int):
    if not await gate(i, "log-info"):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    row = await get_log(log_id)
    if not row or row["guild_id"] != i.guild.id:
        return await i.response.send_message(tr("log_not_found", lang), ephemeral=True)
    if row["author_id"] != i.user.id and not await can_review(i.user, row["type"]):
        return await i.response.send_message(tr("no_perm", lang), ephemeral=True)
    e = discord.Embed(title=f"Log #{log_id} — {LOG_TYPES.get(row['type'], {}).get('label', row['type'])}",
                      color=discord.Color.blurple())
    e.add_field(name="Status", value=STATUS_LABEL.get(row["status"], row["status"]))
    e.add_field(name="Author", value=f"<@{row['author_id']}>")
    e.add_field(name="Points", value=str(row["points"] or 0))
    e.add_field(name="Season", value=str(row["season_id"] or "main"))
    e.add_field(name="Created", value=str(row["created_at"])[:16])
    if row["reviewer_id"]:
        e.add_field(name="Reviewer", value=f"<@{row['reviewer_id']}>")
    if row["reason"]:
        e.add_field(name="Reason", value=clip(row["reason"], 300), inline=False)
    if row["parent_id"]:
        e.add_field(name="Resubmission of", value=f"#{row['parent_id']}")
    if row["review_channel"] and row["review_msg"]:
        e.add_field(name="Review message",
                    value=f"https://discord.com/channels/{i.guild.id}/{row['review_channel']}/{row['review_msg']}",
                    inline=False)
    await i.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="reviewer-stats", description="Reviewer activity & speed | نشاط وسرعة المراجعين")
async def reviewer_stats(i: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30):
    lang = await user_lang(i.guild.id, i.user.id)
    if not await check_cmd_perm(i, "reviewer-stats"):
        return await i.response.send_message(tr("no_role", lang), ephemeral=True)
    if not (is_editor(i.user) or await can_review_any(i.user)):
        return await i.response.send_message(tr("no_perm", lang), ephemeral=True)
    since = (now_utc() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT reviewer_id, SUM(status='accepted'), SUM(status='rejected'), COUNT(*), "
            "AVG((julianday(reviewed_at) - julianday(created_at)) * 24) FROM logs "
            "WHERE guild_id=? AND reviewer_id IS NOT NULL AND reviewed_at>=? AND status IN ('accepted','rejected') "
            "GROUP BY reviewer_id ORDER BY COUNT(*) DESC LIMIT 10", (i.guild.id, since))
        rows = await cur.fetchall()
    if not rows:
        return await i.response.send_message(f"ℹ️ No reviews in the last {days} days.", ephemeral=True)
    lines = [f"**Reviewer activity — last {days} days**"]
    for uid, acc, rej, total, hrs in rows:
        lines.append(f"<@{uid}> — {total} reviews (✅ {acc or 0} / ❌ {rej or 0}) — avg {hrs or 0:.1f}h")
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())


# ================================================================ أمر اللغة (Per-User Language)
class LanguageSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=name, value=code, emoji=flag) for code, (name, flag) in LANGS.items()]
        super().__init__(placeholder="🌐 Choose your language / اختر لغتك المفضلة", min_values=1, max_values=1,
                         options=options)

    async def callback(self, i: discord.Interaction):
        lang = self.values[0]
        if lang not in LANGS:
            return await i.response.send_message(tr("invalid_lang", DEFAULT_LANG), ephemeral=True)
        await set_setting(i.guild.id, f"state:lang:{i.user.id}", lang)
        flag = LANGS[lang][1]
        await i.response.send_message(f"{flag} " + tr("lang_changed", lang), ephemeral=True)


class LanguageView(discord.ui.View):
    def __init__(self, requester_id):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.add_item(LanguageSelect())

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.requester_id


@bot.tree.command(name="language", description="Choose your language | اختيار لغتك المفضلة")
async def language_cmd(i: discord.Interaction):
    if not await gate(i, "language"):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    flag = LANGS.get(lang, ("", "🌐"))[1]
    await i.response.send_message(f"{flag} " + tr("language_prompt", lang),
                                  view=LanguageView(i.user.id), ephemeral=True)


# ================================================================ الطواقم الكاملة (Crews System)
CREW_NAME_RE = re.compile(r"[^\n\r@<>`]{2,40}")
MAX_CREWS_PER_GUILD = 100
CREW_TEAMS = [("team_z_data", "Team Z"), ("team_y_data", "Team Y"), ("team_x_data", "Team X"),
              ("team_s_data", "Team S"), ("team_a_data", "Team A")]


def _id_list(raw):
    try:
        v = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [x for x in v if isinstance(x, int) and not isinstance(x, bool)] if isinstance(v, list) else []


def _mentions_or(ids, empty_text):
    if not ids:
        return empty_text
    lines = []
    for n, member_id in enumerate(ids, 1):
        line = f"**{n}.** <@{member_id}>"
        candidate = "\n".join(lines + [line])
        if len(candidate) > 900:
            remaining = len(ids) - len(lines)
            suffix = f"\n…and {remaining} more" if remaining > 0 else ""
            return "\n".join(lines) + suffix
        lines.append(line)
    return "\n".join(lines)


class CrewSelect(discord.ui.Select):
    def __init__(self, browser):
        self.browser = browser
        options = [discord.SelectOption(label=n[:100], value=n, emoji="🛡️") for n in browser.page_names]
        super().__init__(placeholder=tr("select_placeholder", browser.lang)[:150],
                         min_values=1, max_values=1, options=options)

    async def callback(self, i: discord.Interaction):
        name = self.values[0]
        lang = await user_lang(i.guild.id, i.user.id)
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT owner_id, captains_data, team_z_data, team_y_data, team_x_data, team_s_data, team_a_data, "
                "created_at, team_z_commanders_data, team_y_commanders_data, team_x_commanders_data, "
                "team_s_commanders_data, team_a_commanders_data FROM crews WHERE guild_id=? AND crew_name=?",
                (i.guild.id, name))
            row = await cur.fetchone()
        if not row:
            return await i.response.send_message(tr("crew_not_found", lang), ephemeral=True)
        owner, cap, *rest = row
        teams, created_at, commanders = rest[:5], rest[5], rest[6:11]
        none, empty = tr("none", lang), tr("empty", lang)
        flag = LANGS.get(lang, ("", "🌐"))[1]
        e = discord.Embed(
            title=f"{flag} 🏰 {clip(name, 70)}",
            description="━━━━━━━━━━━━━━━━━━\n🎖️ **Crew Leadership & Teams**\n━━━━━━━━━━━━━━━━━━",
            color=discord.Color.from_rgb(184, 139, 48), timestamp=now_utc())
        e.add_field(name="👑 Owner", value=f"<@{owner}>" if owner else "—", inline=True)
        e.add_field(name="⚔️ Captains", value=_mentions_or(_id_list(cap), none), inline=True)
        for (col, label), raw, commander_raw in zip(CREW_TEAMS, teams, commanders):
            members = _id_list(raw)
            leaders = _id_list(commander_raw)
            e.add_field(name=f"🛡️ {label} · Players ({len(members)})",
                        value=_mentions_or(members, empty), inline=True)
            e.add_field(name=f"🎖️ {label} · {tr('team_commanders', lang)} ({len(leaders)})",
                        value=_mentions_or(leaders, none), inline=True)
        e.set_footer(text=f"📅 {tr('founded', lang)}: {str(created_at)[:10]}  •  DragonSlayer Crew System")
        await i.response.send_message(embed=e, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class CrewBrowserView(discord.ui.View):
    def __init__(self, names, lang, requester_id, page=0):
        super().__init__(timeout=180)
        self.names = names
        self.lang = lang
        self.requester_id = requester_id
        self.page = page
        self.page_size = 25
        self.page_names = self.names[self.page * self.page_size:(self.page + 1) * self.page_size]
        self.add_item(CrewSelect(self))
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = (self.page + 1) * self.page_size >= len(self.names)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.requester_id

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_button(self, i: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._refresh(i)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, i: discord.Interaction, button: discord.ui.Button):
        max_page = max(0, (len(self.names) - 1) // self.page_size)
        self.page = min(max_page, self.page + 1)
        await self._refresh(i)

    async def _refresh(self, i: discord.Interaction):
        self.clear_items()
        self.page_names = self.names[self.page * self.page_size:(self.page + 1) * self.page_size]
        self.add_item(CrewSelect(self))
        self.add_item(self.previous_button)
        self.add_item(self.next_button)
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = (self.page + 1) * self.page_size >= len(self.names)
        pages = max(1, (len(self.names) + self.page_size - 1) // self.page_size)
        await i.response.edit_message(content=tr("crews_prompt", self.lang) + "\n" +
                                       tr("browse_page", self.lang, page=self.page + 1, pages=pages), view=self)


def parse_member_tokens(raw: Optional[str]):
    if not raw or not raw.strip():
        return []
    mention_ids = re.findall(r"<@!?(\d+)>", raw)
    remainder = re.sub(r"<@!?\d+>", " ", raw)
    tokens = re.split(r"[\s,;|]+", remainder.strip())
    ids = []
    for value in [*mention_ids, *tokens]:
        value = value.strip()
        if not value:
            continue
        if not value.isdigit():
            raise ValueError("Use member mentions or numeric Discord IDs")
        uid = int(value)
        if uid <= 0:
            raise ValueError("Invalid member ID")
        if uid not in ids:
            ids.append(uid)
    return ids


async def resolve_crew_members(guild: discord.Guild, raw: Optional[str]):
    ids = parse_member_tokens(raw)
    members = []
    for uid in ids:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                raise ValueError("member not found")
        if not member.bot:
            members.append(member)
    return members


@bot.tree.command(name="create-new-crew", description="Create a crew with multiple players and team commanders | إنشاء طاقم")
@app_commands.describe(
    crew_name="Crew name", owner_member="Crew owner",
    captains="Captain mentions/IDs",
    team_z_members="Team Z players", team_z_commanders="Team Z commanders",
    team_y_members="Team Y players", team_y_commanders="Team Y commanders",
    team_x_members="Team X players", team_x_commanders="Team X commanders",
    team_s_members="Team S players", team_s_commanders="Team S commanders",
    team_a_members="Team A players", team_a_commanders="Team A commanders")
async def create_new_crew(
        i: discord.Interaction,
        crew_name: app_commands.Range[str, 2, 40],
        owner_member: discord.Member,
        captains: Optional[app_commands.Range[str, 1, 1000]] = None,
        team_z_members: Optional[app_commands.Range[str, 1, 1000]] = None,
        team_z_commanders: Optional[app_commands.Range[str, 1, 1000]] = None,
        team_y_members: Optional[app_commands.Range[str, 1, 1000]] = None,
        team_y_commanders: Optional[app_commands.Range[str, 1, 1000]] = None,
        team_x_members: Optional[app_commands.Range[str, 1, 1000]] = None,
        team_x_commanders: Optional[app_commands.Range[str, 1, 1000]] = None,
        team_s_members: Optional[app_commands.Range[str, 1, 1000]] = None,
        team_s_commanders: Optional[app_commands.Range[str, 1, 1000]] = None,
        team_a_members: Optional[app_commands.Range[str, 1, 1000]] = None,
        team_a_commanders: Optional[app_commands.Range[str, 1, 1000]] = None):
    if not await gate(i, "create-new-crew", editor=True):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    crew_name = crew_name.strip()
    if not CREW_NAME_RE.fullmatch(crew_name):
        return await i.response.send_message(tr("crew_bad_name", lang), ephemeral=True)
    if owner_member.bot:
        return await i.response.send_message(tr("crew_bad_owner", lang), ephemeral=True)

    raw_groups = (captains, team_z_members, team_y_members, team_x_members, team_s_members, team_a_members,
                  team_z_commanders, team_y_commanders, team_x_commanders, team_s_commanders, team_a_commanders)
    try:
        groups = [await resolve_crew_members(i.guild, raw) for raw in raw_groups]
    except ValueError:
        return await i.response.send_message(tr("crew_bad_members", lang), ephemeral=True)
    lists = [[member.id for member in group] for group in groups]
    if any(len(group) > 100 for group in lists):
        return await i.response.send_message(tr("crew_bad_members", lang), ephemeral=True)

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT crew_name FROM crews WHERE guild_id=?", (i.guild.id,))
        existing = [r[0] for r in await cur.fetchall()]
        if crew_name.casefold() in {n.casefold() for n in existing}:
            return await i.response.send_message(tr("crew_exists", lang), ephemeral=True)
        if len(existing) >= MAX_CREWS_PER_GUILD:
            return await i.response.send_message(tr("crew_limit", lang, max=MAX_CREWS_PER_GUILD), ephemeral=True)
        try:
            await db.execute(
                "INSERT INTO crews(guild_id, crew_name, owner_id, captains_data, team_z_data, team_y_data, "
                "team_x_data, team_s_data, team_a_data, team_z_commanders_data, team_y_commanders_data, "
                "team_x_commanders_data, team_s_commanders_data, team_a_commanders_data, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (i.guild.id, crew_name, owner_member.id, *[json.dumps(x) for x in lists], now_utc().isoformat()))
            await db.commit()
        except sqlite3.IntegrityError:
            return await i.response.send_message(tr("crew_exists", lang), ephemeral=True)
        except sqlite3.OperationalError:
            return await i.response.send_message(tr("db_err", lang), ephemeral=True)

    await audit(f"🎖️ {i.user.mention} created crew **{crew_name}** (owner {owner_member.mention})", i.guild)
    await i.response.send_message(tr("crew_created", lang, name=crew_name), ephemeral=True)


class CrewPurgeConfirmView(discord.ui.View):
    def __init__(self, requester_id: int, guild_id: int):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.guild_id = guild_id

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.requester_id

    @discord.ui.button(label="Delete all crews", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT COUNT(*) FROM crews WHERE guild_id=?", (self.guild_id,))
            count = (await cur.fetchone())[0]
            await db.execute("DELETE FROM crews WHERE guild_id=?", (self.guild_id,))
            await db.commit()
        for child in self.children:
            child.disabled = True
        await i.response.edit_message(
            content=f"🗑️ Deleted {count} crews from this server." if count else "ℹ️ No crews saved to delete.",
            view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await i.response.edit_message(content="Deletion cancelled.", view=self)


@bot.tree.command(name="crews-delete-all", description="Delete all saved crews in this server | حذف كل الطواقم")
async def crews_delete_all(i: discord.Interaction):
    if not await gate(i, "crews-delete-all", editor=True):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT COUNT(*) FROM crews WHERE guild_id=?", (i.guild.id,))
        count = (await cur.fetchone())[0]
    if not count:
        return await i.response.send_message(tr("no_crews", lang), ephemeral=True)
    await i.response.send_message(
        f"⚠️ Final confirmation: all **{count}** saved crews in this server will be permanently deleted.",
        view=CrewPurgeConfirmView(i.user.id, i.guild.id), ephemeral=True)


@bot.tree.command(name="crews-list", description="Browse all crews from a dropdown | استعراض كل الطواقم")
async def crews_list(i: discord.Interaction):
    if not await gate(i, "crews-list"):
        return
    lang = await user_lang(i.guild.id, i.user.id)
    flag = LANGS.get(lang, ("", "🌐"))[1]
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT crew_name FROM crews WHERE guild_id=? ORDER BY crew_name COLLATE NOCASE", (i.guild.id,))
        names = [r[0] for r in await cur.fetchall()]
    if not names:
        return await i.response.send_message(tr("no_crews", lang), ephemeral=True)
    pages = max(1, (len(names) + 24) // 25)
    view = CrewBrowserView(names, lang, i.user.id)
    await i.response.send_message(f"{flag} " + tr("crews_prompt", lang) + "\n" +
                                  tr("browse_page", lang, page=1, pages=pages),
                                  view=view, ephemeral=True)


# ================================================================ مهام تلقائية
async def run_reminders():
    now = now_utc()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, type, guild_id, created_at, review_channel, review_msg FROM logs "
            "WHERE status='pending' AND COALESCE(reminded,0)=0 AND guild_id!=0")
        rows = await cur.fetchall()
    for l in rows:
        try:
            guild = bot.get_guild(l["guild_id"])
            if not guild:
                continue
            hours = await rule(guild.id, "reminder_hours")
            if hours <= 0:
                continue
            created = dt.datetime.fromisoformat(l["created_at"])
            if (now - created).total_seconds() < hours * 3600:
                continue
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
                except discord.HTTPException:
                    pass
            await db_exec("UPDATE logs SET reminded=1 WHERE id=?", (l["id"],))
        except Exception as e:
            print(f"⚠️ Reminder error for log {l['id']}: {e}")


async def run_backup():
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
            os.unlink(path)


async def run_weekly():
    now = now_utc()
    if now.weekday() != 6:
        return
    week_key = now.strftime("%Y-W%U")
    for guild in bot.guilds:
        try:
            if await rule(guild.id, "weekly_post") != 1:
                continue
            if await raw_setting(guild.id, "state:last_weekly_post") == week_key:
                continue
            ch_id = await cfg_get(guild.id, "all", "log_channel")
            ch = guild.get_channel(ch_id) if ch_id else None
            if not ch:
                continue
            season = await get_current_season(guild.id)
            async with aiosqlite.connect(DB) as db:
                cur = await db.execute(
                    "SELECT user_id, SUM(points) AS total FROM point_events WHERE guild_id=? AND season_id=? "
                    "AND created_at >= ? GROUP BY user_id HAVING total>0 ORDER BY total DESC LIMIT 10",
                    (guild.id, season, (now - timedelta(days=7)).isoformat()))
                rows = await cur.fetchall()
            if rows:
                text = f"🏆 **Weekly Top 10 ({week_key})**\n\n" + "\n".join(
                    f"**{n}.** <@{u}> — {p} pts" for n, (u, p) in enumerate(rows, 1))
                await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
            await set_setting(guild.id, "state:last_weekly_post", week_key)
        except Exception as e:
            print(f"⚠️ Weekly leaderboard error in guild {guild.id}: {e}")


@tasks.loop(minutes=30)
async def maintenance():
    for job in (run_reminders, run_backup, run_weekly):
        try:
            await job()
        except Exception as e:
            print(f"⚠️ Maintenance job {job.__name__} failed: {e}")


@maintenance.before_loop
async def before_maintenance():
    await bot.wait_until_ready()


if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("❌ DISCORD_TOKEN is missing in environment variables.")
    bot.run(token)