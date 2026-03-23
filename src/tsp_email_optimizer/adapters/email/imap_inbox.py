from __future__ import annotations

import imaplib
import logging
from datetime import datetime
from email import message_from_bytes, policy
from email.message import Message
from email.utils import parsedate_to_datetime

from tsp_email_optimizer.domain.models import EmailMessage

logger = logging.getLogger(__name__)


class ImapInbox:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        mailbox: str = "INBOX",
        subject_contains: str | None = None,
        unread_scan_limit: int = 200,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._mailbox = mailbox
        self._subject_contains = subject_contains.strip().lower() if subject_contains else None
        self._unread_scan_limit = max(1, unread_scan_limit)

    def fetch_unprocessed(self, limit: int = 1) -> list[EmailMessage]:
        logger.info(
            "Checking mailbox=%r for unread emails (subject filter=%r, limit=%d, scan_limit=%d).",
            self._mailbox,
            self._subject_contains,
            limit,
            self._unread_scan_limit,
        )
        with self._connect() as conn:
            conn.select(self._mailbox)
            status, data = conn.search(None, "UNSEEN")
            if status != "OK" or not data or not data[0]:
                logger.info("No unread IMAP messages returned by search.")
                return []

            unread_uids = data[0].split()
            inspect_count = (
                min(len(unread_uids), limit)
                if not self._subject_contains
                else min(len(unread_uids), self._unread_scan_limit)
            )
            # Inspect newest unread first to prioritize fresh test emails.
            uids = unread_uids[-inspect_count:][::-1]
            logger.info(
                "IMAP search found unread_total=%d; inspecting newest=%d.",
                len(unread_uids),
                len(uids),
            )
            messages: list[EmailMessage] = []
            mismatch_count = 0
            sample_mismatched_subjects: list[str] = []
            for uid_bytes in uids:
                uid = uid_bytes.decode("utf-8")
                fetch_status, fetched = conn.fetch(uid, "(RFC822)")
                if fetch_status != "OK" or not fetched:
                    logger.warning("Failed to fetch email uid=%s; skipping.", uid)
                    continue
                raw = fetched[0][1]
                if not raw:
                    logger.warning("Email uid=%s has empty raw payload; skipping.", uid)
                    continue
                parsed = message_from_bytes(raw, policy=policy.default)
                email = self._to_email_message(uid=uid, parsed=parsed)
                if self._subject_contains:
                    subject = (email.subject or "").lower()
                    if self._subject_contains not in subject:
                        mismatch_count += 1
                        if len(sample_mismatched_subjects) < 5:
                            sample_mismatched_subjects.append(self._shorten(email.subject))
                        logger.debug(
                            "Skipping uid=%s due to subject mismatch subject=%r filter=%r",
                            uid,
                            email.subject,
                            self._subject_contains,
                        )
                        continue
                messages.append(email)
                logger.info("Accepted candidate uid=%s subject=%r", uid, email.subject)
                if len(messages) >= limit:
                    break
            if not messages and self._subject_contains:
                logger.info(
                    "No subject matches found for filter=%r after inspecting=%d unread messages. "
                    "Mismatches=%d sample_subjects=%r",
                    self._subject_contains,
                    len(uids),
                    mismatch_count,
                    sample_mismatched_subjects,
                )
            logger.info("Returning %d matching unread email(s).", len(messages))
            return messages

    def mark_processed(self, uid: str) -> None:
        logger.info("Marking email uid=%s as seen.", uid)
        with self._connect() as conn:
            conn.select(self._mailbox)
            conn.store(uid, "+FLAGS", "\\Seen")

    def _connect(self) -> imaplib.IMAP4_SSL:
        logger.debug("Opening IMAP SSL connection to host=%r port=%d", self._host, self._port)
        conn = imaplib.IMAP4_SSL(self._host, self._port)
        conn.login(self._username, self._password)
        logger.debug("IMAP login successful for user=%r", self._username)
        return conn

    @staticmethod
    def _shorten(value: str | None, max_len: int = 80) -> str:
        if not value:
            return ""
        text = value.strip()
        return text if len(text) <= max_len else f"{text[:max_len]}..."

    def _to_email_message(self, uid: str, parsed: Message) -> EmailMessage:
        subject = parsed.get("Subject", "")
        sender = parsed.get("From", "")
        message_id = parsed.get("Message-ID", "")
        in_reply_to = parsed.get("In-Reply-To")
        references = parsed.get("References")
        date_header = parsed.get("Date")
        received_at = datetime.now()
        if date_header:
            try:
                received_at = parsedate_to_datetime(date_header)
            except (TypeError, ValueError):
                received_at = datetime.now()

        body_text = self._extract_body(parsed)
        return EmailMessage(
            uid=uid,
            subject=subject,
            sender=sender,
            body_text=body_text,
            message_id=message_id,
            in_reply_to=in_reply_to,
            references=references,
            received_at=received_at,
        )

    @staticmethod
    def _extract_body(msg: Message) -> str:
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = part.get("Content-Disposition", "")
                if content_type == "text/plain" and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
            return ""

        payload = msg.get_payload(decode=True)
        if not payload:
            return ""
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")

