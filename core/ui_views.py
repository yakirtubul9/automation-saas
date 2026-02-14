# core/ui_views.py
from __future__ import annotations

from datetime import datetime, date
import re
from typing import Any, Optional

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import QuerySet
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import select_template
from django.utils import timezone

from .models import Appointment, Client, Room, Service, Reminder, AuditEvent, Provider, BusinessMembership
from .views import _get_or_create_business_for_user


_DIGITS_RE = re.compile(r"\D+")


def _digits_only(value: str) -> str:
    return _DIGITS_RE.sub("", value or "")


def _mask_client_name(*, role: str, appt: Appointment, current_provider: Optional[Provider]) -> str:
    """
    Privacy by default:
    - Staff/Owner: never see patient names.
    - Provider: sees patient name only for own appointments.
    """
    if not appt.client_id:
        return ""
    if role in (BusinessMembership.Role.STAFF, BusinessMembership.Role.OWNER):
        return "מטופל"
    if role == BusinessMembership.Role.PROVIDER:
        if current_provider and appt.provider_id == current_provider.id:
            # Best-effort: show real name
            try:
                return appt.client.display_name
            except Exception:
                return ""
        return "מטופל"
    return "מטופל"


def _parse_day_param(day_str: str | None) -> date:
    if not day_str:
        return timezone.localdate()
    try:
        # HTML date input => YYYY-MM-DD
        return datetime.strptime(day_str, "%Y-%m-%d").date()
    except Exception:
        return timezone.localdate()


def _clamp_int(value: Any, default: int, *, min_v: int, max_v: int) -> int:
    try:
        n = int(value)
    except Exception:
        return default
    return max(min_v, min(max_v, n))


def _get_membership_for_business(user, business):
    return (
        BusinessMembership.objects.filter(user=user, business=business)
        .order_by("id")
        .first()
    )


def _resolve_current_provider(*, business, membership: Optional[BusinessMembership]) -> Optional[Provider]:
    """
    There is no Provider.user FK in the project yet.
    Best-effort mapping:
      - match membership.whatsapp_number (digits) to Provider.whatsapp_number (digits)
      - fallback: if exactly one provider in business, use it
    """
    if not membership:
        return None
    if membership.role != BusinessMembership.Role.PROVIDER:
        return None

    mem_digits = _digits_only(membership.whatsapp_number)
    if mem_digits:
        cand = None
        for p in Provider.objects.filter(business=business).only("id", "whatsapp_number", "display_name"):
            if _digits_only(p.whatsapp_number) == mem_digits:
                cand = p
                break
        if cand:
            return cand

    providers = list(Provider.objects.filter(business=business).only("id", "whatsapp_number", "display_name")[:2])
    if len(providers) == 1:
        return providers[0]
    return None


@login_required
def ui_home(request):
    return render(request, "app/home.html")


@login_required
def ui_appointments(request):
    business = _get_or_create_business_for_user(request.user)
    membership = _get_membership_for_business(request.user, business)
    current_role = (membership.role if membership else BusinessMembership.Role.STAFF)
    current_provider = _resolve_current_provider(business=business, membership=membership)

    day = _parse_day_param(request.GET.get("day"))
    open_only = request.GET.get("open") in ("1", "true", "yes", "on")

    provider_id = request.GET.get("provider") or ""
    room_id = request.GET.get("room") or ""
    status = request.GET.get("status") or ""

    # Robust pagination params
    per_page = _clamp_int(request.GET.get("per_page"), 25, min_v=1, max_v=200)
    page_number = request.GET.get("page") or request.GET.get("p") or "1"
    page_number = _clamp_int(page_number, 1, min_v=1, max_v=10_000)

    qs: QuerySet[Appointment] = (
        Appointment.objects.filter(business=business, start_time__date=day)
        .select_related("provider", "room", "client", "service")
        .order_by("start_time", "id")
    )

    # If Provider is logged in, default to own provider filter (but still allow explicit param)
    if current_role == BusinessMembership.Role.PROVIDER and current_provider and not provider_id:
        provider_id = str(current_provider.id)

    if provider_id:
        qs = qs.filter(provider_id=provider_id)
    if room_id:
        qs = qs.filter(room_id=room_id)
    if status:
        qs = qs.filter(status=status)
    if open_only:
        qs = qs.filter(client__isnull=True)

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(page_number)
    page_qs = page_obj.object_list

    # Rows for new UI templates/tests
    rows: list[dict[str, Any]] = []
    for appt in page_qs:
        rows.append(
            {
                "id": appt.id,
                "start": appt.start_time,
                "end": appt.end_time,
                "provider": getattr(appt.provider, "name", "") if appt.provider_id else "",
                "room": getattr(appt.room, "name", "") if appt.room_id else "",
                "client": _mask_client_name(role=current_role, appt=appt, current_provider=current_provider),
                "service": getattr(appt.service, "name", "") if appt.service_id else "",
                "status": appt.status,
            }
        )

    providers = Provider.objects.filter(business=business).order_by("display_name")
    rooms = Room.objects.filter(business=business).order_by("name")
    statuses = Appointment.Status.choices  # type: ignore[attr-defined]

    # Choose first existing template to avoid TemplateDoesNotExist across branches
    template = select_template(
        [
            "app/appointments.html",
            "app/appointments_list.html",
            "UI/appointments.html",
        ]
    )

    context = {
        "business": business,
        "current_role": current_role,
        "current_provider": current_provider,
        "day": day,
        "providers": providers,
        "rooms": rooms,
        "statuses": statuses,
        "appointments": page_qs,  # keeps old templates working
        "rows": rows,  # used by tests + new templates
        "page_obj": page_obj,
        "paginator": paginator,
        "per_page": per_page,
        "filters": {
            "provider": provider_id,
            "room": room_id,
            "status": status,
            "open": "1" if open_only else "",
        },
    }
    return render(request, template.template.name, context)


@login_required
def ui_clients(request):
    business = _get_or_create_business_for_user(request.user)
    membership = _get_membership_for_business(request.user, business)
    current_role = (membership.role if membership else BusinessMembership.Role.STAFF)
    current_provider = _resolve_current_provider(business=business, membership=membership)

    q = (request.GET.get("q") or "").strip()
    per_page = _clamp_int(request.GET.get("per_page"), 25, min_v=1, max_v=200)
    page_number = request.GET.get("page") or request.GET.get("p") or "1"
    page_number = _clamp_int(page_number, 1, min_v=1, max_v=10_000)

    qs = Client.objects.filter(business=business).order_by("display_name", "id")
    if q:
        qs = qs.filter(display_name__icontains=q)

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(page_number)

    template = select_template(
        [
            "app/clients.html",
            "app/clients_list.html",
            "UI/clients.html",
        ]
    )
    return render(
        request,
        template.template.name,
        {
            "business": business,
            "current_role": current_role,
            "current_provider": current_provider,
            "clients": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "q": q,
            "per_page": per_page,
        },
    )


@login_required
def ui_ops(request):
    business = _get_or_create_business_for_user(request.user)
    template = select_template(["app/ops.html", "UI/ops.html"])
    return render(request, template.template.name, {"business": business})


# ---------------------------------------------------------------------------
# MVP-safe stubs for template URL reversals.
# These exist because the server-rendered templates include links/actions to
# create/edit/change-status flows that are not required for the Stage 11 tests.
# They must exist to prevent NoReverseMatch / AttributeError during rendering.
# ---------------------------------------------------------------------------


@login_required
def ui_appointment_create(request):
    # Not implemented in MVP UI polish stage. Redirect back to the calendar.
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    return redirect("ui_appointments")


@login_required
def ui_appointment_edit(request, appt_id: int):
    # Minimal: ensure object exists in current business, then redirect.
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    business = _get_or_create_business_for_user(request.user)
    get_object_or_404(Appointment, id=appt_id, business=business)
    return redirect("ui_appointments")


@login_required
def ui_appointment_status(request, appt_id: int):
    # Minimal: accept POST and redirect. Real status changes are handled elsewhere.
    if request.method not in ("POST", "GET"):
        return HttpResponseNotAllowed(["GET", "POST"])
    business = _get_or_create_business_for_user(request.user)
    get_object_or_404(Appointment, id=appt_id, business=business)
    return redirect("ui_appointments")


@login_required
def ui_client_detail(request, client_id: int):
    business = _get_or_create_business_for_user(request.user)
    get_object_or_404(Client, id=client_id, business=business)
    return redirect("ui_clients")


@login_required
def ui_clinic(request):
    # Placeholder for clinic settings screen.
    return HttpResponse("OK")
