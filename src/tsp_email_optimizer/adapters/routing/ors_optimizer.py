from __future__ import annotations

import csv
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

from tsp_email_optimizer.domain.models import ExtractedTrip, GeocodedStop, OptimizedRoute

logger = logging.getLogger(__name__)


class OpenRouteServiceOptimizer:
    _GEOCODE_CANDIDATES_SIZE = 5
    _MIN_ACCEPTED_CONFIDENCE = 0.65
    _MIN_ACCEPTED_SCORE = 55.0

    def __init__(
        self,
        api_key: str,
        profile: str = "driving-car",
        timeout_s: int = 30,
        geocode_cache_path: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._profile = profile
        self._timeout_s = timeout_s
        self._headers = {"Authorization": self._api_key, "Content-Type": "application/json"}
        self._geocode_cache_path = Path(geocode_cache_path) if geocode_cache_path else None
        self._geocode_cache: dict[str, dict[str, str]] = {}
        self._load_geocode_cache()

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
            result = self._geocode_one(address)
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

    def _geocode_one(self, address: str) -> tuple[float, float, float | None] | None:
        cache_key = self._normalize_address(address)
        cached = self._geocode_cache.get(cache_key)
        if cached:
            found = cached.get("found", "false").lower() == "true"
            if not found:
                logger.info(
                    "Ignoring legacy not-found cache entry for address=%r; retrying geocode.",
                    address,
                )

            lon_str = cached.get("lon")
            lat_str = cached.get("lat")
            conf_str = cached.get("confidence", "")
            if lon_str and lat_str:
                confidence = float(conf_str) if conf_str else None
                if (
                    confidence is not None
                    and confidence < self._MIN_ACCEPTED_CONFIDENCE
                ):
                    logger.warning(
                        "Ignoring low-confidence cached geocode for address=%r confidence=%.2f",
                        address,
                        confidence,
                    )
                else:
                    logger.info("Geocode cache hit for address=%r", address)
                    return float(lon_str), float(lat_str), confidence

        candidate = self._query_best_geocode_candidate(address)
        if candidate is None:
            simplified_address = self._simplify_address_for_retry(address)
            if simplified_address and simplified_address != address:
                logger.info(
                    "Retrying geocode with simplified address original=%r simplified=%r",
                    address,
                    simplified_address,
                )
                candidate = self._query_best_geocode_candidate(simplified_address)
        if candidate is None:
            logger.warning("No accepted geocode candidate for address=%r", address)
            return None

        lon, lat, parsed_confidence, score = candidate
        logger.info(
            "Accepted geocode candidate address=%r lon=%.6f lat=%.6f confidence=%s score=%.2f",
            address,
            lon,
            lat,
            parsed_confidence,
            score,
        )
        self._upsert_geocode_cache(
            address=address,
            found=True,
            lon=lon,
            lat=lat,
            confidence=parsed_confidence,
        )
        return lon, lat, parsed_confidence

    def _query_best_geocode_candidate(
        self,
        address: str,
    ) -> tuple[float, float, float | None, float] | None:
        url = "https://api.openrouteservice.org/geocode/search"
        params = {"text": address, "size": self._GEOCODE_CANDIDATES_SIZE}
        logger.info(
            "ORS geocode request url=%s headers=%s params=%s",
            url,
            self._masked_headers(),
            params,
        )
        response = requests.get(
            url,
            headers=self._headers,
            params=params,
            timeout=self._timeout_s,
        )
        logger.debug("Geocode response status=%d for address=%r", response.status_code, address)
        if response.status_code >= 400:
            logger.error(
                "ORS geocode error status=%d body=%s",
                response.status_code,
                response.text,
            )
        response.raise_for_status()

        payload = response.json()
        features = payload.get("features", [])
        if not features:
            return None

        best_candidate: tuple[float, float, float | None, float] | None = None
        best_score = float("-inf")
        for feature in features:
            candidate = self._build_scored_candidate(address=address, feature=feature)
            if candidate is None:
                continue
            lon, lat, confidence, score = candidate
            if score > best_score:
                best_score = score
                best_candidate = (lon, lat, confidence, score)

        if best_candidate is None:
            return None

        _, _, confidence, score = best_candidate
        if confidence is not None and confidence < self._MIN_ACCEPTED_CONFIDENCE:
            logger.warning(
                "Rejecting geocode candidate due to low confidence address=%r confidence=%.2f score=%.2f",
                address,
                confidence,
                score,
            )
            return None
        if score < self._MIN_ACCEPTED_SCORE:
            logger.warning(
                "Rejecting geocode candidate due to low score address=%r score=%.2f",
                address,
                score,
            )
            return None
        return best_candidate

    def _build_scored_candidate(
        self, address: str, feature: dict
    ) -> tuple[float, float, float | None, float] | None:
        coordinates = feature.get("geometry", {}).get("coordinates", [])
        if len(coordinates) < 2:
            return None
        lon = float(coordinates[0])
        lat = float(coordinates[1])
        properties = feature.get("properties", {})
        confidence_raw = properties.get("confidence")
        confidence = float(confidence_raw) if confidence_raw is not None else None

        candidate_tokens = self._tokenize_text(self._build_feature_text(properties))
        input_tokens = self._tokenize_text(address)
        overlap = 0.0
        if input_tokens:
            overlap = len(input_tokens & candidate_tokens) / len(input_tokens)

        city_expected = "vina" in input_tokens and "mar" in input_tokens
        country_expected = "chile" in input_tokens
        city_match = "vina" in candidate_tokens and "mar" in candidate_tokens
        country_match = "chile" in candidate_tokens

        score = (confidence or 0.0) * 100.0
        score += overlap * 35.0
        if city_expected:
            score += 20.0 if city_match else -20.0
        if country_expected:
            score += 15.0 if country_match else -15.0
        return lon, lat, confidence, score

    @staticmethod
    def _build_feature_text(properties: dict) -> str:
        values = [
            properties.get("label"),
            properties.get("name"),
            properties.get("street"),
            properties.get("locality"),
            properties.get("county"),
            properties.get("region"),
            properties.get("country"),
        ]
        return " ".join(str(value) for value in values if value)

    @staticmethod
    def _tokenize_text(value: str) -> set[str]:
        normalized = OpenRouteServiceOptimizer._normalize_for_search(value)
        tokens = set(re.findall(r"[a-z0-9]+", normalized))
        stopwords = {"de", "del", "la", "las", "los", "y", "en", "entre", "av", "avda"}
        return {token for token in tokens if token not in stopwords}

    @staticmethod
    def _normalize_for_search(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value)
        ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
        return " ".join(ascii_text.strip().lower().split())

    @staticmethod
    def _simplify_address_for_retry(address: str) -> str:
        parts = [part.strip() for part in address.split(",") if part.strip()]
        if not parts:
            return address
        filtered = [
            part
            for part in parts
            if not OpenRouteServiceOptimizer._normalize_for_search(part).startswith(
                ("entre ", "esquina ")
            )
        ]
        if not filtered:
            return address
        if len(filtered) > 4:
            # Keep street + last location hints (city/region/country/postal code).
            filtered = [filtered[0], *filtered[-3:]]
        return ", ".join(filtered)

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

    def _load_geocode_cache(self) -> None:
        if not self._geocode_cache_path:
            return
        self._geocode_cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._geocode_cache_path.exists():
            with self._geocode_cache_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "address_key",
                        "address",
                        "found",
                        "lon",
                        "lat",
                        "confidence",
                        "updated_at_utc",
                    ],
                )
                writer.writeheader()
            return

        with self._geocode_cache_path.open("r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = row.get("address_key")
                if key:
                    self._geocode_cache[key] = row
        logger.info(
            "Loaded geocode cache entries=%d path=%r",
            len(self._geocode_cache),
            str(self._geocode_cache_path),
        )

    def _upsert_geocode_cache(
        self,
        address: str,
        found: bool,
        lon: float | None,
        lat: float | None,
        confidence: float | None,
    ) -> None:
        if not self._geocode_cache_path:
            return

        key = self._normalize_address(address)
        self._geocode_cache[key] = {
            "address_key": key,
            "address": address,
            "found": "true" if found else "false",
            "lon": "" if lon is None else str(lon),
            "lat": "" if lat is None else str(lat),
            "confidence": "" if confidence is None else str(confidence),
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._persist_geocode_cache()

    def _persist_geocode_cache(self) -> None:
        if not self._geocode_cache_path:
            return
        rows = sorted(
            self._geocode_cache.values(),
            key=lambda row: row.get("address_key", ""),
        )
        with self._geocode_cache_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "address_key",
                    "address",
                    "found",
                    "lon",
                    "lat",
                    "confidence",
                    "updated_at_utc",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
        logger.debug("Persisted geocode cache entries=%d", len(rows))

    def _resolve_start_location(
        self, trip: ExtractedTrip, geocoded_stops: list[GeocodedStop]
    ) -> tuple[GeocodedStop, list[str]]:
        notes: list[str] = []
        if trip.start_address:
            result = self._geocode_one(trip.start_address)
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
    def _normalize_address(value: str) -> str:
        return " ".join(value.strip().lower().split())

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

