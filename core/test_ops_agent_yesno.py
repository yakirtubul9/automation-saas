from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone
from django.contrib.auth import get_user_model

from core.models import Business, BusinessMembership, Room
from core.agents import ops_agent


def _make_payload(*, from_digits: str, text: str, phone_number_id: str = "pnid1") -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "messages": [
                                {"from": from_digits, "type": "text", "text": {"body": text}}
                            ],
                        }
                    }
                ]
            }
        ]
    }


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class OpsAgentYesNoParsingTests(TestCase):
    def setUp(self) -> None:
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", email="o@example.com", password="x")
        self.business = Business.objects.create(owner=self.owner, name="B1", timezone="Asia/Jerusalem")

        # Rooms (names are "1","2"...)
        self.room1 = Room.objects.create(business=self.business, name="1")
        self.room2 = Room.objects.create(business=self.business, name="2")

        # Membership-based auth
        self.ops_sender = "972501112233"
        BusinessMembership.objects.create(
            business=self.business,
            user=self.owner,
            role=BusinessMembership.Role.OWNER,
            whatsapp_number=self.ops_sender,
        )

        # Make sure env whitelist isn't required for this test
        os.environ.pop("OPS_SENDER_WHITELIST", None)
        os.environ.pop("WHATSAPP_OPS_SENDER_WHITELIST", None)

    def test_confirm_accepts_embedded_yes_token(self) -> None:
        # Ask to close room -> should ask for confirm
        out1 = ops_agent.handle_whatsapp_webhook_payload(
            _make_payload(from_digits=self.ops_sender, text="סגור חדר 2 מחר 10:00-12:00 סיבה: תחזוקה")
        )
        self.assertIn("לאשר", out1.body)

        # Confirm with embedded prefix (this was the bug)
        out2 = ops_agent.handle_whatsapp_webhook_payload(
            _make_payload(from_digits=self.ops_sender, text="בעלים: כן")
        )
        self.assertIn("אושר", out2.body)
