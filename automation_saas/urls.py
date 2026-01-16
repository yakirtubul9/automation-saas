# automation_saas/urls.py
from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views

from core import views as core_views
from core import api as core_api

from django.http import HttpResponse

urlpatterns = [
    path("admin/", admin.site.urls),
    path("probe/", lambda r: HttpResponse("PROBE_OK")),
    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", core_views.logout_view, name="logout"),
    path("", core_views.dashboard, name="dashboard"),
    path("settings/", core_views.settings_view, name="settings"),
    path("a/<str:token>/<str:action>/", core_views.appointment_action_view, name="appointment_action"),

    # Minimal internal API (used later by Agents). For now - login required.
    path("api/reserve-slot/", core_api.reserve_slot_view, name="reserve_slot"),
    path("api/availability/", core_api.availability_view, name="availability"),
    path("api/assign-client/", core_api.assign_client_view, name="assign_client"),
]


