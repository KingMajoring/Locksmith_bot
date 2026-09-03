"""Locksmith self-service portal (/locksmith/) — mobile-first views for
locksmiths (see apps.accounts.middleware.RestrictLocksmithsToPortalMiddleware
and apps.accounts.adapter, which route a Locksmith-linked Microsoft
sign-in here instead of into office/admin).

Two things a locksmith can do:
1. Enter their own latest weekly stock check (stock_check_entry) — same
   WeeklyStockCheck/StockCheckItem data and save logic as the office
   entry_detail view (apps.stock_accuracy.views), just scoped to their
   own checks and served from a simpler template.
2. Dispose parts against a job they're on today (job_detail) — "today"
   comes live from Optimo (list_orders_for_date), not the overnight
   CompletedJob pull, since a job assigned today won't be in that table
   until tomorrow's pull runs.

Office/admin staff can also *preview* the portal as any locksmith (see
start_preview/stop_preview below) without their own login becoming
locksmith-linked — linking would hand them off to
RestrictLocksmithsToPortalMiddleware and lock them out of office/admin
pages until unlinked. Preview mode acts on that locksmith's real data
(real stock checks, real Handl disposal writes), so it's meant for a
deliberate, supervised test against a locksmith who knows it's
happening — not a sandbox.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.integrations.handl import get_handl_client
from apps.integrations.optimo import get_optimo_client
from apps.job_completion.services.pulling import _report_id_from_order_no
from apps.locksmiths.models import Locksmith
from apps.stock_accuracy.models import WeeklyStockCheck

from .models import PortalDisposal

_PREVIEW_SESSION_KEY = "locksmith_portal_preview_id"


def _locksmith_for_request(request):
    """The Locksmith this request acts as: the signed-in user's own
    linked profile, or — for office/admin staff previewing the portal
    (see start_preview) — the one chosen via session, if any."""
    profile = getattr(request.user, "locksmith_profile", None)
    if profile is not None:
        return profile
    if request.user.is_staff:
        preview_id = request.session.get(_PREVIEW_SESSION_KEY)
        if preview_id:
            return Locksmith.objects.filter(pk=preview_id, active=True).first()
    return None


def _is_preview(request) -> bool:
    return getattr(request.user, "locksmith_profile", None) is None


def _no_locksmith_access(request):
    messages.error(
        request,
        "That page is for locksmiths only. Office staff can preview it from a "
        "locksmith's page in the admin (\"Preview portal\").",
    )
    return redirect("stock_accuracy:dashboard")


def _todays_jobs_for(locksmith):
    """(order_no, report_id) pairs for jobs assigned to this locksmith
    today, via Optimo directly — see module docstring for why.

    A locksmith flagged sees_all_jobs_for_testing (an office/admin test
    account exercising the portal, not a real field locksmith) instead
    gets every job scheduled today, unfiltered by driver — they have no
    real Optimo driverSerial of their own to filter by."""
    summaries = get_optimo_client().list_orders_for_date(timezone.localdate())

    if not locksmith.sees_all_jobs_for_testing:
        driver_serials = set(
            locksmith.optimo_driver_ids.values_list("optimo_driver_serial", flat=True)
        )
        if not driver_serials:
            return []
        summaries = [s for s in summaries if s.driver_serial in driver_serials]

    jobs = []
    for summary in summaries:
        report_id = _report_id_from_order_no(summary.order_no)
        if report_id is None:
            continue
        jobs.append({"order_no": summary.order_no, "report_id": report_id})
    return jobs


@login_required
def dashboard(request):
    locksmith = _locksmith_for_request(request)
    if locksmith is None:
        return _no_locksmith_access(request)

    latest_check = locksmith.stock_checks.order_by("-week_starting").first()
    jobs = _todays_jobs_for(locksmith)

    return render(
        request,
        "locksmith_portal/dashboard.html",
        {
            "locksmith": locksmith,
            "latest_check": latest_check,
            "jobs": jobs,
            "today": timezone.localdate(),
            "is_preview": _is_preview(request),
        },
    )


@login_required
def stock_check_entry(request, pk):
    locksmith = _locksmith_for_request(request)
    if locksmith is None:
        return _no_locksmith_access(request)

    weekly_check = get_object_or_404(WeeklyStockCheck, pk=pk, locksmith=locksmith)
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

        messages.success(request, "Counts saved — thanks.")
        return redirect("locksmith_portal:dashboard")

    return render(
        request,
        "locksmith_portal/stock_check.html",
        {
            "weekly_check": weekly_check,
            "items": items,
            "locksmith": locksmith,
            "is_preview": _is_preview(request),
        },
    )


@login_required
def job_detail(request, order_no):
    locksmith = _locksmith_for_request(request)
    if locksmith is None:
        return _no_locksmith_access(request)

    report_id = _report_id_from_order_no(order_no)
    if report_id is None:
        messages.error(request, "That job doesn't have a valid job reference.")
        return redirect("locksmith_portal:dashboard")

    # Confirm this order_no is genuinely one of today's jobs assigned to
    # this locksmith, rather than trusting whatever's in the URL.
    todays = {job["order_no"] for job in _todays_jobs_for(locksmith)}
    if order_no not in todays:
        messages.error(request, "That job isn't on your schedule for today.")
        return redirect("locksmith_portal:dashboard")

    handl = get_handl_client()
    soter_ids = locksmith.soter_id_list
    stock_lines = handl.list_current_stock(soter_ids)

    if request.method == "POST":
        disposed_any = False
        for line in stock_lines:
            raw = request.POST.get(f"dispose_{line.part_code}", "").strip()
            if not raw:
                continue
            try:
                qty = int(raw)
            except ValueError:
                messages.error(request, f"'{raw}' isn't a valid quantity for {line.part_code}.")
                continue
            if qty <= 0:
                continue
            if qty > line.qty:
                messages.error(
                    request,
                    f"You only have {line.qty} of {line.part_code} in stock — "
                    f"can't dispose {qty}.",
                )
                continue

            # Which of a locksmith's (usually two, "(V)"/"(A)") Soter IDs
            # a disposal should be written against isn't confirmed — see
            # apps.integrations.handl.SQLHandlClient.record_disposal's
            # docstring. Using the first configured one for now; needs a
            # supervised test disposal before this is trusted for real.
            soter_id = soter_ids[0] if soter_ids else ""
            disposal = PortalDisposal.objects.create(
                locksmith=locksmith,
                created_by=request.user,
                order_no=order_no,
                report_id=report_id,
                part_code=line.part_code,
                part_name=line.part_name,
                quantity=qty,
            )
            try:
                handl.record_disposal(soter_id, report_id, line.part_code, qty)
            except Exception as exc:
                disposal.handl_error = str(exc)
                disposal.save(update_fields=["handl_error"])
                messages.warning(
                    request,
                    f"Recorded {qty} x {line.part_code} disposed, but something went "
                    "wrong saving it to Soter — the office will follow up.",
                )
            else:
                disposal.handl_synced = True
                disposal.save(update_fields=["handl_synced"])
                disposed_any = True

        if disposed_any:
            messages.success(request, "Parts disposed and saved.")
        return redirect("locksmith_portal:job_detail", order_no=order_no)

    return render(
        request,
        "locksmith_portal/job_detail.html",
        {
            "order_no": order_no,
            "report_id": report_id,
            "stock_lines": stock_lines,
            "locksmith": locksmith,
            "is_preview": _is_preview(request),
        },
    )


@login_required
def start_preview(request, locksmith_id):
    """Lets office/admin staff view the portal as a chosen locksmith,
    without their own login becoming locksmith-linked (which would hand
    them off to RestrictLocksmithsToPortalMiddleware and lock them out
    of office/admin pages). Linked from the locksmith's admin page.

    Acts on that locksmith's real data — real stock checks, real Handl
    disposal writes — so this is for a deliberate, supervised test, not
    a sandbox."""
    if not request.user.is_staff:
        messages.error(request, "Only office/admin staff can preview the portal.")
        return redirect("stock_accuracy:dashboard")

    locksmith = get_object_or_404(Locksmith, pk=locksmith_id, active=True)
    request.session[_PREVIEW_SESSION_KEY] = locksmith.pk
    messages.warning(
        request,
        f"Previewing the portal as {locksmith.name} — stock counts and disposals "
        "here affect their real data. Use \"Stop previewing\" when you're done.",
    )
    return redirect("locksmith_portal:dashboard")


@login_required
def stop_preview(request):
    request.session.pop(_PREVIEW_SESSION_KEY, None)
    return redirect("stock_accuracy:dashboard")
