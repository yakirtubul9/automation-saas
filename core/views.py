# core/views.py
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils import timezone

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
