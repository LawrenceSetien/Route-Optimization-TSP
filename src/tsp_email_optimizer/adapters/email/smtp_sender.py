from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage as SmtpEmailMessage
from email.utils import parseaddr

from tsp_email_optimizer.domain.models import EmailMessage

logger = logging.getLogger(__name__)


class SmtpReplySender:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password

    def reply(self, original_email: EmailMessage, subject: str, body: str) -> None:
        recipient = parseaddr(original_email.sender)[1] or original_email.sender
        logger.info("Preparing SMTP reply recipient=%r subject=%r", recipient, subject)
        msg = SmtpEmailMessage()
        msg["From"] = self._username
        msg["To"] = recipient
        msg["Subject"] = subject
        if original_email.message_id:
            msg["In-Reply-To"] = original_email.message_id
            refs = original_email.references or ""
            msg["References"] = f"{refs} {original_email.message_id}".strip()
        msg.set_content(body)

        with smtplib.SMTP(self._host, self._port) as smtp:
            smtp.starttls()
            smtp.login(self._username, self._password)
            smtp.send_message(msg)
        logger.info("SMTP reply sent recipient=%r", recipient)

