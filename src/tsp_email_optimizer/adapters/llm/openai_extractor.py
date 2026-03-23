from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator

from tsp_email_optimizer.domain.models import EmailMessage, ExtractedTrip

logger = logging.getLogger(__name__)


class ExtractedTripSchema(BaseModel):
    trip_date: str = Field(description="Date in format YYYY-MM-DD")
    departure_time: str = Field(description="Time in 24h format HH:MM")
    timezone: str
    start_address: str | None = None
    addresses: list[str]
    language_detected: str
    confidence: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("addresses")
    @classmethod
    def addresses_must_have_min_two(cls, value: list[str]) -> list[str]:
        if len(value) < 2:
            raise ValueError("At least two addresses are required.")
        return value


class OpenAiTripExtractor:
    def __init__(self, api_key: str, model: str, timezone: str, max_retries: int = 2) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._timezone = timezone
        self._max_retries = max_retries

    def extract(self, email: EmailMessage, request_id: str) -> ExtractedTrip:
        logger.info(
            "Starting LLM extraction request_id=%s model=%s subject=%r",
            request_id,
            self._model,
            email.subject,
        )
        payload = self._extract_json_with_retries(email)
        try:
            parsed = ExtractedTripSchema.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"Extraction validation failed: {exc}") from exc

        logger.info(
            "LLM extraction completed request_id=%s addresses=%d language=%s confidence=%.2f",
            request_id,
            len(parsed.addresses),
            parsed.language_detected,
            parsed.confidence,
        )
        return ExtractedTrip(
            request_id=request_id,
            email_subject=email.subject,
            email_from=email.sender,
            trip_date=parsed.trip_date,
            departure_time=parsed.departure_time,
            timezone=parsed.timezone,
            start_address=parsed.start_address,
            addresses=parsed.addresses,
            language_detected=parsed.language_detected,
            confidence=parsed.confidence,
            warnings=parsed.warnings,
        )

    def _extract_json_with_retries(self, email: EmailMessage) -> dict:
        now_local = datetime.now(tz=ZoneInfo(self._timezone)).strftime("%Y-%m-%d")
        last_error = "unknown error"
        for attempt in range(self._max_retries + 1):
            logger.info(
                "Calling OpenAI for extraction attempt=%d/%d subject=%r",
                attempt + 1,
                self._max_retries + 1,
                email.subject,
            )
            prompt = self._build_prompt(email=email, now_local=now_local, attempt=attempt)
            response = self._client.chat.completions.create(
                model=self._model,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You extract route planning data from emails. "
                            "Return valid JSON only with the expected schema."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
            )
            raw_content = response.choices[0].message.content or "{}"
            try:
                parsed_payload = json.loads(raw_content)
                logger.debug("OpenAI returned valid JSON payload.")
                return parsed_payload
            except json.JSONDecodeError as exc:
                last_error = str(exc)
                logger.warning(
                    "OpenAI returned non-JSON output on attempt=%d: %s",
                    attempt + 1,
                    last_error,
                )
                continue

        raise ValueError(f"Extraction failed after retries: {last_error}")

    def _build_prompt(self, email: EmailMessage, now_local: str, attempt: int) -> str:
        repair_note = ""
        if attempt > 0:
            repair_note = (
                "Previous answer was invalid. Return strict JSON only with no markdown, "
                "no trailing commentary, no extra keys.\n"
            )
        schema_example = {
            "trip_date": "YYYY-MM-DD",
            "departure_time": "HH:MM",
            "timezone": self._timezone,
            "start_address": "Calle Limache 3426, Vina del Mar, Chile",
            "addresses": ["Address 1", "Address 2"],
            "language_detected": "es",
            "confidence": 0.95,
            "warnings": [],
        }
        return (
            f"{repair_note}"
            "Extract route planning data from this email.\n"
            "Rules:\n"
            f"- Assume current local date is {now_local}.\n"
            f"- If year is missing, infer from current date and append warning in `warnings`.\n"
            "- Preserve each address exactly as written.\n"
            "- Extract `start_address` from phrases like `Salida desde:`.\n"
            "- Normalize date to YYYY-MM-DD and time to HH:MM (24h).\n"
            "- Return only JSON with this exact structure:\n"
            f"{json.dumps(schema_example, ensure_ascii=True)}\n\n"
            f"Email subject:\n{email.subject}\n\n"
            f"Email body:\n{email.body_text}\n"
        )

