from django.core.mail import EmailMessage
from django.utils import timezone

from ..models import WeeklyStockCheck
from .excel_export import build_workbook


def send_weekly_check(weekly_check: WeeklyStockCheck) -> None:
    locksmith = weekly_check.locksmith
    if not locksmith.email:
        raise ValueError(f"{locksmith} has no email address on file.")

    workbook = build_workbook(weekly_check)
    filename = f"stock-check-{locksmith.name.replace(' ', '_')}-{weekly_check.week_starting}.xlsx"

    message = EmailMessage(
        subject=f"Weekly stock check — week commencing {weekly_check.week_starting:%d %b %Y}",
        body=(
            f"Hi {locksmith.name},\n\n"
            "Please count the parts on the attached sheet and reply with the "
            "figures filled in.\n\nThanks,\nWGTK Ops"
        ),
        to=[locksmith.email],
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
