from __future__ import annotations

import logging
from uuid import uuid4

from tsp_email_optimizer.domain.models import RequestStatus
from tsp_email_optimizer.domain.ports import (
    EmailInbox,
    EmailReplySender,
    RouteMapRenderer,
    RouteOptimizer,
    TripExtractor,
    TripRepository,
)
from tsp_email_optimizer.services.reply_builder import ReplyBuilder

logger = logging.getLogger(__name__)


class EmailOptimizationPipeline:
    def __init__(
        self,
        inbox: EmailInbox,
        sender: EmailReplySender,
        extractor: TripExtractor,
        optimizer: RouteOptimizer,
        repository: TripRepository,
        reply_builder: ReplyBuilder,
        map_renderer: RouteMapRenderer | None = None,
    ) -> None:
        self._inbox = inbox
        self._sender = sender
        self._extractor = extractor
        self._optimizer = optimizer
        self._repository = repository
        self._reply_builder = reply_builder
        self._map_renderer = map_renderer

    def process_next(self) -> bool:
        logger.info("Polling inbox for unread candidate emails.")
        emails = self._inbox.fetch_unprocessed(limit=1)
        if not emails:
            logger.info("No unread emails found.")
            return False

        email = emails[0]
        request_id = str(uuid4())
        logger.info(
            "Selected email uid=%s request_id=%s subject=%r sender=%r",
            email.uid,
            request_id,
            email.subject,
            email.sender,
        )

        try:
            logger.info("Step 1/6 Extracting structured trip data from email.")
            trip = self._extractor.extract(email=email, request_id=request_id)
            logger.info(
                "Extraction succeeded request_id=%s addresses=%d confidence=%.2f",
                request_id,
                len(trip.addresses),
                trip.confidence,
            )

            logger.info("Step 2/6 Saving extracted request to CSV.")
            self._repository.save_request(trip=trip, status=RequestStatus.EXTRACTED)

            if len(trip.addresses) < 2:
                raise ValueError("At least 2 addresses are required for optimization.")

            logger.info("Step 3/6 Running route optimization.")
            route = self._optimizer.optimize(trip)
            logger.info(
                "Optimization succeeded request_id=%s optimized_stops=%d distance_m=%s duration_s=%s",
                request_id,
                len(route.ordered_stops),
                route.total_distance_m,
                route.total_duration_s,
            )
            logger.info("Step 4/6 Saving optimized route to CSV.")
            self._repository.save_optimized_route(route)
            self._repository.update_request_status(request_id, RequestStatus.OPTIMIZED)

            map_path: str | None = None
            if self._map_renderer is not None:
                logger.info("Step 5/6 Generating route map.")
                try:
                    map_path = self._map_renderer.render(route)
                except Exception:  # noqa: BLE001 - non-critical visualization
                    logger.exception(
                        "Map generation failed request_id=%s; continuing without map.",
                        request_id,
                    )

            logger.info("Step 6/6 Sending reply email.")
            reply_subject = self._build_reply_subject(email.subject)
            reply_body = self._reply_builder.build_success_reply(
                trip=trip,
                route=route,
                map_path=map_path,
            )
            self._sender.reply(original_email=email, subject=reply_subject, body=reply_body)

            self._repository.update_request_status(request_id, RequestStatus.REPLIED)
            self._inbox.mark_processed(email.uid)
            logger.info("Processing completed successfully request_id=%s", request_id)
            return True

        except Exception as exc:  # noqa: BLE001 - keep pipeline resilient
            logger.exception(
                "Pipeline failed for email uid=%s request_id=%s", email.uid, request_id
            )
            try:
                self._handle_failure(email=email, request_id=request_id, error=exc)
            except Exception:  # noqa: BLE001 - don't let failure handling crash app
                logger.exception(
                    "Failure handling crashed for email uid=%s request_id=%s",
                    email.uid,
                    request_id,
                )
            try:
                self._inbox.mark_processed(email.uid)
            except Exception:  # noqa: BLE001 - best effort finalization
                logger.exception("Could not mark failed email as seen uid=%s", email.uid)
            return True

    def _handle_failure(self, email, request_id: str, error: Exception) -> None:
        reason = str(error)
        logger.error("Handling failure request_id=%s reason=%s", request_id, reason)
        status = (
            RequestStatus.EXTRACTION_REVIEW_NEEDED
            if "extract" in reason.lower() or "address" in reason.lower()
            else RequestStatus.FAILED
        )
        body = self._reply_builder.build_clarification_reply(reason=reason)
        subject = self._build_reply_subject(email.subject)
        logger.info("Sending clarification reply request_id=%s", request_id)
        self._sender.reply(original_email=email, subject=subject, body=body)
        logger.info("Clarification reply sent request_id=%s", request_id)

        logger.info("Updating failure status request_id=%s status=%s", request_id, status.value)
        try:
            self._repository.update_request_status(request_id, status, status_note=reason)
        except Exception:  # noqa: BLE001 - keep user notification path robust
            logger.exception(
                "Could not update request status after failure request_id=%s", request_id
            )

    @staticmethod
    def _build_reply_subject(original_subject: str) -> str:
        if original_subject.lower().startswith("re:"):
            return original_subject
        return f"Re: {original_subject}"

