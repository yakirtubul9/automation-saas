from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import Appointment, AuditEvent, Business, BusinessMembership, Client, Provider, Room, Service


# Some installations may not yet include all enum members / fields.
# Keep the API resilient during incremental migrations.
RESERVED_STATUS = getattr(Appointment.Status, "RESERVED", "reserved")


ACTIVE_APPOINTMENT_STATUSES = {
    RESERVED_STATUS,
    Appointment.Status.SCHEDULED,
    Appointment.Status.CONFIRMED,
    Appointment.Status.CANCELLATION_REQUESTED,
}


def _safe_create_audit_event(**kwargs: Any) -> None:
    """Create an AuditEvent if the model supports the provided fields.

    Your project evolves; older DB schemas may not have the newer columns.
    We silently drop unknown fields so API endpoints won't crash.
    """

    try:
        field_names = {f.name for f in AuditEvent._meta.fields}
        create_kwargs = {k: v for k, v in kwargs.items() if k in field_names}
        if create_kwargs:
            AuditEvent.objects.create(**create_kwargs)
    except Exception:
        # Audit must never break core flows.
        return


def _json_error(status: int, code: str, message: str, *, extra: Optional[dict[str, Any]] = None) -> JsonResponse:
    payload: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if extra:
        payload["error"].update(extra)
    return JsonResponse(payload, status=status)


def _get_business_for_user(user) -> Optional[Business]:
    """Best-effort business resolution.

    Priority:
      1) provider_profile.business
      2) membership.business
      3) owned business
    """
    provider = getattr(user, "provider_profile", None)
    if provider and getattr(provider, "business_id", None):
        return provider.business

    membership = BusinessMembership.objects.filter(user=user).select_related("business").order_by("id").first()
    if membership:
        return membership.business

    return Business.objects.filter(owner=user).order_by("id").first()


def _parse_datetime_in_business_tz(*, business: Business, value: str) -> Optional[datetime]:
    """Parse ISO datetime.

    Accepts:
      - aware dt (with timezone) -> returned as-is
      - naive dt -> interpreted in business timezone
    """
    dt = parse_datetime(value)
    if dt is None:
        return None
    if timezone.is_aware(dt):
        return dt
    tz = ZoneInfo(getattr(business, "timezone", "Asia/Jerusalem") or "Asia/Jerusalem")
    return timezone.make_aware(dt, tz)


def _overlaps_qs(*, business: Business, start: datetime, end: datetime, **kwargs):
    return Appointment.objects.filter(
        business=business,
        start_time__lt=end,
        end_time__gt=start,
        status__in=ACTIVE_APPOINTMENT_STATUSES,
        **kwargs,
    )


@dataclass(frozen=True)
class ReserveSlotInput:
    start_time: datetime
    end_time: datetime
    provider_id: Optional[int]


def _parse_reserve_slot_input(request: HttpRequest, business: Business) -> ReserveSlotInput | JsonResponse:
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return _json_error(400, "bad_json", "Body must be valid JSON")

    start_raw = body.get("start_time")
    end_raw = body.get("end_time")
    provider_id = body.get("provider_id")

    if not start_raw or not end_raw:
        return _json_error(400, "missing_fields", "start_time and end_time are required")

    start_dt = _parse_datetime_in_business_tz(business=business, value=str(start_raw))
    end_dt = _parse_datetime_in_business_tz(business=business, value=str(end_raw))
    if start_dt is None or end_dt is None:
        return _json_error(
            400,
            "bad_datetime",
            "start_time/end_time must be ISO datetime (e.g. 2026-01-15T12:00 or 2026-01-15T12:00:00+02:00)",
        )

    if start_dt >= end_dt:
        return _json_error(400, "bad_range", "start_time must be earlier than end_time")

    # sanity limits: prevent accidental huge reservations
    if (end_dt - start_dt).total_seconds() > 12 * 60 * 60:
        return _json_error(400, "too_long", "Reservation too long (max 12 hours)")

    now = timezone.now()
    if end_dt <= now:
        return _json_error(400, "in_past", "end_time is in the past")

    pid: Optional[int] = None
    if provider_id is not None:
        try:
            pid = int(provider_id)
        except (TypeError, ValueError):
            return _json_error(400, "bad_provider_id", "provider_id must be an integer")

    return ReserveSlotInput(start_time=start_dt, end_time=end_dt, provider_id=pid)


@login_required
def reserve_slot_view(request: HttpRequest) -> JsonResponse:
    """Reserve a slot for a provider (creates an Appointment with client=NULL, status=reserved).

    POST JSON:
      {
        "start_time": "2026-01-15T12:00",
        "end_time": "2026-01-15T14:00",
        "provider_id": 123   # optional; required for staff/owner reserving for someone else
      }
    """
    if request.method != "POST":
        return _json_error(405, "method_not_allowed", "Use POST")

    business = _get_business_for_user(request.user)
    if not business:
        return _json_error(403, "no_business", "No business context for this user")

    parsed = _parse_reserve_slot_input(request, business)
    if isinstance(parsed, JsonResponse):
        return parsed

    # Resolve provider permissions
    provider: Optional[Provider] = None
    caller_provider = getattr(request.user, "provider_profile", None)

    if parsed.provider_id is None:
        # provider reserves for self
        if not caller_provider:
            return _json_error(400, "missing_provider", "provider_id is required for non-provider users")
        provider = caller_provider
    else:
        # staff/owner reserves for a provider
        provider = Provider.objects.filter(pk=parsed.provider_id, business=business).first()
        if not provider:
            return _json_error(404, "provider_not_found", "Provider not found")

        # Check role in this business
        role = (
            BusinessMembership.objects.filter(user=request.user, business=business)
            .values_list("role", flat=True)
            .first()
        )
        if role not in {BusinessMembership.Role.OWNER, BusinessMembership.Role.STAFF}:
            return _json_error(403, "forbidden", "Only owner/staff can reserve slots for other providers")

    if not provider.specialty_id:
        return _json_error(400, "provider_missing_specialty", "Provider must have a specialty to match rooms")

    # Candidate rooms must match provider specialty and be active.
    base_rooms = (
        Room.objects.filter(
            business=business,
            is_active=True,
            specialties=provider.specialty,
        )
        .distinct()
        .order_by("id")
    )
    if not base_rooms.exists():
        return _json_error(
            409,
            "no_matching_rooms",
            "No active rooms match this provider's specialty",
        )

    start_dt = parsed.start_time
    end_dt = parsed.end_time

    with transaction.atomic():
        # Lock candidate rooms to reduce race conditions.
        rooms = list(base_rooms.select_for_update())

        # provider conflict check (one query)
        if _overlaps_qs(business=business, start=start_dt, end=end_dt, provider=provider).exists():
            return _json_error(409, "provider_conflict", "Provider already has a conflicting slot")

        chosen_room: Optional[Room] = None
        for room in rooms:
            if not _overlaps_qs(business=business, start=start_dt, end=end_dt, room=room).exists():
                chosen_room = room
                break

        if not chosen_room:
            return _json_error(409, "no_room_available", "No matching room is available for this time range")

        appt = Appointment.objects.create(
            business=business,
            provider=provider,
            room=chosen_room,
            client=None,
            service=None,
            start_time=start_dt,
            end_time=end_dt,
            status=RESERVED_STATUS,
        )

        _safe_create_audit_event(
            business=business,
            actor=request.user,
            entity_type="Appointment",
            entity_id=str(appt.id),
            action="reserve_slot",
            before=None,
            after={
                "id": appt.id,
                "provider_id": provider.id,
                "room_id": chosen_room.id,
                "start_time": appt.start_time.isoformat(),
                "end_time": appt.end_time.isoformat(),
                "status": appt.status,
            },
            meta={"channel": "api"},
        )

    return JsonResponse(
        {
            "ok": True,
            "appointment": {
                "id": appt.id,
                "business_id": business.id,
                "provider_id": provider.id,
                "room_id": chosen_room.id,
                "start_time": appt.start_time.isoformat(),
                "end_time": appt.end_time.isoformat(),
                "status": appt.status,
            },
        }
    )


def _get_user_role_in_business(user, business: Business) -> Optional[str]:
    return (
        BusinessMembership.objects.filter(user=user, business=business)
        .values_list("role", flat=True)
        .first()
    )


def _require_staff_or_owner(user, business: Business) -> bool:
    role = _get_user_role_in_business(user, business)
    return role in {BusinessMembership.Role.OWNER, BusinessMembership.Role.STAFF}


@login_required
def availability_view(request: HttpRequest) -> JsonResponse:
    """List upcoming free slots (reserved appointments without a client).

    GET params:
      - provider_id: optional int
      - limit: optional int (default 3, max 10)
      - from: optional ISO datetime (defaults now)
      - to: optional ISO datetime (defaults from+30 days)

    RBAC:
      - staff/owner can query any provider in the business
      - (future) provider users can omit provider_id to query themselves
    """
    if request.method != "GET":
        return _json_error(405, "method_not_allowed", "Use GET")

    business = _get_business_for_user(request.user)
    if not business:
        return _json_error(403, "no_business", "No business context for this user")

    provider_id_raw = request.GET.get("provider_id")
    caller_provider = getattr(request.user, "provider_profile", None)
    provider: Optional[Provider] = None

    if provider_id_raw:
        try:
            provider_id = int(provider_id_raw)
        except (TypeError, ValueError):
            return _json_error(400, "bad_provider_id", "provider_id must be an integer")

        if not _require_staff_or_owner(request.user, business):
            return _json_error(403, "forbidden", "Only owner/staff can query availability for other providers")

        provider = Provider.objects.filter(pk=provider_id, business=business).first()
        if not provider:
            return _json_error(404, "provider_not_found", "Provider not found")
    else:
        if not caller_provider:
            return _json_error(400, "missing_provider", "provider_id is required for non-provider users")
        provider = caller_provider

    limit_raw = request.GET.get("limit")
    limit = 3
    if limit_raw is not None and str(limit_raw).strip() != "":
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            return _json_error(400, "bad_limit", "limit must be an integer")
    limit = max(1, min(limit, 10))

    from_raw = request.GET.get("from")
    to_raw = request.GET.get("to")

    from_dt = timezone.now()
    if from_raw:
        parsed = _parse_datetime_in_business_tz(business=business, value=str(from_raw))
        if parsed is None:
            return _json_error(400, "bad_datetime", "from must be ISO datetime")
        from_dt = parsed

    to_dt = None
    if to_raw:
        parsed = _parse_datetime_in_business_tz(business=business, value=str(to_raw))
        if parsed is None:
            return _json_error(400, "bad_datetime", "to must be ISO datetime")
        to_dt = parsed
    else:
        to_dt = from_dt + timedelta(days=30)

    if to_dt <= from_dt:
        return _json_error(400, "bad_range", "to must be later than from")

    qs = (
        Appointment.objects.filter(
            business=business,
            provider=provider,
            client__isnull=True,
            status=RESERVED_STATUS,
            start_time__gte=from_dt,
            start_time__lt=to_dt,
        )
        .select_related("room")
        .order_by("start_time")
    )

    slots = []
    for appt in qs[:limit]:
        slots.append(
            {
                "slot_id": appt.id,
                "provider_id": appt.provider_id,
                "room_id": appt.room_id,
                "start_time": appt.start_time.isoformat(),
                "end_time": appt.end_time.isoformat(),
            }
        )

    return JsonResponse({"ok": True, "provider_id": provider.id, "slots": slots})


@dataclass(frozen=True)
class AssignClientInput:
    slot_id: int
    client_id: int
    service_id: Optional[int]


def _parse_assign_client_input(request: HttpRequest) -> AssignClientInput | JsonResponse:
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return _json_error(400, "bad_json", "Body must be valid JSON")

    slot_id = body.get("slot_id")
    client_id = body.get("client_id")
    service_id = body.get("service_id")

    if slot_id is None or client_id is None:
        return _json_error(400, "missing_fields", "slot_id and client_id are required")

    try:
        slot_id_int = int(slot_id)
        client_id_int = int(client_id)
    except (TypeError, ValueError):
        return _json_error(400, "bad_id", "slot_id/client_id must be integers")

    service_id_int: Optional[int] = None
    if service_id is not None:
        try:
            service_id_int = int(service_id)
        except (TypeError, ValueError):
            return _json_error(400, "bad_service_id", "service_id must be an integer")

    return AssignClientInput(slot_id=slot_id_int, client_id=client_id_int, service_id=service_id_int)


@login_required
def assign_client_view(request: HttpRequest) -> JsonResponse:
    """Assign a client to an existing reserved slot.

    POST JSON:
      {
        "slot_id": 123,
        "client_id": 456,
        "service_id": 789   # optional
      }

    Behavior:
      - slot must be RESERVED and have client=NULL
      - sets client + (optional) service
      - transitions status to SCHEDULED
      - creates default reminders (idempotent)

    RBAC:
      - staff/owner can assign any slot in their business
      - (future) provider users can assign only their own slots
    """
    if request.method != "POST":
        return _json_error(405, "method_not_allowed", "Use POST")

    business = _get_business_for_user(request.user)
    if not business:
        return _json_error(403, "no_business", "No business context for this user")

    parsed = _parse_assign_client_input(request)
    if isinstance(parsed, JsonResponse):
        return parsed

    caller_provider = getattr(request.user, "provider_profile", None)
    is_staff_owner = _require_staff_or_owner(request.user, business)

    with transaction.atomic():
        slot = (
            Appointment.objects.select_for_update()
            .filter(pk=parsed.slot_id, business=business)
            .first()
        )
        if not slot:
            return _json_error(404, "slot_not_found", "Slot not found")

        if not is_staff_owner:
            if not caller_provider:
                return _json_error(403, "forbidden", "Not allowed")
            if slot.provider_id != getattr(caller_provider, "id", None):
                return _json_error(403, "forbidden", "Providers can only assign their own slots")

        if slot.status != RESERVED_STATUS or slot.client_id is not None:
            return _json_error(409, "slot_not_available", "Slot is not available")

        if slot.end_time <= timezone.now():
            return _json_error(400, "in_past", "Slot is in the past")

        client = Client.objects.filter(pk=parsed.client_id, business=business, is_active=True).first()
        if not client:
            return _json_error(404, "client_not_found", "Client not found")

        service: Optional[Service] = None
        if parsed.service_id is not None:
            service = Service.objects.filter(pk=parsed.service_id, business=business, is_active=True).first()
            if not service:
                return _json_error(404, "service_not_found", "Service not found")

        # Re-validate conflicts in case something changed since the slot was created.
        start_dt, end_dt = slot.start_time, slot.end_time
        if slot.provider_id and _overlaps_qs(
            business=business,
            start=start_dt,
            end=end_dt,
            provider_id=slot.provider_id,
        ).exclude(pk=slot.pk).exists():
            return _json_error(409, "provider_conflict", "Provider already has a conflicting appointment")

        if slot.room_id and _overlaps_qs(
            business=business,
            start=start_dt,
            end=end_dt,
            room_id=slot.room_id,
        ).exclude(pk=slot.pk).exists():
            return _json_error(409, "room_conflict", "Room already has a conflicting appointment")

        before = {
            "id": slot.id,
            "client_id": slot.client_id,
            "service_id": slot.service_id,
            "status": slot.status,
        }

        slot.client = client
        if service is not None:
            slot.service = service
        slot.status = Appointment.Status.SCHEDULED
        slot.save()

        # Existing appointments don't auto-create reminders on update, so do it explicitly.
        from .reminders import ensure_reminders_for_appointment

        ensure_reminders_for_appointment(slot)

        after = {
            "id": slot.id,
            "client_id": slot.client_id,
            "service_id": slot.service_id,
            "status": slot.status,
        }

        _safe_create_audit_event(
            business=business,
            actor_user=request.user,
            action="assign_client_to_slot",
            object_type="Appointment",
            object_id=str(slot.id),
            before=before,
            after=after,
        )

    return JsonResponse(
        {
            "ok": True,
            "appointment": {
                "id": slot.id,
                "business_id": business.id,
                "provider_id": slot.provider_id,
                "room_id": slot.room_id,
                "client_id": slot.client_id,
                "service_id": slot.service_id,
                "start_time": slot.start_time.isoformat(),
                "end_time": slot.end_time.isoformat(),
                "status": slot.status,
            },
        }
    )
