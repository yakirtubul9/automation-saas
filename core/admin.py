from django.contrib import admin
from .models import Business, Client, Service, Appointment, Reminder


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
