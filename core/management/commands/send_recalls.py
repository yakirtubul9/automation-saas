from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import RecallTarget
from core.recall import process_due_recall_targets


class Command(BaseCommand):
    help = "Send recall messages for due recall targets (Stage 5)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Do not send WhatsApp messages")
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        limit = int(options.get("limit") or 50)

        now = timezone.now()
        qs = RecallTarget.objects.filter(status__in=[RecallTarget.Status.PENDING, RecallTarget.Status.OFFERED], due_at__lte=now)
        total = qs.count()

        processed = process_due_recall_targets(limit=limit, execute_send=not dry_run)

        self.stdout.write(self.style.SUCCESS(f"Recall: processed={processed} (eligible={total}) dry_run={dry_run}"))
