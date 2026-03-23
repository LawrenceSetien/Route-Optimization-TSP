from __future__ import annotations

import logging
import re

import requests

from tsp_email_optimizer.domain.ports import Geocoder
from tsp_email_optimizer.domain.models import ExtractedTrip, GeocodedStop, OptimizedRoute

logger = logging.getLogger(__name__)


class OpenRouteServiceOptimizer:
    def __init__(
        self,
        api_key: str,
        geocoder: Geocoder,
        profile: str = "driving-car",
        timeout_s: int = 30,
    ) -> None:
        self._api_key = api_key
        self._geocoder = geocoder
        self._profile = profile
        self._timeout_s = timeout_s
        self._headers = {"Authorization": self._api_key, "Content-Type": "application/json"}

    def optimize(self, trip: ExtractedTrip) -> OptimizedRoute:
        logger.info(
            "Starting ORS optimization request_id=%s addresses=%d profile=%s",
            trip.request_id,
            len(trip.addresses),
            self._profile,
        )
        geocoded, unresolved_addresses = self._geocode_addresses(trip.addresses)
        if len(geocoded) < 2:
            raise ValueError("Not enough geocoded addresses to optimize route.")

        start_location, notes = self._resolve_start_location(trip, geocoded)
        for unresolved in unresolved_addresses:
            notes.append(f"Direccion no encontrada y omitida: {unresolved}")
        logger.info(
            "Resolved optimization start location request_id=%s address=%r lat=%s lon=%s",
            trip.request_id,
            start_location.address,
            start_location.lat,
            start_location.lon,
        )

        logger.info(
            "Geocoding complete request_id=%s geocoded=%d/%d",
            trip.request_id,
            len(geocoded),
            len(trip.addresses),
        )
        route_data = self._run_optimization(geocoded, start_location)
        ordered_stops, total_distance_m, total_duration_s = self._map_optimization_result(
            route_data=route_data,
            geocoded_stops=geocoded,
            request_id=trip.request_id,
        )
        logger.info(
            "ORS optimization complete request_id=%s ordered_stops=%d distance_m=%s duration_s=%s",
            trip.request_id,
            len(ordered_stops),
            total_distance_m,
            total_duration_s,
        )
        return OptimizedRoute(
            request_id=trip.request_id,
            ordered_stops=ordered_stops,
            start_location=start_location,
            total_distance_m=total_distance_m,
            total_duration_s=total_duration_s,
            notes=notes,
        )

    def _geocode_addresses(
        self, addresses: list[str]
    ) -> tuple[list[GeocodedStop], list[str]]:
        geocoded: list[GeocodedStop] = []
        unresolved_addresses: list[str] = []
        for index, address in enumerate(addresses):
            logger.debug("Geocoding address index=%d value=%r", index + 1, address)
            result = self._geocoder.geocode_one(address)
            if result is None:
                logger.warning("Could not geocode address index=%d value=%r", index + 1, address)
                unresolved_addresses.append(address)
                continue
            lon, lat, confidence = result
            geocoded.append(
                GeocodedStop(
                    original_index=index + 1,
                    address=address,
                    lat=lat,
                    lon=lon,
                    geocode_confidence=confidence,
                )
            )
        return geocoded, unresolved_addresses

    def _run_optimization(self, stops: list[GeocodedStop], start_location: GeocodedStop) -> dict:
        url = "https://api.openrouteservice.org/optimization"
        depot_location = [start_location.lon, start_location.lat]
        logger.info(
            "Using vehicle start/end depot: original_index=%d address=%r location=%s",
            start_location.original_index,
            start_location.address,
            depot_location,
        )

        jobs = []
        for idx, stop in enumerate(stops, start=1):
            jobs.append({"id": idx, "location": [stop.lon, stop.lat]})

        payload = {
            "jobs": jobs,
            "vehicles": [
                {
                    "id": 1,
                    "profile": self._profile,
                    "start": depot_location,
                    "end": depot_location,
                }
            ],
            "options": {"g": True},
        }
        logger.info(
            "ORS optimization request url=%s headers=%s payload=%s",
            url,
            self._masked_headers(),
            payload,
        )
        response = requests.post(
            url,
            headers=self._headers,
            json=payload,
            timeout=self._timeout_s,
        )
        logger.info(
            "Called ORS optimization API with jobs=%d status=%d",
            len(jobs),
            response.status_code,
        )
        if response.status_code >= 400:
            logger.error(
                "ORS optimization error status=%d body=%s",
                response.status_code,
                response.text,
            )
            error_message = self._build_optimization_error_message(
                response=response,
                stops=stops,
                start_location=start_location,
            )
            raise ValueError(error_message)
        return response.json()

    def _build_optimization_error_message(
        self,
        response: requests.Response,
        stops: list[GeocodedStop],
        start_location: GeocodedStop,
    ) -> str:
        detail_text = response.text.strip()
        error_text = detail_text
        try:
            payload = response.json()
            if isinstance(payload, dict):
                raw_error = payload.get("error")
                if isinstance(raw_error, str) and raw_error.strip():
                    error_text = raw_error.strip()
        except ValueError:
            pass

        if "could not find routable point" not in error_text.lower():
            return f"ORS optimization fallo (status {response.status_code}): {error_text}"

        coord = self._extract_routable_point_coordinate(error_text)
        if coord is None:
            return (
                "No se encontro un punto enrutable para una de las direcciones enviadas. "
                f"Detalle ORS: {error_text}"
            )

        lon, lat = coord
        matched_label = self._match_coordinate_to_input(
            lon=lon,
            lat=lat,
            stops=stops,
            start_location=start_location,
        )
        if matched_label:
            return (
                "No se encontro punto enrutable para la direccion "
                f"{matched_label} (coordenadas {lon:.6f}, {lat:.6f}). "
                "Esta direccion podria no estar sobre una via transitable o estar mal geocodificada."
            )
        return (
            "No se encontro punto enrutable para una direccion "
            f"(coordenadas {lon:.6f}, {lat:.6f}). "
            "Revisa que la direccion sea completa y especifica."
        )

    @staticmethod
    def _extract_routable_point_coordinate(error_text: str) -> tuple[float, float] | None:
        # ORS example: "coordinate 1: -70.8813180 -32.4998810"
        match = re.search(
            r"coordinate\s+\d+:\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)",
            error_text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        lon = float(match.group(1))
        lat = float(match.group(2))
        return lon, lat

    @staticmethod
    def _match_coordinate_to_input(
        lon: float,
        lat: float,
        stops: list[GeocodedStop],
        start_location: GeocodedStop,
    ) -> str | None:
        candidates: list[tuple[str, float, float]] = [
            (f"de salida '{start_location.address}'", start_location.lon, start_location.lat)
        ]
        for stop in stops:
            candidates.append(
                (
                    f"'{stop.address}' (parada original #{stop.original_index})",
                    stop.lon,
                    stop.lat,
                )
            )

        nearest_label = None
        nearest_distance = float("inf")
        for label, candidate_lon, candidate_lat in candidates:
            distance = abs(candidate_lon - lon) + abs(candidate_lat - lat)
            if distance < nearest_distance:
                nearest_distance = distance
                nearest_label = label

        if nearest_label is None:
            return None
        # Roughly ~220m tolerance around the same point in lat/lon space.
        if nearest_distance <= 0.002:
            return nearest_label
        return None

    def _resolve_start_location(
        self, trip: ExtractedTrip, geocoded_stops: list[GeocodedStop]
    ) -> tuple[GeocodedStop, list[str]]:
        notes: list[str] = []
        if trip.start_address:
            result = self._geocoder.geocode_one(trip.start_address)
            if result is not None:
                lon, lat, confidence = result
                start_stop = GeocodedStop(
                    original_index=0,
                    address=trip.start_address,
                    lat=lat,
                    lon=lon,
                    geocode_confidence=confidence,
                )
                return start_stop, notes

            notes.append(
                "No se pudo geocodificar la direccion de salida; se uso la primera parada geocodificada."
            )
            logger.warning(
                "Could not geocode start address %r, falling back to first geocoded stop.",
                trip.start_address,
            )
        else:
            notes.append("No se indico direccion de salida; se uso la primera parada geocodificada.")

        return geocoded_stops[0], notes

    def _masked_headers(self) -> dict[str, str]:
        masked = dict(self._headers)
        auth = masked.get("Authorization")
        if auth:
            masked["Authorization"] = self._mask_api_key(auth)
        return masked

    @staticmethod
    def _mask_api_key(value: str) -> str:
        if len(value) <= 8:
            return "***"
        return f"{value[:4]}...{value[-4:]}"

    @staticmethod
    def _map_optimization_result(
        route_data: dict,
        geocoded_stops: list[GeocodedStop],
        request_id: str,
    ) -> tuple[list[GeocodedStop], float | None, float | None]:
        routes = route_data.get("routes", [])
        if not routes:
            raise ValueError(f"No routes found for request_id={request_id}")
        route = routes[0]

        by_job_id = {job_id: geocoded_stops[job_id - 1] for job_id in range(1, len(geocoded_stops) + 1)}
        ordered: list[GeocodedStop] = []
        for step in route.get("steps", []):
            if step.get("type") != "job":
                continue
            job_id = step.get("job")
            if not isinstance(job_id, int):
                continue
            stop = by_job_id.get(job_id)
            if stop:
                ordered.append(stop)

        summary = route.get("summary", {})
        distance = summary.get("distance")
        duration = summary.get("duration")
        return ordered, float(distance) if distance is not None else None, float(duration) if duration is not None else None

