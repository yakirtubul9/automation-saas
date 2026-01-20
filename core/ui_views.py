# core/ui_views.py
from __future__ import annotations

from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.utils import timezone

from .models import Appointment, Client, Room, Service, Reminder, AuditEvent, Provider
from .views import _get_or_create_business_for_user


def _mask_client_name(*, request, appointment: Appointment) -> str:
    """Default privacy: Staff/Owner don't see patient names by default.

    Until we add strict RBAC in the UI, we keep the safe default:
    - Provider sees their patients.
    - Everyone else sees 'מטופל'.
    """
    if not appointment.client_id:
        return "-"
    # Provider user match (Provider has FK 'user')
    try:
        if appointment.provider_id and appointment.provider and appointment.provider.user_id == request.user.id:
            return appointment.client.full_name
    except Exception:
        pass
    return "מטופל"


@login_required
def ui_home(request):
    return render(request, "UI/home.html", {})


@login_required
def ui_appointments(request):
    business = _get_or_create_business_for_user(request.user)

    day = (request.GET.get("day") or "").strip()
    if day:
        try:
            target_date = datetime.fromisoformat(day).date()
        except Exception:
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()

    qs = (
        Appointment.objects.select_related("client", "provider", "room", "service")
        .filter(business=business, start_time__date=target_date)
        .order_by("start_time")
    )

    def _provider_label(a: Appointment) -> str:
        if not a.provider_id:
            return "-"
        # Provider model defines __str__ so this is fine
        return str(a.provider)

    def _room_label(a: Appointment) -> str:
        if not a.room_id:
            return "-"
        return str(a.room)

    rows = [
        {
            "id": a.id,
            "start": a.start_time,
            "end": a.end_time,
            "provider": _provider_label(a),
            "room": _room_label(a),
            "client": _mask_client_name(request=request, appointment=a),
            "service": a.service.name if a.service_id else "-",
            "status": a.get_status_display(),
        }
        for a in qs
    ]

    return render(
        request,
        "UI/appointments.html",
        {
            "business": business,
            "target_date": target_date,
            "rows": rows,
        },
    )


@login_required
def ui_clients(request):
    business = _get_or_create_business_for_user(request.user)
    q = (request.GET.get("q") or "").strip()
    clients = Client.objects.filter(business=business).order_by("full_name")
    if q:
        clients = clients.filter(full_name__icontains=q)

    return render(
        request,
        "UI/clients.html",
        {
            "business": business,
            "q": q,
            "clients": clients[:200],
        },
    )


@login_required
def ui_client_detail(request, client_id: int):
    business = _get_or_create_business_for_user(request.user)
    client = get_object_or_404(Client, pk=client_id, business=business)

    appts = (
        Appointment.objects.select_related("service", "provider", "room")
        .filter(business=business, client=client)
        .order_by("-start_time")[:50]
    )
    return render(
        request,
        "UI/client_detail.html",
        {
            "business": business,
            "client": client,
            "appts": appts,
        },
    )


@login_required
def ui_clinic(request):
    business = _get_or_create_business_for_user(request.user)
    rooms = Room.objects.filter(business=business).order_by("name")
    services = Service.objects.filter(business=business).order_by("name")
    providers = Provider.objects.filter(business=business).order_by("id")

    return render(
        request,
        "UI/clinic.html",
        {
            "business": business,
            "rooms": rooms,
            "services": services,
            "providers": providers,
        },
    )


@login_required
def ui_ops(request):
    business = _get_or_create_business_for_user(request.user)
    reminders = (
        Reminder.objects.select_related("appointment")
        .filter(appointment__business=business)
        .order_by("-id")[:50]
    )
    audits = AuditEvent.objects.filter(business=business).order_by("-id")[:50]
    return render(
        request,
        "UI/ops.html",
        {"business": business, "reminders": reminders, "audits": audits},
    )
