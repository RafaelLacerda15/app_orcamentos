from __future__ import annotations

from flask import current_app

from .validation import normalize_user_phone
from .whatsapp import WhatsAppSessionManager


def whatsapp_provider() -> str:
    return (current_app.config.get("WHATSAPP_PROVIDER") or "simulado").strip().lower() or "simulado"


def whatsapp_provider_label(provider: str | None = None) -> str:
    normalized = (provider or whatsapp_provider()).strip().lower()
    if normalized == "pywhatkit":
        return "PyWhatKit"
    return "Simulado"


def send_whatsapp_message(
    phone: str,
    message: str,
    session_manager: WhatsAppSessionManager | None = None,
) -> tuple[bool, str | None]:
    normalized_phone = normalize_user_phone(phone)
    if not normalized_phone:
        return False, "Telefone invalido para envio no WhatsApp."

    provider = whatsapp_provider()
    if provider == "pywhatkit":
        if session_manager is not None:
            sent, error = session_manager.send_message_with_connected_session(
                normalized_phone, message)
            if sent:
                return True, None
            current_app.logger.warning(
                "Falha no envio pela sessao Playwright: %s", error)
            return False, error or "Falha ao enviar pela sessao Playwright."
        return _send_with_pywhatkit(normalized_phone, message)

    current_app.logger.info(
        "WhatsApp (simulado) para %s: %s", normalized_phone, message)
    return True, None


def _send_with_pywhatkit(phone: str, message: str) -> tuple[bool, str | None]:
    try:
        import pywhatkit
    except Exception:
        return False, "PyWhatKit nao instalado. Rode: pip install pywhatkit"

    wait_time = _int_config(
        "WHATSAPP_PYWHATKIT_WAIT_TIME", 15, minimum=5, maximum=120)
    close_time = _int_config(
        "WHATSAPP_PYWHATKIT_CLOSE_TIME", 3, minimum=1, maximum=30)
    close_tab = bool(current_app.config.get(
        "WHATSAPP_PYWHATKIT_CLOSE_TAB", True))

    try:
        pywhatkit.sendwhatmsg_instantly(
            phone_no=phone,
            message=message,
            wait_time=wait_time,
            tab_close=close_tab,
            close_time=close_time,
        )
        return True, None
    except Exception as exc:
        current_app.logger.error(
            "Falha no envio via PyWhatKit para %s: %s", phone, exc)
        return False, str(exc)


def _int_config(key: str, fallback: int, *, minimum: int, maximum: int) -> int:
    raw = current_app.config.get(key, fallback)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = fallback
    return min(max(value, minimum), maximum)