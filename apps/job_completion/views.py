from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from apps.locksmiths.models import Locksmith

from .models import CompletedJob
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
    return render(
        request,
        "job_completion/dashboard.html",
        {
            "needs_categorization": needs_categorization,
            "summaries": summaries,
            "categories": categories,
        },
    )


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
