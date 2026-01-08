from __future__ import annotations

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from .models import Appointment
from .reminders import ensure_reminders_for_appointment, skip_pending_reminders


@receiver(pre_save, sender=Appointment)
def _appointment_pre_save(sender, instance: Appointment, **kwargs):
    # Track previous status for status-change logic
    if not instance.pk:
        instance._previous_status = None  # type: ignore[attr-defined]
        return
    try:
        instance._previous_status = Appointment.objects.only("status").get(pk=instance.pk).status  # type: ignore[attr-defined]
    except Appointment.DoesNotExist:
        instance._previous_status = None  # type: ignore[attr-defined]


@receiver(post_save, sender=Appointment)
def _appointment_post_save(sender, instance: Appointment, created: bool, **kwargs):
    # Create reminders when an appointment is created (for any non-terminal status).
    if created and instance.status not in (
        Appointment.Status.CANCELLED_CLIENT,
        Appointment.Status.CANCELLED_STAFF,
        Appointment.Status.COMPLETED,
        Appointment.Status.NO_SHOW,
    ):
        ensure_reminders_for_appointment(instance)
        return

    # On status changes to cancelled/completed/no-show, skip any pending reminders.
    prev = getattr(instance, "_previous_status", None)
    if prev is not None and prev != instance.status:
        if instance.status in (
            Appointment.Status.CANCELLED_CLIENT,
            Appointment.Status.CANCELLED_STAFF,
            Appointment.Status.COMPLETED,
            Appointment.Status.NO_SHOW,
        ):
            skip_pending_reminders(instance)
