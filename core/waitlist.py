from __future__ import annotations

from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .api import _safe_create_audit_event
from .models import Appointment, WaitlistEntry, WaitlistOffer
from .notifications import get_provider
from .reminders import ensure_reminders_for_appointment
from .views import build_public_waitlist_offer_action_url, make_waitlist_offer_action_token


def _business_tz(business) -> ZoneInfo:
    return ZoneInfo(getattr(business, "timezone", "Asia/Jerusalem") or "Asia/Jerusalem")


def _matches_entry(*, slot: Appointment, entry: WaitlistEntry) -> bool:
    if entry.status != WaitlistEntry.Status.ACTIVE:
        return False
    if not entry.client_id or not entry.client.is_active:
        return False

    if entry.provider_id and slot.provider_id != entry.provider_id:
        return False
    if entry.service_id and slot.service_id != entry.service_id:
        return False

    tz = _business_tz(slot.business)
    local_start = slot.start_time.astimezone(tz)

    if entry.preferred_weekdays:
        try:
            weekdays = {int(x) for x in (entry.preferred_weekdays or [])}
        except Exception:
            weekdays = set()
        if weekdays and local_start.weekday() not in weekdays:
            return False

    if entry.time_window_start and entry.time_window_end:
        t = local_start.time()
        start_t = entry.time_window_start
        end_t = entry.time_window_end
        # Basic window: only support same-day windows (start < end).
        if start_t < end_t:
            if not (start_t <= t <= end_t):
                return False

    if entry.min_notice_hours:
        delta = slot.start_time - timezone.now()
        if delta < timedelta(hours=int(entry.min_notice_hours)):
            return False

    return True


def create_offers_for_slot(
    *,
    slot: Appointment,
    max_offers: Optional[int] = None,
    ttl_minutes: Optional[int] = None,
    execute_send: bool = True,
) -> int:
    """Create and (optionally) send waitlist offers for a freed/available slot.

    Returns number of created offers.
    """
    if slot.client_id is not None:
        return 0

    if slot.status != getattr(Appointment.Status, "RESERVED", "reserved"):
        return 0

    max_offers = int(max_offers or getattr(settings, "WAITLIST_MAX_OFFERS_PER_SLOT", 3) or 3)
    max_offers = max(1, min(max_offers, 10))

    ttl_minutes = int(ttl_minutes or getattr(settings, "WAITLIST_OFFER_TTL_MINUTES", 30) or 30)
    ttl_minutes = max(5, min(ttl_minutes, 24 * 60))

    now = timezone.now()
    expires_at = now + timedelta(minutes=ttl_minutes)

    qs = (
        WaitlistEntry.objects.select_related("client")
        .filter(business=slot.business, status=WaitlistEntry.Status.ACTIVE, client__is_active=True)
        .order_by("created_at", "id")
    )

    created = 0
    offers_to_send: list[WaitlistOffer] = []

    with transaction.atomic():
        # Lock slot to avoid parallel offer creation for the same slot.
        slot_locked = Appointment.objects.select_for_update().filter(pk=slot.pk).first()
        if not slot_locked or slot_locked.client_id is not None or slot_locked.status != slot.status:
            return 0

        existing_pending = set(
            WaitlistOffer.objects.filter(slot=slot_locked, status=WaitlistOffer.Status.PENDING)
            .values_list("entry_id", flat=True)
        )

        for entry in qs:
            if created >= max_offers:
                break
            if entry.id in existing_pending:
                continue
            if not _matches_entry(slot=slot_locked, entry=entry):
                continue

            offer, offer_created = WaitlistOffer.objects.get_or_create(
                business=slot.business,
                entry=entry,
                slot=slot_locked,
                defaults={
                    "status": WaitlistOffer.Status.PENDING,
                    "expires_at": expires_at,
                },
            )
            if not offer_created:
                continue

            offers_to_send.append(offer)
            created += 1

        _safe_create_audit_event(
            business=slot.business,
            actor_user=None,
            action="waitlist_offers_created",
            object_type="Appointment",
            object_id=str(slot.id),
            before=None,
            after={"offers_created": created},
        )

    # Send messages outside the transaction.
    if execute_send and offers_to_send:
        provider = get_provider()
        tz = _business_tz(slot.business)
        when = slot.start_time.astimezone(tz).strftime("%d/%m/%Y %H:%M")

        for offer in offers_to_send:
            client = offer.entry.client
            accept_token = make_waitlist_offer_action_token(offer_id=offer.id, action="accept")
            decline_token = make_waitlist_offer_action_token(offer_id=offer.id, action="decline")
            accept_url = build_public_waitlist_offer_action_url(token=accept_token, action="accept")
            decline_url = build_public_waitlist_offer_action_url(token=decline_token, action="decline")

            body = (
                "התפנה תור!\n"
                f"מועד: {when}\n"
                f"לאישור: {accept_url}\n"
                f"לדחייה: {decline_url}\n"
            )

            try:
                msg_id = provider.send(to=client.phone_number, body=body, template_name="")
                offer.sent_at = timezone.now()
                offer.sent_message_id = msg_id or ""
                offer.send_error = ""
                offer.save(update_fields=["sent_at", "sent_message_id", "send_error"])
            except Exception as e:
                offer.send_error = str(e)
                offer.save(update_fields=["send_error"])

    return created


def _expire_if_needed(offer: WaitlistOffer, *, now) -> bool:
    if offer.expires_at and offer.expires_at <= now:
        offer.status = WaitlistOffer.Status.EXPIRED
        offer.decided_at = now
        offer.decision_note = "expired"
        offer.save(update_fields=["status", "decided_at", "decision_note"])
        return True
    return False


def decline_offer(*, offer_id: int) -> tuple[bool, str]:
    now = timezone.now()
    with transaction.atomic():
        offer = (
            WaitlistOffer.objects.select_for_update()
            .select_related("entry", "entry__client", "slot")
            .filter(pk=offer_id)
            .first()
        )
        if not offer:
            return False, "ההצעה לא נמצאה."

        if offer.status != WaitlistOffer.Status.PENDING:
            return True, "כבר עודכן בעבר."

        if _expire_if_needed(offer, now=now):
            return False, "ההצעה פג תוקף."

        offer.status = WaitlistOffer.Status.DECLINED
        offer.decided_at = now
        offer.decision_note = "declined_by_client"
        offer.save(update_fields=["status", "decided_at", "decision_note"])

        _safe_create_audit_event(
            business=offer.business,
            actor_user=None,
            action="waitlist_offer_declined",
            object_type="WaitlistOffer",
            object_id=str(offer.id),
            before={"status": "pending"},
            after={"status": offer.status},
        )

    return True, "עודכן. תודה."


def accept_offer(*, offer_id: int) -> tuple[bool, str]:
    now = timezone.now()

    with transaction.atomic():
        offer = (
            WaitlistOffer.objects.select_for_update()
            .select_related("entry", "entry__client", "slot")
            .filter(pk=offer_id)
            .first()
        )
        if not offer:
            return False, "ההצעה לא נמצאה."

        if offer.status == WaitlistOffer.Status.ACCEPTED:
            return True, "כבר אושר בעבר."

        if offer.status != WaitlistOffer.Status.PENDING:
            return False, "ההצעה כבר לא זמינה."

        if _expire_if_needed(offer, now=now):
            return False, "ההצעה פג תוקף."

        slot = Appointment.objects.select_for_update().filter(pk=offer.slot_id).first()
        if not slot:
            offer.status = WaitlistOffer.Status.CANCELLED
            offer.decided_at = now
            offer.decision_note = "slot_missing"
            offer.save(update_fields=["status", "decided_at", "decision_note"])
            return False, "התור כבר לא קיים."

        # First-come-first-served: ensure slot is still free.
        if slot.client_id is not None or slot.status != getattr(Appointment.Status, "RESERVED", "reserved"):
            offer.status = WaitlistOffer.Status.CANCELLED
            offer.decided_at = now
            offer.decision_note = "slot_taken"
            offer.save(update_fields=["status", "decided_at", "decision_note"])
            return False, "מישהו כבר תפס את התור."

        # Assign the client.
        slot.client_id = offer.entry.client_id
        slot.status = Appointment.Status.SCHEDULED
        slot.save(update_fields=["client_id", "status"])

        ensure_reminders_for_appointment(slot)

        offer.status = WaitlistOffer.Status.ACCEPTED
        offer.decided_at = now
        offer.decision_note = "accepted_by_client"
        offer.save(update_fields=["status", "decided_at", "decision_note"])

        # Cancel other pending offers for this slot.
        (WaitlistOffer.objects.filter(slot_id=slot.id, status=WaitlistOffer.Status.PENDING)
         .exclude(pk=offer.id)
         .update(status=WaitlistOffer.Status.CANCELLED, decided_at=now, decision_note="other_offer_accepted"))

        _safe_create_audit_event(
            business=offer.business,
            actor_user=None,
            action="waitlist_offer_accepted",
            object_type="Appointment",
            object_id=str(slot.id),
            before={"status": "reserved", "client_id": None},
            after={"status": slot.status, "client_id": slot.client_id},
        )

    return True, "אושר. התור נקבע."
