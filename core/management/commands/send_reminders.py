from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Appointment, Reminder
from core.views import build_public_action_url, make_appointment_action_token
from core.notifications import get_provider


class Command(BaseCommand):
    help = "Send due reminders (MVP: prints messages; with --execute marks as SENT)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually send and mark reminders as SENT (otherwise dry-run print only).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Max reminders to process in one run.",
        )

    def handle(self, *args, **options):
        provider = get_provider()
        now = timezone.now()
        limit = int(options["limit"])
        execute = bool(options["execute"])

        qs = (
            Reminder.objects.select_related("appointment", "appointment__client", "appointment__business")
            .filter(
                status=Reminder.ReminderStatus.PENDING,
                scheduled_time__lte=now,
                appointment__status__in=(Appointment.Status.SCHEDULED, Appointment.Status.CONFIRMED),
                appointment__client__isnull=False,  # slots without client should never be sent
            )
            .order_by("scheduled_time")[:limit]
        )

        reminders = list(qs)
        if not reminders:
            self.stdout.write(self.style.SUCCESS("No due reminders found."))
            return

        self.stdout.write(f"Found {len(reminders)} due reminders (execute={execute}).\n")

        for r in reminders:
            appt = r.appointment
            client = appt.client
            if client is None:
                # Safety net; shouldn't happen because of the filter above
                continue

            token_confirm = make_appointment_action_token(appointment_id=appt.id, action="confirm")
            token_cancel = make_appointment_action_token(appointment_id=appt.id, action="cancel")

            url_confirm = build_public_action_url(token=token_confirm, action="confirm")
            url_cancel = build_public_action_url(token=token_cancel, action="cancel")

            dt = timezone.localtime(appt.start_time)
            date_str = dt.strftime("%d/%m/%Y")
            time_str = dt.strftime("%H:%M")

            msg = (
                f"REMINDER #{r.id} | {r.type} | scheduled={r.scheduled_time:%Y-%m-%d %H:%M}\n"
                f"Client: {client.full_name} | phone={client.phone_number}\n"
                f"Appointment: {appt.start_time:%Y-%m-%d %H:%M} | status={appt.status}\n"
                f"Confirm: {url_confirm}\n"
                f"Cancel:  {url_cancel}\n"
            )
            self.stdout.write(msg)

            provider_message_id = None
            if execute:
                provider_message_id = provider.send(
                    to=client.phone_number,
                    body=msg,  # נשאר בשביל log/debug; בתבנית לא חייבים להשתמש בו
                    template_params=[date_str, time_str, url_confirm, url_cancel],
                )

                r.status = Reminder.ReminderStatus.SENT
                r.sent_at = timezone.now()

                # If you later add a provider_message_id field, this will store it.
                if hasattr(r, "provider_message_id"):
                    r.provider_message_id = provider_message_id

                # Update only fields that exist
                update_fields = ["status", "sent_at"]
                if hasattr(r, "provider_message_id"):
                    update_fields.append("provider_message_id")
                r.save(update_fields=update_fields)

        if execute:
            self.stdout.write(self.style.SUCCESS("\nDone. Marked reminders as SENT."))
        else:
            self.stdout.write(self.style.WARNING("\nDry-run complete. Use --execute to send + mark as SENT."))
