import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    APP_NAME: str = "Handelsimperium"
    SECRET_KEY: str = os.getenv("SECRET_KEY", "super-secret-imperium-key-2026-change-in-prod")
    COOKIE_NAME: str = "imperium_session"
    
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
    DB_USER: str = os.getenv("DB_USER", "handelsimperium_user")
    DB_PASS: str = os.getenv("DB_PASS", "imperium_secret_2026")
    DB_NAME: str = os.getenv("DB_NAME", "handelsimperium")
    
    MARKET_FEE_RATE: float = 0.02  # 2% market fee
    BASE_STORAGE_CAP: float = 1000.0  # Base storage capacity per resource for Level 1 warehouse
    REGIONAL_DEPOT_CAP: float = 500.0  # Max total goods stored in a foreign regional depot
    RATE_LIMIT_ENABLED: bool = True
    CSRF_ENABLED: bool = True
    CSRF_COOKIE_NAME: str = "imperium_csrf"

    ADMIN_USER: str = os.getenv("ADMIN_USER", "admin")
    ADMIN_PASS: str = os.getenv("ADMIN_PASS", "imperium_admin_2026")

    @property
    def conn_str(self) -> str:
        return f"host={self.DB_HOST} port={self.DB_PORT} user={self.DB_USER} password={self.DB_PASS} dbname={self.DB_NAME}"

settings = Settings()

# Canonical reference commodity prices (fallback when no 24h trades exist)
REFERENCE_PRICES = {
    "wood": 4.00,
    "stone": 5.00,
    "iron": 12.00,
    "grain": 3.00,
    "cloth": 8.00,
}

# Daily Export Contract Templates ("Handelskarawanen" Price Floor & Commodity Sink)
CONTRACT_TEMPLATES = {
    "wood": {"amount": 25.0, "reward": 110.00, "title": "Bauholz für die Flotte", "desc": "Die kaiserliche Kriegsmarine benötigt geschlagenes Nutzholz zum Ausbau der Flotte."},
    "stone": {"amount": 20.0, "reward": 110.00, "title": "Bruchstein für Festungsmauern", "desc": "Der Rat der Hanse verstärkt die äußeren Wallanlagen gegen Überfälle."},
    "iron": {"amount": 10.0, "reward": 132.00, "title": "Waffenfähiges Eisen", "desc": "Waffenschmiede der königlichen Garde suchen reines Schmiedeeisen."},
    "grain": {"amount": 35.0, "reward": 115.50, "title": "Korn für kaiserliche Vorratslager", "desc": "Zur Sicherung der Nahrungsmittelversorgung der Städte vor dem Winter."},
    "cloth": {"amount": 15.0, "reward": 132.00, "title": "Segeltuch für Fernhandelsschiffe", "desc": "Gewebtes Tuch für neue Hansekoggen auf der Ostseeroute."},
}

# Starter package for new players
STARTER_CONFIG = {
    "balance": 200.00,
    "inventories": {
        "wood": 50.00,
        "stone": 50.00,
        "iron": 10.00,
        "grain": 10.00,
        "cloth": 10.00,
    },
}


# Default building definitions with multi-resource upgrade requirements
BUILDING_CONFIG = {
    "warehouse": {
        "resource": None,
        "name": "Zentrallager",
        "description": "Erweitert die Lagerkapazität für alle Handelswaren im Kontor.",
        "base_rate": 0.0,
        "base_costs": {
            "wood": 100.0,
            "stone": 80.0,
            "iron": 25.0,
            "balance": 75.0,
        },
    },
    "lumberjack": {
        "resource": "wood",
        "name": "Holzfällerhütte",
        "description": "Schlägt Nutzholz aus den umliegenden Wäldern.",
        "base_rate": 0.25,  # 15 / min
        "base_costs": {
            "wood": 40.0,
            "stone": 20.0,
            "balance": 25.0,
        },
    },
    "quarry": {
        "resource": "stone",
        "name": "Steinbruch",
        "description": "Baut Bruchstein für Befestigungen und Fundamente ab.",
        "base_rate": 0.20,  # 12 / min
        "base_costs": {
            "wood": 60.0,
            "stone": 35.0,
            "balance": 35.0,
        },
    },
    "mine": {
        "resource": "iron",
        "name": "Eisenmine",
        "description": "Fördert wertvolles Eisenerz aus den Tiefen des Berges.",
        "base_rate": 0.10,  # 6 / min
        "base_costs": {
            "wood": 80.0,
            "stone": 60.0,
            "iron": 20.0,
            "balance": 50.0,
        },
    },
    "farm": {
        "resource": "grain",
        "name": "Getreidehof",
        "description": "Erntet goldenes Korn zur Ernährung der Stadtbevölkerung.",
        "base_rate": 0.30,  # 18 / min
        "base_costs": {
            "wood": 45.0,
            "stone": 20.0,
            "balance": 20.0,
        },
    },
    "weaver": {
        "resource": "cloth",
        "name": "Weberei",
        "description": "Verarbeitet Rohfasern zu kostbarem Tuch für den Fernhandel.",
        "base_rate": 0.08,  # 4.8 / min
        "base_costs": {
            "wood": 50.0,
            "stone": 30.0,
            "cloth": 20.0,
            "balance": 40.0,
        },
    },
}

SUPPORTED_RESOURCES = ["wood", "stone", "iron", "grain", "cloth"]

# Guild settings & Cooperative Monument configurations
GUILD_CREATION_FEE: float = 500.00

GUILD_PROJECT_CONFIG = {
    "FREIHAFEN": {
        "name": "Großer Freihafen",
        "description": "Errichtet Zollfreizonen an den Kais. Senkt die Börsen-Handelsgebühr für alle Gildenmitglieder von 2,0% auf 1,5%.",
        "perk_type": "FREIHAFEN",
        "target_costs": {
            "wood": 150.0,
            "stone": 100.0,
            "cloth": 50.0,
            "balance": 300.0,
        },
    },
    "SPEICHERSTADT": {
        "name": "Monumentale Speicherstadt",
        "description": "Errichtet gigantische Backsteinspeicher für die Kaufmannsgilde. Erhöht die Lagerkapazität aller Gildenmitglieder um +10%.",
        "perk_type": "SPEICHERSTADT",
        "target_costs": {
            "wood": 100.0,
            "stone": 200.0,
            "iron": 50.0,
            "balance": 300.0,
        },
    },
}

# Phase 9: Caravan Logistics Configuration
CARAVAN_MAX_CARGO: float = 250.0  # Max total goods units per expedition
TRANSIT_SPEED_FACTOR: float = 12.0  # Transit duration in seconds per Euclidean coordinate distance unit

