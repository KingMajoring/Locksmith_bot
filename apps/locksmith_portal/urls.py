from django.urls import path

from . import views

app_name = "locksmith_portal"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("panic/", views.panic_alert, name="panic_alert"),
    path("stock-check/<int:pk>/", views.stock_check_entry, name="stock_check_entry"),
    # order_no uses the <path:> converter because it can itself contain
    # "/" (real Optimo order numbers have been seen to) — the specific
    # sub-paths below MUST stay listed before the bare
    # "jobs/<order_no>/" pattern, since Django tries patterns in order
    # and <path:> is greedy: were the bare pattern listed first, it
    # would swallow e.g. "X/on-route" whole as one order_no and this
    # sub-path would never be reached.
    path("jobs/<path:order_no>/on-route/", views.job_on_route, name="job_on_route"),
    path("jobs/<path:order_no>/cancel/", views.job_cancel, name="job_cancel"),
    path("jobs/<path:order_no>/arrived/", views.job_arrived, name="job_arrived"),
    path("jobs/<path:order_no>/access-method/", views.job_access_method, name="job_access_method"),
    path("jobs/<path:order_no>/parts/continue/", views.job_parts_continue, name="job_parts_continue"),
    path("jobs/<path:order_no>/parts/", views.job_detail, name="job_detail"),
    path("jobs/<path:order_no>/complete/", views.job_complete, name="job_complete"),
    path("jobs/<path:order_no>/", views.job_overview, name="job_overview"),
    path("preview/stop/", views.stop_preview, name="stop_preview"),
    path("preview/<int:locksmith_id>/", views.start_preview, name="start_preview"),
]
