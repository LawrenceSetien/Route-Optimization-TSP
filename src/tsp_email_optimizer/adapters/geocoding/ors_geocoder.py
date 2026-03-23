from __future__ import annotations

import csv
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


class OrsGeocoder:
    _GEOCODE_CANDIDATES_SIZE = 5
    _MIN_ACCEPTED_CONFIDENCE = 0.65
    _MIN_ACCEPTED_SCORE = 55.0

    def __init__(
        self,
        api_key: str,
        timeout_s: int = 30,
        geocode_cache_path: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._headers = {"Authorization": self._api_key, "Content-Type": "application/json"}
        self._geocode_cache_path = Path(geocode_cache_path) if geocode_cache_path else None
        self._geocode_cache: dict[str, dict[str, str]] = {}
        self._load_geocode_cache()

    def geocode_one(self, address: str) -> tuple[float, float, float | None] | None:
        cache_key = self._normalize_address(address)
        cached = self._geocode_cache.get(cache_key)
        if cached:
            cached_provider = cached.get("provider", "").strip().lower()
            if cached_provider and cached_provider != "ors":
                logger.info(
                    "Ignoring non-ors cache entry for address=%r provider=%r",
                    address,
                    cached_provider,
                )
            else:
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
                    if confidence is not None and confidence < self._MIN_ACCEPTED_CONFIDENCE:
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
        normalized = OrsGeocoder._normalize_for_search(value)
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
            if not OrsGeocoder._normalize_for_search(part).startswith(("entre ", "esquina "))
        ]
        if not filtered:
            return address
        if len(filtered) > 4:
            # Keep street + last location hints (city/region/country/postal code).
            filtered = [filtered[0], *filtered[-3:]]
        return ", ".join(filtered)

    def _load_geocode_cache(self) -> None:
        if not self._geocode_cache_path:
            return
        self._geocode_cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._geocode_cache_path.exists():
            with self._geocode_cache_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "provider",
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
            "provider": "ors",
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
                    "provider",
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
