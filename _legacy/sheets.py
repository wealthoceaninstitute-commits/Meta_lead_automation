from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import gspread
from google.oauth2.service_account import Credentials

from app.config import GOOGLE_SHEET_ID, get_service_account_file

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

DEFAULT_HEADERS = [
    'id', 'created_time', 'ad_id', 'ad_name', 'adset_id', 'adset_name',
    'campaign_id', 'campaign_name', 'form_id', 'form_name', 'is_organic',
    'platform', 'what_is_your_current_experience_level?',
    'please_choose_a_day_for_the_free_seminar', 'full_name', 'phone',
    'lead_status', 'email', 'page_id', 'received_at', 'status', 'error',

    # WhatsApp send/status tracking
    'whatsapp_message_id', 'whatsapp_sent', 'whatsapp_delivered', 'whatsapp_read',
    'whatsapp_failed', 'whatsapp_status', 'whatsapp_error', 'whatsapp_sent_at',
    'whatsapp_delivered_at', 'whatsapp_read_at', 'whatsapp_failed_at',
    'whatsapp_last_status_at', 'whatsapp_conversation_id',

    # Latest inbound reply tracking
    'whatsapp_reply_received', 'whatsapp_reply_text', 'whatsapp_reply_from',
    'whatsapp_reply_at', 'whatsapp_reply_message_id',

    'raw_data',
]

INCOMING_MESSAGE_HEADERS = [
    'received_at', 'from_phone', 'profile_name', 'message_id', 'message_type',
    'message_text', 'timestamp', 'raw_data'
]

_worksheet_cache = None
_incoming_worksheet_cache = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value: str) -> str:
    text = str(value or '').strip().lower()
    for ch in [' ', '-', '.', '/', '\n', '\t']:
        text = text.replace(ch, '_')
    for ch in ['?', ':', ';', ',', '(', ')', '[', ']', '{', '}', '!', "'", '"']:
        text = text.replace(ch, '')
    while '__' in text:
        text = text.replace('__', '_')
    return text.strip('_')


def _as_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (dict, list)):
        return str(value)
    return str(value)


def _digits_only(value: Any) -> str:
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


def _normalize_phone_variants(phone: Any) -> set[str]:
    digits = _digits_only(phone)
    variants = set()
    if not digits:
        return variants
    variants.add(digits)
    variants.add('+' + digits)
    if len(digits) == 10:
        variants.add('91' + digits)
        variants.add('+91' + digits)
    if digits.startswith('91') and len(digits) == 12:
        variants.add(digits[-10:])
        variants.add('+' + digits)
    return variants


def _get_client():
    if not GOOGLE_SHEET_ID:
        raise RuntimeError('GOOGLE_SHEET_ID is missing')
    service_file = get_service_account_file()
    creds = Credentials.from_service_account_file(service_file, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID)


def _ensure_headers(worksheet, required_headers: List[str]) -> List[str]:
    headers: List[str] = worksheet.row_values(1)
    if not headers:
        worksheet.append_row(required_headers, value_input_option='RAW')
        return required_headers[:]

    missing = [h for h in required_headers if h not in headers]
    if missing:
        print('Adding missing Google Sheet headers:', missing, flush=True)
        headers = headers + missing
        worksheet.update('1:1', [headers], value_input_option='RAW')
    return headers


def get_worksheet():
    global _worksheet_cache
    if _worksheet_cache is not None:
        _ensure_headers(_worksheet_cache, DEFAULT_HEADERS)
        return _worksheet_cache

    spreadsheet = _get_client()
    try:
        worksheet = spreadsheet.worksheet('Leads')
    except Exception:
        worksheet = spreadsheet.add_worksheet(title='Leads', rows=1000, cols=60)
        worksheet.append_row(DEFAULT_HEADERS, value_input_option='RAW')

    _ensure_headers(worksheet, DEFAULT_HEADERS)
    _worksheet_cache = worksheet
    return worksheet


def get_incoming_messages_worksheet():
    global _incoming_worksheet_cache
    if _incoming_worksheet_cache is not None:
        _ensure_headers(_incoming_worksheet_cache, INCOMING_MESSAGE_HEADERS)
        return _incoming_worksheet_cache

    spreadsheet = _get_client()
    try:
        worksheet = spreadsheet.worksheet('WhatsApp Messages')
    except Exception:
        worksheet = spreadsheet.add_worksheet(title='WhatsApp Messages', rows=1000, cols=20)
        worksheet.append_row(INCOMING_MESSAGE_HEADERS, value_input_option='RAW')

    _ensure_headers(worksheet, INCOMING_MESSAGE_HEADERS)
    _incoming_worksheet_cache = worksheet
    return worksheet


def _header_index_map(headers: List[str]) -> Dict[str, int]:
    return {str(header).strip(): idx + 1 for idx, header in enumerate(headers)}


def _find_row_by_header_value(worksheet, header: str, target_value: str, prefer_last: bool = False) -> Optional[int]:
    headers = worksheet.row_values(1)
    if header not in headers:
        return None
    col = headers.index(header) + 1
    values = worksheet.col_values(col)
    target = str(target_value or '').strip()
    if not target:
        return None

    matches = [idx for idx, value in enumerate(values, start=1) if idx > 1 and str(value).strip() == target]
    if not matches:
        return None
    return matches[-1] if prefer_last else matches[0]


def _find_latest_row_by_phone(worksheet, phone: str) -> Optional[int]:
    headers = worksheet.row_values(1)
    if 'phone' not in headers:
        return None
    phone_col = headers.index('phone') + 1
    values = worksheet.col_values(phone_col)
    variants = _normalize_phone_variants(phone)
    if not variants:
        return None

    matches = []
    for idx, value in enumerate(values, start=1):
        if idx == 1:
            continue
        if _normalize_phone_variants(value) & variants:
            matches.append(idx)
    return matches[-1] if matches else None


def lead_already_exists(worksheet, lead_id: str) -> bool:
    """Prevent duplicate rows when Meta retries a webhook or you press test repeatedly."""
    if not lead_id:
        return False
    try:
        existing_ids = worksheet.col_values(1)
        return str(lead_id) in {str(x).strip() for x in existing_ids[1:]}
    except Exception as exc:
        print('Duplicate check failed, will append anyway:', exc, flush=True)
        return False


def append_lead_to_sheet(row: Dict[str, Any]) -> bool:
    worksheet = get_worksheet()
    headers = _ensure_headers(worksheet, DEFAULT_HEADERS)

    lead_id = _as_text(row.get('id') or row.get('lead_id') or '')
    if lead_id and lead_already_exists(worksheet, lead_id):
        print(f'Skipping duplicate lead_id already in sheet: {lead_id}', flush=True)
        return False

    # Build lookup with normalized keys so headers with/without ? still match.
    normalized_row = {}
    for k, v in row.items():
        normalized_row[_norm(k)] = v
        normalized_row[str(k).strip().lower()] = v

    values = []
    for header in headers:
        direct_key = str(header).strip().lower()
        norm_key = _norm(header)
        value = normalized_row.get(direct_key, normalized_row.get(norm_key, ''))
        values.append(_as_text(value))

    print('Appending row to Google Sheet:', values, flush=True)
    worksheet.append_row(
        values,
        value_input_option='RAW',  # prevents long IDs/phone numbers becoming 1.36E+15
        insert_data_option='INSERT_ROWS',
    )
    return True


def update_lead_row_by_id(lead_id: str, updates: Dict[str, Any]) -> bool:
    worksheet = get_worksheet()
    headers = _ensure_headers(worksheet, DEFAULT_HEADERS)
    row_number = _find_row_by_header_value(worksheet, 'id', str(lead_id or '').strip())
    if not row_number:
        row_number = _find_row_by_header_value(worksheet, 'lead_id', str(lead_id or '').strip())
    if not row_number:
        print('Could not update lead row: lead_id not found:', lead_id, flush=True)
        return False
    return update_sheet_row(worksheet, row_number, updates)


def update_lead_row_by_message_id(message_id: str, updates: Dict[str, Any]) -> bool:
    worksheet = get_worksheet()
    headers = _ensure_headers(worksheet, DEFAULT_HEADERS)
    row_number = _find_row_by_header_value(worksheet, 'whatsapp_message_id', str(message_id or '').strip())
    if not row_number:
        print('Could not update WhatsApp status: message id not found in sheet:', message_id, flush=True)
        return False
    return update_sheet_row(worksheet, row_number, updates)


def update_latest_lead_row_by_phone(phone: str, updates: Dict[str, Any]) -> bool:
    worksheet = get_worksheet()
    _ensure_headers(worksheet, DEFAULT_HEADERS)
    row_number = _find_latest_row_by_phone(worksheet, phone)
    if not row_number:
        print('Could not update latest lead row: phone not found in sheet:', phone, flush=True)
        return False
    return update_sheet_row(worksheet, row_number, updates)


def update_sheet_row(worksheet, row_number: int, updates: Dict[str, Any]) -> bool:
    headers = _ensure_headers(worksheet, DEFAULT_HEADERS)
    col_map = _header_index_map(headers)

    cells = []
    for key, value in updates.items():
        if key not in col_map:
            headers.append(key)
            worksheet.update('1:1', [headers], value_input_option='RAW')
            col_map = _header_index_map(headers)
        cells.append(gspread.Cell(row_number, col_map[key], _as_text(value)))

    if cells:
        worksheet.update_cells(cells, value_input_option='RAW')
        print(f'Updated Google Sheet row {row_number}:', updates, flush=True)
    return True


def append_whatsapp_incoming_message(row: Dict[str, Any]) -> bool:
    worksheet = get_incoming_messages_worksheet()
    headers = _ensure_headers(worksheet, INCOMING_MESSAGE_HEADERS)

    normalized_row = {}
    for k, v in row.items():
        normalized_row[_norm(k)] = v
        normalized_row[str(k).strip().lower()] = v

    values = []
    for header in headers:
        direct_key = str(header).strip().lower()
        norm_key = _norm(header)
        value = normalized_row.get(direct_key, normalized_row.get(norm_key, ''))
        values.append(_as_text(value))

    print('Appending WhatsApp incoming message to sheet:', values, flush=True)
    worksheet.append_row(values, value_input_option='RAW', insert_data_option='INSERT_ROWS')
    return True
