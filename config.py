import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///tt_generator.db").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _as_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    APP_NAME = "TT Generator"
    APP_VERSION = "1.0.0"
    ASSET_VERSION = os.getenv("ASSET_VERSION", "")
    SECRET_KEY = os.getenv("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    SESSION_COOKIE_SECURE = _as_bool("SESSION_COOKIE_SECURE", False)
    SESSION_COOKIE_HTTPONLY = _as_bool("SESSION_COOKIE_HTTPONLY", True)
    SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = SESSION_COOKIE_SAMESITE

    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 4 * 60 * 60
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024

    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
    ADMIN_SESSION_MINUTES = int(os.getenv("ADMIN_SESSION_MINUTES", "20"))

    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_ENABLED = _as_bool("RATELIMIT_ENABLED", True)
    RATELIMIT_HEADERS_ENABLED = True

    LOG_TO_FILE = _as_bool("LOG_TO_FILE", True)
    LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs")))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    SYLLABUS_DIR = Path(os.getenv("SYLLABUS_DIR", str(BASE_DIR / "data" / "syllabus")))
    DEFAULT_SYLLABUS_FILE = SYLLABUS_DIR / "cbse_class12_2026_27.json"

    AUTO_CREATE_DB = False
    PREFERRED_URL_SCHEME = "https" if _as_bool("SESSION_COOKIE_SECURE", False) else "http"


class DevelopmentConfig(Config):
    DEBUG = True
    LOG_LEVEL = "DEBUG"


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = _as_bool("SESSION_COOKIE_SECURE", True)
    PREFERRED_URL_SCHEME = "https"


class TestingConfig(Config):
    TESTING = True
    DEBUG = False
    SECRET_KEY = "test-secret-key"
    ADMIN_PASSWORD = "test-admin-password"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    LOG_TO_FILE = False
    SESSION_COOKIE_SECURE = False
    SERVER_NAME = None


CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}


def get_config(name: str | None = None):
    key = (name or os.getenv("FLASK_ENV") or "development").strip().lower()
    return CONFIGS.get(key, DevelopmentConfig)
