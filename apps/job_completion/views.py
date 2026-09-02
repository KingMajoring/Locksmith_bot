from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.locksmiths.models import Locksmith

from .models import CompletedJob, FailureCategory
from .services.benchmarking import duration_benchmark
from .services.model_analysis import (
    MASTER_REASON_LABELS,
    company_model_failure_breakdown,
    locksmith_model_failure_breakdown,
)
from .services.reporting import (
    all_locksmith_summaries,
    failure_category_breakdown,
    loss_types_for_locksmith,
    master_reason_breakdown,
    needs_categorization_queryset,
)


@login_required
def dashboard(request):
    needs_categorization = needs_categorization_queryset()[:20]
    summaries = all_locksmith_summaries()
    categories = failure_category_breakdown()
    master_reasons = master_reason_breakdown()
    failure_category_choices = FailureCategory.objects.filter(active=True)
    return render(
        request,
        "job_completion/dashboard.html",
        {
            "needs_categorization": needs_categorization,
            "summaries": summaries,
            "categories": categories,
            "master_reasons": master_reasons,
            "failure_category_choices": failure_category_choices,
        },
    )


@login_required
def categorize_jobs(request):
    """Bulk categorization: the dashboard's "Needs categorization" table
    is one form with a category dropdown per row and a single "Save
    all" button, so office staff can pick reasons for several jobs
    before submitting once — rather than a full page reload per row."""
    if request.method != "POST":
        return redirect("job_completion:dashboard")

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
        updated = CompletedJob.objects.filter(pk=job_pk).update(
            failure_category=category, categorized_by=request.user, categorized_at=timezone.now()
        )
        saved += updated

    if saved:
        messages.success(request, f"Categorized {saved} job(s).")
    else:
        messages.error(request, "Nothing selected — choose a reason for at least one job.")
    return redirect("job_completion:dashboard")


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
