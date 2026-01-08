from __future__ import annotations

from datetime import timedelta

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Appointment, Reminder


REMINDER_RULES = (
    (Reminder.ReminderType.PRIMARY_24H, timedelta(hours=24)),
    (Reminder.ReminderType.SECONDARY_3H, timedelta(hours=3)),
)


def _ensure_reminders_for_appointment(appt: Appointment) -> None:
    # Create the standard reminders only if they don't already exist
    for reminder_type, delta in REMINDER_RULES:
        scheduled_time = appt.start_time - delta

        # If the scheduled time has already passed, we mark it as skipped (MVP behavior).
        status = (
            Reminder.ReminderStatus.PENDING
            if scheduled_time > timezone.now()
            else Reminder.ReminderStatus.SKIPPED
        )

        Reminder.objects.get_or_create(
            appointment=appt,
            type=reminder_type,
            defaults={
                "scheduled_time": scheduled_time,
                "status": status,
                "channel": Reminder.Channel.WHATSAPP,
            },
        )


def _skip_pending_reminders(appt: Appointment) -> None:
    Reminder.objects.filter(
        appointment=appt,
        status=Reminder.ReminderStatus.PENDING,
    ).update(status=Reminder.ReminderStatus.SKIPPED)


@receiver(pre_save, sender=Appointment)
def _appointment_pre_save(sender, instance: Appointment, **kwargs):
    if not instance.pk:
        instance._previous_status = None  # type: ignore[attr-defined]
        return
    try:
        instance._previous_status = Appointment.objects.only("status").get(pk=instance.pk).status  # type: ignore[attr-defined]
    except Appointment.DoesNotExist:
        instance._previous_status = None  # type: ignore[attr-defined]


@receiver(post_save, sender=Appointment)
def _appointment_post_save(sender, instance: Appointment, created: bool, **kwargs):
    # Create reminders when an appointment is created.
    if created and instance.status in (
        Appointment.Status.SCHEDULED,
        Appointment.Status.CONFIRMED,
    ):
        _ensure_reminders_for_appointment(instance)
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
            _skip_pending_reminders(instance)
