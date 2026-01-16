from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Appointment,
    Business,
    BusinessMembership,
    Client,
    Provider,
    Reminder,
    Room,
    Service,
    Specialty,
)
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

        # Create a reserved slot (client=None) to avoid auto-reminder creation side effects.
        # The dashboard's "today_appointments" counts all appointments regardless of client.
        c = Client.objects.create(business=biz, full_name="Client A", phone_number="0500000000")
        s = Service.objects.create(business=biz, name="Service 1", duration_minutes=60)

        now = timezone.now()
        # Ensure appt_today stays on the same calendar date as `today` in the dashboard,
        # regardless of the test runtime hour.
        local_now = timezone.localtime(now)
        if local_now.hour == 0:
            start_today = now + timedelta(hours=1)
        else:
            start_today = now - timedelta(hours=1)

        appt_today = Appointment.objects.create(
            business=biz,
            client=None,
            service=None,
            start_time=start_today,
            end_time=start_today + timedelta(hours=1),
            status=Appointment.Status.RESERVED,
        )
        Appointment.objects.create(
            business=biz,
            client=None,
            service=None,
            start_time=now + timedelta(days=2),
            end_time=now + timedelta(days=2, hours=1),
            status=Appointment.Status.RESERVED,
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


class ReserveSlotApiTests(TestCase):
    def _create_provider(self, **overrides):
        from core.models import Provider
        field_names = {f.name for f in Provider._meta.fields}

        kwargs = {}

        # הכי נפוץ: business
        if "business" in field_names and hasattr(self, "business"):
            kwargs["business"] = self.business

        # שם/כינוי
        if "full_name" in field_names:
            kwargs["full_name"] = "Dr Test"
        elif "name" in field_names:
            kwargs["name"] = "Dr Test"
        elif "display_name" in field_names:
            kwargs["display_name"] = "Dr Test"

        # קישור למשתמש אם קיים
        if "user" in field_names and hasattr(self, "user"):
            kwargs["user"] = self.user

        # מספר וואטסאפ אם קיים
        if "whatsapp_number" in field_names:
            kwargs["whatsapp_number"] = "+972500000000"

        # specialty אם קיים
        if "specialty" in field_names and hasattr(self, "specialty"):
            kwargs["specialty"] = self.specialty

        # דריסות ספציפיות אם תרצה
        kwargs.update(overrides)

        return Provider.objects.create(**kwargs)

    def setUp(self):
        User = get_user_model()
        self.user_provider = User.objects.create_user(username="prov", password="pass12345")
        self.user_staff = User.objects.create_user(username="staff", password="pass12345")

        self.biz = Business.objects.create(owner=self.user_staff, name="Clinic")
        # Give staff a membership in the same business
        BusinessMembership.objects.create(
            business=self.biz,
            user=self.user_staff,
            role=BusinessMembership.Role.STAFF,
        )

        self.spec = Specialty.objects.create(business=self.biz, name="Physio")
        self.room = Room.objects.create(business=self.biz, name="Room 1")
        self.room.specialties.add(self.spec)

        # Make helper-aware defaults available
        self.business = self.biz
        self.specialty = self.spec
        # Create a provider that matches the room specialty and belongs to the same business
        self.provider = self._create_provider(business=self.biz, specialty=self.spec)

    def test_provider_can_reserve_slot_for_self(self):
        # In the current MVP RBAC, reserving slots is done by Staff/Owner for a provider
        # unless Provider is explicitly linked to request.user.
        self.client.login(username="staff", password="pass12345")

        start = (timezone.now() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=2)

        resp = self.client.post(
            reverse("reserve_slot"),
            data=json.dumps(
                {
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "provider_id": self.provider.id,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["ok"])

        appt_id = payload["appointment"]["id"]
        appt = Appointment.objects.get(pk=appt_id)
        self.assertEqual(appt.status, Appointment.Status.RESERVED)
        self.assertIsNone(appt.client_id)
        self.assertEqual(appt.provider_id, self.provider.id)
        self.assertEqual(appt.room_id, self.room.id)

    def test_conflicting_reservation_is_rejected(self):
        self.client.login(username="staff", password="pass12345")
        start = (timezone.now() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=2)

        Appointment.objects.create(
            business=self.biz,
            provider=self.provider,
            room=self.room,
            client=None,
            service=None,
            start_time=start,
            end_time=end,
            status=Appointment.Status.RESERVED,
        )

        resp = self.client.post(
            reverse("reserve_slot"),
            data=json.dumps(
                {
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "provider_id": self.provider.id,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 409)
        payload = resp.json()
        self.assertFalse(payload["ok"])


class AvailabilityAndAssignClientApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user_staff = User.objects.create_user(username="staff2", password="pass12345")

        self.biz = Business.objects.create(owner=self.user_staff, name="Clinic2")
        BusinessMembership.objects.create(
            business=self.biz,
            user=self.user_staff,
            role=BusinessMembership.Role.STAFF,
        )

        self.spec = Specialty.objects.create(business=self.biz, name="Ortho")
        self.room = Room.objects.create(business=self.biz, name="Room A")
        self.room.specialties.add(self.spec)

        self.provider = Provider.objects.create(business=self.biz, display_name="Dr A", specialty=self.spec)

        self.client_obj = Client.objects.create(
            business=self.biz,
            full_name="Client Z",
            phone_number="0500000009",
        )
        self.service = Service.objects.create(business=self.biz, name="Consult", duration_minutes=60)

        self.client.login(username="staff2", password="pass12345")

    def test_availability_returns_upcoming_reserved_slots(self):
        now = timezone.now().replace(second=0, microsecond=0)

        # Create 4 reserved slots; API should return first 3 by default.
        slots = []
        for i in range(4):
            start = now + timedelta(days=1, hours=i)
            end = start + timedelta(hours=1)
            appt = Appointment.objects.create(
                business=self.biz,
                provider=self.provider,
                room=self.room,
                client=None,
                service=None,
                start_time=start,
                end_time=end,
                status=Appointment.Status.RESERVED,
            )
            slots.append(appt)

        resp = self.client.get(
            reverse("availability"),
            {"provider_id": self.provider.id},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider_id"], self.provider.id)
        self.assertEqual(len(payload["slots"]), 3)

        returned_ids = [s["slot_id"] for s in payload["slots"]]
        expected_ids = [slots[0].id, slots[1].id, slots[2].id]
        self.assertEqual(returned_ids, expected_ids)

        # Explicit limit
        resp2 = self.client.get(
            reverse("availability"),
            {"provider_id": self.provider.id, "limit": 2},
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(len(resp2.json()["slots"]), 2)

    def test_assign_client_to_slot_sets_client_and_creates_reminders(self):
        start = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        slot = Appointment.objects.create(
            business=self.biz,
            provider=self.provider,
            room=self.room,
            client=None,
            service=None,
            start_time=start,
            end_time=end,
            status=Appointment.Status.RESERVED,
        )

        resp = self.client.post(
            reverse("assign_client"),
            data=json.dumps(
                {"slot_id": slot.id, "client_id": self.client_obj.id, "service_id": self.service.id}
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["ok"])

        slot.refresh_from_db()
        self.assertEqual(slot.client_id, self.client_obj.id)
        self.assertEqual(slot.service_id, self.service.id)
        self.assertEqual(slot.status, Appointment.Status.SCHEDULED)

        # Reminders are created explicitly when assigning (existing slot update).
        self.assertEqual(Reminder.objects.filter(appointment=slot).count(), 2)

        # Second assignment should fail
        resp2 = self.client.post(
            reverse("assign_client"),
            data=json.dumps({"slot_id": slot.id, "client_id": self.client_obj.id}),
            content_type="application/json",
        )
        self.assertEqual(resp2.status_code, 409)
