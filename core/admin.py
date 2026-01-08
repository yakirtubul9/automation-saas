from django.contrib import admin
from django.utils import timezone

from .models import Business, Client, Service, Appointment, Reminder


def mark_reminders_as_sent(modeladmin, request, queryset):
    now = timezone.now()
    updated = queryset.filter(status=Reminder.ReminderStatus.PENDING).update(
        status=Reminder.ReminderStatus.SENT,
        sent_at=now,
    )
    modeladmin.message_user(request, f"Marked {updated} reminders as SENT.")

mark_reminders_as_sent.short_description = "Mark selected PENDING reminders as SENT"


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "timezone", "created_at")
    search_fields = ("name", "owner__username")


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("full_name", "business", "phone_number", "email", "is_active", "created_at")
    list_filter = ("business", "is_active")
    search_fields = ("full_name", "phone_number", "email")


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "duration_minutes", "is_active")
    list_filter = ("business", "is_active")
    search_fields = ("name",)


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("client", "business", "service", "start_time", "end_time", "status")
    list_filter = ("business", "status", "service")
    search_fields = ("client__full_name",)


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = ("appointment", "type", "channel", "scheduled_time", "status", "sent_at")
    list_filter = ("status", "type", "channel")
    actions = [mark_reminders_as_sent]
