from datetime import date

from django.db import migrations

# WGTK's real pay-run cut-off dates, as given by office — don't line up
# with calendar months (e.g. "January" runs 16 Dec-19 Jan), so this is
# the actual pay-run calendar, not something computable from a rule.
_PAY_PERIODS = [
    (date(2025, 12, 16), date(2026, 1, 19)),
    (date(2026, 1, 20), date(2026, 2, 16)),
    (date(2026, 2, 17), date(2026, 3, 18)),
    (date(2026, 3, 19), date(2026, 4, 17)),
    (date(2026, 4, 18), date(2026, 5, 18)),
    (date(2026, 5, 19), date(2026, 6, 18)),
    (date(2026, 6, 19), date(2026, 7, 20)),
    (date(2026, 7, 21), date(2026, 8, 17)),
    (date(2026, 8, 18), date(2026, 9, 18)),
    (date(2026, 9, 19), date(2026, 10, 19)),
    (date(2026, 10, 20), date(2026, 11, 18)),
    (date(2026, 11, 19), date(2026, 12, 14)),
]


def seed_pay_periods(apps, schema_editor):
    PayPeriod = apps.get_model("locksmith_portal", "PayPeriod")
    PayPeriod.objects.bulk_create(
        [PayPeriod(start_date=start, end_date=end) for start, end in _PAY_PERIODS],
        ignore_conflicts=True,
    )


def unseed_pay_periods(apps, schema_editor):
    PayPeriod = apps.get_model("locksmith_portal", "PayPeriod")
    PayPeriod.objects.filter(start_date__in=[start for start, _end in _PAY_PERIODS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("locksmith_portal", "0022_payperiod"),
    ]

    operations = [
        migrations.RunPython(seed_pay_periods, unseed_pay_periods),
    ]
