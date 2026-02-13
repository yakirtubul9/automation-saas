# core/ui_views.py
from __future__ import annotations

from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponseForbidden
from django.shortcuts import render, get_object_or_404
from django.utils import timezone

from .authz import get_current_context
from .models import (
    Appointment,
    AuditEvent,
    BusinessMembership,
    Client,
    Provider,
    Reminder,
    Room,
    Service,
    WhatsAppMessage,
)


def _mask_client_name(*, role: str, provider: Provider | None, appointment: Appointment) -> str:
    """Default privacy: Staff/Owner don't see patient names by default."""
    if not appointment.client_id:
        return "-"
    if role == BusinessMembership.Role.PROVIDER and provider and appointment.provider_id == provider.id:
        return appointment.client.full_name
    return "מטופל"


def _require_ctx(request):
    ctx = get_current_context(request.user)
    if not ctx:
        return None, HttpResponseForbidden("No business context")
    return ctx, None


@login_required
def ui_home(request):
    ctx, err = _require_ctx(request)
    if err:
        return err
    return render(request, "UI/home.html", {"business": ctx.business, "role": ctx.role})


@login_required
def ui_appointments(request):
    ctx, err = _require_ctx(request)
    if err:
        return err

    business = ctx.business

    # Date filter
    day = (request.GET.get("day") or "").strip()
    if day:
        try:
            target_date = datetime.fromisoformat(day).date()
        except Exception:
            messages.warning(request, "תאריך לא תקין — מציג היום")
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()

    provider_id = (request.GET.get("provider") or "").strip()
    room_id = (request.GET.get("room") or "").strip()
    status = (request.GET.get("status") or "").strip()
    only_open = (request.GET.get("open") or "").strip() in {"1", "true", "yes"}

    qs = (
        Appointment.objects.select_related("client", "provider", "room", "service")
        .filter(business=business, start_time__date=target_date)
        .order_by("start_time")
    )

    # Role scoping: Provider sees only their own appointments/slots (best-effort).
    if ctx.role == BusinessMembership.Role.PROVIDER and ctx.provider:
        qs = qs.filter(provider=ctx.provider)

    if provider_id.isdigit():
        qs = qs.filter(provider_id=int(provider_id))
    if room_id.isdigit():
        qs = qs.filter(room_id=int(room_id))
    if status:
        qs = qs.filter(status=status)
    if only_open:
        qs = qs.filter(client__isnull=True)

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    rows = [
        {
            "id": a.id,
            "start": a.start_time,
            "end": a.end_time,
            "provider": str(a.provider) if a.provider_id else "-",
            "room": str(a.room) if a.room_id else "-",
            "client": _mask_client_name(role=ctx.role, provider=ctx.provider, appointment=a),
            "service": a.service.name if a.service_id else "-",
            "status": a.status,
            "status_label": a.get_status_display(),
        }
        for a in page_obj.object_list
    ]

    providers = Provider.objects.filter(business=business, is_active=True).order_by("display_name")
    rooms = Room.objects.filter(business=business, is_active=True).order_by("name")
    status_choices = list(Appointment.Status.choices)

    return render(
        request,
        "UI/appointments.html",
        {
            "business": business,
            "role": ctx.role,
            "target_date": target_date,
            "rows": rows,
            "providers": providers,
            "rooms": rooms,
            "status_choices": status_choices,
            "filters": {
                "provider": provider_id,
                "room": room_id,
                "status": status,
                "open": "1" if only_open else "",
            },
            "page_obj": page_obj,
        },
    )


@login_required
def ui_clients(request):
    ctx, err = _require_ctx(request)
    if err:
        return err
    business = ctx.business

    q = (request.GET.get("q") or "").strip()
    clients = Client.objects.filter(business=business, is_active=True).order_by("full_name")
    if q:
        clients = clients.filter(full_name__icontains=q)

    paginator = Paginator(clients, 50)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(
        request,
        "UI/clients.html",
        {
            "business": business,
            "role": ctx.role,
            "q": q,
            "clients": page_obj.object_list,
            "page_obj": page_obj,
        },
    )


@login_required
def ui_client_detail(request, client_id: int):
    ctx, err = _require_ctx(request)
    if err:
        return err
    business = ctx.business

    # Privacy: Staff/Owner can see the client record only if we later decide to enable it.
    # For now, allow listing, but keep it minimal (no sensitive medical data exists anyway).
    client = get_object_or_404(Client, pk=client_id, business=business)

    appts = (
        Appointment.objects.select_related("service", "provider", "room")
        .filter(business=business, client=client)
        .order_by("-start_time")
    )
    paginator = Paginator(appts, 25)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(
        request,
        "UI/client_detail.html",
        {
            "business": business,
            "role": ctx.role,
            "client": client,
            "appts": page_obj.object_list,
            "page_obj": page_obj,
        },
    )


@login_required
def ui_clinic(request):
    ctx, err = _require_ctx(request)
    if err:
        return err
    business = ctx.business

    # Basic UI is readable for all roles, but editing is not part of this screen yet.
    rooms = Room.objects.filter(business=business).order_by("name")
    services = Service.objects.filter(business=business).order_by("name")
    providers = Provider.objects.filter(business=business).order_by("display_name")

    return render(
        request,
        "UI/clinic.html",
        {
            "business": business,
            "role": ctx.role,
            "rooms": rooms,
            "services": services,
            "providers": providers,
        },
    )


@login_required
def ui_ops(request):
    ctx, err = _require_ctx(request)
    if err:
        return err
    business = ctx.business

    tab = (request.GET.get("tab") or "errors").strip().lower()
    if tab not in {"errors", "whatsapp", "reminders", "audits"}:
        tab = "errors"

    # Recent errors: audit events marked as failed / WhatsApp outbounds without wa_message_id, etc.
    # We don't have a single "error" model, so we surface the most actionable signals.
    audits_qs = AuditEvent.objects.filter(business=business).order_by("-id")
    wa_qs = WhatsAppMessage.objects.filter(business=business).order_by("-id")
    reminders_qs = Reminder.objects.select_related("appointment").filter(appointment__business=business).order_by("-id")

    # Heuristic errors view:
    error_audits = audits_qs.filter(action__icontains="fail")[:50]
    error_wa = wa_qs.filter(direction=WhatsAppMessage.Direction.OUTBOUND, wa_message_id="")[:50]

    page_size = 50
    if tab == "audits":
        paginator = Paginator(audits_qs, page_size)
        page_obj = paginator.get_page(request.GET.get("page") or 1)
        return render(
            request,
            "UI/ops.html",
            {
                "business": business,
                "role": ctx.role,
                "tab": tab,
                "page_obj": page_obj,
                "audits": page_obj.object_list,
            },
        )

    if tab == "whatsapp":
        paginator = Paginator(wa_qs, page_size)
        page_obj = paginator.get_page(request.GET.get("page") or 1)
        return render(
            request,
            "UI/ops.html",
            {
                "business": business,
                "role": ctx.role,
                "tab": tab,
                "page_obj": page_obj,
                "whatsapp": page_obj.object_list,
            },
        )

    if tab == "reminders":
        paginator = Paginator(reminders_qs, page_size)
        page_obj = paginator.get_page(request.GET.get("page") or 1)
        return render(
            request,
            "UI/ops.html",
            {
                "business": business,
                "role": ctx.role,
                "tab": tab,
                "page_obj": page_obj,
                "reminders": page_obj.object_list,
            },
        )

    # errors
    return render(
        request,
        "UI/ops.html",
        {
            "business": business,
            "role": ctx.role,
            "tab": "errors",
            "error_audits": error_audits,
            "error_wa": error_wa,
        },
    )
