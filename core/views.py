# core/views.py
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils import timezone

from django.conf import settings
from django.core import signing
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseNotFound
from django.middleware.csrf import get_token

from .models import Business, Appointment, Reminder, CancellationRequest, AppointmentChangeProposal


def _get_or_create_business_for_user(user) -> Business:
    # לוקחים את הראשון אם יש כמה, אחרת יוצרים אחד בסיסי
    business = Business.objects.filter(owner=user).order_by("id").first()
    if business:
        return business
    return Business.objects.create(owner=user, name=f"{user.username} Business")


@login_required
def dashboard(request):
    # מקור אמת יחיד: הפונקציה הזו
    business = _get_or_create_business_for_user(request.user)

    now = timezone.now()
    today = now.date()

    upcoming_appointments = (
        Appointment.objects.filter(business=business, start_time__gte=now)
        .order_by("start_time")[:10]
    )

    today_appointments = Appointment.objects.filter(
        business=business,
        start_time__date=today,
    ).count()

    reminders_pending = Reminder.objects.filter(
        appointment__business=business,
        status=Reminder.ReminderStatus.PENDING,
    ).count()

    reminders_sent_today = Reminder.objects.filter(
        appointment__business=business,
        status=Reminder.ReminderStatus.SENT,
        sent_at__date=today,
    ).count()

    context = {
        "business": business,
        "upcoming_appointments": upcoming_appointments,
        "today_appointments": today_appointments,
        "reminders_pending": reminders_pending,
        "reminders_sent_today": reminders_sent_today,
    }

    return render(request, "Core/dashboard.html", context)


@login_required
def settings_view(request):
    business = _get_or_create_business_for_user(request.user)
    return render(request, "Core/settings.html", {"business": business})


def logout_view(request):
    logout(request)
    return redirect("login")



# --- Public confirm/cancel links (MVP) ---

_APPT_ACTION_SALT = "core.appointment.action"


def make_appointment_action_token(*, appointment_id: int, action: str) -> str:
    """Create a signed, time-limited token for a public appointment action."""
    payload = {"aid": int(appointment_id), "act": str(action)}
    return signing.dumps(payload, salt=_APPT_ACTION_SALT, compress=True)


def build_public_action_url(*, token: str, action: str) -> str:
    base = getattr(settings, "SITE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    return f"{base}/a/{token}/{action}/"


def appointment_action_view(request, token: str, action: str):
    """Public endpoint to confirm/cancel an appointment using a signed token.

    Security model (MVP): the signed token is the authorization.
    """
    action = (action or "").strip().lower()
    if action not in {"confirm", "cancel"}:
        return HttpResponseBadRequest("Invalid action")

    max_age = int(getattr(settings, "APPOINTMENT_ACTION_LINK_MAX_AGE_SECONDS", 14 * 24 * 60 * 60))

    try:
        data = signing.loads(token, salt=_APPT_ACTION_SALT, max_age=max_age)
    except signing.SignatureExpired:
        return HttpResponse("הקישור פג תוקף.", status=410)
    except signing.BadSignature:
        return HttpResponseBadRequest("קישור לא תקין.")

    if data.get("act") != action:
        return HttpResponseBadRequest("קישור לא תואם לפעולה.")

    appointment_id = data.get("aid")
    try:
        appt = Appointment.objects.select_related("client", "business").get(pk=appointment_id)
    except Appointment.DoesNotExist:
        return HttpResponseNotFound("התור לא נמצא.")

    # Apply action idempotently
    if action == "confirm":
        if appt.status in {Appointment.Status.CANCELLED_CLIENT, Appointment.Status.CANCELLED_STAFF}:
            return HttpResponse("התור כבר בוטל.", status=409)
        appt.status = Appointment.Status.CONFIRMED
        appt.save(update_fields=["status"])
        return HttpResponse("✅ התור אושר בהצלחה. תודה!", content_type="text/html; charset=utf-8")

    # cancel
    if appt.status in {Appointment.Status.CANCELLED_CLIENT, Appointment.Status.CANCELLED_STAFF}:
        return HttpResponse("התור כבר בוטל.", status=200)

    # Business policy: if auto_cancel_cutoff_hours > 0 and we're inside the cutoff window,
    # do not auto-cancel (create a cancellation request instead).
    cutoff_hours = int(getattr(appt.business, "auto_cancel_cutoff_hours", 0) or 0)
    now = timezone.now()
    hours_until = (appt.start_time - now).total_seconds() / 3600.0

    if cutoff_hours > 0 and hours_until < cutoff_hours:
        # request cancellation approval
        CancellationRequest.objects.create(appointment=appt)
        appt.status = Appointment.Status.CANCELLATION_REQUESTED
        appt.save(update_fields=["status"])
        return HttpResponse(
            "🕒 בקשת הביטול נשלחה לצוות לאישור. תקבל/י עדכון בהקדם.",
            content_type="text/html; charset=utf-8",
        )

    # Auto cancel (anytime when cutoff_hours==0, or outside the cutoff window)
    appt.status = Appointment.Status.CANCELLED_CLIENT
    appt.save(update_fields=["status"])
    # Pending reminders will be skipped by Appointment.save() transition.

    # Stage 4 (Waitlist): create a new available slot and try to fill it.
    # We only do this on the first successful cancellation (the branch above is idempotent).
    try:
        existing = Appointment.objects.filter(
            business=appt.business,
            provider_id=appt.provider_id,
            room_id=appt.room_id,
            start_time=appt.start_time,
            end_time=appt.end_time,
            status=Appointment.Status.RESERVED,
            client__isnull=True,
        ).first()
        if existing is None:
            freed_slot = Appointment.objects.create(
                business=appt.business,
                client=None,
                provider=appt.provider,
                room=appt.room,
                service=appt.service,
                start_time=appt.start_time,
                end_time=appt.end_time,
                status=Appointment.Status.RESERVED,
            )
            from .waitlist import create_offers_for_slot

            create_offers_for_slot(slot=freed_slot)
    except Exception:
        # Waitlist is best-effort; cancellation must always succeed.
        pass

    return HttpResponse("✅ התור בוטל בהצלחה.", content_type="text/html; charset=utf-8")


# --- Public clinic-change approval links (Stage 3) ---

_PROPOSAL_ACTION_SALT = "core.change_proposal.action"


def make_change_proposal_action_token(*, proposal_id: int, action: str) -> str:
    """Create a signed, time-limited token for a public change-proposal action."""
    payload = {"pid": int(proposal_id), "act": str(action)}
    return signing.dumps(payload, salt=_PROPOSAL_ACTION_SALT, compress=True)


def build_public_proposal_action_url(*, token: str, action: str) -> str:
    base = getattr(settings, "SITE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    return f"{base}/p/{token}/{action}/"


def change_proposal_action_view(request, token: str, action: str):
    """Public endpoint to approve/reject a clinic change proposal.

    Security model (MVP): the signed token is the authorization.

    GET  -> show a minimal confirmation page
    POST -> perform the action if still valid
    """
    action = (action or "").strip().lower()
    if action not in {"approve", "reject"}:
        return HttpResponseBadRequest("Invalid action")

    max_age = int(getattr(settings, "CHANGE_PROPOSAL_ACTION_LINK_MAX_AGE_SECONDS", 7 * 24 * 60 * 60))

    try:
        data = signing.loads(token, salt=_PROPOSAL_ACTION_SALT, max_age=max_age)
    except signing.SignatureExpired:
        return HttpResponse("הקישור פג תוקף.", status=410)
    except signing.BadSignature:
        return HttpResponseBadRequest("קישור לא תקין.")

    if data.get("act") != action:
        return HttpResponseBadRequest("קישור לא תואם לפעולה.")

    proposal_id = data.get("pid")
    try:
        proposal = (
            AppointmentChangeProposal.objects
            .select_related("appointment", "appointment__provider", "appointment__room", "proposed_room")
            .get(pk=proposal_id)
        )
    except AppointmentChangeProposal.DoesNotExist:
        return HttpResponseNotFound("הבקשה לא נמצאה.")

    # Expiry/status gate
    now = timezone.now()
    if proposal.status != AppointmentChangeProposal.Status.PENDING:
        return HttpResponse("הבקשה כבר טופלה.", status=409)

    if proposal.expires_at and proposal.expires_at <= now:
        proposal.status = AppointmentChangeProposal.Status.EXPIRED
        proposal.decided_at = now
        proposal.decision_note = "expired"
        proposal.save(update_fields=["status", "decided_at", "decision_note"])
        return HttpResponse("הבקשה פג תוקף.", status=410)

    appt = proposal.appointment

    # Render confirmation page
    if request.method == "GET":
        provider_name = appt.provider.display_name if appt.provider_id else ""
        orig_room = proposal.original_room.name if proposal.original_room_id else "-"
        new_room = proposal.proposed_room.name if proposal.proposed_room_id else "-"
        html = f"""
        <html><body style='font-family: Arial, sans-serif; direction: rtl;'>
          <h2>בקשת שינוי תור</h2>
          <p><b>רופא:</b> {provider_name}</p>
          <p><b>מ:</b> {proposal.original_start_time:%Y-%m-%d %H:%M} עד {proposal.original_end_time:%H:%M} (חדר: {orig_room})</p>
          <p><b>ל:</b> {proposal.proposed_start_time:%Y-%m-%d %H:%M} עד {proposal.proposed_end_time:%H:%M} (חדר: {new_room})</p>
          <p><b>סיבה:</b> {proposal.reason or '-'} </p>
          <form method='post'>
            <button type='submit' style='padding:10px 16px;'>""" + ("מאשר" if action == "approve" else "דוחה") + """</button>
          </form>
        </body></html>
        """
        return HttpResponse(html, content_type="text/html; charset=utf-8")

    # POST -> execute action
    from django.db import transaction
    from . import api as core_api

    with transaction.atomic():
        proposal = (
            AppointmentChangeProposal.objects
            .select_for_update()
            .select_related("appointment", "appointment__provider", "appointment__room", "proposed_room")
            .get(pk=proposal.id)
        )

        if proposal.status != AppointmentChangeProposal.Status.PENDING:
            return HttpResponse("הבקשה כבר טופלה.", status=409)

        now = timezone.now()
        if proposal.expires_at and proposal.expires_at <= now:
            proposal.status = AppointmentChangeProposal.Status.EXPIRED
            proposal.decided_at = now
            proposal.decision_note = "expired"
            proposal.save(update_fields=["status", "decided_at", "decision_note"])
            return HttpResponse("הבקשה פג תוקף.", status=410)

        appt = Appointment.objects.select_for_update().select_related("provider", "room", "business").get(pk=proposal.appointment_id)

        # Stale proposal protection: appointment changed since proposal creation
        if (
            appt.start_time != proposal.original_start_time
            or appt.end_time != proposal.original_end_time
            or appt.room_id != proposal.original_room_id
        ):
            proposal.status = AppointmentChangeProposal.Status.REJECTED
            proposal.decided_at = now
            proposal.decision_note = "stale"
            proposal.save(update_fields=["status", "decided_at", "decision_note"])
            core_api._safe_create_audit_event(
                business=proposal.business,
                actor_user=None,
                action="change_proposal_stale_rejected",
                object_type="AppointmentChangeProposal",
                object_id=str(proposal.id),
                before={"appointment": appt.id},
                after={"status": proposal.status, "note": "stale"},
            )
            return HttpResponse("הבקשה כבר לא רלוונטית (התור השתנה).", status=409)

        if action == "reject":
            proposal.status = AppointmentChangeProposal.Status.REJECTED
            proposal.decided_at = now
            proposal.decision_note = "rejected_by_provider"
            proposal.save(update_fields=["status", "decided_at", "decision_note"])
            core_api._safe_create_audit_event(
                business=proposal.business,
                actor_user=None,
                action="change_proposal_rejected",
                object_type="AppointmentChangeProposal",
                object_id=str(proposal.id),
                before={
                    "appointment": appt.id,
                    "room_id": appt.room_id,
                    "start_time": appt.start_time.isoformat(),
                    "end_time": appt.end_time.isoformat(),
                },
                after={"status": proposal.status},
            )
            return HttpResponse("דחית את הבקשה. תודה.", content_type="text/html; charset=utf-8")

        # approve
        ok, error_message, alternatives = core_api._validate_and_apply_appointment_change(
            proposal=proposal,
            appointment=appt,
            actor_user=None,
        )
        if not ok:
            # Keep proposal pending so staff can update/create a new one.
            proposal.last_error_code = "approve_validation_failed"
            proposal.last_error_message = error_message
            proposal.last_error_payload = {"alternatives": alternatives}
            proposal.last_attempted_at = now
            proposal.save(update_fields=["last_error_code", "last_error_message", "last_error_payload", "last_attempted_at"])

            core_api._safe_create_audit_event(
                business=proposal.business,
                actor_user=None,
                action="change_proposal_approve_failed",
                object_type="AppointmentChangeProposal",
                object_id=str(proposal.id),
                before={"status": proposal.status},
                after={"status": proposal.status, "error": error_message},
            )

            alt_html = ""
            if alternatives:
                alt_html = "<p><b>חלופות אפשריות:</b></p><ul>" + "".join(
                    f"<li>{a.get('start_time','')} (room_id={a.get('room_id')})</li>" for a in alternatives
                ) + "</ul>"
            return HttpResponse(
                f"לא ניתן לאשר כרגע: {error_message}" + alt_html,
                status=409,
                content_type="text/html; charset=utf-8",
            )

        proposal.status = AppointmentChangeProposal.Status.APPROVED
        proposal.decided_at = now
        proposal.decision_note = "approved_by_provider"
        proposal.save(update_fields=["status", "decided_at", "decision_note"])

        return HttpResponse("אושר. התור עודכן בהצלחה.", content_type="text/html; charset=utf-8")


# --- Public waitlist offer links (Stage 4) ---

_WAITLIST_OFFER_ACTION_SALT = "core.waitlist_offer.action"


def make_waitlist_offer_action_token(*, offer_id: int, action: str) -> str:
    payload = {"oid": int(offer_id), "act": str(action)}
    return signing.dumps(payload, salt=_WAITLIST_OFFER_ACTION_SALT, compress=True)


def build_public_waitlist_offer_action_url(*, token: str, action: str) -> str:
    base = getattr(settings, "SITE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    return f"{base}/w/{token}/{action}/"


def waitlist_offer_action_view(request, token: str, action: str):
    """Public endpoint for a waitlist offer: accept / decline.

    GET  -> show a minimal confirmation page
    POST -> perform the action if still valid
    """
    action = (action or "").strip().lower()
    if action not in {"accept", "decline"}:
        return HttpResponseBadRequest("Invalid action")

    max_age = int(getattr(settings, "WAITLIST_OFFER_ACTION_LINK_MAX_AGE_SECONDS", 7 * 24 * 60 * 60))

    try:
        data = signing.loads(token, salt=_WAITLIST_OFFER_ACTION_SALT, max_age=max_age)
    except signing.SignatureExpired:
        return HttpResponse("הקישור פג תוקף.", status=410)
    except signing.BadSignature:
        return HttpResponseBadRequest("קישור לא תקין.")

    if data.get("act") != action:
        return HttpResponseBadRequest("קישור לא תואם לפעולה.")

    offer_id = data.get("oid")
    if not offer_id:
        return HttpResponseBadRequest("קישור לא תקין.")

    from .models import WaitlistOffer

    offer = (
        WaitlistOffer.objects
        .select_related("entry", "entry__client", "slot", "slot__service", "slot__provider")
        .filter(pk=offer_id)
        .first()
    )
    if not offer:
        return HttpResponseNotFound("ההצעה לא נמצאה.")

    # If the offer itself expired (independent of the signed token expiry),
    # we treat it as gone and persist the status for audit/consistency.
    now = timezone.now()
    if offer.status == WaitlistOffer.Status.PENDING and offer.expires_at and offer.expires_at <= now:
        offer.status = WaitlistOffer.Status.EXPIRED
        offer.decided_at = now
        offer.decision_note = "expired"
        offer.save(update_fields=["status", "decided_at", "decision_note"])
        return HttpResponse("ההצעה פג תוקף.", status=410)

    if offer.status == WaitlistOffer.Status.EXPIRED:
        return HttpResponse("ההצעה פג תוקף.", status=410)

    # Render confirmation page
    if request.method == "GET":
        client_name = offer.entry.client.full_name if offer.entry_id else ""
        service_name = offer.slot.service.name if getattr(offer.slot, "service_id", None) else ""
        provider_name = offer.slot.provider.display_name if getattr(offer.slot, "provider_id", None) else ""
        dt = timezone.localtime(offer.slot.start_time)
        when = dt.strftime("%Y-%m-%d %H:%M")
        csrf = get_token(request)
        button = "מאשר" if action == "accept" else "דוחה"
        html = f"""
        <html><body style='font-family: Arial, sans-serif; direction: rtl;'>
          <h2>הצעת תור שהתפנה</h2>
          <p><b>ללקוח:</b> {client_name}</p>
          <p><b>שירות:</b> {service_name or '-'} </p>
          <p><b>רופא:</b> {provider_name or '-'} </p>
          <p><b>מועד:</b> {when}</p>
          <form method='post'>
            <input type='hidden' name='csrfmiddlewaretoken' value='{csrf}' />
            <button type='submit' style='padding:10px 16px;'>{button}</button>
          </form>
        </body></html>
        """
        return HttpResponse(html, content_type="text/html; charset=utf-8")

    # POST -> execute action
    from .waitlist import accept_offer, decline_offer

    try:
        if action == "decline":
            ok, message = decline_offer(offer_id=int(offer_id))
        else:
            ok, message = accept_offer(offer_id=int(offer_id))
    except Exception:
        ok, message = False, "שגיאה פנימית."

    if ok:
        status = 200
    else:
        status = 410 if "פג תוקף" in (message or "") else 409
    return HttpResponse(message, status=status, content_type="text/html; charset=utf-8")



