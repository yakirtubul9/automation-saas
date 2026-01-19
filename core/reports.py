from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.utils import timezone

from .models import Appointment, Business


ACTIVE_UPCOMING_STATUSES = (
    Appointment.Status.RESERVED,
    Appointment.Status.SCHEDULED,
    Appointment.Status.CONFIRMED,
    Appointment.Status.CANCELLATION_REQUESTED,
)

TERMINAL_STATUSES = (
    Appointment.Status.CANCELLED_CLIENT,
    Appointment.Status.CANCELLED_STAFF,
    Appointment.Status.COMPLETED,
    Appointment.Status.NO_SHOW,
)


@dataclass(frozen=True)
class WeeklyMetrics:
    business_id: int
    week_start: date  # Monday (inclusive)
    week_end: date  # next Monday (exclusive)

    prev_total: int
    prev_booked: int
    prev_completed: int
    prev_no_show: int
    prev_cancelled: int
    prev_no_show_rate_pct: float

    next_capacity: int
    next_booked: int
    next_free: int
    next_occupancy_pct: float

    alerts: List[str]
    recommendations: List[str]
    notes: List[str]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # JSONField does not auto-serialize date objects across all DB backends
        d["week_start"] = self.week_start.isoformat()
        d["week_end"] = self.week_end.isoformat()
        return d


def _local_midnight(d: date, tz) -> datetime:
    return timezone.make_aware(datetime.combine(d, time(0, 0)), tz)


def get_week_bounds(*, business: Business, as_of: Optional[datetime] = None) -> Tuple[date, date]:
    """Return (week_start, week_end) as dates in business timezone.

    Week is Monday..next Monday.
    """
    if as_of is None:
        as_of = timezone.now()

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(business.timezone or settings.TIME_ZONE)
    except Exception:
        tz = timezone.get_current_timezone()

    local = timezone.localtime(as_of, tz)
    week_start = local.date() - timedelta(days=local.weekday())
    week_end = week_start + timedelta(days=7)
    return week_start, week_end


def compute_weekly_metrics(*, business: Business, as_of: Optional[datetime] = None) -> WeeklyMetrics:
    if as_of is None:
        as_of = timezone.now()

    # Business timezone
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(business.timezone or settings.TIME_ZONE)
    except Exception:
        tz = timezone.get_current_timezone()

    week_start, week_end = get_week_bounds(business=business, as_of=as_of)
    prev_start = week_start - timedelta(days=7)
    prev_end = week_start

    prev_start_dt = _local_midnight(prev_start, tz)
    prev_end_dt = _local_midnight(prev_end, tz)
    next_start_dt = _local_midnight(week_start, tz)
    next_end_dt = _local_midnight(week_end, tz)

    prev_qs = Appointment.objects.filter(business=business, start_time__gte=prev_start_dt, start_time__lt=prev_end_dt)
    prev_total = prev_qs.count()
    prev_booked = prev_qs.filter(client__isnull=False).count()
    prev_completed = prev_qs.filter(status=Appointment.Status.COMPLETED).count()
    prev_no_show = prev_qs.filter(status=Appointment.Status.NO_SHOW).count()
    prev_cancelled = prev_qs.filter(status__in=(Appointment.Status.CANCELLED_CLIENT, Appointment.Status.CANCELLED_STAFF)).count()

    denom = prev_completed + prev_no_show
    prev_no_show_rate_pct = (prev_no_show / denom * 100.0) if denom else 0.0

    next_qs = Appointment.objects.filter(business=business, start_time__gte=next_start_dt, start_time__lt=next_end_dt)

    # Capacity is defined by "active" slot statuses only. Cancelled/completed/no-show are excluded
    # to avoid double-counting (e.g. cancelled appt + freed reserved slot).
    cap_qs = next_qs.filter(status__in=ACTIVE_UPCOMING_STATUSES)
    next_capacity = cap_qs.count()
    next_booked = cap_qs.filter(client__isnull=False).count()
    next_free = cap_qs.filter(client__isnull=True).count()
    next_occupancy_pct = (next_booked / next_capacity * 100.0) if next_capacity else 0.0

    alerts: List[str] = []
    recs: List[str] = []
    notes: List[str] = []

    no_show_alert_pct = float(getattr(settings, "WEEKLY_REPORT_NO_SHOW_ALERT_PCT", 20))
    week_empty_pct = float(getattr(settings, "WEEKLY_REPORT_WEEK_EMPTY_PCT", 40))
    week_full_pct = float(getattr(settings, "WEEKLY_REPORT_WEEK_FULL_PCT", 90))
    min_capacity_slots = int(getattr(settings, "WEEKLY_REPORT_MIN_CAPACITY_SLOTS", 10))

    if denom >= 5 and prev_no_show_rate_pct >= no_show_alert_pct:
        alerts.append(f"No-show גבוה בשבוע הקודם: {prev_no_show_rate_pct:.1f}% ({prev_no_show}/{denom})")
        recs.append("לשקול להקשיח מדיניות ביטול/דמי אי-הגעה ולוודא שתזכורות נשלחות בזמן.")

    if next_capacity < min_capacity_slots:
        alerts.append(f"כמעט אין שיבוצים בשבוע הקרוב: capacity={next_capacity}")
        recs.append("להוסיף Slots לרופאים / לפתוח שעות ביומן כדי לאפשר קביעת תורים.")
    else:
        if next_occupancy_pct < week_empty_pct:
            alerts.append(f"השבוע הקרוב כמעט ריק: תפוסה {next_occupancy_pct:.1f}% ({next_booked}/{next_capacity})")
            recs.append("להריץ מילוי חורים (Waitlist) ולשקול Recall למטופלים מתאימים.")
        elif next_occupancy_pct > week_full_pct:
            alerts.append(f"השבוע הקרוב כמעט מלא: תפוסה {next_occupancy_pct:.1f}% ({next_booked}/{next_capacity})")
            recs.append("לשקול לפתוח עוד Slots/חדרים כדי לא לאבד ביקושים.")

    # Revenue is not available yet (no service pricing) — be explicit.
    notes.append("הכנסה משוערת לא מחושבת בשלב זה (אין מחיר לשירותים במודל).")

    # De-duplicate recs while keeping order
    seen = set()
    recs_unique: List[str] = []
    for r in recs:
        if r not in seen:
            seen.add(r)
            recs_unique.append(r)

    return WeeklyMetrics(
        business_id=business.id,
        week_start=week_start,
        week_end=week_end,
        prev_total=prev_total,
        prev_booked=prev_booked,
        prev_completed=prev_completed,
        prev_no_show=prev_no_show,
        prev_cancelled=prev_cancelled,
        prev_no_show_rate_pct=prev_no_show_rate_pct,
        next_capacity=next_capacity,
        next_booked=next_booked,
        next_free=next_free,
        next_occupancy_pct=next_occupancy_pct,
        alerts=alerts,
        recommendations=recs_unique,
        notes=notes,
    )


def render_weekly_report_text(*, business: Business, metrics: WeeklyMetrics) -> str:
    # Show inclusive week dates (Mon..Sun)
    week_end_inclusive = metrics.week_end - timedelta(days=1)
    prev_week_start = metrics.week_start - timedelta(days=7)
    prev_week_end_inclusive = metrics.week_start - timedelta(days=1)

    lines: List[str] = []
    lines.append(f"דוח שבועי — {business.name}")
    lines.append(f"שבוע קודם: {prev_week_start:%d/%m}–{prev_week_end_inclusive:%d/%m}")
    lines.append(
        f"• סה״כ שיבוצים: {metrics.prev_total} | עם מטופל: {metrics.prev_booked} | הושלמו: {metrics.prev_completed} | לא הגיעו: {metrics.prev_no_show} | בוטלו: {metrics.prev_cancelled}"
    )
    denom = metrics.prev_completed + metrics.prev_no_show
    if denom:
        lines.append(f"• שיעור No-show (מתוך הגיע/לא הגיע): {metrics.prev_no_show_rate_pct:.1f}%")

    lines.append("")
    lines.append(f"שבוע קרוב: {metrics.week_start:%d/%m}–{week_end_inclusive:%d/%m}")
    lines.append(
        f"• capacity (Slots פעילים): {metrics.next_capacity} | תפוסים (עם מטופל): {metrics.next_booked} | פנויים: {metrics.next_free} | תפוסה: {metrics.next_occupancy_pct:.1f}%"
    )

    lines.append("")
    if metrics.alerts:
        lines.append("חריגות:")
        for a in metrics.alerts:
            lines.append(f"• {a}")
    else:
        lines.append("חריגות: אין")

    if metrics.recommendations:
        lines.append("")
        lines.append("המלצות פעולה:")
        for r in metrics.recommendations:
            lines.append(f"• {r}")

    if metrics.notes:
        lines.append("")
        for n in metrics.notes:
            lines.append(f"הערה: {n}")

    return "\n".join(lines).strip()


def render_weekly_report_subject(*, business: Business, metrics: WeeklyMetrics) -> str:
    week_end_inclusive = metrics.week_end - timedelta(days=1)
    return f"דוח שבועי — {business.name} ({metrics.week_start:%d/%m}–{week_end_inclusive:%d/%m})"
