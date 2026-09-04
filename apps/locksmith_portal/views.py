"""Locksmith self-service portal (/locksmith/) — mobile-first views for
locksmiths (see apps.accounts.middleware.RestrictLocksmithsToPortalMiddleware
and apps.accounts.adapter, which route a Locksmith-linked Microsoft
sign-in here instead of into office/admin).

Two things a locksmith can do:
1. Enter their own latest weekly stock check (stock_check_entry) — same
   WeeklyStockCheck/StockCheckItem data and save logic as the office
   entry_detail view (apps.stock_accuracy.views), just scoped to their
   own checks and served from a simpler template.
2. Dispose parts against a job they're on today, or page back to a past
   day to add/review disposals there (job_detail) — the job list comes
   live from Optimo (list_orders_for_date) rather than the overnight
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

import logging
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.http import require_POST

from apps.integrations.handl import get_handl_client
from apps.integrations.optimo import get_optimo_client
from apps.integrations.photos import get_photo_storage
from apps.job_completion.services.pulling import _report_id_from_order_no
from apps.locksmiths.models import Locksmith
from apps.stock_accuracy.models import WeeklyStockCheck

from .models import JobVisit, JobVisitPhoto, PortalDisposal

logger = logging.getLogger(__name__)

_PREVIEW_SESSION_KEY = "locksmith_portal_preview_id"
MAX_PHOTO_BYTES = 15 * 1024 * 1024


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


def _jobs_for_date(locksmith, for_date):
    """(order_no, report_id) pairs for jobs assigned to this locksmith
    on for_date, via Optimo directly — see module docstring for why.
    Used both for today's jobs and for past days a locksmith pages back
    to (see _selected_date).

    A locksmith flagged sees_all_jobs_for_testing (an office/admin test
    account exercising the portal, not a real field locksmith) instead
    gets every job scheduled that day, unfiltered by driver — they have
    no real Optimo driverSerial of their own to filter by."""
    summaries = get_optimo_client().list_orders_for_date(for_date)

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


def _selected_date(request):
    """The day being viewed on the portal (dashboard/job list), from
    ?date=YYYY-MM-DD — defaults to, and is clamped to never go beyond,
    today (so a locksmith can page back through past days to add or
    review disposals, but not into jobs Optimo hasn't scheduled yet)."""
    today = timezone.localdate()
    raw = request.GET.get("date")
    if not raw:
        return today
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return today
    return min(parsed, today)


def _disposal_totals(locksmith, order_nos):
    """order_no -> {"quantity": total parts disposed, "parts": distinct
    disposal lines} for jobs already actioned through the portal — the
    green tick + count shown against each job on the dashboard."""
    if not order_nos:
        return {}
    rows = (
        PortalDisposal.objects.filter(locksmith=locksmith, order_no__in=order_nos)
        .values("order_no")
        .annotate(quantity=Sum("quantity"), parts=Count("id"))
    )
    return {row["order_no"]: row for row in rows}


def _job_visit_context(request, order_no):
    """Common setup for every on-route/arrived/parts/complete view:
    resolves the locksmith, report_id, and selected day, confirms the
    job is genuinely scheduled for them that day, and gets-or-creates
    this locksmith's JobVisit for it. Returns (context_dict, None) on
    success, or (None, redirect_response) — having already set the
    appropriate message — if any of that fails, so callers just do
    `ctx, early = _job_visit_context(...); if ctx is None: return early`."""
    locksmith = _locksmith_for_request(request)
    if locksmith is None:
        return None, _no_locksmith_access(request)

    report_id = _report_id_from_order_no(order_no)
    if report_id is None:
        messages.error(request, "That job doesn't have a valid job reference.")
        return None, redirect("locksmith_portal:dashboard")

    selected_date = _selected_date(request)
    dashboard_url = f"{reverse('locksmith_portal:dashboard')}?date={selected_date.isoformat()}"

    scheduled = {job["order_no"] for job in _jobs_for_date(locksmith, selected_date)}
    if order_no not in scheduled:
        messages.error(request, "That job isn't on your schedule for that day.")
        return None, redirect(dashboard_url)

    visit, _created = JobVisit.objects.get_or_create(
        locksmith=locksmith, order_no=order_no, defaults={"report_id": report_id}
    )

    return {
        "locksmith": locksmith,
        "report_id": report_id,
        "selected_date": selected_date,
        "dashboard_url": dashboard_url,
        "visit": visit,
    }, None


def _write_handl_note(locksmith, report_id, text):
    """Best-effort — a Handl note recording a job-visit stage is useful
    but not critical the way a disposal's stock/financial write is, so
    a failure here is logged (visible in App Service logs / Application
    Insights) rather than surfaced to the locksmith or blocking their
    progress through the job."""
    handl = get_handl_client()
    actioned_by = locksmith.soter_user_id or settings.HANDL_PORTAL_CREATED_BY_USER_ID
    try:
        handl.add_report_note(report_id, text, actioned_by_user_id=actioned_by)
    except Exception:
        logger.exception("Failed to write Handl note for report %s", report_id)


def _photo_links_html(urls):
    """Handl's own Notes field isn't HTML-escaped on display (confirmed
    live: an existing "File Closed" note's <strong> tag renders as real
    bold text, not literal angle brackets) — so a real <a> tag here
    renders as an actual clickable link in Handl's activity feed,
    rather than a plain-text URL office staff would have to copy out.
    urls are always our own generated blob URLs (get_photo_storage()),
    never locksmith-typed text, so this is safe without escaping."""
    return ", ".join(
        f'<a href="{url}" target="_blank">Photo {i}</a>' for i, url in enumerate(urls, start=1)
    )


def _save_visit_photos(request, visit, report_id, stage, kind, files):
    """Validates and uploads each file via get_photo_storage(), records
    a JobVisitPhoto per one, and returns the list of URLs saved (for
    the Handl note) — a file that fails type/size validation is
    skipped with an error message rather than aborting the whole
    batch, so one bad file doesn't lose the rest of a locksmith's
    photos."""
    storage = get_photo_storage()
    urls = []
    for f in files:
        if not (f.content_type or "").startswith("image/"):
            messages.error(request, f"'{f.name}' isn't an image — skipped.")
            continue
        if f.size > MAX_PHOTO_BYTES:
            messages.error(request, f"'{f.name}' is too large ({MAX_PHOTO_BYTES // (1024 * 1024)}MB max) — skipped.")
            continue
        url = storage.upload(
            report_id=report_id, stage=stage, filename=f.name,
            content=f.read(), content_type=f.content_type,
        )
        JobVisitPhoto.objects.create(visit=visit, kind=kind, url=url)
        urls.append(url)
    return urls


@login_required
def dashboard(request):
    locksmith = _locksmith_for_request(request)
    if locksmith is None:
        return _no_locksmith_access(request)

    today = timezone.localdate()
    selected_date = _selected_date(request)

    latest_check = locksmith.stock_checks.order_by("-week_starting").first()
    jobs = _jobs_for_date(locksmith, selected_date)
    order_nos = [job["order_no"] for job in jobs]
    totals = _disposal_totals(locksmith, order_nos)
    visits = {
        v.order_no: v
        for v in JobVisit.objects.filter(locksmith=locksmith, order_no__in=order_nos)
    }
    for job in jobs:
        summary = totals.get(job["order_no"])
        job["disposed_quantity"] = summary["quantity"] if summary else 0
        job["disposed_parts"] = summary["parts"] if summary else 0
        visit = visits.get(job["order_no"])
        job["visit_stage"] = visit.stage if visit else JobVisit.Stage.NOT_STARTED
        job["visit_stage_label"] = visit.get_stage_display() if visit else None

    return render(
        request,
        "locksmith_portal/dashboard.html",
        {
            "locksmith": locksmith,
            "latest_check": latest_check,
            "jobs": jobs,
            "today": today,
            "selected_date": selected_date,
            "is_today": selected_date == today,
            "prev_date": selected_date - timedelta(days=1),
            "next_date": selected_date + timedelta(days=1) if selected_date < today else None,
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
def job_overview(request, order_no):
    """The stepper landing page for one job: on route -> arrived (before
    photos) -> parts disposed -> complete (after photos, notes,
    outcome) — each step's action link only shown once the previous one
    is done, per stage in JobVisit.Stage."""
    ctx, early = _job_visit_context(request, order_no)
    if ctx is None:
        return early
    visit = ctx["visit"]

    disposal_count = PortalDisposal.objects.filter(
        locksmith=ctx["locksmith"], order_no=order_no
    ).count()

    return render(
        request,
        "locksmith_portal/job_overview.html",
        {
            "order_no": order_no,
            "report_id": ctx["report_id"],
            "visit": visit,
            "disposal_count": disposal_count,
            "selected_date": ctx["selected_date"],
            "is_today": ctx["selected_date"] == timezone.localdate(),
            "dashboard_url": ctx["dashboard_url"],
            "is_preview": _is_preview(request),
        },
    )


@login_required
@require_POST
def job_on_route(request, order_no):
    ctx, early = _job_visit_context(request, order_no)
    if ctx is None:
        return early
    locksmith, report_id, visit = ctx["locksmith"], ctx["report_id"], ctx["visit"]

    if visit.stage == JobVisit.Stage.NOT_STARTED:
        visit.stage = JobVisit.Stage.ON_ROUTE
        visit.on_route_at = timezone.now()
        visit.save(update_fields=["stage", "on_route_at"])
        _write_handl_note(
            locksmith, report_id, f"'{locksmith.van_soter_display_name}' is on route to this job."
        )

    overview_url = f"{reverse('locksmith_portal:job_overview', args=[order_no])}?date={ctx['selected_date'].isoformat()}"
    return redirect(overview_url)


@login_required
def job_arrived(request, order_no):
    ctx, early = _job_visit_context(request, order_no)
    if ctx is None:
        return early
    locksmith, report_id, visit = ctx["locksmith"], ctx["report_id"], ctx["visit"]
    selected_date = ctx["selected_date"]
    overview_url = f"{reverse('locksmith_portal:job_overview', args=[order_no])}?date={selected_date.isoformat()}"

    if visit.stage not in (JobVisit.Stage.ON_ROUTE, JobVisit.Stage.ARRIVED):
        messages.error(request, "Mark yourself on route first.")
        return redirect(overview_url)

    if request.method == "POST":
        photos = request.FILES.getlist("photos")
        if not photos:
            messages.error(request, "Add at least one before-job photo to continue.")
        else:
            urls = _save_visit_photos(
                request, visit, report_id, "before", JobVisitPhoto.Kind.BEFORE, photos
            )
            if urls:
                visit.stage = JobVisit.Stage.ARRIVED
                visit.arrived_at = timezone.now()
                visit.save(update_fields=["stage", "arrived_at"])
                _write_handl_note(
                    locksmith, report_id,
                    f"'{locksmith.van_soter_display_name}' has arrived on site. "
                    f"Before-job photos: {_photo_links_html(urls)}",
                )
                messages.success(request, "Arrival photos saved.")
                return redirect(overview_url)

    return render(
        request,
        "locksmith_portal/job_photo_upload.html",
        {
            "order_no": order_no,
            "report_id": report_id,
            "heading": "Arrived — before photos",
            "instructions": "Take a photo of the vehicle/site before you start work.",
            "action_url": f"{reverse('locksmith_portal:job_arrived', args=[order_no])}?date={selected_date.isoformat()}",
            "dashboard_url": ctx["dashboard_url"],
            "back_url": overview_url,
            "is_preview": _is_preview(request),
        },
    )


@login_required
@require_POST
def job_parts_continue(request, order_no):
    ctx, early = _job_visit_context(request, order_no)
    if ctx is None:
        return early
    visit = ctx["visit"]

    if visit.stage == JobVisit.Stage.ARRIVED:
        visit.stage = JobVisit.Stage.PARTS_DONE
        visit.parts_done_at = timezone.now()
        visit.save(update_fields=["stage", "parts_done_at"])

    complete_url = f"{reverse('locksmith_portal:job_complete', args=[order_no])}?date={ctx['selected_date'].isoformat()}"
    return redirect(complete_url)


@login_required
def job_complete(request, order_no):
    ctx, early = _job_visit_context(request, order_no)
    if ctx is None:
        return early
    locksmith, report_id, visit = ctx["locksmith"], ctx["report_id"], ctx["visit"]
    selected_date = ctx["selected_date"]
    overview_url = f"{reverse('locksmith_portal:job_overview', args=[order_no])}?date={selected_date.isoformat()}"

    if visit.stage == JobVisit.Stage.DONE:
        messages.info(request, "This job is already marked done.")
        return redirect(overview_url)
    if visit.stage != JobVisit.Stage.PARTS_DONE:
        messages.error(request, "Dispose parts (or continue past that step) first.")
        return redirect(overview_url)

    if request.method == "POST":
        photos = request.FILES.getlist("photos")
        notes_text = request.POST.get("notes", "").strip()
        outcome = request.POST.get("outcome")

        errors = False
        if not photos:
            messages.error(request, "Add at least one photo of the completed job.")
            errors = True
        if outcome not in (JobVisit.Outcome.COMPLETED, JobVisit.Outcome.FAILED):
            messages.error(request, "Choose Completed or Failed.")
            errors = True

        if not errors:
            urls = _save_visit_photos(
                request, visit, report_id, "after", JobVisitPhoto.Kind.AFTER, photos
            )
            if urls:
                visit.notes = notes_text
                visit.outcome = outcome
                visit.stage = JobVisit.Stage.DONE
                visit.completed_at = timezone.now()
                visit.save(update_fields=["notes", "outcome", "stage", "completed_at"])

                note = (
                    f"'{locksmith.van_soter_display_name}' marked this job as "
                    f"{visit.get_outcome_display()}. After-job photos: {_photo_links_html(urls)}"
                )
                if notes_text:
                    # escape()'d — unlike the photo URLs (our own, safe to
                    # embed as raw <a> tags), this is locksmith-typed free
                    # text going into a field Handl renders as live HTML.
                    note += f" Notes: {escape(notes_text)}"
                _write_handl_note(locksmith, report_id, note)

                messages.success(request, "Job marked complete.")
                return redirect(overview_url)

    return render(
        request,
        "locksmith_portal/job_complete.html",
        {
            "order_no": order_no,
            "report_id": report_id,
            "action_url": f"{reverse('locksmith_portal:job_complete', args=[order_no])}?date={selected_date.isoformat()}",
            "dashboard_url": ctx["dashboard_url"],
            "back_url": overview_url,
            "is_preview": _is_preview(request),
        },
    )


@login_required
def job_detail(request, order_no):
    ctx, early = _job_visit_context(request, order_no)
    if ctx is None:
        return early
    locksmith = ctx["locksmith"]
    report_id = ctx["report_id"]
    selected_date = ctx["selected_date"]
    dashboard_url = ctx["dashboard_url"]
    visit = ctx["visit"]

    overview_url = f"{reverse('locksmith_portal:job_overview', args=[order_no])}?date={selected_date.isoformat()}"
    if visit.stage not in (JobVisit.Stage.ARRIVED, JobVisit.Stage.PARTS_DONE):
        messages.error(request, "Mark yourself arrived (with before photos) first.")
        return redirect(overview_url)

    handl = get_handl_client()
    soter_ids = locksmith.soter_id_list
    stock_lines = handl.list_current_stock(soter_ids)
    previous_disposals = PortalDisposal.objects.filter(
        locksmith=locksmith, order_no=order_no
    ).order_by("-created_at")

    if request.method == "POST":
        by_code = {line.part_code.upper(): line for line in stock_lines}
        by_name = {line.part_name.lower(): line for line in stock_lines}

        def _resolve(raw_input):
            # A datalist pick comes back as "SKU — Name"; typing
            # (without picking a suggestion) may be a bare SKU or the
            # part name instead — try all three.
            candidate = raw_input.split(" — ", 1)[0].strip()
            return (
                by_code.get(candidate.upper())
                or by_name.get(candidate.lower())
                or by_name.get(raw_input.strip().lower())
            )

        # Aggregate by part first, in case the same part was searched
        # for and added across more than one row.
        requested: dict[str, int] = {}
        for raw_code, raw_qty in zip(
            request.POST.getlist("part_code"), request.POST.getlist("quantity")
        ):
            raw_code = raw_code.strip()
            raw_qty = raw_qty.strip()
            if not raw_code and not raw_qty:
                continue
            line = _resolve(raw_code) if raw_code else None
            if line is None:
                if raw_code:
                    messages.error(request, f"Couldn't find '{raw_code}' in your stock.")
                continue
            if not raw_qty:
                continue
            try:
                qty = int(raw_qty)
            except ValueError:
                messages.error(request, f"'{raw_qty}' isn't a valid quantity for {line.part_code}.")
                continue
            if qty <= 0:
                continue
            requested[line.part_code] = requested.get(line.part_code, 0) + qty

        disposed_any = False
        for part_code, qty in requested.items():
            line = by_code[part_code.upper()]
            if qty > line.qty:
                messages.error(
                    request,
                    f"You only have {line.qty} of {line.part_code} in stock — "
                    f"can't dispose {qty}.",
                )
                continue

            # A disposal is consumed from the locksmith's physical van
            # stock, so it's deliberately written against their "(V)"
            # Soter id (see Locksmith.van_soter_id) rather than "(A)" or
            # whichever happens to be configured first — confirmed via
            # a supervised manual test disposal.
            soter_id = locksmith.van_soter_id or ""
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
                handl.record_disposal(
                    soter_id,
                    report_id,
                    line.part_code,
                    line.part_name,
                    qty,
                    # Real disposals attribute to the specific locksmith's
                    # own Soter login (confirmed live), not a shared
                    # account — falls back to the placeholder setting only
                    # if this locksmith hasn't been through a Soter sync
                    # since soter_user_id was added.
                    actioned_by_user_id=(
                        locksmith.soter_user_id or settings.HANDL_PORTAL_CREATED_BY_USER_ID
                    ),
                    locksmith_display_name=locksmith.van_soter_display_name,
                )
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
        job_url = f"{reverse('locksmith_portal:job_detail', args=[order_no])}?date={selected_date.isoformat()}"
        return redirect(job_url)

    return render(
        request,
        "locksmith_portal/job_detail.html",
        {
            "order_no": order_no,
            "report_id": report_id,
            "stock_lines": stock_lines,
            "previous_disposals": previous_disposals,
            "locksmith": locksmith,
            "selected_date": selected_date,
            "is_today": selected_date == timezone.localdate(),
            "dashboard_url": dashboard_url,
            "overview_url": overview_url,
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
