"""Central configuration loader — reads .env and profile.yaml."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def load_profile() -> dict:
    profile_path = BASE_DIR / "data" / "profile.yaml"
    with open(profile_path, "r") as f:
        return yaml.safe_load(f)


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


# Shortcuts
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
ADZUNA_APP_ID = env("ADZUNA_APP_ID")
ADZUNA_APP_KEY = env("ADZUNA_APP_KEY")
ADZUNA_COUNTRY = env("ADZUNA_COUNTRY", "us")
LINKEDIN_EMAIL = env("LINKEDIN_EMAIL")
LINKEDIN_PASSWORD = env("LINKEDIN_PASSWORD")
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")
SMTP_HOST = env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")
REPORT_EMAIL_TO = env("REPORT_EMAIL_TO")
DRY_RUN = env_bool("DRY_RUN")
MAX_DAILY_APPLICATIONS = int(env("MAX_DAILY_APPLICATIONS", "20"))
DB_PATH = BASE_DIR / "data" / "jobs.db"
RESUME_PATH = BASE_DIR / "data" / "resume.pdf"
TEMPLATES_DIR = BASE_DIR / "templates"
