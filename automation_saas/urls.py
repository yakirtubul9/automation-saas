# automation_saas/urls.py
from django.contrib import admin
from django.urls import path, include
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
    path("app/", include("core.ui_urls")),
    path("a/<str:token>/<str:action>/", core_views.appointment_action_view, name="appointment_action"),

    # Minimal internal API (used later by Agents). For now - login required.
    path("api/reserve-slot/", core_api.reserve_slot_view, name="reserve_slot"),
    path("api/reserve-slots/", core_api.reserve_slots_view, name="reserve_slots"),
    path("api/availability/", core_api.availability_view, name="availability"),
    path("api/assign-client/", core_api.assign_client_view, name="assign_client"),
    path("api/room-blocks/", core_api.room_block_view, name="room_blocks"),
    path("api/waitlist/", core_api.waitlist_view, name="waitlist"),
    path("api/change-proposals/", core_api.change_proposal_create_view, name="change_proposal_create"),
    path("api/change-proposals/<int:proposal_id>/cancel/", core_api.change_proposal_cancel_view, name="change_proposal_cancel"),
    path("api/change-proposals/<int:proposal_id>/resend/", core_api.change_proposal_resend_view, name="change_proposal_resend"),
    path("p/<str:token>/<str:action>/", core_views.change_proposal_action_view, name="change_proposal_action"),
    path("w/<str:token>/<str:action>/", core_views.waitlist_offer_action_view, name="waitlist_offer_action"),
    path("r/<str:token>/<str:action>/", core_views.recall_offer_action_view, name="recall_offer_action"),

    # WhatsApp Cloud webhook (Stage 8: conversational client agent)
    path("webhooks/whatsapp/", core_api.whatsapp_webhook_view, name="whatsapp_webhook"),
]


