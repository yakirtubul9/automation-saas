from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Business, BusinessMembership, WeeklyReportLog
from core.notifications import get_provider
from core.reports import compute_weekly_metrics, render_weekly_report_subject, render_weekly_report_text


class Command(BaseCommand):
    help = "Send weekly KPI report + basic alerts to Owner/Staff (Stage 6)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually send messages and persist as SENT (otherwise dry-run).",
        )
        parser.add_argument("--business-id", type=int, default=None, help="Run only for a single business id")
        parser.add_argument(
            "--as-of",
            type=str,
            default=None,
            help="Compute report as-of datetime (ISO). Default: now.",
        )
        parser.add_argument(
            "--channels",
            type=str,
            default="whatsapp",
            help="Comma-separated: whatsapp,email,both. Default: whatsapp",
        )

    def handle(self, *args, **options):
        execute = bool(options.get("execute"))
        business_id = options.get("business_id")

        as_of_raw = options.get("as_of")
        as_of: Optional[datetime]
        if as_of_raw:
            try:
                as_of = datetime.fromisoformat(as_of_raw)
                if timezone.is_naive(as_of):
                    as_of = timezone.make_aware(as_of, timezone.get_current_timezone())
            except Exception as e:
                raise SystemExit(f"Invalid --as-of value: {as_of_raw} ({e})")
        else:
            as_of = timezone.now()

        channels_raw = (options.get("channels") or "whatsapp").strip().lower()
        if channels_raw == "both":
            channels = {"whatsapp", "email"}
        else:
            channels = {c.strip() for c in channels_raw.split(",") if c.strip()}
        channels = {c for c in channels if c in {"whatsapp", "email"}}
        if not channels:
            channels = {"whatsapp"}

        qs = Business.objects.all().order_by("id")
        if business_id:
            qs = qs.filter(id=business_id)

        businesses = list(qs)
        if not businesses:
            self.stdout.write(self.style.WARNING("No businesses found."))
            return

        provider = get_provider() if "whatsapp" in channels else None

        total_sent = 0
        total_failed = 0

        for biz in businesses:
            metrics = compute_weekly_metrics(business=biz, as_of=as_of)
            subject = render_weekly_report_subject(business=biz, metrics=metrics)
            body = render_weekly_report_text(business=biz, metrics=metrics)

            memberships_qs = (
                BusinessMembership.objects.select_related("user")
                .filter(
                    business=biz,
                    role__in=(BusinessMembership.Role.OWNER, BusinessMembership.Role.STAFF),
                    receive_weekly_report=True,
                )
                .order_by("role", "id")
            )

            memberships = list(memberships_qs)
            # Safety: some businesses may rely only on Business.owner without a membership row.
            if not any((m.role == BusinessMembership.Role.OWNER and m.user_id == biz.owner_id) for m in memberships):
                memberships.insert(
                    0,
                    BusinessMembership(
                        business=biz,
                        user=biz.owner,
                        role=BusinessMembership.Role.OWNER,
                        whatsapp_number="",
                        receive_weekly_report=True,
                        receive_alerts=True,
                    ),
                )

            recipients_log: List[dict] = []
            send_errors: List[str] = []
            channels_used: List[str] = []

            for m in memberships:
                user = m.user
                # WhatsApp
                if "whatsapp" in channels and provider is not None and m.whatsapp_number:
                    recipients_log.append({"role": m.role, "channel": "whatsapp", "to": m.whatsapp_number})
                    if execute:
                        try:
                            tpl_name = getattr(settings, "WEEKLY_REPORT_TEMPLATE_NAME", None)
                            if tpl_name:
                                provider.send(
                                    to=m.whatsapp_number,
                                    body=body,
                                    template_params=[body],
                                    template_name=tpl_name,
                                )
                            else:
                                # Force text mode even if a global template exists
                                provider.send(to=m.whatsapp_number, body=body, template_name="")
                            channels_used.append("whatsapp")
                            total_sent += 1
                        except Exception as e:
                            total_failed += 1
                            send_errors.append(f"whatsapp:{m.whatsapp_number}:{e}")
                    else:
                        self.stdout.write(f"[DRY] WhatsApp -> {m.whatsapp_number}\n{body}\n")

                # Email
                if "email" in channels and getattr(settings, "WEEKLY_REPORT_SEND_EMAIL", False) and user.email:
                    recipients_log.append({"role": m.role, "channel": "email", "to": user.email})
                    if execute:
                        try:
                            from django.core.mail import send_mail

                            from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com")
                            send_mail(subject, body, from_email, [user.email], fail_silently=False)
                            channels_used.append("email")
                            total_sent += 1
                        except Exception as e:
                            total_failed += 1
                            send_errors.append(f"email:{user.email}:{e}")
                    else:
                        self.stdout.write(f"[DRY] Email -> {user.email}\nSubject: {subject}\n{body}\n")

            # Persist a log record (always)
            status = WeeklyReportLog.Status.DRY_RUN
            err = ""
            if execute:
                if send_errors:
                    status = WeeklyReportLog.Status.FAILED
                    err = "\n".join(send_errors)[:5000]
                else:
                    status = WeeklyReportLog.Status.SENT

            WeeklyReportLog.objects.create(
                business=biz,
                week_start=metrics.week_start,
                week_end=metrics.week_end,
                payload=metrics.to_dict(),
                channels=list(sorted(set(channels_used))) if execute else list(sorted(channels)),
                recipients=recipients_log,
                status=status,
                error=err,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Weekly reports done. execute={execute} sent={total_sent} failed={total_failed} businesses={len(businesses)}"
            )
        )
