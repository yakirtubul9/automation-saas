from __future__ import annotations

from django.contrib.auth.models import User
from django.db import models


class Business(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="businesses")
    name = models.CharField(max_length=200)
    timezone = models.CharField(max_length=64, default="Asia/Jerusalem")
    # 0 = allow automatic client cancellation anytime
    auto_cancel_cutoff_hours = models.PositiveIntegerField(default=0)

    # Ops / clinic WhatsApp number (Agent 2: Owner/Staff)
    # Use WhatsApp Cloud API phone_number_id for reliable webhook routing.
    ops_whatsapp_display_number = models.CharField(max_length=50, blank=True, default="")
    ops_whatsapp_phone_number_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name


class Specialty(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="specialties")
    name = models.CharField(max_length=120)

    class Meta:
        unique_together = ("business", "name")

    def __str__(self) -> str:
        return f"{self.name} ({self.business.name})"


class Room(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="rooms")
    name = models.CharField(max_length=120)
    specialties = models.ManyToManyField(Specialty, related_name="rooms", blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("business", "name")

    def __str__(self) -> str:
        return f"{self.name} ({self.business.name})"


class RoomBlock(models.Model):
    """Hard block on a room's availability (maintenance, closure, etc.).

    Blocks are treated as conflicts for any slot/appointment creation.
    """

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="room_blocks")
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="blocks")
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_room_blocks",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["business", "room", "start_time", "end_time"]),
        ]

    def __str__(self) -> str:
        return f"Block {self.room.name} {self.start_time:%Y-%m-%d %H:%M}-{self.end_time:%H:%M}"


class Provider(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="providers")
    display_name = models.CharField(max_length=200)
    whatsapp_number = models.CharField(max_length=50, blank=True, default="")
    whatsapp_phone_number_id = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="WhatsApp Cloud API phone_number_id for reliable routing",
    )
    specialty = models.ForeignKey(Specialty, on_delete=models.SET_NULL, null=True, blank=True, related_name="providers")
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.display_name} ({self.business.name})"


class BusinessMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        STAFF = "staff", "Staff"
        PROVIDER = "provider", "Provider"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="business_memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STAFF)

    # Optional contact channel for Owner/Staff (WhatsApp-first ops)
    whatsapp_number = models.CharField(max_length=50, blank=True, default="")

    # Notification prefs (Stage 6)
    receive_weekly_report = models.BooleanField(default=True)
    receive_alerts = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("business", "user", "role")

    def __str__(self) -> str:
        return f"{self.user.username} -> {self.business.name} ({self.role})"


class Client(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="clients")
    full_name = models.CharField(max_length=200)
    phone_number = models.CharField(max_length=50)
    email = models.EmailField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.full_name} ({self.business.name})"


class ClientOnboarding(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        NEEDS_INFO = "needs_info", "Needs info"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="client_onboardings")
    phone_number = models.CharField(max_length=50)
    full_name = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Onboarding {self.phone_number} ({self.status})"


class Service(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="services")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    duration_minutes = models.PositiveIntegerField(default=60)
    # Optional mapping so room suitability can be enforced per domain/field.
    specialty = models.ForeignKey(
        Specialty,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="services",
    )
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.business.name})"


class Appointment(models.Model):
    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        SCHEDULED = "scheduled", "מתוכנן"
        CONFIRMED = "confirmed_by_client", 'אושר ע"י לקוח'
        CANCELLATION_REQUESTED = "cancellation_requested", "בקשת ביטול (ממתין לצוות)"
        CANCELLED_CLIENT = "cancelled_by_client", 'בוטל ע"י לקוח'
        CANCELLED_STAFF = "cancelled_by_staff", 'בוטל ע"י צוות'
        COMPLETED = "completed", "הושלם"
        NO_SHOW = "no_show", "לא הגיע"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="appointments")

    # Slot model: an appointment may exist without a client (reserved time).
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="appointments")

    provider = models.ForeignKey(Provider, on_delete=models.SET_NULL, null=True, blank=True, related_name="appointments")
    room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True, related_name="appointments")

    service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True, blank=True)

    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.SCHEDULED)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        who = self.client.full_name if self.client_id else (self.provider.display_name if self.provider_id else "Slot")
        return f"{who} @ {self.start_time:%Y-%m-%d %H:%M} ({self.business.name})"

    def save(self, *args, **kwargs):
        """Persist appointment and ensure default reminders exist on first save.

        This is a safety net (e.g., if signals are not loaded for any reason).
        Reminders are only created for appointments that actually have a client.
        """
        is_new = self.pk is None
        prev_status = None
        if not is_new:
            prev_status = (
                Appointment.objects.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )

        super().save(*args, **kwargs)

        # Create default reminders once (only for appointments that have a client)
        if (
            is_new
            and self.client_id is not None
            and self.status
            not in (
                Appointment.Status.CANCELLED_CLIENT,
                Appointment.Status.CANCELLED_STAFF,
                Appointment.Status.COMPLETED,
                Appointment.Status.NO_SHOW,
            )
        ):
            from .reminders import ensure_reminders_for_appointment

            ensure_reminders_for_appointment(self)

        # On transition to terminal/cancelled statuses, skip pending reminders
        if prev_status is not None and prev_status != self.status:
            if self.status in (
                Appointment.Status.CANCELLED_CLIENT,
                Appointment.Status.CANCELLED_STAFF,
                Appointment.Status.COMPLETED,
                Appointment.Status.NO_SHOW,
            ):
                from .reminders import skip_pending_reminders

                skip_pending_reminders(self)


        # Stage 5 (Recall): when an appointment is marked completed, create a RecallTarget (best-effort).
        if prev_status is not None and prev_status != self.status and self.status == Appointment.Status.COMPLETED:
            try:
                from .recall import ensure_recall_target_for_completed_appointment

                ensure_recall_target_for_completed_appointment(self)
            except Exception:
                pass


class CancellationRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name="cancellation_requests")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"CancellationRequest({self.appointment_id}) {self.status}"


class AppointmentChangeProposal(models.Model):
    """A clinic-initiated change proposal that requires provider approval.

    MVP scope:
      - Proposal created by Staff/Owner due to a clinic constraint (e.g. room malfunction)
      - Provider approves/rejects via public links
      - On approval, we re-validate conflicts and apply the move atomically

    Notes:
      - We snapshot the original appointment fields to prevent stale approvals.
      - The public link itself is the authorization (signed token).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="change_proposals")
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name="change_proposals")

    # Snapshot of the appointment at proposal creation (for stale-proposal protection)
    original_room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True, related_name="original_change_proposals")
    original_start_time = models.DateTimeField()
    original_end_time = models.DateTimeField()

    # Proposed new values
    proposed_room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True, related_name="proposed_change_proposals")
    proposed_start_time = models.DateTimeField()
    proposed_end_time = models.DateTimeField()

    reason = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    expires_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_change_proposals")

    # Notification bookkeeping (MVP: mostly for debugging/ops)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_message_id = models.CharField(max_length=120, blank=True, default="")
    send_error = models.TextField(blank=True, default="")

    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=120, blank=True, default="")

    # Ops / troubleshooting (persist approval failures so staff can act quickly)
    last_error_code = models.CharField(max_length=60, blank=True, default="")
    last_error_message = models.TextField(blank=True, default="")
    last_error_payload = models.JSONField(null=True, blank=True)
    last_attempted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["business", "status", "created_at"]),
            models.Index(fields=["business", "expires_at"]),
        ]

    def __str__(self) -> str:
        return f"ChangeProposal({self.appointment_id}) {self.status}"


class WaitlistEntry(models.Model):
    """A client's request to be notified when a suitable slot becomes available."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        FULFILLED = "fulfilled", "Fulfilled"
        CANCELLED = "cancelled", "Cancelled"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="waitlist_entries")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="waitlist_entries")
    provider = models.ForeignKey(Provider, on_delete=models.SET_NULL, null=True, blank=True, related_name="waitlist_entries")
    service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True, blank=True, related_name="waitlist_entries")

    # Optional preference filters
    preferred_weekdays = models.JSONField(default=list, blank=True)  # list[int] (Mon=0..Sun=6)
    time_window_start = models.TimeField(null=True, blank=True)
    time_window_end = models.TimeField(null=True, blank=True)
    min_notice_hours = models.PositiveIntegerField(default=0)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_waitlist_entries")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["business", "status", "created_at"]),
            models.Index(fields=["business", "provider", "service"]),
        ]

    def __str__(self) -> str:
        return f"WaitlistEntry({self.client_id}) {self.status}"


class WaitlistOffer(models.Model):
    """An offer sent to a waitlist entry for a specific available slot."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="waitlist_offers")
    entry = models.ForeignKey(WaitlistEntry, on_delete=models.CASCADE, related_name="offers")
    slot = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name="waitlist_offers")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField(null=True, blank=True)

    sent_at = models.DateTimeField(null=True, blank=True)
    sent_message_id = models.CharField(max_length=120, blank=True, default="")
    send_error = models.TextField(blank=True, default="")

    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=120, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("entry", "slot")
        indexes = [
            models.Index(fields=["business", "status", "created_at"]),
            models.Index(fields=["business", "expires_at"]),
            models.Index(fields=["slot", "status"]),
        ]

    def __str__(self) -> str:
        return f"WaitlistOffer({self.entry_id}->{self.slot_id}) {self.status}"


class RecallProtocol(models.Model):
    """Recall protocol per (business, service).

    interval_days: how many days after a completed appointment we should initiate recall.
    """

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="recall_protocols")
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="recall_protocols")

    interval_days = models.PositiveIntegerField(default=90)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("business", "service")

    def __str__(self) -> str:
        return f"RecallProtocol({self.business_id},{self.service_id}) {self.interval_days}d"


class RecallTarget(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        OFFERED = "offered", "Offered"
        BOOKED = "booked", "Booked"
        CANCELLED = "cancelled", "Cancelled"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="recall_targets")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="recall_targets")
    provider = models.ForeignKey(Provider, on_delete=models.SET_NULL, null=True, blank=True, related_name="recall_targets")
    service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True, blank=True, related_name="recall_targets")

    source_appointment = models.ForeignKey(
        Appointment,
        on_delete=models.CASCADE,
        related_name="recall_targets",
        help_text="The completed appointment that created this recall target.",
    )

    due_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    last_notified_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    booked_appointment = models.ForeignKey(
        Appointment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recall_bookings",
        help_text="If the client already booked (or accepted an offer), this points to the booked appointment.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("business", "source_appointment")
        indexes = [
            models.Index(fields=["business", "status", "due_at"]),
            models.Index(fields=["business", "client", "service"]),
        ]

    def __str__(self) -> str:
        return f"RecallTarget({self.client_id}) {self.status} due={self.due_at:%Y-%m-%d}"


class RecallOffer(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="recall_offers")
    target = models.ForeignKey(RecallTarget, on_delete=models.CASCADE, related_name="offers")
    slot = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name="recall_offers")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField(null=True, blank=True)

    sent_at = models.DateTimeField(null=True, blank=True)
    sent_message_id = models.CharField(max_length=120, blank=True, default="")
    send_error = models.TextField(blank=True, default="")

    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=120, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("target", "slot")
        indexes = [
            models.Index(fields=["business", "status", "created_at"]),
            models.Index(fields=["business", "expires_at"]),
            models.Index(fields=["slot", "status"]),
        ]

    def __str__(self) -> str:
        return f"RecallOffer({self.target_id}->{self.slot_id}) {self.status}"


class AuditEvent(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="audit_events")
    actor_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_events")
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100, blank=True, default="")
    object_id = models.CharField(max_length=100, blank=True, default="")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action}"


class WeeklyReportLog(models.Model):
    """Stage 6: Stores weekly report runs (for debugging/ops).

    We keep the report payload and send results to diagnose delivery issues.
    """

    class Status(models.TextChoices):
        DRY_RUN = "dry_run", "Dry run"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="weekly_report_logs")
    week_start = models.DateField()
    week_end = models.DateField()
    payload = models.JSONField(default=dict, blank=True)
    channels = models.JSONField(default=list, blank=True)  # e.g. ["whatsapp", "email"]
    recipients = models.JSONField(default=list, blank=True)  # list of dicts
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRY_RUN)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["business", "week_start", "status", "created_at"])]

    def __str__(self) -> str:
        return f"WeeklyReportLog({self.business_id}) {self.week_start}..{self.week_end} {self.status}"


class WhatsAppMessage(models.Model):
    """Stores inbound/outbound WhatsApp messages for debugging and audit.

    Stage 8: Conversational client agent relies on these logs
    to support troubleshooting and basic idempotency.
    """

    class Direction(models.TextChoices):
        INBOUND = "in", "Inbound"
        OUTBOUND = "out", "Outbound"

    class Purpose(models.TextChoices):
        REMINDER = "reminder", "Reminder"
        CLIENT_AGENT = "client_agent", "Client agent"
        OPS_AGENT = "ops_agent", "Ops agent"
        OTHER = "other", "Other"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="whatsapp_messages")

    # Best-effort linkage (not always known at ingest time)
    provider = models.ForeignKey(Provider, on_delete=models.SET_NULL, null=True, blank=True, related_name="whatsapp_messages")
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="whatsapp_messages")

    direction = models.CharField(max_length=8, choices=Direction.choices)
    purpose = models.CharField(max_length=20, choices=Purpose.choices, default=Purpose.OTHER)

    wa_message_id = models.CharField(max_length=200, blank=True, default="")
    from_number = models.CharField(max_length=50, blank=True, default="")
    to_number = models.CharField(max_length=50, blank=True, default="")
    body = models.TextField(blank=True, default="")
    raw_payload = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["business", "created_at"]),
            models.Index(fields=["business", "direction", "created_at"]),
            models.Index(fields=["wa_message_id"]),
        ]

    def __str__(self) -> str:
        return f"WA {self.direction} {self.purpose} {self.created_at:%Y-%m-%d %H:%M}"


class ConversationSession(models.Model):
    """Short-lived state machine for WhatsApp conversations.

    We keep state minimal and expire sessions aggressively so that
    the agent doesn't get stuck.
    """

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="conversation_sessions")
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="conversation_sessions")

    # WhatsApp sender number (patient)
    wa_from_number = models.CharField(max_length=50)

    state = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("business", "provider", "wa_from_number")
        indexes = [
            models.Index(fields=["business", "provider", "wa_from_number"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self) -> str:
        return f"Session({self.provider_id} {self.wa_from_number})"


class OpsConversationSession(models.Model):
    """Short-lived state for Ops WhatsApp agent (Owner/Staff).

    Kept separate from ConversationSession to avoid coupling to Provider/patient flows.
    """

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="ops_conversation_sessions")
    membership = models.ForeignKey(
        BusinessMembership,
        on_delete=models.CASCADE,
        related_name="ops_conversation_sessions",
        help_text="Owner/Staff membership that initiated the conversation",
    )
    wa_from_number = models.CharField(max_length=50)
    state = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("business", "wa_from_number")

    def __str__(self) -> str:
        return f"OpsSession({self.business_id}, {self.wa_from_number})"



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
    status = models.CharField(max_length=16, choices=ReminderStatus.choices, default=ReminderStatus.PENDING)
    type = models.CharField(max_length=32, choices=ReminderType.choices, default=ReminderType.PRIMARY_24H)
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.WHATSAPP)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Reminder {self.type} for {self.appointment}"