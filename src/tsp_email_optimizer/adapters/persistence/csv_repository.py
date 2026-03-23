from __future__ import annotations

import csv
import logging
from pathlib import Path

from tsp_email_optimizer.domain.models import ExtractedTrip, OptimizedRoute, RequestStatus

logger = logging.getLogger(__name__)


class CsvTripRepository:
    REQUEST_HEADERS = [
        "request_id",
        "email_subject",
        "email_from",
        "trip_date",
        "departure_time",
        "timezone",
        "start_address",
        "language_detected",
        "confidence",
        "warnings",
        "status",
        "status_note",
        "total_stops",
    ]
    STOPS_HEADERS = [
        "request_id",
        "original_index",
        "optimized_index",
        "address",
        "lat",
        "lon",
        "geocode_confidence",
    ]

    def __init__(self, output_dir: str) -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._requests_file = self._output_dir / "requests.csv"
        self._stops_file = self._output_dir / "stops.csv"
        self._ensure_files()
        logger.info(
            "CSV repository initialized output_dir=%r requests_file=%r stops_file=%r",
            str(self._output_dir),
            str(self._requests_file),
            str(self._stops_file),
        )

    def save_request(self, trip: ExtractedTrip, status: RequestStatus) -> None:
        row = {
            "request_id": trip.request_id,
            "email_subject": trip.email_subject,
            "email_from": trip.email_from,
            "trip_date": trip.trip_date,
            "departure_time": trip.departure_time,
            "timezone": trip.timezone,
            "start_address": trip.start_address or "",
            "language_detected": trip.language_detected,
            "confidence": trip.confidence,
            "warnings": " | ".join(trip.warnings),
            "status": status.value,
            "status_note": "",
            "total_stops": len(trip.addresses),
        }
        self._append_row(self._requests_file, self.REQUEST_HEADERS, row)
        logger.info(
            "Saved request row request_id=%s status=%s total_stops=%d",
            trip.request_id,
            status.value,
            len(trip.addresses),
        )

    def save_optimized_route(self, route: OptimizedRoute) -> None:
        logger.info(
            "Saving optimized route rows request_id=%s stops=%d",
            route.request_id,
            len(route.ordered_stops),
        )
        for optimized_index, stop in enumerate(route.ordered_stops, start=1):
            row = {
                "request_id": route.request_id,
                "original_index": stop.original_index,
                "optimized_index": optimized_index,
                "address": stop.address,
                "lat": stop.lat,
                "lon": stop.lon,
                "geocode_confidence": stop.geocode_confidence
                if stop.geocode_confidence is not None
                else "",
            }
            self._append_row(self._stops_file, self.STOPS_HEADERS, row)
        logger.info("Saved optimized route rows request_id=%s", route.request_id)

    def update_request_status(
        self, request_id: str, status: RequestStatus, status_note: str = ""
    ) -> None:
        rows = self._read_csv(self._requests_file)
        if not rows:
            logger.warning(
                "No request rows available when updating status request_id=%s status=%s",
                request_id,
                status.value,
            )
            return
        updated = False
        for row in rows:
            if row["request_id"] == request_id:
                row["status"] = status.value
                row["status_note"] = status_note
                updated = True
        self._write_all(self._requests_file, self.REQUEST_HEADERS, rows)
        if updated:
            logger.info("Updated request status request_id=%s status=%s", request_id, status.value)
        else:
            logger.warning(
                "Request id not found while updating status request_id=%s status=%s",
                request_id,
                status.value,
            )

    def _ensure_files(self) -> None:
        if not self._requests_file.exists():
            self._write_all(self._requests_file, self.REQUEST_HEADERS, [])
        if not self._stops_file.exists():
            self._write_all(self._stops_file, self.STOPS_HEADERS, [])

    @staticmethod
    def _append_row(path: Path, headers: list[str], row: dict) -> None:
        with path.open("a", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=headers)
            writer.writerow(row)

    @staticmethod
    def _read_csv(path: Path) -> list[dict]:
        with path.open("r", newline="", encoding="utf-8") as csv_file:
            return list(csv.DictReader(csv_file))

    @staticmethod
    def _write_all(path: Path, headers: list[str], rows: list[dict]) -> None:
        with path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

