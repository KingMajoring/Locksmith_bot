from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.locksmiths.models import Locksmith

from .models import CompletedJob, FailureCategory
from .services.benchmarking import duration_benchmark
from .services.reporting import (
    all_locksmith_summaries,
    failure_category_breakdown,
    needs_categorization_queryset,
    service_types_for_locksmith,
)


@login_required
def dashboard(request):
    needs_categorization = needs_categorization_queryset()[:20]
    summaries = all_locksmith_summaries()
    categories = failure_category_breakdown()
    failure_category_choices = FailureCategory.objects.filter(active=True)
    return render(
        request,
        "job_completion/dashboard.html",
        {
            "needs_categorization": needs_categorization,
            "summaries": summaries,
            "categories": categories,
            "failure_category_choices": failure_category_choices,
        },
    )


@login_required
def categorize_job(request, pk):
    job = get_object_or_404(CompletedJob, pk=pk)
    if request.method == "POST":
        category_id = request.POST.get("failure_category")
        if category_id:
            job.failure_category = get_object_or_404(FailureCategory, pk=category_id)
            job.categorized_by = request.user
            job.categorized_at = timezone.now()
            job.save(update_fields=["failure_category", "categorized_by", "categorized_at"])
            messages.success(request, f"{job.order_no} categorized as {job.failure_category}.")
        else:
            messages.error(request, "Choose a category first.")
    return redirect("job_completion:dashboard")


@login_required
def locksmith_report(request, pk):
    locksmith = get_object_or_404(Locksmith, pk=pk)
    service_types = service_types_for_locksmith(locksmith)
    benchmarks = [duration_benchmark(locksmith, service_type) for service_type in service_types]
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
