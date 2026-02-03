from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.timezone import localtime

from core.agents.phone import normalize_phone
from core.models import (
    Appointment,
    AuditEvent,
    Business,
    BusinessMembership,
    OpsConversationSession,
    Provider,
    Room,
    RoomBlock,
    WhatsAppMessage,
)
from core.notifications import get_provider

# ---------- small helpers (shared style with client_agent) ----------

_YN_CLEAN_RE = re.compile(r"[^\w\u0590-\u05FF]+", re.UNICODE)

def _norm_yn(text: str) -> str:
    t = (text or "").strip().lower()
    t = _YN_CLEAN_RE.sub("", t)
    return t

YES_WORDS = {"כן", "מאשר", "מאשרת", "ok", "okay", "y", "yes", "1"}
NO_WORDS = {"לא", "דוחה", "reject", "no", "0", "2"}

def _default_cc() -> Optional[str]:
    return (getattr(settings, "DEFAULT_COUNTRY_CODE", "") or "") or None

def _fmt_dt(business: Business, dt: datetime) -> str:
    tz = ZoneInfo(business.timezone or "Asia/Jerusalem")
    return localtime(dt, tz).strftime("%a %d/%m %H:%M")

def _extract_wa_events(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            for msg in value.get("messages") or []:
                yield {"message": msg, "metadata": metadata, "contacts": value.get("contacts") or []}

def _extract_text(message: Dict[str, Any]) -> str:
    if not isinstance(message, dict):
        return ""
    if message.get("type") == "text":
        return ((message.get("text") or {}).get("body") or "").strip()
    return ""

def _safe_create_audit_event(**kwargs: Any) -> None:
    try:
        field_names = {f.name for f in AuditEvent._meta.fields}
        create_kwargs = {k: v for k, v in kwargs.items() if k in field_names}
        if create_kwargs:
            AuditEvent.objects.create(**create_kwargs)
    except Exception:
        return

def _parse_iso_dt(value: str) -> datetime:
    """Parse an ISO datetime that may be aware or naive."""
    dt = datetime.fromisoformat(value)
    if timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt, timezone.get_current_timezone())


def _send_text(*, business: Business, membership: BusinessMembership, to_number: str, body: str) -> str:
    to_num = normalize_phone(to_number, default_country_code=_default_cc())
    from_num = normalize_phone(business.ops_whatsapp_display_number or "", default_country_code=_default_cc())

    out = WhatsAppMessage.objects.create(
        business=business,
        provider=None,
        client=None,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        purpose=WhatsAppMessage.Purpose.OPS_AGENT,
        wa_message_id="",
        from_number=from_num,
        to_number=to_num,
        body=body,
        raw_payload={"status": "sending"},
    )

    wa = None
    try:
        wa = get_provider()
        msg_id = wa.send(to=to_num, body=body, template_name="")
        out.wa_message_id = msg_id or ""
        out.raw_payload = {"status": "sent", "provider": wa.__class__.__name__}
        out.save(update_fields=["wa_message_id", "raw_payload"])
        return msg_id or ""
    except Exception as e:
        out.raw_payload = {
            "status": "failed",
            "provider": (wa.__class__.__name__ if wa else None),
            "error_type": type(e).__name__,
            "error": str(e),
        }
        out.save(update_fields=["raw_payload"])
        return ""

# ---------- ops agent logic ----------

@dataclass(frozen=True)
class _ParsedCommand:
    intent: str
    room_id: Optional[int] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    appointment_id: Optional[int] = None
    proposed_room_id: Optional[int] = None
    reason: str = ""
    range_kind: str = "today"  # for schedule queries

_RANGE_RE = re.compile(r"\b(היום|מחר|שבוע)\b")
_ROOM_RE = re.compile(r"חדר\s*(\d+)")
_APPT_RE = re.compile(r"(?:תור|appt|appointment)\s*#?\s*(\d+)")
_TO_ROOM_RE = re.compile(r"לחדר\s*(\d+)")
_TIME_RANGE_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?\b")

def _parse_command(*, business: Business, text: str) -> _ParsedCommand:
    t = (text or "").strip()
    tl = t.lower()

    # schedule
    if any(w in tl for w in ["תציג", "הצג", "מה יש", "תמונה", "show"]) and _RANGE_RE.search(t):
        rk = _RANGE_RE.search(t).group(1)
        return _ParsedCommand(intent="show_schedule", range_kind={"היום": "today", "מחר": "tomorrow", "שבוע": "week"}.get(rk, "today"))

    # close room
    if any(w in tl for w in ["סגור", "חסום", "סגירה", "close", "block"]) and "חדר" in tl:
        room_m = _ROOM_RE.search(t)
        room_id = int(room_m.group(1)) if room_m else None
        start, end = _parse_date_time_range(business=business, text=t)
        reason = _extract_reason(t)
        return _ParsedCommand(intent="close_room", room_id=room_id, start=start, end=end, reason=reason)

    # move appointment (create proposal)
    if any(w in tl for w in ["העבר", "שנה", "להעביר", "move", "reschedule"]) and ("תור" in tl or "appointment" in tl):
        appt_m = _APPT_RE.search(t)
        appt_id = int(appt_m.group(1)) if appt_m else None
        to_room_m = _TO_ROOM_RE.search(t)
        to_room_id = int(to_room_m.group(1)) if to_room_m else None
        reason = _extract_reason(t)
        return _ParsedCommand(intent="move_appointment", appointment_id=appt_id, proposed_room_id=to_room_id, reason=reason)

    # help
    return _ParsedCommand(intent="help")

def _extract_reason(text: str) -> str:
    # heuristic: everything after "כי" or "סיבה:"
    if "סיבה" in text:
        parts = text.split("סיבה", 1)[-1]
        parts = parts.split(":", 1)[-1]
        return parts.strip()
    if "כי" in text:
        return text.split("כי", 1)[-1].strip()
    return ""

def _parse_date_time_range(*, business: Business, text: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    tz = ZoneInfo(business.timezone or "Asia/Jerusalem")
    now = timezone.now()
    base_day = None
    if "מחר" in text:
        base_day = localtime(now, tz).date() + timedelta(days=1)
    elif "היום" in text:
        base_day = localtime(now, tz).date()
    else:
        # optional: if not specified, default today
        base_day = localtime(now, tz).date()

    m = _TIME_RANGE_RE.search(text)
    if not m:
        return None, None
    sh = int(m.group(1))
    sm = int(m.group(2) or 0)
    eh = int(m.group(3))
    em = int(m.group(4) or 0)

    start_local = datetime(base_day.year, base_day.month, base_day.day, sh, sm, tzinfo=tz)
    end_local = datetime(base_day.year, base_day.month, base_day.day, eh, em, tzinfo=tz)
    if end_local <= start_local:
        end_local = end_local + timedelta(days=1)

    return timezone.make_aware(start_local.replace(tzinfo=None), tz), timezone.make_aware(end_local.replace(tzinfo=None), tz)

def _get_or_create_session(*, business: Business, membership: BusinessMembership, wa_from_number: str) -> OpsConversationSession:
    now = timezone.now()
    session, _ = OpsConversationSession.objects.get_or_create(
        business=business,
        wa_from_number=wa_from_number,
        defaults={"membership": membership, "expires_at": now + timedelta(minutes=30), "state": {}},
    )

    # If membership changed for same sender, update to latest for safety.
    if session.membership_id != membership.id:
        session.membership = membership
        session.state = {}
        session.expires_at = now + timedelta(minutes=30)
        session.save(update_fields=["membership", "state", "expires_at"])
        return session

    if session.expires_at and session.expires_at < now:
        session.state = {}
        session.expires_at = now + timedelta(minutes=30)
        session.save(update_fields=["state", "expires_at"])
    return session

def _require_pin_if_configured(*, session: OpsConversationSession, text: str) -> Tuple[bool, Optional[str]]:
    pin = (getattr(settings, "OPS_AGENT_PIN", "") or "").strip()
    if not pin:
        return True, None

    now = timezone.now()
    verified_until_raw = (session.state or {}).get("verified_until")
    if verified_until_raw:
        try:
            verified_until = _parse_iso_dt(str(verified_until_raw))
            if verified_until > now:
                return True, None
        except Exception:
            pass

    # Allow "PIN 1234" or just the pin itself
    tl = (text or "").strip()
    candidate = tl.replace(" ", "")
    if candidate == pin or candidate.lower().startswith("pin") and candidate.lower().endswith(pin):
        session.state["verified_until"] = (now + timedelta(hours=12)).isoformat()
        session.save(update_fields=["state"])
        return True, "אימות הצליח. איך אפשר לעזור?"

    return False, "כדי להמשיך נדרש קוד אימות. שלח: PIN <קוד>"

def _schedule_range(*, business: Business, kind: str) -> Tuple[datetime, datetime]:
    tz = ZoneInfo(business.timezone or "Asia/Jerusalem")
    now = localtime(timezone.now(), tz)
    if kind == "tomorrow":
        day = now.date() + timedelta(days=1)
        start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz)
        end = start + timedelta(days=1)
    elif kind == "week":
        start = datetime(now.year, now.month, now.day, 0, 0, tzinfo=tz)
        end = start + timedelta(days=7)
    else:
        start = datetime(now.year, now.month, now.day, 0, 0, tzinfo=tz)
        end = start + timedelta(days=1)

    return timezone.make_aware(start.replace(tzinfo=None), tz), timezone.make_aware(end.replace(tzinfo=None), tz)

def _mask_schedule_lines(*, business: Business, qs) -> str:
    lines = []
    for a in qs:
        has_client = bool(a.client_id)
        status = a.status
        label = "עם מטופל" if has_client else "ללא מטופל"
        lines.append(
            f"#{a.id} | {_fmt_dt(business, a.start_time)}-{localtime(a.end_time, ZoneInfo(business.timezone or 'Asia/Jerusalem')).strftime('%H:%M')} | חדר: {(a.room.name if a.room_id else '-')} | רופא: {(a.provider.display_name if a.provider_id else '-')} | {label} | {status}"
        )
    return "\n".join(lines)

def _help_text() -> str:
    return (
        "פקודות לדוגמה:\n"
        "• תציג היום\n"
        "• תציג מחר\n"
        "• תציג שבוע\n"
        "• סגור חדר 2 היום 10-12 סיבה: מזגן\n"
        "• העבר תור 123 לחדר 3 סיבה: תקלה\n"
        "\nברירת מחדל: לא מציגים פרטי מטופלים."
    )

def handle_whatsapp_webhook_payload(payload: Dict[str, Any]) -> None:
    """Ops Agent entry point (Owner/Staff clinic WhatsApp)."""
    cc = _default_cc()

    for ev in _extract_wa_events(payload):
        msg = ev["message"]
        metadata = ev["metadata"]
        wa_message_id = (msg.get("id") or "").strip()
        from_raw = (msg.get("from") or "").strip()
        from_number = normalize_phone(from_raw, default_country_code=cc)

        to_display_number = (metadata.get("display_phone_number") or "").strip()
        to_phone_number_id = (metadata.get("phone_number_id") or "").strip()

        # best-effort idempotency
        if wa_message_id and WhatsAppMessage.objects.filter(wa_message_id=wa_message_id, direction=WhatsAppMessage.Direction.INBOUND).exists():
            continue

        business = Business.objects.filter(ops_whatsapp_phone_number_id=to_phone_number_id).order_by("id").first()
        if business is None and to_display_number:
            business = Business.objects.filter(ops_whatsapp_display_number=to_display_number).order_by("id").first()
        if business is None:
            continue  # not an ops number

        text = _extract_text(msg)

        membership = (
            BusinessMembership.objects.filter(
                business=business,
                role__in=[BusinessMembership.Role.OWNER, BusinessMembership.Role.STAFF],
            )
            .filter(whatsapp_number__isnull=False)
            .order_by("id")
            .first()
        )
        # strict whitelist: number must match a membership.whatsapp_number
        membership = BusinessMembership.objects.filter(
            business=business,
            role__in=[BusinessMembership.Role.OWNER, BusinessMembership.Role.STAFF],
            whatsapp_number=from_number,
        ).select_related("user").first()

        WhatsAppMessage.objects.create(
            business=business,
            provider=None,
            client=None,
            direction=WhatsAppMessage.Direction.INBOUND,
            purpose=WhatsAppMessage.Purpose.OPS_AGENT,
            wa_message_id=wa_message_id,
            from_number=from_number,
            to_number=normalize_phone(to_display_number, default_country_code=cc),
            body=text,
            raw_payload={"message": msg, "metadata": metadata},
        )

        if membership is None:
            # do not leak anything
            _send_text(business=business, membership=BusinessMembership(business=business, user=business.owner, role=BusinessMembership.Role.OWNER), to_number=from_number, body="המספר לא מורשה לפעולות תפעול.")
            continue

        session = _get_or_create_session(business=business, membership=membership, wa_from_number=from_number)

        ok, pin_msg = _require_pin_if_configured(session=session, text=text)
        if not ok:
            _send_text(business=business, membership=membership, to_number=from_number, body=pin_msg or "נדרש אימות")
            continue
        if pin_msg:
            _send_text(business=business, membership=membership, to_number=from_number, body=pin_msg)
            continue

        # handle yes/no confirmation
        pending = (session.state or {}).get("pending")
        yn = _norm_yn(text)
        if pending and yn in YES_WORDS.union(NO_WORDS):
            if yn in NO_WORDS:
                session.state.pop("pending", None)
                session.expires_at = timezone.now() + timedelta(minutes=30)
                session.save(update_fields=["state", "expires_at"])
                _send_text(business=business, membership=membership, to_number=from_number, body="בוטל. מה עוד?")
                continue

            # YES -> execute
            try:
                _execute_pending(session=session)
                _send_text(business=business, membership=membership, to_number=from_number, body="בוצע. מה עוד?")
            except ValueError as e:
                code = str(e)
                _send_text(business=business, membership=membership, to_number=from_number, body=f"לא בוצע: {code}")
            except Exception:
                _send_text(business=business, membership=membership, to_number=from_number, body="משהו השתבש בביצוע הפעולה.")
            continue

        cmd = _parse_command(business=business, text=text)

        if cmd.intent == "help":
            _send_text(business=business, membership=membership, to_number=from_number, body=_help_text())
            continue

        if cmd.intent == "show_schedule":
            start, end = _schedule_range(business=business, kind=cmd.range_kind)
            qs = (
                Appointment.objects.filter(business=business, start_time__gte=start, start_time__lt=end)
                .select_related("room", "provider")
                .order_by("start_time")[:50]
            )
            body = _mask_schedule_lines(business=business, qs=qs)
            if not body:
                body = "אין שיבוצים בטווח שביקשת."
            _send_text(business=business, membership=membership, to_number=from_number, body=body)
            continue

        if cmd.intent == "close_room":
            if not cmd.room_id or not cmd.start or not cmd.end:
                _send_text(business=business, membership=membership, to_number=from_number, body="חסר מידע. דוגמה: סגור חדר 2 היום 10-12 סיבה: מזגן")
                continue
            room = Room.objects.filter(business=business, id=cmd.room_id).first()
            if not room:
                _send_text(business=business, membership=membership, to_number=from_number, body="חדר לא נמצא.")
                continue

            impacted = Appointment.objects.filter(
                business=business,
                room=room,
                start_time__lt=cmd.end,
                end_time__gt=cmd.start,
            ).exclude(status__in=[Appointment.Status.CANCELLED_CLIENT, Appointment.Status.CANCELLED_STAFF]).count()

            summary = (
                f"סגירת חדר {room.name} בין {_fmt_dt(business, cmd.start)} ל-{_fmt_dt(business, cmd.end)}\n"
                f"סיבה: {cmd.reason or '-'}\n"
                f"תורים/שריונים מושפעים (ללא פרטי מטופלים): {impacted}\n\n"
                "לאשר? (כן/לא)"
            )
            session.state["pending"] = {
                "action": "close_room",
                "room_id": room.id,
                "start": cmd.start.isoformat(),
                "end": cmd.end.isoformat(),
                "reason": cmd.reason or "",
            }
            session.expires_at = timezone.now() + timedelta(minutes=10)
            session.save(update_fields=["state", "expires_at"])
            _send_text(business=business, membership=membership, to_number=from_number, body=summary)
            continue

        if cmd.intent == "move_appointment":
            if not cmd.appointment_id:
                _send_text(business=business, membership=membership, to_number=from_number, body="צריך מזהה תור. דוגמה: העבר תור 123 לחדר 3 סיבה: תקלה")
                continue
            appt = Appointment.objects.filter(business=business, id=cmd.appointment_id).select_related("provider", "room").first()
            if not appt:
                _send_text(business=business, membership=membership, to_number=from_number, body="תור לא נמצא.")
                continue
            if not appt.provider_id:
                _send_text(business=business, membership=membership, to_number=from_number, body="לתור אין רופא משויך.")
                continue

            proposed_room = appt.room
            if cmd.proposed_room_id:
                proposed_room = Room.objects.filter(business=business, id=cmd.proposed_room_id).first()
                if not proposed_room:
                    _send_text(business=business, membership=membership, to_number=from_number, body="חדר יעד לא נמצא.")
                    continue

            summary = (
                "יצירת בקשת אישור לרופא לשינוי תור:\n"
                f"תור #{appt.id}\n"
                f"מ: {_fmt_dt(business, appt.start_time)} עד {_fmt_dt(business, appt.end_time)} (חדר: {(appt.room.name if appt.room_id else '-')})\n"
                f"ל: {_fmt_dt(business, appt.start_time)} עד {_fmt_dt(business, appt.end_time)} (חדר: {(proposed_room.name if proposed_room else '-')})\n"
                f"סיבה: {cmd.reason or '-'}\n\n"
                "לאשר? (כן/לא)"
            )

            session.state["pending"] = {
                "action": "create_change_proposal",
                "appointment_id": appt.id,
                "proposed_room_id": (proposed_room.id if proposed_room else None),
                "reason": cmd.reason or "",
            }
            session.expires_at = timezone.now() + timedelta(minutes=10)
            session.save(update_fields=["state", "expires_at"])
            _send_text(business=business, membership=membership, to_number=from_number, body=summary)
            continue

        _send_text(business=business, membership=membership, to_number=from_number, body=_help_text())


def _execute_pending(*, session: OpsConversationSession) -> None:
    pending = (session.state or {}).get("pending") or {}
    action = pending.get("action")
    business = session.business
    actor_user = session.membership.user

    if action == "close_room":
        room_id = int(pending.get("room_id"))
        start = _parse_iso_dt(str(pending.get("start")))
        end = _parse_iso_dt(str(pending.get("end")))
        reason = (pending.get("reason") or "").strip()

        room = Room.objects.filter(business=business, id=room_id).first()
        if not room:
            raise ValueError("room_not_found")

        with transaction.atomic():
            block = RoomBlock.objects.create(
                business=business,
                room=room,
                start_time=start,
                end_time=end,
                reason=reason,
                created_by=actor_user,
                is_active=True,
            )
            _safe_create_audit_event(
                business=business,
                actor_user=actor_user,
                action="room_block_created",
                object_type="RoomBlock",
                object_id=str(block.id),
                before={},
                after={
                    "room_id": room.id,
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "reason": reason,
                },
            )

        session.state.pop("pending", None)
        session.expires_at = timezone.now() + timedelta(minutes=30)
        session.save(update_fields=["state", "expires_at"])
        return

    if action == "create_change_proposal":
        appt_id = int(pending.get("appointment_id"))
        proposed_room_id = pending.get("proposed_room_id")
        reason = (pending.get("reason") or "").strip()

        appt = Appointment.objects.select_related("provider", "room").filter(business=business, id=appt_id).first()
        if not appt:
            raise ValueError("appointment_not_found")

        from core.models import AppointmentChangeProposal  # local import
        from core.views import make_change_proposal_action_token, build_public_proposal_action_url
        from core.api import _validate_appointment_move, _appointment_is_changeable  # best-effort reuse

        if not _appointment_is_changeable(appt):
            raise ValueError("appointment_not_changeable")

        proposed_room = appt.room
        if proposed_room_id is not None:
            proposed_room = Room.objects.filter(business=business, id=int(proposed_room_id)).first()
            if not proposed_room:
                raise ValueError("room_not_found")

        ok, msg, _alts = _validate_appointment_move(
            business=business,
            appointment=appt,
            provider=appt.provider,
            new_room=proposed_room,
            new_start=appt.start_time,
            new_end=appt.end_time,
        )
        if not ok:
            raise ValueError("invalid_proposal:" + (msg or ""))

        proposal = AppointmentChangeProposal.objects.create(
            business=business,
            appointment=appt,
            original_room=appt.room,
            original_start_time=appt.start_time,
            original_end_time=appt.end_time,
            proposed_room=proposed_room,
            proposed_start_time=appt.start_time,
            proposed_end_time=appt.end_time,
            reason=reason,
            expires_at=timezone.now() + timedelta(minutes=int(getattr(settings, "CHANGE_PROPOSAL_DEFAULT_EXPIRES_MINUTES", 60))),
            created_by=actor_user,
        )

        approve_token = make_change_proposal_action_token(proposal_id=proposal.id, action="approve")
        reject_token = make_change_proposal_action_token(proposal_id=proposal.id, action="reject")
        approve_url = build_public_proposal_action_url(token=approve_token, action="approve")
        reject_url = build_public_proposal_action_url(token=reject_token, action="reject")

        provider_number = (appt.provider.whatsapp_number or "").strip()
        if provider_number:
            body = (
                "בקשת שינוי תור עקב אילוץ מרפאה.\n"
                f"סיבה: {reason or '-'}\n"
                f"מ: {appt.start_time:%Y-%m-%d %H:%M} עד {appt.end_time:%H:%M} (חדר: {(appt.room.name if appt.room_id else '-')})\n"
                f"ל: {appt.start_time:%Y-%m-%d %H:%M} עד {appt.end_time:%H:%M} (חדר: {(proposed_room.name if proposed_room else '-')})\n\n"
                f"לאישור: {approve_url}\n"
                f"לדחייה: {reject_url}\n"
            )
            try:
                msg_id = get_provider().send(to=provider_number, body=body, template_name="")
                proposal.sent_at = timezone.now()
                proposal.sent_message_id = msg_id or ""
                proposal.save(update_fields=["sent_at", "sent_message_id"])
            except Exception as e:
                proposal.send_error = str(e)
                proposal.save(update_fields=["send_error"])

        _safe_create_audit_event(
            business=business,
            actor_user=actor_user,
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

        session.state.pop("pending", None)
        session.expires_at = timezone.now() + timedelta(minutes=30)
        session.save(update_fields=["state", "expires_at"])
        return

    raise ValueError("unknown_action")
