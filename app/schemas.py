from pydantic import BaseModel
from typing import Any, Optional


class LeadOut(BaseModel):
    id:              int
    meta_lead_id:    Optional[str] = None
    created_time:    Optional[str] = None
    full_name:       Optional[str] = None
    phone:           Optional[str] = None
    email:           Optional[str] = None
    city:            Optional[str] = None
    experience:      Optional[str] = None
    preferred_day:   Optional[str] = None
    campaign_name:   Optional[str] = None
    form_name:       Optional[str] = None
    ad_name:         Optional[str] = None
    ad_id:           Optional[str] = None
    form_id:         Optional[str] = None
    platform:        Optional[str] = None
    # Session details (was seminar_*)
    session_day:     Optional[str] = None
    session_date:    Optional[str] = None
    session_time:    Optional[str] = None
    arrival_time:    Optional[str] = None
    venue:           Optional[str] = None
    # CRM
    status:          Optional[str] = None
    owner:           Optional[str] = None
    notes:           Optional[str] = None
    next_followup_at: Optional[str] = None
    # WhatsApp
    whatsapp_sent:        bool = False
    whatsapp_delivered:   bool = False
    whatsapp_read:        bool = False
    whatsapp_failed:      bool = False
    whatsapp_status:      Optional[str] = None
    whatsapp_message_id:  Optional[str] = None
    latest_reply_text:    Optional[str] = None
    latest_reply_at:      Optional[str] = None
    unread_count:         int = 0
    created_at:           Optional[Any] = None

    class Config:
        from_attributes = True


class LeadCreate(BaseModel):
    full_name:     str = ""
    phone:         str
    email:         str = ""
    city:          str = ""
    experience:    str = ""
    preferred_day: str = ""
    campaign_name: str = "Manual"
    platform:      str = "FB"
    status:        str = "New"
    notes:         str = ""


class LeadUpdate(BaseModel):
    full_name:       Optional[str] = None
    phone:           Optional[str] = None
    email:           Optional[str] = None
    city:            Optional[str] = None
    experience:      Optional[str] = None
    preferred_day:   Optional[str] = None
    status:          Optional[str] = None
    owner:           Optional[str] = None
    notes:           Optional[str] = None
    next_followup_at: Optional[str] = None
    campaign_name:   Optional[str] = None
    platform:        Optional[str] = None


class FollowUpIn(BaseModel):
    followup_date:      str = ""
    response:           str = ""
    confirmed:          str = ""
    session_date:       str = ""
    next_followup_date: str = ""
    remarks:            str = ""


class ReplyIn(BaseModel):
    phone:   str
    text:    str
    lead_id: Optional[int] = None


class TestWhatsAppIn(BaseModel):
    name:     str = "Test"
    phone:    str
    form_id:  str = ""
