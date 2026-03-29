import base64
import json
from urllib import error, parse, request

from flask import current_app


def send_verification_code(phone: str, code: str) -> tuple[bool, str | None]:
    provider = (current_app.config.get("SMS_PROVIDER") or "console").strip().lower()
    message = f"Codigo de verificacao: {code}"

    if provider == "twilio":
        return _send_with_twilio(phone, message)

    current_app.logger.info("SMS (console) para %s: %s", phone, message)
    return True, code


def _send_with_twilio(phone: str, message: str) -> tuple[bool, str | None]:
    account_sid = (current_app.config.get("TWILIO_ACCOUNT_SID") or "").strip()
    auth_token = (current_app.config.get("TWILIO_AUTH_TOKEN") or "").strip()
    from_number = (current_app.config.get("TWILIO_FROM_NUMBER") or "").strip()
    if not account_sid or not auth_token or not from_number:
        current_app.logger.error("Twilio nao configurado corretamente (SID/TOKEN/FROM).")
        return False, None

    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = parse.urlencode({"From": from_number, "To": phone, "Body": message}).encode()
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    req = request.Request(endpoint, data=payload, headers=headers, method="POST")

    try:
        with request.urlopen(req, timeout=15) as response:
            status = response.getcode()
            if 200 <= status < 300:
                return True, None
            current_app.logger.error("Falha Twilio (status=%s)", status)
            return False, None
    except error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="ignore")
            parsed = json.loads(body) if body else {}
            current_app.logger.error("Twilio HTTPError %s: %s", exc.code, parsed)
        except Exception:
            current_app.logger.error("Twilio HTTPError %s", exc.code)
        return False, None
    except error.URLError as exc:
        current_app.logger.error("Erro de rede no envio SMS: %s", exc)
        return False, None
