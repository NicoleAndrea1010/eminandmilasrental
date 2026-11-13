import base64
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _is_service_role_key(value: str) -> bool:
    if value.startswith("sb_secret_"):
        return True
    parts = value.split(".")
    if len(parts) != 3:
        return False
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        )
    except (ValueError, UnicodeError):
        return False
    return isinstance(payload, dict) and payload.get("role") == "service_role"


@dataclass(frozen=True)
class Settings:
    app_env: str
    session_secret: str
    supabase_url: str
    supabase_publishable_key: str
    listing_view: str
    listing_cache_ttl_seconds: int
    inquiry_limit: int
    inquiry_window_seconds: int
    local_database_path: Path = PROJECT_ROOT / "instance" / "rentals.sqlite3"

    @classmethod
    def from_env(cls) -> "Settings":
        app_env = os.getenv("APP_ENV", "development").strip().lower()
        session_secret = os.getenv("SESSION_SECRET", "").strip()
        configured_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        configured_key = (
            os.getenv("SUPABASE_PUBLISHABLE_KEY")
            or os.getenv("SUPABASE_ANON_KEY", "")
        ).strip()
        local_database_path = Path(
            os.getenv("LOCAL_DATABASE_PATH", "").strip()
            or "instance/rentals.sqlite3"
        ).expanduser()
        if not local_database_path.is_absolute():
            local_database_path = PROJECT_ROOT / local_database_path
        settings = cls(
            app_env=app_env,
            session_secret=session_secret or secrets.token_urlsafe(32),
            supabase_url=configured_url,
            supabase_publishable_key=configured_key,
            listing_view=os.getenv(
                "SUPABASE_LISTING_VIEW", "public_listings"
            ).strip(),
            listing_cache_ttl_seconds=int(
                os.getenv("LISTING_CACHE_TTL_SECONDS", "90")
            ),
            inquiry_limit=int(os.getenv("INQUIRY_LIMIT", "3")),
            inquiry_window_seconds=int(
                os.getenv("INQUIRY_WINDOW_SECONDS", "1800")
            ),
            local_database_path=local_database_path,
        )
        if not settings.listing_view.isidentifier():
            raise ValueError("SUPABASE_LISTING_VIEW must be a simple SQL identifier")
        if settings.listing_cache_ttl_seconds < 0:
            raise ValueError("LISTING_CACHE_TTL_SECONDS cannot be negative")
        if settings.inquiry_limit < 1 or settings.inquiry_window_seconds < 1:
            raise ValueError("Inquiry rate-limit settings must be positive")
        if bool(configured_url) != bool(configured_key):
            raise ValueError(
                "Set both SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY to enable Supabase."
            )
        if configured_url:
            try:
                parsed_url = urlsplit(configured_url)
                port = parsed_url.port
                has_valid_authority = bool(
                    parsed_url.hostname
                    and (port is None or 1 <= port <= 65535)
                )
            except ValueError:
                has_valid_authority = False
                parsed_url = None
            if (
                not has_valid_authority
                or parsed_url is None
                or parsed_url.scheme not in {"http", "https"}
                or parsed_url.username is not None
                or parsed_url.password is not None
                or parsed_url.path
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ValueError(
                    "SUPABASE_URL must be an HTTP(S) project origin without a path or credentials."
                )
            if app_env == "production" and parsed_url.scheme != "https":
                raise ValueError("SUPABASE_URL must use HTTPS in production.")
        if _is_service_role_key(configured_key):
            raise ValueError(
                "Use a Supabase publishable or anon key, never a service-role key."
            )
        if settings.app_env == "production" and (
            len(session_secret) < 32
            or session_secret == "replace-with-a-long-random-secret"
        ):
            raise ValueError(
                "Set a random SESSION_SECRET of at least 32 characters in production"
            )
        if app_env == "production" and not configured_url:
            raise ValueError(
                "Set SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY in production."
            )
        return settings
