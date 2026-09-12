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
    BASE_STORAGE_CAP: float = 1000.0  # Base storage capacity per resource
    STORAGE_CAP_PER_LEVEL: float = 500.0

    @property
    def conn_str(self) -> str:
        return f"host={self.DB_HOST} port={self.DB_PORT} user={self.DB_USER} password={self.DB_PASS} dbname={self.DB_NAME}"

settings = Settings()

# Default building-to-resource mapping and base production rates (per second)
BUILDING_CONFIG = {
    "lumberjack": {
        "resource": "wood",
        "name": "Holzfällerhütte",
        "base_rate": 0.25, # 15 / min
        "upgrade_cost_base": 50.0,
    },
    "quarry": {
        "resource": "stone",
        "name": "Steinbruch",
        "base_rate": 0.20, # 12 / min
        "upgrade_cost_base": 75.0,
    },
    "mine": {
        "resource": "iron",
        "name": "Eisenmine",
        "base_rate": 0.10, # 6 / min
        "upgrade_cost_base": 150.0,
    },
    "farm": {
        "resource": "grain",
        "name": "Getreidehof",
        "base_rate": 0.30, # 18 / min
        "upgrade_cost_base": 40.0,
    },
    "weaver": {
        "resource": "cloth",
        "name": "Weberei",
        "base_rate": 0.08, # 4.8 / min
        "upgrade_cost_base": 120.0,
    },
}

SUPPORTED_RESOURCES = ["wood", "stone", "iron", "grain", "cloth"]
