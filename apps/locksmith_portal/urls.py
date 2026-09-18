from django.urls import path

from . import views

app_name = "locksmith_portal"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("search/", views.job_search, name="job_search"),
    path("history/<int:pk>/", views.job_history_detail, name="job_history_detail"),
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
    # Multi-vehicle stop only (see JobVisitVehicle) — the ?vehicle=<id>
    # each of these takes is a query param, not a path segment, so it
    # doesn't need its own <path:> entry here. Listed before
    # job_access_method/job_complete below: <path:order_no> is greedy
    # enough to swallow ".../vehicle" and still match those patterns'
    # own "access-method/"/"complete/" suffix, so if this block were
    # listed after them, Django would match those first with a bogus
    # order_no and this block would never be reached (confirmed live in
    # MultiVehicleJobTests — exactly that shadowing).
    path(
        "jobs/<path:order_no>/vehicle/before-photos/",
        views.vehicle_before_photos,
        name="vehicle_before_photos",
    ),
    path(
        "jobs/<path:order_no>/vehicle/access-method/",
        views.vehicle_access_method,
        name="vehicle_access_method",
    ),
    path("jobs/<path:order_no>/vehicle/complete/", views.vehicle_complete, name="vehicle_complete"),
    path("jobs/<path:order_no>/sign-off/", views.job_signoff, name="job_signoff"),
    path("jobs/<path:order_no>/arrived/", views.job_arrived, name="job_arrived"),
    path("jobs/<path:order_no>/access-method/", views.job_access_method, name="job_access_method"),
    path("jobs/<path:order_no>/parts/continue/", views.job_parts_continue, name="job_parts_continue"),
    path("jobs/<path:order_no>/parts/", views.job_detail, name="job_detail"),
    path(
        "jobs/<path:order_no>/parts/<int:disposal_id>/edit/",
        views.edit_disposal,
        name="edit_disposal",
    ),
    path("jobs/<path:order_no>/complete/", views.job_complete, name="job_complete"),
    path(
        "jobs/<path:order_no>/photos/<str:kind>/",
        views.job_photo_upload_one,
        name="job_photo_upload_one",
    ),
    path("jobs/<path:order_no>/", views.job_overview, name="job_overview"),
    path("preview/stop/", views.stop_preview, name="stop_preview"),
    path("preview/<int:locksmith_id>/", views.start_preview, name="start_preview"),
]
