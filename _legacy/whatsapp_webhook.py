import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.sheets import (
    append_whatsapp_incoming_message,
    update_lead_row_by_message_id,
    update_latest_lead_row_by_phone,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_to_iso(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except Exception:
        return _now_iso()


def _extract_message_text(message: Dict[str, Any]) -> str:
    msg_type = message.get('type', '')
    if msg_type == 'text':
        return ((message.get('text') or {}).get('body') or '').strip()
    if msg_type == 'button':
        button = message.get('button') or {}
        return (button.get('text') or button.get('payload') or '').strip()
    if msg_type == 'interactive':
        interactive = message.get('interactive') or {}
        if interactive.get('type') == 'button_reply':
            reply = interactive.get('button_reply') or {}
            return (reply.get('title') or reply.get('id') or '').strip()
        if interactive.get('type') == 'list_reply':
            reply = interactive.get('list_reply') or {}
            return (reply.get('title') or reply.get('id') or '').strip()
    if msg_type == 'image':
        return '[image]'
    if msg_type == 'audio':
        return '[audio]'
    if msg_type == 'video':
        return '[video]'
    if msg_type == 'document':
        return '[document]'
    if msg_type == 'location':
        location = message.get('location') or {}
        return f"[location] {location.get('latitude','')},{location.get('longitude','')}".strip()
    return f'[{msg_type or "unknown"}]'


def _status_updates(status_item: Dict[str, Any]) -> Dict[str, Any]:
    status = str(status_item.get('status') or '').lower().strip()
    ts = _timestamp_to_iso(status_item.get('timestamp'))
    errors = status_item.get('errors') or []
    conversation = status_item.get('conversation') or {}
    conversation_id = conversation.get('id', '')

    updates: Dict[str, Any] = {
        'whatsapp_status': status,
        'whatsapp_last_status_at': ts,
    }

    if conversation_id:
        updates['whatsapp_conversation_id'] = conversation_id

    if status in ('sent', 'accepted'):
        updates['whatsapp_sent'] = 'Yes'
        updates['whatsapp_sent_at'] = ts
    elif status == 'delivered':
        updates['whatsapp_sent'] = 'Yes'
        updates['whatsapp_delivered'] = 'Yes'
        updates['whatsapp_delivered_at'] = ts
    elif status == 'read':
        updates['whatsapp_sent'] = 'Yes'
        updates['whatsapp_delivered'] = 'Yes'
        updates['whatsapp_read'] = 'Yes'
        updates['whatsapp_read_at'] = ts
    elif status == 'failed':
        updates['whatsapp_failed'] = 'Yes'
        updates['whatsapp_failed_at'] = ts
        if errors:
            updates['whatsapp_error'] = json.dumps(errors, ensure_ascii=False)

    return updates


def process_whatsapp_statuses(value: Dict[str, Any]) -> List[str]:
    processed: List[str] = []
    for status_item in value.get('statuses') or []:
        message_id = status_item.get('id') or ''
        status = str(status_item.get('status') or '').lower().strip()
        updates = _status_updates(status_item)
        ok = update_lead_row_by_message_id(message_id, updates)
        print('WhatsApp status webhook:', json.dumps(status_item, ensure_ascii=False), flush=True)
        print('WhatsApp status sheet update result:', ok, updates, flush=True)
        processed.append(f'{message_id}:{status}:{ok}')
    return processed


def process_whatsapp_incoming_messages(value: Dict[str, Any]) -> List[str]:
    processed: List[str] = []
    contacts = value.get('contacts') or []
    profile_by_wa_id = {}
    for contact in contacts:
        wa_id = str(contact.get('wa_id') or '')
        profile = contact.get('profile') or {}
        profile_by_wa_id[wa_id] = profile.get('name', '')

    for message in value.get('messages') or []:
        from_phone = str(message.get('from') or '')
        message_id = str(message.get('id') or '')
        msg_type = str(message.get('type') or '')
        timestamp_iso = _timestamp_to_iso(message.get('timestamp'))
        text = _extract_message_text(message)
        profile_name = profile_by_wa_id.get(from_phone, '')

        incoming_row = {
            'received_at': _now_iso(),
            'from_phone': from_phone,
            'profile_name': profile_name,
            'message_id': message_id,
            'message_type': msg_type,
            'message_text': text,
            'timestamp': timestamp_iso,
            'raw_data': json.dumps(message, ensure_ascii=False),
        }
        append_whatsapp_incoming_message(incoming_row)

        lead_updates = {
            'whatsapp_reply_received': 'Yes',
            'whatsapp_reply_text': text,
            'whatsapp_reply_from': from_phone,
            'whatsapp_reply_at': timestamp_iso,
            'whatsapp_reply_message_id': message_id,
        }
        ok = update_latest_lead_row_by_phone(from_phone, lead_updates)
        print('WhatsApp incoming message webhook:', json.dumps(incoming_row, ensure_ascii=False), flush=True)
        print('WhatsApp incoming message latest lead update result:', ok, lead_updates, flush=True)
        processed.append(f'{from_phone}:{message_id}:{ok}')
    return processed


def is_whatsapp_webhook_payload(payload: Dict[str, Any]) -> bool:
    for entry in payload.get('entry') or []:
        for change in entry.get('changes') or []:
            value = change.get('value') or {}
            if value.get('messaging_product') == 'whatsapp':
                return True
            if value.get('messages') or value.get('statuses'):
                return True
    return False


def process_whatsapp_webhook_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    status_results: List[str] = []
    message_results: List[str] = []

    for entry in payload.get('entry') or []:
        for change in entry.get('changes') or []:
            value = change.get('value') or {}
            if not value:
                continue
            if value.get('statuses'):
                status_results.extend(process_whatsapp_statuses(value))
            if value.get('messages'):
                message_results.extend(process_whatsapp_incoming_messages(value))

    return {
        'statuses': status_results,
        'messages': message_results,
    }
