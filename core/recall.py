from __future__ import annotations

from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .api import _safe_create_audit_event, ACTIVE_APPOINTMENT_STATUSES
from .models import Appointment, RecallOffer, RecallProtocol, RecallTarget
from .notifications import get_provider
from .reminders import ensure_reminders_for_appointment
from .views import build_public_recall_offer_action_url, make_recall_offer_action_token


def _business_tz(business) -> ZoneInfo:
    return ZoneInfo(getattr(business, "timezone", "Asia/Jerusalem") or "Asia/Jerusalem")


def ensure_recall_target_for_completed_appointment(appt: Appointment) -> Optional[RecallTarget]:
    """Create (idempotent) a recall target when an appointment is completed.

    Preconditions:
      - appointment has client, provider, service
      - a RecallProtocol exists for (business, service) and is active
    """
    if not appt.business_id or not appt.client_id or not appt.provider_id or not appt.service_id:
        return None

    protocol = (
        RecallProtocol.objects.filter(
            business_id=appt.business_id,
            service_id=appt.service_id,
            is_active=True,
        )
        .order_by("id")
        .first()
    )
    if not protocol:
        return None

    interval_days = int(protocol.interval_days or 0)
    if interval_days <= 0:
        return None

    due_at = appt.end_time + timedelta(days=interval_days)

    target, created = RecallTarget.objects.get_or_create(
        business_id=appt.business_id,
        source_appointment_id=appt.id,
        defaults={
            "client_id": appt.client_id,
            "provider_id": appt.provider_id,
            "service_id": appt.service_id,
            "due_at": due_at,
            "status": RecallTarget.Status.PENDING,
        },
    )

    if created:
        _safe_create_audit_event(
            business=appt.business,
            actor_user=None,
            action="recall_target_created",
            object_type="RecallTarget",
            object_id=str(target.id),
            before=None,
            after={
                "due_at": target.due_at.isoformat(),
                "client_id": target.client_id,
                "provider_id": target.provider_id,
                "service_id": target.service_id,
                "source_appointment_id": target.source_appointment_id,
            },
        )

    return target


def _find_existing_future_booking(*, target: RecallTarget, now) -> Optional[Appointment]:
    """Default policy: if the client already has a future appointment (same provider+service), mark as booked."""
    qs = Appointment.objects.filter(
        business_id=target.business_id,
        client_id=target.client_id,
        start_time__gte=now,
        status__in=ACTIVE_APPOINTMENT_STATUSES,
    )
    if target.provider_id:
        qs = qs.filter(provider_id=target.provider_id)
    if target.service_id:
        qs = qs.filter(service_id=target.service_id)

    return qs.order_by("start_time", "id").first()


def _expire_if_needed(offer: RecallOffer, *, now) -> bool:
    if offer.expires_at and offer.expires_at <= now:
        offer.status = RecallOffer.Status.EXPIRED
        offer.decided_at = now
        offer.decision_note = "expired"
        offer.save(update_fields=["status", "decided_at", "decision_note"])
        return True
    return False


def create_offers_for_target(
    *,
    target: RecallTarget,
    max_offers: Optional[int] = None,
    ttl_minutes: Optional[int] = None,
    lookahead_days: Optional[int] = None,
    execute_send: bool = True,
) -> int:
    """Create and (optionally) send recall offers for a due target.

    Returns number of created offers.
    """
    if target.status in {RecallTarget.Status.BOOKED, RecallTarget.Status.CANCELLED}:
        return 0

    now = timezone.now()

    # If already booked (default policy), resolve it.
    existing = _find_existing_future_booking(target=target, now=now)
    if existing:
        target.status = RecallTarget.Status.BOOKED
        target.booked_appointment_id = existing.id
        target.resolved_at = now
        target.save(update_fields=["status", "booked_appointment", "resolved_at"])
        _safe_create_audit_event(
            business=target.business,
            actor_user=None,
            action="recall_target_auto_booked",
            object_type="RecallTarget",
            object_id=str(target.id),
            before={"status": "pending"},
            after={"status": target.status, "booked_appointment_id": existing.id},
        )
        return 0

    max_offers = int(max_offers or getattr(settings, "RECALL_MAX_OFFERS_PER_TARGET", 3) or 3)
    max_offers = max(1, min(max_offers, 10))

    ttl_minutes = int(ttl_minutes or getattr(settings, "RECALL_OFFER_TTL_MINUTES", 60) or 60)
    ttl_minutes = max(10, min(ttl_minutes, 14 * 24 * 60))

    lookahead_days = int(lookahead_days or getattr(settings, "RECALL_LOOKAHEAD_DAYS", 21) or 21)
    lookahead_days = max(1, min(lookahead_days, 90))

    expires_at = now + timedelta(minutes=ttl_minutes)

    # Find candidate reserved slots for the provider.
    slots_qs = Appointment.objects.filter(
        business_id=target.business_id,
        provider_id=target.provider_id,
        client_id__isnull=True,
        status=getattr(Appointment.Status, "RESERVED", "reserved"),
        start_time__gte=now,
        start_time__lte=now + timedelta(days=lookahead_days),
    ).order_by("start_time", "id")

    created = 0
    offers_to_send: list[RecallOffer] = []

    with transaction.atomic():
        t = RecallTarget.objects.select_for_update().filter(pk=target.pk).first()
        if not t:
            return 0
        if t.status in {RecallTarget.Status.BOOKED, RecallTarget.Status.CANCELLED}:
            return 0

        existing_pending_slots = set(
            RecallOffer.objects.filter(target_id=t.id, status=RecallOffer.Status.PENDING)
            .values_list("slot_id", flat=True)
        )

        for slot in slots_qs[: max_offers * 5]:
            if created >= max_offers:
                break
            if slot.id in existing_pending_slots:
                continue

            offer, offer_created = RecallOffer.objects.get_or_create(
                business_id=t.business_id,
                target_id=t.id,
                slot_id=slot.id,
                defaults={
                    "status": RecallOffer.Status.PENDING,
                    "expires_at": expires_at,
                },
            )
            if not offer_created:
                continue
            offers_to_send.append(offer)
            created += 1

        if created:
            t.status = RecallTarget.Status.OFFERED
            t.last_notified_at = now
            t.save(update_fields=["status", "last_notified_at"])

        _safe_create_audit_event(
            business=t.business,
            actor_user=None,
            action="recall_offers_created",
            object_type="RecallTarget",
            object_id=str(t.id),
            before=None,
            after={"offers_created": created},
        )

    if execute_send and offers_to_send:
        provider = get_provider()
        tz = _business_tz(target.business)
        client = target.client
        service_name = target.service.name if target.service_id else ""
        provider_name = target.provider.display_name if target.provider_id else ""

        lines = [
            "תזכורת לביקור המשך (Recall)",
            f"שירות: {service_name or '-'}",
            f"רופא: {provider_name or '-'}",
            "בחר אחד מהחלונות הבאים:",
        ]

        for idx, offer in enumerate(offers_to_send, start=1):
            slot = Appointment.objects.filter(pk=offer.slot_id).first()
            if not slot:
                continue
            when = slot.start_time.astimezone(tz).strftime("%d/%m/%Y %H:%M")
            accept_token = make_recall_offer_action_token(offer_id=offer.id, action="accept")
            decline_token = make_recall_offer_action_token(offer_id=offer.id, action="decline")
            accept_url = build_public_recall_offer_action_url(token=accept_token, action="accept")
            decline_url = build_public_recall_offer_action_url(token=decline_token, action="decline")
            lines.append(f"{idx}) {when} | לאישור: {accept_url} | לדחייה: {decline_url}")

        body = "\n".join(lines) + "\n"

        for offer in offers_to_send:
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


def decline_offer(*, offer_id: int) -> tuple[bool, str]:
    now = timezone.now()
    with transaction.atomic():
        offer = (
            RecallOffer.objects.select_for_update()
            .select_related("target", "target__client", "slot")
            .filter(pk=offer_id)
            .first()
        )
        if not offer:
            return False, "ההצעה לא נמצאה."

        if offer.status != RecallOffer.Status.PENDING:
            return True, "כבר עודכן בעבר."

        if _expire_if_needed(offer, now=now):
            return False, "ההצעה פג תוקף."

        offer.status = RecallOffer.Status.DECLINED
        offer.decided_at = now
        offer.decision_note = "declined_by_client"
        offer.save(update_fields=["status", "decided_at", "decision_note"])

        _safe_create_audit_event(
            business=offer.business,
            actor_user=None,
            action="recall_offer_declined",
            object_type="RecallOffer",
            object_id=str(offer.id),
            before={"status": "pending"},
            after={"status": offer.status},
        )

    return True, "עודכן. תודה."


def _cancel_pending_offers_for_slot(*, slot_id: int, now, note: str) -> None:
    (RecallOffer.objects.filter(slot_id=slot_id, status=RecallOffer.Status.PENDING)
     .update(status=RecallOffer.Status.CANCELLED, decided_at=now, decision_note=note))


def accept_offer(*, offer_id: int) -> tuple[bool, str]:
    now = timezone.now()
    with transaction.atomic():
        offer = (
            RecallOffer.objects.select_for_update()
            .select_related("target", "target__client", "target__service", "slot")
            .filter(pk=offer_id)
            .first()
        )
        if not offer:
            return False, "ההצעה לא נמצאה."

        if offer.status == RecallOffer.Status.ACCEPTED:
            return True, "כבר אושר בעבר."
        if offer.status != RecallOffer.Status.PENDING:
            return False, "ההצעה כבר לא זמינה."

        if _expire_if_needed(offer, now=now):
            return False, "ההצעה פג תוקף."

        target = RecallTarget.objects.select_for_update().filter(pk=offer.target_id).first()
        if not target or target.status in {RecallTarget.Status.CANCELLED}:
            offer.status = RecallOffer.Status.CANCELLED
            offer.decided_at = now
            offer.decision_note = "target_missing_or_cancelled"
            offer.save(update_fields=["status", "decided_at", "decision_note"])
            return False, "הבקשה כבר לא זמינה."

        slot = Appointment.objects.select_for_update().filter(pk=offer.slot_id).first()
        if not slot:
            offer.status = RecallOffer.Status.CANCELLED
            offer.decided_at = now
            offer.decision_note = "slot_missing"
            offer.save(update_fields=["status", "decided_at", "decision_note"])
            return False, "התור כבר לא קיים."

        # Ensure slot is still free.
        if slot.client_id is not None or slot.status != getattr(Appointment.Status, "RESERVED", "reserved"):
            offer.status = RecallOffer.Status.CANCELLED
            offer.decided_at = now
            offer.decision_note = "slot_taken"
            offer.save(update_fields=["status", "decided_at", "decision_note"])
            return False, "מישהו כבר תפס את התור."

        # Assign the client & service to the slot.
        slot.client_id = target.client_id
        if target.service_id and (slot.service_id is None):
            slot.service_id = target.service_id
        slot.status = Appointment.Status.SCHEDULED
        slot.save(update_fields=["client_id", "service_id", "status"])

        ensure_reminders_for_appointment(slot)

        # Resolve target.
        target.status = RecallTarget.Status.BOOKED
        target.booked_appointment_id = slot.id
        target.resolved_at = now
        target.save(update_fields=["status", "booked_appointment", "resolved_at"])

        offer.status = RecallOffer.Status.ACCEPTED
        offer.decided_at = now
        offer.decision_note = "accepted_by_client"
        offer.save(update_fields=["status", "decided_at", "decision_note"])

        # Cancel other pending offers for this target.
        (RecallOffer.objects.filter(target_id=target.id, status=RecallOffer.Status.PENDING)
         .exclude(pk=offer.id)
         .update(status=RecallOffer.Status.CANCELLED, decided_at=now, decision_note="other_offer_accepted"))

        # Also cancel any other pending recall offers for the same slot (other targets).
        _cancel_pending_offers_for_slot(slot_id=slot.id, now=now, note="slot_assigned")

        # Stage 4 (Waitlist): slot is no longer available; cancel any pending waitlist offers for it.
        try:
            from .models import WaitlistOffer
            (WaitlistOffer.objects.filter(slot_id=slot.id, status=WaitlistOffer.Status.PENDING)
             .update(status=WaitlistOffer.Status.CANCELLED, decided_at=now, decision_note="slot_assigned"))
        except Exception:
            pass

        _safe_create_audit_event(
            business=target.business,
            actor_user=None,
            action="recall_offer_accepted",
            object_type="Appointment",
            object_id=str(slot.id),
            before={"status": "reserved", "client_id": None},
            after={"status": slot.status, "client_id": slot.client_id, "service_id": slot.service_id},
        )

    return True, "אושר. התור נקבע."


def process_due_recall_targets(*, limit: int = 50, execute_send: bool = True) -> int:
    """Process due recall targets (Stage 5).

    - Picks targets with status pending/offered and due_at <= now
    - Default: if already booked, resolves as booked
    - Otherwise creates (and optionally sends) offers

    Returns number of targets processed.
    """
    now = timezone.now()

    limit = int(limit or 50)
    limit = max(1, min(limit, 500))

    min_interval_hours = int(getattr(settings, "RECALL_MIN_HOURS_BETWEEN_NOTIFICATIONS", 24) or 24)
    min_interval = timedelta(hours=max(0, min_interval_hours))

    qs = (
        RecallTarget.objects
        .select_related("client", "provider", "service", "business")
        .filter(status__in=[RecallTarget.Status.PENDING, RecallTarget.Status.OFFERED], due_at__lte=now)
        .order_by("due_at", "id")
    )

    processed = 0
    for target in qs[:limit]:
        # Throttle notifications to avoid spamming.
        if target.last_notified_at and min_interval and target.last_notified_at > now - min_interval:
            continue
        try:
            create_offers_for_target(target=target, execute_send=execute_send)
            processed += 1
        except Exception:
            # Recall processing must not crash the whole run.
            continue

    return processed
