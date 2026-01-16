from __future__ import annotations

from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import Appointment, Reminder

# Rules: (reminder_type, lead_time)
REMINDER_RULES = (
    (Reminder.ReminderType.PRIMARY_24H, timedelta(hours=24)),
    (Reminder.ReminderType.SECONDARY_3H, timedelta(hours=3)),
)


def ensure_reminders_for_appointment(appt: Appointment) -> None:
    """Create the default reminders for an appointment (idempotent)."""
    # Slots without a client should not generate reminders.
    if appt.client_id is None:
        return

    now = timezone.now()

    for reminder_type, lead in REMINDER_RULES:
        # Default: lead time
        scheduled_time = appt.start_time - lead

        # Special rule: if appointment is on Sunday, 24h reminder goes to Friday 12:00
        if reminder_type == Reminder.ReminderType.PRIMARY_24H:
            # Work in local time to interpret "Sunday" correctly
            if timezone.is_aware(appt.start_time):
                tz = timezone.get_current_timezone()
                start_local = timezone.localtime(appt.start_time, tz)
                if start_local.weekday() == 6:  # Monday=0 ... Sunday=6
                    friday_date = start_local.date() - timedelta(days=2)
                    scheduled_time = timezone.make_aware(datetime.combine(friday_date, time(12, 0)), tz)
            else:
                # Fallback if USE_TZ=False
                if appt.start_time.weekday() == 6:
                    friday_date = appt.start_time.date() - timedelta(days=2)
                    scheduled_time = datetime.combine(friday_date, time(12, 0))

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
