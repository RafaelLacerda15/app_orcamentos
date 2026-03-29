"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-03-08 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("subscription_status", sa.String(length=32), nullable=False, server_default="inactive"),
        sa.Column("subscription_started_at", sa.DateTime(), nullable=True),
        sa.Column("subscription_expires_at", sa.DateTime(), nullable=True),
        sa.Column("subscription_last_payment_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_subscription_status", "users", ["subscription_status"], unique=False)
    op.create_index("ix_users_subscription_expires_at", "users", ["subscription_expires_at"], unique=False)
    op.create_index("ix_users_subscription_last_payment_at", "users", ["subscription_last_payment_at"], unique=False)

    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("company", sa.String(length=120), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
    )
    op.create_index("ix_suppliers_phone", "suppliers", ["phone"], unique=False)
    op.create_index("ix_suppliers_email", "suppliers", ["email"], unique=False)
    op.create_index("ix_suppliers_owner_user_id", "suppliers", ["owner_user_id"], unique=False)

    op.create_table(
        "message_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
    )
    op.create_index("ix_message_templates_owner_user_id", "message_templates", ["owner_user_id"], unique=False)

    op.create_table(
        "message_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.ForeignKeyConstraint(["template_id"], ["message_templates.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
    )
    op.create_index("ix_message_history_sent_at", "message_history", ["sent_at"], unique=False)
    op.create_index("ix_message_history_owner_user_id", "message_history", ["owner_user_id"], unique=False)

    op.create_table(
        "subscription_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("plan_name", sa.String(length=80), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="BRL"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("period_start_at", sa.DateTime(), nullable=True),
        sa.Column("period_end_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_subscription_orders_user_id", "subscription_orders", ["user_id"], unique=False)
    op.create_index("ix_subscription_orders_status", "subscription_orders", ["status"], unique=False)
    op.create_index("ix_subscription_orders_created_at", "subscription_orders", ["created_at"], unique=False)
    op.create_index("ix_subscription_orders_paid_at", "subscription_orders", ["paid_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_subscription_orders_paid_at", table_name="subscription_orders")
    op.drop_index("ix_subscription_orders_created_at", table_name="subscription_orders")
    op.drop_index("ix_subscription_orders_status", table_name="subscription_orders")
    op.drop_index("ix_subscription_orders_user_id", table_name="subscription_orders")
    op.drop_table("subscription_orders")

    op.drop_index("ix_message_history_owner_user_id", table_name="message_history")
    op.drop_index("ix_message_history_sent_at", table_name="message_history")
    op.drop_table("message_history")

    op.drop_index("ix_message_templates_owner_user_id", table_name="message_templates")
    op.drop_table("message_templates")

    op.drop_index("ix_suppliers_owner_user_id", table_name="suppliers")
    op.drop_index("ix_suppliers_email", table_name="suppliers")
    op.drop_index("ix_suppliers_phone", table_name="suppliers")
    op.drop_table("suppliers")

    op.drop_index("ix_users_subscription_last_payment_at", table_name="users")
    op.drop_index("ix_users_subscription_expires_at", table_name="users")
    op.drop_index("ix_users_subscription_status", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
