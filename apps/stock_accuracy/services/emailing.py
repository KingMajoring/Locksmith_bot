from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from ..models import EmailSettings, WeeklyStockCheck
from .excel_export import build_workbook


def send_weekly_check(weekly_check: WeeklyStockCheck) -> None:
    locksmith = weekly_check.locksmith
    if not locksmith.email:
        raise ValueError(f"{locksmith} has no email address on file.")

    workbook = build_workbook(weekly_check)
    filename = f"stock-check-{locksmith.name.replace(' ', '_')}-{weekly_check.week_starting}.xlsx"
    subject = f"Weekly stock check — week commencing {weekly_check.week_starting:%d %b %Y}"

    # Pre-go-live safety net: while EmailSettings.emails_live is off
    # (the "Emails live" toggle on the Stock Accuracy dashboard), every
    # stock check goes to STOCK_CHECK_TEST_REDIRECT_EMAIL instead of the
    # real locksmith, clearly labelled with who it would really have
    # gone to — lets office admin verify real SMTP delivery without
    # locksmiths receiving test emails. Refuses to send at all (rather
    # than silently falling through to the real locksmith) if emails
    # aren't live and no test address is configured.
    if EmailSettings.current().emails_live:
        recipient = locksmith.email
    else:
        redirect_to = settings.STOCK_CHECK_TEST_REDIRECT_EMAIL
        if not redirect_to:
            raise ValueError(
                "Emails are not live (see the Stock Accuracy dashboard toggle) "
                "and no STOCK_CHECK_TEST_REDIRECT_EMAIL is configured — refusing "
                "to send to avoid emailing a real locksmith by accident."
            )
        recipient = redirect_to
        subject = f"[TEST — would go to {locksmith.email}] {subject}"

    message = EmailMessage(
        subject=subject,
        body=(
            f"Hi {locksmith.name},\n\n"
            "Please count the parts on the attached sheet and reply with the "
            "figures filled in.\n\nThanks,\nWGTK Ops"
        ),
        to=[recipient],
    )
    message.attach(
        filename,
        workbook.read(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    message.send()

    weekly_check.status = WeeklyStockCheck.Status.SENT
    weekly_check.sent_at = timezone.now()
    weekly_check.save(update_fields=["status", "sent_at"])
