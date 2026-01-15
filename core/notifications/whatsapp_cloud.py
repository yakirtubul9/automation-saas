from __future__ import annotations

import os
import re
from typing import Optional

import requests
from django.utils import timezone

from .base import NotificationProvider
from typing import Optional, Sequence


def _normalize_phone(to: str, *, default_country_code: Optional[str] = None) -> str:
    """
    WhatsApp Cloud API expects an international number with country code.
    We'll strip non-digits and (optionally) convert local Israeli "05..." to "9725...".
    """
    digits = re.sub(r"\D+", "", to or "")
    if not digits:
        raise ValueError("Empty phone number")

    # If user stored "+972..." it's now "972..."
    # If stored "05X..." and you set DEFAULT_COUNTRY_CODE=972 -> convert.
    if default_country_code and digits.startswith("0"):
        digits = default_country_code + digits[1:]

    return digits

def _sanitize_template_param(text: str) -> str:
    """
    WhatsApp template params cannot include newline/tab chars or >4 consecutive spaces.
    We'll flatten whitespace safely.
    """
    s = (text or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r" {5,}", "    ", s)   # max 4 spaces in a row
    s = re.sub(r"\s+", " ", s).strip()
    return s

class WhatsAppCloudProvider(NotificationProvider):
    """
    Sends messages via WhatsApp Cloud API (Meta Graph).
    Endpoint pattern: https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages
    :contentReference[oaicite:2]{index=2}
    """

    def __init__(self) -> None:
        self.access_token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
        self.phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        self.graph_version = os.getenv("WHATSAPP_GRAPH_VERSION", "v18.0").strip()
        self.default_country_code = os.getenv("DEFAULT_COUNTRY_CODE", "").strip() or None

        # Optional: if you have an approved template, set it and we’ll use it.
        self.template_name = os.getenv("WHATSAPP_TEMPLATE_NAME", "").strip() or None
        self.template_lang = os.getenv("WHATSAPP_TEMPLATE_LANG", "he").strip()

        if not self.access_token:
            raise ValueError("Missing WHATSAPP_ACCESS_TOKEN")
        if not self.phone_number_id:
            raise ValueError("Missing WHATSAPP_PHONE_NUMBER_ID")

        self.base_url = f"https://graph.facebook.com/{self.graph_version}/{self.phone_number_id}/messages"

    def send(self, *, to: str, body: str, template_params: Optional[Sequence[str]] = None) -> str:
        to_norm = _normalize_phone(to, default_country_code=self.default_country_code)
        print(f"[WA DEBUG] to_raw={to} to_norm={to_norm} from_phone_number_id={self.phone_number_id}")

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        if self.template_name:
            if (self.template_name or "").strip().lower() == "hello_world":
                payload = {
                    "messaging_product": "whatsapp",
                    "to": to_norm,
                    "type": "template",
                    "template": {
                        "name": self.template_name,
                        "language": {"code": self.template_lang},
                    },
                }
            else:
                # אם לא הועברו פרמטרים — נשמור תאימות אחורה ונשלח את body כפרמטר אחד
                params = list(template_params) if template_params is not None else [body]

                payload = {
                    "messaging_product": "whatsapp",
                    "to": to_norm,
                    "type": "template",
                    "template": {
                        "name": self.template_name,
                        "language": {"code": self.template_lang},
                        "components": [
                            {
                                "type": "body",
                                "parameters": [
                                    {"type": "text", "text": _sanitize_template_param(p)[:1024]}
                                    for p in params
                                ],
                            }
                        ],
                    },
                }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": to_norm,
                "type": "text",
                "text": {"body": body[:4096]},
            }

        resp = requests.post(self.base_url, json=payload, headers=headers, timeout=20)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        if resp.status_code >= 400:
            raise RuntimeError(f"WhatsApp send failed ({resp.status_code}): {data}")

        # Successful responses include an id (often a wamid*).
        # :contentReference[oaicite:3]{index=3}
        msg_id = None
        if isinstance(data, dict):
            msgs = data.get("messages") or []
            if msgs and isinstance(msgs, list) and isinstance(msgs[0], dict):
                msg_id = msgs[0].get("id")

        return msg_id or f"whatsapp-{timezone.now().timestamp()}"
