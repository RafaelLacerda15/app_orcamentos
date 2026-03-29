import re

from sqlalchemy import func, or_

from ..models import Supplier
from .importers import normalize_phone

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    return normalized or None


def validate_email(value: str | None) -> bool:
    if not value:
        return True
    return bool(EMAIL_PATTERN.match(value))


def normalize_user_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None

    if len(digits) in {10, 11}:
        return f"+55{digits}"
    if digits.startswith("55") and len(digits) in {12, 13}:
        return f"+{digits}"
    if len(digits) in {10, 11, 12, 13, 14, 15}:
        return f"+{digits}"
    return None


def validate_user_phone(value: str | None) -> bool:
    return normalize_user_phone(value) is not None


def has_duplicate_contact(
    email: str | None,
    phone: str | None,
    owner_user_id: int,
    supplier_id: int | None = None,
) -> bool:
    if not email and not phone:
        return False

    query = Supplier.query.filter(Supplier.owner_user_id == owner_user_id)
    if supplier_id:
        query = query.filter(Supplier.id != supplier_id)

    clauses = []
    if email:
        clauses.append(func.lower(Supplier.email) == email.lower())
    if phone:
        clauses.append(Supplier.phone == phone)
    if not clauses:
        return False

    return query.filter(or_(*clauses)).first() is not None


def normalize_supplier_payload(form_data: dict[str, str]) -> dict[str, str | None]:
    return {
        "name": form_data.get("name", "").strip(),
        "company": form_data.get("company", "").strip() or None,
        "phone": normalize_phone(form_data.get("phone", "").strip()) if form_data.get("phone") else None,
        "email": normalize_email(form_data.get("email")),
        "notes": form_data.get("notes", "").strip() or None,
    }
