from django.db import models
from django.contrib.auth.models import User


class Business(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="businesses")
    name = models.CharField(max_length=200)
    timezone = models.CharField(max_length=64, default="Asia/Jerusalem")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Client(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="clients")
    full_name = models.CharField(max_length=200)
    phone_number = models.CharField(max_length=50)
    email = models.EmailField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.full_name} ({self.business.name})"


class Service(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="services")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    duration_minutes = models.PositiveIntegerField(default=60)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.business.name})"


class Appointment(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "מתוכנן"
        CONFIRMED = "confirmed_by_client", "אושר ע\"י לקוח"
        CANCELLED_CLIENT = "cancelled_by_client", "בוטל ע\"י לקוח"
        CANCELLED_STAFF = "cancelled_by_staff", "בוטל ע\"י צוות"
        COMPLETED = "completed", "הושלם"
        NO_SHOW = "no_show", "לא הגיע"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="appointments")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="appointments")
    service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True, blank=True)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.SCHEDULED,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.client.full_name} @ {self.start_time} ({self.business.name})"


class Reminder(models.Model):
    class ReminderStatus(models.TextChoices):
        PENDING = "pending", "ממתין"
        SENT = "sent", "נשלח"
        FAILED = "failed", "נכשל"
        SKIPPED = "skipped", "דולג"

    class ReminderType(models.TextChoices):
        PRIMARY_24H = "primary_24h", "24 שעות לפני"
        SECONDARY_3H = "secondary_3h", "3 שעות לפני"
        CUSTOM = "custom", "מותאם"

    class Channel(models.TextChoices):
        WHATSAPP = "whatsapp", "WhatsApp"
        SMS = "sms", "SMS"
        EMAIL = "email", "Email"

    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name="reminders")
    scheduled_time = models.DateTimeField()
    sent_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(
        max_length=16,
        choices=ReminderStatus.choices,
        default=ReminderStatus.PENDING,
    )
    type = models.CharField(
        max_length=32,
        choices=ReminderType.choices,
        default=ReminderType.PRIMARY_24H,
    )
    channel = models.CharField(
        max_length=16,
        choices=Channel.choices,
        default=Channel.WHATSAPP,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Reminder {self.type} for {self.appointment}"
