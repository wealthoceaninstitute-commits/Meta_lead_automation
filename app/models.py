from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, ForeignKey, JSON, Index
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from .db import Base


class FormConfig(Base):
    """
    Maps a Meta form_id to campaign/session details + WhatsApp template.
    Managed via frontend — no code change needed when new ads/forms are added.
    """
    __tablename__ = "form_configs"

    id             = Column(Integer, primary_key=True, index=True)
    form_id        = Column(String(100), unique=True, nullable=False, index=True)
    form_name      = Column(String(255), nullable=True)   # friendly label
    campaign_name  = Column(String(255), nullable=False)
    day            = Column(String(40),  nullable=False)  # Sunday / Friday / Saturday
    session_time   = Column(String(80),  nullable=True)   # e.g. 10:30 AM to 12:30 PM
    arrival_time   = Column(String(80),  nullable=True)   # e.g. 10:15 AM
    venue          = Column(String(500), nullable=True)
    wa_template    = Column(String(255), nullable=True)   # WhatsApp template name
    wa_language    = Column(String(20),  default="en")
    # Ordered, comma-separated list of values sent as the template body variables.
    # Allowed keys: name, campaign, session_day, session_date, session_time,
    # arrival_time, venue, city.  Empty = use the built-in default for the template.
    wa_params      = Column(String(500), nullable=True)
    is_active      = Column(Boolean, default=True)
    notes          = Column(Text, nullable=True)
    # Set when the system created this row by itself (new ad form discovered).
    auto_created   = Column(Boolean, default=False)
    # True until a human confirms the mapping on the Form Config page.
    needs_review   = Column(Boolean, default=False)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    updated_at     = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Lead(Base):
    __tablename__ = "leads"

    id             = Column(Integer, primary_key=True, index=True)
    meta_lead_id   = Column(String(100), unique=True, index=True, nullable=True)
    created_time   = Column(String(80), nullable=True)
    full_name      = Column(String(255), nullable=True)
    phone          = Column(String(40), index=True, nullable=True)
    email          = Column(String(255), nullable=True)
    city           = Column(String(120), nullable=True)
    experience     = Column(String(255), nullable=True)
    preferred_day  = Column(String(80), nullable=True)

    # Campaign / ad metadata (from webhook value)
    campaign_id    = Column(String(100), nullable=True)
    campaign_name  = Column(String(255), nullable=True)
    adset_id       = Column(String(100), nullable=True)
    adset_name     = Column(String(255), nullable=True)
    ad_id          = Column(String(100), nullable=True)
    ad_name        = Column(String(255), nullable=True)
    form_id        = Column(String(100), nullable=True)
    form_name      = Column(String(255), nullable=True)
    platform       = Column(String(80), nullable=True)
    is_organic     = Column(String(20), nullable=True)
    raw            = Column(JSON, nullable=True)

    # Session details (resolved from FormConfig)
    session_day    = Column(String(40), nullable=True)
    session_date   = Column(String(80), nullable=True)
    session_time   = Column(String(80), nullable=True)
    arrival_time   = Column(String(80), nullable=True)
    venue          = Column(String(500), nullable=True)

    # Ingestion health: ok | pending_fetch | needs_config | no_phone | unrecoverable
    sync_status    = Column(String(40), index=True, nullable=True)
    sync_error     = Column(Text, nullable=True)
    fetch_attempts = Column(Integer, default=0)

    # CRM fields
    status         = Column(String(80), default="New")
    owner          = Column(String(120), nullable=True)
    notes          = Column(Text, nullable=True)
    next_followup_at = Column(String(80), nullable=True)

    # WhatsApp
    whatsapp_sent          = Column(Boolean, default=False)
    whatsapp_delivered     = Column(Boolean, default=False)
    whatsapp_read          = Column(Boolean, default=False)
    whatsapp_failed        = Column(Boolean, default=False)
    whatsapp_status        = Column(String(80), nullable=True)
    whatsapp_message_id    = Column(String(500), index=True, nullable=True)
    whatsapp_sent_at       = Column(String(80), nullable=True)
    whatsapp_delivered_at  = Column(String(80), nullable=True)
    whatsapp_read_at       = Column(String(80), nullable=True)
    whatsapp_failed_at     = Column(String(80), nullable=True)
    whatsapp_last_status_at = Column(String(80), nullable=True)
    whatsapp_error         = Column(Text, nullable=True)
    wa_attempts            = Column(Integer, default=0)
    latest_reply_text      = Column(Text, nullable=True)
    latest_reply_at        = Column(String(80), nullable=True)
    unread_count           = Column(Integer, default=0)

    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    updated_at  = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    followups   = relationship("FollowUp", back_populates="lead", cascade="all, delete-orphan")


Index("idx_leads_phone_updated", Lead.phone, Lead.updated_at)


class FollowUp(Base):
    __tablename__ = "followups"
    id               = Column(Integer, primary_key=True, index=True)
    lead_id          = Column(Integer, ForeignKey("leads.id"), index=True)
    followup_no      = Column(Integer, nullable=True)
    followup_date    = Column(String(80), nullable=True)
    response         = Column(Text, nullable=True)
    confirmed        = Column(String(40), nullable=True)
    session_date     = Column(String(80), nullable=True)
    next_followup_date = Column(String(80), nullable=True)
    remarks          = Column(Text, nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
    lead             = relationship("Lead", back_populates="followups")


class WhatsAppMessage(Base):
    __tablename__ = "whatsapp_messages"
    id             = Column(Integer, primary_key=True, index=True)
    wa_message_id  = Column(String(500), index=True, nullable=True)
    lead_id        = Column(Integer, ForeignKey("leads.id"), nullable=True, index=True)
    phone          = Column(String(40), index=True)
    contact_name   = Column(String(255), nullable=True)
    direction      = Column(String(20))
    message_type   = Column(String(50), default="text")
    body           = Column(Text, nullable=True)
    status         = Column(String(80), nullable=True)
    raw            = Column(JSON, nullable=True)
    timestamp      = Column(String(80), nullable=True)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())


class WhatsAppStatusLog(Base):
    __tablename__ = "whatsapp_status_logs"
    id             = Column(Integer, primary_key=True, index=True)
    wa_message_id  = Column(String(500), index=True)
    status         = Column(String(80))
    recipient_id   = Column(String(80), nullable=True)
    timestamp      = Column(String(80), nullable=True)
    raw            = Column(JSON, nullable=True)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())


class WhatsAppTemplate(Base):
    __tablename__ = "whatsapp_templates"
    id                   = Column(Integer, primary_key=True, index=True)
    meta_template_id     = Column(String(100), nullable=True, index=True)
    name                 = Column(String(255), nullable=False, index=True)
    category             = Column(String(80), nullable=False, default="MARKETING")
    language             = Column(String(20), nullable=False, default="en")
    status               = Column(String(80), nullable=True)
    header_type          = Column(String(20), nullable=True)
    header_text          = Column(Text, nullable=True)
    header_media_handle  = Column(String(500), nullable=True)
    body_text            = Column(Text, nullable=False)
    body_variables       = Column(JSON, nullable=True)
    footer_text          = Column(Text, nullable=True)
    buttons              = Column(JSON, nullable=True)
    meta_raw             = Column(JSON, nullable=True)
    rejection_reason     = Column(Text, nullable=True)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    updated_at           = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AppSetting(Base):
    """Small key/value store. Used for tokens that can be rotated from the CRM
    (stored encrypted) and for health snapshots / alert cool-downs."""
    __tablename__ = "app_settings"
    key        = Column(String(120), primary_key=True)
    value      = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SystemEvent(Base):
    """Audit trail shown on the System page: token failures, syncs, alerts…"""
    __tablename__ = "system_events"
    id         = Column(Integer, primary_key=True, index=True)
    kind       = Column(String(60), index=True)
    level      = Column(String(20), default="info")   # info | warn | error
    message    = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
