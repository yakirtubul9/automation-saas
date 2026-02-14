# core/ui_urls.py
from django.urls import path

from . import ui_views

urlpatterns = [
    path("", ui_views.ui_home, name="ui_home"),
    path("appointments/", ui_views.ui_appointments, name="ui_appointments"),
    # Keep URL names stable for templates (appointments_list.html + partials).
    # These are MVP-safe stubs / minimal implementations.
    path("appointments/new/", ui_views.ui_appointment_create, name="ui_appointment_create"),
    path("appointments/<int:appt_id>/edit/", ui_views.ui_appointment_edit, name="ui_appointment_edit"),
    path("appointments/<int:appt_id>/status/", ui_views.ui_appointment_status, name="ui_appointment_status"),
    path("clients/", ui_views.ui_clients, name="ui_clients"),
    path("clients/<int:client_id>/", ui_views.ui_client_detail, name="ui_client_detail"),
    path("clinic/", ui_views.ui_clinic, name="ui_clinic"),
    path("ops/", ui_views.ui_ops, name="ui_ops"),
]
