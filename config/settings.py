"""Typed settings loaded from the environment (.env) plus the applicant profile."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = PROJECT_ROOT / "config" / "profile.toml"

# Non-negotiable ceiling on emails sent per day, regardless of .env.
HARD_DAILY_CAP = 15

load_dotenv(PROJECT_ROOT / ".env")


def _resolve(path_str: str) -> Path:
    path = Path(path_str)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    gemini_fallback_model: str = "gemini-3.1-flash-lite"   # "" disables fallback
    gemini_min_interval: float = 6.0      # seconds between Gemini calls (free-tier friendly)
    hunter_api_key: str = ""
    gmail_credentials_file: Path = PROJECT_ROOT / "credentials" / "gmail_client_secret.json"
    gmail_token_file: Path = PROJECT_ROOT / "credentials" / "gmail_token.json"
    sender_email: str = ""
    sender_name: str = ""
    database_path: Path = PROJECT_ROOT / "data" / "outreach.db"
    daily_email_limit: int = HARD_DAILY_CAP
    india_ratio: float = 0.8
    timezone: str = "Asia/Kolkata"
    qualify_threshold: int = 60            # relevance score needed to qualify an opportunity
    send_interval_seconds: float = 45.0    # pause between emails in a batch send
    followup_after_days: int = 5           # days without a reply before a follow-up is suggested
    max_followups: int = 1                 # follow-ups per original email

    def __post_init__(self) -> None:
        # Clamp to safe ranges so a typo in .env can never exceed the cap.
        object.__setattr__(
            self, "daily_email_limit", max(0, min(self.daily_email_limit, HARD_DAILY_CAP))
        )
        object.__setattr__(self, "india_ratio", max(0.0, min(self.india_ratio, 1.0)))
        object.__setattr__(self, "qualify_threshold", max(0, min(self.qualify_threshold, 100)))
        object.__setattr__(self, "gemini_min_interval", max(0.0, self.gemini_min_interval))

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip(),
            gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite").strip(),
            gemini_min_interval=_float_env("GEMINI_MIN_INTERVAL_SECONDS", 6.0),
            hunter_api_key=os.getenv("HUNTER_API_KEY", "").strip(),
            gmail_credentials_file=_resolve(
                os.getenv("GMAIL_CREDENTIALS_FILE", "credentials/gmail_client_secret.json")
            ),
            gmail_token_file=_resolve(os.getenv("GMAIL_TOKEN_FILE", "credentials/gmail_token.json")),
            sender_email=os.getenv("SENDER_EMAIL", "").strip(),
            sender_name=os.getenv("SENDER_NAME", "").strip(),
            database_path=_resolve(os.getenv("DATABASE_PATH", "data/outreach.db")),
            daily_email_limit=_int_env("DAILY_EMAIL_LIMIT", HARD_DAILY_CAP),
            india_ratio=_float_env("INDIA_RATIO", 0.8),
            timezone=os.getenv("TIMEZONE", "Asia/Kolkata").strip(),
            qualify_threshold=_int_env("QUALIFY_THRESHOLD", 60),
            send_interval_seconds=_float_env("SEND_INTERVAL_SECONDS", 45.0),
            followup_after_days=_int_env("FOLLOWUP_AFTER_DAYS", 5),
            max_followups=max(0, min(_int_env("MAX_FOLLOWUPS", 1), 2)),
        )

    def integration_status(self) -> dict[str, bool]:
        """Whether each external integration has the configuration it needs."""
        return {
            "Gemini": bool(self.gemini_api_key),
            "Hunter.io": bool(self.hunter_api_key),
            "Gmail (OAuth client)": self.gmail_credentials_file.exists(),
            "Gmail (authorized)": self.gmail_token_file.exists(),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def load_profile(path: Path = PROFILE_PATH) -> dict:
    """Load the applicant profile and targeting preferences."""
    with path.open("rb") as fh:
        return tomllib.load(fh)
