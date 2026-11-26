import logging
from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-only-change-me-not-for-production-use"
log = logging.getLogger("foyo.config")


class Settings(BaseSettings):
    """Runtime settings, read from the environment or `.env`."""

    # env_ignore_empty: a variable left blank (e.g. an optional secret in a hosting dashboard) counts as unset.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    app_name: str = "Foyo API"
    # "production" unless told otherwise: a forgotten setting must fail safe, not open.
    # Local development sets ENVIRONMENT=development in .env.
    environment: str = "production"

    # SQLite by default so the API runs with zero setup; set a Postgres URL in production,
    # e.g. postgresql+psycopg://foyo:foyo@localhost:5432/foyo
    database_url: str = "sqlite:///./foyo.db"

    jwt_secret: str = ""  # required in production (32+ characters); a dev default is used in development
    jwt_algorithm: str = "HS256"
    access_token_days: int = 30

    # Loads the demo household (Famille Kouassi, known password) into an empty database.
    # Defaults to on in development only, and is refused in production.
    seed_demo: bool | None = None
    public_base_url: str = "https://foyo.app"

    # Transactional e-mail: "log" writes messages to the server log, "smtp" sends them (e.g. Gmail).
    email_backend: str = "log"
    password_reset_minutes: int = 30
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587  # 587 = STARTTLS, 465 = implicit TLS
    smtp_username: str = ""  # the Gmail address
    smtp_password: SecretStr = SecretStr("")  # a Gmail *App Password*, not the account password
    smtp_from: str = ""  # defaults to smtp_username; Gmail only allows your address or a verified alias
    smtp_from_name: str = "Foyo"
    smtp_timeout_seconds: int = 15
    # Where this API is reachable from the outside (links in e-mails, e.g. e-mail verification).
    public_api_url: str = "http://localhost:8000"
    # Set by Render on web services (https://<name>.onrender.com): used when PUBLIC_API_URL isn't set.
    render_external_url: str = ""
    # Google / Apple sign-in: accepted OAuth client ids (web/Android/iOS) and Apple bundle / service ids.
    google_client_ids: list[str] = []
    apple_audiences: list[str] = []
    # RevenueCat → server: the "Authorization" value set on the webhook in the RevenueCat dashboard.
    revenuecat_webhook_secret: str = ""
    # Uploaded photos (task proof, receipts). Mount a persistent volume here in production.
    media_dir: str = "./media"
    # Telemetry (no account needed): limits per client IP and per installation, per API process.
    # A phone normally sends ~2 event batches a minute and a handful of crashes a day.
    telemetry_ip_requests_per_minute: int = 120
    telemetry_install_requests_per_minute: int = 20
    telemetry_install_events_per_hour: int = 2000
    telemetry_ip_crashes_per_minute: int = 30
    telemetry_install_crashes_per_hour: int = 30
    # Browsers only (the mobile app doesn't need CORS). Empty = no cross-origin access.
    # Push notifications: "log" writes them to the server log, "fcm" sends them with Firebase Cloud Messaging.
    push_backend: str = "log"
    fcm_credentials_file: str = ""  # service-account JSON downloaded from the Firebase console
    cors_origins: list[str] = []
    # Households live in West Africa: "today", due dates and the 21:00 report use this clock,
    # whatever the server's own timezone.
    timezone: str = "Africa/Abidjan"
    # Failed sign-ins before an account is locked for `login_lock_minutes`.
    login_max_failures: int = 5
    login_lock_minutes: int = 15

    @property
    def is_dev(self) -> bool:
        return self.environment in ("development", "test")

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        # Hosts (Render, Heroku…) give "postgresql://" or "postgres://"; SQLAlchemy needs the psycopg driver.
        for prefix in ("postgresql://", "postgres://"):
            if self.database_url.startswith(prefix):
                self.database_url = "postgresql+psycopg://" + self.database_url[len(prefix) :]
        if self.public_api_url == "http://localhost:8000" and self.render_external_url:
            self.public_api_url = self.render_external_url.rstrip("/")
        if self.seed_demo is None:
            self.seed_demo = self.is_dev
        if self.is_dev:
            self.jwt_secret = self.jwt_secret or DEV_JWT_SECRET
            if not self.cors_origins:
                self.cors_origins = ["*"]
            return self
        problems = []
        if len(self.jwt_secret) < 32 or self.jwt_secret == DEV_JWT_SECRET:
            problems.append("JWT_SECRET must be set to a random string of 32+ characters")
        if self.seed_demo:
            problems.append("SEED_DEMO=true would create an admin account with a public password")
        if "*" in self.cors_origins:
            problems.append("CORS_ORIGINS must list the allowed websites, not '*'")
        if problems:
            raise ValueError(f"Unsafe settings for ENVIRONMENT={self.environment}: " + "; ".join(problems))
        if self.database_url.startswith("sqlite"):
            log.warning("ENVIRONMENT=%s with SQLite: use Postgres for anything beyond one small server", self.environment)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
