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

from ..models import PartUnitConversion, StockCheckItem, VirtualStockItem, WeeklyStockCheck


def _recently_checked_part_codes(locksmith: Locksmith, weeks: int) -> set[str]:
    cutoff = timezone.now().date() - timedelta(weeks=weeks)
    return set(
        StockCheckItem.objects.filter(
            weekly_check__locksmith=locksmith,
            weekly_check__week_starting__gte=cutoff,
        ).values_list("part_code", flat=True)
    )


def _choose_lines(locksmith: Locksmith, handl) -> tuple[list, dict]:
    """Returns (chosen StockUsage lines, their ExpectedStock lookup) —
    the lookup is returned alongside so generate_weekly_check doesn't
    need a second Handl round trip for stock already fetched here."""
    since = date.today() - timedelta(days=settings.STOCK_CHECK_USAGE_WINDOW_DAYS)
    usage = handl.get_stock_usage(locksmith.soter_id_list, since)

    # Some Handl SKUs (e.g. a 3D job token) get "disposed" against jobs
    # for tracking/billing but aren't physical van stock a locksmith
    # actually holds — asking them to count it makes no sense. Admin
    # manages the excluded list (see VirtualStockItem), matched
    # case-insensitively since SKU casing isn't guaranteed consistent.
    virtual_codes = {
        code.upper() for code in VirtualStockItem.objects.values_list("part_code", flat=True)
    }
    usage = [u for u in usage if u.part_code.upper() not in virtual_codes]

    usage_sorted = sorted(usage, key=lambda u: u.qty_used, reverse=True)
    pool = usage_sorted[: settings.STOCK_CHECK_POOL_SIZE]

    # get_stock_usage ranks by *disposal* history, which isn't the same
    # as "ever held as real van stock" — most notably a client-supplied
    # part (see HandlClient.record_client_supplied_disposal) is logged
    # as a disposal for reporting but deliberately never touches
    # Inventory_Locksmith_Stock, since it never came out of this
    # locksmith's van in the first place. Reported live: asking a
    # locksmith to physically count something that was never tracked as
    # their stock makes no sense. get_expected_stock's own result is
    # the ground truth for "has an Inventory_Locksmith_Stock row at
    # all" (even one at 0 still appears — only a SKU with zero matching
    # rows is absent), so only offer parts that show up in it.
    expected = handl.get_expected_stock(locksmith.soter_id_list, [u.part_code for u in pool])
    pool = [u for u in pool if u.part_code in expected]

    recently_checked = _recently_checked_part_codes(
        locksmith, settings.STOCK_CHECK_NO_REPEAT_WEEKS
    )
    eligible = [u for u in pool if u.part_code not in recently_checked]
    excluded = [u for u in pool if u.part_code in recently_checked]

    lines_needed = settings.STOCK_CHECK_LINES_PER_WEEK
    if len(eligible) < lines_needed:
        eligible = eligible + excluded[: lines_needed - len(eligible)]

    k = min(lines_needed, len(eligible))
    return random.sample(eligible, k=k), expected


def generate_weekly_check(locksmith: Locksmith, week_starting: date) -> WeeklyStockCheck:
    existing = WeeklyStockCheck.objects.filter(
        locksmith=locksmith, week_starting=week_starting
    ).first()
    if existing:
        return existing

    handl = get_handl_client()
    chosen, expected = _choose_lines(locksmith, handl)

    # Some SKUs (e.g. VXRP2 — pins sold to locksmiths in packs of 10)
    # are recorded in Handl by the pack, but a locksmith physically
    # counting van stock counts individual pins, not packs — comparing
    # Handl's expected_qty directly against that count produced a
    # wildly wrong variance. Applied once here, at generation time, to
    # the frozen expected_qty/unit_cost, so it's comparable to the
    # individual-unit count the locksmith actually enters (unit_cost is
    # divided by the same factor so £ impact stays meaningful in those
    # same individual units).
    conversions = {
        c.part_code.upper(): c.units_per_pack
        for c in PartUnitConversion.objects.all()
    }

    weekly_check = WeeklyStockCheck.objects.create(
        locksmith=locksmith, week_starting=week_starting
    )
    StockCheckItem.objects.bulk_create(
        StockCheckItem(
            weekly_check=weekly_check,
            part_code=u.part_code,
            part_name=u.part_name,
            expected_qty=(
                expected[u.part_code].expected_qty
                * conversions.get(u.part_code.upper(), 1)
            ),
            unit_cost=(
                expected[u.part_code].unit_cost
                / conversions.get(u.part_code.upper(), 1)
            ),
        )
        for u in chosen
    )
    return weekly_check
