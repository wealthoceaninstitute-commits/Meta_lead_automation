from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    database_url:    str  = Field(default="sqlite:///./local_crm.db", alias="DATABASE_URL")
    jwt_secret:      str  = Field(default="dev-secret-change-me",     alias="JWT_SECRET")
    jwt_algorithm:   str  = "HS256"
    jwt_days:        int  = Field(default=30, alias="JWT_DAYS")
    admin_username:  str  = Field(default="admin",   alias="ADMIN_USERNAME")
    admin_password:  str  = Field(default="admin123", alias="ADMIN_PASSWORD")
    frontend_origin: str  = Field(default="http://localhost:3000", alias="FRONTEND_ORIGIN")

    # ── Meta (Facebook / Instagram lead ads) ────────────────────────────────
    meta_verify_token: str = Field(default="verify-token", alias="META_VERIFY_TOKEN")
    meta_access_token: str = Field(default="", alias="META_ACCESS_TOKEN")
    meta_page_access_token: str = Field(default="", alias="META_PAGE_ACCESS_TOKEN")
    meta_page_id:    str  = Field(default="", alias="META_PAGE_ID")
    meta_app_id:     str  = Field(default="", alias="META_APP_ID")
    meta_app_secret: str  = Field(default="", alias="META_APP_SECRET")
    meta_graph_version: str = Field(default="v25.0", alias="META_GRAPH_VERSION")

    # ── WhatsApp Cloud API ──────────────────────────────────────────────────
    whatsapp_enabled:          bool = Field(default=False, alias="WHATSAPP_ENABLED")
    whatsapp_access_token:     str  = Field(default="", alias="WHATSAPP_ACCESS_TOKEN")
    whatsapp_phone_number_id:  str  = Field(default="", alias="WHATSAPP_PHONE_NUMBER_ID")
    whatsapp_business_account_id: str = Field(default="", alias="WHATSAPP_BUSINESS_ACCOUNT_ID")
    # Public image/video/document URL for templates that have a media HEADER, as
    # "template_name=https://…" pairs (space/comma separated).
    wa_header_images:          str  = Field(default="", alias="WA_HEADER_IMAGES")

    # NOTE: WhatsApp template name / language / variables are NOT configured here any more —
    # they live per form in CRM → Form Config.

    # ── Sessions (defaults used when a form is auto-mapped by day) ──────────
    seminar_timezone:  str = Field(default="Asia/Kolkata", alias="SEMINAR_TIMEZONE")
    seminar_venue:     str = Field(
        default="Kuvempunagara, Mysuru - https://g.co/kgs/FDbcqh", alias="SEMINAR_VENUE")

    seminar_sunday_time:    str = Field(default="10:30 AM to 12:30 PM", alias="SEMINAR_SUNDAY_TIME")
    seminar_sunday_arrival: str = Field(default="10:15 AM",             alias="SEMINAR_SUNDAY_ARRIVAL")

    seminar_friday_time:    str = Field(default="6:00 PM to 8:00 PM",  alias="SEMINAR_FRIDAY_TIME")
    seminar_friday_arrival: str = Field(default="5:45 PM",             alias="SEMINAR_FRIDAY_ARRIVAL")

    # If a lead arrives on session day, still invite them to *today's* session
    # as long as it starts at least this many minutes from now.
    session_cutoff_minutes: int = Field(default=60, alias="SESSION_CUTOFF_MINUTES")

    # ── Reliability ─────────────────────────────────────────────────────────
    scheduler_enabled:        bool = Field(default=True, alias="SCHEDULER_ENABLED")
    sync_interval_minutes:    int  = Field(default=20,   alias="SYNC_INTERVAL_MINUTES")
    token_check_minutes:      int  = Field(default=15,   alias="TOKEN_CHECK_MINUTES")
    # Never auto-WhatsApp a lead older than this (protects against blasting
    # old leads when a backfill / sync pulls them in).
    auto_send_max_age_hours:  int  = Field(default=36,   alias="AUTO_SEND_MAX_AGE_HOURS")
    pull_lookback_hours:      int  = Field(default=96,   alias="PULL_LOOKBACK_HOURS")
    # Leads older than this are never re-fetched / retried; if still unfetched they are written off
    # (status "unrecoverable") so they cost no more Meta API calls.
    # New forms older than this are not added to Form Config automatically (keeps the list short);
    # add an old form by hand if you need it.
    discover_max_age_days:    int  = Field(default=150,  alias="DISCOVER_MAX_AGE_DAYS")
    retry_max_age_hours:      int  = Field(default=48,   alias="RETRY_MAX_AGE_HOURS")
    warn_token_days:          int  = Field(default=10,   alias="WARN_TOKEN_DAYS")

    # ── Alerts (all optional) ───────────────────────────────────────────────
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id:   str = Field(default="", alias="TELEGRAM_CHAT_ID")
    alert_webhook_url:  str = Field(default="", alias="ALERT_WEBHOOK_URL")   # Slack / ntfy / any POST
    smtp_host:          str = Field(default="", alias="SMTP_HOST")
    smtp_port:          int = Field(default=587, alias="SMTP_PORT")
    smtp_user:          str = Field(default="", alias="SMTP_USER")
    smtp_password:      str = Field(default="", alias="SMTP_PASSWORD")
    alert_email_to:     str = Field(default="", alias="ALERT_EMAIL_TO")

    keep_alive_enabled: bool = Field(default=True, alias="KEEP_ALIVE_ENABLED")
    render_external_url: str = Field(default="", alias="RENDER_EXTERNAL_URL")

    class Config:
        env_file = ".env"
        populate_by_name = True
        extra = "ignore"


settings = Settings()
