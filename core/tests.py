from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import (
    Appointment,
    AuditEvent,
    Business,
    BusinessMembership,
    Client,
    Provider,
    Reminder,
    Room,
    RoomBlock,
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


class Stage2SlotsApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user_staff = User.objects.create_user(username="staff2", password="pass12345")

        self.biz = Business.objects.create(owner=self.user_staff, name="Clinic 2")
        BusinessMembership.objects.create(
            business=self.biz,
            user=self.user_staff,
            role=BusinessMembership.Role.STAFF,
        )

        self.spec = Specialty.objects.create(business=self.biz, name="Derm")
        self.room1 = Room.objects.create(business=self.biz, name="Room A")
        self.room1.specialties.add(self.spec)
        self.room2 = Room.objects.create(business=self.biz, name="Room B")
        self.room2.specialties.add(self.spec)

        self.provider = Provider.objects.create(
            business=self.biz,
            display_name="Dr Derm",
            whatsapp_number="+972500000001",
            specialty=self.spec,
        )

        self.client.login(username="staff2", password="pass12345")

    def test_reserve_slots_weekly_creates_multiple_reserved_slots(self):
        start = (timezone.now() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)

        resp = self.client.post(
            reverse("reserve_slots"),
            data=json.dumps(
                {
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "provider_id": self.provider.id,
                    "rrule": {"freq": "weekly", "interval": 1, "byweekday": [start.weekday()], "count": 3},
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["created"]), 3)
        self.assertEqual(len(payload["skipped"]), 0)

        appts = Appointment.objects.filter(business=self.biz, provider=self.provider).order_by("start_time")
        self.assertEqual(appts.count(), 3)
        for a in appts:
            self.assertEqual(a.status, Appointment.Status.RESERVED)
            self.assertIsNone(a.client_id)
            # Must be one of the matching rooms
            self.assertIn(a.room_id, {self.room1.id, self.room2.id})

    def test_room_block_prevents_reservation_in_that_room(self):
        # Block room1 for the desired time; the system should pick room2 instead.
        start = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        RoomBlock.objects.create(
            business=self.biz,
            room=self.room1,
            start_time=start,
            end_time=end,
            reason="maintenance",
            created_by=self.user_staff,
            is_active=True,
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
        self.assertEqual(resp.status_code, 200)
        appt_id = resp.json()["appointment"]["id"]
        appt = Appointment.objects.get(pk=appt_id)
        self.assertEqual(appt.room_id, self.room2.id)

    def test_assign_client_rejects_service_with_mismatched_specialty(self):
        # Create a reserved slot
        start = (timezone.now() + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        slot = Appointment.objects.create(
            business=self.biz,
            provider=self.provider,
            room=self.room1,
            client=None,
            service=None,
            start_time=start,
            end_time=end,
            status=Appointment.Status.RESERVED,
        )

        client_obj = Client.objects.create(
            business=self.biz,
            full_name="Patient 1",
            phone_number="+972500000099",
            is_active=True,
        )

        other_spec = Specialty.objects.create(business=self.biz, name="Ortho")
        mismatched_service = Service.objects.create(
            business=self.biz,
            name="Ortho consult",
            duration_minutes=60,
            specialty=other_spec,
            is_active=True,
        )

        resp = self.client.post(
            reverse("assign_client"),
            data=json.dumps(
                {"slot_id": slot.id, "client_id": client_obj.id, "service_id": mismatched_service.id}
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "specialty_mismatch")

    @override_settings(
        BUSINESS_WORKING_HOURS={
            0: [("09:00", "17:00")],
            1: [("09:00", "17:00")],
            2: [("09:00", "17:00")],
            3: [("09:00", "17:00")],
            4: [("09:00", "17:00")],
            5: [("09:00", "17:00")],
            6: [("09:00", "17:00")],
        },
        SLOT_STEP_MINUTES=15,
        ALTERNATIVES_LOOKAHEAD_DAYS=7,
    )
    def test_alternatives_respect_working_hours_and_grid(self):
        """If the requested time is blocked and the remaining window is too short,
        alternatives must roll to the next day's opening window (not spill after close).
        """
        # Choose a start close to the end of the working day (16:00-17:00).
        base = timezone.localtime(timezone.now()) + timedelta(days=1)
        start_local = base.replace(hour=16, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(hours=1)
        start = start_local.astimezone(timezone.get_current_timezone())
        end = end_local.astimezone(timezone.get_current_timezone())

        # Block BOTH matching rooms for the requested time, forcing "no_room_available".
        RoomBlock.objects.create(
            business=self.biz,
            room=self.room1,
            start_time=start,
            end_time=end,
            reason="blocked",
            created_by=self.user_staff,
            is_active=True,
        )
        RoomBlock.objects.create(
            business=self.biz,
            room=self.room2,
            start_time=start,
            end_time=end,
            reason="blocked",
            created_by=self.user_staff,
            is_active=True,
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
        self.assertEqual(payload["error"]["code"], "no_room_available")
        alts = payload["error"].get("alternatives") or []
        self.assertTrue(len(alts) >= 1)

        # First alternative should be on the next day, aligned to the 09:00 opening.
        alt0 = alts[0]
        alt_start = parse_datetime(alt0["start_time"])
        self.assertIsNotNone(alt_start)
        alt_local = timezone.localtime(alt_start)

        self.assertEqual(alt_local.minute % 15, 0)
        self.assertEqual((alt_local.hour, alt_local.minute), (9, 0))





class ChangeProposalStage3Tests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner3", password="pass")
        self.staff = User.objects.create_user(username="staff3", password="pass")

        self.business = Business.objects.create(owner=self.owner, name="Clinic")
        BusinessMembership.objects.create(business=self.business, user=self.staff, role=BusinessMembership.Role.STAFF)

        self.specialty = Specialty.objects.create(business=self.business, name="Ortho")

        self.room1 = Room.objects.create(business=self.business, name="R1", is_active=True)
        self.room1.specialties.add(self.specialty)
        self.room2 = Room.objects.create(business=self.business, name="R2", is_active=True)
        self.room2.specialties.add(self.specialty)

        self.provider = Provider.objects.create(
            business=self.business,
            display_name="Dr A",
            whatsapp_number="+972500000000",
            specialty=self.specialty,
        )

        self.client_obj = Client.objects.create(business=self.business, full_name="Client", phone_number="0500000000")
        self.service = Service.objects.create(business=self.business, name="Consult", duration_minutes=30, specialty=self.specialty)

        from zoneinfo import ZoneInfo
        tz = ZoneInfo(self.business.timezone)
        local_now = timezone.now().astimezone(tz)
        start_local = (local_now + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(minutes=30)
        start = start_local.astimezone(ZoneInfo("UTC"))
        end = end_local.astimezone(ZoneInfo("UTC"))

        self.appt = Appointment.objects.create(
            business=self.business,
            provider=self.provider,
            room=self.room1,
            client=self.client_obj,
            service=self.service,
            start_time=start,
            end_time=end,
            status=Appointment.Status.SCHEDULED,
        )

    def test_staff_can_create_and_provider_can_approve_change_proposal(self):
        # room1 is broken -> block it -> propose moving to room2
        RoomBlock.objects.create(
            business=self.business,
            room=self.room1,
            start_time=self.appt.start_time,
            end_time=self.appt.end_time,
            reason="AC broken",
            is_active=True,
        )

        self.client.login(username="staff3", password="pass")
        resp = self.client.post(
            "/api/change-proposals/",
            data=json.dumps({
                "appointment_id": self.appt.id,
                "proposed_room_id": self.room2.id,
                "reason": "Room 1 maintenance",
                "expires_in_minutes": 60,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        self.assertTrue(payload.get("ok"))
        approve_path = payload["approve"]["path"]

        # Provider approves via public link
        get_resp = self.client.get(approve_path)
        self.assertEqual(get_resp.status_code, 200)

        post_resp = self.client.post(approve_path)
        self.assertEqual(post_resp.status_code, 200, post_resp.content)

        self.appt.refresh_from_db()
        self.assertEqual(self.appt.room_id, self.room2.id)

        self.assertTrue(AuditEvent.objects.filter(action="change_proposal_approved_and_applied").exists())

    def test_provider_can_reject_change_proposal(self):
        self.client.login(username="staff3", password="pass")
        resp = self.client.post(
            "/api/change-proposals/",
            data=json.dumps({
                "appointment_id": self.appt.id,
                "proposed_room_id": self.room2.id,
                "reason": "Try move",
                "expires_in_minutes": 60,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        reject_path = payload["reject"]["path"]

        post_resp = self.client.post(reject_path)
        self.assertEqual(post_resp.status_code, 200)

        self.appt.refresh_from_db()
        self.assertEqual(self.appt.room_id, self.room1.id)

        from .models import AppointmentChangeProposal
        self.assertTrue(AppointmentChangeProposal.objects.filter(status=AppointmentChangeProposal.Status.REJECTED).exists())

    def test_expired_change_proposal_returns_410(self):
        self.client.login(username="staff3", password="pass")
        resp = self.client.post(
            "/api/change-proposals/",
            data=json.dumps({
                "appointment_id": self.appt.id,
                "proposed_room_id": self.room2.id,
                "reason": "Time sensitive",
                "expires_in_minutes": 60,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        approve_path = payload["approve"]["path"]

        from .models import AppointmentChangeProposal
        proposal_id = payload["proposal"]["id"]
        AppointmentChangeProposal.objects.filter(pk=proposal_id).update(expires_at=timezone.now() - timedelta(minutes=1))

        r = self.client.post(approve_path)
        self.assertEqual(r.status_code, 410)

    def test_staff_can_list_cancel_and_resend_change_proposals(self):
        # create
        self.client.login(username="staff3", password="pass")
        resp = self.client.post(
            "/api/change-proposals/",
            data=json.dumps({
                "appointment_id": self.appt.id,
                "proposed_room_id": self.room2.id,
                "reason": "Move",
                "expires_in_minutes": 60,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        proposal_id = payload["proposal"]["id"]

        # list (GET) - should return the proposal
        list_resp = self.client.get("/api/change-proposals/?status=pending")
        self.assertEqual(list_resp.status_code, 200, list_resp.content)
        data = list_resp.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(any(it["id"] == proposal_id for it in data.get("items", [])))

        # resend
        resend_resp = self.client.post(f"/api/change-proposals/{proposal_id}/resend/")
        self.assertEqual(resend_resp.status_code, 200, resend_resp.content)
        from .models import AppointmentChangeProposal
        p = AppointmentChangeProposal.objects.get(pk=proposal_id)
        self.assertIsNotNone(p.sent_at)
        self.assertTrue(p.sent_message_id.startswith("mock-"))

        # cancel
        cancel_resp = self.client.post(f"/api/change-proposals/{proposal_id}/cancel/")
        self.assertEqual(cancel_resp.status_code, 200, cancel_resp.content)
        p.refresh_from_db()
        self.assertEqual(p.status, AppointmentChangeProposal.Status.CANCELLED)

        # approve should now fail (already handled)
        approve_path = payload["approve"]["path"]
        r = self.client.post(approve_path)
        self.assertEqual(r.status_code, 409)

    def test_approve_failure_persists_last_error_for_ops(self):
        # create proposal to move to room2
        self.client.login(username="staff3", password="pass")
        resp = self.client.post(
            "/api/change-proposals/",
            data=json.dumps({
                "appointment_id": self.appt.id,
                "proposed_room_id": self.room2.id,
                "reason": "Move",
                "expires_in_minutes": 60,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        proposal_id = payload["proposal"]["id"]

        # create a conflicting appointment in room2 at the same time AFTER proposal creation
        Appointment.objects.create(
            business=self.business,
            provider=self.provider,
            room=self.room2,
            client=self.client_obj,
            service=self.service,
            start_time=self.appt.start_time,
            end_time=self.appt.end_time,
            status=Appointment.Status.SCHEDULED,
        )

        approve_path = payload["approve"]["path"]
        r = self.client.post(approve_path)
        self.assertEqual(r.status_code, 409)

        from .models import AppointmentChangeProposal
        p = AppointmentChangeProposal.objects.get(pk=proposal_id)
        self.assertEqual(p.status, AppointmentChangeProposal.Status.PENDING)
        self.assertTrue(p.last_error_code)
        self.assertTrue(p.last_error_message)
        self.assertIsNotNone(p.last_attempted_at)
