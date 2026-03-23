from __future__ import annotations

from typing import Protocol

from tsp_email_optimizer.domain.models import (
    EmailMessage,
    ExtractedTrip,
    OptimizedRoute,
    RequestStatus,
)


class EmailInbox(Protocol):
    def fetch_unprocessed(self, limit: int = 1) -> list[EmailMessage]:
        ...

    def mark_processed(self, uid: str) -> None:
        ...


class EmailReplySender(Protocol):
    def reply(self, original_email: EmailMessage, subject: str, body: str) -> None:
        ...


class TripExtractor(Protocol):
    def extract(self, email: EmailMessage, request_id: str) -> ExtractedTrip:
        ...


class RouteOptimizer(Protocol):
    def optimize(self, trip: ExtractedTrip) -> OptimizedRoute:
        ...


class TripRepository(Protocol):
    def save_request(self, trip: ExtractedTrip, status: RequestStatus) -> None:
        ...

    def save_optimized_route(self, route: OptimizedRoute) -> None:
        ...

    def update_request_status(
        self, request_id: str, status: RequestStatus, status_note: str = ""
    ) -> None:
        ...


class RouteMapRenderer(Protocol):
    def render(self, route: OptimizedRoute) -> str | None:
        ...

