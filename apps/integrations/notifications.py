"""Lone-worker safety alerts for the locksmith portal (panic button,
overdue-job escalation — see apps.locksmith_portal.views/models and
the check_overdue_visits management command).

Ships as SMS via Twilio to start (works same-day, no separate
approval); WhatsApp needs a WhatsApp Business sender approved through
Meta first, which can take a few days even once Twilio itself is set
up. send_message() is deliberately channel-agnostic so swapping SMS
for WhatsApp later (e.g. by sending "whatsapp:+..." numbers through
Twilio's WhatsApp API instead) doesn't need any change at the call
sites.

Until TWILIO_ACCOUNT_SID/AUTH_TOKEN/SMS_FROM are all set,
get_notification_service() returns MockNotificationService (just
logs), so the rest of this can be built/tested without a real Twilio
account.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from django.conf import settings

logger = logging.getLogger(__name__)


class NotificationService(ABC):
    @abstractmethod
    def send_message(self, to: str, body: str) -> None:
        """Sends one alert to a phone number. Best-effort by
        convention at the call site (a failed safety alert shouldn't
        itself raise and block the locksmith) — but raises here so the
        caller can decide how to handle/log a delivery failure."""


class MockNotificationService(NotificationService):
    """Local dev/tests: just logs — no real message sent."""

    def send_message(self, to: str, body: str) -> None:
        logger.info("MockNotificationService: would send to %s: %s", to, body)


class TwilioNotificationService(NotificationService):
    """Real Twilio-backed implementation. The twilio package is
    imported lazily so importing this module (e.g. for
    MockNotificationService in dev/tests) doesn't require it to be
    installed until it's actually needed."""

    def send_message(self, to: str, body: str) -> None:
        from twilio.rest import Client

        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        client.messages.create(to=to, from_=settings.TWILIO_SMS_FROM, body=body)


def get_notification_service() -> NotificationService:
    if settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and settings.TWILIO_SMS_FROM:
        return TwilioNotificationService()
    return MockNotificationService()
