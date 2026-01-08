# core/views.py
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils import timezone

from django.conf import settings
from django.core import signing
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseNotFound

from .models import Business, Appointment, Reminder


def _get_or_create_business_for_user(user) -> Business:
    # לוקחים את הראשון אם יש כמה, אחרת יוצרים אחד בסיסי
    business = Business.objects.filter(owner=user).order_by("id").first()
    if business:
        return business
    return Business.objects.create(owner=user, name=f"{user.username} Business")


@login_required
def dashboard(request):
    # מקור אמת יחיד: הפונקציה הזו
    business = _get_or_create_business_for_user(request.user)

    now = timezone.now()
    today = now.date()

    upcoming_appointments = (
        Appointment.objects.filter(business=business, start_time__gte=now)
        .order_by("start_time")[:10]
    )

    today_appointments = Appointment.objects.filter(
        business=business,
        start_time__date=today,
    ).count()

    reminders_pending = Reminder.objects.filter(
        appointment__business=business,
        status=Reminder.ReminderStatus.PENDING,
    ).count()

    reminders_sent_today = Reminder.objects.filter(
        appointment__business=business,
        status=Reminder.ReminderStatus.SENT,
        sent_at__date=today,
    ).count()

    context = {
        "business": business,
        "upcoming_appointments": upcoming_appointments,
        "today_appointments": today_appointments,
        "reminders_pending": reminders_pending,
        "reminders_sent_today": reminders_sent_today,
    }

    return render(request, "Core/dashboard.html", context)


@login_required
def settings_view(request):
    business = _get_or_create_business_for_user(request.user)
    return render(request, "Core/settings.html", {"business": business})


def logout_view(request):
    logout(request)
    return redirect("login")



# --- Public confirm/cancel links (MVP) ---

_APPT_ACTION_SALT = "core.appointment.action"


def make_appointment_action_token(*, appointment_id: int, action: str) -> str:
    """Create a signed, time-limited token for a public appointment action."""
    payload = {"aid": int(appointment_id), "act": str(action)}
    return signing.dumps(payload, salt=_APPT_ACTION_SALT, compress=True)


def build_public_action_url(*, token: str, action: str) -> str:
    base = getattr(settings, "SITE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    return f"{base}/a/{token}/{action}/"


def appointment_action_view(request, token: str, action: str):
    """Public endpoint to confirm/cancel an appointment using a signed token.

    Security model (MVP): the signed token is the authorization.
    """
    action = (action or "").strip().lower()
    if action not in {"confirm", "cancel"}:
        return HttpResponseBadRequest("Invalid action")

    max_age = int(getattr(settings, "APPOINTMENT_ACTION_LINK_MAX_AGE_SECONDS", 14 * 24 * 60 * 60))

    try:
        data = signing.loads(token, salt=_APPT_ACTION_SALT, max_age=max_age)
    except signing.SignatureExpired:
        return HttpResponse("הקישור פג תוקף.", status=410)
    except signing.BadSignature:
        return HttpResponseBadRequest("קישור לא תקין.")

    if data.get("act") != action:
        return HttpResponseBadRequest("קישור לא תואם לפעולה.")

    appointment_id = data.get("aid")
    try:
        appt = Appointment.objects.select_related("client", "business").get(pk=appointment_id)
    except Appointment.DoesNotExist:
        return HttpResponseNotFound("התור לא נמצא.")

    # Apply action idempotently
    if action == "confirm":
        if appt.status in {Appointment.Status.CANCELLED_CLIENT, Appointment.Status.CANCELLED_STAFF}:
            return HttpResponse("התור כבר בוטל.", status=409)
        appt.status = Appointment.Status.CONFIRMED
        appt.save(update_fields=["status"])
        return HttpResponse("✅ התור אושר בהצלחה. תודה!", content_type="text/html; charset=utf-8")

    # cancel
    if appt.status in {Appointment.Status.CANCELLED_CLIENT, Appointment.Status.CANCELLED_STAFF}:
        return HttpResponse("התור כבר בוטל.", status=200)

    appt.status = Appointment.Status.CANCELLED_CLIENT
    appt.save(update_fields=["status"])
    # Pending reminders will be skipped by the signal.
    return HttpResponse("✅ התור בוטל בהצלחה.", content_type="text/html; charset=utf-8")
