from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from core.models import Appointment, Business, CancellationRequest, Client, Provider, RoomBlock, Service


RESERVED_STATUS = getattr(Appointment.Status, "RESERVED", "reserved")


def _overlaps_qs(*, business: Business, start, end, provider_id: Optional[int] = None, room_id: Optional[int] = None):
    qs = Appointment.objects.filter(business=business, start_time__lt=end, end_time__gt=start)
    # Conflicts should ignore cancelled appointments (they no longer occupy the calendar).
    qs = qs.exclude(status__in=[Appointment.Status.CANCELLED_CLIENT, Appointment.Status.CANCELLED_STAFF])
    if provider_id is not None:
        qs = qs.filter(provider_id=provider_id)
    if room_id is not None:
        qs = qs.filter(room_id=room_id)
    return qs


def list_free_slots(*, business: Business, provider: Provider, limit: int = 3, from_dt=None, to_dt=None):
    from_dt = from_dt or timezone.now()
    to_dt = to_dt or (from_dt + timedelta(days=30))
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
    return list(qs[: max(1, min(limit, 10))])


def assign_client_to_slot_system(
    *,
    business: Business,
    slot_id: int,
    client: Client,
    service: Optional[Service] = None,
    actor_user: Optional[User] = None,
):
    """Assign a client to a reserved slot without relying on a logged-in user.

    NOTE (Postgres): avoid combining select_for_update() with select_related() on nullable FKs.
    Postgres rejects `FOR UPDATE` when the query includes a LEFT OUTER JOIN (nullable side).
    We lock the Appointment row only, and let Django lazily fetch related rows if needed.
    """
    with transaction.atomic():
        # Lock the slot row only (no joins here).
        slot = Appointment.objects.select_for_update().filter(pk=slot_id, business=business).first()
        if not slot:
            raise ValueError("slot_not_found")
        if slot.status != RESERVED_STATUS or slot.client_id is not None:
            raise ValueError("slot_not_available")
        if slot.end_time <= timezone.now():
            raise ValueError("in_past")

        if service is not None and service.business_id != business.id:
            raise ValueError("service_not_found")

        # Specialty enforcement (same as API)
        if service is not None and service.specialty_id is not None:
            if slot.provider_id and getattr(slot.provider, "specialty_id", None) is not None:
                if slot.provider.specialty_id != service.specialty_id:
                    raise ValueError("specialty_mismatch")
            if slot.room_id and not slot.room.specialties.filter(pk=service.specialty_id).exists():
                raise ValueError("specialty_mismatch")

        # Revalidate conflicts.
        start_dt, end_dt = slot.start_time, slot.end_time
        if slot.provider_id and _overlaps_qs(
            business=business,
            start=start_dt,
            end=end_dt,
            provider_id=slot.provider_id,
        ).exclude(pk=slot.pk).exists():
            raise ValueError("provider_conflict")

        if slot.room_id and _overlaps_qs(
            business=business,
            start=start_dt,
            end=end_dt,
            room_id=slot.room_id,
        ).exclude(pk=slot.pk).exists():
            raise ValueError("room_conflict")

        # RoomBlock conflicts (safety)
        if slot.room_id and RoomBlock.objects.filter(
            business=business,
            room_id=slot.room_id,
            is_active=True,
            start_time__lt=end_dt,
            end_time__gt=start_dt,
        ).exists():
            raise ValueError("room_block")

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

        from core.reminders import ensure_reminders_for_appointment

        ensure_reminders_for_appointment(slot)

        # Best-effort cancel any pending waitlist offers
        try:
            from core.models import WaitlistOffer

            now = timezone.now()
            (
                WaitlistOffer.objects.filter(slot_id=slot.id, status=WaitlistOffer.Status.PENDING)
                .update(status=WaitlistOffer.Status.CANCELLED, decided_at=now, decision_note="slot_assigned")
            )
        except Exception:
            pass

        after = {
            "id": slot.id,
            "client_id": slot.client_id,
            "service_id": slot.service_id,
            "status": slot.status,
        }

        from core.models import AuditEvent

        AuditEvent.objects.create(
            business=business,
            actor_user=actor_user,
            action="assign_client_to_slot",
            object_type="Appointment",
            object_id=str(slot.id),
            before=before,
            after=after,
        )

        return slot


def cancel_appointment_by_client_system(
    *,
    business: Business,
    appointment_id: int,
    actor_user: Optional[User] = None,
):
    """Cancel (or request cancel) for an appointment, following business policy.

    NOTE (Postgres): same as assign_client_to_slot_system — lock without joins.
    """
    with transaction.atomic():
        appt = Appointment.objects.select_for_update().filter(pk=appointment_id, business=business).first()
        if not appt:
            raise ValueError("appointment_not_found")

        if appt.status in {Appointment.Status.CANCELLED_CLIENT, Appointment.Status.CANCELLED_STAFF}:
            return appt

        cutoff_hours = int(getattr(business, "auto_cancel_cutoff_hours", 0) or 0)
        now = timezone.now()
        hours_until = (appt.start_time - now).total_seconds() / 3600.0

        before = {
            "id": appt.id,
            "status": appt.status,
        }

        if cutoff_hours > 0 and hours_until < cutoff_hours:
            CancellationRequest.objects.create(appointment=appt)
            appt.status = Appointment.Status.CANCELLATION_REQUESTED
            appt.save(update_fields=["status"])
        else:
            appt.status = Appointment.Status.CANCELLED_CLIENT
            appt.save(update_fields=["status"])

            # Create freed slot + trigger waitlist (best-effort), same as public link flow
            try:
                existing = Appointment.objects.filter(
                    business=appt.business,
                    provider_id=appt.provider_id,
                    room_id=appt.room_id,
                    start_time=appt.start_time,
                    end_time=appt.end_time,
                    status=RESERVED_STATUS,
                    client__isnull=True,
                ).first()
                if existing is None:
                    freed_slot = Appointment.objects.create(
                        business=appt.business,
                        client=None,
                        provider=appt.provider,
                        room=appt.room,
                        service=appt.service,
                        start_time=appt.start_time,
                        end_time=appt.end_time,
                        status=RESERVED_STATUS,
                    )
                    from core.waitlist import create_offers_for_slot

                    create_offers_for_slot(slot=freed_slot)
            except Exception:
                pass

        from core.models import AuditEvent

        AuditEvent.objects.create(
            business=business,
            actor_user=actor_user,
            action="cancel_appointment_by_client",
            object_type="Appointment",
            object_id=str(appt.id),
            before=before,
            after={"id": appt.id, "status": appt.status},
        )
        return appt


def reschedule_appointment_system(
    *,
    business: Business,
    old_appointment_id: int,
    new_slot_id: int,
    actor_user: Optional[User] = None,
):
    """Reschedule by moving the client to a new reserved slot and cancelling the old appointment.

    NOTE (Postgres): lock each Appointment row without joins (no select_related with FOR UPDATE).
    """
    with transaction.atomic():
        old_appt = Appointment.objects.select_for_update().filter(pk=old_appointment_id, business=business).first()
        if not old_appt:
            raise ValueError("appointment_not_found")
        if old_appt.client_id is None:
            raise ValueError("not_a_client_appointment")

        new_slot = Appointment.objects.select_for_update().filter(pk=new_slot_id, business=business).first()
        if not new_slot:
            raise ValueError("slot_not_found")

        if new_slot.status != RESERVED_STATUS or new_slot.client_id is not None:
            raise ValueError("slot_not_available")

        # Assign client to the new slot (keeps service if possible)
        assign_client_to_slot_system(
            business=business,
            slot_id=new_slot.id,
            client=old_appt.client,
            service=old_appt.service,
            actor_user=actor_user,
        )

        # Cancel old appointment (policy-aware)
        cancel_appointment_by_client_system(
            business=business,
            appointment_id=old_appt.id,
            actor_user=actor_user,
        )

        return new_slot
