# إعدادات اختيارية. bot.py بيشتغل حتى لو ناقص منها أي حاجة (بياخد قيم افتراضية).
# الأفضل تحط OWNER_ID و DISCORD_TOKEN في Railway Variables.
OWNER_ID = 0

EDITOR_ROLES = ["The Admiral", "Empyrean Monarch", "Commander"]
REVIEWER_ROLES = ["The Admiral", "Empyrean Monarch", "Commander", "Astral Apotheosis"]
TRYOUTER_ROLES = ["Tryout Master"]
CREW_ROLE_KEYWORD = "Crew"

LADDER = [
    ("Lvl. 20", 20),
    ("Lvl. 40", 60),
    ("Lvl. 60", 130),
    ("Lvl. 80", 220),
    ("Lvl. 100", 350),
]

DEFAULT_POINTS = {
    "war_win": 10, "war_loss": 5,
    "1v1_win": 8, "1v1_loss": 4,
    "2v2_win": 6, "2v2_loss": 3,
    "training": 3, "tryout": 5,
}

POINT_LABELS = {
    "war_win": "War - Win", "war_loss": "War - Loss",
    "1v1_win": "1v1 - Win", "1v1_loss": "1v1 - Loss",
    "2v2_win": "2v2 - Win", "2v2_loss": "2v2 - Loss",
    "training": "Training", "tryout": "Tryout",
}

# كل حقل: (المفتاح، العنوان، طويل؟، إجباري؟)
LOG_TYPES = {
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
