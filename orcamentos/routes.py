from datetime import timedelta
from functools import wraps
from pathlib import Path
import re
import secrets
import shutil

import base64
import hashlib
import hmac as _hmac

from flask import Blueprint, Response, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_, case, func, or_, text
from sqlalchemy.orm import aliased
from werkzeug.security import check_password_hash, generate_password_hash

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
    SubscriptionCheckout
)
from .services.exporters import history_to_csv_bytes, suppliers_to_csv_bytes
from .services.importers import import_suppliers_from_rows, parse_rows_from_file
from .services.messaging import render_message
from .services.mailer import send_verification_code_email
from .services.timezone import server_now
from .services.validation import (
    has_duplicate_contact,
    normalize_supplier_payload,
    normalize_user_phone,
    validate_email,
)
from .services.whatsapp_delivery import send_whatsapp_message, whatsapp_provider, whatsapp_provider_label
from .services.whatsapp import WhatsAppSessionManager

bp = Blueprint("main", __name__)

SUPPLIERS_PER_PAGE = 15
HISTORY_PER_PAGE = 25
SESSION_MEMBER_KEY = "member_user_id"
SESSION_ADMIN_KEY = "admin_user_id"
SESSION_MEMBER_LOGIN_AT_KEY = "member_login_at"
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCK_SECONDS = 900
_LOGIN_ATTEMPTS: dict[str, dict[str, float | int]] = {}
PASSWORD_RESET_VERIFICATION_SESSION_KEY = "password_reset_verification_id"

PLAN_FEATURES: dict[str, dict[str, int | bool | None]] = {
    "trial": {
        "max_suppliers": 10,
        "max_templates": 2,
        "max_bulk_recipients": 20,
        "can_import_contacts": False,
        "can_whatsapp_session": True,
    },
    "starter": {
        "max_suppliers": 100,
        "max_templates": 10,
        "max_bulk_recipients": 50,
        "can_import_contacts": False,
        "can_whatsapp_session": True,
    },
    "pro": {
        "max_suppliers": 500,
        "max_templates": 30,
        "max_bulk_recipients": 150,
        "can_import_contacts": True,
        "can_whatsapp_session": True,
    },
    "business": {
        "max_suppliers": None,
        "max_templates": None,
        "max_bulk_recipients": None,
        "can_import_contacts": True,
        "can_whatsapp_session": True,
    },
}


@bp.before_app_request
def load_current_user() -> None:
    g.is_admin_area = request.path.startswith("/admin")

    member_id = session.get(SESSION_MEMBER_KEY)
    admin_id = session.get(SESSION_ADMIN_KEY)

    member_user = db.session.get(User, member_id) if member_id else None
    admin_user = db.session.get(User, admin_id) if admin_id else None

    if member_id and member_user is None:
        session.pop(SESSION_MEMBER_KEY, None)
        session.pop(SESSION_MEMBER_LOGIN_AT_KEY, None)
    if admin_id and admin_user is None:
        session.pop(SESSION_ADMIN_KEY, None)

    if member_user and _member_requires_reauth(member_user.id, session.get(SESSION_MEMBER_LOGIN_AT_KEY)):
        session.pop(SESSION_MEMBER_KEY, None)
        session.pop(SESSION_MEMBER_LOGIN_AT_KEY, None)
        member_user = None

    g.member_user = member_user
    g.admin_user = admin_user if (admin_user and admin_user.is_admin) else None
    if admin_user and not admin_user.is_admin:
        session.pop(SESSION_ADMIN_KEY, None)

    g.current_user = g.admin_user if g.is_admin_area else g.member_user


@bp.app_context_processor
def inject_current_user() -> dict[str, object]:
    member_user = getattr(g, "member_user", None)
    member_features = _member_plan_features(member_user) if member_user and not member_user.is_admin else {}
    member_plan_key = str(member_features.get("plan_key")) if member_features.get("plan_key") else None
    member_supplier_limit = member_features.get("max_suppliers")
    member_template_limit = member_features.get("max_templates")
    member_bulk_limit = member_features.get("max_bulk_recipients")
    return {
        "current_user": getattr(g, "current_user", None),
        "member_user": member_user,
        "admin_user": getattr(g, "admin_user", None),
        "is_admin_area": getattr(g, "is_admin_area", False),
        "member_subscription_active": _is_member_subscription_active(member_user) if member_user else False,
        "member_plan_label": _member_plan_label(member_user) if member_user and not member_user.is_admin else None,
        "member_plan_key": member_plan_key,
        "member_can_import_contacts": bool(member_features.get("can_import_contacts")),
        "member_can_whatsapp_session": bool(member_features.get("can_whatsapp_session")),
        "member_supplier_limit": member_supplier_limit if isinstance(member_supplier_limit, int) else None,
        "member_template_limit": member_template_limit if isinstance(member_template_limit, int) else None,
        "member_bulk_limit": member_bulk_limit if isinstance(member_bulk_limit, int) else None,
        "format_currency_from_cents": _format_currency_from_cents,
        "payment_test_mode": _payment_test_mode_enabled(),
        "app_env": (current_app.config.get("APP_ENV") or "development"),
    }


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not getattr(g, "member_user", None):
            return redirect(url_for("main.login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        user = getattr(g, "admin_user", None)
        if not user:
            return redirect(url_for("main.admin_login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped


def member_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        user = getattr(g, "member_user", None)
        if not user:
            return redirect(url_for("main.login", next=request.path))
        if _member_is_suspended(user):
            session.pop(SESSION_MEMBER_KEY, None)
            session.pop(SESSION_MEMBER_LOGIN_AT_KEY, None)
            flash("Sua conta esta suspensa. Fale com o suporte.", "error")
            return redirect(url_for("main.login"))
        return view_func(*args, **kwargs)

    return wrapped


def api_member_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        user = getattr(g, "member_user", None)
        if not user:
            return jsonify({"error": "nao_autenticado"}), 401
        if _member_is_suspended(user):
            session.pop(SESSION_MEMBER_KEY, None)
            session.pop(SESSION_MEMBER_LOGIN_AT_KEY, None)
            return jsonify({"error": "conta_suspensa"}), 403
        return view_func(*args, **kwargs)

    return wrapped


def subscription_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        user = getattr(g, "member_user", None)
        if not user:
            return redirect(url_for("main.login", next=request.path))
        if _member_is_suspended(user):
            session.pop(SESSION_MEMBER_KEY, None)
            session.pop(SESSION_MEMBER_LOGIN_AT_KEY, None)
            flash("Sua conta esta suspensa. Fale com o suporte.", "error")
            return redirect(url_for("main.login"))
        if not _is_member_subscription_active(user):
            flash("Sua assinatura esta inativa. Escolha um plano para continuar.", "error")
            return redirect(url_for("main.subscription"))
        return view_func(*args, **kwargs)

    return wrapped


def api_subscription_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        user = getattr(g, "member_user", None)
        if not user:
            return jsonify({"error": "nao_autenticado"}), 401
        if _member_is_suspended(user):
            session.pop(SESSION_MEMBER_KEY, None)
            session.pop(SESSION_MEMBER_LOGIN_AT_KEY, None)
            return jsonify({"error": "conta_suspensa"}), 403
        if not _is_member_subscription_active(user):
            return jsonify({"error": "assinatura_inativa"}), 402
        return view_func(*args, **kwargs)

    return wrapped


def _safe_next_url() -> str | None:
    target = request.args.get("next", "").strip()
    if target.startswith("/"):
        return target
    return None


def _get_page_arg() -> int:
    raw = request.args.get("page", "1").strip()
    try:
        page = int(raw)
    except ValueError:
        return 1
    return max(page, 1)


def _member_owner_id() -> int:
    return g.member_user.id


def _format_currency_from_cents(amount_cents: int, currency: str = "BRL") -> str:
    amount = max(amount_cents, 0) / 100
    if currency.upper() == "BRL":
        return f"R$ {amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{currency.upper()} {amount:,.2f}"


def _is_member_subscription_active(user: User) -> bool:
    if user.subscription_status != "active":
        return False
    if user.subscription_expires_at is None:
        return False
    return user.subscription_expires_at >= server_now()


def _member_plan_label(user: User) -> str:
    latest_order = (
        SubscriptionOrder.query.filter_by(user_id=user.id)
        .order_by(SubscriptionOrder.created_at.desc())
        .first()
    )
    if latest_order and latest_order.status == "paid":
        return latest_order.plan_name
    if latest_order and latest_order.status == "pending":
        return f"Pendente ({latest_order.plan_name})"
    if _is_member_subscription_active(user):
        return "Teste 15 dias"
    return "Sem plano"


def _plan_key_from_plan_name(plan_name: str | None) -> str | None:
    normalized_name = (plan_name or "").strip().lower()
    if not normalized_name:
        return None
    for plan in _subscription_plans():
        if str(plan["name"]).strip().lower() == normalized_name:
            return str(plan["key"])
    return None


def _active_member_plan_key(user: User | None) -> str | None:
    if user is None or not _is_member_subscription_active(user):
        return None
    latest_paid_order = (
        SubscriptionOrder.query.filter_by(user_id=user.id, status="paid")
        .order_by(SubscriptionOrder.created_at.desc())
        .first()
    )
    if latest_paid_order is None:
        return "trial"
    return _plan_key_from_plan_name(latest_paid_order.plan_name) or "starter"


def _member_plan_features(user: User | None) -> dict[str, int | bool | None | str]:
    plan_key = _active_member_plan_key(user)
    effective_key = plan_key or "starter"
    base_features = dict(PLAN_FEATURES.get(effective_key, PLAN_FEATURES["starter"]))
    base_features["plan_key"] = plan_key
    return base_features


def _member_has_feature(user: User | None, feature_name: str) -> bool:
    return bool(_member_plan_features(user).get(feature_name))


def _member_plan_limit(user: User | None, limit_name: str) -> int | None:
    value = _member_plan_features(user).get(limit_name)
    return value if isinstance(value, int) else None


def _member_is_suspended(user: User | None) -> bool:
    if user is None or user.is_admin:
        return False
    return (user.subscription_status or "").strip().lower() == "suspended"


def _forced_member_reauth_map() -> dict[int, float]:
    store = current_app.extensions.setdefault("forced_member_reauth", {})
    if not isinstance(store, dict):
        store = {}
        current_app.extensions["forced_member_reauth"] = store
    return store


def _force_member_reauth(user_id: int) -> None:
    _forced_member_reauth_map()[int(user_id)] = server_now().timestamp()


def _member_requires_reauth(user_id: int, login_ts: float | int | None) -> bool:
    forced_ts = float(_forced_member_reauth_map().get(int(user_id), 0))
    if forced_ts <= 0:
        return False
    try:
        current_login_ts = float(login_ts or 0)
    except (TypeError, ValueError):
        current_login_ts = 0
    return current_login_ts <= forced_ts


def _record_admin_audit(action: str, target_user_id: int | None = None, details: str | None = None) -> None:
    admin_user = getattr(g, "admin_user", None)
    if admin_user is None:
        return
    db.session.add(
        AdminAuditLog(
            admin_user_id=admin_user.id,
            target_user_id=target_user_id,
            action=action,
            details=details,
        )
    )


def _redirect_admin_return(default_endpoint: str, **default_kwargs):
    target = request.form.get("return_to", "").strip()
    if target.startswith("/admin"):
        return redirect(target)
    return redirect(url_for(default_endpoint, **default_kwargs))


def _admin_return_to_current() -> str:
    if request.query_string:
        return request.full_path.rstrip("?")
    return request.path


def _percent(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round((part / whole) * 100, 1)


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _subscription_plans() -> list[dict[str, str | int]]:
    currency = (current_app.config.get("SUBSCRIPTION_CURRENCY") or "BRL").strip().upper() or "BRL"

    def _price(key: str, fallback: int) -> int:
        try:
            return int(current_app.config.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    return [
        {
            "key": "starter",
            "name": (current_app.config.get("PLAN_STARTER_NAME") or "Starter").strip() or "Starter",
            "amount_cents": _price("PLAN_STARTER_PRICE_CENTS", 4900),
            "currency": currency,
        },
        {
            "key": "pro",
            "name": (current_app.config.get("PLAN_PRO_NAME") or "Pro").strip() or "Pro",
            "amount_cents": _price("PLAN_PRO_PRICE_CENTS", 9900),
            "currency": currency,
        },
        {
            "key": "business",
            "name": (current_app.config.get("PLAN_BUSINESS_NAME") or "Business").strip() or "Business",
            "amount_cents": _price("PLAN_BUSINESS_PRICE_CENTS", 19900),
            "currency": currency,
        },
    ]


def _subscription_plan_by_key(plan_key: str | None) -> dict[str, str | int] | None:
    target = (plan_key or "").strip().lower()
    for plan in _subscription_plans():
        if plan["key"] == target:
            return plan
    return None


def _login_guard_key(scope: str, username: str) -> str:
    remote_addr = (request.remote_addr or "unknown").strip()
    normalized_username = username.strip().lower()
    return f"{scope}:{remote_addr}:{normalized_username}"


def _consume_login_status(scope: str, username: str) -> tuple[bool, int]:
    now_ts = server_now().timestamp()
    key = _login_guard_key(scope, username)
    state = _LOGIN_ATTEMPTS.get(key)
    if not state:
        return False, 0

    lock_until = float(state.get("lock_until", 0))
    if lock_until > now_ts:
        return True, int(lock_until - now_ts)

    first_failure_at = float(state.get("first_failure_at", now_ts))
    if now_ts - first_failure_at > LOGIN_WINDOW_SECONDS:
        _LOGIN_ATTEMPTS.pop(key, None)
    return False, 0


def _record_login_failure(scope: str, username: str) -> None:
    now_ts = server_now().timestamp()
    key = _login_guard_key(scope, username)
    state = _LOGIN_ATTEMPTS.get(key)
    if not state or (now_ts - float(state.get("first_failure_at", now_ts))) > LOGIN_WINDOW_SECONDS:
        state = {"attempts": 0, "first_failure_at": now_ts, "lock_until": 0}
    state["attempts"] = int(state.get("attempts", 0)) + 1
    if int(state["attempts"]) >= LOGIN_MAX_ATTEMPTS:
        state["lock_until"] = now_ts + LOGIN_LOCK_SECONDS
    _LOGIN_ATTEMPTS[key] = state


def _clear_login_failures(scope: str, username: str) -> None:
    _LOGIN_ATTEMPTS.pop(_login_guard_key(scope, username), None)


def _payment_test_mode_enabled() -> bool:
    return bool(current_app.config.get("PAYMENT_TEST_MODE", False))


def _apply_paid_subscription_order(order: SubscriptionOrder, paid_at=None) -> None:
    user = db.session.get(User, order.user_id)
    if user is None:
        raise ValueError("Usuario do pedido nao encontrado.")
    if order.status == "paid":
        return

    now = paid_at or server_now()
    if user.subscription_expires_at and user.subscription_expires_at > now:
        period_start = user.subscription_expires_at
    else:
        period_start = now
    period_end = period_start + timedelta(days=30)

    order.status = "paid"
    order.paid_at = now
    order.period_start_at = period_start
    order.period_end_at = period_end

    user.subscription_status = "active"
    if user.subscription_started_at is None:
        user.subscription_started_at = period_start
    user.subscription_expires_at = period_end
    user.subscription_last_payment_at = now


def _webhook_token_is_valid() -> bool:
    expected_token = (current_app.config.get("PAYMENT_WEBHOOK_TOKEN") or "").strip()
    if not expected_token:
        return False
    provided_token = (request.headers.get("X-Webhook-Token") or "").strip()
    return secrets.compare_digest(provided_token, expected_token)


def _supplier_query_with_search(owner_user_id: int, text: str):
    query = Supplier.query.filter_by(owner_user_id=owner_user_id).order_by(Supplier.created_at.desc())
    if not text:
        return query

    pattern = f"%{text}%"
    return query.filter(
        or_(
            Supplier.name.ilike(pattern),
            Supplier.company.ilike(pattern),
            Supplier.phone.ilike(pattern),
            Supplier.email.ilike(pattern),
        )
    )


def _history_redirect_target():
    status_filter = request.form.get("status", request.args.get("status", "")).strip().lower()
    page_raw = request.form.get("page", request.args.get("page", "1")).strip()

    params: dict[str, str | int] = {}
    if status_filter:
        params["status"] = status_filter
    if page_raw.isdigit() and int(page_raw) > 1:
        params["page"] = int(page_raw)

    return redirect(url_for("main.history", **params))


def _username_key(username: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9_-]", "_", username.strip().lower())
    return key or "usuario"


def _get_whatsapp_manager() -> WhatsAppSessionManager:
    user = getattr(g, "member_user", None)
    if user is None:
        raise RuntimeError("Usuario nao autenticado")

    managers = current_app.extensions.setdefault("whatsapp_managers", {})
    manager_key = f"user_{user.id}_{_username_key(user.username)}"

    manager = managers.get(manager_key)
    if manager:
        return manager

    profile_dir = Path(current_app.instance_path) / "whatsapp_sessions" / manager_key
    manager = WhatsAppSessionManager(
        profile_dir=profile_dir,
        connect_timeout_seconds=_int_config("WHATSAPP_CONNECT_TIMEOUT_SECONDS", 180, minimum=30, maximum=3600),
        send_min_interval_seconds=_float_config("WHATSAPP_SEND_MIN_INTERVAL_SECONDS", 1.0, minimum=0.0, maximum=30.0),
        send_max_interval_seconds=_float_config("WHATSAPP_SEND_MAX_INTERVAL_SECONDS", 1.8, minimum=0.0, maximum=45.0),
        send_burst_size=_int_config("WHATSAPP_SEND_BURST_SIZE", 10, minimum=0, maximum=100),
        send_burst_pause_min_seconds=_float_config(
            "WHATSAPP_SEND_BURST_PAUSE_MIN_SECONDS",
            6.0,
            minimum=0.0,
            maximum=120.0,
        ),
        send_burst_pause_max_seconds=_float_config(
            "WHATSAPP_SEND_BURST_PAUSE_MAX_SECONDS",
            10.0,
            minimum=0.0,
            maximum=180.0,
        ),
    )
    managers[manager_key] = manager
    return manager


def _ensure_pywhatkit_session_ready() -> tuple[bool, str | None]:
    status, state_message = _pywhatkit_session_state(start_if_disconnected=True)

    if status == "connected":
        return True, None
    if status == "waiting_qr":
        return False, state_message or "Sessao WhatsApp aguardando QR Code. Acesse Configuracoes, escaneie e tente novamente."
    if status == "connecting":
        return False, state_message or "Sessao WhatsApp iniciando. Aguarde alguns segundos e tente novamente."
    return False, state_message or "Sessao WhatsApp nao conectada. Abra Configuracoes e inicie o login."


def _pywhatkit_session_state(*, start_if_disconnected: bool) -> tuple[str, str | None]:
    manager = _get_whatsapp_manager()
    state = manager.get_state()
    status = str(state.get("status") or "disconnected").strip().lower()

    if start_if_disconnected and status in {"disconnected", "error"}:
        manager.start()
        state = manager.get_state()
        status = str(state.get("status") or "disconnected").strip().lower()

    message = str(state.get("message") or "").strip() or None
    return status, message


def _float_config(key: str, fallback: float, *, minimum: float, maximum: float) -> float:
    raw = current_app.config.get(key, fallback)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = fallback
    return min(max(value, minimum), maximum)


def _int_config(key: str, fallback: int, *, minimum: int, maximum: int) -> int:
    raw = current_app.config.get(key, fallback)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = fallback
    return min(max(value, minimum), maximum)


def _registration_code_length() -> int:
    try:
        value = int(
            current_app.config.get(
                "VERIFICATION_CODE_LENGTH",
                current_app.config.get("SMS_CODE_LENGTH", 6),
            )
        )
    except (TypeError, ValueError):
        value = 6
    return min(max(value, 4), 8)


def _registration_code_ttl_minutes() -> int:
    try:
        value = int(
            current_app.config.get(
                "VERIFICATION_CODE_TTL_MINUTES",
                current_app.config.get("SMS_CODE_TTL_MINUTES", 10),
            )
        )
    except (TypeError, ValueError):
        value = 10
    return min(max(value, 1), 60)


def _registration_max_attempts() -> int:
    try:
        value = int(
            current_app.config.get(
                "VERIFICATION_MAX_VERIFY_ATTEMPTS",
                current_app.config.get("SMS_MAX_VERIFY_ATTEMPTS", 5),
            )
        )
    except (TypeError, ValueError):
        value = 5
    return min(max(value, 1), 15)


def _generate_numeric_code(length: int) -> str:
    digits = "0123456789"
    return "".join(secrets.choice(digits) for _ in range(length))


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not local or not domain:
        return email
    if len(local) <= 2:
        masked_local = local[0] + "*"
    else:
        masked_local = local[:2] + ("*" * (len(local) - 2))
    return f"{masked_local}@{domain}"


def _register_form_payload() -> dict[str, str]:
    return {
        "username": request.form.get("username", "").strip(),
        "email": request.form.get("email", "").strip().lower(),
        "phone": request.form.get("phone", "").strip(),
        # ← novo
        "tax_id": re.sub(r"[^0-9]", "", request.form.get("tax_id", "")),
    }


def _render_register(form_data: dict[str, str] | None = None):
    return render_template("register.html", form_data=form_data or {})


def _get_password_reset_verification() -> PasswordResetVerification | None:
    verification_id = session.get(PASSWORD_RESET_VERIFICATION_SESSION_KEY)
    if not verification_id:
        return None

    verification = PasswordResetVerification.query.filter_by(id=verification_id).first()
    if verification is None:
        session.pop(PASSWORD_RESET_VERIFICATION_SESSION_KEY, None)
        return None

    if verification.expires_at < server_now():
        db.session.delete(verification)
        db.session.commit()
        session.pop(PASSWORD_RESET_VERIFICATION_SESSION_KEY, None)
        return None

    return verification


def _forgot_password_form_payload() -> dict[str, str]:
    return {
        "email": request.form.get("email", "").strip().lower(),
    }


def _render_forgot_password(
    form_data: dict[str, str] | None = None,
    verification: PasswordResetVerification | None = None,
):
    pending = verification is not None
    data = form_data or {}
    if pending:
        data = {"email": verification.email}

    return render_template(
        "forgot_password.html",
        form_data=data,
        verification_pending=pending,
        verification_email_masked=_mask_email(verification.email) if verification else None,
        verification_code_length=_registration_code_length(),
    )


@bp.route("/login", methods=["GET", "POST"])
def login():
    user = getattr(g, "member_user", None)
    if user:
        return redirect(url_for("main.dashboard"))

    next_target = _safe_next_url()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Informe usuario e senha.", "error")
            return render_template("login.html")

        is_locked, remaining_seconds = _consume_login_status("member", username)
        if is_locked:
            wait_minutes = max(1, (remaining_seconds + 59) // 60)
            flash(f"Muitas tentativas. Tente novamente em {wait_minutes} minuto(s).", "error")
            return render_template("login.html")

        login_user = User.query.filter(func.lower(User.username) == username.lower()).first()
        if not login_user or not login_user.check_password(password):
            _record_login_failure("member", username)
            flash("Credenciais invalidas.", "error")
            return render_template("login.html")
        if login_user.is_admin:
            _record_login_failure("member", username)
            flash("Usuario administrativo deve acessar /admin/login.", "error")
            return render_template("login.html")
        if _member_is_suspended(login_user):
            _record_login_failure("member", username)
            flash("Sua conta esta suspensa. Entre em contato com o suporte.", "error")
            return render_template("login.html")

        _clear_login_failures("member", username)
        session[SESSION_MEMBER_KEY] = login_user.id
        session[SESSION_MEMBER_LOGIN_AT_KEY] = server_now().timestamp()
        flash("Login realizado com sucesso.", "success")
        if _is_member_subscription_active(login_user):
            return redirect(next_target or url_for("main.dashboard"))
        return redirect(url_for("main.subscription"))

    return render_template("login.html")


@bp.route("/esqueci-senha", methods=["GET", "POST"])
def forgot_password():
    if getattr(g, "member_user", None):
        return redirect(url_for("main.dashboard"))

    verification = _get_password_reset_verification()

    if request.method == "POST":
        action = request.form.get("action", "start").strip().lower()

        if action == "reset_password":
            if verification is None:
                flash("Solicite o codigo de recuperacao primeiro.", "error")
                return _render_forgot_password(form_data=_forgot_password_form_payload())

            code = request.form.get("verification_code", "").strip()
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            code_length = _registration_code_length()
            max_attempts = _registration_max_attempts()
            if not code.isdigit() or len(code) != code_length:
                flash(f"Informe um codigo de {code_length} digitos.", "error")
                return _render_forgot_password(verification=verification)
            if len(new_password) < 6:
                flash("Nova senha deve ter ao menos 6 caracteres.", "error")
                return _render_forgot_password(verification=verification)
            if new_password != confirm_password:
                flash("As senhas nao conferem.", "error")
                return _render_forgot_password(verification=verification)
            if verification.attempts >= max_attempts:
                db.session.delete(verification)
                db.session.commit()
                session.pop(PASSWORD_RESET_VERIFICATION_SESSION_KEY, None)
                flash("Limite de tentativas excedido. Solicite um novo codigo.", "error")
                return _render_forgot_password()

            if not check_password_hash(verification.code_hash, code):
                verification.attempts += 1
                db.session.commit()
                remaining = max(max_attempts - verification.attempts, 0)
                if remaining == 0:
                    db.session.delete(verification)
                    db.session.commit()
                    session.pop(PASSWORD_RESET_VERIFICATION_SESSION_KEY, None)
                    flash("Codigo invalido e limite de tentativas excedido. Solicite novo codigo.", "error")
                    return _render_forgot_password()
                flash(f"Codigo invalido. Tentativas restantes: {remaining}.", "error")
                return _render_forgot_password(verification=verification)

            user = db.session.get(User, verification.user_id)
            if user is None or user.email.lower() != verification.email.lower():
                db.session.delete(verification)
                db.session.commit()
                session.pop(PASSWORD_RESET_VERIFICATION_SESSION_KEY, None)
                flash("Solicitacao invalida. Tente novamente.", "error")
                return _render_forgot_password()

            user.set_password(new_password)
            db.session.delete(verification)
            db.session.commit()
            session.pop(PASSWORD_RESET_VERIFICATION_SESSION_KEY, None)
            flash("Senha redefinida com sucesso. Faca login com a nova senha.", "success")
            return redirect(url_for("main.login"))

        if action == "resend_code":
            if verification is None:
                flash("Solicite o codigo de recuperacao primeiro.", "error")
                return _render_forgot_password(form_data=_forgot_password_form_payload())

            code = _generate_numeric_code(_registration_code_length())
            verification.code_hash = generate_password_hash(code)
            verification.expires_at = server_now() + timedelta(minutes=_registration_code_ttl_minutes())
            verification.attempts = 0
            db.session.commit()

            sent, preview_code = send_verification_code_email(verification.email, code)
            if not sent:
                flash("Nao foi possivel reenviar o email agora. Tente novamente em instantes.", "error")
                return _render_forgot_password(verification=verification)

            flash("Codigo reenviado por email.", "success")
            if preview_code:
                flash(f"Codigo (modo teste): {preview_code}", "success")
            return _render_forgot_password(verification=verification)

        form_data = _forgot_password_form_payload()
        email = form_data["email"]
        if not email or not validate_email(email):
            flash("Informe um email valido.", "error")
            return _render_forgot_password(form_data=form_data)

        old_verification = _get_password_reset_verification()
        if old_verification is not None:
            db.session.delete(old_verification)
            db.session.commit()
            session.pop(PASSWORD_RESET_VERIFICATION_SESSION_KEY, None)

        user = User.query.filter(func.lower(User.email) == email.lower()).first()
        if user is None or user.is_admin:
            flash("Se este email estiver cadastrado, enviaremos um codigo de recuperacao.", "success")
            return _render_forgot_password(form_data=form_data)

        PasswordResetVerification.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        code = _generate_numeric_code(_registration_code_length())
        verification = PasswordResetVerification(
            user_id=user.id,
            email=user.email,
            code_hash=generate_password_hash(code),
            expires_at=server_now() + timedelta(minutes=_registration_code_ttl_minutes()),
            attempts=0,
        )
        db.session.add(verification)
        db.session.commit()
        session[PASSWORD_RESET_VERIFICATION_SESSION_KEY] = verification.id

        sent, preview_code = send_verification_code_email(user.email, code)
        if not sent:
            db.session.delete(verification)
            db.session.commit()
            session.pop(PASSWORD_RESET_VERIFICATION_SESSION_KEY, None)
            flash("Nao foi possivel enviar o email de recuperacao. Tente novamente.", "error")
            return _render_forgot_password(form_data=form_data)

        flash("Codigo de recuperacao enviado por email.", "success")
        if preview_code:
            flash(f"Codigo (modo teste): {preview_code}", "success")
        return _render_forgot_password(verification=verification)

    return _render_forgot_password(verification=verification)


@bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    user = getattr(g, "admin_user", None)
    if user and user.is_admin:
        return redirect(url_for("main.admin_dashboard"))

    next_target = _safe_next_url()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Informe usuario e senha.", "error")
            return render_template("admin_login.html")

        is_locked, remaining_seconds = _consume_login_status("admin", username)
        if is_locked:
            wait_minutes = max(1, (remaining_seconds + 59) // 60)
            flash(f"Muitas tentativas administrativas. Tente em {wait_minutes} minuto(s).", "error")
            return render_template("admin_login.html")

        admin_user = User.query.filter(func.lower(User.username) == username.lower(), User.is_admin.is_(True)).first()
        if not admin_user or not admin_user.check_password(password):
            _record_login_failure("admin", username)
            flash("Credenciais administrativas invalidas.", "error")
            return render_template("admin_login.html")

        _clear_login_failures("admin", username)
        session[SESSION_ADMIN_KEY] = admin_user.id
        flash("Login administrativo realizado com sucesso.", "success")
        return redirect(next_target or url_for("main.admin_dashboard"))

    return render_template("admin_login.html")


@bp.route("/cadastro", methods=["GET", "POST"])
def register():
    if getattr(g, "member_user", None):
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        form_data = _register_form_payload()
        username = form_data["username"]
        email = form_data["email"]
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        raw_phone = form_data["phone"]
        normalized_phone = normalize_user_phone(raw_phone)

        if not username or not email or not password or not raw_phone:
            flash("Preencha usuario, email, telefone e senha.", "error")
            return _render_register(form_data=form_data)
        if len(username) < 3:
            flash("Usuario deve ter ao menos 3 caracteres.", "error")
            return _render_register(form_data=form_data)
        if len(password) < 6:
            flash("Senha deve ter ao menos 6 caracteres.", "error")
            return _render_register(form_data=form_data)
        if password != confirm_password:
            flash("As senhas nao conferem.", "error")
            return _render_register(form_data=form_data)
        if not validate_email(email):
            flash("Email invalido.", "error")
            return _render_register(form_data=form_data)
        if not normalized_phone:
            flash("Telefone invalido. Informe DDD e numero valido.", "error")
            return _render_register(form_data=form_data)
        
        tax_id_clean = re.sub(r"[^0-9]", "", form_data.get("tax_id", ""))
        if not tax_id_clean or len(tax_id_clean) not in (11, 14):
            flash("Informe um CPF (11 dígitos) ou CNPJ (14 dígitos) válido.", "error")
            return _render_register(form_data=form_data)
        
        username_exists = User.query.filter(func.lower(User.username) == username.lower()).first()
        if username_exists:
            flash("Nome de usuario ja cadastrado.", "error")
            return _render_register(form_data=form_data)

        email_exists = User.query.filter(func.lower(User.email) == email.lower()).first()
        if email_exists:
            flash("Email ja cadastrado.", "error")
            return _render_register(form_data=form_data)

        phone_exists = User.query.filter(User.phone == normalized_phone).first()
        if phone_exists:
            flash("Telefone ja cadastrado.", "error")
            return _render_register(form_data=form_data)

        is_first_user = User.query.count() == 0
        user = User(
            username=username,
            email=email,
            phone=normalized_phone,
            tax_id=tax_id_clean,
            phone_verified=False,
            is_admin=is_first_user,
        )
        user.set_password(password)
        trial_start = server_now()
        user.subscription_status = "active"
        user.subscription_started_at = trial_start
        user.subscription_expires_at = trial_start + timedelta(days=15)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Nao foi possivel concluir o cadastro. Verifique os dados e tente novamente.", "error")
            return _render_register(form_data=form_data)

        session[SESSION_MEMBER_KEY] = user.id
        session[SESSION_MEMBER_LOGIN_AT_KEY] = server_now().timestamp()
        if is_first_user:
            flash("Conta criada. Voce e o administrador inicial com teste de 15 dias ativo.", "success")
        else:
            flash("Conta criada com sucesso. Seu teste de 15 dias ja esta ativo.", "success")
        return redirect(url_for("main.dashboard"))

    return _render_register()


@bp.post("/logout")
@login_required
def logout():
    session.pop(SESSION_MEMBER_KEY, None)
    session.pop(SESSION_MEMBER_LOGIN_AT_KEY, None)
    flash("Sessao do usuario encerrada.", "success")
    return redirect(url_for("main.login"))


@bp.post("/admin/logout")
@admin_required
def admin_logout():
    session.pop(SESSION_ADMIN_KEY, None)
    flash("Sessao administrativa encerrada.", "success")
    return redirect(url_for("main.admin_login"))


@bp.get("/")
@subscription_required
def dashboard():
    owner_user_id = _member_owner_id()
    user = g.member_user
    supplier_count = Supplier.query.filter_by(owner_user_id=owner_user_id).count()
    message_count = MessageHistory.query.filter_by(owner_user_id=owner_user_id).count()
    template_count = MessageTemplate.query.filter_by(owner_user_id=owner_user_id).count()
    recent_activities = (
        MessageHistory.query.filter_by(owner_user_id=owner_user_id).order_by(MessageHistory.sent_at.desc()).limit(8).all()
    )
    last_30_days = server_now() - timedelta(days=30)
    messages_last_30 = MessageHistory.query.filter(
        MessageHistory.owner_user_id == owner_user_id,
        MessageHistory.sent_at >= last_30_days,
    ).count()
    provider = whatsapp_provider()
    whatsapp_connection_warning = None
    whatsapp_status = None

    if provider == "pywhatkit" and _member_has_feature(user, "can_whatsapp_session"):
        whatsapp_status, state_message = _pywhatkit_session_state(start_if_disconnected=False)
        if whatsapp_status != "connected":
            whatsapp_connection_warning = state_message or "WhatsApp desconectado. Abra Configuracoes para conectar."

    return render_template(
        "dashboard.html",
        supplier_count=supplier_count,
        message_count=message_count,
        template_count=template_count,
        messages_last_30=messages_last_30,
        recent_activities=recent_activities,
        subscription_active=_is_member_subscription_active(user),
        subscription_expires_at=user.subscription_expires_at,
        whatsapp_connection_warning=whatsapp_connection_warning,
        whatsapp_status=whatsapp_status,
    )


@bp.get("/admin")
@admin_required
def admin_dashboard():
    now = server_now()
    last_24h = now - timedelta(hours=24)
    last_7_days = now - timedelta(days=7)
    last_30_days = now - timedelta(days=30)
    last_60_days = now - timedelta(days=60)
    expiring_cutoff = now + timedelta(days=7)
    admin_return_to = _admin_return_to_current()

    users_common_query = User.query.filter_by(is_admin=False)
    users_total = User.query.count()
    users_admin = User.query.filter_by(is_admin=True).count()
    users_common = users_common_query.count()
    users_with_active_subscription = users_common_query.filter(
        User.subscription_status == "active",
        User.subscription_expires_at.is_not(None),
        User.subscription_expires_at >= now,
    ).count()
    users_with_expired_subscription = users_common_query.filter(
        User.subscription_expires_at.is_not(None),
        User.subscription_expires_at < now,
    ).count()
    users_suspended = users_common_query.filter(User.subscription_status == "suspended").count()
    users_renewed = users_common_query.filter(
        User.subscription_last_payment_at.is_not(None),
        User.subscription_last_payment_at >= last_30_days,
    ).count()
    latest_users = User.query.filter_by(is_admin=False).order_by(User.created_at.desc()).limit(10).all()

    pending_orders_count = SubscriptionOrder.query.filter_by(status="pending").count()
    pending_orders = (
        SubscriptionOrder.query.filter_by(status="pending").order_by(SubscriptionOrder.created_at.desc()).limit(6).all()
    )
    pending_orders_over_2_days = SubscriptionOrder.query.filter(
        SubscriptionOrder.status == "pending",
        SubscriptionOrder.created_at <= now - timedelta(days=2),
    ).count()

    paid_revenue_last_30_cents = _safe_int(
        db.session.query(func.coalesce(func.sum(SubscriptionOrder.amount_cents), 0))
        .filter(
            SubscriptionOrder.status == "paid",
            SubscriptionOrder.paid_at.is_not(None),
            SubscriptionOrder.paid_at >= last_30_days,
        )
        .scalar()
    )
    paid_revenue_prev_30_cents = _safe_int(
        db.session.query(func.coalesce(func.sum(SubscriptionOrder.amount_cents), 0))
        .filter(
            SubscriptionOrder.status == "paid",
            SubscriptionOrder.paid_at.is_not(None),
            SubscriptionOrder.paid_at >= last_60_days,
            SubscriptionOrder.paid_at < last_30_days,
        )
        .scalar()
    )
    pending_revenue_cents = _safe_int(
        db.session.query(func.coalesce(func.sum(SubscriptionOrder.amount_cents), 0))
        .filter(SubscriptionOrder.status == "pending")
        .scalar()
    )

    latest_paid_order_subq = (
        db.session.query(
            SubscriptionOrder.user_id.label("user_id"),
            func.max(SubscriptionOrder.id).label("latest_order_id"),
        )
        .filter(SubscriptionOrder.status == "paid")
        .group_by(SubscriptionOrder.user_id)
        .subquery()
    )
    mrr_cents = _safe_int(
        db.session.query(func.coalesce(func.sum(SubscriptionOrder.amount_cents), 0))
        .join(latest_paid_order_subq, SubscriptionOrder.id == latest_paid_order_subq.c.latest_order_id)
        .join(User, User.id == latest_paid_order_subq.c.user_id)
        .filter(
            User.is_admin.is_(False),
            User.subscription_status == "active",
            User.subscription_expires_at.is_not(None),
            User.subscription_expires_at >= now,
        )
        .scalar()
    )
    active_paid_users = _safe_int(
        db.session.query(func.count(func.distinct(SubscriptionOrder.user_id)))
        .join(User, User.id == SubscriptionOrder.user_id)
        .filter(
            SubscriptionOrder.status == "paid",
            User.is_admin.is_(False),
            User.subscription_status == "active",
            User.subscription_expires_at.is_not(None),
            User.subscription_expires_at >= now,
        )
        .scalar()
    )
    active_trial_users = max(users_with_active_subscription - active_paid_users, 0)
    lost_subscriptions_last_30 = users_common_query.filter(
        User.subscription_expires_at.is_not(None),
        User.subscription_expires_at < now,
        User.subscription_expires_at >= last_30_days,
    ).count()
    churn_base = users_with_active_subscription + lost_subscriptions_last_30

    financial_summary = {
        "paid_revenue_last_30_cents": paid_revenue_last_30_cents,
        "paid_revenue_prev_30_cents": paid_revenue_prev_30_cents,
        "paid_revenue_delta_percent": _percent(
            paid_revenue_last_30_cents - paid_revenue_prev_30_cents,
            paid_revenue_prev_30_cents or 1,
        )
        if paid_revenue_prev_30_cents > 0
        else 0.0,
        "pending_revenue_cents": pending_revenue_cents,
        "mrr_cents": mrr_cents,
        "active_paid_users": active_paid_users,
        "active_trial_users": active_trial_users,
        "lost_subscriptions_last_30": lost_subscriptions_last_30,
        "churn_rate_last_30": _percent(lost_subscriptions_last_30, churn_base),
    }

    members_with_suppliers = _safe_int(
        db.session.query(func.count(func.distinct(Supplier.owner_user_id)))
        .join(User, User.id == Supplier.owner_user_id)
        .filter(User.is_admin.is_(False))
        .scalar()
    )
    members_with_messages = _safe_int(
        db.session.query(func.count(func.distinct(MessageHistory.owner_user_id)))
        .join(User, User.id == MessageHistory.owner_user_id)
        .filter(User.is_admin.is_(False))
        .scalar()
    )
    members_with_paid_orders = _safe_int(
        db.session.query(func.count(func.distinct(SubscriptionOrder.user_id)))
        .join(User, User.id == SubscriptionOrder.user_id)
        .filter(
            User.is_admin.is_(False),
            SubscriptionOrder.status == "paid",
        )
        .scalar()
    )

    funnel_steps_raw = [
        ("Cadastrados", users_common),
        ("Com fornecedores", members_with_suppliers),
        ("Com envios", members_with_messages),
        ("Com assinatura paga", members_with_paid_orders),
    ]
    funnel_steps: list[dict[str, int | float | str]] = []
    previous_count = users_common
    for label, count in funnel_steps_raw:
        funnel_steps.append(
            {
                "label": label,
                "count": count,
                "conversion_total": _percent(count, users_common),
                "conversion_prev": _percent(count, previous_count) if previous_count else 0.0,
            }
        )
        previous_count = count

    messages_last_24h = MessageHistory.query.filter(MessageHistory.sent_at >= last_24h).count()
    messages_last_7d = MessageHistory.query.filter(MessageHistory.sent_at >= last_7_days).count()
    delivered_last_7d = MessageHistory.query.filter(
        MessageHistory.sent_at >= last_7_days,
        MessageHistory.status == "enviado",
    ).count()
    failed_last_7d = MessageHistory.query.filter(
        MessageHistory.sent_at >= last_7_days,
        MessageHistory.status == "erro_envio",
    ).count()
    simulated_last_7d = MessageHistory.query.filter(
        MessageHistory.sent_at >= last_7_days,
        MessageHistory.status == "simulado",
    ).count()

    health_by_user_rows = (
        db.session.query(
            User.id.label("user_id"),
            User.username.label("username"),
            func.count(MessageHistory.id).label("total_count"),
            func.coalesce(func.sum(case((MessageHistory.status == "enviado", 1), else_=0)), 0).label("success_count"),
            func.coalesce(func.sum(case((MessageHistory.status == "erro_envio", 1), else_=0)), 0).label("failed_count"),
            func.max(MessageHistory.sent_at).label("last_sent_at"),
        )
        .join(
            MessageHistory,
            and_(
                MessageHistory.owner_user_id == User.id,
                MessageHistory.sent_at >= last_7_days,
            ),
            isouter=True,
        )
        .filter(User.is_admin.is_(False))
        .group_by(User.id, User.username, User.created_at)
        .order_by(func.count(MessageHistory.id).desc(), User.created_at.desc())
        .limit(12)
        .all()
    )
    health_by_user: list[dict[str, object]] = []
    for row in health_by_user_rows:
        sent_total = _safe_int(row.success_count) + _safe_int(row.failed_count)
        health_by_user.append(
            {
                "user_id": row.user_id,
                "username": row.username,
                "total_count": _safe_int(row.total_count),
                "success_count": _safe_int(row.success_count),
                "failed_count": _safe_int(row.failed_count),
                "failure_rate": _percent(_safe_int(row.failed_count), sent_total),
                "last_sent_at": row.last_sent_at,
            }
        )

    delivery_health = {
        "messages_last_24h": messages_last_24h,
        "messages_last_7d": messages_last_7d,
        "delivered_last_7d": delivered_last_7d,
        "failed_last_7d": failed_last_7d,
        "simulated_last_7d": simulated_last_7d,
        "success_rate_last_7d": _percent(delivered_last_7d, delivered_last_7d + failed_last_7d),
        "health_by_user": health_by_user,
    }

    supplier_stats_subq = (
        db.session.query(
            Supplier.owner_user_id.label("user_id"),
            func.count(Supplier.id).label("suppliers_count"),
        )
        .group_by(Supplier.owner_user_id)
        .subquery()
    )
    template_stats_subq = (
        db.session.query(
            MessageTemplate.owner_user_id.label("user_id"),
            func.count(MessageTemplate.id).label("templates_count"),
        )
        .group_by(MessageTemplate.owner_user_id)
        .subquery()
    )
    message_stats_subq = (
        db.session.query(
            MessageHistory.owner_user_id.label("user_id"),
            func.count(MessageHistory.id).label("messages_count"),
            func.coalesce(
                func.sum(
                    case(
                        (MessageHistory.sent_at >= last_30_days, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("messages_30d_count"),
            func.max(MessageHistory.sent_at).label("last_sent_at"),
        )
        .group_by(MessageHistory.owner_user_id)
        .subquery()
    )

    top_user_rows = (
        db.session.query(
            User,
            func.coalesce(supplier_stats_subq.c.suppliers_count, 0).label("suppliers_count"),
            func.coalesce(template_stats_subq.c.templates_count, 0).label("templates_count"),
            func.coalesce(message_stats_subq.c.messages_count, 0).label("messages_count"),
            func.coalesce(message_stats_subq.c.messages_30d_count, 0).label("messages_30d_count"),
            message_stats_subq.c.last_sent_at.label("last_sent_at"),
        )
        .outerjoin(supplier_stats_subq, supplier_stats_subq.c.user_id == User.id)
        .outerjoin(template_stats_subq, template_stats_subq.c.user_id == User.id)
        .outerjoin(message_stats_subq, message_stats_subq.c.user_id == User.id)
        .filter(User.is_admin.is_(False))
        .order_by(
            func.coalesce(message_stats_subq.c.messages_count, 0).desc(),
            User.created_at.desc(),
        )
        .limit(12)
        .all()
    )
    top_users: list[dict[str, object]] = []
    for row in top_user_rows:
        user = row[0]
        top_users.append(
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "subscription_status": user.subscription_status,
                "subscription_expires_at": user.subscription_expires_at,
                "suppliers_count": _safe_int(row.suppliers_count),
                "templates_count": _safe_int(row.templates_count),
                "messages_count": _safe_int(row.messages_count),
                "messages_30d_count": _safe_int(row.messages_30d_count),
                "last_sent_at": row.last_sent_at,
            }
        )

    expiring_users = (
        User.query.filter(
            User.is_admin.is_(False),
            User.subscription_status == "active",
            User.subscription_expires_at.is_not(None),
            User.subscription_expires_at >= now,
            User.subscription_expires_at <= expiring_cutoff,
        )
        .order_by(User.subscription_expires_at.asc())
        .limit(8)
        .all()
    )
    stale_active_users = (
        db.session.query(
            User,
            message_stats_subq.c.last_sent_at.label("last_sent_at"),
        )
        .outerjoin(message_stats_subq, message_stats_subq.c.user_id == User.id)
        .filter(
            User.is_admin.is_(False),
            User.subscription_status == "active",
            User.subscription_expires_at.is_not(None),
            User.subscription_expires_at >= now,
            or_(
                message_stats_subq.c.last_sent_at.is_(None),
                message_stats_subq.c.last_sent_at < last_30_days,
            ),
        )
        .order_by(User.subscription_expires_at.asc())
        .limit(8)
        .all()
    )
    high_failure_users = [
        row for row in health_by_user if row["failed_count"] >= 3 and float(row["failure_rate"]) >= 40.0
    ][:8]

    alert_items = [
        {"label": "Usuarios suspensos", "count": users_suspended},
        {"label": "Pedidos pendentes", "count": pending_orders_count},
        {"label": "Pedidos pendentes > 2 dias", "count": pending_orders_over_2_days},
        {"label": "Assinaturas vencidas", "count": users_with_expired_subscription},
        {"label": "Risco de expiracao (7 dias)", "count": len(expiring_users)},
        {"label": "Usuarios ativos sem envios 30d", "count": len(stale_active_users)},
        {"label": "Usuarios com alta falha de envio", "count": len(high_failure_users)},
    ]

    moderation_users = User.query.filter_by(is_admin=False).order_by(User.created_at.desc()).limit(12).all()

    audit_action = request.args.get("audit_action", "").strip()
    admin_actor = aliased(User)
    target_user = aliased(User)
    audit_query = (
        db.session.query(
            AdminAuditLog,
            admin_actor.username.label("admin_username"),
            target_user.username.label("target_username"),
        )
        .join(admin_actor, AdminAuditLog.admin_user_id == admin_actor.id)
        .outerjoin(target_user, AdminAuditLog.target_user_id == target_user.id)
    )
    if audit_action:
        audit_query = audit_query.filter(AdminAuditLog.action == audit_action)
    audit_logs = audit_query.order_by(AdminAuditLog.created_at.desc()).limit(40).all()
    audit_actions = [
        row[0]
        for row in db.session.query(AdminAuditLog.action).distinct().order_by(AdminAuditLog.action.asc()).all()
        if row[0]
    ]

    uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    sqlite_path = None
    sqlite_size_kb = None
    sqlite_integrity = {"status": "nao_disponivel", "message": "Cheque de integridade disponivel apenas para SQLite."}
    if uri.startswith("sqlite:///"):
        raw_path = uri.replace("sqlite:///", "", 1)
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = Path(current_app.instance_path) / candidate
        sqlite_path = str(candidate.resolve())
        if candidate.exists():
            sqlite_size_kb = round(candidate.stat().st_size / 1024, 2)
        try:
            integrity_row = db.session.execute(text("PRAGMA integrity_check")).first()
            integrity_value = str(integrity_row[0]).strip() if integrity_row else "unknown"
            sqlite_integrity = {
                "status": "ok" if integrity_value.lower() == "ok" else "error",
                "message": integrity_value,
            }
        except Exception as exc:
            sqlite_integrity = {
                "status": "error",
                "message": f"Falha ao executar PRAGMA integrity_check: {exc}",
            }

    db_health = {
        "uri": uri,
        "sqlite_path": sqlite_path,
        "sqlite_size_kb": sqlite_size_kb,
        "sqlite_integrity": sqlite_integrity,
        "table_counts": {
            "users": User.query.count(),
            "suppliers": Supplier.query.count(),
            "message_templates": MessageTemplate.query.count(),
            "message_history": MessageHistory.query.count(),
            "subscription_orders": SubscriptionOrder.query.count(),
            "signup_verifications": SignupVerification.query.count(),
            "password_reset_verifications": PasswordResetVerification.query.count(),
            "admin_audit_logs": AdminAuditLog.query.count(),
        },
        "active_whatsapp_managers": len(current_app.extensions.get("whatsapp_managers", {})),
    }

    search_query = request.args.get("q", "").strip()
    user_search_results: list[User] = []
    supplier_search_results = []
    if search_query:
        pattern = f"%{search_query.lower()}%"
        raw_pattern = f"%{search_query}%"
        user_search_results = (
            User.query.filter(
                User.is_admin.is_(False),
                or_(
                    func.lower(User.username).like(pattern),
                    func.lower(User.email).like(pattern),
                    func.lower(func.coalesce(User.phone, "")).like(pattern),
                ),
            )
            .order_by(User.created_at.desc())
            .limit(20)
            .all()
        )
        supplier_search_results = (
            db.session.query(
                Supplier,
                User.username.label("owner_username"),
                User.id.label("owner_user_id"),
            )
            .join(User, Supplier.owner_user_id == User.id)
            .filter(
                or_(
                    func.lower(Supplier.name).like(pattern),
                    func.lower(func.coalesce(Supplier.company, "")).like(pattern),
                    func.lower(func.coalesce(Supplier.email, "")).like(pattern),
                    func.coalesce(Supplier.phone, "").like(raw_pattern),
                )
            )
            .order_by(Supplier.created_at.desc())
            .limit(20)
            .all()
        )

    support_user_id_raw = request.args.get("support_user_id", "").strip()
    support_user = None
    if support_user_id_raw.isdigit():
        support_user = User.query.filter_by(id=int(support_user_id_raw), is_admin=False).first()
    elif len(user_search_results) == 1:
        support_user = user_search_results[0]

    support_profile = None
    if support_user:
        recent_messages = (
            db.session.query(
                MessageHistory,
                Supplier.name.label("supplier_name"),
            )
            .join(Supplier, MessageHistory.supplier_id == Supplier.id, isouter=True)
            .filter(MessageHistory.owner_user_id == support_user.id)
            .order_by(MessageHistory.sent_at.desc())
            .limit(8)
            .all()
        )
        recent_orders = (
            SubscriptionOrder.query.filter_by(user_id=support_user.id)
            .order_by(SubscriptionOrder.created_at.desc())
            .limit(8)
            .all()
        )
        recent_audits = (
            db.session.query(
                AdminAuditLog,
                admin_actor.username.label("admin_username"),
            )
            .join(admin_actor, AdminAuditLog.admin_user_id == admin_actor.id)
            .filter(AdminAuditLog.target_user_id == support_user.id)
            .order_by(AdminAuditLog.created_at.desc())
            .limit(8)
            .all()
        )
        manager_key = f"user_{support_user.id}_{_username_key(support_user.username)}"
        manager = current_app.extensions.get("whatsapp_managers", {}).get(manager_key)
        manager_state = manager.get_state() if manager else {"status": "disconnected", "message": "Sessao nao inicializada."}
        support_profile = {
            "user": support_user,
            "stats": {
                "suppliers_count": Supplier.query.filter_by(owner_user_id=support_user.id).count(),
                "templates_count": MessageTemplate.query.filter_by(owner_user_id=support_user.id).count(),
                "messages_count": MessageHistory.query.filter_by(owner_user_id=support_user.id).count(),
                "messages_30d_count": MessageHistory.query.filter(
                    MessageHistory.owner_user_id == support_user.id,
                    MessageHistory.sent_at >= last_30_days,
                ).count(),
                "pending_orders_count": SubscriptionOrder.query.filter_by(user_id=support_user.id, status="pending").count(),
            },
            "recent_messages": recent_messages,
            "recent_orders": recent_orders,
            "recent_audits": recent_audits,
            "whatsapp_state": manager_state,
        }

    return render_template(
        "admin_dashboard.html",
        now=now,
        admin_return_to=admin_return_to,
        users_total=users_total,
        users_admin=users_admin,
        users_common=users_common,
        users_suspended=users_suspended,
        users_renewed=users_renewed,
        users_with_active_subscription=users_with_active_subscription,
        users_with_expired_subscription=users_with_expired_subscription,
        pending_orders_count=pending_orders_count,
        pending_orders=pending_orders,
        financial_summary=financial_summary,
        funnel_steps=funnel_steps,
        delivery_health=delivery_health,
        top_users=top_users,
        alert_items=alert_items,
        expiring_users=expiring_users,
        stale_active_users=stale_active_users,
        high_failure_users=high_failure_users,
        moderation_users=moderation_users,
        audit_logs=audit_logs,
        audit_actions=audit_actions,
        audit_action=audit_action,
        db_health=db_health,
        search_query=search_query,
        user_search_results=user_search_results,
        supplier_search_results=supplier_search_results,
        support_profile=support_profile,
        payment_test_mode=_payment_test_mode_enabled(),
        latest_users=latest_users,
    )


@bp.get("/admin/usuarios")
@admin_required
def admin_users():
    renewed_cutoff = server_now() - timedelta(days=30)
    admin_return_to = _admin_return_to_current()
    users = User.query.order_by(User.created_at.desc()).all()
    renewed_users = (
        User.query.filter(
            User.subscription_last_payment_at.is_not(None),
            User.subscription_last_payment_at >= renewed_cutoff,
        )
        .order_by(User.subscription_last_payment_at.desc())
        .all()
    )
    pending_orders = (
        SubscriptionOrder.query.filter_by(status="pending")
        .order_by(SubscriptionOrder.created_at.desc())
        .limit(100)
        .all()
    )
    master_username = (current_app.config.get("MASTER_USERNAME") or "").strip()
    return render_template(
        "admin_users.html",
        users=users,
        renewed_users=renewed_users,
        pending_orders=pending_orders,
        payment_test_mode=_payment_test_mode_enabled(),
        master_username=master_username,
        admin_return_to=admin_return_to,
    )


@bp.post("/admin/usuarios/<int:user_id>/remover")
@admin_required
def remove_user(user_id: int):
    user = User.query.get_or_404(user_id)
    master_username = (current_app.config.get("MASTER_USERNAME") or "").strip()
    if master_username and user.username.lower() == master_username.lower():
        flash(f"Nao e permitido remover o usuario master ({master_username}).", "error")
        return redirect(url_for("main.admin_users"))
    if user.id == g.current_user.id:
        flash("Nao e permitido remover o proprio usuario logado.", "error")
        return redirect(url_for("main.admin_users"))

    MessageHistory.query.filter_by(owner_user_id=user.id).delete(synchronize_session=False)
    MessageTemplate.query.filter_by(owner_user_id=user.id).delete(synchronize_session=False)
    Supplier.query.filter_by(owner_user_id=user.id).delete(synchronize_session=False)
    SubscriptionOrder.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    PasswordResetVerification.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    _record_admin_audit("remove_user", target_user_id=user.id, details=f"Usuario removido: {user.username}")
    _force_member_reauth(user.id)
    db.session.delete(user)
    db.session.commit()
    flash("Usuario removido com sucesso.", "success")
    return _redirect_admin_return("main.admin_users")


@bp.post("/admin/usuarios/<int:user_id>/revogar-assinatura")
@admin_required
def revoke_user_subscription(user_id: int):
    user = User.query.get_or_404(user_id)
    master_username = (current_app.config.get("MASTER_USERNAME") or "").strip()
    if master_username and user.username.lower() == master_username.lower():
        flash(f"Nao e permitido revogar assinatura do usuario master ({master_username}).", "error")
        return redirect(url_for("main.admin_users"))
    if user.id == g.current_user.id:
        flash("Nao e permitido revogar a propria assinatura enquanto logado.", "error")
        return redirect(url_for("main.admin_users"))

    user.subscription_status = "inactive"
    user.subscription_expires_at = None
    _record_admin_audit("revoke_subscription", target_user_id=user.id, details=f"Assinatura revogada: {user.username}")
    _force_member_reauth(user.id)
    db.session.commit()
    flash("Assinatura revogada com sucesso.", "success")
    return _redirect_admin_return("main.admin_users")


@bp.post("/admin/usuarios/<int:user_id>/suspender")
@admin_required
def suspend_user(user_id: int):
    user = User.query.get_or_404(user_id)
    master_username = (current_app.config.get("MASTER_USERNAME") or "").strip()
    if user.is_admin or (master_username and user.username.lower() == master_username.lower()):
        flash("Nao e permitido suspender este usuario.", "error")
        return _redirect_admin_return("main.admin_dashboard")
    if user.id == g.current_user.id:
        flash("Nao e permitido suspender o proprio usuario logado.", "error")
        return _redirect_admin_return("main.admin_dashboard")

    user.subscription_status = "suspended"
    _record_admin_audit("suspend_user", target_user_id=user.id, details=f"Conta suspensa: {user.username}")
    _force_member_reauth(user.id)
    db.session.commit()
    flash("Usuario suspenso com sucesso.", "success")
    return _redirect_admin_return("main.admin_dashboard")


@bp.post("/admin/usuarios/<int:user_id>/reativar")
@admin_required
def reactivate_user(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash("Usuario admin nao precisa de reativacao.", "error")
        return _redirect_admin_return("main.admin_dashboard")

    if user.subscription_expires_at and user.subscription_expires_at >= server_now():
        user.subscription_status = "active"
    else:
        user.subscription_status = "inactive"
    _record_admin_audit("reactivate_user", target_user_id=user.id, details=f"Conta reativada: {user.username}")
    db.session.commit()
    flash("Usuario reativado.", "success")
    return _redirect_admin_return("main.admin_dashboard")


@bp.post("/admin/usuarios/<int:user_id>/forcar-logout")
@admin_required
def force_user_logout(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash("Nao e permitido forcar logout de admin por essa acao.", "error")
        return _redirect_admin_return("main.admin_dashboard")

    _force_member_reauth(user.id)
    _record_admin_audit("force_logout", target_user_id=user.id, details=f"Forcado novo login: {user.username}")
    db.session.commit()
    flash("Logout forcado aplicado. O usuario precisara autenticar novamente.", "success")
    return _redirect_admin_return("main.admin_dashboard")


@bp.post("/admin/usuarios/<int:user_id>/resetar-whatsapp")
@admin_required
def reset_user_whatsapp_session(user_id: int):
    user = User.query.get_or_404(user_id)
    manager_key = f"user_{user.id}_{_username_key(user.username)}"
    managers = current_app.extensions.setdefault("whatsapp_managers", {})
    manager = managers.pop(manager_key, None)
    if manager:
        try:
            manager.stop()
        except Exception:
            pass

    profile_dir = Path(current_app.instance_path) / "whatsapp_sessions" / manager_key
    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)

    _record_admin_audit("reset_whatsapp_session", target_user_id=user.id, details=f"Sessao WhatsApp resetada: {user.username}")
    db.session.commit()
    flash("Sessao WhatsApp do usuario foi resetada.", "success")
    return _redirect_admin_return("main.admin_dashboard")


@bp.get("/admin/banco")
@admin_required
def admin_database():
    uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    sqlite_path = None
    sqlite_size_kb = None
    if uri.startswith("sqlite:///"):
        raw_path = uri.replace("sqlite:///", "", 1)
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = Path(current_app.instance_path) / candidate
        sqlite_path = str(candidate.resolve())
        if candidate.exists():
            sqlite_size_kb = round(candidate.stat().st_size / 1024, 2)

    table_counts = {
        "users": User.query.count(),
        "suppliers": Supplier.query.count(),
        "message_templates": MessageTemplate.query.count(),
        "message_history": MessageHistory.query.count(),
        "subscription_orders": SubscriptionOrder.query.count(),
        "signup_verifications": SignupVerification.query.count(),
        "password_reset_verifications": PasswordResetVerification.query.count(),
        "admin_audit_logs": AdminAuditLog.query.count(),
    }
    return render_template(
        "admin_database.html",
        uri=uri,
        sqlite_path=sqlite_path,
        sqlite_size_kb=sqlite_size_kb,
        table_counts=table_counts,
    )


@bp.get("/configuracoes")
@subscription_required
def settings():
    user = g.member_user
    if not _member_has_feature(user, "can_whatsapp_session"):
        flash("Seu plano atual nao inclui integracao com WhatsApp.", "error")
        return redirect(url_for("main.subscription"))
    provider = whatsapp_provider()
    return render_template(
        "settings.html",
        whatsapp_provider=provider,
        whatsapp_provider_label=whatsapp_provider_label(provider),
        whatsapp_is_pywhatkit=provider == "pywhatkit",
        whatsapp_test_default_phone=user.phone or "",
        pywhatkit_wait_time=current_app.config.get("WHATSAPP_PYWHATKIT_WAIT_TIME", 15),
        pywhatkit_close_time=current_app.config.get("WHATSAPP_PYWHATKIT_CLOSE_TIME", 3),
        pywhatkit_close_tab=bool(current_app.config.get("WHATSAPP_PYWHATKIT_CLOSE_TAB", True)),
    )


@bp.post("/configuracoes/whatsapp/teste")
@subscription_required
def send_whatsapp_test_message():
    user = g.member_user
    if not _member_has_feature(user, "can_whatsapp_session"):
        flash("Seu plano atual nao inclui integracao com WhatsApp.", "error")
        return redirect(url_for("main.subscription"))

    app_env = (current_app.config.get("APP_ENV") or "development").strip().lower()
    if app_env != "development":
        flash("Teste de envio disponivel apenas no ambiente development.", "error")
        return redirect(url_for("main.settings"))

    provider = whatsapp_provider()
    if provider != "pywhatkit":
        flash("Envio de teste disponivel apenas quando WHATSAPP_PROVIDER=pywhatkit.", "error")
        return redirect(url_for("main.settings"))

    session_ready, session_error = _ensure_pywhatkit_session_ready()
    if not session_ready:
        flash(session_error or "Sessao WhatsApp indisponivel.", "error")
        return redirect(url_for("main.settings"))
    manager = _get_whatsapp_manager()

    raw_phone = request.form.get("phone", "").strip()
    normalized_phone = normalize_user_phone(raw_phone)
    if not normalized_phone:
        flash("Informe um telefone valido no formato +55DDDNUMERO.", "error")
        return redirect(url_for("main.settings"))

    message = request.form.get("message", "").strip()
    if not message:
        flash("Informe a mensagem de teste.", "error")
        return redirect(url_for("main.settings"))

    sent, error_message = send_whatsapp_message(normalized_phone, message, session_manager=manager)
    if sent:
        flash("Mensagem de teste enviada via PyWhatKit.", "success")
    else:
        flash(f"Falha ao enviar teste via PyWhatKit: {error_message or 'erro desconhecido.'}", "error")
    return redirect(url_for("main.settings"))


@bp.get("/assinatura")
@member_required
def subscription():
    user = g.member_user
    plans = _subscription_plans()
    current_plan_key = _active_member_plan_key(user)
    for plan in plans:
        plan["amount_label"] = _format_currency_from_cents(int(plan["amount_cents"]), str(plan["currency"]))
        plan_key = str(plan["key"])
        plan["is_current"] = bool(_is_member_subscription_active(user) and current_plan_key == plan_key)
        plan["display_name"] = "Plano Atual" if plan["is_current"] else str(plan["name"])
        plan_limits = PLAN_FEATURES.get(plan_key, PLAN_FEATURES["starter"])
        supplier_limit = plan_limits.get("max_suppliers")
        template_limit = plan_limits.get("max_templates")
        bulk_limit = plan_limits.get("max_bulk_recipients")
        plan["feature_items"] = [
            (
                f"Ate {supplier_limit} fornecedores"
                if isinstance(supplier_limit, int)
                else "Fornecedores ilimitados"
            ),
            (
                f"Ate {template_limit} templates"
                if isinstance(template_limit, int)
                else "Templates ilimitados"
            ),
            (
                f"Ate {bulk_limit} destinatarios por envio"
                if isinstance(bulk_limit, int)
                else "Envio em lote ilimitado"
            ),
            "Importacao de contatos" if bool(plan_limits.get("can_import_contacts")) else "Sem importacao de contatos",
            "Integracao com WhatsApp" if bool(plan_limits.get("can_whatsapp_session")) else "Sem integracao com WhatsApp",
        ]
    orders = SubscriptionOrder.query.filter_by(user_id=user.id).order_by(SubscriptionOrder.created_at.desc()).limit(20).all()
    has_pending_order = any(order.status == "pending" for order in orders)
    return render_template(
        "subscription.html",
        plans=plans,
        subscription_active=_is_member_subscription_active(user),
        subscription_expires_at=user.subscription_expires_at,
        subscription_last_payment_at=user.subscription_last_payment_at,
        orders=orders,
        has_pending_order=has_pending_order,
    )


@bp.get("/assinatura/status")
@member_required
def subscription_status():
    user = g.member_user
    pending_orders_count = SubscriptionOrder.query.filter_by(user_id=user.id, status="pending").count()
    return jsonify(
        {
            "subscription_active": _is_member_subscription_active(user),
            "subscription_status": user.subscription_status,
            "subscription_expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
            "pending_orders_count": pending_orders_count,
        }
    )


@bp.post("/assinatura/comprar")
@member_required
def create_subscription_order():
    user = g.member_user
    plan_key = request.form.get("plan_key", "").strip().lower()
    selected_plan = _subscription_plan_by_key(plan_key)
    if selected_plan is None:
        flash("Plano invalido.", "error")
        return redirect(url_for("main.subscription"))

    pending_order = SubscriptionOrder.query.filter_by(user_id=user.id, status="pending").first()
    if pending_order:
        flash("Voce ja possui um pedido pendente. Confirme ou cancele antes de criar outro.", "error")
        return redirect(url_for("main.subscription"))

    order = SubscriptionOrder(
        user_id=user.id,
        plan_name=str(selected_plan["name"]),
        amount_cents=int(selected_plan["amount_cents"]),
        currency=str(selected_plan["currency"]),
        status="pending",
    )
    db.session.add(order)
    db.session.commit()
    flash(f"Pedido do plano {selected_plan['name']} criado. Agora confirme o pagamento para ativar a assinatura.", "success")
    return redirect(url_for("main.subscription"))


@bp.post("/admin/assinaturas/pedidos/<int:order_id>/confirmar")
@admin_required
def admin_confirm_subscription_order(order_id: int):
    if not _payment_test_mode_enabled():
        flash("Confirmacao manual de pagamento disponivel apenas em ambiente de teste.", "error")
        return redirect(url_for("main.admin_users"))

    order = SubscriptionOrder.query.filter_by(id=order_id).first_or_404()
    if order.status != "pending":
        flash("Somente pedidos pendentes podem ser confirmados.", "error")
        return redirect(url_for("main.admin_users"))

    _apply_paid_subscription_order(order)
    db.session.commit()
    flash("Pagamento confirmado e assinatura ativada.", "success")
    return redirect(url_for("main.admin_users"))


@bp.post("/webhooks/pagamento")
@csrf.exempt
def payment_webhook():
    if not _webhook_token_is_valid():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    raw_order_id = payload.get("order_id")
    status = str(payload.get("status", "")).strip().lower()
    if raw_order_id is None or not str(raw_order_id).isdigit():
        return jsonify({"error": "invalid_order_id"}), 400
    if status != "paid":
        return jsonify({"ok": True, "ignored": True}), 202

    order_id = int(raw_order_id)
    order = SubscriptionOrder.query.filter_by(id=order_id).first()
    if order is None:
        return jsonify({"error": "order_not_found"}), 404
    if order.status == "paid":
        return jsonify({"ok": True, "already_paid": True}), 200

    _apply_paid_subscription_order(order)
    db.session.commit()
    return jsonify({"ok": True}), 200


@bp.post("/assinatura/pedidos/<int:order_id>/cancelar")
@member_required
def cancel_subscription_order(order_id: int):
    user = g.member_user
    order = SubscriptionOrder.query.filter_by(id=order_id, user_id=user.id).first_or_404()
    if order.status != "pending":
        flash("Somente pedidos pendentes podem ser cancelados.", "error")
        return redirect(url_for("main.subscription"))

    order.status = "canceled"
    db.session.commit()
    flash("Pedido pendente cancelado.", "success")
    return redirect(url_for("main.subscription"))


@bp.post("/configuracoes/whatsapp/iniciar")
@api_subscription_required
def start_whatsapp_session():
    user = g.member_user
    if not _member_has_feature(user, "can_whatsapp_session"):
        return jsonify({"error": "plano_nao_permite", "message": "Seu plano atual nao permite integracao com WhatsApp."}), 403
    manager = _get_whatsapp_manager()
    manager.start()
    return jsonify(manager.get_state())


@bp.post("/configuracoes/whatsapp/parar")
@api_subscription_required
def stop_whatsapp_session():
    user = g.member_user
    if not _member_has_feature(user, "can_whatsapp_session"):
        return jsonify({"error": "plano_nao_permite", "message": "Seu plano atual nao permite integracao com WhatsApp."}), 403
    manager = _get_whatsapp_manager()
    manager.stop()
    return jsonify(manager.get_state())


@bp.get("/configuracoes/whatsapp/status")
@api_subscription_required
def whatsapp_status():
    user = g.member_user
    if not _member_has_feature(user, "can_whatsapp_session"):
        return jsonify({"error": "plano_nao_permite", "message": "Seu plano atual nao permite integracao com WhatsApp."}), 403
    manager = _get_whatsapp_manager()
    return jsonify(manager.get_state())


@bp.route("/fornecedores")
@subscription_required
def suppliers():
    owner_user_id = _member_owner_id()
    user = g.member_user
    query = request.args.get("q", "").strip()
    page = _get_page_arg()
    supplier_query = _supplier_query_with_search(owner_user_id, query)
    pagination = supplier_query.paginate(page=page, per_page=SUPPLIERS_PER_PAGE, error_out=False)
    supplier_limit = _member_plan_limit(user, "max_suppliers")
    suppliers_total = Supplier.query.filter_by(owner_user_id=owner_user_id).count()
    can_create_supplier = supplier_limit is None or suppliers_total < supplier_limit
    return render_template(
        "suppliers.html",
        suppliers=pagination.items,
        pagination=pagination,
        query=query,
        suppliers_total=suppliers_total,
        supplier_limit=supplier_limit,
        can_create_supplier=can_create_supplier,
    )


@bp.get("/fornecedores/exportar.csv")
@subscription_required
def export_suppliers():
    owner_user_id = _member_owner_id()
    query = request.args.get("q", "").strip()
    rows = _supplier_query_with_search(owner_user_id, query).all()
    payload = suppliers_to_csv_bytes(rows)
    return Response(
        payload,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=fornecedores.csv"},
    )


@bp.route("/fornecedores/novo", methods=["GET", "POST"])
@subscription_required
def create_supplier():
    owner_user_id = _member_owner_id()
    user = g.member_user
    supplier_limit = _member_plan_limit(user, "max_suppliers")
    suppliers_total = Supplier.query.filter_by(owner_user_id=owner_user_id).count()
    if supplier_limit is not None and suppliers_total >= supplier_limit:
        flash(f"Limite do plano atingido: {supplier_limit} fornecedores. Faca upgrade para continuar.", "error")
        return redirect(url_for("main.suppliers"))

    if request.method == "POST":
        payload = normalize_supplier_payload(request.form)
        if not payload["name"]:
            flash("Nome e obrigatorio.", "error")
            return render_template("supplier_form.html", title="Novo fornecedor", supplier=None, form_data=payload)
        if not payload["phone"] and not payload["email"]:
            flash("Informe telefone ou email para contato.", "error")
            return render_template("supplier_form.html", title="Novo fornecedor", supplier=None, form_data=payload)
        if not validate_email(payload["email"]):
            flash("Email invalido.", "error")
            return render_template("supplier_form.html", title="Novo fornecedor", supplier=None, form_data=payload)
        if has_duplicate_contact(payload["email"], payload["phone"], owner_user_id=owner_user_id):
            flash("Ja existe fornecedor com esse telefone ou email.", "error")
            return render_template("supplier_form.html", title="Novo fornecedor", supplier=None, form_data=payload)

        payload["owner_user_id"] = owner_user_id
        supplier = Supplier(**payload)
        db.session.add(supplier)
        db.session.commit()
        flash("Fornecedor criado com sucesso.", "success")
        return redirect(url_for("main.suppliers"))

    return render_template("supplier_form.html", title="Novo fornecedor", supplier=None, form_data={})


@bp.route("/fornecedores/<int:supplier_id>/editar", methods=["GET", "POST"])
@subscription_required
def edit_supplier(supplier_id: int):
    owner_user_id = _member_owner_id()
    supplier = Supplier.query.filter_by(id=supplier_id, owner_user_id=owner_user_id).first_or_404()
    if request.method == "POST":
        payload = normalize_supplier_payload(request.form)
        if not payload["name"]:
            flash("Nome e obrigatorio.", "error")
            return render_template("supplier_form.html", title="Editar fornecedor", supplier=supplier, form_data=payload)
        if not payload["phone"] and not payload["email"]:
            flash("Informe telefone ou email para contato.", "error")
            return render_template("supplier_form.html", title="Editar fornecedor", supplier=supplier, form_data=payload)
        if not validate_email(payload["email"]):
            flash("Email invalido.", "error")
            return render_template("supplier_form.html", title="Editar fornecedor", supplier=supplier, form_data=payload)
        if has_duplicate_contact(
            payload["email"],
            payload["phone"],
            owner_user_id=owner_user_id,
            supplier_id=supplier.id,
        ):
            flash("Ja existe fornecedor com esse telefone ou email.", "error")
            return render_template("supplier_form.html", title="Editar fornecedor", supplier=supplier, form_data=payload)

        supplier.name = payload["name"]
        supplier.company = payload["company"]
        supplier.phone = payload["phone"]
        supplier.email = payload["email"]
        supplier.notes = payload["notes"]
        db.session.commit()
        flash("Fornecedor atualizado.", "success")
        return redirect(url_for("main.suppliers"))

    return render_template("supplier_form.html", title="Editar fornecedor", supplier=supplier, form_data={})


@bp.post("/fornecedores/<int:supplier_id>/excluir")
@subscription_required
def delete_supplier(supplier_id: int):
    owner_user_id = _member_owner_id()
    supplier = Supplier.query.filter_by(id=supplier_id, owner_user_id=owner_user_id).first_or_404()
    db.session.delete(supplier)
    db.session.commit()
    flash("Fornecedor removido.", "success")
    return redirect(url_for("main.suppliers"))


@bp.route("/importar", methods=["GET", "POST"])
@subscription_required
def import_contacts():
    owner_user_id = _member_owner_id()
    user = g.member_user
    if not _member_has_feature(user, "can_import_contacts"):
        flash("Seu plano atual nao permite importacao. Faca upgrade para Pro ou Business.", "error")
        return redirect(url_for("main.subscription"))

    supplier_limit = _member_plan_limit(user, "max_suppliers")
    result = None
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Selecione um arquivo CSV ou XLSX.", "error")
            return render_template("import.html", result=result)

        try:
            file_bytes = file.read()
            if len(file_bytes) > 5 * 1024 * 1024:
                flash("Arquivo muito grande. Limite: 5MB.", "error")
                return render_template("import.html", result=result)

            rows = parse_rows_from_file(file.filename, file_bytes)
            existing_suppliers = Supplier.query.filter_by(owner_user_id=owner_user_id).all()
            existing_total = len(existing_suppliers)
            if supplier_limit is not None and existing_total >= supplier_limit:
                flash(f"Limite de fornecedores do plano atingido ({supplier_limit}).", "error")
                return render_template("import.html", result=result)

            created, result = import_suppliers_from_rows(rows, existing_suppliers)
            if supplier_limit is not None:
                available_slots = max(supplier_limit - existing_total, 0)
                if len(created) > available_slots:
                    blocked_count = len(created) - available_slots
                    created = created[:available_slots]
                    result.imported = len(created)
                    result.skipped += blocked_count
                    result.errors.append(
                        f"Limite do plano atingido: {blocked_count} contato(s) nao foram importados."
                    )
            for supplier in created:
                supplier.owner_user_id = owner_user_id
                db.session.add(supplier)
            db.session.commit()

            flash(
                f"Importacao finalizada: {result.imported} novos, {result.skipped} ignorados.",
                "success",
            )
        except ValueError as exc:
            flash(str(exc), "error")
        except Exception:
            flash("Falha ao importar arquivo.", "error")

    return render_template("import.html", result=result)


@bp.route("/mensagens", methods=["GET", "POST"])
@subscription_required
def messages():
    owner_user_id = _member_owner_id()
    user = g.member_user
    template_limit = _member_plan_limit(user, "max_templates")
    bulk_limit = _member_plan_limit(user, "max_bulk_recipients")
    provider = whatsapp_provider()
    is_pywhatkit = provider == "pywhatkit"
    provider_label = whatsapp_provider_label(provider)
    whatsapp_session_connected = True
    whatsapp_session_message = None

    if is_pywhatkit:
        status, state_message = _pywhatkit_session_state(start_if_disconnected=False)
        whatsapp_session_connected = status == "connected"
        if not whatsapp_session_connected:
            whatsapp_session_message = state_message or "Sessao WhatsApp nao conectada. Abra Configuracoes e inicie o login."
    expects_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def _response_for_send(message: str, category: str = "success"):
        if expects_json:
            return jsonify({"ok": category == "success", "category": category, "message": message})
        flash(message, category)
        return redirect(url_for("main.messages"))

    if request.method == "POST":
        action = request.form.get("action")

        if action == "create_template":
            name = request.form.get("template_name", "").strip()
            body = request.form.get("template_body", "").strip()
            existing_templates_count = MessageTemplate.query.filter_by(owner_user_id=owner_user_id).count()
            if template_limit is not None and existing_templates_count >= template_limit:
                flash(f"Limite de templates do plano atingido ({template_limit}).", "error")
                return redirect(url_for("main.messages") + "#tab-templates")
            if not name or not body:
                flash("Nome e conteudo do template sao obrigatorios.", "error")
            else:
                existing = MessageTemplate.query.filter_by(owner_user_id=owner_user_id, name=name).first()
                if existing:
                    flash("Ja existe um template com esse nome.", "error")
                else:
                    db.session.add(MessageTemplate(name=name, body=body, owner_user_id=owner_user_id))
                    try:
                        db.session.commit()
                        flash("Template criado.", "success")
                    except IntegrityError:
                        db.session.rollback()
                        flash("Nome de template ja utilizado. Escolha outro nome.", "error")
            return redirect(url_for("main.messages") + "#tab-templates")

        if action == "send_messages":
            selected_ids = request.form.getlist("supplier_ids")
            template_id = request.form.get("template_id", "").strip()
            custom_body = request.form.get("custom_body", "").strip()
            produto = request.form.get("produto", "").strip()

            if not selected_ids:
                return _response_for_send("Selecione ao menos um fornecedor.", "error")

            unique_ids: set[int] = set()
            for raw_id in selected_ids:
                if raw_id.isdigit():
                    unique_ids.add(int(raw_id))

            if bulk_limit is not None and len(unique_ids) > bulk_limit:
                return _response_for_send(f"Seu plano permite ate {bulk_limit} destinatarios por envio.", "error")

            template = None
            template_body = ""
            if template_id:
                if not template_id.isdigit():
                    return _response_for_send("Template invalido.", "error")
                template = MessageTemplate.query.filter_by(id=int(template_id), owner_user_id=owner_user_id).first()
                if template:
                    template_body = template.body

            if custom_body:
                template_body = custom_body

            if not template_body:
                return _response_for_send("Selecione um template ou escreva uma mensagem customizada.", "error")

            if is_pywhatkit:
                session_ready, session_error = _ensure_pywhatkit_session_ready()
                if not session_ready:
                    if expects_json:
                        return jsonify(
                            {
                                "ok": False,
                                "category": "error",
                                "message": session_error or "Sessao WhatsApp indisponivel.",
                                "redirect_url": url_for("main.settings"),
                            }
                        )
                    flash(session_error or "Sessao WhatsApp indisponivel.", "error")
                    return redirect(url_for("main.settings"))
                manager = _get_whatsapp_manager()
            else:
                manager = None

            sent_count = 0
            failed_count = 0
            skipped_count = 0

            for supplier_id in unique_ids:
                supplier = Supplier.query.filter_by(id=supplier_id, owner_user_id=owner_user_id).first()
                if not supplier:
                    skipped_count += 1
                    continue
                if not supplier.phone and not supplier.email:
                    skipped_count += 1
                    continue

                content = render_message(
                    template_body,
                    {
                        "name": supplier.name,
                        "company": supplier.company or "",
                        "phone": supplier.phone or "",
                        "email": supplier.email or "",
                    },
                    {"produto": produto},
                )
                status = "simulado"
                if is_pywhatkit:
                    if not supplier.phone:
                        skipped_count += 1
                        continue
                    delivered, _error = send_whatsapp_message(
                        supplier.phone,
                        content,
                        session_manager=manager,
                    )
                    if delivered:
                        status = "enviado"
                        sent_count += 1
                    else:
                        status = "erro_envio"
                        failed_count += 1
                else:
                    sent_count += 1
                db.session.add(
                    MessageHistory(
                        supplier_id=supplier.id,
                        template_id=template.id if template else None,
                        content=content,
                        status=status,
                        owner_user_id=owner_user_id,
                    )
                )

            db.session.commit()
            if is_pywhatkit:
                return _response_for_send(
                    (
                        f"Envio via {provider_label}: {sent_count} enviadas, "
                        f"{failed_count} com falha, {skipped_count} ignoradas."
                    ),
                    "success" if failed_count == 0 else "error",
                )
            return _response_for_send(f"Mensagens registradas: {sent_count}. Ignoradas: {skipped_count}.", "success")

    templates = MessageTemplate.query.filter_by(owner_user_id=owner_user_id).order_by(MessageTemplate.created_at.desc()).all()
    suppliers = Supplier.query.filter_by(owner_user_id=owner_user_id).order_by(Supplier.name.asc()).all()
    return render_template(
        "messages.html",
        templates=templates,
        suppliers=suppliers,
        template_limit=template_limit,
        bulk_limit=bulk_limit,
        whatsapp_provider=provider,
        whatsapp_provider_label=provider_label,
        whatsapp_is_pywhatkit=is_pywhatkit,
        whatsapp_session_connected=whatsapp_session_connected,
        whatsapp_session_message=whatsapp_session_message,
    )


@bp.post("/mensagens/templates/<int:template_id>/excluir")
@subscription_required
def delete_template(template_id: int):
    owner_user_id = _member_owner_id()
    template = MessageTemplate.query.filter_by(id=template_id, owner_user_id=owner_user_id).first_or_404()
    detached_count = MessageHistory.query.filter_by(owner_user_id=owner_user_id, template_id=template.id).update(
        {"template_id": None},
        synchronize_session=False,
    )
    db.session.delete(template)
    db.session.commit()
    flash(
        f"Template removido. Historicos desvinculados: {detached_count}.",
        "success",
    )
    return redirect(url_for("main.messages") + "#tab-templates")


@bp.route("/historico")
@subscription_required
def history():
    owner_user_id = _member_owner_id()
    status_filter = request.args.get("status", "").strip().lower()
    page = _get_page_arg()

    query = MessageHistory.query.filter_by(owner_user_id=owner_user_id).order_by(MessageHistory.sent_at.desc())
    if status_filter:
        query = query.filter(MessageHistory.status == status_filter)

    pagination = query.paginate(page=page, per_page=HISTORY_PER_PAGE, error_out=False)
    statuses = [
        row[0]
        for row in db.session.query(MessageHistory.status).filter(MessageHistory.owner_user_id == owner_user_id).distinct().all()
        if row[0]
    ]
    return render_template(
        "history.html",
        rows=pagination.items,
        pagination=pagination,
        status_filter=status_filter,
        statuses=sorted(statuses),
    )


@bp.post("/historico/<int:history_id>/excluir")
@subscription_required
def delete_history_entry(history_id: int):
    owner_user_id = _member_owner_id()
    entry = MessageHistory.query.filter_by(id=history_id, owner_user_id=owner_user_id).first_or_404()
    db.session.delete(entry)
    db.session.commit()
    flash("Registro removido do historico.", "success")
    return _history_redirect_target()


@bp.post("/historico/limpar")
@subscription_required
def clear_history():
    owner_user_id = _member_owner_id()
    deleted_count = MessageHistory.query.filter_by(owner_user_id=owner_user_id).delete()
    db.session.commit()
    flash(f"Historico limpo. Registros removidos: {deleted_count}.", "success")
    return redirect(url_for("main.history"))


@bp.get("/historico/exportar.csv")
@subscription_required
def export_history():
    owner_user_id = _member_owner_id()
    status_filter = request.args.get("status", "").strip().lower()
    query = MessageHistory.query.filter_by(owner_user_id=owner_user_id).order_by(MessageHistory.sent_at.desc())
    if status_filter:
        query = query.filter(MessageHistory.status == status_filter)
    rows = query.all()
    payload = history_to_csv_bytes(rows)
    return Response(
        payload,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=historico_mensagens.csv"},
    )


def _abacatepay_mode() -> str:
    return (current_app.config.get("ABACATEPAY_MODE") or "mock").strip().lower()


def _abacatepay_api_key() -> str:
    return (current_app.config.get("ABACATEPAY_API_KEY") or "").strip()


def _abacatepay_configured() -> bool:
    return bool(_abacatepay_api_key())


def _checkout_owner_or_404(checkout_id: int) -> "SubscriptionCheckout":
    """Retorna o checkout garantindo que pertence ao usuário logado."""
    checkout = SubscriptionCheckout.query.filter_by(
        id=checkout_id,
        user_id=g.member_user.id,
    ).first_or_404()
    return checkout


def _apply_paid_checkout(checkout: "SubscriptionCheckout") -> None:
    if checkout.status == "PAID":
        return

    now = server_now()
    checkout.status = "PAID"
    checkout.paid_at = now

    user = db.session.get(User, checkout.user_id)
    if user is None:
        return

    order = SubscriptionOrder(
        user_id=user.id,
        plan_name=checkout.plan_name,
        amount_cents=checkout.amount_cents,
        currency=checkout.currency,
        status="pending",
    )
    db.session.add(order)
    db.session.flush()
    _apply_paid_subscription_order(order, paid_at=now)


def _abacatepay_request(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> tuple[bool, dict, str]:
    import urllib.request
    import urllib.error
    import json as _json

    api_key = _abacatepay_api_key()
    base_url = "https://api.abacatepay.com"
    normalized_path = path if path.startswith("/") else f"/{path}"
    url = f"{base_url}{normalized_path}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{query}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-API-Key": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; AppOrcamentos/1.0)",
    }
    body = _json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            data = _json.loads(raw)
            return True, data, ""
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8")
        try:
            detail = _json.loads(raw_error)
            msg = detail.get("message") or detail.get("error") or str(exc)
        except Exception:
            msg = raw_error
        return False, {}, msg
    except Exception as exc:
        return False, {}, str(exc)


def _create_abacatepay_billing(checkout: "SubscriptionCheckout") -> tuple[bool, dict, str]:
    user = db.session.get(User, checkout.user_id)
    if user is None:
        return False, {}, "Usuário não encontrado."

    return_url = url_for("main.billing_abacatepay_return", checkout=checkout.id, _external=True)

    payload = {
        "frequency": "ONE_TIME",
        "methods": ["PIX"],
        "products": [
            {
                "externalId": f"plan_{checkout.plan_key}",
                "name": f"Assinatura {checkout.plan_name}"[:80],
                "description": f"Assinatura {checkout.plan_name}"[:140],
                "price": checkout.amount_cents,
                "quantity": 1,
            }
        ],
        "returnUrl": return_url,
        "completionUrl": return_url,
        "externalId": f"checkout_{checkout.id}",
        "metadata": {
            "checkout_id": str(checkout.id),
            "plan_key": checkout.plan_key,
            "plan_name": checkout.plan_name,
            "user_email": user.email,
        },
    }

    # Adiciona dados do cliente se disponível
    customer_payload = {
        "name": user.username,
        "email": user.email,
        "cellphone": user.phone or "",
        "taxId": user.tax_id or "",
        "externalId": f"user_{user.id}",
    }
    abacatepay_customer_id = getattr(user, "abacatepay_customer_id", None)
    if abacatepay_customer_id:
        payload["customerId"] = abacatepay_customer_id
    else:
        payload["customer"] = customer_payload

    ok, data, error = _abacatepay_request("POST", "/v1/billing/create", payload=payload)

    if not ok or not isinstance(data, dict):
        return False, {}, error or "Falha ao criar cobrança."

    billing_data = data.get("data")
    if not isinstance(billing_data, dict):
        return False, {}, "Resposta da AbacatePay sem campo data."

    result = {
        "provider_charge_id": str(billing_data.get("id") or "").strip(),
        "status": str(billing_data.get("status") or "PENDING").strip().upper(),
        "checkout_url": billing_data.get("url"),
        "customer_id": str(
            (billing_data.get("customer") or {}).get("id") or ""
        ).strip() or None,
    }

    if not result["provider_charge_id"]:
        return False, {}, "Resposta da AbacatePay sem ID da cobrança."

    return True, result, ""


def _sync_checkout_status(checkout: "SubscriptionCheckout") -> tuple[bool, str]:
    if not checkout.provider_charge_id:
        return False, "Cobrança sem ID no provider."

    ok, data, error = _abacatepay_request(
        "GET", "/v1/pixQrCode/check", params={"id": checkout.provider_charge_id}
    )
    if not ok or not isinstance(data, dict):
        return False, error

    billing_data = data.get("data") or {}
    remote_status = str(billing_data.get("status") or "").strip().upper()

    if remote_status == "PAID" and checkout.status != "PAID":
        _apply_paid_checkout(checkout)
        db.session.commit()

    return True, remote_status


def _simulate_abacatepay_checkout(charge_id: str) -> tuple[bool, str]:
    ok, _data, error = _abacatepay_request(
        "POST", "/v1/pixQrCode/simulate-payment", params={"id": charge_id}
    )
    return ok, error


# ---------------------------------------------------------------------------
# Página de planos
# ---------------------------------------------------------------------------

@bp.get("/planos")
@member_required
def plans():
    user = g.member_user
    plans_list = _subscription_plans()
    current_plan_key = _active_member_plan_key(user)

    for plan in plans_list:
        plan_key = str(plan["key"])
        plan["amount_label"] = _format_currency_from_cents(
            int(plan["amount_cents"]), str(plan["currency"])
        )
        plan["is_current"] = bool(
            _is_member_subscription_active(
                user) and current_plan_key == plan_key
        )
        plan["display_name"] = "Plano Atual" if plan["is_current"] else str(
            plan["name"])

        plan_limits = PLAN_FEATURES.get(plan_key, PLAN_FEATURES["starter"])
        supplier_limit = plan_limits.get("max_suppliers")
        template_limit = plan_limits.get("max_templates")
        bulk_limit = plan_limits.get("max_bulk_recipients")

        plan["feature_items"] = [
            (
                f"Até {supplier_limit} fornecedores"
                if isinstance(supplier_limit, int)
                else "Fornecedores ilimitados"
            ),
            (
                f"Até {template_limit} templates"
                if isinstance(template_limit, int)
                else "Templates ilimitados"
            ),
            (
                f"Até {bulk_limit} destinatários por envio"
                if isinstance(bulk_limit, int)
                else "Envio em lote ilimitado"
            ),
            "Importação de contatos"
            if bool(plan_limits.get("can_import_contacts"))
            else "Sem importação de contatos",
            "Integração com WhatsApp"
            if bool(plan_limits.get("can_whatsapp_session"))
            else "Sem integração com WhatsApp",
        ]

    # Busca checkout ativo passado via query string (após criar um pedido)
    checkout_id_raw = request.args.get("checkout", "").strip()
    active_checkout = None
    if checkout_id_raw.isdigit():
        active_checkout = SubscriptionCheckout.query.filter_by(
            id=int(checkout_id_raw), user_id=user.id
        ).first()

    pending_checkout = (
        SubscriptionCheckout.query.filter_by(user_id=user.id, status="PENDING")
        .order_by(SubscriptionCheckout.created_at.desc())
        .first()
    )

    return render_template(
        "subscription.html",
        plans=plans_list,
        current_plan_key=current_plan_key,
        subscription_active=_is_member_subscription_active(user),
        subscription_expires_at=user.subscription_expires_at,
        abacatepay_mode=_abacatepay_mode(),
        abacatepay_configured=_abacatepay_configured(),
        active_checkout=active_checkout,
        active_checkout_plan=_subscription_plan_by_key(active_checkout.plan_key) if active_checkout else None,
        pending_checkout=pending_checkout,
        payment_test_mode=_payment_test_mode_enabled(),
        # ↓ variáveis que o subscription.html ainda usa
        orders=SubscriptionOrder.query.filter_by(user_id=user.id).order_by(SubscriptionOrder.created_at.desc()).limit(20).all(),
        has_pending_order=pending_checkout is not None,
    )


# ---------------------------------------------------------------------------
# Iniciar checkout
# ---------------------------------------------------------------------------

@bp.post("/planos/checkout/iniciar/<string:plan_key>")
@member_required
def billing_checkout_start(plan_key: str):
    user = g.member_user
    selected_plan = _subscription_plan_by_key(plan_key)

    if selected_plan is None:
        flash("Plano inválido.", "error")
        return redirect(url_for("main.plans"))
    
    if not user.phone or not user.tax_id:
        flash("Preencha seu telefone e CPF/CNPJ no cadastro antes de assinar.", "error")
        return redirect(url_for("main.subscription"))
    # Não permite iniciar novo checkout com um pendente aberto
    existing_pending = SubscriptionCheckout.query.filter_by(
        user_id=user.id, status="PENDING"
    ).first()
    if existing_pending:
        flash(
            "Você já tem um pedido pendente. Finalize ou cancele antes de criar outro.",
            "error",
        )
        return redirect(url_for("main.plans", checkout=existing_pending.id))

    mode = _abacatepay_mode()

    checkout = SubscriptionCheckout(
        user_id=user.id,
        plan_key=str(selected_plan["key"]),
        plan_name=str(selected_plan["name"]),
        amount_cents=int(selected_plan["amount_cents"]),
        currency=str(selected_plan["currency"]),
        provider="abacatepay",
        mode=mode,
        status="PENDING",
    )

    # ── Modo MOCK: simula checkout sem chamar API ──
    if mode == "mock":
        checkout.provider_charge_id = f"mock_{secrets.token_hex(8)}"
        db.session.add(checkout)
        db.session.commit()
        flash(
            "Pedido criado (modo mock). Use o botão 'Simular pagamento' para ativar.",
            "success",
        )
        return redirect(url_for("main.plans", checkout=checkout.id))

    # ── Modo DEV: marca como pago imediatamente ──
    if mode == "dev":
        if not _abacatepay_configured():
            flash("Configure ABACATEPAY_API_KEY no .env para usar o modo dev.", "error")
            return redirect(url_for("main.subscription"))

        db.session.add(checkout)
        db.session.flush()

        ok, data, error = _create_abacatepay_billing(checkout)

        if not ok or not data:
            db.session.rollback()
            flash(f"Falha ao criar cobrança na AbacatePay: {error}", "error")
            return redirect(url_for("main.subscription"))

        checkout.provider_charge_id = data["provider_charge_id"]
        checkout.provider_checkout_url = data.get("checkout_url")

        created_customer_id = (data.get("customer_id") or "").strip()
        if created_customer_id and not getattr(user, "abacatepay_customer_id", None):
            user.abacatepay_customer_id = created_customer_id

        # Ativa a assinatura independente do status
        _apply_paid_checkout(checkout)
        db.session.commit()

        # Redireciona para tela de pagamento se tiver URL
        # if checkout.provider_checkout_url:
        #     return redirect(checkout.provider_checkout_url)

        flash("Plano ativado com sucesso.", "success")
        return redirect(url_for("main.plans"))

    # ── Modo LIVE: chama a AbacatePay ──
    if mode == "live":
        if not _abacatepay_configured():
            flash(
                "Configure ABACATEPAY_API_KEY no .env para gerar cobranças reais.",
                "error",
            )
            return redirect(url_for("main.plans"))

        db.session.add(checkout)
        db.session.flush()  # gera o ID para usar no externalId

        ok, data, error = _create_abacatepay_billing(checkout)
        if not ok or not data:
            db.session.rollback()
            flash(f"Falha ao criar cobrança na AbacatePay: {error}", "error")
            return redirect(url_for("main.plans"))

        checkout.provider_charge_id = data["provider_charge_id"]
        checkout.status = data["status"]
        checkout.provider_checkout_url = data.get("checkout_url")

        # Persiste customer_id no usuário para reuso futuro
        created_customer_id = (data.get("customer_id") or "").strip()
        if created_customer_id and not getattr(user, "abacatepay_customer_id", None):
            user.abacatepay_customer_id = created_customer_id

        if checkout.status == "PAID":
            _apply_paid_checkout(checkout)

        db.session.commit()

        if checkout.provider_checkout_url:
            return redirect(checkout.provider_checkout_url)

        flash(
            "Cobrança criada. Finalize o pagamento para ativar o plano.",
            "success",
        )
        return redirect(url_for("main.plans", checkout=checkout.id))

    flash("ABACATEPAY_MODE inválido. Use mock, dev ou live.", "error")
    return redirect(url_for("main.plans"))


# ---------------------------------------------------------------------------
# Cancelar checkout pendente
# ---------------------------------------------------------------------------

@bp.post("/planos/checkout/<int:checkout_id>/cancelar")
@member_required
def billing_checkout_cancel(checkout_id: int):
    checkout = _checkout_owner_or_404(checkout_id)
    if checkout.status != "PENDING":
        flash("Somente pedidos pendentes podem ser cancelados.", "error")
        return redirect(url_for("main.plans"))

    checkout.status = "CANCELED"
    db.session.commit()
    flash("Pedido cancelado.", "success")
    return redirect(url_for("main.plans"))


# ---------------------------------------------------------------------------
# Consultar status do checkout (polling via JS)
# ---------------------------------------------------------------------------

@bp.get("/planos/checkout/<int:checkout_id>/status")
@api_member_required
def billing_checkout_status(checkout_id: int):
    checkout = SubscriptionCheckout.query.filter_by(
        id=checkout_id, user_id=g.member_user.id
    ).first_or_404()

    # Se pago mas assinatura ainda não ativada, aplica agora
    if checkout.status == "PAID":
        user = g.member_user
        if not _is_member_subscription_active(user):
            _apply_paid_checkout(checkout)
            db.session.commit()

    # Consulta status remoto se ainda pendente
    if checkout.status == "PENDING" and checkout.provider_charge_id:
        ok, message = _sync_checkout_status(checkout)
        if not ok:
            return (
                jsonify(
                    {
                        "ok": False,
                        "status": checkout.status,
                        "message": message,
                        "plan_key": checkout.plan_key,
                    }
                ),
                400,
            )

    plan = _subscription_plan_by_key(checkout.plan_key)
    return jsonify(
        {
            "ok": True,
            "status": checkout.status,
            "plan_key": checkout.plan_key,
            "plan_name": plan["name"] if plan else checkout.plan_name,
            "paid": checkout.status == "PAID",
            "checkout_url": checkout.provider_checkout_url,
        }
    )


# ---------------------------------------------------------------------------
# Simular pagamento (modo mock / dev)
# ---------------------------------------------------------------------------

@bp.post("/planos/checkout/<int:checkout_id>/simular")
@member_required
def billing_checkout_simulate(checkout_id: int):
    checkout = _checkout_owner_or_404(checkout_id)
    mode = _abacatepay_mode()

    if checkout.status == "PAID":
        flash("Este pedido já foi pago.", "success")
        return redirect(url_for("main.plans", checkout=checkout.id))

    if mode not in {"mock", "dev"}:
        flash("Simulação disponível apenas nos modos mock/dev.", "error")
        return redirect(url_for("main.plans", checkout=checkout.id))

    # Mock: aplica diretamente
    if mode == "mock":
        _apply_paid_checkout(checkout)
        db.session.commit()
        flash("Pagamento simulado com sucesso (mock). Plano ativado!", "success")
        return redirect(url_for("main.plans"))

    # Dev: chama endpoint de simulação da AbacatePay
    if mode == "dev":
        if not checkout.provider_charge_id:
            flash("Checkout sem ID de cobrança. Crie um novo pedido.", "error")
            return redirect(url_for("main.plans"))

        ok, error = _simulate_abacatepay_checkout(checkout.provider_charge_id)
        if not ok:
            flash(
                f"Falha ao simular pagamento na AbacatePay: {error}", "error")
            return redirect(url_for("main.plans", checkout=checkout.id))

        sync_ok, sync_message = _sync_checkout_status(checkout)
        if not sync_ok:
            flash(
                f"Pagamento simulado, mas falha ao sincronizar: {sync_message}",
                "error",
            )
            return redirect(url_for("main.plans", checkout=checkout.id))

        flash("Pagamento simulado e assinatura atualizada.", "success")
        return redirect(url_for("main.plans"))

    flash("Modo inválido.", "error")
    return redirect(url_for("main.plans"))


# ---------------------------------------------------------------------------
# Teste de conexão com a AbacatePay (somente modo dev)
# ---------------------------------------------------------------------------

@bp.post("/planos/abacatepay/testar")
@member_required
def billing_abacatepay_test():
    if _abacatepay_mode() != "dev":
        flash(
            "Teste de API AbacatePay disponível apenas com ABACATEPAY_MODE=dev.",
            "error",
        )
        return redirect(url_for("main.plans"))

    ok, data, error = _abacatepay_request("GET", "/billing")
    if ok:
        flash("Conexão com AbacatePay funcionando corretamente.", "success")
    else:
        flash(f"Falha na conexão com AbacatePay: {error}", "error")
    return redirect(url_for("main.plans"))


# ---------------------------------------------------------------------------
# Retorno após pagamento externo (redirect da AbacatePay)
# ---------------------------------------------------------------------------

@bp.route("/planos/retorno", methods=["GET", "POST"])
@csrf.exempt
def billing_abacatepay_return():
    checkout_id_raw = request.args.get("checkout", "").strip()
    if not checkout_id_raw:
        checkout_id_raw = (request.form.get("checkout") or "").strip()

    checkout_id = int(checkout_id_raw) if checkout_id_raw.isdigit() else None

    user = getattr(g, "member_user", None)
    if user and checkout_id:
        checkout = SubscriptionCheckout.query.filter_by(
            id=checkout_id, user_id=user.id
        ).first()
        if checkout and checkout.status == "PENDING":
            _sync_checkout_status(checkout)

    if user:
        target = url_for("main.plans")
        if checkout_id:
            target = url_for("main.plans", checkout=checkout_id)
        return redirect(target)

    next_url = (
        url_for("main.plans", checkout=checkout_id)
        if checkout_id
        else url_for("main.plans")
    )
    return redirect(url_for("main.login", next=next_url))


# ---------------------------------------------------------------------------
# Webhook da AbacatePay
# ---------------------------------------------------------------------------

@bp.post("/webhooks/abacatepay")
@csrf.exempt
def webhook_abacatepay():
    mode = _abacatepay_mode()
    configured_secret = (
        current_app.config.get("ABACATEPAY_WEBHOOK_SECRET") or ""
    ).strip()
    configured_public_key = (
        current_app.config.get("ABACATEPAY_WEBHOOK_PUBLIC_KEY") or ""
    ).strip()

    # Valida secret no modo live
    if not configured_secret and mode == "live":
        return jsonify({"ok": False, "error": "webhook_secret_obrigatorio_no_live"}), 401

    if configured_secret:
        request_secret = request.args.get("webhookSecret", "").strip()
        if not _hmac.compare_digest(request_secret, configured_secret):
            return jsonify({"ok": False, "error": "nao_autorizado"}), 401

    # Valida assinatura HMAC opcional
    if configured_public_key:
        signature = request.headers.get("X-Webhook-Signature", "").strip()
        if not signature:
            return jsonify({"ok": False, "error": "assinatura_ausente"}), 401
        raw_body = request.get_data(cache=True) or b""
        expected = base64.b64encode(
            _hmac.new(
                configured_public_key.encode("utf-8"),
                raw_body,
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        if not _hmac.compare_digest(signature, expected):
            return jsonify({"ok": False, "error": "assinatura_invalida"}), 401

    payload = request.get_json(silent=True) or {}
    event_name = str(payload.get("event") or "").strip().lower()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

    # Ignora eventos irrelevantes
    if event_name not in {"billing.paid", "pixqrcode.paid", "checkout.completed"}:
        return jsonify({"ok": True, "ignorado": True}), 200

    # ── Extrai identificadores do payload ──
    billing_data = data.get("billing") if isinstance(
        data.get("billing"), dict) else {}
    checkout_data = data.get("checkout") if isinstance(
        data.get("checkout"), dict) else {}
    pix_data = data.get("pixQrCode") if isinstance(
        data.get("pixQrCode"), dict) else {}

    provider_charge_id = str(
        billing_data.get("id")
        or billing_data.get("billingId")
        or checkout_data.get("id")
        or pix_data.get("id")
        or data.get("id")
        or ""
    ).strip()

    metadata: dict = {}
    for src in (data, billing_data, checkout_data):
        if isinstance(src.get("metadata"), dict):
            metadata.update(src["metadata"])

    external_id = str(
        billing_data.get("externalId")
        or checkout_data.get("externalId")
        or data.get("externalId")
        or metadata.get("externalId")
        or metadata.get("checkout_id")
        or ""
    ).strip()

    plan_key_hint = str(metadata.get("plan_key") or "").strip().lower()

    customer_data: dict = {}
    for src in (billing_data, checkout_data, data):
        if isinstance(src.get("customer"), dict):
            customer_data = src["customer"]
            break

    customer_metadata = (
        customer_data.get("metadata")
        if isinstance(customer_data.get("metadata"), dict)
        else {}
    )
    customer_id = str(customer_data.get("id") or "").strip()
    customer_email = str(
        customer_data.get("email")
        or customer_metadata.get("email")
        or metadata.get("user_email")
        or ""
    ).strip().lower()

    # ── Localiza o checkout correspondente ──
    checkout = None

    if provider_charge_id:
        checkout = (
            SubscriptionCheckout.query.filter_by(provider="abacatepay")
            .filter(SubscriptionCheckout.provider_charge_id == provider_charge_id)
            .order_by(SubscriptionCheckout.id.desc())
            .first()
        )

    if not checkout and external_id:
        checkout_id = None
        if external_id.isdigit():
            checkout_id = int(external_id)
        elif external_id.startswith("checkout_"):
            raw = external_id.replace("checkout_", "").strip()
            if raw.isdigit():
                checkout_id = int(raw)
        if checkout_id:
            checkout = SubscriptionCheckout.query.filter_by(
                id=checkout_id, provider="abacatepay"
            ).first()

    if not checkout and customer_email:
        user = User.query.filter(func.lower(
            User.email) == customer_email).first()
        if user:
            q = SubscriptionCheckout.query.filter_by(
                user_id=user.id, provider="abacatepay", status="PENDING"
            )
            if plan_key_hint:
                q = q.filter_by(plan_key=plan_key_hint)
            checkout = q.order_by(SubscriptionCheckout.id.desc()).first()

    if not checkout and customer_id:
        user = User.query.filter(
            User.abacatepay_customer_id == customer_id
        ).first() if hasattr(User, "abacatepay_customer_id") else None
        if user:
            q = SubscriptionCheckout.query.filter_by(
                user_id=user.id, provider="abacatepay", status="PENDING"
            )
            if plan_key_hint:
                q = q.filter_by(plan_key=plan_key_hint)
            checkout = q.order_by(SubscriptionCheckout.id.desc()).first()

    if checkout is None:
        return jsonify({"ok": True, "ignorado": True, "motivo": "checkout_nao_encontrado"}), 200

    # Atualiza provider_charge_id se estava vazio
    if provider_charge_id and not checkout.provider_charge_id:
        checkout.provider_charge_id = provider_charge_id

    _apply_paid_checkout(checkout)
    db.session.commit()

    return jsonify({"ok": True}), 200
