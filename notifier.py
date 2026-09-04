# Handles user notifications and admin email alerts for the drumkit worker.
# Same pattern as the audio worker's notifier.py.
import os
import httpx
from supabase_client import supabase

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "support@uforiya.com")
FROM_EMAIL = "Uforiya <no-reply@uforiya.com>"


def notify_user(user_id: str, notification_type: str, title: str, message: str, drum_kit_id: str = None) -> None:
    """
    Insert a notification row for the user. Picked up by Supabase Realtime
    on the frontend.

    notification_type must be an existing notification_type enum value —
    "processing_complete" and "processing_failed" are reused as-is
    (feature-agnostic labels, same as tracks use), "drumkit_review_needed"
    is the one drumkit-specific addition. Column names match the real
    notifications schema: message (not body), related_drum_kit_id (not
    drum_kit_id), no read/is_read set here (defaults to false in the table).
    """
    try:
        payload = {
            "user_id": user_id,
            "type": notification_type,
            "title": title,
            "message": message,
        }
        if drum_kit_id:
            payload["related_drum_kit_id"] = drum_kit_id
        supabase.table("notifications").insert(payload).execute()
    except Exception as e:
        print(f"[notifier] Failed to insert notification: {e}")


def email_admin(subject: str, html: str) -> None:
    """Send an alert email to the admin via Resend. Only fires on final processing failure."""
    if not RESEND_API_KEY:
        print("[notifier] No RESEND_API_KEY set — skipping admin email")
        return
    try:
        httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": FROM_EMAIL,
                "to": ADMIN_EMAIL,
                "subject": subject,
                "html": html,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[notifier] Failed to send admin email: {e}")