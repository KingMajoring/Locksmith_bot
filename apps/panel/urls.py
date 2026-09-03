from django.urls import path

from . import views

app_name = "panel"

urlpatterns = [
    path("spend/", views.spend, name="spend"),
]
