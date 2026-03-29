from __future__ import annotations

import secrets
from typing import Callable

from flask import abort, current_app, request, session
from flask_sqlalchemy import SQLAlchemy

try:
    from flask_wtf.csrf import CSRFProtect as _CSRFProtect
except ModuleNotFoundError:
    _CSRFProtect = None


class _FallbackCSRFProtect:
    def init_app(self, app) -> None:
        @app.context_processor
        def inject_csrf_token() -> dict[str, Callable[[], str]]:
            return {"csrf_token": self.generate_csrf}

        @app.before_request
        def csrf_protect() -> None:
            if not current_app.config.get("WTF_CSRF_ENABLED", True):
                return
            if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
                return

            view = current_app.view_functions.get(request.endpoint or "")
            if view and getattr(view, "_csrf_exempt", False):
                return

            session_token = session.get("_csrf_token")
            request_token = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
            if not session_token or not request_token or not secrets.compare_digest(str(session_token), str(request_token)):
                abort(400, description="CSRF token missing or invalid.")

    def generate_csrf(self) -> str:
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token

    def exempt(self, view_func):
        setattr(view_func, "_csrf_exempt", True)
        return view_func


db = SQLAlchemy()
csrf = _CSRFProtect() if _CSRFProtect is not None else _FallbackCSRFProtect()
