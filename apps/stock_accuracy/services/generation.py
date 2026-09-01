"""Pick this week's 10 lines for a locksmith and create the WeeklyStockCheck.

Selection rule: rank the locksmith's parts by usage over a trailing window
(STOCK_CHECK_USAGE_WINDOW_DAYS) to build a fast-movers pool, exclude lines
checked in the last STOCK_CHECK_NO_REPEAT_WEEKS weeks so coverage rotates
across the pool, then randomly draw STOCK_CHECK_LINES_PER_WEEK from what's
left. If exclusion leaves too few candidates, top up with the
least-recently-checked excluded ones so a full check always goes out.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from django.conf import settings
from django.utils import timezone

from apps.integrations.handl import get_handl_client
from apps.locksmiths.models import Locksmith

from ..models import StockCheckItem, WeeklyStockCheck


def _recently_checked_part_codes(locksmith: Locksmith, weeks: int) -> set[str]:
    cutoff = timezone.now().date() - timedelta(weeks=weeks)
    return set(
        StockCheckItem.objects.filter(
            weekly_check__locksmith=locksmith,
            weekly_check__week_starting__gte=cutoff,
        ).values_list("part_code", flat=True)
    )


def _choose_lines(locksmith: Locksmith) -> list:
    handl = get_handl_client()
    since = date.today() - timedelta(days=settings.STOCK_CHECK_USAGE_WINDOW_DAYS)
    usage = handl.get_stock_usage(locksmith.soter_id_list, since)
    usage_sorted = sorted(usage, key=lambda u: u.qty_used, reverse=True)
    pool = usage_sorted[: settings.STOCK_CHECK_POOL_SIZE]

    recently_checked = _recently_checked_part_codes(
        locksmith, settings.STOCK_CHECK_NO_REPEAT_WEEKS
    )
    eligible = [u for u in pool if u.part_code not in recently_checked]
    excluded = [u for u in pool if u.part_code in recently_checked]

    lines_needed = settings.STOCK_CHECK_LINES_PER_WEEK
    if len(eligible) < lines_needed:
        eligible = eligible + excluded[: lines_needed - len(eligible)]

    k = min(lines_needed, len(eligible))
    return random.sample(eligible, k=k)


def generate_weekly_check(locksmith: Locksmith, week_starting: date) -> WeeklyStockCheck:
    existing = WeeklyStockCheck.objects.filter(
        locksmith=locksmith, week_starting=week_starting
    ).first()
    if existing:
        return existing

    chosen = _choose_lines(locksmith)
    handl = get_handl_client()
    expected = handl.get_expected_stock(
        locksmith.soter_id_list, [u.part_code for u in chosen]
    )

    weekly_check = WeeklyStockCheck.objects.create(
        locksmith=locksmith, week_starting=week_starting
    )
    StockCheckItem.objects.bulk_create(
        StockCheckItem(
            weekly_check=weekly_check,
            part_code=u.part_code,
            part_name=u.part_name,
            expected_qty=expected[u.part_code].expected_qty,
            unit_cost=expected[u.part_code].unit_cost,
        )
        for u in chosen
    )
    return weekly_check
