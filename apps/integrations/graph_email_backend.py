"""Django email backend that sends via Microsoft Graph's sendMail API
instead of SMTP.

Avoids needing SMTP AUTH (increasingly disabled tenant-wide on
Microsoft 365) or an app password. Uses an app-only (client
credentials) Graph app registration with Mail.Send permission, calling
POST /users/{MS_GRAPH_MAIL_SENDER}/sendMail — WGTK's setup authenticates
as MS_GRAPH_MAIL_SENDER (e.g. admin@wgtk.co.uk) but sends with the
message's From set to MS_GRAPH_MAIL_FROM (e.g. parts@wgtk.co.uk, a
shared mailbox the sender has Send As rights on).

Enable by setting EMAIL_BACKEND to
"apps.integrations.graph_email_backend.MicrosoftGraphEmailBackend" and
the MS_GRAPH_MAIL_* settings (see config/settings/base.py).
"""
from __future__ import annotations

import base64

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

_TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_SEND_MAIL_URL_TMPL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"


class MicrosoftGraphEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        try:
            token = self._get_access_token()
        except Exception:
            if self.fail_silently:
                return 0
            raise

        sent_count = 0
        for message in email_messages:
            try:
                self._send_one(message, token)
                sent_count += 1
            except Exception:
                if not self.fail_silently:
                    raise
        return sent_count

    def _get_access_token(self) -> str:
        response = requests.post(
            _TOKEN_URL_TMPL.format(tenant_id=settings.MS_GRAPH_MAIL_TENANT_ID),
            data={
                "client_id": settings.MS_GRAPH_MAIL_CLIENT_ID,
                "client_secret": settings.MS_GRAPH_MAIL_CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def _send_one(self, message, token: str) -> None:
        graph_message = {
            "subject": message.subject,
            "body": {"contentType": "Text", "content": message.body},
            "toRecipients": [
                {"emailAddress": {"address": address}} for address in message.to
            ],
        }
        if settings.MS_GRAPH_MAIL_FROM:
            graph_message["from"] = {
                "emailAddress": {"address": settings.MS_GRAPH_MAIL_FROM}
            }
        if message.attachments:
            graph_message["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": filename,
                    "contentType": mimetype or "application/octet-stream",
                    "contentBytes": base64.b64encode(
                        content if isinstance(content, bytes) else content.encode()
                    ).decode(),
                }
                for filename, content, mimetype in message.attachments
            ]

        response = requests.post(
            _SEND_MAIL_URL_TMPL.format(sender=settings.MS_GRAPH_MAIL_SENDER),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"message": graph_message, "saveToSentItems": "true"},
            timeout=30,
        )
        response.raise_for_status()
