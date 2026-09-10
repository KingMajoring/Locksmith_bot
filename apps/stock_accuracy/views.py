from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.integrations.handl import get_handl_client
from apps.locksmiths.models import Locksmith

from .models import WeeklyStockCheck
from .services.reporting import flagged_items_queryset, line_summary, locksmith_summary


@login_required
def dashboard(request):
    pending = (
        WeeklyStockCheck.objects.filter(completed_at__isnull=True)
        .select_related("locksmith")
        .order_by("week_starting")
    )
    locksmiths = Locksmith.objects.filter(active=True)
    summaries = [locksmith_summary(l) for l in locksmiths]
    top_lines = line_summary()[:10]
    return render(
        request,
        "stock_accuracy/dashboard.html",
        {
            "pending": pending,
            "summaries": summaries,
            "top_lines": top_lines,
        },
    )


@login_required
def entry_detail(request, pk):
    weekly_check = get_object_or_404(WeeklyStockCheck, pk=pk)
    items = weekly_check.items.all()

    if request.method == "POST":
        for item in items:
            raw = request.POST.get(f"qty_{item.id}", "").strip()
            if raw == "":
                continue
            try:
                qty = int(raw)
            except ValueError:
                messages.error(request, f"'{raw}' isn't a valid quantity for {item.part_code}.")
                continue
            item.actual_qty = qty
            item.entered_by = request.user
            item.entered_at = timezone.now()
            item.save(update_fields=["actual_qty", "entered_by", "entered_at"])

        weekly_check.status = WeeklyStockCheck.Status.AWAITING_ENTRY
        if weekly_check.is_fully_entered:
            weekly_check.status = WeeklyStockCheck.Status.COMPLETED
            weekly_check.completed_at = timezone.now()
        weekly_check.save(update_fields=["status", "completed_at"])

        messages.success(request, "Counts saved.")
        return redirect("stock_accuracy:dashboard")

    return render(
        request,
        "stock_accuracy/entry_detail.html",
        {"weekly_check": weekly_check, "items": items},
    )


@login_required
@require_POST
def confirm_check(request, pk):
    """Office has reviewed (and, if needed, corrected via entry_detail)
    every counted quantity and wants Handl's own
    Inventory_Locksmith_Stock to actually match it — not just noted as
    a variance in our own reporting (entry_detail alone only ever
    writes to StockCheckItem.actual_qty here, never to Handl). Pushes
    each line's actual_qty to Handl one at a time, best-effort per
    line, so one bad line doesn't block the rest."""
    weekly_check = get_object_or_404(WeeklyStockCheck, pk=pk)
    if not weekly_check.is_fully_entered:
        messages.error(request, "Enter a count for every line before confirming.")
        return redirect("stock_accuracy:entry_detail", pk=pk)

    handl = get_handl_client()
    locksmith = weekly_check.locksmith
    actioned_by = locksmith.soter_user_id or settings.HANDL_PORTAL_CREATED_BY_USER_ID

    error_count = 0
    for item in weekly_check.items.all():
        try:
            handl.set_locksmith_stock_quantity(
                locksmith.soter_id_list, item.part_code, item.actual_qty,
                actioned_by_user_id=actioned_by,
                locksmith_display_name=locksmith.van_soter_display_name,
            )
            item.handl_synced = True
            item.handl_error = ""
        except Exception as exc:
            item.handl_synced = False
            item.handl_error = str(exc)
            error_count += 1
        item.save(update_fields=["handl_synced", "handl_error"])

    weekly_check.confirmed_at = timezone.now()
    weekly_check.confirmed_by = request.user
    weekly_check.save(update_fields=["confirmed_at", "confirmed_by"])

    if error_count:
        messages.error(
            request,
            f"Confirmed, but {error_count} line(s) couldn't be updated in Handl — "
            "see the errors below.",
        )
    else:
        messages.success(request, "Confirmed — Handl's stock now matches these counts.")
    return redirect("stock_accuracy:entry_detail", pk=pk)


@login_required
def locksmith_report(request, pk):
    locksmith = get_object_or_404(Locksmith, pk=pk)
    summary = locksmith_summary(locksmith)
    flagged = [
        item
        for item in flagged_items_queryset(weeks=12)
        if item.weekly_check.locksmith_id == locksmith.id
    ]
    return render(
        request,
        "stock_accuracy/locksmith_report.html",
        {"locksmith": locksmith, "summary": summary, "flagged": flagged},
    )
