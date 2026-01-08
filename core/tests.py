from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Business, Client, Service, Appointment, Reminder
from core.views import build_public_action_url, make_appointment_action_token


class DashboardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="u1", password="pass12345")

    def test_dashboard_requires_login(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])

    def test_dashboard_creates_business_if_missing(self):
        self.assertFalse(Business.objects.filter(owner=self.user).exists())
        self.client.login(username="u1", password="pass12345")

        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)

        self.assertTrue(Business.objects.filter(owner=self.user).exists())

    def test_dashboard_counts(self):
        self.client.login(username="u1", password="pass12345")
        self.client.get(reverse("dashboard"))  # trigger business creation
        biz = Business.objects.get(owner=self.user)

        c = Client.objects.create(business=biz, full_name="Client A", phone_number="0500000000")
        s = Service.objects.create(business=biz, name="Service 1", duration_minutes=60)

        now = timezone.now()
        appt_today = Appointment.objects.create(
            business=biz,
            client=c,
            service=s,
            start_time=now + timedelta(hours=2),
            end_time=now + timedelta(hours=3),
            status=Appointment.Status.SCHEDULED,
        )
        Appointment.objects.create(
            business=biz,
            client=c,
            service=s,
            start_time=now + timedelta(days=2),
            end_time=now + timedelta(days=2, hours=1),
            status=Appointment.Status.SCHEDULED,
        )

        Reminder.objects.create(
            appointment=appt_today,
            scheduled_time=now + timedelta(minutes=5),
            status=Reminder.ReminderStatus.PENDING,
            type=Reminder.ReminderType.SECONDARY_3H,
            channel=Reminder.Channel.WHATSAPP,
        )

        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(resp.context["today_appointments"], 1)
        self.assertEqual(resp.context["reminders_pending"], 1)
        self.assertEqual(resp.context["reminders_sent_today"], 0)


class ReminderAutomationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="owner", password="pass12345")
        self.biz = Business.objects.create(owner=self.user, name="Biz")
        self.client_obj = Client.objects.create(
            business=self.biz, full_name="Client A", phone_number="0500000000"
        )
        self.service = Service.objects.create(business=self.biz, name="Service 1", duration_minutes=60)

    def test_auto_creates_two_reminders_on_appointment_create(self):
        # Start time far enough so both reminders are in the future
        start = timezone.now() + timedelta(days=3)
        appt = Appointment.objects.create(
            business=self.biz,
            client=self.client_obj,
            service=self.service,
            start_time=start,
            end_time=start + timedelta(minutes=60),
            status=Appointment.Status.SCHEDULED,
        )

        reminders = list(Reminder.objects.filter(appointment=appt).order_by("scheduled_time"))
        self.assertEqual(len(reminders), 2)

        types = {r.type for r in reminders}
        self.assertIn(Reminder.ReminderType.PRIMARY_24H, types)
        self.assertIn(Reminder.ReminderType.SECONDARY_3H, types)

        for r in reminders:
            self.assertEqual(r.status, Reminder.ReminderStatus.PENDING)

    def test_cancel_skips_pending_reminders(self):
        start = timezone.now() + timedelta(days=3)
        appt = Appointment.objects.create(
            business=self.biz,
            client=self.client_obj,
            service=self.service,
            start_time=start,
            end_time=start + timedelta(minutes=60),
            status=Appointment.Status.SCHEDULED,
        )
        self.assertEqual(
            Reminder.objects.filter(appointment=appt, status=Reminder.ReminderStatus.PENDING).count(), 2
        )

        appt.status = Appointment.Status.CANCELLED_CLIENT
        appt.save()

        self.assertEqual(
            Reminder.objects.filter(appointment=appt, status=Reminder.ReminderStatus.PENDING).count(), 0
        )
        self.assertEqual(
            Reminder.objects.filter(appointment=appt, status=Reminder.ReminderStatus.SKIPPED).count(), 2
        )


class PublicLinksTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="owner2", password="pass12345")
        self.biz = Business.objects.create(owner=self.user, name="Biz2")
        self.client_obj = Client.objects.create(
            business=self.biz, full_name="Client B", phone_number="0500000001"
        )
        self.service = Service.objects.create(business=self.biz, name="Service 1", duration_minutes=60)

    def test_public_confirm_sets_status_confirmed(self):
        start = timezone.now() + timedelta(days=2)
        appt = Appointment.objects.create(
            business=self.biz,
            client=self.client_obj,
            service=self.service,
            start_time=start,
            end_time=start + timedelta(minutes=60),
            status=Appointment.Status.SCHEDULED,
        )

        token = make_appointment_action_token(appointment_id=appt.id, action="confirm")
        url_path = f"/a/{token}/confirm/"
        resp = self.client.get(url_path)
        self.assertEqual(resp.status_code, 200)

        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.CONFIRMED)

    def test_public_cancel_sets_status_cancelled(self):
        start = timezone.now() + timedelta(days=2)
        appt = Appointment.objects.create(
            business=self.biz,
            client=self.client_obj,
            service=self.service,
            start_time=start,
            end_time=start + timedelta(minutes=60),
            status=Appointment.Status.SCHEDULED,
        )

        token = make_appointment_action_token(appointment_id=appt.id, action="cancel")
        url_path = f"/a/{token}/cancel/"
        resp = self.client.get(url_path)
        self.assertEqual(resp.status_code, 200)

        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.CANCELLED_CLIENT)



class AutoReminderCreationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="u3", password="pass12345")
        self.client.login(username="u3", password="pass12345")
        self.client.get(reverse("dashboard"))
        self.biz = Business.objects.get(owner=self.user)
        self.c = Client.objects.create(business=self.biz, full_name="Client B", phone_number="0500000001")
        self.s = Service.objects.create(business=self.biz, name="Service X", duration_minutes=60)

    def test_auto_reminders_created_on_appointment_create(self):
        now = timezone.now()
        start = now + timedelta(days=3)
        appt = Appointment.objects.create(
            business=self.biz,
            client=self.c,
            service=self.s,
            start_time=start,
            end_time=start + timedelta(minutes=60),
            status=Appointment.Status.SCHEDULED,
        )
        self.assertEqual(appt.reminders.count(), 2)
        types = set(appt.reminders.values_list("type", flat=True))
        self.assertIn(Reminder.ReminderType.PRIMARY_24H, types)
        self.assertIn(Reminder.ReminderType.SECONDARY_3H, types)
