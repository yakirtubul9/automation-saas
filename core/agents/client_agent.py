from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.utils import timezone
from django.utils.timezone import localtime
from zoneinfo import ZoneInfo

from core.agents.appointment_ops import (
    assign_client_to_slot_system,
    cancel_appointment_by_client_system,
    list_free_slots,
    reschedule_appointment_system,
)
from core.agents.phone import normalize_phone, phones_equivalent
from core.models import (
    Appointment,
    Business,
    Client,
    ConversationSession,
    Provider,
    Service,
    WhatsAppMessage,
)
from core.notifications import get_provider


YES_WORDS = {"כן", "מאשר", "מאשרת", "ok", "okay", "y", "yes", "1"}
NO_WORDS = {"לא", "דוחה", "reject", "no", "0", "2"}


def _is_sensitive_medical(text: str) -> bool:
    t = (text or "").lower()
    keywords = [
        "כאב",
        "דימום",
        "חום",
        "קוצר",
        "אלרג",
        "ליחה",
        "פריחה",
        "הקאה",
        "שלשול",
    ]
    return any(k in t for k in keywords)


def _detect_intent(text: str) -> str:
    t = (text or "").strip().lower()
    if not t:
        return "unknown"
    if any(w in t for w in ["לבטל", "ביטול", "cancel"]):
        return "cancel"
    if any(w in t for w in ["להזיז", "לדחות", "לשנות", "להעביר", "reschedule", "change"]):
        return "reschedule"
    if any(w in t for w in ["לקבוע", "קביע", "תור", "להזמין", "book", "schedule"]):
        return "book"
    if any(w in t for w in ["איפה", "מתי", "כתובת", "שעות", "info", "help", "עזרה"]):
        return "info"
    return "unknown"


def _fmt_dt(business: Business, dt) -> str:
    tz = ZoneInfo(business.timezone or "Asia/Jerusalem")
    dtt = localtime(dt, tz)
    return dtt.strftime("%a %d/%m %H:%M")


def _default_cc() -> Optional[str]:
    # keep consistent with WhatsApp send normalization
    return (getattr(settings, "DEFAULT_COUNTRY_CODE", "") or "") or None


def _send_text(*, business: Business, provider: Optional[Provider], client: Optional[Client], to_number: str, body: str) -> str:
    """Send WhatsApp text and persist an outbound log.

    Why we log first:
      - When sending fails (token/permissions/template/etc.), we still want an OUTBOUND row
        so it's visible in /admin and we can diagnose quickly.
      - The webhook handler should not crash because an outbound send failed.
    """
    from_num = normalize_phone(getattr(provider, "whatsapp_number", ""), default_country_code=_default_cc())
    to_num = normalize_phone(to_number, default_country_code=_default_cc())

    out = WhatsAppMessage.objects.create(
        business=business,
        provider=provider,
        client=client,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        purpose=WhatsAppMessage.Purpose.CLIENT_AGENT,
        wa_message_id="",
        from_number=from_num,
        to_number=to_num,
        body=body,
        raw_payload={"status": "sending"},
    )

    try:
        wa = get_provider()
        # template_name=="" forces TEXT mode even if env template exists.
        msg_id = wa.send(to=to_num, body=body, template_name="")
        out.wa_message_id = msg_id or ""
        out.raw_payload = {"status": "sent", "provider": wa.__class__.__name__}
        out.save(update_fields=["wa_message_id", "raw_payload"])
        return msg_id
    except Exception as e:
        out.raw_payload = {"status": "failed", "provider": wa.__class__.__name__, "error": str(e)}
        out.save(update_fields=["raw_payload"])
        print(f"[WA SEND ERROR] to={to_num} err={e}", flush=True)
        return ""



def _get_or_create_session(*, business: Business, provider: Provider, wa_from_number: str) -> ConversationSession:
    now = timezone.now()
    session, _ = ConversationSession.objects.get_or_create(
        business=business,
        provider=provider,
        wa_from_number=wa_from_number,
        defaults={"state": {}, "expires_at": now + timedelta(minutes=30)},
    )
    # Expire aggressively to reduce confusing states.
    if session.expires_at and session.expires_at < now:
        session.state = {}
    session.expires_at = now + timedelta(minutes=30)
    session.save(update_fields=["state", "expires_at", "updated_at"])
    return session


def _get_or_create_client(*, business: Business, wa_from_number: str, display_name: str) -> Client:
    client = Client.objects.filter(business=business, phone_number=wa_from_number).first()
    if client:
        return client
    name = (display_name or "").strip() or "לקוח"
    return Client.objects.create(business=business, full_name=name[:200], phone_number=wa_from_number)



def _provider_for_phone_number_id(phone_number_id: str) -> Optional[Provider]:
    pnid = (phone_number_id or "").strip()
    if not pnid:
        return None
    return Provider.objects.filter(is_active=True, whatsapp_phone_number_id=pnid).first()


def _provider_for_display_number(display_phone_number: str) -> Optional[Provider]:
    cc = _default_cc()
    # We store provider.whatsapp_number per provider; match by equivalence
    for p in Provider.objects.filter(is_active=True):
        if phones_equivalent(p.whatsapp_number, display_phone_number, default_country_code=cc):
            return p
    return None


def _extract_wa_events(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Yield message events in a WhatsApp Cloud webhook payload."""
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            for msg in value.get("messages") or []:
                yield {"message": msg, "metadata": metadata, "contacts": value.get("contacts") or []}


def _extract_text(message: Dict[str, Any]) -> str:
    if not isinstance(message, dict):
        return ""
    mtype = message.get("type")
    if mtype == "text":
        return ((message.get("text") or {}).get("body") or "").strip()
    # Minimal: we only handle text in MVP.
    return ""


def handle_whatsapp_webhook_payload(payload: Dict[str, Any]) -> None:
    """Main entry point: processes all messages and sends responses."""
    cc = _default_cc()

    for ev in _extract_wa_events(payload):
        msg = ev["message"]
        metadata = ev["metadata"]
        contacts = ev["contacts"]

        wa_message_id = (msg.get("id") or "").strip()
        from_number_raw = (msg.get("from") or "").strip()
        from_number = normalize_phone(from_number_raw, default_country_code=cc)
        to_display_number = (metadata.get("display_phone_number") or "").strip()
        to_phone_number_id = (metadata.get("phone_number_id") or "").strip()

        # Store inbound log (best-effort idempotency)
        if wa_message_id:
            if WhatsAppMessage.objects.filter(wa_message_id=wa_message_id, direction=WhatsAppMessage.Direction.INBOUND).exists():
                continue

        provider = _provider_for_phone_number_id(to_phone_number_id) or _provider_for_display_number(to_display_number)
        if provider is None:
            # Unknown recipient number -> ignore (could be the ops agent in the future)
            continue
        business = provider.business

        display_name = ""
        if contacts and isinstance(contacts, list) and isinstance(contacts[0], dict):
            display_name = ((contacts[0].get("profile") or {}).get("name") or "")

        client = _get_or_create_client(business=business, wa_from_number=from_number, display_name=display_name)

        text = _extract_text(msg)
        WhatsAppMessage.objects.create(
            business=business,
            provider=provider,
            client=client,
            direction=WhatsAppMessage.Direction.INBOUND,
            purpose=WhatsAppMessage.Purpose.CLIENT_AGENT,
            wa_message_id=wa_message_id,
            from_number=from_number,
            to_number=normalize_phone(to_display_number, default_country_code=cc),
            body=text,
            raw_payload={"message": msg, "metadata": metadata},
        )

        if _is_sensitive_medical(text):
            _send_text(
                business=business,
                provider=provider,
                client=client,
                to_number=from_number,
                body="כאן מטפלים רק בענייני תורים (קביעת/שינוי/ביטול). אם יש שאלה רפואית — פנו לרופא/ה ישירות.",
            )
            continue

        session = _get_or_create_session(business=business, provider=provider, wa_from_number=from_number)
        _process_text(business=business, provider=provider, client=client, session=session, text=text)


def _process_text(*, business: Business, provider: Provider, client: Client, session: ConversationSession, text: str) -> None:
    state: Dict[str, Any] = dict(session.state or {})
    t = (text or "").strip()
    lower = t.lower()

    # 1) Pending confirmation
    pending = state.get("pending")
    if isinstance(pending, dict) and pending.get("action"):
        if lower in YES_WORDS:
            _execute_pending(business=business, provider=provider, client=client, session=session)
            return
        if lower in NO_WORDS:
            state.pop("pending", None)
            session.state = state
            session.save(update_fields=["state", "updated_at"])
            _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="בוטל. איך אפשר לעזור עוד?")
            return
        _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="כדי להמשיך, נא להשיב 'כן' לאישור או 'לא' לביטול.")
        return

    # 2) Expecting a numeric choice (service/slot/appointment)
    expecting = state.get("expecting")
    if expecting in {"service", "slot", "appointment", "reschedule_slot"}:
        choice = _parse_choice_number(t)
        if choice is None:
            _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="לא הבנתי. נא להשיב עם מספר מהאפשרויות.")
            return
        options = state.get("options") or []
        if not isinstance(options, list) or choice < 1 or choice > len(options):
            _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="המספר לא תקין. נסה שוב.")
            return
        selected = options[choice - 1]

        if expecting == "service":
            state["service_id"] = int(selected["id"])
            state.pop("expecting", None)
            state.pop("options", None)
            session.state = state
            session.save(update_fields=["state", "updated_at"])
            _offer_slots(business=business, provider=provider, client=client, session=session)
            return

        if expecting == "slot":
            slot_id = int(selected["id"])
            service_id = state.get("service_id")
            service = Service.objects.filter(pk=service_id, business=business, is_active=True).first() if service_id else None
            slot = Appointment.objects.filter(pk=slot_id, business=business).select_related("room").first()
            if not slot:
                _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="האופציה כבר לא זמינה. ננסה למצוא חלונות אחרים.")
                state.clear()
                session.state = state
                session.save(update_fields=["state", "updated_at"])
                _offer_slots(business=business, provider=provider, client=client, session=session)
                return
            summary = f"לקבוע תור ל-{_fmt_dt(business, slot.start_time)} בחדר {getattr(slot.room,'name','')}?"
            state["pending"] = {"action": "book", "slot_id": slot_id, "service_id": service.id if service else None}
            state.pop("expecting", None)
            state.pop("options", None)
            session.state = state
            session.save(update_fields=["state", "updated_at"])
            _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body=f"{summary}\nהשב/י 'כן' לאישור או 'לא' לביטול.")
            return

        if expecting == "appointment":
            appt_id = int(selected["id"])
            appt = Appointment.objects.filter(pk=appt_id, business=business).first()
            if not appt:
                _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="התור לא נמצא. נסה שוב.")
                state.clear()
                session.state = state
                session.save(update_fields=["state", "updated_at"])
                return
            state["pending"] = {"action": state.get("intent") or "cancel", "appointment_id": appt_id}
            state.pop("expecting", None)
            state.pop("options", None)
            session.state = state
            session.save(update_fields=["state", "updated_at"])
            _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body=f"לבטל את התור ב-{_fmt_dt(business, appt.start_time)}?\nהשב/י 'כן' לאישור או 'לא' לביטול.")
            return

        if expecting == "reschedule_slot":
            new_slot_id = int(selected["id"])
            old_appt_id = int(state.get("old_appointment_id") or 0)
            new_slot = Appointment.objects.filter(pk=new_slot_id, business=business).first()
            if not new_slot or new_slot.client_id is not None:
                _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="האופציה כבר לא זמינה. ננסה חלונות אחרים.")
                _offer_reschedule_slots(business=business, provider=provider, client=client, session=session, old_appointment_id=old_appt_id)
                return
            state["pending"] = {"action": "reschedule", "appointment_id": old_appt_id, "slot_id": new_slot_id}
            state.pop("expecting", None)
            state.pop("options", None)
            session.state = state
            session.save(update_fields=["state", "updated_at"])
            _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body=f"להעביר את התור לחלון { _fmt_dt(business, new_slot.start_time) }?\nהשב/י 'כן' לאישור או 'לא' לביטול.")
            return

    # 3) New intent
    intent = _detect_intent(t)
    if intent == "info":
        _send_text(
            business=business,
            provider=provider,
            client=client,
            to_number=client.phone_number,
            body="אפשר: \n1) קביעת תור\n2) שינוי תור\n3) ביטול תור\nכתוב/י מה תרצה/י.",
        )
        return

    if intent == "book":
        state.clear()
        state["intent"] = "book"
        session.state = state
        session.save(update_fields=["state", "updated_at"])
        _start_booking(business=business, provider=provider, client=client, session=session)
        return

    if intent == "cancel":
        state.clear()
        state["intent"] = "cancel"
        session.state = state
        session.save(update_fields=["state", "updated_at"])
        _start_cancel(business=business, provider=provider, client=client, session=session)
        return

    if intent == "reschedule":
        state.clear()
        state["intent"] = "reschedule"
        session.state = state
        session.save(update_fields=["state", "updated_at"])
        _start_reschedule(business=business, provider=provider, client=client, session=session)
        return

    _send_text(
        business=business,
        provider=provider,
        client=client,
        to_number=client.phone_number,
        body="לא בטוח שהבנתי. אפשר לכתוב: 'קביעת תור', 'שינוי תור', או 'ביטול תור'.",
    )


def _parse_choice_number(text: str) -> Optional[int]:
    t = (text or "").strip()
    # allow forms like "1" or "1." or "1)"
    num = ""
    for ch in t:
        if ch.isdigit():
            num += ch
        else:
            break
    if not num:
        return None
    try:
        return int(num)
    except Exception:
        return None


def _eligible_services(*, business: Business, provider: Provider) -> List[Service]:
    qs = Service.objects.filter(business=business, is_active=True)
    # If provider has specialty, prefer services that match it or are general.
    if provider.specialty_id is not None:
        qs = qs.filter()
        services = list(qs)
        # Reorder: matching specialty first, then general
        matching = [s for s in services if s.specialty_id == provider.specialty_id]
        general = [s for s in services if s.specialty_id is None]
        other = [s for s in services if s.specialty_id not in {provider.specialty_id, None}]
        return matching + general + other
    return list(qs)


def _start_booking(*, business: Business, provider: Provider, client: Client, session: ConversationSession) -> None:
    services = _eligible_services(business=business, provider=provider)
    if not services:
        _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="אין שירותים זמינים כרגע. אפשר לפנות לצוות.")
        return
    if len(services) == 1:
        session.state = {"intent": "book", "service_id": services[0].id}
        session.save(update_fields=["state", "updated_at"])
        _offer_slots(business=business, provider=provider, client=client, session=session)
        return

    options = [{"id": s.id, "label": s.name} for s in services[:6]]
    session.state = {"intent": "book", "expecting": "service", "options": options}
    session.save(update_fields=["state", "updated_at"])
    lines = ["איזה שירות תרצה/י?"]
    for i, o in enumerate(options, start=1):
        lines.append(f"{i}) {o['label']}")
    _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="\n".join(lines))


def _offer_slots(*, business: Business, provider: Provider, client: Client, session: ConversationSession) -> None:
    state = dict(session.state or {})
    service_id = state.get("service_id")
    service = Service.objects.filter(pk=service_id, business=business, is_active=True).first() if service_id else None

    slots = list_free_slots(business=business, provider=provider, limit=3)
    if not slots:
        _send_text(
            business=business,
            provider=provider,
            client=client,
            to_number=client.phone_number,
            body="לא מצאתי חלונות פנויים בקרוב. אפשר לנסות תאריך אחר או לפנות לצוות.",
        )
        return

    options = [{"id": s.id, "label": _fmt_dt(business, s.start_time)} for s in slots]
    state["expecting"] = "slot"
    state["options"] = options
    session.state = state
    session.save(update_fields=["state", "updated_at"])

    header = f"חלונות פנויים{(' עבור ' + service.name) if service else ''}:"
    lines = [header]
    for i, o in enumerate(options, start=1):
        lines.append(f"{i}) {o['label']}")
    lines.append("השב/י עם מספר לבחירה.")
    _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="\n".join(lines))


def _start_cancel(*, business: Business, provider: Provider, client: Client, session: ConversationSession) -> None:
    now = timezone.now()
    qs = Appointment.objects.filter(
        business=business,
        provider=provider,
        client=client,
        start_time__gte=now,
    ).exclude(status__in=[Appointment.Status.CANCELLED_CLIENT, Appointment.Status.CANCELLED_STAFF]).order_by("start_time")
    appts = list(qs[:3])
    if not appts:
        _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="לא מצאתי תורים עתידיים לביטול.")
        return
    if len(appts) == 1:
        session.state = {"intent": "cancel", "pending": {"action": "cancel", "appointment_id": appts[0].id}}
        session.save(update_fields=["state", "updated_at"])
        _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body=f"לבטל את התור ב-{_fmt_dt(business, appts[0].start_time)}?\nהשב/י 'כן' לאישור או 'לא' לביטול.")
        return

    options = [{"id": a.id, "label": _fmt_dt(business, a.start_time)} for a in appts]
    session.state = {"intent": "cancel", "expecting": "appointment", "options": options}
    session.save(update_fields=["state", "updated_at"])
    lines = ["איזה תור לבטל?"]
    for i, o in enumerate(options, start=1):
        lines.append(f"{i}) {o['label']}")
    _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="\n".join(lines))


def _start_reschedule(*, business: Business, provider: Provider, client: Client, session: ConversationSession) -> None:
    now = timezone.now()
    qs = Appointment.objects.filter(
        business=business,
        provider=provider,
        client=client,
        start_time__gte=now,
    ).exclude(status__in=[Appointment.Status.CANCELLED_CLIENT, Appointment.Status.CANCELLED_STAFF]).order_by("start_time")
    appt = qs.first()
    if not appt:
        _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="לא מצאתי תור עתידי לשינוי.")
        return
    # MVP: reschedule the nearest upcoming appointment
    session.state = {"intent": "reschedule", "old_appointment_id": appt.id}
    session.save(update_fields=["state", "updated_at"])
    _offer_reschedule_slots(business=business, provider=provider, client=client, session=session, old_appointment_id=appt.id)


def _offer_reschedule_slots(*, business: Business, provider: Provider, client: Client, session: ConversationSession, old_appointment_id: int) -> None:
    slots = list_free_slots(business=business, provider=provider, limit=3)
    if not slots:
        _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="אין חלונות פנויים בקרוב לשינוי. אפשר לפנות לצוות.")
        return
    options = [{"id": s.id, "label": _fmt_dt(business, s.start_time)} for s in slots]
    session.state = {"intent": "reschedule", "old_appointment_id": old_appointment_id, "expecting": "reschedule_slot", "options": options}
    session.save(update_fields=["state", "updated_at"])
    lines = ["לאיזה חלון להעביר?"]
    for i, o in enumerate(options, start=1):
        lines.append(f"{i}) {o['label']}")
    _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="\n".join(lines))


def _execute_pending(*, business: Business, provider: Provider, client: Client, session: ConversationSession) -> None:
    state = dict(session.state or {})
    pending = state.get("pending") or {}
    action = pending.get("action")

    # אם אין pending תקין - אין מה לבצע
    if not action:
        session.state = {}
        session.save(update_fields=["state", "updated_at"])
        _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="לא מצאתי פעולה לאישור. אפשר להתחיל מחדש.")
        return

    try:
        if action == "book":
            slot_id = int(pending.get("slot_id"))
            service_id = pending.get("service_id")
            service = Service.objects.filter(pk=service_id, business=business, is_active=True).first() if service_id else None

            slot = assign_client_to_slot_system(business=business, slot_id=slot_id, client=client, service=service)

            # הצלחה: מנקים state
            session.state = {}
            session.save(update_fields=["state", "updated_at"])

            _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body=f"✅ נקבע תור ל-{_fmt_dt(business, slot.start_time)}.")
            return

        if action == "cancel":
            appt_id = int(pending.get("appointment_id"))
            appt = cancel_appointment_by_client_system(business=business, appointment_id=appt_id)

            session.state = {}
            session.save(update_fields=["state", "updated_at"])

            if appt.status == Appointment.Status.CANCELLATION_REQUESTED:
                _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="🕒 בקשת הביטול נשלחה לצוות לאישור. תקבל/י עדכון בהקדם.")
            else:
                _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="✅ התור בוטל.")
            return

        if action == "reschedule":
            old_id = int(pending.get("appointment_id"))
            new_slot_id = int(pending.get("slot_id"))
            reschedule_appointment_system(business=business, old_appointment_id=old_id, new_slot_id=new_slot_id)

            session.state = {}
            session.save(update_fields=["state", "updated_at"])

            _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="✅ עודכן. התור הועבר לחלון החדש.")
            return

        # פעולה לא מוכרת
        session.state = {}
        session.save(update_fields=["state", "updated_at"])
        _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="משהו השתבש. אפשר לנסות שוב.")
        return

    except ValueError as e:
        code = str(e)
        print(f"[CLIENT_AGENT] execute_pending ValueError action={action} code={code} pending={pending}", flush=True)

        if code in {"slot_not_available", "provider_conflict", "room_conflict", "room_block"}:
            # פה *לא* מנקים הכל — מציעים חלופות, ומשאירים state חדש ש-_offer_slots יכתוב
            state.pop("pending", None)
            session.state = state
            session.save(update_fields=["state", "updated_at"])

            _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="האופציה לא זמינה יותר. ננסה חלונות אחרים.")
            if action == "book":
                _offer_slots(business=business, provider=provider, client=client, session=session)
            return

        # כשל אחר: מנקים state ומחזירים הודעה
        session.state = {}
        session.save(update_fields=["state", "updated_at"])
        _send_text(business=business, provider=provider, client=client, to_number=client.phone_number, body="לא הצלחתי לבצע את הפעולה. אפשר לנסות שוב או לפנות לצוות.")
        return

