# core/ui_urls.py
from django.urls import path

from . import ui_views

urlpatterns = [
    path("", ui_views.ui_home, name="ui_home"),
    path("appointments/", ui_views.ui_appointments, name="ui_appointments"),
    path("clients/", ui_views.ui_clients, name="ui_clients"),
    path("clients/<int:client_id>/", ui_views.ui_client_detail, name="ui_client_detail"),
    path("clinic/", ui_views.ui_clinic, name="ui_clinic"),
    path("ops/", ui_views.ui_ops, name="ui_ops"),
]
