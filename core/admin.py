from django.contrib import admin
from django.utils import timezone

from .models import (Business, BusinessMembership, Client, ClientOnboarding, Service, Appointment, Reminder,
                     Specialty, Room, Provider, CancellationRequest, AuditEvent)


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
    list_display = ("name", "owner", "timezone", "auto_cancel_cutoff_hours", "created_at")
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
    list_display = ("business", "provider", "room", "client", "service", "start_time", "end_time", "status")
    list_filter = ("business", "status", "service")
    search_fields = ("client__full_name", "provider__display_name")


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = ("appointment", "type", "channel", "scheduled_time", "status", "sent_at")
    list_filter = ("status", "type", "channel")
    actions = [mark_reminders_as_sent]


@admin.register(Specialty)
class SpecialtyAdmin(admin.ModelAdmin):
    list_display = ("name", "business")
    list_filter = ("business",)
    search_fields = ("name",)


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "is_active")
    list_filter = ("business", "is_active")
    search_fields = ("name",)
    filter_horizontal = ("specialties",)


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("display_name", "business", "specialty", "whatsapp_number", "is_active")
    list_filter = ("business", "is_active", "specialty")
    search_fields = ("display_name", "whatsapp_number")


@admin.register(BusinessMembership)
class BusinessMembershipAdmin(admin.ModelAdmin):
    list_display = ("business", "user", "role", "created_at")
    list_filter = ("business", "role")
    search_fields = ("user__username", "business__name")


@admin.register(ClientOnboarding)
class ClientOnboardingAdmin(admin.ModelAdmin):
    list_display = ("business", "phone_number", "full_name", "status", "created_at")
    list_filter = ("business", "status")
    search_fields = ("phone_number", "full_name")


@admin.register(CancellationRequest)
class CancellationRequestAdmin(admin.ModelAdmin):
    list_display = ("appointment", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("appointment__id",)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("business", "created_at", "action", "actor_user", "object_type", "object_id")
    list_filter = ("business", "action")
    search_fields = ("action", "object_type", "object_id", "actor_user__username")
