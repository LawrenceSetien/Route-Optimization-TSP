from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RequestStatus(str, Enum):
    RECEIVED = "RECEIVED"
    EXTRACTED = "EXTRACTED"
    EXTRACTION_REVIEW_NEEDED = "EXTRACTION_REVIEW_NEEDED"
    GEOCODING_FAILED = "GEOCODING_FAILED"
    OPTIMIZED = "OPTIMIZED"
    REPLIED = "REPLIED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class EmailMessage:
    uid: str
    subject: str
    sender: str
    body_text: str
    message_id: str
    in_reply_to: str | None = None
    references: str | None = None
    received_at: datetime | None = None


@dataclass(frozen=True)
class ExtractedTrip:
    request_id: str
    email_subject: str
    email_from: str
    trip_date: str
    departure_time: str
    timezone: str
    start_address: str | None
    addresses: list[str]
    language_detected: str
    confidence: float
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GeocodedStop:
    original_index: int
    address: str
    lat: float
    lon: float
    geocode_confidence: float | None = None


@dataclass(frozen=True)
class OptimizedRoute:
    request_id: str
    ordered_stops: list[GeocodedStop]
    start_location: GeocodedStop | None = None
    total_distance_m: float | None = None
    total_duration_s: float | None = None
    notes: list[str] = field(default_factory=list)

