from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Appointment, Reminder
from core.views import build_public_action_url, make_appointment_action_token
from core.notifications import get_provider
provider = get_provider()


class Command(BaseCommand):
    help = "Send due reminders (MVP: prints messages; with --execute marks as SENT)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually mark reminders as SENT (otherwise dry-run print only).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Max reminders to process in one run.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        limit = int(options["limit"])
        execute = bool(options["execute"])

        qs = (
            Reminder.objects.select_related("appointment", "appointment__client", "appointment__business")
            .filter(
                status=Reminder.ReminderStatus.PENDING,
                scheduled_time__lte=now,
                appointment__status__in=(Appointment.Status.SCHEDULED, Appointment.Status.CONFIRMED),
            )
            .order_by("scheduled_time")[:limit]
        )

        if not qs:
            self.stdout.write(self.style.SUCCESS("No due reminders found."))
            return

        self.stdout.write(f"Found {len(qs)} due reminders (execute={execute}).\n")

        for r in qs:
            appt = r.appointment
            client = appt.client

            token_confirm = make_appointment_action_token(appointment_id=appt.id, action="confirm")
            token_cancel = make_appointment_action_token(appointment_id=appt.id, action="cancel")

            url_confirm = build_public_action_url(token=token_confirm, action="confirm")
            url_cancel = build_public_action_url(token=token_cancel, action="cancel")

            msg = (
                f"REMINDER #{r.id} | {r.type} | scheduled={r.scheduled_time:%Y-%m-%d %H:%M}\n"
                f"Client: {client.full_name} | phone={client.phone_number}\n"
                f"Appointment: {appt.start_time:%Y-%m-%d %H:%M} | status={appt.status}\n"
                f"Confirm: {url_confirm}\n"
                f"Cancel:  {url_cancel}\n"
            )
            self.stdout.write(msg)

            if execute:
                provider_id = provider.send(to=client.phone_number, body=msg)
                r.status = Reminder.ReminderStatus.SENT
                r.sent_at = now
                r.save(update_fields=["status", "sent_at"])

        if execute:
            self.stdout.write(self.style.SUCCESS("\nDone. Marked reminders as SENT."))
        else:
            self.stdout.write(self.style.WARNING("\nDry-run complete. Use --execute to mark as SENT."))
