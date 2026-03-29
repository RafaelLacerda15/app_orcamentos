import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from sqlalchemy import func, text

from .extensions import csrf, db
from .models import (
    AdminAuditLog,
    MessageHistory,
    MessageTemplate,
    PasswordResetVerification,
    SignupVerification,
    SubscriptionOrder,
    Supplier,
    User,
)
from .routes import bp

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _env_value(name: str) -> str | None:
    # Alguns editores salvam .env com BOM e o primeiro nome pode vir com "\ufeff".
    return (os.getenv(name) or os.getenv(f"\ufeff{name}") or "").strip() or None


def _master_credentials() -> tuple[str | None, str | None, str | None]:
    username = _env_value("MASTER_USERNAME")
    password = _env_value("MASTER_PASSWORD")
    email = _env_value("MASTER_EMAIL")
    return username, password, email


def _env_int(name: str, fallback: int) -> int:
    value = _env_value(name)
    if value is None:
        return fallback
    try:
        return int(value)
    except ValueError:
        return fallback


def _env_float(name: str, fallback: float) -> float:
    value = _env_value(name)
    if value is None:
        return fallback
    try:
        return float(value)
    except ValueError:
        return fallback


def _env_bool(name: str, fallback: bool) -> bool:
    value = _env_value(name)
    if value is None:
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_database_url(database_url: str) -> str:
    normalized_url = database_url.strip()
    if normalized_url.startswith("postgres://"):
        normalized_url = normalized_url.replace("postgres://", "postgresql://", 1)
    if normalized_url.startswith("postgresql://"):
        normalized_url = normalized_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return normalized_url


def _database_uri() -> str:
    database_url = _env_value("DATABASE_URL")
    if not database_url:
        return "sqlite:///orcamentos.db"
    return _normalize_database_url(database_url)


def _ensure_master_admin() -> None:
    master_username, master_password, master_email = _master_credentials()
    if not master_username or not master_password or not master_email:
        return

    username_l = master_username.lower()
    email_l = master_email.lower()

    master_by_username = User.query.filter(func.lower(User.username) == username_l).first()
    master_by_email = User.query.filter(func.lower(User.email) == email_l).first()

    # Reaproveita registro existente por username ou email para evitar colisao de UNIQUE(email).
    master = master_by_username or master_by_email
    if master is None:
        master = User(username=master_username, email=master_email, is_admin=True)
        master.set_password(master_password)
        db.session.add(master)
        db.session.commit()
        return

    changed = False
    if not master.is_admin:
        master.is_admin = True
        changed = True
    if master.email.lower() != email_l:
        email_taken = User.query.filter(
            func.lower(User.email) == email_l,
            User.id != master.id,
        ).first()
        if email_taken is None:
            master.email = master_email
            changed = True
    if master.username.lower() != username_l:
        username_taken = User.query.filter(
            func.lower(User.username) == username_l,
            User.id != master.id,
        ).first()
        if username_taken is None:
            master.username = master_username
            changed = True
        else:
            # Evita erro de UNIQUE(username) em cenarios legados.
            master.is_admin = True
            changed = True
    if not master.check_password(master_password):
        master.set_password(master_password)
        changed = True
    if changed:
        db.session.commit()


def _pick_default_owner_id(master_username: str | None = None) -> int | None:
    non_admin = User.query.filter_by(is_admin=False).order_by(User.id.asc()).first()
    if non_admin:
        return non_admin.id

    if master_username:
        master = User.query.filter(func.lower(User.username) == master_username.lower()).first()
        if master:
            return master.id

    any_user = User.query.order_by(User.id.asc()).first()
    return any_user.id if any_user else None


def _ensure_multi_tenant_columns() -> None:
    if db.engine.dialect.name != "sqlite":
        return

    def _column_exists(table_name: str, column_name: str) -> bool:
        rows = db.session.execute(text(f"PRAGMA table_info({table_name})")).mappings().all()
        return any(row["name"] == column_name for row in rows)

    for table_name in ("suppliers", "message_templates", "message_history"):
        if not _column_exists(table_name, "owner_user_id"):
            db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN owner_user_id INTEGER"))

    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_suppliers_owner_user_id ON suppliers (owner_user_id)"))
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS ix_message_templates_owner_user_id ON message_templates (owner_user_id)")
    )
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_message_history_owner_user_id ON message_history (owner_user_id)"))

    master_username, _, _ = _master_credentials()
    owner_id = _pick_default_owner_id(master_username)
    if owner_id is not None:
        db.session.execute(text("UPDATE suppliers SET owner_user_id = :owner WHERE owner_user_id IS NULL"), {"owner": owner_id})
        db.session.execute(
            text("UPDATE message_templates SET owner_user_id = :owner WHERE owner_user_id IS NULL"),
            {"owner": owner_id},
        )
        db.session.execute(
            text("UPDATE message_history SET owner_user_id = :owner WHERE owner_user_id IS NULL"),
            {"owner": owner_id},
        )

    db.session.commit()


def _ensure_saas_columns() -> None:
    if db.engine.dialect.name != "sqlite":
        return

    def _column_exists(table_name: str, column_name: str) -> bool:
        rows = db.session.execute(text(f"PRAGMA table_info({table_name})")).mappings().all()
        return any(row["name"] == column_name for row in rows)

    column_statements = {
        "subscription_status": "ALTER TABLE users ADD COLUMN subscription_status TEXT DEFAULT 'inactive'",
        "subscription_started_at": "ALTER TABLE users ADD COLUMN subscription_started_at DATETIME",
        "subscription_expires_at": "ALTER TABLE users ADD COLUMN subscription_expires_at DATETIME",
        "subscription_last_payment_at": "ALTER TABLE users ADD COLUMN subscription_last_payment_at DATETIME",
        "phone": "ALTER TABLE users ADD COLUMN phone TEXT",
        "phone_verified": "ALTER TABLE users ADD COLUMN phone_verified BOOLEAN DEFAULT 0",
        "tax_id": "ALTER TABLE users ADD COLUMN tax_id TEXT",
        "abacatepay_customer_id": "ALTER TABLE users ADD COLUMN abacatepay_customer_id TEXT",
    }
    for column_name, statement in column_statements.items():
        if not _column_exists("users", column_name):
            db.session.execute(text(statement))

    db.session.execute(text("UPDATE users SET subscription_status = 'inactive' WHERE subscription_status IS NULL"))
    db.session.execute(text("UPDATE users SET phone_verified = 0 WHERE phone_verified IS NULL"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_subscription_status ON users (subscription_status)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_subscription_expires_at ON users (subscription_expires_at)"))
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS ix_users_subscription_last_payment_at ON users (subscription_last_payment_at)")
    )
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_phone ON users (phone)"))
    db.session.commit()
    if not _column_exists("signup_verifications", "tax_id"):
        db.session.execute(text("ALTER TABLE signup_verifications ADD COLUMN tax_id TEXT"))

def create_app(test_config: dict | None = None) -> Flask:
    master_username, _, master_email = _master_credentials()
    secret_key = _env_value("SECRET_KEY")
    if not secret_key:
        secret_key = "test-secret-key" if (test_config and test_config.get("TESTING")) else None
    if not secret_key:
        raise RuntimeError("SECRET_KEY nao configurada. Defina SECRET_KEY no .env.")

    app_mode = (_env_value("APP_ENV") or _env_value("FLASK_ENV") or "production").strip().lower()
    payment_test_mode_default = app_mode != "production"

    app = Flask(__name__)
    app.config.update(
        APP_ENV=app_mode,
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=_database_uri(),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_NAME="app_orcamentos_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=_env_value("SESSION_COOKIE_SAMESITE") or "Lax",
        SESSION_COOKIE_SECURE=_env_bool("SESSION_COOKIE_SECURE", False),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=_env_int("SESSION_LIFETIME_HOURS", 12)),
        MASTER_USERNAME=master_username,
        MASTER_EMAIL=master_email,
        PLAN_STARTER_NAME=_env_value("PLAN_STARTER_NAME") or "Starter",
        PLAN_STARTER_PRICE_CENTS=_env_int("PLAN_STARTER_PRICE_CENTS", 4900),
        PLAN_PRO_NAME=_env_value("PLAN_PRO_NAME") or "Pro",
        PLAN_PRO_PRICE_CENTS=_env_int("PLAN_PRO_PRICE_CENTS", 9900),
        PLAN_BUSINESS_NAME=_env_value("PLAN_BUSINESS_NAME") or "Business",
        PLAN_BUSINESS_PRICE_CENTS=_env_int("PLAN_BUSINESS_PRICE_CENTS", 19900),
        SUBSCRIPTION_CURRENCY=_env_value("SUBSCRIPTION_CURRENCY") or "BRL",
        PAYMENT_WEBHOOK_TOKEN=_env_value("PAYMENT_WEBHOOK_TOKEN") or "",
        PAYMENT_TEST_MODE=_env_bool("PAYMENT_TEST_MODE", payment_test_mode_default),
        ABACATEPAY_MODE=_env_value("ABACATEPAY_MODE") or "mock",
        ABACATEPAY_API_KEY=_env_value("ABACATEPAY_API_KEY") or "",
        ABACATEPAY_WEBHOOK_SECRET=_env_value("ABACATEPAY_WEBHOOK_SECRET") or "",
        ABACATEPAY_WEBHOOK_PUBLIC_KEY=_env_value("ABACATEPAY_WEBHOOK_PUBLIC_KEY") or "",
        EMAIL_PROVIDER=_env_value("EMAIL_PROVIDER") or "console",
        VERIFICATION_CODE_LENGTH=_env_int("VERIFICATION_CODE_LENGTH", _env_int("SMS_CODE_LENGTH", 6)),
        VERIFICATION_CODE_TTL_MINUTES=_env_int("VERIFICATION_CODE_TTL_MINUTES", _env_int("SMS_CODE_TTL_MINUTES", 10)),
        VERIFICATION_MAX_VERIFY_ATTEMPTS=_env_int(
            "VERIFICATION_MAX_VERIFY_ATTEMPTS",
            _env_int("SMS_MAX_VERIFY_ATTEMPTS", 5),
        ),
        VERIFICATION_EMAIL_SUBJECT=_env_value("VERIFICATION_EMAIL_SUBJECT") or "Codigo de verificacao",
        SMTP_HOST=_env_value("SMTP_HOST") or "",
        SMTP_PORT=_env_int("SMTP_PORT", 587),
        SMTP_USERNAME=_env_value("SMTP_USERNAME") or "",
        SMTP_PASSWORD=_env_value("SMTP_PASSWORD") or "",
        SMTP_FROM_EMAIL=_env_value("SMTP_FROM_EMAIL") or "",
        SMTP_USE_TLS=_env_bool("SMTP_USE_TLS", True),
        WHATSAPP_PROVIDER=_env_value("WHATSAPP_PROVIDER") or "simulado",
        WHATSAPP_PYWHATKIT_WAIT_TIME=_env_int("WHATSAPP_PYWHATKIT_WAIT_TIME", 15),
        WHATSAPP_PYWHATKIT_CLOSE_TIME=_env_int("WHATSAPP_PYWHATKIT_CLOSE_TIME", 3),
        WHATSAPP_PYWHATKIT_CLOSE_TAB=_env_bool("WHATSAPP_PYWHATKIT_CLOSE_TAB", True),
        WHATSAPP_SEND_MIN_INTERVAL_SECONDS=_env_float("WHATSAPP_SEND_MIN_INTERVAL_SECONDS", 1.0),
        WHATSAPP_SEND_MAX_INTERVAL_SECONDS=_env_float("WHATSAPP_SEND_MAX_INTERVAL_SECONDS", 1.8),
        WHATSAPP_SEND_BURST_SIZE=_env_int("WHATSAPP_SEND_BURST_SIZE", 10),
        WHATSAPP_SEND_BURST_PAUSE_MIN_SECONDS=_env_float("WHATSAPP_SEND_BURST_PAUSE_MIN_SECONDS", 6.0),
        WHATSAPP_SEND_BURST_PAUSE_MAX_SECONDS=_env_float("WHATSAPP_SEND_BURST_PAUSE_MAX_SECONDS", 10.0),
    )
    if test_config:
        app.config.update(test_config)
    if app.config.get("TESTING"):
        app.config.setdefault("WTF_CSRF_ENABLED", False)

    db.init_app(app)
    csrf.init_app(app)
    app.register_blueprint(bp)

    with app.app_context():
        db.create_all()
        # Em bases SQLite legadas, precisamos adicionar colunas SaaS antes de qualquer query ORM em User.
        _ensure_saas_columns()
        _ensure_master_admin()
        _ensure_multi_tenant_columns()

    return app
