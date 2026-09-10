import hmac
from datetime import date
from io import StringIO

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import models
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.locksmiths.models import Locksmith
from apps.locksmith_portal.models import PortalDisposalEdit

from .models import CompletedJob, FailureCategory
from .services.benchmarking import duration_benchmark
from .services.costing import parts_cost_for_jobs
from .services.daily import day_pills, jobs_for_day, next_offset, prev_offset, review_flags, summarize_day
from .services import job_information
from .services.model_analysis import (
    MASTER_REASON_LABELS,
    company_model_failure_breakdown,
    locksmith_model_failure_breakdown,
)
from .services.reporting import (
    all_locksmith_summaries,
    failed_jobs_list as failed_jobs_list_service,
    failure_category_breakdown,
    loss_types_for_locksmith,
    master_reason_breakdown,
    needs_categorization_queryset,
)
from .services.trends import (
    locksmith_wgtk_fault_trend,
    make_model_failure_trend,
    monthly_failure_trend,
)


@login_required
def dashboard(request):
    summaries = all_locksmith_summaries()
    return render(
        request,
        "job_completion/dashboard.html",
        {
            "summaries": summaries,
        },
    )


@login_required
def failed_jobs_list(request):
    """Drill-down from Job Failures' category/master-reason breakdowns
    (and, filtered by locksmith too, from the locksmith report) to the
    actual failed jobs behind a count — same 90-day window those
    breakdowns use. "category" is either a FailureCategory pk or the
    literal "uncategorized"; same for "master_reason"."""
    category_param = request.GET.get("category", "")
    master_reason_param = request.GET.get("master_reason", "")
    locksmith_param = request.GET.get("locksmith", "")

    category_id = None
    uncategorized = category_param == "uncategorized"
    if category_param and not uncategorized:
        try:
            category_id = int(category_param)
        except ValueError:
            category_id = None

    locksmith_id = None
    if locksmith_param:
        try:
            locksmith_id = int(locksmith_param)
        except ValueError:
            locksmith_id = None

    jobs = list(
        failed_jobs_list_service(
            category_id=category_id,
            uncategorized=uncategorized,
            master_reason=master_reason_param or None,
            locksmith_id=locksmith_id,
        )
    )
    parts_costs = parts_cost_for_jobs(jobs)
    for job in jobs:
        job.parts_cost = parts_costs.get(job.order_no)

    heading = "Failed jobs"
    if uncategorized:
        heading = "Failed jobs — uncategorized"
    elif category_id is not None:
        category = FailureCategory.objects.filter(pk=category_id).first()
        heading = f"Failed jobs — {category.name}" if category else heading
    elif master_reason_param:
        heading = f"Failed jobs — {dict(FailureCategory.MasterReason.choices).get(master_reason_param, master_reason_param)}"
    if locksmith_id is not None:
        locksmith = Locksmith.objects.filter(pk=locksmith_id).first()
        if locksmith:
            heading += f" — {locksmith.name}"

    return render(
        request,
        "job_completion/failed_jobs_list.html",
        {
            "jobs": jobs,
            "heading": heading,
        },
    )


@login_required
def job_failures(request):
    needs_categorization = list(needs_categorization_queryset())
    parts_costs = parts_cost_for_jobs(needs_categorization)
    for job in needs_categorization:
        job.parts_cost = parts_costs.get(job.order_no)
    failure_category_choices = FailureCategory.objects.filter(active=True)
    categories = failure_category_breakdown()
    master_reasons = master_reason_breakdown()
    return render(
        request,
        "job_completion/job_failures.html",
        {
            "needs_categorization": needs_categorization,
            "failure_category_choices": failure_category_choices,
            "categories": categories,
            "master_reasons": master_reasons,
        },
    )


@login_required
def categorize_jobs(request):
    """Bulk categorization: the Job Failures page's "Needs categorization"
    table is one form with a category dropdown per row and a single
    "Save all" button, so office staff can pick reasons for several
    jobs before submitting once — rather than a full page reload per
    row."""
    if request.method != "POST":
        return redirect("job_completion:job_failures")

    categories_by_id = {str(c.pk): c for c in FailureCategory.objects.all()}
    saved = 0
    for key, category_id in request.POST.items():
        if not key.startswith("category_") or not category_id:
            continue
        job_pk = key.removeprefix("category_")
        if not job_pk.isdigit():
            continue
        category = categories_by_id.get(category_id)
        if not category:
            continue
        # The QC checkbox is read alongside category, not standalone —
        # a row only gets a notes_sufficient judgement when office
        # actually categorizes it this same submission, same as
        # categorized_by/categorized_at.
        notes_sufficient = f"notes_ok_{job_pk}" in request.POST
        updated = CompletedJob.objects.filter(pk=job_pk).update(
            failure_category=category, categorized_by=request.user, categorized_at=timezone.now(),
            notes_sufficient=notes_sufficient,
        )
        saved += updated

    if saved:
        messages.success(request, f"Categorized {saved} job(s).")
    else:
        messages.error(request, "Nothing selected — choose a reason for at least one job.")
    return redirect("job_completion:job_failures")


@login_required
def disposal_reviews(request):
    """Every locksmith-portal disposal edit, void, or late add — a
    locksmith can correct any part they've recorded, but every such
    correction requires a reason and lands here for office/managers to
    review (in addition to the matching note left on the Handl claim
    itself). Same "no separate manager role" access as the rest of this
    app — anyone with office access can see and mark these reviewed."""
    edits = list(
        PortalDisposalEdit.objects.select_related(
            "disposal", "disposal__locksmith", "performed_by", "reviewed_by",
        ).order_by(models.F("reviewed_at").asc(nulls_first=True), "-performed_at")
    )
    return render(request, "job_completion/disposal_reviews.html", {"edits": edits})


@login_required
@require_POST
def mark_disposal_edits_reviewed(request):
    ids = [pk for pk in request.POST.getlist("edit_id") if pk.isdigit()]
    updated = PortalDisposalEdit.objects.filter(pk__in=ids, reviewed_at__isnull=True).update(
        reviewed_at=timezone.now(), reviewed_by=request.user,
    )
    if updated:
        messages.success(request, f"Marked {updated} entr{'y' if updated == 1 else 'ies'} reviewed.")
    else:
        messages.error(request, "Nothing selected.")
    return redirect("job_completion:disposal_reviews")


@login_required
def model_analysis(request):
    scope = request.GET.get("scope") if request.GET.get("scope") == "company" else "locksmith"
    if scope == "company":
        breakdown = company_model_failure_breakdown()
    else:
        breakdown = locksmith_model_failure_breakdown()
    return render(
        request,
        "job_completion/model_analysis.html",
        {"breakdown": breakdown, "scope": scope, "master_reason_labels": MASTER_REASON_LABELS},
    )


@login_required
def failure_trend(request):
    scope = request.GET.get("scope")
    if scope not in {"locksmith", "model"}:
        scope = "month"

    context = {"scope": scope}
    if scope == "month":
        rows = monthly_failure_trend()
        context["monthly_rows"] = rows
        context["max_failed"] = max((r["failed"] for r in rows), default=0)
    elif scope == "locksmith":
        data = locksmith_wgtk_fault_trend()
        context["months"] = data["months"]
        context["rows"] = data["rows"]
    else:
        data = make_model_failure_trend()
        context["months"] = data["months"]
        context["rows"] = data["rows"]

    return render(request, "job_completion/failure_trend.html", context)


@login_required
def jobs_by_day(request):
    try:
        offset = max(int(request.GET.get("offset", 0)), 0)
    except ValueError:
        offset = 0

    pills = day_pills(offset)

    selected_str = request.GET.get("date")
    selected = None
    if selected_str:
        try:
            selected = date.fromisoformat(selected_str)
        except ValueError:
            selected = None
    if selected is None:
        selected = pills[0]

    jobs = list(jobs_for_day(selected))
    parts_costs = parts_cost_for_jobs(jobs)
    for job in jobs:
        job.parts_cost = parts_costs.get(job.order_no)
        if job.net_cost is not None and job.parts_cost is not None:
            job.margin = round(job.net_cost - job.parts_cost, 2)
        else:
            job.margin = None
        job.review_flags = review_flags(job)

    # Summary totals always reflect the whole day, regardless of the
    # flagged-only filter below — otherwise switching the filter on
    # would make the day's own totals look like they'd changed.
    summary = summarize_day(jobs)

    flagged_only = request.GET.get("flagged") == "1"
    display_jobs = [job for job in jobs if job.review_flags] if flagged_only else jobs

    return render(
        request,
        "job_completion/jobs_by_day.html",
        {
            "pills": pills,
            "selected": selected,
            "jobs": display_jobs,
            "offset": offset,
            "next_offset": next_offset(offset),
            "prev_offset": prev_offset(offset),
            "summary": summary,
            "flagged_only": flagged_only,
        },
    )


@login_required
def locksmith_report(request, pk):
    locksmith = get_object_or_404(Locksmith, pk=pk)
    loss_types = loss_types_for_locksmith(locksmith)
    benchmarks = [duration_benchmark(locksmith, loss_type) for loss_type in loss_types]
    jobs = (
        CompletedJob.objects.filter(locksmith=locksmith)
        .select_related("failure_category")
        .order_by("-job_date")[:50]
    )
    return render(
        request,
        "job_completion/locksmith_report.html",
        {"locksmith": locksmith, "benchmarks": benchmarks, "jobs": jobs},
    )


# --- Job Information: Margin / Timing, both a Make -> Model family ->
# Year drill-down over the same underlying summary data (see
# services/job_information.py) — the two report kinds just render
# different columns from it, and share these three helpers.


def _job_info_makes(request, template):
    service = request.GET.get("service") or None
    return render(
        request,
        template,
        {
            "rows": job_information.makes_summary(service=service),
            "services": job_information.available_services(),
            "selected_service": service,
        },
    )


def _job_info_models(request, template, make):
    service = request.GET.get("service") or None
    return render(
        request,
        template,
        {
            "make": make,
            "rows": job_information.models_summary(make, service=service),
            "services": job_information.available_services(),
            "selected_service": service,
        },
    )


def _job_info_years(request, template, make, model_family):
    service = request.GET.get("service") or None
    return render(
        request,
        template,
        {
            "make": make,
            "model_family": model_family,
            "rows": job_information.years_summary(make, model_family, service=service),
            "services": job_information.available_services(),
            "selected_service": service,
        },
    )


@login_required
def margin_makes(request):
    return _job_info_makes(request, "job_completion/margin_makes.html")


@login_required
def margin_models(request, make):
    return _job_info_models(request, "job_completion/margin_models.html", make)


@login_required
def margin_years(request, make, model_family):
    return _job_info_years(request, "job_completion/margin_years.html", make, model_family)


@login_required
def timing_makes(request):
    return _job_info_makes(request, "job_completion/timing_makes.html")


@login_required
def timing_models(request, make):
    return _job_info_models(request, "job_completion/timing_models.html", make)


@login_required
def timing_years(request, make, model_family):
    return _job_info_years(request, "job_completion/timing_years.html", make, model_family)


# --- Scheduled jobs over HTTP (replaces Azure WebJobs — see
# SCHEDULED_JOB_TOKEN in config/settings/base.py for why) -------------------

_SCHEDULABLE_COMMANDS = {
    "pull_completed_jobs",
    "refresh_job_financials",
    "send_weekly_stock_checks",
    "check_overdue_visits",
}


@csrf_exempt
@require_POST
def run_scheduled_job(request, command_name):
    """Runs one of a fixed allow-list of management commands. Deliberately
    not @login_required — the caller is a GitHub Actions scheduled
    workflow, which can't go through Microsoft SSO — authenticated
    instead by a shared secret header, compared in constant time to
    resist timing attacks."""
    token = request.headers.get("X-Job-Token", "")
    expected = settings.SCHEDULED_JOB_TOKEN
    if not expected or not hmac.compare_digest(token, expected):
        return HttpResponseForbidden("Forbidden")
    if command_name not in _SCHEDULABLE_COMMANDS:
        return HttpResponseForbidden("Unknown command")

    out = StringIO()
    try:
        call_command(command_name, stdout=out)
    except CommandError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)
    return JsonResponse({"ok": True, "output": out.getvalue()})
