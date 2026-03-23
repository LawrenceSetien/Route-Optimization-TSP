from __future__ import annotations

import csv
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


class GoogleGeocoder:
    _MIN_ACCEPTED_SCORE = 45.0
    _MAX_UNKNOWN_ERROR_RETRIES = 1

    def __init__(
        self,
        api_key: str,
        timeout_s: int = 30,
        geocode_cache_path: str | None = None,
        language: str | None = None,
        region: str | None = None,
        components: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._language = language.strip() if language else None
        self._region = region.strip() if region else None
        self._components = components.strip() if components else None
        self._geocode_cache_path = Path(geocode_cache_path) if geocode_cache_path else None
        self._geocode_cache: dict[str, dict[str, str]] = {}
        self._load_geocode_cache()

    def geocode_one(self, address: str) -> tuple[float, float, float | None] | None:
        cache_key = self._normalize_address(address)
        cached = self._geocode_cache.get(cache_key)
        if cached:
            cached_provider = cached.get("provider", "").strip().lower()
            if cached_provider and cached_provider != "google":
                logger.info(
                    "Ignoring non-google cache entry for address=%r provider=%r",
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
                    logger.info("Google geocode cache hit for address=%r", address)
                    return float(lon_str), float(lat_str), confidence

        candidate = self._query_best_geocode_candidate(address)
        if candidate is None:
            simplified_address = self._simplify_address_for_retry(address)
            if simplified_address and simplified_address != address:
                logger.info(
                    "Retrying Google geocode with simplified address original=%r simplified=%r",
                    address,
                    simplified_address,
                )
                candidate = self._query_best_geocode_candidate(simplified_address)
        if candidate is None:
            logger.warning("No accepted Google geocode candidate for address=%r", address)
            return None

        lon, lat, parsed_confidence, score = candidate
        logger.info(
            "Accepted Google geocode candidate address=%r lon=%.6f lat=%.6f confidence=%s score=%.2f",
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
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = self._build_params(address=address)

        logger.info(
            "Google geocode request url=%s params=%s",
            url,
            self._masked_params_for_log(params),
        )

        retries_left = self._MAX_UNKNOWN_ERROR_RETRIES
        while True:
            response = requests.get(url, params=params, timeout=self._timeout_s)
            logger.debug(
                "Google geocode HTTP status=%d for address=%r",
                response.status_code,
                address,
            )
            if response.status_code >= 400:
                logger.error(
                    "Google geocode HTTP error status=%d body=%s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()

            payload = response.json()
            status = str(payload.get("status", "")).strip().upper()
            error_message = payload.get("error_message")
            if status == "OK":
                break
            if status == "ZERO_RESULTS":
                return None
            if status == "UNKNOWN_ERROR" and retries_left > 0:
                retries_left -= 1
                logger.warning(
                    "Google geocode returned UNKNOWN_ERROR for address=%r; retrying once.",
                    address,
                )
                continue
            details = f" ({error_message})" if error_message else ""
            raise ValueError(
                f"Google geocoding failed for address={address!r} status={status}{details}"
            )

        results = payload.get("results", [])
        if not results:
            return None

        best_candidate: tuple[float, float, float | None, float] | None = None
        best_score = float("-inf")
        for result in results:
            candidate = self._build_scored_candidate(address=address, result=result)
            if candidate is None:
                continue
            lon, lat, confidence, score = candidate
            if score > best_score:
                best_score = score
                best_candidate = (lon, lat, confidence, score)

        if best_candidate is None:
            return None

        _, _, _, score = best_candidate
        if score < self._MIN_ACCEPTED_SCORE:
            logger.warning(
                "Rejecting Google geocode candidate due to low score address=%r score=%.2f",
                address,
                score,
            )
            return None
        return best_candidate

    def _build_scored_candidate(
        self, address: str, result: dict
    ) -> tuple[float, float, float | None, float] | None:
        location = result.get("geometry", {}).get("location", {})
        lat_raw = location.get("lat")
        lon_raw = location.get("lng")
        if lat_raw is None or lon_raw is None:
            return None

        lat = float(lat_raw)
        lon = float(lon_raw)
        location_type = str(result.get("geometry", {}).get("location_type", "")).strip().upper()
        partial_match = bool(result.get("partial_match", False))
        types = [str(t).strip().lower() for t in result.get("types", []) if t]

        candidate_text = self._build_result_text(result=result)
        candidate_tokens = self._tokenize_text(candidate_text)
        input_tokens = self._tokenize_text(address)
        overlap = 0.0
        if input_tokens:
            overlap = len(input_tokens & candidate_tokens) / len(input_tokens)

        score = overlap * 100.0
        score += self._location_type_weight(location_type)
        score += self._result_type_weight(types)
        if partial_match:
            score -= 22.0

        # Keep local quality hinting used by existing addresses.
        city_expected = "vina" in input_tokens and "mar" in input_tokens
        country_expected = "chile" in input_tokens
        city_match = "vina" in candidate_tokens and "mar" in candidate_tokens
        country_match = "chile" in candidate_tokens
        if city_expected:
            score += 16.0 if city_match else -16.0
        if country_expected:
            score += 10.0 if country_match else -10.0

        confidence = max(0.0, min(1.0, score / 100.0))
        return lon, lat, confidence, score

    def _build_params(self, address: str) -> dict[str, str]:
        params = {"address": address, "key": self._api_key}
        if self._language:
            params["language"] = self._language
        if self._region:
            params["region"] = self._region
        if self._components:
            params["components"] = self._components
        return params

    @staticmethod
    def _build_result_text(result: dict) -> str:
        fields: list[str] = []
        formatted_address = result.get("formatted_address")
        if formatted_address:
            fields.append(str(formatted_address))
        for component in result.get("address_components", []):
            long_name = component.get("long_name")
            short_name = component.get("short_name")
            if long_name:
                fields.append(str(long_name))
            if short_name:
                fields.append(str(short_name))
        return " ".join(fields)

    @staticmethod
    def _location_type_weight(location_type: str) -> float:
        if location_type == "ROOFTOP":
            return 20.0
        if location_type == "RANGE_INTERPOLATED":
            return 8.0
        if location_type == "GEOMETRIC_CENTER":
            return -4.0
        if location_type == "APPROXIMATE":
            return -10.0
        return 0.0

    @staticmethod
    def _result_type_weight(types: list[str]) -> float:
        if "street_address" in types:
            return 18.0
        if "premise" in types or "subpremise" in types:
            return 14.0
        if "route" in types:
            return 6.0
        if "intersection" in types:
            return 4.0
        if "locality" in types:
            return -8.0
        return 0.0

    @staticmethod
    def _tokenize_text(value: str) -> set[str]:
        normalized = GoogleGeocoder._normalize_for_search(value)
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
            if not GoogleGeocoder._normalize_for_search(part).startswith(("entre ", "esquina "))
        ]
        if not filtered:
            return address
        if len(filtered) > 4:
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
            "provider": "google",
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

    @staticmethod
    def _masked_params_for_log(params: dict[str, str]) -> dict[str, str]:
        safe = dict(params)
        key = safe.get("key")
        if key:
            safe["key"] = GoogleGeocoder._mask_api_key(key)
        return safe

    @staticmethod
    def _mask_api_key(value: str) -> str:
        if len(value) <= 8:
            return "***"
        return f"{value[:4]}...{value[-4:]}"

    @staticmethod
    def _normalize_address(value: str) -> str:
        return " ".join(value.strip().lower().split())
