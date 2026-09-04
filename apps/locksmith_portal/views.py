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
from apps.job_completion.models import FailureCategory
from apps.job_completion.services.labels import display_loss_type
from apps.job_completion.services.pulling import _report_id_from_order_no
from apps.locksmiths.models import Locksmith
from apps.stock_accuracy.models import WeeklyStockCheck

from .models import JobVisit, JobVisitPhoto, PortalDisposal

logger = logging.getLogger(__name__)

DISCLAIMER_TEXT = (
    "We need to attempt to gain access with a rod and airbag. This is by "
    "placing an airbag in the door frame to provide enough gap to get a rod "
    "inside to attempt to pull the door handle. Although damage is rare, it "
    "might cause small bodywork damage that WGTK can't be held liable for."
)

# Handl's other "key issue" loss types (everything in its loss-type
# dropdown besides Locked in Property/Gain access and Spare Key, which
# have their own distinct flows) — start all of them on the same
# checklist as AKL/Lost, rather than the generic single before/after
# photo. Easy to split any one of them out with its own list later once
# office has seen it in use.
_AKL_STYLE_SERVICE_LABELS = (
    "AKL", "Stolen", "Broken", "Ignition/Door Barrel", "Extraction",
    "Immobiliser", "Key Broken in Lock", "Diagnosis", "Other",
)

# Named photo slots for the arrival step, by service (Handl loss_type
# display label — see services/labels.py) — a service not listed here
# just gets the generic single "before" photo (the pre-existing
# behaviour). (kind, required) pairs.
_ARRIVAL_PHOTO_SLOTS_BY_SERVICE = {
    label: [
        (JobVisitPhoto.Kind.FRONT_OF_CAR, True),
        (JobVisitPhoto.Kind.DOOR_LOCK, True),
        (JobVisitPhoto.Kind.DAMAGE, False),
    ]
    for label in _AKL_STYLE_SERVICE_LABELS
}

# Named photo slots for the completion step, by service (Handl loss_type
# display label — see services/labels.py) — the specific evidence photos
# the business wants captured per job type, rather than one generic
# "after" bucket. (label, required) pairs.
#
# AKL covers both "Lost - with no spare" and "Lost - with spare" (Handl
# doesn't have a separate loss_type for these — it's the same "LOST"
# claim with a true/false spare-key flag) — both need the exact same
# photos, so one shared list covers it until office asks for them to
# diverge.
_AFTER_PHOTO_SLOTS_BY_SERVICE = {
    **{
        label: [
            (JobVisitPhoto.Kind.IGNITION_ON, True),
            (JobVisitPhoto.Kind.KEYS_SUPPLIED, True),
            (JobVisitPhoto.Kind.DAMAGE, False),
        ]
        for label in _AKL_STYLE_SERVICE_LABELS
    },
    "Spare Key": [
        (JobVisitPhoto.Kind.FRONT_OF_CAR, True),
        (JobVisitPhoto.Kind.DOOR_LOCK, True),
        (JobVisitPhoto.Kind.DAMAGE, False),
        (JobVisitPhoto.Kind.KEYS_SUPPLIED, True),
        (JobVisitPhoto.Kind.CLIENT_KEY, True),
        (JobVisitPhoto.Kind.IGNITION_ON, True),
    ],
}


def _loss_label_for(report_id):
    """Handl's loss_type display label (e.g. "Gain access", "AKL",
    "Spare Key") for this job, or "" if it can't be looked up — decides
    which arrival/completion questions/photo slots job_arrived,
    job_complete (and, for Gain access, job_access_method) show."""
    try:
        details = get_handl_client().get_job_details([report_id]).get(report_id)
    except Exception:
        logger.exception("Failed to fetch Handl job details for report %s", report_id)
        return ""
    return display_loss_type(details.loss_type) if details else ""


def _arrival_photo_slots(loss_label):
    """(kind, required) pairs for the arrival step's photo prompts. A
    service not specifically modelled here just gets one generic
    "before" photo, the original behaviour."""
    return _ARRIVAL_PHOTO_SLOTS_BY_SERVICE.get(loss_label, [(JobVisitPhoto.Kind.BEFORE, True)])


def _after_photo_slots(loss_label):
    """(kind, required) pairs for the completion step's photo prompts.
    Gain access is handled entirely at the earlier access-method step
    (see job_access_method — how they got in, and door-frame photos for
    an airbag entry, both happen before parts disposal), so it just
    gets the generic fallback here like anything else not specifically
    modelled."""
    return _AFTER_PHOTO_SLOTS_BY_SERVICE.get(loss_label, [(JobVisitPhoto.Kind.AFTER, True)])


def _access_method_photo_slots(access_method):
    """Door-frame photos are only expected for an airbag entry — picking
    a lock cleanly doesn't leave anything to photograph there."""
    if access_method == JobVisit.AccessMethod.AIRBAG:
        return [(JobVisitPhoto.Kind.DOOR_FRAME, True)]
    return []


# The failure-reason list is office's own admin-managed
# apps.job_completion.models.FailureCategory (Job Completion > Failure
# categories in the office admin) — the same list used for office's own
# reporting, rather than a separate locksmith-only enum. Deliberately
# NOT a self-rating: FailureCategory.master_reason is how "was this the
# locksmith's fault" gets classified, set by office against each
# category, invisible to the locksmith filling out this form.
#
# A few categories aren't meant to be locksmith-selectable at all, and
# some need a specific follow-up question depending on which one's
# picked — both hardcoded here by name (matching the codebase's existing
# per-service dicts above) rather than another admin-configurable layer.
_FAILURE_CATEGORIES_HIDDEN_FROM_LOCKSMITH = {
    "Not a failure", "Skill set - locksmith", "Uncategorized",
}
_FAILURE_CATEGORIES_NEEDING_SKU = {
    "Incorrect parts - ordered by WGTK", "Parts not arrived", "Parts issue - (suppiler)",
    "Further parts required (should have been known)", "Further parts required (unknown)",
    "Parts issue - (WGTK Stock)", "Stock issues- not on van as expected.",
}
_FAILURE_CATEGORIES_NEEDING_REATTEND = {
    "programmer issue", "Skill set - placed incorrectly",
    "Vehicle not available - client aware", "Vehicle not available - client NOT aware",
}


def _selectable_failure_categories():
    return FailureCategory.objects.filter(active=True).exclude(
        name__in=_FAILURE_CATEGORIES_HIDDEN_FROM_LOCKSMITH
    ).order_by("name")


def _decode_data_url(data_url):
    """Decodes a data: URL (e.g. from a <canvas>.toDataURL()) into
    (content_type, bytes). Used for the disclaimer signature, captured
    client-side as a PNG canvas drawing."""
    import base64

    header, _, encoded = data_url.partition(",")
    content_type = "image/png"
    if header.startswith("data:") and ";" in header:
        content_type = header[len("data:"):header.index(";")] or content_type
    return content_type, base64.b64decode(encoded)

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


def _update_optimo_status(order_no, status, *, start_time=None, end_time=None):
    """Best-effort, same rationale as _write_handl_note — pushes this
    stage into Optimo too (on_route/servicing/success/failed), the same
    as if the locksmith had used Optimo's own driver app for it,
    including triggering Optimo's own customer-facing order-tracking
    notifications where this account has them configured."""
    try:
        get_optimo_client().update_completion_status(
            order_no, status, start_time=start_time, end_time=end_time
        )
    except Exception:
        logger.exception("Failed to push Optimo status %s for order %s", status, order_no)


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
    try:
        job_details = get_handl_client().get_job_details([job["report_id"] for job in jobs])
    except Exception:
        logger.exception("Failed to fetch Handl job details for the dashboard job list")
        job_details = {}
    for job in jobs:
        summary = totals.get(job["order_no"])
        job["disposed_quantity"] = summary["quantity"] if summary else 0
        job["disposed_parts"] = summary["parts"] if summary else 0
        visit = visits.get(job["order_no"])
        job["visit_stage"] = visit.stage if visit else JobVisit.Stage.NOT_STARTED
        job["visit_stage_label"] = visit.get_stage_display() if visit else None
        details = job_details.get(job["report_id"])
        job["make"] = details.make if details else ""
        job["model"] = details.model if details else ""
        job["year"] = details.year if details else ""
        job["reg"] = details.reg if details else ""
        job["service"] = display_loss_type(details.loss_type) if details else ""

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
    photos) -> [Gain access only: access method + disclaimer] -> parts
    disposed -> complete (after photos, notes, outcome) — each step's
    action link only shown once the previous one is done, per stage in
    JobVisit.Stage."""
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
            "is_gain_access": _loss_label_for(ctx["report_id"]) == "Gain access",
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
        _update_optimo_status(order_no, "on_route")

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

    slot_prompts = _arrival_photo_slots(_loss_label_for(report_id))

    if request.method == "POST":
        errors = []
        slot_files = {}
        for kind, required in slot_prompts:
            files = request.FILES.getlist(f"photo_{kind}")
            if required and not files:
                errors.append(f"Add at least one photo: {JobVisitPhoto.Kind(kind).label}.")
            slot_files[kind] = files

        if errors:
            for error in errors:
                messages.error(request, error)
        else:
            note_parts = [f"'{locksmith.van_soter_display_name}' has arrived on site."]
            any_uploaded = False
            for kind, files in slot_files.items():
                if not files:
                    continue
                urls = _save_visit_photos(request, visit, report_id, kind, kind, files)
                if urls:
                    any_uploaded = True
                    note_parts.append(f"{JobVisitPhoto.Kind(kind).label}: {_photo_links_html(urls)}")

            if not any_uploaded:
                # Every attached file failed image/size validation (see
                # _save_visit_photos) — nothing to advance the stage for.
                messages.error(request, "Add at least one before-job photo to continue.")
            else:
                visit.stage = JobVisit.Stage.ARRIVED
                visit.arrived_at = timezone.now()
                visit.save(update_fields=["stage", "arrived_at"])
                _write_handl_note(locksmith, report_id, " ".join(note_parts))
                _update_optimo_status(order_no, "servicing", start_time=visit.arrived_at)
                messages.success(request, "Arrival photos saved.")
                return redirect(overview_url)

    photo_slots = [
        {"kind": kind, "label": JobVisitPhoto.Kind(kind).label, "required": required}
        for kind, required in slot_prompts
    ]

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
            "photo_slots": photo_slots,
            "is_preview": _is_preview(request),
        },
    )


@login_required
def job_access_method(request, order_no):
    """Gain access jobs only: how the locksmith got in, and — for an
    airbag entry — the damage disclaimer (signed on the locksmith's
    phone) plus door-frame photos. Sits between arrived and parts
    disposal, not at completion: the disclaimer needs to be agreed
    before work continues, not retrospectively once the job's done."""
    ctx, early = _job_visit_context(request, order_no)
    if ctx is None:
        return early
    locksmith, report_id, visit = ctx["locksmith"], ctx["report_id"], ctx["visit"]
    selected_date = ctx["selected_date"]
    overview_url = f"{reverse('locksmith_portal:job_overview', args=[order_no])}?date={selected_date.isoformat()}"

    if visit.stage not in (JobVisit.Stage.ARRIVED, JobVisit.Stage.PARTS_DONE, JobVisit.Stage.DONE):
        messages.error(request, "Mark yourself arrived first.")
        return redirect(overview_url)

    if _loss_label_for(report_id) != "Gain access":
        return redirect(overview_url)

    if request.method == "POST":
        access_method = request.POST.get("access_method", "")
        pick_used = request.POST.get("pick_used", "").strip()
        signature_data_url = request.POST.get("disclaimer_signature", "").strip()

        errors = []
        if access_method not in (JobVisit.AccessMethod.PICKED, JobVisit.AccessMethod.AIRBAG):
            errors.append("Choose whether you picked the lock or used the airbag.")
        elif access_method == JobVisit.AccessMethod.PICKED and not pick_used:
            errors.append("Enter what pick was used.")
        elif access_method == JobVisit.AccessMethod.AIRBAG and not signature_data_url:
            errors.append("The customer needs to sign the disclaimer before continuing.")

        slot_pairs = _access_method_photo_slots(access_method)
        slot_files = {}
        for kind, required in slot_pairs:
            files = request.FILES.getlist(f"photo_{kind}")
            if required and not files:
                errors.append(f"Add at least one photo: {JobVisitPhoto.Kind(kind).label}.")
            slot_files[kind] = files

        if errors:
            for error in errors:
                messages.error(request, error)
        else:
            note_parts = []
            if access_method == JobVisit.AccessMethod.PICKED:
                note_parts.append(
                    f"'{locksmith.van_soter_display_name}' gained access by picking "
                    f"(pick used: {escape(pick_used)})."
                )
            else:
                content_type, signature_bytes = _decode_data_url(signature_data_url)
                signature_url = get_photo_storage().upload(
                    report_id=report_id, stage="disclaimer", filename="signature.png",
                    content=signature_bytes, content_type=content_type,
                )
                JobVisitPhoto.objects.create(
                    visit=visit, kind=JobVisitPhoto.Kind.DISCLAIMER_SIGNATURE, url=signature_url
                )
                visit.disclaimer_signed_at = timezone.now()
                note_parts.append(
                    f"'{locksmith.van_soter_display_name}' is attempting access via airbag — "
                    f"customer signed the damage disclaimer: {_photo_links_html([signature_url])}"
                )

            for kind, files in slot_files.items():
                if not files:
                    continue
                urls = _save_visit_photos(request, visit, report_id, kind, kind, files)
                if urls:
                    note_parts.append(f"{JobVisitPhoto.Kind(kind).label}: {_photo_links_html(urls)}")

            visit.access_method = access_method
            visit.pick_used = pick_used if access_method == JobVisit.AccessMethod.PICKED else ""
            visit.save(update_fields=["access_method", "pick_used", "disclaimer_signed_at"])

            _write_handl_note(locksmith, report_id, " ".join(note_parts))

            messages.success(request, "Access method recorded.")
            return redirect(overview_url)

    return render(
        request,
        "locksmith_portal/job_access_method.html",
        {
            "order_no": order_no,
            "report_id": report_id,
            "action_url": f"{reverse('locksmith_portal:job_access_method', args=[order_no])}?date={selected_date.isoformat()}",
            "dashboard_url": ctx["dashboard_url"],
            "back_url": overview_url,
            "is_preview": _is_preview(request),
            "disclaimer_text": DISCLAIMER_TEXT,
        },
    )


@login_required
@require_POST
def job_parts_continue(request, order_no):
    ctx, early = _job_visit_context(request, order_no)
    if ctx is None:
        return early
    visit = ctx["visit"]

    if not visit.access_method and _loss_label_for(ctx["report_id"]) == "Gain access":
        messages.error(request, "Record how you gained access first.")
        return redirect(
            f"{reverse('locksmith_portal:job_access_method', args=[order_no])}?date={ctx['selected_date'].isoformat()}"
        )

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

    loss_label = _loss_label_for(report_id)

    if request.method == "POST":
        notes_text = request.POST.get("notes", "").strip()
        outcome = request.POST.get("outcome")
        failure_sku_needed = request.POST.get("failure_sku_needed", "").strip()
        failure_reattend_action = request.POST.get("failure_reattend_action", "")
        failure_category_id = request.POST.get("failure_category", "")
        failure_category = (
            _selectable_failure_categories().filter(pk=failure_category_id).first()
            if failure_category_id else None
        )
        completion_signature_data_url = request.POST.get("completion_signature", "").strip()

        errors = []
        if outcome not in (JobVisit.Outcome.COMPLETED, JobVisit.Outcome.FAILED):
            errors.append("Choose Completed or Failed.")

        if outcome == JobVisit.Outcome.COMPLETED and not completion_signature_data_url:
            errors.append("The customer needs to sign to confirm they're happy with the job.")

        if outcome == JobVisit.Outcome.FAILED:
            if failure_category is None:
                errors.append("Choose a reason for the failure.")
            else:
                if failure_category.name in _FAILURE_CATEGORIES_NEEDING_SKU and not failure_sku_needed:
                    errors.append("Enter the SKU / part needed.")
                if (
                    failure_category.name in _FAILURE_CATEGORIES_NEEDING_REATTEND
                    and failure_reattend_action not in JobVisit.ReattendAction.values
                ):
                    errors.append("Choose a reattend option.")

        slot_pairs = _after_photo_slots(loss_label)
        slot_files = {}
        for kind, required in slot_pairs:
            files = request.FILES.getlist(f"photo_{kind}")
            if required and not files:
                errors.append(f"Add at least one photo: {JobVisitPhoto.Kind(kind).label}.")
            slot_files[kind] = files

        if errors:
            for error in errors:
                messages.error(request, error)
        else:
            note_parts = [
                f"'{locksmith.van_soter_display_name}' marked this job as "
                f"{JobVisit.Outcome(outcome).label}."
            ]

            for kind, files in slot_files.items():
                if not files:
                    continue
                urls = _save_visit_photos(request, visit, report_id, kind, kind, files)
                if urls:
                    note_parts.append(f"{JobVisitPhoto.Kind(kind).label}: {_photo_links_html(urls)}")

            if failure_category is not None:
                detail = ""
                if failure_category.name in _FAILURE_CATEGORIES_NEEDING_SKU:
                    detail = f" (SKU / part needed: {escape(failure_sku_needed)})"
                elif failure_category.name in _FAILURE_CATEGORIES_NEEDING_REATTEND:
                    detail = f" ({JobVisit.ReattendAction(failure_reattend_action).label})"
                note_parts.append(f"Failure reason: {escape(failure_category.name)}{detail}.")

            if outcome == JobVisit.Outcome.COMPLETED:
                content_type, signature_bytes = _decode_data_url(completion_signature_data_url)
                signature_url = get_photo_storage().upload(
                    report_id=report_id, stage="completion", filename="signature.png",
                    content=signature_bytes, content_type=content_type,
                )
                JobVisitPhoto.objects.create(
                    visit=visit, kind=JobVisitPhoto.Kind.COMPLETION_SIGNATURE, url=signature_url
                )
                visit.completion_signed_at = timezone.now()
                note_parts.append(
                    f"Customer signed to confirm they're happy with the job: "
                    f"{_photo_links_html([signature_url])}"
                )

            if notes_text:
                # escape()'d — unlike the photo/signature URLs (our own,
                # safe to embed as raw <a> tags), this is locksmith-typed
                # free text going into a field Handl renders as live HTML.
                note_parts.append(f"Notes: {escape(notes_text)}")

            visit.notes = notes_text
            visit.outcome = outcome
            visit.failure_category = failure_category if outcome == JobVisit.Outcome.FAILED else None
            visit.failure_sku_needed = (
                failure_sku_needed
                if failure_category and failure_category.name in _FAILURE_CATEGORIES_NEEDING_SKU else ""
            )
            visit.failure_reattend_action = (
                failure_reattend_action
                if failure_category and failure_category.name in _FAILURE_CATEGORIES_NEEDING_REATTEND else ""
            )
            visit.stage = JobVisit.Stage.DONE
            visit.completed_at = timezone.now()
            visit.save(update_fields=[
                "notes", "outcome",
                "failure_category", "failure_sku_needed", "failure_reattend_action",
                "completion_signed_at", "stage", "completed_at",
            ])

            _write_handl_note(locksmith, report_id, " ".join(note_parts))
            _update_optimo_status(
                order_no,
                "success" if outcome == JobVisit.Outcome.COMPLETED else "failed",
                start_time=visit.arrived_at, end_time=visit.completed_at,
            )

            messages.success(request, "Job marked complete.")
            return redirect(overview_url)

    photo_slots = [
        {"kind": kind, "label": JobVisitPhoto.Kind(kind).label, "required": required}
        for kind, required in _after_photo_slots(loss_label)
    ]
    failure_categories = [
        {
            "id": category.pk,
            "name": category.name,
            "needs_sku": category.name in _FAILURE_CATEGORIES_NEEDING_SKU,
            "needs_reattend": category.name in _FAILURE_CATEGORIES_NEEDING_REATTEND,
        }
        for category in _selectable_failure_categories()
    ]

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
            "loss_label": loss_label,
            "photo_slots": photo_slots,
            "failure_categories": failure_categories,
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

    if not visit.access_method and _loss_label_for(report_id) == "Gain access":
        messages.error(request, "Record how you gained access first.")
        return redirect(
            f"{reverse('locksmith_portal:job_access_method', args=[order_no])}?date={selected_date.isoformat()}"
        )

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
