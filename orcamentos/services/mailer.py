import smtplib
from email.message import EmailMessage

from flask import current_app


def send_verification_code_email(email: str, code: str) -> tuple[bool, str | None]:
    provider = (current_app.config.get("EMAIL_PROVIDER") or "console").strip().lower()
    subject = (current_app.config.get("VERIFICATION_EMAIL_SUBJECT") or "Codigo de verificacao").strip()
    body = (
        "Seu codigo de verificacao e: "
        f"{code}\n\n"
        "Se voce nao solicitou este cadastro, ignore este email."
    )

    if provider == "smtp":
        return _send_with_smtp(email, subject, body)

    current_app.logger.info("Email (console) para %s | assunto=%s | codigo=%s", email, subject, code)
    return True, code


def _send_with_smtp(to_email: str, subject: str, body: str) -> tuple[bool, str | None]:
    host = (current_app.config.get("SMTP_HOST") or "").strip()
    port_raw = current_app.config.get("SMTP_PORT", 587)
    username = (current_app.config.get("SMTP_USERNAME") or "").strip()
    password = (current_app.config.get("SMTP_PASSWORD") or "").strip()
    from_email = (current_app.config.get("SMTP_FROM_EMAIL") or username).strip()
    use_tls = bool(current_app.config.get("SMTP_USE_TLS", True))

    if not host or not from_email:
        current_app.logger.error("SMTP nao configurado corretamente (SMTP_HOST/SMTP_FROM_EMAIL).")
        return False, None

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 587

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_email
    message["To"] = to_email
    message.set_content(body)

    try:
        with smtplib.SMTP(host=host, port=port, timeout=20) as smtp:
            if use_tls:
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)
        return True, None
    except Exception as exc:
        current_app.logger.error("Falha no envio SMTP para %s: %s", to_email, exc)
        return False, None
