from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .models import Appointment, Reminder

# Rules: (reminder_type, lead_time)
REMINDER_RULES = (
    (Reminder.ReminderType.PRIMARY_24H, timedelta(hours=24)),
    (Reminder.ReminderType.SECONDARY_3H, timedelta(hours=3)),
)


def ensure_reminders_for_appointment(appt: Appointment) -> None:
    """Create the default reminders for an appointment (idempotent)."""
    now = timezone.now()

    for reminder_type, lead in REMINDER_RULES:
        scheduled_time = appt.start_time - lead
        status = (
            Reminder.ReminderStatus.PENDING
            if scheduled_time > now
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


def skip_pending_reminders(appt: Appointment) -> int:
    """Skip any pending reminders attached to an appointment."""
    return appt.reminders.filter(status=Reminder.ReminderStatus.PENDING).update(
        status=Reminder.ReminderStatus.SKIPPED
    )
