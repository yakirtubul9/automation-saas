# core/views.py
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.utils import timezone

from .models import Business, Appointment, Reminder


@login_required
def dashboard(request):
    """
    דשבורד פשוט לבעל העסק:
    - מניח שלכל משתמש יש Business אחד כרגע.
    """
    business = get_object_or_404(Business, owner=request.user)

    now = timezone.now()

    # תורים קרובים (הבא בתור + קצת קדימה)
    upcoming_appointments = (
        Appointment.objects
        .filter(business=business, start_time__gte=now)
        .order_by("start_time")[:10]
    )

    # סטטיסטיקות קטנות
    today = now.date()
    tomorrow = today + timedelta(days=1)

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
    return render(request, "core/dashboard.html", context)


@login_required
def settings_view(request):
    """
    מסך הגדרות בסיסי – כרגע רק placeholder.
    בהמשך נוסיף:
    - הגדרת זמני תזכורות
    - טמפלייטים להודעות
    - ערוצי תקשורת מועדפים
    """
    business = get_object_or_404(Business, owner=request.user)
    return render(request, "core/settings.html", {"business": business})

