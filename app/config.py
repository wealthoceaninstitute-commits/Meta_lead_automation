from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    database_url:    str  = Field(default="sqlite:///./local_crm.db", alias="DATABASE_URL")
    jwt_secret:      str  = Field(default="dev-secret-change-me",     alias="JWT_SECRET")
    jwt_algorithm:   str  = "HS256"
    admin_username:  str  = Field(default="admin",   alias="ADMIN_USERNAME")
    admin_password:  str  = Field(default="admin123", alias="ADMIN_PASSWORD")
    frontend_origin: str  = Field(default="http://localhost:3000", alias="FRONTEND_ORIGIN")
    meta_verify_token: str = Field(default="verify-token", alias="META_VERIFY_TOKEN")
    meta_access_token: str = Field(default="", alias="META_ACCESS_TOKEN")
    meta_page_access_token: str = Field(default="", alias="META_PAGE_ACCESS_TOKEN")
    meta_page_id:    str  = Field(default="", alias="META_PAGE_ID")
    meta_app_id:     str  = Field(default="", alias="META_APP_ID")

    whatsapp_enabled:          bool = Field(default=False, alias="WHATSAPP_ENABLED")
    whatsapp_access_token:     str  = Field(default="", alias="WHATSAPP_ACCESS_TOKEN")
    whatsapp_phone_number_id:  str  = Field(default="", alias="WHATSAPP_PHONE_NUMBER_ID")
    whatsapp_business_account_id: str = Field(default="", alias="WHATSAPP_BUSINESS_ACCOUNT_ID")

    # Default template (fallback when form_id not in DB)
    whatsapp_template_name: str = Field(
        default="woi_seminar_registration_followup", alias="WHATSAPP_TEMPLATE_NAME")
    whatsapp_language_code: str = Field(default="en", alias="WHATSAPP_LANGUAGE_CODE")

    seminar_timezone:  str = Field(default="Asia/Kolkata", alias="SEMINAR_TIMEZONE")
    seminar_venue:     str = Field(
        default="Kuvempunagara, Mysuru - https://g.co/kgs/FDbcqh", alias="SEMINAR_VENUE")

    # Sunday seminar
    seminar_sunday_time:    str = Field(default="10:30 AM to 12:30 PM", alias="SEMINAR_SUNDAY_TIME")
    seminar_sunday_arrival: str = Field(default="10:15 AM",             alias="SEMINAR_SUNDAY_ARRIVAL")

    # Friday session (replaces Thursday)
    seminar_friday_time:    str = Field(default="6:00 PM to 8:00 PM",  alias="SEMINAR_FRIDAY_TIME")
    seminar_friday_arrival: str = Field(default="5:45 PM",             alias="SEMINAR_FRIDAY_ARRIVAL")

    keep_alive_enabled: bool = Field(default=True, alias="KEEP_ALIVE_ENABLED")
    render_external_url: str = Field(default="", alias="RENDER_EXTERNAL_URL")

    class Config:
        env_file = ".env"
        populate_by_name = True


settings = Settings()
