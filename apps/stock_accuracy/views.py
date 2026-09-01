from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.locksmiths.models import Locksmith

from .models import WeeklyStockCheck
from .services.reporting import flagged_items_queryset, line_summary, locksmith_summary


@login_required
def dashboard(request):
    pending = (
        WeeklyStockCheck.objects.filter(sent_at__isnull=False, completed_at__isnull=True)
        .select_related("locksmith")
        .order_by("week_starting")
    )
    locksmiths = Locksmith.objects.filter(active=True)
    summaries = [locksmith_summary(l) for l in locksmiths]
    top_lines = line_summary()[:10]
    return render(
        request,
        "stock_accuracy/dashboard.html",
        {"pending": pending, "summaries": summaries, "top_lines": top_lines},
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
