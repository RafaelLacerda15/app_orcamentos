from datetime import timedelta

import pytest

from orcamentos import create_app
from orcamentos.extensions import db
from orcamentos.models import User
from orcamentos.services.timezone import server_now

MASTER_USERNAME = "Usuario_Master"
MASTER_PASSWORD = "load_usuario@_master"
MASTER_EMAIL = "usuario_master@app.local"


@pytest.fixture()
def app():
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "WHATSAPP_PROVIDER": "simulado",
        }
    )
    with app.app_context():
        db.drop_all()
        db.create_all()
        master = User(username=MASTER_USERNAME, email=MASTER_EMAIL, is_admin=True)
        master.set_password(MASTER_PASSWORD)
        db.session.add(master)
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def auth_client(client, app):
    with app.app_context():
        now = server_now()
        user = User(
            username="usuario_comum",
            email="usuario@example.com",
            is_admin=False,
            subscription_status="active",
            subscription_started_at=now,
            subscription_expires_at=now + timedelta(days=30),
            subscription_last_payment_at=now,
        )
        user.set_password("123456")
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    with client.session_transaction() as session_store:
        session_store["member_user_id"] = user_id

    return client


@pytest.fixture()
def admin_client(client, app):
    with app.app_context():
        master = User.query.filter_by(username=MASTER_USERNAME).first()
        assert master is not None
        user_id = master.id

    with client.session_transaction() as session_store:
        session_store["admin_user_id"] = user_id

    return client
