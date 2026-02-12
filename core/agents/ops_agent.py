from __future__ import annotations

import os
import re
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from core.models import Business, BusinessMembership, Room, RoomBlock
from core.notifications import get_provider

logger = logging.getLogger(__name__)


# =========================
# Public API (called by webhook router)
# =========================

@dataclass(frozen=True)
class OutgoingMessage:
    to: str
    body: str
    template_name: str = ""


def handle_whatsapp_webhook_payload(payload: dict[str, Any]) -> OutgoingMessage:
    """
    Ops Agent entrypoint.

    - Adds mechanical logs with correlation id (cid)
    - Supports 'בעלים: כן' by stripping prefix before yes/no detection
    """
    cid = _get_corr_id(payload)

    raw_text, sender_wa, phone_number_id = _extract_first_inbound_text_and_sender(payload)
    text_in, requested_mode = _strip_mode_prefix(raw_text)

    business = _resolve_business(phone_number_id=phone_number_id)
    if business is None:
        logger.info("[OPS_AGENT] cid=%s section=Router event=NoBusiness sender=%s raw=%r text=%r", cid, sender_wa, raw_text, text_in)
        return _send(sender_wa, "לא נמצא עסק במערכת.")

    is_ops = _is_ops_sender(business=business, sender_wa=sender_wa)
    yn_peek = _parse_yes_no(text_in)
    logger.info(
        "[OPS_AGENT] cid=%s section=InboundParse sender=%s raw=%r text=%r requested_mode=%r yesno_detected=%s yesno_value=%s business_id=%s",
        cid,
        sender_wa,
        raw_text,
        text_in,
        requested_mode,
        yn_peek is not None,
        yn_peek,
        business.id,
    )

    if not is_ops:
        return _send(sender_wa, "המספר לא מורשה לפעולות תפעול.")

    # 1) Pending confirmation?
    pending = _pending_get(business_id=business.id, sender_wa=sender_wa)
    if pending:
        logger.info("[OPS_AGENT] cid=%s section=Pending event=Found sender=%s action=%s", cid, sender_wa, pending.get("action"))
        decision = _parse_yes_no(text_in)
        if decision is None:
            logger.info("[OPS_AGENT] cid=%s section=PendingDecision event=NeedYesNo sender=%s", cid, sender_wa)
            return _send(sender_wa, "לא הבנתי. לאשר? (כן/לא)")
        if decision is False:
            _pending_clear(business_id=business.id, sender_wa=sender_wa)
            logger.info("[OPS_AGENT] cid=%s section=PendingDecision event=Rejected sender=%s", cid, sender_wa)
            return _send(sender_wa, "בוטל.")
        try:
            logger.info("[OPS_AGENT] cid=%s section=Execute event=Begin sender=%s action=%s", cid, sender_wa, pending.get("action"))
            out = _execute_pending(business=business, sender_wa=sender_wa, pending=pending)
            logger.info("[OPS_AGENT] cid=%s section=Execute event=Ok sender=%s action=%s", cid, sender_wa, pending.get("action"))
            return out
        except Exception as e:
            logger.exception("ops_agent execute_pending failed cid=%s", cid)
            logger.info("[OPS_AGENT] cid=%s section=Execute event=Error sender=%s error=%s", cid, sender_wa, type(e).__name__)
            return _send(sender_wa, "משהו השתבש בביצוע הפעולה.")
        finally:
            _pending_clear(business_id=business.id, sender_wa=sender_wa)

    # 2) New command
    cmd = _parse_close_room_command(text_in, business_tz=business.timezone or "Asia/Jerusalem")
    if cmd:
        room, start_dt, end_dt, reason = cmd
        logger.info(
            "[OPS_AGENT] cid=%s section=Command event=Parsed sender=%s intent=close_room room_id=%s start=%s end=%s has_reason=%s",
            cid,
            sender_wa,
            room.id,
            start_dt.isoformat(timespec="minutes"),
            end_dt.isoformat(timespec="minutes"),
            bool(reason),
        )
        _pending_set(
            business_id=business.id,
            sender_wa=sender_wa,
            pending={
                "action": "close_room",
                "room_id": room.id,
                "start_time": start_dt.isoformat(),
                "end_time": end_dt.isoformat(),
                "reason": reason,
            },
            ttl_seconds=10 * 60,
        )
        logger.info("[OPS_AGENT] cid=%s section=Pending event=Set sender=%s action=close_room ttl_seconds=%s", cid, sender_wa, 600)
        return _send(
            sender_wa,
            f"חדר {room.name} יסגר {start_dt:%Y-%m-%d %H:%M}-{end_dt:%H:%M}."
            + (f"
סיבה: {reason}" if reason else "")
            + "
לאשר? (כן/לא)"
        )

    logger.info("[OPS_AGENT] cid=%s section=Fallback event=Menu sender=%s text=%r", cid, sender_wa, text_in)
    return _send(
        sender_wa,
        "פקודות לדוגמה:
"
        "• סגור חדר 2 מחר 10:00-12:00 סיבה: תחזוקה
"
        "• סגור חדר 1 היום 14:00-15:00
"
        "
לאישור פעולה ממתינה: ענה כן/לא (אפשר גם 'בעלים: כן')."
    )

# =========================
# Command parsing
# =========================

_CLOSE_ROOM_RE = re.compile(
    r"^\s*סגור\s+חדר\s*(?P<room>\d+)\s+"
    r"(?P<day>היום|מחר|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\s+"
    r"(?P<start>\d{1,2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2})"
    r"(?:\s+סיבה[:：]?\s*(?P<reason>.+))?\s*$"
)


def _parse_close_room_command(text_in: str, *, business_tz: str) -> Optional[tuple[Room, datetime, datetime, str]]:
    """
    Parses:
      סגור חדר 2 מחר 10:00-12:00 סיבה: תחזוקה
    Returns: (Room, start_dt, end_dt, reason)
    """
    t = (text_in or "").strip()
    if not t:
        return None

    m = _CLOSE_ROOM_RE.match(t)
    if not m:
        return None

    tz = ZoneInfo(business_tz or "Asia/Jerusalem")

    room_token = m.group("room")
    room = _resolve_room(room_token)
    if room is None:
        return None

    day_token = m.group("day")
    d = _parse_day_token(day_token, tz=tz)
    if d is None:
        return None

    start_t = _parse_hhmm(m.group("start"))
    end_t = _parse_hhmm(m.group("end"))
    if start_t is None or end_t is None:
        return None

    start_dt = timezone.make_aware(datetime.combine(d, start_t), tz)
    end_dt = timezone.make_aware(datetime.combine(d, end_t), tz)
    if end_dt <= start_dt:
        # allow crossing midnight? not in MVP.
        return None

    reason = (m.group("reason") or "").strip()
    return room, start_dt, end_dt, reason


def _resolve_room(room_token: str) -> Optional[Room]:
    # Prefer "name" match (users say "חדר 2" but Room.name is "2")
    room = Room.objects.filter(name=str(int(room_token))).order_by("id").first()
    if room:
        return room
    # Fallback by id
    try:
        rid = int(room_token)
    except Exception:
        return None
    return Room.objects.filter(pk=rid).order_by("id").first()


def _parse_day_token(token: str, *, tz: ZoneInfo) -> Optional[date]:
    token = (token or "").strip()
    today = timezone.now().astimezone(tz).date()

    if token == "היום":
        return today
    if token == "מחר":
        return today + timedelta(days=1)

    # YYYY-MM-DD
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
        try:
            y, m, d = map(int, token.split("-"))
            return date(y, m, d)
        except Exception:
            return None

    # DD/MM/YYYY or DD/MM/YY
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", token):
        try:
            dd, mm, yy = token.split("/")
            y = int(yy)
            if y < 100:
                y += 2000
            return date(y, int(mm), int(dd))
        except Exception:
            return None

    return None


def _parse_hhmm(s: str) -> Optional[dtime]:
    try:
        hh, mm = s.split(":", 1)
        h = int(hh)
        m = int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return dtime(h, m)
    except Exception:
        return None
    return None


# =========================
# Pending storage (cache)
# =========================

def _pending_key(*, business_id: int, sender_wa: str) -> str:
    sender_digits = "".join(ch for ch in (sender_wa or "") if ch.isdigit())
    return f"ops_pending:{business_id}:{sender_digits}"


def _pending_get(*, business_id: int, sender_wa: str) -> Optional[dict[str, Any]]:
    return cache.get(_pending_key(business_id=business_id, sender_wa=sender_wa))


def _pending_set(*, business_id: int, sender_wa: str, pending: dict[str, Any], ttl_seconds: int) -> None:
    cache.set(_pending_key(business_id=business_id, sender_wa=sender_wa), pending, ttl_seconds)


def _pending_clear(*, business_id: int, sender_wa: str) -> None:
    cache.delete(_pending_key(business_id=business_id, sender_wa=sender_wa))



# =========================
# Mode/prefix stripping
# =========================

_MODE_PREFIX_RE = re.compile(r"^\s*(בעלים|מטופל|לקוח|בעל|מנהל)\s*[:：\-]\s*", re.UNICODE)

def _strip_mode_prefix(text: str) -> tuple[str, str | None]:
    t = (text or "").strip()
    m = _MODE_PREFIX_RE.match(t)
    if not m:
        return t, None
    mode = m.group(1)
    return t[m.end():].strip(), mode

# =========================
# Yes/No parsing
# =========================

_YES_TOKENS = ("כן", "כן!", "yes", "y", "ok", "אשר", "מאשר", "מאשרת")
_NO_TOKENS = ("לא", "לא!", "no", "n", "בטל", "לבטל", "דחה", "דוחה")


def _parse_yes_no(text_in: str) -> Optional[bool]:
    """
    Accepts:
      'כן'
      'בעלים: כן'
      'מנהל כן!'
    """
    t = (text_in or "").strip().lower()
    if not t:
        return None

    # Drop leading "role:" prefixes (בעלים:, בעל:, מנהל:)
    t = re.sub(r"^\s*(בעלים|בעל|מנהל)\s*[:：]\s*", "", t).strip()

    # Normalize punctuation to spaces
    t_norm = re.sub(r"[^\w\u0590-\u05FF]+", " ", t).strip()

    has_yes = any(tok in t_norm.split() or tok in t_norm for tok in _YES_TOKENS)
    has_no = any(tok in t_norm.split() or tok in t_norm for tok in _NO_TOKENS)

    if has_yes and not has_no:
        return True
    if has_no and not has_yes:
        return False
    return None


# =========================
# Execute actions
# =========================

def _execute_pending(*, business: Business, sender_wa: str, pending: dict[str, Any]) -> OutgoingMessage:
    action = pending.get("action")
    if action == "close_room":
        room_id = int(pending.get("room_id"))
        reason = str(pending.get("reason") or "").strip()
        start_time = _parse_iso_dt(str(pending.get("start_time") or ""), business_tz=business.timezone)
        end_time = _parse_iso_dt(str(pending.get("end_time") or ""), business_tz=business.timezone)
        if not start_time or not end_time:
            return _send(sender_wa, "שגיאה: תזמון לא תקין.")
        room = Room.objects.filter(pk=room_id, business=business).first()
        if not room:
            return _send(sender_wa, "שגיאה: חדר לא נמצא.")
        RoomBlock.objects.create(
            business=business,
            room=room,
            start_time=start_time,
            end_time=end_time,
            reason=reason,
            created_by=business.owner,
            is_active=True,
        )
        return _send(sender_wa, f"אושר. חדר {room.name} נסגר {start_time:%Y-%m-%d %H:%M}-{end_time:%H:%M}.")
    return _send(sender_wa, "שגיאה: פעולה לא נתמכת.")


def _parse_iso_dt(value: str, *, business_tz: str | None) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return None
    if timezone.is_aware(dt):
        return dt
    tz = ZoneInfo(business_tz or "Asia/Jerusalem")
    return timezone.make_aware(dt, tz)


# =========================
# Business resolution & auth
# =========================

def _resolve_business(*, phone_number_id: str) -> Optional[Business]:
    # Prefer ops_whatsapp_phone_number_id if present, fallback to first business.
    if phone_number_id:
        try:
            b = Business.objects.filter(ops_whatsapp_phone_number_id=phone_number_id).order_by("id").first()
            if b:
                return b
        except Exception:
            pass
    return Business.objects.order_by("id").first()


def _is_ops_sender(*, business: Business, sender_wa: str) -> bool:
    if not sender_wa:
        return False
    sender_digits = "".join(ch for ch in sender_wa if ch.isdigit())
    if not sender_digits:
        return False

    # Option B: env whitelist (comma-separated E164 without '+', or with '+')
    raw = os.getenv("OPS_SENDER_WHITELIST", "") or os.getenv("WHATSAPP_OPS_SENDER_WHITELIST", "") or ""
    if raw.strip():
        allowed = []
        for part in raw.split(","):
            d = "".join(ch for ch in part.strip() if ch.isdigit())
            if d:
                allowed.append(d)
        if any(sender_digits.endswith(a[-9:]) for a in allowed):
            return True

    # Membership based
    qs = BusinessMembership.objects.filter(
        business=business,
        role__in=[BusinessMembership.Role.OWNER, BusinessMembership.Role.STAFF],
    ).exclude(whatsapp_number__isnull=True).exclude(whatsapp_number__exact="")
    for num in qs.values_list("whatsapp_number", flat=True):
        num_digits = "".join(ch for ch in str(num) if ch.isdigit())
        if not num_digits:
            continue
        if sender_digits == num_digits or sender_digits.endswith(num_digits[-9:]):
            return True

    return False


def _extract_first_inbound_text_and_sender(p: dict) -> tuple[str, str, str]:
    """
    Return (text, sender_wa, phone_number_id). Best-effort; empty strings if missing.
    """
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


def _send(to: str, body: str) -> OutgoingMessage:
    # Best-effort actual send (mocked in tests)
    try:
        get_provider().send(to=to, body=body, template_name="")
    except Exception:
        pass
    return OutgoingMessage(to=to, body=body, template_name="")
