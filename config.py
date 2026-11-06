GUILD_ID = 1353141752038686771
REVIEW_CHANNEL_ID = 1421009011729236018
LOG_CHANNEL_ID = 1488622328962093191
AUDIT_CHANNEL_ID = 1488622875224182866
OWNER_ID = 1314489658364657684

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
    "war_win": "Crew War - Win",
    "war_loss": "Crew War - Loss",
    "outside_win": "Outside 1v1 - Win",
    "outside_loss": "Outside 1v1 - Loss",
    "internal": "Internal Fight",
    "tryout": "Tryout",
}

MAX_VIDEO_AGE_DAYS = 14

LOG_TYPES = {
    "war": {
        "label": "Dragon Slayer's Crew War Log",
        "emoji": "⚔️",
        "fields": [
            ("crew", "Enemy Crew Name", False),
            ("my_team", "Dragon Slayer's Fighters", True),
            ("their_team", "Enemy Crew Fighters", True),
            ("score", "Score (e.g. 5-3)", False),
            ("notes", "Notes / Details", True),
        ],
    },
    "outside": {
        "label": "Dragon Slayer's 1v1 Log",
        "emoji": "🥊",
        "fields": [
            ("my_fighter", "Dragon Slayer's Fighter", False),
            ("opponent", "Opponent Fighter (Name & Team)", False),
            ("score", "Score (e.g. 3-0)", False),
            ("notes", "Notes / Details", True),
        ],
    },
    "internal": {
        "label": "Dragon Slayer's Internal Log",
        "emoji": "🤝",
        "fields": [
            ("players", "Fighters", False),
            ("score", "Score (e.g. 5-3)", False),
            ("notes", "Notes / Details", False),
        ],
    },
    "tryout": {
        "label": "Dragon Slayer's Tryout Log",
        "emoji": "🎯",
        "fields": [
            ("candidate", "Candidate Username", False),
            ("score", "Score / Rating", False),
            ("notes", "Notes / Details", True),
        ],
    },
}
