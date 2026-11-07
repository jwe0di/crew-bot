OWNER_ID = 0

REVIEW_CHANNEL_ID = None
LOG_CHANNEL_ID = None
AUDIT_CHANNEL_ID = None

EDITOR_ROLES = ["The Admiral", "Empyrean Monarch", "Commander"]
REVIEWER_ROLES = ["The Admiral", "Empyrean Monarch", "Commander", "Astral Apotheosis"]
TRYOUTER_ROLES = ["Tryout Master"]

LADDER = [
    ("Lvl. 20", 20),
    ("Lvl. 40", 60),
    ("Lvl. 60", 130),
    ("Lvl. 80", 220),
    ("Lvl. 100", 350),
]

DEFAULT_POINTS = {
    "war_win": 10,
    "war_loss": 5,
    "outside_win": 8,
    "outside_loss": 4,
    "internal": 3,
    "tryout": 5,
}

POINT_LABELS = {
    "war_win": "War - Win",
    "war_loss": "War - Loss",
    "outside_win": "Outside 1v1 - Win",
    "outside_loss": "Outside 1v1 - Loss",
    "internal": "Internal fight",
    "tryout": "Tryout",
}

MAX_VIDEO_AGE_DAYS = 14

LOG_TYPES = {
    "war": {
        "label": "Crew War Log",
        "fields": [
            ("my_team", "Who fought for Dragon Slayers", True, True),
            ("crew", "Enemy crew name", False, True),
            ("their_team", "Who fought for the enemy crew", True, True),
            ("score", "Score (e.g. 5-3)", False, True),
            ("notes", "Notes (optional)", True, False),
        ],
    },
    "outside": {
        "label": "Outside 1v1 Log",
        "fields": [
            ("my_fighter", "Who fought for us", False, True),
            ("opponent", "Who we fought against", False, True),
            ("score", "Score (e.g. 5-3)", False, True),
            ("notes", "Notes (optional)", True, False),
        ],
    },
    "internal": {
        "label": "Internal Fight Log",
        "fields": [
            ("p1", "Fighter 1", False, True),
            ("p2", "Fighter 2", False, True),
            ("score", "Score (e.g. 5-3)", False, True),
            ("notes", "Notes (optional)", True, False),
        ],
    },
    "tryout": {
        "label": "Tryout Log",
        "fields": [
            ("score", "Tryout score / rating", False, True),
            ("roblox", "Candidate Roblox username (optional)", False, False),
            ("notes", "Notes (optional)", True, False),
        ],
    },
}