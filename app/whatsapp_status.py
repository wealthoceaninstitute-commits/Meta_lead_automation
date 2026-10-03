"""
WhatsApp delivery-status webhook processing
-------------------------------------------
Meta sends status callbacks (sent / delivered / read / failed) to your configured
webhook URL for every outgoing template message. This module updates the matching
WhatsAppMessage row (by wa_message_id) so the CRM can show real delivery counts
instead of just "accepted".

INTEGRATION
===========
In whatever service receives Meta's webhook (the endpoint Meta calls), after you
parse the incoming JSON body, route any status events through apply_status_updates.

IMPORTANT: that service MUST write to the SAME database the CRM reads from
(your Neon PostgreSQL). If your webhook runs on a separate service/DB, either point
Meta's callback URL at the CRM backend, or have the webhook service import this
module against the shared Neon DB.

FastAPI example
---------------
    from .db import get_db
    from .whatsapp_status import apply_status_updates

    @app.post("/webhook")
    async def whatsapp_webhook(request: Request, db: Session = Depends(get_db)):
        body = await request.json()
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                # delivery/read/failed status events:
                if value.get("statuses"):
                    apply_status_updates(value, db)
                # ... your existing incoming-message (inbox) handling stays here ...
        return {"status": "ok"}

The webhook verification (GET challenge) and your inbox logic are unchanged — this
only adds status tracking for outgoing messages.
"""

import logging
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from .models import WhatsAppMessage

logger = logging.getLogger(__name__)

# Status progression rank. We only ever move a message FORWARD through its
# lifecycle, because webhooks can arrive out of order (a late "sent" must not
# overwrite an already-recorded "delivered"/"read"). "failed" is terminal and
# outranks delivery progress so a stray late event can't undo it.
#
#   accepted  → our local state the instant Meta returns a message id
#   sent      → Meta handed it to WhatsApp
#   delivered → reached the recipient's device
#   read      → recipient opened it (blue ticks)
#   failed    → not delivered (e.g. 131049 marketing frequency cap)
_RANK = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}


def apply_status_updates(value: dict, db: Session) -> int:
    """
    Process the 'statuses' array from a WhatsApp webhook 'value' object and update
    the matching WhatsAppMessage rows. Returns the number of rows actually updated.

    Safe to call on every webhook 'value' — it no-ops when there are no statuses.
    """
    statuses = value.get("statuses") or []
    if not statuses:
        return 0

    updated = 0
    for s in statuses:
        wamid = s.get("id")
        new_status = (s.get("status") or "").lower()

        if not wamid or new_status not in _RANK:
            continue

        row = (
            db.query(WhatsAppMessage)
            .filter(WhatsAppMessage.wa_message_id == wamid)
            .first()
        )
        if not row:
            # Status arrived before the send record committed, or the message was
            # sent outside this system. Skip quietly — a later poll will reconcile.
            logger.info(
                "status webhook: no message row for wamid=%s status=%s", wamid, new_status
            )
            continue

        current = (row.status or "accepted").lower()

        # Never downgrade. The only time we accept an equal/lower rank is the very
        # first real status replacing our local "accepted" placeholder.
        if current != "accepted" and _RANK.get(new_status, 0) <= _RANK.get(current, 0):
            continue

        row.status = new_status

        # Persist a light status trail in `raw`. For failures, keep the full error
        # array so the UI can surface WHY it failed (e.g. code 131049 = freq cap).
        if new_status == "failed":
            row.raw = {
                "status": "failed",
                "timestamp": s.get("timestamp"),
                "recipient_id": s.get("recipient_id"),
                "errors": s.get("errors") or [],
            }
        else:
            row.raw = {
                "status": new_status,
                "timestamp": s.get("timestamp"),
                "recipient_id": s.get("recipient_id"),
            }

        updated += 1

    if updated:
        db.commit()

    return updated


def extract_status_error(raw) -> Tuple[Optional[str], Optional[str]]:
    """
    Pull (error_code, error_title) out of a stored failed-status payload.
    Used by the /message-status endpoint to tell the UI why a send failed.
    """
    import json

    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict):
            errors = raw.get("errors") or []
            if errors:
                e = errors[0]
                code = e.get("code")
                title = e.get("title") or e.get("message")
                return (str(code) if code is not None else None), title
    except Exception:
        pass
    return None, None
