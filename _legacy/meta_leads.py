import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import requests

from app.config import META_GRAPH_VERSION, META_PAGE_ACCESS_TOKEN
from app.sheets import append_lead_to_sheet, update_lead_row_by_id
from app.whatsapp import send_whatsapp_template


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_key(value: str) -> str:
    """Normalize Meta field names and Google Sheet headers the same way."""
    text = str(value or '').strip().lower()
    for ch in [' ', '-', '.', '/', '\n', '\t']:
        text = text.replace(ch, '_')
    for ch in ['?', ':', ';', ',', '(', ')', '[', ']', '{', '}', '!', "'", '"']:
        text = text.replace(ch, '')
    while '__' in text:
        text = text.replace('__', '_')
    return text.strip('_')


def clean_meta_value(value: Any) -> str:
    """Clean values exported by Meta, especially p:+91..., l:..., ag:... style IDs."""
    if value is None:
        return ''
    text = str(value).strip()
    # Meta exports often prefix IDs/phone in CSV exports. API normally does not,
    # but this keeps values clean if they ever come through that way.
    for prefix in ('l:', 'ag:', 'as:', 'c:', 'f:', 'p:'):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def field_value(item: Dict[str, Any]) -> str:
    values = item.get('values') or []
    if isinstance(values, list):
        return ', '.join(clean_meta_value(v) for v in values if v is not None)
    return clean_meta_value(values)


def get_field(field_data: List[Dict[str, Any]], possible_names: List[str]) -> str:
    normalized = {normalize_key(x) for x in possible_names}
    for item in field_data or []:
        name = normalize_key(item.get('name', ''))
        value = field_value(item)
        if name in normalized and value:
            return value
    return ''


def normalize_phone(phone: str) -> str:
    if not phone:
        return ''
    phone = clean_meta_value(phone)
    digits = ''.join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) == 10:
        return '+91' + digits
    if digits.startswith('91') and len(digits) == 12:
        return '+' + digits
    if digits.startswith('0') and len(digits) == 11:
        return '+91' + digits[-10:]
    return phone if str(phone).startswith('+') else digits


def graph_get(object_id: str, fields: str, timeout: int = 25) -> Dict[str, Any]:
    if not META_PAGE_ACCESS_TOKEN:
        raise RuntimeError('META_PAGE_ACCESS_TOKEN is missing')
    url = f'https://graph.facebook.com/{META_GRAPH_VERSION}/{object_id}'
    params = {'access_token': META_PAGE_ACCESS_TOKEN, 'fields': fields}
    response = requests.get(url, params=params, timeout=timeout)
    print(f'Meta GET /{object_id}?fields={fields}:', response.status_code, response.text, flush=True)
    response.raise_for_status()
    return response.json()


def fetch_lead_details(leadgen_id: str) -> Dict[str, Any]:
    """Fetch full lead answers.

    Do not request unsupported fields such as form_name/page_id from the Lead object.
    A single bad field makes the entire Graph call fail.
    """
    safe_fields = 'id,created_time,field_data,ad_id,form_id,is_organic,platform'
    return graph_get(leadgen_id, safe_fields)


def fetch_optional_object(object_id: str, fields: str) -> Dict[str, Any]:
    object_id = clean_meta_value(object_id)
    if not object_id:
        return {}
    try:
        return graph_get(object_id, fields, timeout=15)
    except Exception as exc:
        print(f'Optional Meta object fetch failed for {object_id}: {exc}', flush=True)
        return {}


def enrich_ad_form_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    """Fill ad/adset/campaign/form names if token has permission.

    Lead object often gives only ad_id/form_id. CSV export has names, but webhook does not.
    These extra calls fill names for real leads when Meta allows it.
    """
    ad_id = clean_meta_value(row.get('ad_id', ''))
    form_id = clean_meta_value(row.get('form_id', ''))

    if ad_id:
        # On Meta, ad_id from leadgen is enough to resolve ad name, adset and campaign.
        ad = fetch_optional_object(ad_id, 'id,name,adset_id,campaign_id')
        row['ad_name'] = row.get('ad_name') or ad.get('name', '')
        row['adset_id'] = clean_meta_value(row.get('adset_id') or ad.get('adset_id', ''))
        row['campaign_id'] = clean_meta_value(row.get('campaign_id') or ad.get('campaign_id', ''))

    if row.get('adset_id'):
        adset = fetch_optional_object(str(row['adset_id']), 'id,name,campaign_id')
        row['adset_name'] = row.get('adset_name') or adset.get('name', '')
        row['campaign_id'] = clean_meta_value(row.get('campaign_id') or adset.get('campaign_id', ''))

    if row.get('campaign_id'):
        campaign = fetch_optional_object(str(row['campaign_id']), 'id,name')
        row['campaign_name'] = row.get('campaign_name') or campaign.get('name', '')

    if form_id:
        form = fetch_optional_object(form_id, 'id,name')
        row['form_name'] = row.get('form_name') or form.get('name', '')

    return row


def map_lead_to_row(lead: Dict[str, Any], webhook_value: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    webhook_value = webhook_value or {}
    field_data = lead.get('field_data', []) or []

    full_name = get_field(field_data, [
        'full_name', 'name', 'first_name', 'customer_name', 'your_name',
        'what is your full name?', 'full name'
    ])
    phone = get_field(field_data, [
        'phone_number', 'phone', 'mobile_number', 'mobile', 'whatsapp_number',
        'phone number', 'mobile number', 'whatsapp number'
    ])
    email = get_field(field_data, ['email', 'email_address', 'email address'])

    row: Dict[str, Any] = {
        'id': clean_meta_value(lead.get('id') or webhook_value.get('leadgen_id', '')),
        'lead_id': clean_meta_value(lead.get('id') or webhook_value.get('leadgen_id', '')),
        'created_time': lead.get('created_time') or webhook_value.get('created_time', ''),
        'ad_id': clean_meta_value(lead.get('ad_id') or webhook_value.get('ad_id', '') or webhook_value.get('adgroup_id', '')),
        'ad_name': lead.get('ad_name', ''),
        'adset_id': clean_meta_value(lead.get('adset_id') or webhook_value.get('adset_id', '') or webhook_value.get('adgroup_id', '')),
        'adset_name': lead.get('adset_name', ''),
        'campaign_id': clean_meta_value(lead.get('campaign_id') or webhook_value.get('campaign_id', '')),
        'campaign_name': lead.get('campaign_name', ''),
        'form_id': clean_meta_value(lead.get('form_id') or webhook_value.get('form_id', '')),
        'form_name': '',
        'is_organic': lead.get('is_organic', ''),
        'platform': lead.get('platform', ''),
        'full_name': full_name,
        'name': full_name,
        'phone': normalize_phone(phone),
        'phone_number': normalize_phone(phone),
        'email': email,
        'page_id': clean_meta_value(webhook_value.get('page_id', '')),
        'received_at': now_iso(),
        'lead_status': 'new',
        'status': 'processed',
        'error': '',
        'raw_data': json.dumps(lead, ensure_ascii=False),
    }

    # Add every Meta form question into the row dictionary, both original and normalized.
    for item in field_data:
        original_name = str(item.get('name', '')).strip()
        if not original_name:
            continue
        value = field_value(item)
        row[original_name] = value
        row[normalize_key(original_name)] = value

    # Explicit aliases for your current Google Sheet columns.
    row['what_is_your_current_experience_level?'] = get_field(field_data, [
        'what_is_your_current_experience_level?',
        'what_is_your_current_experience_level',
        'what is your current experience level?',
        'current experience level',
        'experience level',
    ])
    row['what_is_your_current_experience_level'] = row['what_is_your_current_experience_level?']

    row['please_choose_a_day_for_the_free_seminar'] = get_field(field_data, [
        'please_choose_a_day_for_the_free_seminar',
        'please choose a day for the free seminar',
        'please choose a day for the free seminar?',
        'seminar day',
        'free seminar day',
    ])

    # Only try metadata after the main lead data is mapped. If optional calls fail,
    # the lead still writes with name/phone/form answers.
    row = enrich_ad_form_metadata(row)
    return row



def send_welcome_message_for_lead(row: Dict[str, Any]) -> None:
    """Send WhatsApp welcome template and update the same lead row.

    This is intentionally non-fatal: Google Sheet writing should not fail just
    because WhatsApp is not approved/configured yet.
    """
    phone = row.get('phone') or row.get('phone_number') or ''
    name = row.get('name') or row.get('full_name') or ''
    lead_id = row.get('id') or row.get('lead_id') or ''
    try:
        result = send_whatsapp_template(phone=phone, name=name, lead=row)
        print('WhatsApp welcome result:', result, flush=True)

        message_id = ''
        message_status = ''
        messages = result.get('messages') or []
        if messages:
            message_id = messages[0].get('id', '')
            message_status = messages[0].get('message_status', '') or 'accepted'

        update_lead_row_by_id(lead_id, {
            'whatsapp_message_id': message_id,
            'whatsapp_sent': 'Yes',
            'whatsapp_status': message_status or 'accepted',
            'whatsapp_sent_at': now_iso(),
            'whatsapp_error': '',
        })
    except Exception as exc:
        error_text = str(exc)
        print('WARNING: WhatsApp welcome message failed:', error_text, flush=True)
        if lead_id:
            update_lead_row_by_id(lead_id, {
                'whatsapp_sent': 'No',
                'whatsapp_failed': 'Yes',
                'whatsapp_status': 'send_failed',
                'whatsapp_error': error_text,
            })

def extract_leadgen_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value', {}) or {}
            if value.get('leadgen_id'):
                items.append(value)

    sample = payload.get('sample', {}) or {}
    sample_value = sample.get('value', {}) or {}
    if sample_value.get('leadgen_id'):
        print('Detected Meta dashboard test sample payload:', sample_value, flush=True)
        items.append(sample_value)

    return items


def subscribe_page_to_app(page_id: str) -> bool:
    if not META_PAGE_ACCESS_TOKEN:
        print('WARNING: META_PAGE_ACCESS_TOKEN missing — cannot subscribe page', flush=True)
        return False
    url = f'https://graph.facebook.com/{META_GRAPH_VERSION}/{page_id}/subscribed_apps'
    params = {'access_token': META_PAGE_ACCESS_TOKEN, 'subscribed_fields': 'leadgen'}
    try:
        response = requests.post(url, params=params, timeout=15)
        print(f'Page subscription [{page_id}]:', response.status_code, response.text, flush=True)
        return response.ok
    except Exception as exc:
        print('Page subscription error:', exc, flush=True)
        return False


def process_leadgen_payload(payload: Dict[str, Any]) -> List[str]:
    processed: List[str] = []
    items = extract_leadgen_items(payload)
    print('Incoming leadgen items:', items, flush=True)

    if not items:
        print('WARNING: No leadgen_id found in payload:', json.dumps(payload), flush=True)
        append_lead_to_sheet({
            'id': '',
            'received_at': now_iso(),
            'status': 'no_leadgen_id',
            'error': 'No leadgen_id found in webhook payload',
            'raw_data': json.dumps(payload, ensure_ascii=False),
        })
        return processed

    for item in items:
        leadgen_id = str(item.get('leadgen_id', '')).strip()
        try:
            if leadgen_id and set(leadgen_id) == {'4'}:
                row = map_lead_to_row({'id': leadgen_id, 'field_data': []}, item)
                row['status'] = 'dashboard_test_dummy'
                row['error'] = 'Dummy leadgen_id from Meta Webhooks dashboard test. Real lead fetch skipped.'
                row['raw_data'] = json.dumps(item, ensure_ascii=False)
                appended = append_lead_to_sheet(row)
                # Do not send WhatsApp for Meta dashboard dummy test leads.
                processed.append(f'dashboard-test:{leadgen_id}')
                continue

            lead = fetch_lead_details(leadgen_id)
            row = map_lead_to_row(lead, item)
            appended = append_lead_to_sheet(row)
            if appended:
                send_welcome_message_for_lead(row)
            else:
                print('WhatsApp welcome skipped because this lead was already in the sheet.', flush=True)
            processed.append(row.get('id', leadgen_id))
        except Exception as exc:
            error_text = str(exc)
            print(f'ERROR processing leadgen_id {leadgen_id}: {error_text}', flush=True)
            append_lead_to_sheet({
                'id': clean_meta_value(leadgen_id),
                'lead_id': clean_meta_value(leadgen_id),
                'created_time': item.get('created_time', ''),
                'ad_id': clean_meta_value(item.get('ad_id', '') or item.get('adgroup_id', '')),
                'form_id': clean_meta_value(item.get('form_id', '')),
                'page_id': clean_meta_value(item.get('page_id', '')),
                'received_at': now_iso(),
                'lead_status': 'error',
                'status': 'error',
                'error': error_text,
                'raw_data': json.dumps({'webhook_value': item}, ensure_ascii=False),
            })
            processed.append(f'error:{leadgen_id}')

    return processed
