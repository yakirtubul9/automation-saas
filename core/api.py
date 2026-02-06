from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    Appointment,
    AppointmentChangeProposal,
    AuditEvent,
    Business,
    BusinessMembership,
    Client,
    Provider,
    Room,
    RoomBlock,
    Service,
    WaitlistEntry,
)
from .notifications import get_provider


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


def _room_block_overlaps(*, business: Business, room: Room, start: datetime, end: datetime) -> bool:
    return RoomBlock.objects.filter(
        business=business,
        room=room,
        is_active=True,
        start_time__lt=end,
        end_time__gt=start,
    ).exists()


def _choose_available_room(*, business: Business, provider: Provider, start: datetime, end: datetime) -> Optional[Room]:
    """Pick the first available room matching provider specialty, respecting room blocks."""
    if not provider.specialty_id:
        return None

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
        return None

    rooms = list(base_rooms.select_for_update())
    for room in rooms:
        if _room_block_overlaps(business=business, room=room, start=start, end=end):
            continue
        if not _overlaps_qs(business=business, start=start, end=end, room=room).exists():
            return room
    return None


def _suggest_alternatives(
    *,
    business: Business,
    provider: Provider,
    desired_start: datetime,
    desired_end: datetime,
    max_suggestions: int = 3,
) -> list[dict[str, Any]]:
    """Suggest alternatives when the requested time isn't feasible.

    Hardening goals:
      - suggest ONLY within business working hours (configurable via settings)
      - align to a configurable time grid (default: 15 minutes)
      - avoid suggesting slots that spill outside the working window

    Settings (optional):
      - BUSINESS_WORKING_HOURS: dict[int, list[tuple[str, str]]]
          weekday -> list of ("HH:MM", "HH:MM") windows.
          weekday uses Python's convention: Monday=0 .. Sunday=6.
          Example:
            {
              0: [("08:00","20:00")],
              1: [("08:00","20:00")],
              2: [("08:00","20:00")],
              3: [("08:00","20:00")],
              4: [("08:00","14:00")],
              5: [],
              6: [("08:00","20:00")]
            }
      - SLOT_STEP_MINUTES: int (default 15)
      - ALTERNATIVES_LOOKAHEAD_DAYS: int (default 14)
    """
    if max_suggestions <= 0:
        return []

    duration = desired_end - desired_start
    suggestions: list[dict[str, Any]] = []

    tz = ZoneInfo(getattr(business, "timezone", "Asia/Jerusalem") or "Asia/Jerusalem")
    step_min = int(getattr(settings, "SLOT_STEP_MINUTES", 15) or 15)
    step_min = max(5, min(step_min, 60))

    lookahead_days = int(getattr(settings, "ALTERNATIVES_LOOKAHEAD_DAYS", 14) or 14)
    lookahead_days = max(1, min(lookahead_days, 31))

    def _parse_hhmm(v: str) -> Optional[dtime]:
        try:
            hh, mm = v.split(":", 1)
            return dtime(hour=int(hh), minute=int(mm))
        except Exception:
            return None

    def _default_working_hours() -> dict[int, list[tuple[dtime, dtime]]]:
        """Israel-friendly defaults: Sun-Thu 08:00-20:00, Fri 08:00-14:00, Sat closed."""
        base = {
            0: [(dtime(8, 0), dtime(20, 0))],
            1: [(dtime(8, 0), dtime(20, 0))],
            2: [(dtime(8, 0), dtime(20, 0))],
            3: [(dtime(8, 0), dtime(20, 0))],
            4: [(dtime(8, 0), dtime(14, 0))],
            5: [],
            6: [(dtime(8, 0), dtime(20, 0))],
        }
        return base

    def _get_working_hours() -> dict[int, list[tuple[dtime, dtime]]]:
        raw = getattr(settings, "BUSINESS_WORKING_HOURS", None)
        if not isinstance(raw, dict):
            return _default_working_hours()

        parsed: dict[int, list[tuple[dtime, dtime]]] = {i: [] for i in range(7)}
        for k, windows in raw.items():
            try:
                weekday = int(k)
            except Exception:
                continue
            if weekday < 0 or weekday > 6:
                continue

            if not isinstance(windows, (list, tuple)):
                continue
            for w in windows:
                if not isinstance(w, (list, tuple)) or len(w) != 2:
                    continue
                st = _parse_hhmm(str(w[0]))
                en = _parse_hhmm(str(w[1]))
                if st and en and (datetime.combine(date.today(), st) < datetime.combine(date.today(), en)):
                    parsed[weekday].append((st, en))

        # If the user provided an empty/invalid dict, fallback.
        if not any(parsed.values()):
            return _default_working_hours()
        return parsed

    def _ceil_to_step(dt: datetime, *, step_minutes: int) -> datetime:
        """Ceil an aware datetime to the next step boundary (in local time)."""
        local = dt.astimezone(tz)
        discard = (local.minute % step_minutes) * 60 + local.second
        if discard == 0 and local.microsecond == 0:
            return local
        delta = step_minutes * 60 - discard
        rounded = local + timedelta(seconds=delta)
        return rounded.replace(second=0, microsecond=0)

    working_hours = _get_working_hours()

    # For alternatives we do NOT lock rows. Suggestions are advisory.
    # We also avoid per-candidate DB queries by preloading conflicts into memory.
    if not provider.specialty_id:
        return []

    eligible_rooms = list(
        Room.objects.filter(
            business=business,
            is_active=True,
            specialties=provider.specialty,
        )
        .distinct()
        .order_by("id")
    )
    if not eligible_rooms:
        return []

    range_start = desired_start
    range_end = desired_start + timedelta(days=lookahead_days + 1)

    def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
        if not intervals:
            return []
        intervals_sorted = sorted(intervals, key=lambda x: x[0])
        merged: list[tuple[datetime, datetime]] = []
        cur_s, cur_e = intervals_sorted[0]
        for s, e in intervals_sorted[1:]:
            if s >= cur_e:
                merged.append((cur_s, cur_e))
                cur_s, cur_e = s, e
            else:
                if e > cur_e:
                    cur_e = e
        merged.append((cur_s, cur_e))
        return merged

    def _has_overlap(merged: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
        # merged is sorted and non-overlapping.
        for s, e in merged:
            if s >= end:
                return False
            if e > start:
                return True
        return False

    provider_busy_raw = list(
        Appointment.objects.filter(
            business=business,
            provider=provider,
            start_time__lt=range_end,
            end_time__gt=range_start,
            status__in=ACTIVE_APPOINTMENT_STATUSES,
        ).values_list("start_time", "end_time")
    )
    provider_busy = _merge_intervals([(s, e) for s, e in provider_busy_raw])

    room_busy_map: dict[int, list[tuple[datetime, datetime]]] = {r.id: [] for r in eligible_rooms}

    room_appts = Appointment.objects.filter(
        business=business,
        room_id__in=[r.id for r in eligible_rooms],
        start_time__lt=range_end,
        end_time__gt=range_start,
        status__in=ACTIVE_APPOINTMENT_STATUSES,
    ).values_list("room_id", "start_time", "end_time")
    for rid, s, e in room_appts:
        room_busy_map[int(rid)].append((s, e))

    room_blocks = RoomBlock.objects.filter(
        business=business,
        room_id__in=[r.id for r in eligible_rooms],
        is_active=True,
        start_time__lt=range_end,
        end_time__gt=range_start,
    ).values_list("room_id", "start_time", "end_time")
    for rid, s, e in room_blocks:
        room_busy_map[int(rid)].append((s, e))

    for rid, intervals in list(room_busy_map.items()):
        room_busy_map[rid] = _merge_intervals(intervals)

    def _find_available_room(start: datetime, end: datetime) -> Optional[int]:
        for r in eligible_rooms:
            if not _has_overlap(room_busy_map.get(r.id, []), start, end):
                return r.id
        return None

    start_local = desired_start.astimezone(tz)
    base_day = start_local.date()

    # We iterate day-by-day within the lookahead range.
    for day_offset in range(0, lookahead_days + 1):
        cand_day = base_day + timedelta(days=day_offset)
        weekday = cand_day.weekday()
        windows = working_hours.get(weekday) or []
        if not windows:
            continue

        for win_start_t, win_end_t in windows:
            win_start = timezone.make_aware(datetime.combine(cand_day, win_start_t), tz)
            win_end = timezone.make_aware(datetime.combine(cand_day, win_end_t), tz)

            # First day: start after requested time (rounded up). Other days: from window start.
            scan_start = win_start
            if day_offset == 0:
                scan_start = max(win_start, _ceil_to_step(desired_start, step_minutes=step_min))
            else:
                scan_start = _ceil_to_step(win_start, step_minutes=step_min)

            # Skip if even the earliest start cannot fit.
            if scan_start + duration > win_end:
                continue

            # Iterate candidates on the time grid within the window.
            cur = scan_start
            # Hard cap in case someone sets a very small step.
            iter_guard = 0
            while cur + duration <= win_end:
                iter_guard += 1
                if iter_guard > 500:
                    break

                cand_start = cur
                cand_end = cand_start + duration

                # Provider conflict check (in-memory).
                if _has_overlap(provider_busy, cand_start, cand_end):
                    cur = cur + timedelta(minutes=step_min)
                    continue

                chosen_room_id = _find_available_room(cand_start, cand_end)
                if chosen_room_id is None:
                    cur = cur + timedelta(minutes=step_min)
                    continue

                suggestions.append(
                    {
                        "start_time": cand_start.isoformat(),
                        "end_time": cand_end.isoformat(),
                        "room_id": chosen_room_id,
                    }
                )
                if len(suggestions) >= max_suggestions:
                    return suggestions

                cur = cur + timedelta(minutes=step_min)

    return suggestions


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

    # Debug: confirm which DB this process is connected to (helps when Admin and webhook seem out of sync).
    try:
        from django.db import connection
        db = getattr(connection, 'settings_dict', {}) or {}
        print(f"[WA DB] ENGINE={db.get('ENGINE')} NAME={db.get('NAME')} HOST={db.get('HOST')}", flush=True)
    except Exception as e:
        print(f"[WA DB] failed: {e}", flush=True)


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

    start_dt = parsed.start_time
    end_dt = parsed.end_time

    with transaction.atomic():
        # provider conflict check (one query)
        if _overlaps_qs(business=business, start=start_dt, end=end_dt, provider=provider).exists():
            return _json_error(409, "provider_conflict", "Provider already has a conflicting slot")

        chosen_room = _choose_available_room(business=business, provider=provider, start=start_dt, end=end_dt)

        if not chosen_room:
            alternatives = _suggest_alternatives(
                business=business,
                provider=provider,
                desired_start=start_dt,
                desired_end=end_dt,
            )
            return _json_error(
                409,
                "no_room_available",
                "No matching room is available for this time range",
                extra={"alternatives": alternatives},
            )

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


@dataclass(frozen=True)
class ReserveSlotsInput:
    start_time: datetime
    end_time: datetime
    provider_id: Optional[int]
    rrule: dict[str, Any]


def _parse_reserve_slots_input(request: HttpRequest, business: Business) -> ReserveSlotsInput | JsonResponse:
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return _json_error(400, "bad_json", "Body must be valid JSON")

    # Debug: confirm which DB this process is connected to (helps when Admin and webhook seem out of sync).
    try:
        from django.db import connection
        db = getattr(connection, 'settings_dict', {}) or {}
        print(f"[WA DB] ENGINE={db.get('ENGINE')} NAME={db.get('NAME')} HOST={db.get('HOST')}", flush=True)
    except Exception as e:
        print(f"[WA DB] failed: {e}", flush=True)


    start_raw = body.get("start_time")
    end_raw = body.get("end_time")
    provider_id = body.get("provider_id")
    rrule = body.get("rrule")

    if not start_raw or not end_raw or not isinstance(rrule, dict):
        return _json_error(400, "missing_fields", "start_time, end_time and rrule are required")

    start_dt = _parse_datetime_in_business_tz(business=business, value=str(start_raw))
    end_dt = _parse_datetime_in_business_tz(business=business, value=str(end_raw))
    if start_dt is None or end_dt is None:
        return _json_error(400, "bad_datetime", "start_time/end_time must be ISO datetime")
    if start_dt >= end_dt:
        return _json_error(400, "bad_range", "start_time must be earlier than end_time")
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

    # Validate rule minimally.
    freq = str(rrule.get("freq", "")).lower().strip()
    if freq not in {"daily", "weekly", "monthly"}:
        return _json_error(400, "bad_rrule", "rrule.freq must be one of: daily, weekly, monthly")

    interval = rrule.get("interval", 1)
    try:
        interval_int = int(interval)
    except (TypeError, ValueError):
        return _json_error(400, "bad_rrule", "rrule.interval must be an integer")
    if interval_int < 1 or interval_int > 52:
        return _json_error(400, "bad_rrule", "rrule.interval must be between 1 and 52")
    rrule["interval"] = interval_int

    count = rrule.get("count")
    if count is not None:
        try:
            count_int = int(count)
        except (TypeError, ValueError):
            return _json_error(400, "bad_rrule", "rrule.count must be an integer")
        if count_int < 1 or count_int > 200:
            return _json_error(400, "bad_rrule", "rrule.count must be between 1 and 200")
        rrule["count"] = count_int

    until_raw = rrule.get("until")
    if until_raw is not None:
        until_dt = _parse_datetime_in_business_tz(business=business, value=str(until_raw))
        if until_dt is None:
            return _json_error(400, "bad_rrule", "rrule.until must be ISO datetime")
        rrule["until"] = until_dt

    byweekday = rrule.get("byweekday")
    if byweekday is not None:
        if not isinstance(byweekday, list):
            return _json_error(400, "bad_rrule", "rrule.byweekday must be a list of integers 0..6")
        try:
            days = [int(x) for x in byweekday]
        except (TypeError, ValueError):
            return _json_error(400, "bad_rrule", "rrule.byweekday must be a list of integers 0..6")
        if any(d < 0 or d > 6 for d in days):
            return _json_error(400, "bad_rrule", "rrule.byweekday entries must be between 0 and 6")
        rrule["byweekday"] = sorted(set(days))

    return ReserveSlotsInput(start_time=start_dt, end_time=end_dt, provider_id=pid, rrule=rrule)


def _add_months(dt: datetime, months: int) -> datetime:
    """Add months to a datetime, clamping day-of-month when necessary."""
    # Preserve time/tz.
    year = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1

    # Clamp day.
    day = dt.day
    # Find last day of target month by stepping to first of next month minus a day.
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=dt.tzinfo)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=dt.tzinfo)
    last_day = (next_month - timedelta(days=1)).day
    day = min(day, last_day)
    return dt.replace(year=year, month=month, day=day)


def _generate_occurrences(*, start: datetime, end: datetime, rule: dict[str, Any]) -> list[tuple[datetime, datetime]]:
    """Generate (start,end) tuples based on a minimal RRULE subset."""
    freq = str(rule.get("freq", "")).lower().strip()
    interval: int = int(rule.get("interval", 1))
    count: Optional[int] = rule.get("count")
    until: Optional[datetime] = rule.get("until")
    byweekday: Optional[list[int]] = rule.get("byweekday")

    duration = end - start
    out: list[tuple[datetime, datetime]] = []

    if freq == "daily":
        i = 0
        while True:
            cur_start = start + timedelta(days=i * interval)
            cur_end = cur_start + duration
            if until is not None and cur_start > until:
                break
            out.append((cur_start, cur_end))
            i += 1
            if count is not None and len(out) >= int(count):
                break

    elif freq == "weekly":
        # Default weekday is the weekday of the initial start.
        weekdays = byweekday if byweekday else [start.weekday()]
        # Generate week by week.
        week_index = 0
        while True:
            week_start = start + timedelta(weeks=week_index * interval)
            # For each weekday in this week, compute the date.
            for wd in weekdays:
                delta = wd - week_start.weekday()
                cur_start = week_start + timedelta(days=delta)
                # Keep only occurrences on/after the initial start date.
                if cur_start < start:
                    continue
                cur_end = cur_start + duration
                if until is not None and cur_start > until:
                    return out
                out.append((cur_start, cur_end))
                if count is not None and len(out) >= int(count):
                    return out
            week_index += 1
            # Safety stop.
            if week_index > 400:
                break

    elif freq == "monthly":
        i = 0
        while True:
            cur_start = _add_months(start, i * interval)
            cur_end = cur_start + duration
            if until is not None and cur_start > until:
                break
            out.append((cur_start, cur_end))
            i += 1
            if count is not None and len(out) >= int(count):
                break

    return out


@login_required
def reserve_slots_view(request: HttpRequest) -> JsonResponse:
    """Reserve a series of slots (daily/weekly/monthly).

    POST JSON:
      {
        "start_time": "2026-02-01T12:00",
        "end_time": "2026-02-01T14:00",
        "provider_id": 123,  # optional for provider users
        "rrule": {"freq": "weekly", "interval": 1, "byweekday": [6], "count": 8}
      }

    Returns a mixed result with created items and skipped items (with optional alternatives).
    """
    if request.method != "POST":
        return _json_error(405, "method_not_allowed", "Use POST")

    business = _get_business_for_user(request.user)
    if not business:
        return _json_error(403, "no_business", "No business context for this user")

    parsed = _parse_reserve_slots_input(request, business)
    if isinstance(parsed, JsonResponse):
        return parsed

    # Resolve provider permissions (same policy as reserve_slot_view).
    provider: Optional[Provider] = None
    caller_provider = getattr(request.user, "provider_profile", None)

    if parsed.provider_id is None:
        if not caller_provider:
            return _json_error(400, "missing_provider", "provider_id is required for non-provider users")
        provider = caller_provider
    else:
        provider = Provider.objects.filter(pk=parsed.provider_id, business=business).first()
        if not provider:
            return _json_error(404, "provider_not_found", "Provider not found")
        role = (
            BusinessMembership.objects.filter(user=request.user, business=business)
            .values_list("role", flat=True)
            .first()
        )
        if role not in {BusinessMembership.Role.OWNER, BusinessMembership.Role.STAFF}:
            return _json_error(403, "forbidden", "Only owner/staff can reserve slots for other providers")

    if not provider.specialty_id:
        return _json_error(400, "provider_missing_specialty", "Provider must have a specialty to match rooms")

    occurrences = _generate_occurrences(start=parsed.start_time, end=parsed.end_time, rule=parsed.rrule)
    if not occurrences:
        return _json_error(400, "bad_rrule", "No occurrences generated")
    if len(occurrences) > 200:
        return _json_error(400, "bad_rrule", "Too many occurrences (max 200)")

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    with transaction.atomic():
        # Lock matching rooms to reduce race conditions for the whole batch.
        _ = list(
            Room.objects.filter(business=business, is_active=True, specialties=provider.specialty)
            .distinct()
            .select_for_update()
        )

        for (start_dt, end_dt) in occurrences:
            # Provider conflict
            if _overlaps_qs(business=business, start=start_dt, end=end_dt, provider=provider).exists():
                skipped.append(
                    {
                        "start_time": start_dt.isoformat(),
                        "end_time": end_dt.isoformat(),
                        "reason": "provider_conflict",
                    }
                )
                continue

            chosen_room = _choose_available_room(business=business, provider=provider, start=start_dt, end=end_dt)
            if chosen_room is None:
                skipped.append(
                    {
                        "start_time": start_dt.isoformat(),
                        "end_time": end_dt.isoformat(),
                        "reason": "no_room_available",
                        "alternatives": _suggest_alternatives(
                            business=business,
                            provider=provider,
                            desired_start=start_dt,
                            desired_end=end_dt,
                        ),
                    }
                )
                continue

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

            created.append(
                {
                    "id": appt.id,
                    "provider_id": provider.id,
                    "room_id": chosen_room.id,
                    "start_time": start_dt.isoformat(),
                    "end_time": end_dt.isoformat(),
                }
            )

    return JsonResponse(
        {
            "ok": True,
            "provider_id": provider.id,
            "created": created,
            "skipped": skipped,
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


@dataclass(frozen=True)
class RoomBlockInput:
    room_id: int
    start_time: datetime
    end_time: datetime
    reason: str


def _parse_room_block_input(request: HttpRequest, business: Business) -> RoomBlockInput | JsonResponse:
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return _json_error(400, "bad_json", "Body must be valid JSON")

    # Debug: confirm which DB this process is connected to (helps when Admin and webhook seem out of sync).
    try:
        from django.db import connection
        db = getattr(connection, 'settings_dict', {}) or {}
        print(f"[WA DB] ENGINE={db.get('ENGINE')} NAME={db.get('NAME')} HOST={db.get('HOST')}", flush=True)
    except Exception as e:
        print(f"[WA DB] failed: {e}", flush=True)


    room_id = body.get("room_id")
    start_raw = body.get("start_time")
    end_raw = body.get("end_time")
    reason = str(body.get("reason") or "").strip()

    if room_id is None or not start_raw or not end_raw:
        return _json_error(400, "missing_fields", "room_id, start_time and end_time are required")
    try:
        room_id_int = int(room_id)
    except (TypeError, ValueError):
        return _json_error(400, "bad_room_id", "room_id must be an integer")

    start_dt = _parse_datetime_in_business_tz(business=business, value=str(start_raw))
    end_dt = _parse_datetime_in_business_tz(business=business, value=str(end_raw))
    if start_dt is None or end_dt is None:
        return _json_error(400, "bad_datetime", "start_time/end_time must be ISO datetime")
    if start_dt >= end_dt:
        return _json_error(400, "bad_range", "start_time must be earlier than end_time")

    return RoomBlockInput(room_id=room_id_int, start_time=start_dt, end_time=end_dt, reason=reason)


@login_required
def room_block_view(request: HttpRequest) -> JsonResponse:
    """Create or list room blocks.

    POST: staff/owner only.
    GET: staff/owner only (MVP).
    """
    business = _get_business_for_user(request.user)
    if not business:
        return _json_error(403, "no_business", "No business context for this user")

    if not _require_staff_or_owner(request.user, business):
        return _json_error(403, "forbidden", "Only owner/staff can manage room blocks")

    if request.method == "POST":
        print("WA INBOUND POST received", flush=True)
        parsed = _parse_room_block_input(request, business)
        if isinstance(parsed, JsonResponse):
            return parsed

        room = Room.objects.filter(pk=parsed.room_id, business=business).first()
        if not room:
            return _json_error(404, "room_not_found", "Room not found")

        block = RoomBlock.objects.create(
            business=business,
            room=room,
            start_time=parsed.start_time,
            end_time=parsed.end_time,
            reason=parsed.reason,
            created_by=request.user,
            is_active=True,
        )

        impacted_ids = list(
            Appointment.objects.filter(
                business=business,
                room=room,
                status__in=ACTIVE_APPOINTMENT_STATUSES,
                start_time__lt=parsed.end_time,
                end_time__gt=parsed.start_time,
            ).values_list("id", flat=True)[:100]
        )

        return JsonResponse(
            {
                "ok": True,
                "block": {
                    "id": block.id,
                    "room_id": room.id,
                    "start_time": block.start_time.isoformat(),
                    "end_time": block.end_time.isoformat(),
                    "reason": block.reason,
                    "is_active": block.is_active,
                },
                "impacted_appointments": impacted_ids,
            }
        )

    if request.method == "GET":
        from_raw = request.GET.get("from")
        to_raw = request.GET.get("to")
        room_id_raw = request.GET.get("room_id")

        from_dt = timezone.now() - timedelta(days=30)
        if from_raw:
            parsed = _parse_datetime_in_business_tz(business=business, value=str(from_raw))
            if parsed is None:
                return _json_error(400, "bad_datetime", "from must be ISO datetime")
            from_dt = parsed

        to_dt = timezone.now() + timedelta(days=90)
        if to_raw:
            parsed = _parse_datetime_in_business_tz(business=business, value=str(to_raw))
            if parsed is None:
                return _json_error(400, "bad_datetime", "to must be ISO datetime")
            to_dt = parsed

        qs = RoomBlock.objects.filter(
            business=business,
            is_active=True,
            start_time__lt=to_dt,
            end_time__gt=from_dt,
        ).select_related("room").order_by("start_time")

        if room_id_raw:
            try:
                rid = int(room_id_raw)
            except (TypeError, ValueError):
                return _json_error(400, "bad_room_id", "room_id must be an integer")
            qs = qs.filter(room_id=rid)

        blocks = [
            {
                "id": b.id,
                "room_id": b.room_id,
                "start_time": b.start_time.isoformat(),
                "end_time": b.end_time.isoformat(),
                "reason": b.reason,
            }
            for b in qs[:200]
        ]

        return JsonResponse({"ok": True, "blocks": blocks})

    return _json_error(405, "method_not_allowed", "Use GET or POST")


# ============================
# Stage 4 — Waitlist (API-first)
# ============================


@dataclass(frozen=True)
class WaitlistEntryInput:
    client_id: int
    provider_id: Optional[int]
    service_id: Optional[int]
    preferred_weekdays: list[int]
    time_window_start: Optional[str]
    time_window_end: Optional[str]
    min_notice_hours: int
    notes: str


def _parse_waitlist_entry_input(request: HttpRequest) -> WaitlistEntryInput | JsonResponse:
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return _json_error(400, "bad_json", "Body must be valid JSON")

    # Debug: confirm which DB this process is connected to (helps when Admin and webhook seem out of sync).
    try:
        from django.db import connection
        db = getattr(connection, 'settings_dict', {}) or {}
        print(f"[WA DB] ENGINE={db.get('ENGINE')} NAME={db.get('NAME')} HOST={db.get('HOST')}", flush=True)
    except Exception as e:
        print(f"[WA DB] failed: {e}", flush=True)


    client_id = body.get("client_id")
    if client_id is None:
        return _json_error(400, "missing_fields", "client_id is required")
    try:
        client_id_int = int(client_id)
    except (TypeError, ValueError):
        return _json_error(400, "bad_client_id", "client_id must be an integer")

    provider_id = body.get("provider_id")
    provider_id_int: Optional[int] = None
    if provider_id is not None and provider_id != "":
        try:
            provider_id_int = int(provider_id)
        except (TypeError, ValueError):
            return _json_error(400, "bad_provider_id", "provider_id must be an integer")

    service_id = body.get("service_id")
    service_id_int: Optional[int] = None
    if service_id is not None and service_id != "":
        try:
            service_id_int = int(service_id)
        except (TypeError, ValueError):
            return _json_error(400, "bad_service_id", "service_id must be an integer")

    weekdays_raw = body.get("preferred_weekdays") or []
    weekdays: list[int] = []
    if isinstance(weekdays_raw, list):
        for x in weekdays_raw:
            try:
                xi = int(x)
            except (TypeError, ValueError):
                continue
            if 0 <= xi <= 6:
                weekdays.append(xi)

    time_start = body.get("time_window_start")
    time_end = body.get("time_window_end")
    time_start_s = str(time_start).strip() if time_start is not None else None
    time_end_s = str(time_end).strip() if time_end is not None else None
    if time_start_s == "":
        time_start_s = None
    if time_end_s == "":
        time_end_s = None

    min_notice = body.get("min_notice_hours") or 0
    try:
        min_notice_int = int(min_notice)
    except (TypeError, ValueError):
        return _json_error(400, "bad_min_notice_hours", "min_notice_hours must be an integer")
    if min_notice_int < 0:
        min_notice_int = 0

    notes = str(body.get("notes") or "").strip()

    return WaitlistEntryInput(
        client_id=client_id_int,
        provider_id=provider_id_int,
        service_id=service_id_int,
        preferred_weekdays=weekdays,
        time_window_start=time_start_s,
        time_window_end=time_end_s,
        min_notice_hours=min_notice_int,
        notes=notes,
    )


def _parse_time_optional(value: Optional[str]) -> Optional[dtime]:
    if not value:
        return None
    try:
        parts = str(value).strip().split(":")
        if len(parts) < 2:
            return None
        h = int(parts[0])
        m = int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return dtime(hour=h, minute=m)
    except Exception:
        return None


@login_required
def waitlist_view(request: HttpRequest) -> JsonResponse:
    """Create/list waitlist entries.

    POST JSON:
      {
        "client_id": 123,
        "service_id": 456,          # optional
        "provider_id": 789,         # optional
        "preferred_weekdays": [0,2],# optional (Mon=0..Sun=6)
        "time_window_start": "10:00",# optional
        "time_window_end": "14:00",  # optional
        "min_notice_hours": 24,     # optional
        "notes": "..."             # optional
      }

    GET:
      returns up to 200 entries.

    RBAC: owner/staff only (MVP)
    """
    business = _get_business_for_user(request.user)
    if not business:
        return _json_error(403, "no_business", "No business context for this user")

    if not _require_staff_or_owner(request.user, business):
        return _json_error(403, "forbidden", "Only owner/staff can manage waitlist")

    if request.method == "GET":
        qs = (
            WaitlistEntry.objects
            .filter(business=business)
            .select_related("client", "provider", "service")
            .order_by("-created_at")
        )
        entries = []
        for e in qs[:200]:
            entries.append(
                {
                    "id": e.id,
                    "client_id": e.client_id,
                    "provider_id": e.provider_id,
                    "service_id": e.service_id,
                    "preferred_weekdays": e.preferred_weekdays or [],
                    "time_window_start": e.time_window_start.isoformat() if e.time_window_start else None,
                    "time_window_end": e.time_window_end.isoformat() if e.time_window_end else None,
                    "min_notice_hours": e.min_notice_hours,
                    "status": e.status,
                    "notes": e.notes,
                    "created_at": e.created_at.isoformat(),
                }
            )
        return JsonResponse({"ok": True, "entries": entries})

    if request.method != "POST":
        return _json_error(405, "method_not_allowed", "Use GET or POST")

    parsed = _parse_waitlist_entry_input(request)
    if isinstance(parsed, JsonResponse):
        return parsed

    client = Client.objects.filter(pk=parsed.client_id, business=business, is_active=True).first()
    if not client:
        return _json_error(404, "client_not_found", "Client not found")

    provider = None
    if parsed.provider_id is not None:
        provider = Provider.objects.filter(pk=parsed.provider_id, business=business, is_active=True).first()
        if not provider:
            return _json_error(404, "provider_not_found", "Provider not found")

    service = None
    if parsed.service_id is not None:
        service = Service.objects.filter(pk=parsed.service_id, business=business, is_active=True).first()
        if not service:
            return _json_error(404, "service_not_found", "Service not found")

    tws = _parse_time_optional(parsed.time_window_start)
    twe = _parse_time_optional(parsed.time_window_end)
    if (parsed.time_window_start or parsed.time_window_end) and (tws is None or twe is None):
        return _json_error(400, "bad_time_window", "time_window_start/end must be HH:MM")

    entry = WaitlistEntry.objects.create(
        business=business,
        client=client,
        provider=provider,
        service=service,
        preferred_weekdays=parsed.preferred_weekdays,
        time_window_start=tws,
        time_window_end=twe,
        min_notice_hours=parsed.min_notice_hours,
        status=WaitlistEntry.Status.ACTIVE,
        notes=parsed.notes,
        created_by=request.user,
    )

    _safe_create_audit_event(
        business=business,
        actor_user=request.user,
        action="waitlist_entry_created",
        object_type="WaitlistEntry",
        object_id=str(entry.id),
        before=None,
        after={
            "client_id": entry.client_id,
            "provider_id": entry.provider_id,
            "service_id": entry.service_id,
            "preferred_weekdays": entry.preferred_weekdays,
            "time_window_start": entry.time_window_start.isoformat() if entry.time_window_start else None,
            "time_window_end": entry.time_window_end.isoformat() if entry.time_window_end else None,
            "min_notice_hours": entry.min_notice_hours,
        },
    )

    return JsonResponse({"ok": True, "entry_id": entry.id})


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

    # Debug: confirm which DB this process is connected to (helps when Admin and webhook seem out of sync).
    try:
        from django.db import connection
        db = getattr(connection, 'settings_dict', {}) or {}
        print(f"[WA DB] ENGINE={db.get('ENGINE')} NAME={db.get('NAME')} HOST={db.get('HOST')}", flush=True)
    except Exception as e:
        print(f"[WA DB] failed: {e}", flush=True)


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

        # Room/domain enforcement (Stage 2): when service has a specialty, ensure consistency.
        if service is not None and service.specialty_id is not None:
            if slot.provider_id and getattr(slot.provider, "specialty_id", None) is not None:
                if slot.provider.specialty_id != service.specialty_id:
                    return _json_error(409, "specialty_mismatch", "Service specialty does not match provider specialty")
            if slot.room_id and not slot.room.specialties.filter(pk=service.specialty_id).exists():
                return _json_error(409, "specialty_mismatch", "Service specialty does not match room specialty")

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

        # Stage 4 (Waitlist): slot is no longer available; cancel any pending offers for it.
        try:
            from .models import WaitlistOffer
            now = timezone.now()
            (WaitlistOffer.objects.filter(slot_id=slot.id, status=WaitlistOffer.Status.PENDING)
             .update(status=WaitlistOffer.Status.CANCELLED, decided_at=now, decision_note="slot_assigned"))
        except Exception:
            pass

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


# ============================
# Stage 3 — Clinic constraints change proposals (Approval flow)
# ============================


def _is_on_time_grid(dt: datetime, step_minutes: int) -> bool:
    return (
        dt.minute % step_minutes == 0
        and dt.second == 0
        and dt.microsecond == 0
    )


def _validate_within_working_hours(*, business: Business, start: datetime, end: datetime) -> tuple[bool, str]:
    """Validate that [start,end] fits within configured working hours.

    Uses settings.BUSINESS_WORKING_HOURS mapping weekday->(open,close).
    Weekday is ISO: Monday=0 ... Sunday=6.

    If no config exists, we allow any time (MVP default).
    """

    working_hours = getattr(settings, "BUSINESS_WORKING_HOURS", None)
    if not working_hours:
        return True, ""

    tz = ZoneInfo(getattr(business, "timezone", None) or "UTC")
    start_local = start.astimezone(tz) if timezone.is_aware(start) else start.replace(tzinfo=tz)
    end_local = end.astimezone(tz) if timezone.is_aware(end) else end.replace(tzinfo=tz)

    if start_local.date() != end_local.date():
        return False, "Appointment must not cross days"

    wd = start_local.weekday()
    day_cfg = working_hours.get(wd)
    if not day_cfg:
        return False, "Outside working days"

    open_hm, close_hm = day_cfg
    try:
        oh, om = map(int, str(open_hm).split(":", 1))
        ch, cm = map(int, str(close_hm).split(":", 1))
    except Exception:
        return False, "Invalid working hours configuration"

    open_dt = start_local.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_dt = start_local.replace(hour=ch, minute=cm, second=0, microsecond=0)

    if start_local < open_dt or end_local > close_dt:
        return False, "Outside working hours"

    return True, ""


def _appointment_is_changeable(appt: Appointment) -> bool:
    # Only allow changes for active appointments (including reserved slots)
    return appt.status in ACTIVE_APPOINTMENT_STATUSES


def _validate_appointment_move(*, business: Business, appointment: Appointment, provider: Provider, new_room: Room, new_start: datetime, new_end: datetime) -> tuple[bool, str, list[dict[str, Any]]]:
    """Validate that moving an appointment to (new_room, new_start, new_end) is allowed.

    Returns: (ok, error_message, alternatives)
    """

    if new_end <= new_start:
        return False, "end_time must be after start_time", []

    step_minutes = int(getattr(settings, "SLOT_STEP_MINUTES", 15))
    if not _is_on_time_grid(new_start, step_minutes) or not _is_on_time_grid(new_end, step_minutes):
        return False, "Times must align to the time grid", []

    ok_hours, msg = _validate_within_working_hours(business=business, start=new_start, end=new_end)
    if not ok_hours:
        return False, msg, []

    if not new_room.is_active:
        return False, "Room is inactive", []

    # Room specialty constraint (critical feature)
    if provider.specialty_id is not None:
        if not new_room.specialties.filter(pk=provider.specialty_id).exists():
            return False, "Room is not compatible with provider specialty", []

    # Room blocks
    if _room_block_overlaps(business=business, room=new_room, start=new_start, end=new_end):
        alts = _suggest_alternatives(
            business=business,
            provider=provider,
            desired_start=new_start,
            desired_end=new_end,
            max_suggestions=5,
        )
        return False, "Room is blocked at the requested time", alts

    # Room overlap excluding current appointment
    if _overlaps_qs(business=business, start=new_start, end=new_end, room_id=new_room.id).exclude(pk=appointment.pk).exists():
        alts = _suggest_alternatives(
            business=business,
            provider=provider,
            desired_start=new_start,
            desired_end=new_end,
            max_suggestions=5,
        )
        return False, "Room has a conflicting appointment", alts

    # Provider overlap excluding current appointment
    if provider and _overlaps_qs(business=business, start=new_start, end=new_end, provider_id=provider.id).exclude(pk=appointment.pk).exists():
        alts = _suggest_alternatives(
            business=business,
            provider=provider,
            desired_start=new_start,
            desired_end=new_end,
            max_suggestions=5,
        )
        return False, "Provider has a conflicting appointment", alts

    return True, "", []


def _validate_and_apply_appointment_change(*, proposal: AppointmentChangeProposal, appointment: Appointment, actor_user) -> tuple[bool, str, list[dict[str, Any]]]:
    """Re-validate and apply a proposal atomically.

    Called from the public approval view.
    """

    business = proposal.business
    if appointment.business_id != business.id:
        return False, "Business mismatch", []

    if not _appointment_is_changeable(appointment):
        return False, "Appointment is not changeable", []

    provider = appointment.provider
    if provider is None:
        return False, "Appointment missing provider", []

    new_room = proposal.proposed_room
    if new_room is None:
        return False, "Proposal missing room", []

    new_start = proposal.proposed_start_time
    new_end = proposal.proposed_end_time

    ok, msg, alts = _validate_appointment_move(
        business=business,
        appointment=appointment,
        provider=provider,
        new_room=new_room,
        new_start=new_start,
        new_end=new_end,
    )
    if not ok:
        return False, msg, alts

    before = {
        "id": appointment.id,
        "room_id": appointment.room_id,
        "start_time": appointment.start_time.isoformat(),
        "end_time": appointment.end_time.isoformat(),
        "status": appointment.status,
    }

    appointment.room = new_room
    appointment.start_time = new_start
    appointment.end_time = new_end
    appointment.save(update_fields=["room", "start_time", "end_time"])

    after = {
        "id": appointment.id,
        "room_id": appointment.room_id,
        "start_time": appointment.start_time.isoformat(),
        "end_time": appointment.end_time.isoformat(),
        "status": appointment.status,
    }

    _safe_create_audit_event(
        business=business,
        actor_user=actor_user,
        action="change_proposal_approved_and_applied",
        object_type="Appointment",
        object_id=str(appointment.id),
        before=before,
        after=after,
    )

    return True, "", []


@login_required
def change_proposal_create_view(request: HttpRequest) -> JsonResponse:
    """Create or list clinic change proposals (Staff/Owner).

    POST -> create a proposal and (best-effort) send approve/reject links to the provider.
    GET  -> list proposals for ops (filters supported).
    """

    business = _get_business_for_user(request.user)
    if not business:
        return _json_error(403, "no_business", "No business associated with user")

    # Staff/Owner only
    is_owner = business.owner_id == request.user.id
    is_staff = BusinessMembership.objects.filter(
        business=business,
        user=request.user,
        role__in=[BusinessMembership.Role.STAFF, BusinessMembership.Role.OWNER],
    ).exists()

    if not (is_owner or is_staff):
        return _json_error(403, "forbidden", "Only staff/owner can create change proposals")

    if request.method == "GET":
        status_filter = (request.GET.get("status") or "").strip()
        provider_id = (request.GET.get("provider_id") or "").strip()
        appointment_id = (request.GET.get("appointment_id") or "").strip()
        limit_raw = (request.GET.get("limit") or "50").strip()
        offset_raw = (request.GET.get("offset") or "0").strip()

        try:
            limit = max(1, min(200, int(limit_raw)))
            offset = max(0, int(offset_raw))
        except Exception:
            return _json_error(400, "invalid_pagination", "limit/offset must be integers")

        qs = (
            AppointmentChangeProposal.objects
            .filter(business=business)
            .select_related(
                "appointment",
                "appointment__provider",
                "appointment__room",
                "original_room",
                "proposed_room",
            )
            .order_by("-created_at")
        )

        if status_filter:
            statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
            qs = qs.filter(status__in=statuses)

        if provider_id:
            try:
                pid = int(provider_id)
                qs = qs.filter(appointment__provider_id=pid)
            except Exception:
                return _json_error(400, "invalid_provider_id", "provider_id must be an integer")

        if appointment_id:
            try:
                aid = int(appointment_id)
                qs = qs.filter(appointment_id=aid)
            except Exception:
                return _json_error(400, "invalid_appointment_id", "appointment_id must be an integer")

        total = qs.count()
        items = []
        for p in qs[offset : offset + limit]:
            appt = p.appointment
            items.append(
                {
                    "id": p.id,
                    "status": p.status,
                    "reason": p.reason,
                    "created_at": p.created_at.isoformat(),
                    "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                    "sent_at": p.sent_at.isoformat() if p.sent_at else None,
                    "send_error": p.send_error,
                    "last_error_code": p.last_error_code,
                    "last_error_message": p.last_error_message,
                    "last_error_payload": p.last_error_payload,
                    "last_attempted_at": p.last_attempted_at.isoformat() if p.last_attempted_at else None,
                    "appointment": {
                        "id": appt.id,
                        "provider_id": appt.provider_id,
                        "provider_name": appt.provider.display_name if appt.provider_id else "",
                        "room_id": appt.room_id,
                        "room_name": appt.room.name if appt.room_id else "",
                        "start_time": appt.start_time.isoformat(),
                        "end_time": appt.end_time.isoformat(),
                        "status": appt.status,
                    },
                    "proposed": {
                        "room_id": p.proposed_room_id,
                        "room_name": p.proposed_room.name if p.proposed_room_id else "",
                        "start_time": p.proposed_start_time.isoformat(),
                        "end_time": p.proposed_end_time.isoformat(),
                    },
                }
            )

        return JsonResponse({"ok": True, "total": total, "limit": limit, "offset": offset, "items": items})

    if request.method != "POST":
        return _json_error(405, "method_not_allowed", "GET or POST required")

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        return _json_error(400, "invalid_json", "Invalid JSON body")

    appt_id = data.get("appointment_id")
    if not appt_id:
        return _json_error(400, "missing_appointment_id", "appointment_id is required")

    try:
        appt = Appointment.objects.select_related("provider", "room", "business").get(pk=int(appt_id), business=business)
    except Appointment.DoesNotExist:
        return _json_error(404, "appointment_not_found", "Appointment not found")

    if not appt.provider_id:
        return _json_error(400, "missing_provider", "Appointment has no provider")

    provider = appt.provider

    if not _appointment_is_changeable(appt):
        return _json_error(409, "appointment_not_changeable", "Appointment cannot be changed in its current status")

    # Proposed values (defaults to current)
    proposed_start = appt.start_time
    proposed_end = appt.end_time

    if data.get("proposed_start_time"):
        parsed = _parse_datetime_in_business_tz(business=business, value=str(data.get("proposed_start_time")))
        if not parsed:
            return _json_error(400, "invalid_proposed_start_time", "Invalid proposed_start_time")
        proposed_start = parsed

    if data.get("proposed_end_time"):
        parsed = _parse_datetime_in_business_tz(business=business, value=str(data.get("proposed_end_time")))
        if not parsed:
            return _json_error(400, "invalid_proposed_end_time", "Invalid proposed_end_time")
        proposed_end = parsed

    proposed_room = appt.room
    if data.get("proposed_room_id") is not None:
        try:
            proposed_room = Room.objects.get(pk=int(data.get("proposed_room_id")), business=business)
        except Room.DoesNotExist:
            return _json_error(404, "room_not_found", "Room not found")

    if proposed_room is None:
        # Try to choose an available room
        proposed_room = _choose_available_room(business=business, provider=provider, start=proposed_start, end=proposed_end)
        if proposed_room is None:
            alts = _suggest_alternatives(business=business, provider=provider, desired_start=proposed_start, desired_end=proposed_end, max_suggestions=5)
            return _json_error(409, "no_room_available", "No compatible room available", extra={"alternatives": alts})

    if (
        proposed_start == appt.start_time
        and proposed_end == appt.end_time
        and proposed_room.id == appt.room_id
    ):
        return _json_error(400, "no_change", "Proposed change is identical to current appointment")

    reason = (data.get("reason") or "").strip()

    expires_in_minutes = data.get("expires_in_minutes")
    expires_at = None
    if expires_in_minutes is None:
        expires_in_minutes = int(getattr(settings, "CHANGE_PROPOSAL_DEFAULT_EXPIRES_MINUTES", 60))

    try:
        expires_in_minutes = int(expires_in_minutes)
    except Exception:
        return _json_error(400, "invalid_expires_in_minutes", "expires_in_minutes must be an integer")

    if expires_in_minutes < 0 or expires_in_minutes > 60 * 24 * 14:
        return _json_error(400, "invalid_expires_in_minutes", "expires_in_minutes out of allowed range")

    if expires_in_minutes > 0:
        expires_at = timezone.now() + timedelta(minutes=expires_in_minutes)

    # Validate move now (still re-validated on approval)
    ok, msg, alts = _validate_appointment_move(
        business=business,
        appointment=appt,
        provider=provider,
        new_room=proposed_room,
        new_start=proposed_start,
        new_end=proposed_end,
    )
    if not ok:
        return _json_error(409, "invalid_proposal", msg, extra={"alternatives": alts})

    proposal = AppointmentChangeProposal.objects.create(
        business=business,
        appointment=appt,
        original_room=appt.room,
        original_start_time=appt.start_time,
        original_end_time=appt.end_time,
        proposed_room=proposed_room,
        proposed_start_time=proposed_start,
        proposed_end_time=proposed_end,
        reason=reason,
        expires_at=expires_at,
        created_by=request.user,
    )

    # Build approve/reject links
    from .views import make_change_proposal_action_token, build_public_proposal_action_url

    approve_token = make_change_proposal_action_token(proposal_id=proposal.id, action="approve")
    reject_token = make_change_proposal_action_token(proposal_id=proposal.id, action="reject")

    approve_path = f"/p/{approve_token}/approve/"
    reject_path = f"/p/{reject_token}/reject/"

    approve_url = build_public_proposal_action_url(token=approve_token, action="approve")
    reject_url = build_public_proposal_action_url(token=reject_token, action="reject")

    # Send notification (best-effort)
    provider_number = (provider.whatsapp_number or "").strip()
    if provider_number:
        body = (
            "בקשת שינוי תור עקב אילוץ מרפאה.\n"
            f"סיבה: {reason or '-'}\n"
            f"מ: {appt.start_time:%Y-%m-%d %H:%M} עד {appt.end_time:%H:%M} (חדר: {(appt.room.name if appt.room_id else '-')})\n"
            f"ל: {proposed_start:%Y-%m-%d %H:%M} עד {proposed_end:%H:%M} (חדר: {(proposed_room.name if proposed_room else '-')})\n\n"
            f"לאישור: {approve_url}\n"
            f"לדחייה: {reject_url}\n"
        )
        try:
            msg_id = get_provider().send(to=provider_number, body=body, template_name="")
            proposal.sent_at = timezone.now()
            proposal.sent_message_id = msg_id
            proposal.save(update_fields=["sent_at", "sent_message_id"])
        except Exception as e:
            proposal.send_error = str(e)
            proposal.save(update_fields=["send_error"])

    _safe_create_audit_event(
        business=business,
        actor_user=request.user,
        action="change_proposal_created",
        object_type="AppointmentChangeProposal",
        object_id=str(proposal.id),
        before={
            "appointment": appt.id,
            "room_id": appt.room_id,
            "start_time": appt.start_time.isoformat(),
            "end_time": appt.end_time.isoformat(),
        },
        after={
            "proposed_room_id": proposal.proposed_room_id,
            "proposed_start_time": proposal.proposed_start_time.isoformat(),
            "proposed_end_time": proposal.proposed_end_time.isoformat(),
            "expires_at": proposal.expires_at.isoformat() if proposal.expires_at else None,
        },
    )

    return JsonResponse(
        {
            "ok": True,
            "proposal": {
                "id": proposal.id,
                "appointment_id": appt.id,
                "status": proposal.status,
                "expires_at": proposal.expires_at.isoformat() if proposal.expires_at else None,
            },
            "approve": {"path": approve_path, "url": approve_url},
            "reject": {"path": reject_path, "url": reject_url},
        }
    )


@login_required
def change_proposal_cancel_view(request: HttpRequest, proposal_id: int) -> JsonResponse:
    """Cancel a pending change proposal (Staff/Owner)."""

    if request.method != "POST":
        return _json_error(405, "method_not_allowed", "POST required")

    business = _get_business_for_user(request.user)
    if not business:
        return _json_error(403, "no_business", "No business associated with user")

    is_owner = business.owner_id == request.user.id
    is_staff = BusinessMembership.objects.filter(
        business=business,
        user=request.user,
        role__in=[BusinessMembership.Role.STAFF, BusinessMembership.Role.OWNER],
    ).exists()
    if not (is_owner or is_staff):
        return _json_error(403, "forbidden", "Only staff/owner can cancel change proposals")

    try:
        proposal = AppointmentChangeProposal.objects.select_related("appointment").get(pk=int(proposal_id), business=business)
    except AppointmentChangeProposal.DoesNotExist:
        return _json_error(404, "not_found", "Proposal not found")

    if proposal.status != AppointmentChangeProposal.Status.PENDING:
        return _json_error(409, "not_pending", "Only pending proposals can be cancelled")

    proposal.status = AppointmentChangeProposal.Status.CANCELLED
    proposal.decided_at = timezone.now()
    proposal.decision_note = "cancelled_by_staff"
    proposal.save(update_fields=["status", "decided_at", "decision_note"])

    _safe_create_audit_event(
        business=business,
        actor_user=request.user,
        action="change_proposal_cancelled",
        object_type="AppointmentChangeProposal",
        object_id=str(proposal.id),
        before={"status": AppointmentChangeProposal.Status.PENDING},
        after={"status": proposal.status},
    )

    return JsonResponse({"ok": True, "proposal": {"id": proposal.id, "status": proposal.status}})


@login_required
def change_proposal_resend_view(request: HttpRequest, proposal_id: int) -> JsonResponse:
    """Re-send approve/reject links to the provider (Staff/Owner)."""

    if request.method != "POST":
        return _json_error(405, "method_not_allowed", "POST required")

    business = _get_business_for_user(request.user)
    if not business:
        return _json_error(403, "no_business", "No business associated with user")

    is_owner = business.owner_id == request.user.id
    is_staff = BusinessMembership.objects.filter(
        business=business,
        user=request.user,
        role__in=[BusinessMembership.Role.STAFF, BusinessMembership.Role.OWNER],
    ).exists()
    if not (is_owner or is_staff):
        return _json_error(403, "forbidden", "Only staff/owner can resend change proposals")

    try:
        proposal = (
            AppointmentChangeProposal.objects
            .select_related("appointment", "appointment__provider", "appointment__room", "proposed_room")
            .get(pk=int(proposal_id), business=business)
        )
    except AppointmentChangeProposal.DoesNotExist:
        return _json_error(404, "not_found", "Proposal not found")

    if proposal.status != AppointmentChangeProposal.Status.PENDING:
        return _json_error(409, "not_pending", "Only pending proposals can be resent")

    appt = proposal.appointment
    provider = appt.provider
    provider_number = (provider.whatsapp_number or "").strip() if provider else ""
    if not provider_number:
        return _json_error(409, "missing_provider_number", "Provider has no WhatsApp number")

    from .views import make_change_proposal_action_token, build_public_proposal_action_url

    approve_token = make_change_proposal_action_token(proposal_id=proposal.id, action="approve")
    reject_token = make_change_proposal_action_token(proposal_id=proposal.id, action="reject")
    approve_url = build_public_proposal_action_url(token=approve_token, action="approve")
    reject_url = build_public_proposal_action_url(token=reject_token, action="reject")

    body = (
        "תזכורת: בקשת שינוי תור עקב אילוץ מרפאה.\n"
        f"סיבה: {proposal.reason or '-'}\n"
        f"מ: {proposal.original_start_time:%Y-%m-%d %H:%M} עד {proposal.original_end_time:%H:%M} (חדר: {(proposal.original_room.name if proposal.original_room_id else '-')})\n"
        f"ל: {proposal.proposed_start_time:%Y-%m-%d %H:%M} עד {proposal.proposed_end_time:%H:%M} (חדר: {(proposal.proposed_room.name if proposal.proposed_room_id else '-')})\n\n"
        f"לאישור: {approve_url}\n"
        f"לדחייה: {reject_url}\n"
    )

    try:
        msg_id = get_provider().send(to=provider_number, body=body, template_name="")
        proposal.sent_at = timezone.now()
        proposal.sent_message_id = msg_id
        proposal.send_error = ""
        proposal.save(update_fields=["sent_at", "sent_message_id", "send_error"])
    except Exception as e:
        proposal.send_error = str(e)
        proposal.save(update_fields=["send_error"])
        return _json_error(502, "send_failed", "Failed to send notification")

    _safe_create_audit_event(
        business=business,
        actor_user=request.user,
        action="change_proposal_resent",
        object_type="AppointmentChangeProposal",
        object_id=str(proposal.id),
        before={"sent_at": None},
        after={"sent_at": proposal.sent_at.isoformat(), "sent_message_id": proposal.sent_message_id},
    )

    return JsonResponse(
        {
            "ok": True,
            "proposal": {"id": proposal.id, "status": proposal.status, "sent_at": proposal.sent_at.isoformat(), "sent_message_id": proposal.sent_message_id},
            "approve": {"url": approve_url},
            "reject": {"url": reject_url},
        }
    )


@csrf_exempt
def whatsapp_webhook_view(request: HttpRequest) -> JsonResponse:
    """WhatsApp Cloud webhook.

    GET: verification (hub.challenge)
    POST: inbound messages -> routes to OPS agent (owner/staff) or Client agent.

    Notes:
      - CSRF exempt (called by Meta)
      - Actions-only: routing only; agents execute deterministic flows.
    """

    def _mask(v: object) -> str:
        s = str(v or "")
        if not s:
            return ""
        if len(s) <= 6:
            return s[:1] + "***" + s[-1:]
        return s[:3] + "***" + s[-3:]

    if request.method == "GET":
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")

        expected_settings = getattr(settings, "WHATSAPP_WEBHOOK_VERIFY_TOKEN", "") or ""
        expected_env = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "") or ""
        expected = expected_settings or expected_env

        print(
            f"WA VERIFY debug: mode={mode!r} token={_mask(token)} challenge={challenge!r} "
            f"expected_settings={_mask(expected_settings)} expected_env={_mask(expected_env)} expected_used={_mask(expected)}",
            flush=True,
        )

        if mode == "subscribe" and expected and token == expected and challenge is not None:
            return HttpResponse(challenge, content_type="text/plain", status=200)

        return _json_error(403, "forbidden", "Verification failed")

    if request.method != "POST":
        return _json_error(405, "method_not_allowed", "Use GET or POST")

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return _json_error(400, "bad_json", "Body must be valid JSON")

    # Debug: confirm which DB this process is connected to (helps when Admin and webhook seem out of sync).
    try:
        from django.db import connection
        db = getattr(connection, 'settings_dict', {}) or {}
        print(f"[WA DB] ENGINE={db.get('ENGINE')} NAME={db.get('NAME')} HOST={db.get('HOST')}", flush=True)
    except Exception as e:
        print(f"[WA DB] failed: {e}", flush=True)


    # Import locally to keep module import graph simple.
    from core.agents.client_agent import handle_whatsapp_webhook_payload as handle_client
    from core.agents.ops_agent import handle_whatsapp_webhook_payload as handle_ops

    def _extract_first_inbound_text_and_sender(p: dict) -> tuple[str, str, str]:
        """Return (text, sender_wa, phone_number_id). Best-effort; empty strings if missing."""
        try:
            entry0 = (p.get("entry") or [])[0] or {}
            change0 = (entry0.get("changes") or [])[0] or {}
            value = change0.get("value") or {}

            meta = value.get("metadata") or {}
            phone_number_id = str(meta.get("phone_number_id") or "")

            msgs = value.get("messages") or []
            if not msgs:
                return "", "", phone_number_id

            msg0 = msgs[0] or {}
            sender = str(msg0.get("from") or "")

            mtype = msg0.get("type")
            if mtype == "text":
                body = ((msg0.get("text") or {}).get("body") or "")
                return str(body), sender, phone_number_id

            if mtype == "interactive":
                inter = msg0.get("interactive") or {}
                itype = inter.get("type")
                if itype == "button_reply":
                    title = ((inter.get("button_reply") or {}).get("title") or "")
                    return str(title), sender, phone_number_id
                if itype == "list_reply":
                    title = ((inter.get("list_reply") or {}).get("title") or "")
                    return str(title), sender, phone_number_id

            return "", sender, phone_number_id
        except Exception:
            return "", "", ""

    def _should_route_to_ops(*, text_in: str) -> bool:
        t = (text_in or "").strip()
        if not t:
            return False
        keywords = ["בעלים", "בעל", "מנהל"]
        return any(k in t for k in keywords)

    def _digits(s: str) -> str:
        return "".join(ch for ch in (s or "") if ch.isdigit())

    def _is_ops_sender(*, business: Business, sender_wa: str) -> bool:
        """Hard RBAC: only whitelisted staff/owner numbers for this business.

        Diagnostics:
          - Prints how many memberships with a whatsapp_number were found for this business.
          - Prints a few sample rows (id, role, whatsapp_number) to ensure we are querying the same DB you see in Admin.

        IMPORTANT: Be tolerant to legacy role encodings ("Owner", "owner", enums, ints).
        """
        if not business or not sender_wa:
            return False

        sender_digits = _digits(sender_wa)
        if not sender_digits:
            return False

        # Fallback whitelist for Test-number mode / bootstrap (comma-separated digits or E164).
        # Example: OPS_WHATSAPP_WHITELIST=972502221246,9725xxxxxxx
        wl_raw = os.getenv('OPS_WHATSAPP_WHITELIST', '') or ''
        if wl_raw.strip():
            for item in wl_raw.split(','):
                cand = _digits(item.strip())
                if not cand:
                    continue
                if sender_digits == cand or sender_digits.endswith(cand) or cand.endswith(sender_digits):
                    print(f"[WA RBAC] env_whitelist_match sender={sender_digits}", flush=True)
                    return True


        try:
            qs_all = BusinessMembership.objects.filter(business_id=business.id)
            total_for_business = qs_all.count()
            with_numbers = qs_all.exclude(whatsapp_number__isnull=True).exclude(whatsapp_number__exact="")
            with_numbers_count = with_numbers.count()
            print(
                f"[WA RBAC] business_id={business.id} memberships_total={total_for_business} memberships_with_numbers={with_numbers_count}",
                flush=True,
            )
            # Print up to 5 rows to see what the webhook process actually reads from DB.
            for mid, role, wnum in with_numbers.values_list("id", "role", "whatsapp_number")[:5]:
                print(f"[WA RBAC] row id={mid} role={role!r} whatsapp_number={wnum!r}", flush=True)
        except Exception as e:
            print(f"[WA RBAC] debug_failed err={type(e).__name__}:{e}", flush=True)
            # continue RBAC evaluation best-effort

        enum_owner = getattr(BusinessMembership.Role, "OWNER", None)
        enum_staff = getattr(BusinessMembership.Role, "STAFF", None)

        def _role_ok(role_val: object) -> bool:
            if role_val is None:
                return False
            s = str(role_val).strip()
            if not s:
                return False
            s_low = s.lower()
            return s_low in {"owner", "staff"} or role_val in {enum_owner, enum_staff}

        # Don't filter in SQL by role to avoid enum/legacy mismatches; filter in Python.
        qs = BusinessMembership.objects.filter(business_id=business.id).exclude(whatsapp_number__isnull=True).exclude(whatsapp_number__exact="")

        for role_val, num in qs.values_list("role", "whatsapp_number"):
            if not _role_ok(role_val):
                continue
            num_digits = _digits(num)
            if not num_digits:
                continue
            if sender_digits == num_digits:
                return True
            # suffix match for tolerating country code / formatting differences
            if sender_digits.endswith(num_digits) or num_digits.endswith(sender_digits):
                return True

        return False

    inbound_text, sender_wa, phone_number_id = _extract_first_inbound_text_and_sender(payload)

    # Resolve business by ops phone_number_id when possible.
    business = None
    if phone_number_id:
        business = Business.objects.filter(ops_whatsapp_phone_number_id=phone_number_id).order_by("id").first()
    if business is None:
        business = Business.objects.order_by("id").first()

    # Router debug (never break webhook)
    try:
        kw = _should_route_to_ops(text_in=inbound_text)
        is_ops = _is_ops_sender(business=business, sender_wa=sender_wa) if business else False
        print(
            f"[WA ROUTER] text={inbound_text!r} sender={sender_wa} phone_number_id={phone_number_id} "
            f"business_id={getattr(business, 'id', None)} kw={kw} is_ops_sender={is_ops}",
            flush=True,
        )
    except Exception:
        kw = False
        is_ops = False

    try:
        if business and kw and is_ops:
            handle_ops(payload)
        else:
            handle_client(payload)
    except Exception as e:
        print(f"[WA WEBHOOK ERROR] {e}", flush=True)
        logger.exception("WA webhook processing failed")
        return JsonResponse({"ok": False, "error": "processing_failed"}, status=500)

    return JsonResponse({"ok": True})
