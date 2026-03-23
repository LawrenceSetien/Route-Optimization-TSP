from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env into process environment for local development/testing.
# This lets `AppConfig.from_env()` work without requiring `source .env`.
load_dotenv()


@dataclass(frozen=True)
class AppConfig:
    openai_api_key: str
    openroute_api_key: str
    geocoding_provider: str
    google_geocoding_api_key: str | None
    google_geocoding_language: str | None
    google_geocoding_region: str | None
    google_geocoding_components: str | None
    email_imap_host: str
    email_imap_port: int
    email_smtp_host: str
    email_smtp_port: int
    email_username: str
    email_password: str
    email_subject_contains: str | None
    email_unread_scan_limit: int
    app_timezone: str
    csv_output_path: str
    openai_model: str
    ors_profile: str
    max_extraction_retries: int
    map_enabled: bool
    map_output_path: str

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            openai_api_key=_required("OPENAI_API_KEY"),
            openroute_api_key=_required("OPENROUTESERVICE_API_KEY"),
            geocoding_provider=os.getenv("GEOCODING_PROVIDER", "ors").strip().lower(),
            google_geocoding_api_key=_optional("GOOGLE_GEOCODING_API_KEY"),
            google_geocoding_language=_optional("GOOGLE_GEOCODING_LANGUAGE"),
            google_geocoding_region=_optional("GOOGLE_GEOCODING_REGION"),
            google_geocoding_components=_optional("GOOGLE_GEOCODING_COMPONENTS"),
            email_imap_host=_required("EMAIL_IMAP_HOST"),
            email_imap_port=int(os.getenv("EMAIL_IMAP_PORT", "993")),
            email_smtp_host=_required("EMAIL_SMTP_HOST"),
            email_smtp_port=int(os.getenv("EMAIL_SMTP_PORT", "587")),
            email_username=_required("EMAIL_USERNAME"),
            email_password=_required("EMAIL_PASSWORD"),
            email_subject_contains=_optional("EMAIL_SUBJECT_CONTAINS"),
            email_unread_scan_limit=int(os.getenv("EMAIL_UNREAD_SCAN_LIMIT", "200")),
            app_timezone=os.getenv("APP_TIMEZONE", "America/Santiago"),
            csv_output_path=os.getenv("CSV_OUTPUT_PATH", "./data"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            ors_profile=os.getenv("ORS_PROFILE", "driving-car"),
            max_extraction_retries=int(os.getenv("MAX_EXTRACTION_RETRIES", "2")),
            map_enabled=_as_bool(os.getenv("MAP_ENABLED"), default=True),
            map_output_path=os.getenv("MAP_OUTPUT_PATH", "./data/maps"),
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

