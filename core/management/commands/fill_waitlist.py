from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Appointment
from core.waitlist import create_offers_for_slot


class Command(BaseCommand):
    help = "Fill upcoming free slots from the waitlist (Stage 4)."

    def add_arguments(self, parser):
        parser.add_argument("--hours-from", type=int, default=24)
        parser.add_argument("--hours-to", type=int, default=72)
        parser.add_argument("--limit-slots", type=int, default=200)
        parser.add_argument("--max-offers", type=int, default=3)
        parser.add_argument("--ttl-min", type=int, default=30)
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually create offers and send messages. Without this flag, it's a dry-run.",
        )

    def handle(self, *args, **options):
        hours_from = max(0, int(options["hours_from"]))
        hours_to = max(hours_from, int(options["hours_to"]))
        limit_slots = max(1, int(options["limit_slots"]))
        max_offers = max(1, int(options["max_offers"]))
        ttl_min = max(5, int(options["ttl_min"]))
        execute = bool(options["execute"])

        now = timezone.now()
        start = now + timedelta(hours=hours_from)
        end = now + timedelta(hours=hours_to)

        reserved_status = getattr(Appointment.Status, "RESERVED", "reserved")

        qs = (
            Appointment.objects
            .filter(client__isnull=True, status=reserved_status, start_time__gte=start, start_time__lte=end)
            .select_related("business")
            .order_by("start_time")
        )

        slots = list(qs[:limit_slots])
        self.stdout.write(f"Found {len(slots)} free slots between {start.isoformat()} and {end.isoformat()}")

        if not execute:
            self.stdout.write("Dry-run: no offers created. Use --execute to run.")
            return

        total_created = 0
        for slot in slots:
            created = create_offers_for_slot(slot=slot, max_offers=max_offers, ttl_minutes=ttl_min, execute_send=True)
            total_created += created

        self.stdout.write(f"Done. offers_created={total_created}")
