from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .services.timezone import server_now
from datetime import datetime

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False, unique=True, index=True)
    email = db.Column(db.String(120), nullable=False, unique=True, index=True)
    phone = db.Column(db.String(32), nullable=True, index=True)
    phone_verified = db.Column(db.Boolean, default=False, nullable=False)
    tax_id = db.Column(db.String(32), nullable=True)
    abacatepay_customer_id = db.Column(db.String(128), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    subscription_status = db.Column(db.String(32), default="inactive", nullable=False, index=True)
    subscription_started_at = db.Column(db.DateTime, nullable=True)
    subscription_expires_at = db.Column(db.DateTime, nullable=True, index=True)
    subscription_last_payment_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=server_now, nullable=False)

    suppliers = db.relationship("Supplier", back_populates="owner", cascade="all, delete-orphan")
    templates = db.relationship("MessageTemplate", back_populates="owner", cascade="all, delete-orphan")
    message_history = db.relationship("MessageHistory", back_populates="owner", cascade="all, delete-orphan")
    subscription_orders = db.relationship("SubscriptionOrder", back_populates="user", cascade="all, delete-orphan")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Supplier(db.Model):
    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    company = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(32), nullable=True, index=True)
    email = db.Column(db.String(120), nullable=True, index=True)
    notes = db.Column(db.Text, nullable=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=server_now, nullable=False)

    owner = db.relationship("User", back_populates="suppliers")
    history = db.relationship("MessageHistory", back_populates="supplier", cascade="all, delete-orphan")


class MessageTemplate(db.Model):
    __tablename__ = "message_templates"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    body = db.Column(db.Text, nullable=False)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=server_now, nullable=False)

    owner = db.relationship("User", back_populates="templates")
    history = db.relationship("MessageHistory", back_populates="template")


class MessageHistory(db.Model):
    __tablename__ = "message_history"

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey("message_templates.id"), nullable=True)
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(32), nullable=False, default="simulado")
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    sent_at = db.Column(db.DateTime, default=server_now, nullable=False, index=True)

    owner = db.relationship("User", back_populates="message_history")
    supplier = db.relationship("Supplier", back_populates="history")
    template = db.relationship("MessageTemplate", back_populates="history")


class SubscriptionOrder(db.Model):
    __tablename__ = "subscription_orders"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    plan_name = db.Column(db.String(80), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(8), nullable=False, default="BRL")
    status = db.Column(db.String(32), nullable=False, default="pending", index=True)
    created_at = db.Column(db.DateTime, default=server_now, nullable=False, index=True)
    paid_at = db.Column(db.DateTime, nullable=True, index=True)
    period_start_at = db.Column(db.DateTime, nullable=True)
    period_end_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", back_populates="subscription_orders")


class SignupVerification(db.Model):
    __tablename__ = "signup_verifications"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False, index=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    phone = db.Column(db.String(32), nullable=False, index=True)
    tax_id = db.Column(db.String(32), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    code_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=server_now, nullable=False, index=True)


class PasswordResetVerification(db.Model):
    __tablename__ = "password_reset_verifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    code_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=server_now, nullable=False, index=True)


class AdminAuditLog(db.Model):
    __tablename__ = "admin_audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    admin_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    action = db.Column(db.String(120), nullable=False, index=True)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=server_now, nullable=False, index=True)

class SubscriptionCheckout(db.Model):
    __tablename__ = "subscription_checkouts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # Plano e valor
    plan_key = db.Column(db.String(32), nullable=False)          # "starter" | "pro" | "business"
    plan_name = db.Column(db.String(64), nullable=False)         # nome legível
    amount_cents = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(8), nullable=False, default="BRL")

    # Provedor de pagamento
    provider = db.Column(db.String(32), nullable=False, default="abacatepay")
    mode = db.Column(db.String(16), nullable=False, default="mock")  # "mock" | "dev" | "live"

    # Status: PENDING | PAID | CANCELED | EXPIRED
    status = db.Column(db.String(32), nullable=False, default="PENDING", index=True)

    # Dados devolvidos pela AbacatePay
    provider_charge_id = db.Column(db.String(128), nullable=True, index=True)
    provider_checkout_url = db.Column(db.String(512), nullable=True)
    provider_customer_id = db.Column(db.String(128), nullable=True)  # customer_id da AbacatePay

    # Timestamps
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)  # quando o checkout expira (opcional)

    # Relacionamento com User (assumindo que seu modelo User existe)
    user = db.relationship("User", backref=db.backref("checkouts", lazy="dynamic"))

    def __repr__(self) -> str:
        return f"<SubscriptionCheckout id={self.id} user_id={self.user_id} plan={self.plan_key} status={self.status}>"