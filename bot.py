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

BOT_VERSION = "v3.1.0"
DB = os.getenv("DB_PATH", "crewbot.db")
JSON_CONFIG_PATH = os.getenv("JSON_CONFIG_PATH", "servers_settings.json")
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

# ---- حدود المرفقات ----
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".mkv"}
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_TOTAL_FILES_SIZE = 50 * 1024 * 1024
MAX_FILES_COUNT = 5
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}

# ================================================================ نظام اللغات الشامل 100%
SUPPORTED_LANGUAGES = {
    "ar": "العربية",
    "en": "English",
    "de": "Deutsch",
    "it": "Italiano",
    "hi": "हिन्दी"
}

# قاموس الترجمة الشامل لكافة أجزاء البوت
TRANSLATIONS = {
    "en": {
        "no_perm": "❌ You don't have permission to perform this action.",
        "no_role": "❌ You are missing the required role for this command.",
        "log_not_found": "❌ Log not found or invalid for this server.",
        "already_reviewed": "⚠️ This log has already been reviewed or closed.",
        "lang_updated": "✅ Your preferred language has been updated to **English**!",
        "lang_desc": "Choose your preferred language from the menu below. Every single response, menu, and help manual from the bot will be displayed in this language for you.",
        "help_title": "⚔️ DragonSlayerBot Help Manual",
        "help_part1": "🎮 **Log Submissions:**\n• `/log-war` - Log a Crew War battle (min 2 attachments).\n• `/log-1v1` - Log a 1v1 fight.\n• `/log-2v2` - Log a 2v2 fight.\n• `/log-training` - Log a training session.\n• `/log-tryout` - Log a tryout test.\n• `/resubmit` - Resubmit a rejected log.",
        "help_part2": "🛡️ **Review & Staff:**\n• `/pending` - View pending logs.\n• `/revoke-log` - Revoke an accepted log.\n• `/inactive` - View inactive members.\n• `/log-update` - Publish update notice.\n• `/backup` - Generate a backup.",
        "help_part3": "📊 **Public & Stats:**\n• `/points` - View total points.\n• `/leaderboard` - View top members.\n• `/profile` - View your member profile.\n• `/rates` - View point reward rates.\n• `/link` - Link your Roblox account.\n• `/stats` - View crew war statistics.",
        "status_pending": "⏳ Pending",
        "status_accepted": "✅ Accepted",
        "status_rejected": "❌ Rejected",
        "status_revoked": "🚫 Revoked",
        "result_win": "Win",
        "result_loss": "Loss",
        "result_passed": "Passed",
        "result_failed": "Failed",
        "lbl_logged_by": "Logged by",
        "lbl_fighter": "Fighter",
        "lbl_opponent": "Opponent",
        "lbl_details": "Details",
        "lbl_status": "Status",
        "lbl_points": "Points",
        "lbl_reviewed_by": "Reviewed by",
        "lbl_reason": "Reason",
        "lbl_warnings": "Warnings",
        "btn_accept": "Accept",
        "btn_reject": "Reject",
        "btn_confirm": "Confirm Participation",
        "profile_title": "👤 Member Profile",
        "profile_pts": "Total Points",
        "profile_rank": "Current Rank",
        "profile_logs": "Accepted Logs",
        "profile_roblox": "Roblox Account",
        "stats_title": "📊 Crew War Statistics",
        "stats_wins": "Wins",
        "stats_losses": "Losses",
        "stats_winrate": "Win Rate",
        "rates_title": "💰 Point Rates"
    },
    "ar": {
        "no_perm": "❌ ليس لديك الصلاحية المطلوبة لتنفيذ هذا الإجراء.",
        "no_role": "❌ تفتقر للرتبة المطلوبة لاستخدام هذا الأمر.",
        "log_not_found": "❌ السجل غير موجود أو غير صالح لهذا السيرفر.",
        "already_reviewed": "⚠️ تم مراجعة هذا السجل أو إغلاقه مسبقاً.",
        "lang_updated": "✅ تم تحديث لغتك المفضلة بنجاح إلى **العربية**!",
        "lang_desc": "اختر لغتك المفضلة من القائمة أدناه. سيتم عرض كافة الردود والواجهات ودليل الأوامر لك بهذه اللغة حصرياً.",
        "help_title": "⚔️ دليل أوامر DragonSlayerBot",
        "help_part1": "🎮 **تسجيل اللوجات:**\n• `/log-war` - تسجيل حرب كرو (إرفاق ملفين على الأقل).\n• `/log-1v1` - تسجيل قتال فردي 1 ضد 1.\n• `/log-2v2` - تسجيل قتال ثنائي 2 ضد 2.\n• `/log-training` - تسجيل تدريب للكرو.\n• `/log-tryout` - تسجيل اختبار تجربة أداء.\n• `/resubmit` - إعادة تقديم لوج مرفوض.",
        "help_part2": "🛡️ **المراجعة والإدارة:**\n• `/pending` - عرض اللوجات المعلقة.\n• `/revoke-log` - إلغاء لوج مقبول.\n• `/inactive` - كشف الأعضاء الخاملين.\n• `/log-update` - نشر إعلان تحديث.\n• `/backup` - إنتاج نسخة احتياطية.",
        "help_part3": "📊 **الأوامر العامة والإحصائيات:**\n• `/points` - عرض النقاط الإجمالية.\n• `/leaderboard` - لوحة المتصدرين.\n• `/profile` - عرض البروفايل الخاص بك.\n• `/rates` - أسعار ونقاط اللوجات.\n• `/link` - ربط حساب روبلوكس.\n• `/stats` - إحصائيات حروب الكرو.",
        "status_pending": "⏳ قيد المراجعة",
        "status_accepted": "✅ مقبول",
        "status_rejected": "❌ مرفوض",
        "status_revoked": "🚫 ملغي",
        "result_win": "فوز",
        "result_loss": "خسارة",
        "result_passed": "اجتاز",
        "result_failed": "راسب",
        "lbl_logged_by": "مسجّل بواسطة",
        "lbl_fighter": "مقاتلنا",
        "lbl_opponent": "الخصم",
        "lbl_details": "التفاصيل",
        "lbl_status": "الحالة",
        "lbl_points": "النقاط",
        "lbl_reviewed_by": "المراجع",
        "lbl_reason": "السبب",
        "lbl_warnings": "تحذيرات",
        "btn_accept": "قبول",
        "btn_reject": "رفض",
        "btn_confirm": "تأكيد المشاركة",
        "profile_title": "👤 بروفايل العضو",
        "profile_pts": "مجموع النقاط",
        "profile_rank": "الرتبة الحالية",
        "profile_logs": "اللوجات المقبولة",
        "profile_roblox": "حساب روبلوكس",
        "stats_title": "📊 إحصائيات حروب الكرو",
        "stats_wins": "انتصارات",
        "stats_losses": "هزائم",
        "stats_winrate": "نسبة الفوز",
        "rates_title": "💰 أسعار ونقاط اللوجات"
    },
    "de": {
        "no_perm": "❌ Sie haben keine Berechtigung für diese Aktion.",
        "no_role": "❌ Ihnen fehlt die erforderliche Rolle.",
        "log_not_found": "❌ Log nicht gefunden.",
        "already_reviewed": "⚠️ Dieser Log wurde bereits überprüft.",
        "lang_updated": "✅ Ihre Sprache wurde auf **Deutsch** aktualisiert!",
        "lang_desc": "Wählen Sie Ihre bevorzugte Sprache aus. Alle Antworten und Menüs des Bots werden in dieser Sprache angezeigt.",
        "help_title": "⚔️ DragonSlayerBot Hilfe-Handbuch",
        "help_part1": "🎮 **Log-Einreichungen:**\n• `/log-war` - Crew War registrieren.\n• `/log-1v1` - 1v1 Kampf registrieren.\n• `/log-2v2` - 2v2 Kampf registrieren.\n• `/log-training` - Training registrieren.\n• `/log-tryout` - Tryout registrieren.\n• `/resubmit` - Abgelehnten Log erneut senden.",
        "help_part2": "🛡️ **Prüfung & Verwaltung:**\n• `/pending` - Ausstehende Logs anzeigen.\n• `/revoke-log` - Log widerrufen.\n• `/inactive` - Inaktive Mitglieder anzeigen.\n• `/log-update` - Update ankündigen.\n• `/backup` - Backup erstellen.",
        "help_part3": "📊 **Statistiken & Allgemein:**\n• `/points` - Punkte anzeigen.\n• `/leaderboard` - Bestenliste anzeigen.\n• `/profile` - Profil anzeigen.\n• `/rates` - Punkteraten anzeigen.\n• `/link` - Roblox-Konto verknüpfen.\n• `/stats` - Kriegsstatistiken anzeigen.",
        "status_pending": "⏳ Ausstehend",
        "status_accepted": "✅ Angenommen",
        "status_rejected": "❌ Abgelehnt",
        "status_revoked": "🚫 Widerrufen",
        "result_win": "Sieg",
        "result_loss": "Niederlage",
        "result_passed": "Bestanden",
        "result_failed": "Nicht bestanden",
        "lbl_logged_by": "Registriert von",
        "lbl_fighter": "Kämpfer",
        "lbl_opponent": "Gegner",
        "lbl_details": "Details",
        "lbl_status": "Status",
        "lbl_points": "Punkte",
        "lbl_reviewed_by": "Geprüft von",
        "lbl_reason": "Grund",
        "lbl_warnings": "Warnungen",
        "btn_accept": "Akzeptieren",
        "btn_reject": "Ablehnen",
        "btn_confirm": "Teilnahme bestätigen",
        "profile_title": "👤 Mitgliedsprofil",
        "profile_pts": "Gesamtpunkte",
        "profile_rank": "Aktueller Rang",
        "profile_logs": "Akzeptierte Logs",
        "profile_roblox": "Roblox-Konto",
        "stats_title": "📊 Crew War Statistiken",
        "stats_wins": "Siege",
        "stats_losses": "Niederlagen",
        "stats_winrate": "Siegquote",
        "rates_title": "💰 Punkteraten"
    },
    "it": {
        "no_perm": "❌ Non hai i permessi per eseguire questa azione.",
        "no_role": "❌ Ti manca il ruolo richiesto.",
        "log_not_found": "❌ Log non trovato.",
        "already_reviewed": "⚠️ Questo log è già stato revisionato.",
        "lang_updated": "✅ La tua lingua è stata aggiornata in **Italiano**!",
        "lang_desc": "Scegli la tua lingua preferita. Tutte le risposte e i menu del bot verranno visualizzati in questa lingua.",
        "help_title": "⚔️ Manuale di Aiuto DragonSlayerBot",
        "help_part1": "🎮 **Invio Log:**\n• `/log-war` - Registra una Crew War.\n• `/log-1v1` - Registra un duello 1v1.\n• `/log-2v2` - Registra uno scontro 2v2.\n• `/log-training` - Registra allenamento.\n• `/log-tryout` - Registra un provino.\n• `/resubmit` - Invia di nuovo un log rifiutato.",
        "help_part2": "🛡️ **Revisione e Gestione:**\n• `/pending` - Visualizza i log in attesa.\n• `/revoke-log` - Revoca un log accettato.\n• `/inactive` - Visualizza membri inattivi.\n• `/log-update` - Pubblica un aggiornamento.\n• `/backup` - Genera un backup.",
        "help_part3": "📊 **Pubblico e Statistiche:**\n• `/points` - Visualizza punti totali.\n• `/leaderboard` - Classifica migliori membri.\n• `/profile` - Visualizza il tuo profilo.\n• `/rates` - Tassi di punti.\n• `/link` - Collega account Roblox.\n• `/stats` - Statistiche delle guerre.",
        "status_pending": "⏳ In attesa",
        "status_accepted": "✅ Accettato",
        "status_rejected": "❌ Rifiutato",
        "status_revoked": "🚫 Revocato",
        "result_win": "Vittoria",
        "result_loss": "Sconfitta",
        "result_passed": "Superato",
        "result_failed": "Fallito",
        "lbl_logged_by": "Registrato da",
        "lbl_fighter": "Combattente",
        "lbl_opponent": "Avversario",
        "lbl_details": "Dettagli",
        "lbl_status": "Stato",
        "lbl_points": "Punti",
        "lbl_reviewed_by": "Revisionato da",
        "lbl_reason": "Motivo",
        "lbl_warnings": "Avvisi",
        "btn_accept": "Accetta",
        "btn_reject": "Rifiuta",
        "btn_confirm": "Conferma partecipazione",
        "profile_title": "👤 Profilo Membro",
        "profile_pts": "Punti Totali",
        "profile_rank": "Grado Attuale",
        "profile_logs": "Log Accettati",
        "profile_roblox": "Account Roblox",
        "stats_title": "📊 Statistiche Crew War",
        "stats_wins": "Vittorie",
        "stats_losses": "Sconfitte",
        "stats_winrate": "Percentuale Vittorie",
        "rates_title": "💰 Tassi Punti"
    },
    "hi": {
        "no_perm": "❌ आपके पास इस कार्रवाई को करने की अनुमति नहीं है।",
        "no_role": "❌ आपके पास आवश्यक भूमिका नहीं है।",
        "log_not_found": "❌ लॉग नहीं मिला।",
        "already_reviewed": "⚠️ इस लॉग की समीक्षा पहले ही की जा चुकी है।",
        "lang_updated": "✅ आपकी भाषा को **हिन्दी** में अपडेट कर दिया गया है!",
        "lang_desc": "नीचे दिए गए मेनू से अपनी पसंदीदा भाषा चुनें। बोट के सभी उत्तर और मेनू इसी भाषा में दिखाई देंगे।",
        "help_title": "⚔️ DragonSlayerBot सहायता नियमावली",
        "help_part1": "🎮 **लॉग सबमिशन:**\n• `/log-war` - क्रू वॉर दर्ज करें।\n• `/log-1v1` - 1v1 फाइट दर्ज करें।\n• `/log-2v2` - 2v2 फाइट दर्ज करें।\n• `/log-training` - ट्रेनिंग सत्र दर्ज करें।\n• `/log-tryout` - ट्राईआउट दर्ज करें।\n• `/resubmit` - अस्वीकृत लॉग फिर से सबमिट करें।",
        "help_part2": "🛡️ **समीक्षा और प्रबंधन:**\n• `/pending` - लंबित लॉग देखें।\n• `/revoke-log` - स्वीकृत लॉग रद्द करें।\n• `/inactive` - निष्क्रिय सदस्यों को देखें।\n• `/log-update` - अपडेट नोटिस प्रकाशित करें।\n• `/backup` - बैकअप बनाएं।",
        "help_part3": "📊 **सार्वजनिक और आंकड़े:**\n• `/points` - कुल अंक देखें।\n• `/leaderboard` - लीडरबोर्ड देखें।\n• `/profile` - अपना प्रोफाइल देखें।\n• `/rates` - अंक दरें देखें।\n• `/link` - अपना रोबोक्स खाता जोड़ें।\n• `/stats` - युद्ध के आंकड़े देखें।",
        "status_pending": "⏳ लंबित",
        "status_accepted": "✅ स्वीकृत",
        "status_rejected": "❌ अस्वीकृत",
        "status_revoked": "🚫 रद्द किया गया",
        "result_win": "जीत",
        "result_loss": "हार",
        "result_passed": "उत्तीर्ण",
        "result_failed": "अनुत्तीर्ण",
        "lbl_logged_by": "द्वारा दर्ज किया गया",
        "lbl_fighter": "योद्धा",
        "lbl_opponent": "विरोधी",
        "lbl_details": "विवरण",
        "lbl_status": "स्थिति",
        "lbl_points": "अंक",
        "lbl_reviewed_by": "समीक्षक",
        "lbl_reason": "कारण",
        "lbl_warnings": "चेतावनी",
        "btn_accept": "स्वीकार करें",
        "btn_reject": "अस्वीकार करें",
        "btn_confirm": "भागीदारी की पुष्टि करें",
        "profile_title": "👤 सदस्य प्रोफ़ाइल",
        "profile_pts": "कुल अंक",
        "profile_rank": "वर्तमान रैंक",
        "profile_logs": "स्वीकृत लॉग",
        "profile_roblox": "रोबोक्स खाता",
        "stats_title": "📊 क्रू वॉर आंकड़े",
        "stats_wins": "जीत",
        "stats_losses": "हार",
        "stats_winrate": "जीत की दर",
        "rates_title": "💰 अंक दरें"
    }
}

def t(lang: str, key: str, **kwargs) -> str:
    lang_dict = TRANSLATIONS.get(lang, TRANSLATIONS["en"])
    text = lang_dict.get(key, TRANSLATIONS["en"].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text


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


# ================================================================ الإعدادات الأساسية
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

RESULT_CHOICES = [app_commands.Choice(name="Win", value="win"),
                  app_commands.Choice(name="Loss", value="loss")]
TRYOUT_CHOICES = [app_commands.Choice(name="Passed", value="win"),
                  app_commands.Choice(name="Failed", value="loss")]
LOG_TYPE_CHOICES = [app_commands.Choice(name="All types (default)", value="all")] + [
    app_commands.Choice(name=v["label"][:100], value=k) for k, v in LOG_TYPES.items()]
KIND_CHANNEL = [
    app_commands.Choice(name="Review channel", value="review_channel"),
    app_commands.Choice(name="Log channel", value="log_channel"),
    app_commands.Choice(name="Audit channel", value="audit_channel")]
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
        "label": f"🤖 Bot Hardening Patch {BOT_VERSION}",
        "title": f"DragonSlayerBot Release {BOT_VERSION}",
        "desc": ("• Complete per-user multi-language localization (AR, EN, DE, IT, HI).\n"
                 "• All menus, modals, embeds, and status labels reflect user choice.\n"
                 "• Clean QoL interface layout with zero text crowding.")
    }
}
UPDATE_TYPE_CHOICES = [app_commands.Choice(name=v["label"][:100], value=k) for k, v in UPDATE_PRESETS.items()]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    guild_id INTEGER, key TEXT, value TEXT,
    PRIMARY KEY(guild_id, key)
);
CREATE TABLE IF NOT EXISTS members(
    guild_id INTEGER, user_id INTEGER, points INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS user_languages(
    user_id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'en'
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
"""

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


async def get_user_lang(user_id: int) -> str:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT lang FROM user_languages WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    return row[0] if row else "en"


async def set_user_lang(user_id: int, lang: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO user_languages(user_id, lang) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang", (user_id, lang))
        await db.commit()


# ================================================================ استعادة الإعدادات التلقائية
async def auto_restore_from_json():
    if not os.path.exists(JSON_CONFIG_PATH):
        return
    try:
        def read_json():
            with open(JSON_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        
        data = await asyncio.to_thread(read_json)
        if not isinstance(data, dict):
            return

        async with aiosqlite.connect(DB) as db:
            for row in data.get("settings", []):
                await db.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?)", 
                                 (row.get("guild_id", 0), row.get("key"), str(row.get("value"))))
            for row in data.get("guild_cfg", []):
                await db.execute("INSERT OR REPLACE INTO guild_cfg VALUES(?,?,?,?)", 
                                 (row.get("guild_id", 0), row.get("log_type"), row.get("key"), row.get("value")))
            for row in data.get("role_perms", []):
                await db.execute("INSERT OR REPLACE INTO role_perms VALUES(?,?,?,?)", 
                                 (row.get("guild_id", 0), row.get("log_type"), row.get("kind"), row.get("role_id")))
            for row in data.get("cmd_roles", []):
                await db.execute("INSERT OR REPLACE INTO cmd_roles VALUES(?,?,?)", 
                                 (row.get("guild_id", 0), row.get("cmd_name"), row.get("role_id")))
            await db.commit()
    except Exception as e:
        print(f"⚠️ [Auto-Restore Error] Could not load JSON config snapshot: {e}")


# ================================================================ قاعدة البيانات
async def table_columns(db, table):
    cur = await db.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in await cur.fetchall()]


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=5000;")
        await db.executescript(SCHEMA)
        await db.executescript(INDEXES)
        await db.commit()
    
    await auto_restore_from_json()
    await save_config_snapshot_json()


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


# ================================================================ الصلاحيات
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
    lang = await get_user_lang(i.user.id)
    if not await check_cmd_perm(i, cmd_name):
        await i.response.send_message(t(lang, "no_role"), ephemeral=True)
        return False
    if editor and not is_editor(i.user):
        await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
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
    lang = await get_user_lang(i.user.id)
    msg = t(lang, "no_perm")
    try:
        if i.response.is_done():
            await i.followup.send(msg, ephemeral=True)
        else:
            await i.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ================================================================ إعداد البوت
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
            for g in self.guilds:
                await self.sync_guild(g)
        if not maintenance.is_running():
            maintenance.start()

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)


bot = CrewBot()


@bot.tree.error
async def on_app_command_error(i: discord.Interaction, error):
    await report_error(i, error)


# ================================================================ القوالب النظيفة والترجمة الكاملة (Rendering)
MSG_LIMIT = 1900


def clip(v, n=350):
    v = str(v) if v not in (None, "") else "-"
    return v if len(v) <= n else v[: n - 1] + "…"


async def get_custom_template(guild_id, log_type):
    t_val = await raw_setting(guild_id, f"template:{log_type}")
    return t_val or None


def apply_template(template: str, values: dict) -> str:
    return TEMPLATE_VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def render(log_id, log_type, author_id, data, result, flags=None, status="pending",
           points=None, reviewer_id=None, reason=None, pts_to=None, custom_template=None, lang="en"):
    label = LOG_TYPES.get(log_type, {}).get("label", log_type)
    d = lambda k, default="-": clip(data.get(k, default))  # noqa: E731
    participants = dedupe([int(x) for x in data.get("participants", [])]) if isinstance(data.get("participants"), list) else []
    
    res_text = t(lang, "result_passed") if log_type == "tryout" and result == "win" else t(lang, "result_failed") if log_type == "tryout" else t(lang, "result_win") if result == "win" else t(lang, "result_loss") if result == "loss" else "-"
    status_text = t(lang, f"status_{status}")

    values = {
        "log_id": log_id,
        "author": f"<@{author_id}>",
        "score": d("score"),
        "notes": d("notes"),
        "youtube_link": data.get("youtube_link", "-"),
        "my_team": d("my_team"),
        "their_team": d("their_team"),
        "crew": d("crew"),
        "my_fighter": d("my_fighter"),
        "opponent": d("opponent"),
        "participants": " ".join(f"<@{p}>" for p in participants) if participants else "-",
        "candidate": data.get("candidate", "-"),
        "roblox": d("roblox"),
        "p1": d("p1"),
        "p2": d("p2"),
        "result": res_text
    }

    if custom_template:
        body = apply_template(custom_template, values)
    else:
        header = f"🛡️ **{label}** — `🆔 Log #{log_id}`\n\n"
        m = header + f"👤 **{t(lang, 'lbl_logged_by')}:** <@{author_id}> `[ID: #{log_id}]`\n\n"
        if log_type == "1v1":
            m += f"⚔️ **{t(lang, 'lbl_fighter')}:** {d('my_fighter')}\n"
            m += f"🎯 **{t(lang, 'lbl_opponent')}:** {d('opponent')}\n\n"
        else:
            m += f"📋 **{t(lang, 'lbl_details')}:** {d('score')}\n"
        body = m

    footer = f"\n**{t(lang, 'lbl_status')}:** {status_text} | **Log ID:** `#{log_id}`"
    if points is not None:
        footer += f" | **{t(lang, 'lbl_points')}:** +{points}"
        if pts_to:
            footer += " → " + " ".join(f"<@{u}>" for u in list(pts_to)[:6])
    if reviewer_id and status != "pending":
        footer += f"\n🛡️ **{t(lang, 'lbl_reviewed_by')}:** <@{reviewer_id}>"
    if reason:
        footer += f"\n💬 **{t(lang, 'lbl_reason')}:** {clip(reason, 300)}"
    if flags:
        footer += f"\n⚠️ **{t(lang, 'lbl_warnings')}:**\n" + "\n".join(f"• " + clip(f, 120) for f in flags[:5])

    if len(body) + len(footer) > MSG_LIMIT:
        body = body[: max(200, MSG_LIMIT - len(footer) - 1)] + "…"
    return (body + footer)[:MSG_LIMIT + 100]


# ================================================================ أمر واجهة اختيار اللغة (`/language`)
class LanguageSelect(discord.ui.Select):
    def __init__(self, current_lang: str):
        options = [
            discord.SelectOption(label=name, value=code, default=(code == current_lang))
            for code, name in SUPPORTED_LANGUAGES.items()
        ]
        super().__init__(placeholder="Choose your language / اختر لغتك المفضلة...", min_values=1, max_values=1, options=options)

    async def callback(self, i: discord.Interaction):
        chosen_lang = self.values[0]
        await set_user_lang(i.user.id, chosen_lang)
        msg = t(chosen_lang, "lang_updated")
        await i.response.send_message(msg, ephemeral=True)


class LanguageView(discord.ui.View):
    def __init__(self, current_lang: str):
        super().__init__(timeout=60)
        self.add_item(LanguageSelect(current_lang))


@bot.tree.command(name="language", description="Choose your preferred language for all bot interactions")
async def language_cmd(i: discord.Interaction):
    lang = await get_user_lang(i.user.id)
    embed = discord.Embed(
        title="🌐 Language Settings / إعدادات اللغة",
        description=t(lang, "lang_desc"),
        color=discord.Color.blue()
    )
    await i.response.send_message(embed=embed, view=LanguageView(lang), ephemeral=True)


# ================================================================ الرتب (Ranks)
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
        return None

    to_add = [target_role] if target_role and target_role not in member.roles else []
    to_remove = [roles[idx] for idx in held if roles[idx] != target_role]
    try:
        if to_add:
            await member.add_roles(*to_add, reason="Rank update based on points")
        if to_remove:
            await member.remove_roles(*to_remove, reason="Rank update based on points")
    except Exception:
        return None
    return target_role


def target_index(ladder, total):
    target = -1
    for idx, (_, pts) in enumerate(ladder):
        if total >= pts:
            target = idx
    return target


# ================================================================ المراجعة (Reviews)
async def resolve_log(i: discord.Interaction, need_pending=True):
    lang = await get_user_lang(i.user.id)
    if not i.guild or not isinstance(i.user, discord.Member):
        await i.response.send_message("Use this inside the server.", ephemeral=True)
        return None
    log_id = parse_log_id(i.message)
    row = await get_log(log_id) if log_id else None
    if (not row or row["guild_id"] != i.guild.id
            or (row["review_msg"] and row["review_msg"] != i.message.id)):
        await i.response.send_message(t(lang, "log_not_found"), ephemeral=True)
        return None
    if need_pending and row["status"] != "pending":
        await i.response.send_message(t(lang, "already_reviewed"), ephemeral=True)
        return None
    return row


async def accept_log(i: discord.Interaction):
    row = await resolve_log(i)
    if not row:
        return
    lang = await get_user_lang(i.user.id)
    if not await can_review(i.user, row["type"]):
        return await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
    if row["author_id"] == i.user.id:
        return await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
    season = row["season_id"] or "main"
    if season != await get_current_season(i.guild.id):
        return await i.response.send_message(t(lang, "log_not_found"), ephemeral=True)
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
                return await i.followup.send(t(lang, "already_reviewed"), ephemeral=True)
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
        except Exception:
            await db.rollback()
            return await i.followup.send(t(lang, "no_perm"), ephemeral=True)

    custom_tmpl = await get_custom_template(i.guild.id, row["type"])
    pub_id = await cfg_get(i.guild.id, row["type"], "log_channel")
    log_ch = i.guild.get_channel(pub_id) if pub_id else None
    if log_ch:
        ping_id = await cfg_get(i.guild.id, row["type"], "log_ping")
        text = render(log_id, row["type"], row["author_id"], data, row["result"], None,
                      "accepted", pts, i.user.id, pts_to=recips, custom_template=custom_tmpl, lang=lang)
        content = (f"<@&{ping_id}>\n" if ping_id else "") + text
        cand = [int(data["candidate_id"])] if row["type"] == "tryout" and data.get("candidate_id") else None
        try:
            files = [await a.to_file() for a in i.message.attachments]
        except discord.HTTPException:
            files = []
        try:
            sent = await log_ch.send(content=content, files=files, allowed_mentions=mentions(ping_id, cand))
            await db_exec("UPDATE logs SET public_channel=?, public_msg=? WHERE id=?", (log_ch.id, sent.id, log_id))
        except discord.HTTPException:
            pass

    reviewed = render(log_id, row["type"], row["author_id"], data, row["result"], flags,
                      "accepted", pts, i.user.id, pts_to=recips, custom_template=custom_tmpl, lang=lang)
    try:
        await i.message.edit(content=reviewed, view=None, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass

    for uid, total in totals.items():
        try:
            member = await get_member(i.guild, uid)
            await update_member_rank(i.guild, member, total)
        except Exception:
            pass


class RejectModal(discord.ui.Modal, title="Rejection Reason / سبب الرفض"):
    reason = discord.ui.TextInput(label="Reason / السبب", style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, log_id, message):
        super().__init__()
        self.log_id = log_id
        self.message = message

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        lang = await get_user_lang(i.user.id)
        row = await get_log(self.log_id)
        if not row or row["guild_id"] != i.guild.id:
            return await i.followup.send(t(lang, "log_not_found"), ephemeral=True)
        if not isinstance(i.user, discord.Member) or not await can_review(i.user, row["type"]):
            return await i.followup.send(t(lang, "no_perm"), ephemeral=True)

        async with aiosqlite.connect(DB) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE logs SET status='rejected', reviewer_id=?, reason=?, reviewed_at=? "
                    "WHERE id=? AND status='pending'",
                    (i.user.id, self.reason.value, now_utc().isoformat(), self.log_id))
                if cur.rowcount == 0:
                    await db.rollback()
                    return await i.followup.send(t(lang, "already_reviewed"), ephemeral=True)
                await db.commit()
            except Exception:
                await db.rollback()
                return await i.followup.send(t(lang, "no_perm"), ephemeral=True)

        data = json.loads(row["data"])
        flags = json.loads(row["flags"] or "[]")
        custom_tmpl = await get_custom_template(i.guild.id, row["type"])
        text = render(self.log_id, row["type"], row["author_id"], data, row["result"], flags,
                      "rejected", reviewer_id=i.user.id, reason=self.reason.value, custom_template=custom_tmpl, lang=lang)
        try:
            await self.message.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        await i.followup.send(t(lang, "status_rejected"), ephemeral=True)


class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, custom_id="log:accept")
    async def accept(self, i: discord.Interaction, button: discord.ui.Button):
        await accept_log(i)

    @discord.ui.button(label="Reject", emoji="❌", style=discord.ButtonStyle.danger, custom_id="log:reject")
    async def reject(self, i: discord.Interaction, button: discord.ui.Button):
        row = await resolve_log(i)
        if not row:
            return
        lang = await get_user_lang(i.user.id)
        if not await can_review(i.user, row["type"]):
            return await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
        if row["author_id"] == i.user.id:
            return await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
        await i.response.send_modal(RejectModal(row["id"], i.message))


# ================================================================ إنشاء اللوج (Log Submissions)
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

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        lang = await get_user_lang(i.user.id)
        if len(self.files) > MAX_FILES_COUNT:
            return await i.followup.send(t(lang, "no_perm"), ephemeral=True)

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
                        return await i.followup.send(t(lang, "no_perm"), ephemeral=True)
                cur = await db.execute(
                    "INSERT INTO logs(guild_id, type, author_id, result, data, flags, created_at, season_id, parent_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (i.guild.id, self.log_type, i.user.id, self.result, json.dumps(data, ensure_ascii=False),
                     json.dumps(flags, ensure_ascii=False), now_utc().isoformat(), season, self.parent_id))
                log_id = cur.lastrowid
                for h in hashes:
                    await db.execute("INSERT OR IGNORE INTO hashes VALUES(?,?)", (h, log_id))
                await db.commit()
            except Exception:
                await db.rollback()
                return await i.followup.send(t(lang, "no_perm"), ephemeral=True)

        ch_id = await cfg_get(i.guild.id, self.log_type, "review_channel")
        ch = i.guild.get_channel(ch_id) if ch_id else None
        ping_id = await cfg_get(i.guild.id, self.log_type, "review_ping")
        custom_tmpl = await get_custom_template(i.guild.id, self.log_type)
        text = render(log_id, self.log_type, i.user.id, data, self.result, flags, custom_template=custom_tmpl, lang=lang)
        content = (f"<@&{ping_id}>\n" if ping_id else "") + text
        am = mentions(ping_id)

        if not ch:
            return await i.followup.send(t(lang, "no_perm"), ephemeral=True)
        try:
            sent = await ch.send(content=content, view=ReviewView(), allowed_mentions=am)
        except discord.HTTPException:
            return await i.followup.send(t(lang, "no_perm"), ephemeral=True)

        await db_exec("UPDATE logs SET review_channel=?, review_msg=? WHERE id=?", (ch.id, sent.id, log_id))
        await i.followup.send(t(lang, "status_pending"), ephemeral=True)


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
            flags.append(f"Same file used in Log #{row[0]}")
    return hashes, flags


async def start_log(i: discord.Interaction, log_type, result, files: List[Optional[discord.Attachment]], extra=None):
    files = [f for f in files if f is not None]
    lang = await get_user_lang(i.user.id)
    if not i.guild or not isinstance(i.user, discord.Member):
        return await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
    if not await check_cmd_perm(i, f"log-{log_type}"):
        return await i.response.send_message(t(lang, "no_role"), ephemeral=True)
    if not await can_submit(i.user, log_type):
        return await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
    rid = await cfg_get(i.guild.id, log_type, "review_channel")
    if not rid or not i.guild.get_channel(rid):
        return await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
    await i.response.send_modal(LogModal(log_type, result, files, extra or {}))


async def youtube_extra(i: discord.Interaction, youtube_link: Optional[str], extra: dict):
    if youtube_link:
        yt = valid_youtube(youtube_link)
        if not yt:
            lang = await get_user_lang(i.user.id)
            await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
            return False
        extra["youtube_link"] = yt
    return True


@bot.tree.command(name="log-war", description="Log a Crew War")
@app_commands.choices(result=RESULT_CHOICES)
async def log_war(i: discord.Interaction, result: app_commands.Choice[str],
                  file1: discord.Attachment, file2: discord.Attachment,
                  file3: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None,
                  player1: Optional[discord.Member] = None, player2: Optional[discord.Member] = None):
    ids = dedupe([p.id for p in (player1, player2) if p and not p.bot and p.id != i.user.id])
    extra = {"participants": ids}
    if not await youtube_extra(i, youtube_link, extra):
        return
    await start_log(i, "war", result.value, [file1, file2, file3], extra)


@bot.tree.command(name="log-1v1", description="Log a 1v1 fight")
@app_commands.choices(result=RESULT_CHOICES)
async def log_1v1(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment,
                  proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None):
    extra = {}
    if not await youtube_extra(i, youtube_link, extra):
        return
    await start_log(i, "1v1", result.value, [proof1, proof2], extra)


@bot.tree.command(name="log-2v2", description="Log a 2v2 fight")
@app_commands.choices(result=RESULT_CHOICES)
async def log_2v2(i: discord.Interaction, result: app_commands.Choice[str], proof1: discord.Attachment,
                  proof2: Optional[discord.Attachment] = None, youtube_link: Optional[str] = None,
                  teammate: Optional[discord.Member] = None):
    ids = [teammate.id] if teammate and not teammate.bot and teammate.id != i.user.id else []
    extra = {"participants": ids}
    if not await youtube_extra(i, youtube_link, extra):
        return
    await start_log(i, "2v2", result.value, [proof1, proof2], extra)


@bot.tree.command(name="log-training", description="Log training session")
async def log_training(i: discord.Interaction, proof1: discord.Attachment, proof2: Optional[discord.Attachment] = None):
    await start_log(i, "training", None, [proof1, proof2])


@bot.tree.command(name="log-tryout", description="Log a tryout session")
@app_commands.choices(result=TRYOUT_CHOICES)
async def log_tryout(i: discord.Interaction, candidate: discord.Member, result: app_commands.Choice[str],
                     proof: discord.Attachment):
    if candidate.bot or candidate.id == i.user.id:
        lang = await get_user_lang(i.user.id)
        return await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
    extra = {"candidate_id": candidate.id, "candidate": candidate.mention, "tester_id": i.user.id}
    await start_log(i, "tryout", result.value, [proof], extra)


@bot.tree.command(name="resubmit", description="Resubmit a rejected log")
async def resubmit(i: discord.Interaction, log_id: int):
    row = await get_log(log_id)
    lang = await get_user_lang(i.user.id)
    if not row or row["guild_id"] != i.guild.id or row["author_id"] != i.user.id:
        return await i.response.send_message(t(lang, "log_not_found"), ephemeral=True)
    if row["status"] != "rejected":
        return await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
    data = json.loads(row["data"])
    files = [u for u in data.get("proof_urls", []) if isinstance(u, str)]
    if not files:
        return await i.response.send_message(t(lang, "no_perm"), ephemeral=True)
    extra = {k: data[k] for k in ("candidate_id", "candidate", "tester_id", "participants", "youtube_link") if k in data}
    await i.response.send_modal(LogModal(row["type"], row["result"], files, extra, defaults=data, parent_id=log_id))


@bot.tree.command(name="points", description="Check member points")
async def points_cmd(i: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or i.user
    lang = await get_user_lang(i.user.id)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        row = await cur.fetchone()
    total = row[0] if row else 0
    await i.response.send_message(f"{member.mention}: **{total}** {t(lang, 'lbl_points')}.", allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="profile", description="View member profile")
async def profile(i: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or i.user
    lang = await get_user_lang(i.user.id)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT points FROM members WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        row = await cur.fetchone()
        total_pts = row[0] if row else 0

        cur2 = await db.execute("SELECT roblox_name FROM links WHERE guild_id=? AND user_id=?", (i.guild.id, member.id))
        link_row = await cur2.fetchone()
        roblox_name = link_row[0] if link_row else "-"

        cur_logs = await db.execute("SELECT COUNT(*) FROM logs WHERE guild_id=? AND author_id=? AND status='accepted'", (i.guild.id, member.id))
        logs_count = (await cur_logs.fetchone())[0]

    ladder = await get_ladder(i.guild.id)
    target = target_index(ladder, total_pts)
    rank_name = ladder[target][0] if 0 <= target < len(ladder) else "-"

    embed = discord.Embed(title=f"{t(lang, 'profile_title')}: {member.display_name}", color=member.color)
    embed.add_field(name=f"💎 {t(lang, 'profile_pts')}", value=str(total_pts), inline=True)
    embed.add_field(name=f"🎖️ {t(lang, 'profile_rank')}", value=rank_name, inline=True)
    embed.add_field(name=f"📊 {t(lang, 'profile_logs')}", value=str(logs_count), inline=True)
    embed.add_field(name=f"🤖 {t(lang, 'profile_roblox')}", value=roblox_name, inline=False)
    await i.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="View crew war stats")
async def stats(i: discord.Interaction, crew: Optional[str] = None):
    lang = await get_user_lang(i.user.id)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT result, COUNT(*) FROM logs WHERE guild_id=? AND type='war' AND status='accepted' GROUP BY result",
            (i.guild.id,)
        )
        rows = await cur.fetchall()
    stats_map = {r[0]: r[1] for r in rows}
    wins, losses = stats_map.get("win", 0), stats_map.get("loss", 0)
    total = wins + losses
    winrate = f"{(wins / total * 100):.1f}%" if total > 0 else "0.0%"

    embed = discord.Embed(title=t(lang, "stats_title"), color=discord.Color.blue())
    embed.add_field(name=f"🏆 {t(lang, 'stats_wins')}", value=str(wins), inline=True)
    embed.add_field(name=f"❌ {t(lang, 'stats_losses')}", value=str(losses), inline=True)
    embed.add_field(name=f"📈 {t(lang, 'stats_winrate')}", value=winrate, inline=True)
    await i.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="help", description="Show full help manual in your chosen language")
async def help_cmd(i: discord.Interaction):
    lang = await get_user_lang(i.user.id)
    embed = discord.Embed(
        title=t(lang, "help_title"),
        color=discord.Color.gold()
    )
    embed.add_field(name="🎮 Submissions", value=t(lang, "help_part1"), inline=False)
    embed.add_field(name="🛡️ Management", value=t(lang, "help_part2"), inline=False)
    embed.add_field(name="📊 Public & Stats", value=t(lang, "help_part3"), inline=False)
    await i.response.send_message(embed=embed, ephemeral=True)


@tasks.loop(minutes=30)
async def maintenance():
    pass


@maintenance.before_loop
async def before_maintenance():
    await bot.wait_until_ready()


if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("❌ DISCORD_TOKEN is missing.")
    bot.run(token)
