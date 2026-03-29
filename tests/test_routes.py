from datetime import timedelta

from pathlib import Path

from orcamentos.extensions import db
from orcamentos.models import AdminAuditLog, MessageHistory, MessageTemplate, SubscriptionOrder, Supplier, User
from orcamentos.services.timezone import server_now


def _member_owner_id(app) -> int:
    with app.app_context():
        user = User.query.filter_by(username="usuario_comum").first()
        assert user is not None
        return user.id


def _member_whatsapp_manager_key(app) -> str:
    with app.app_context():
        user = User.query.filter_by(username="usuario_comum").first()
        assert user is not None
        return f"user_{user.id}_usuario_comum"


def test_create_supplier_normalizes_email_and_phone(auth_client, app):
    owner_user_id = _member_owner_id(app)

    response = auth_client.post(
        "/fornecedores/novo",
        data={
            "name": "Fornecedor A",
            "company": "Empresa A",
            "phone": "(11) 98765-4321",
            "email": "CONTATO@EMPRESA.COM",
            "notes": "Obs",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Fornecedor criado com sucesso." in response.data

    with app.app_context():
        supplier = Supplier.query.filter_by(owner_user_id=owner_user_id).first()
        assert supplier is not None
        assert supplier.phone == "+5511987654321"
        assert supplier.email == "contato@empresa.com"


def test_create_supplier_blocks_duplicate_contact(auth_client, app):
    owner_user_id = _member_owner_id(app)

    with app.app_context():
        db.session.add(Supplier(name="Ja existe", phone="+5511999998888", email="a@a.com", owner_user_id=owner_user_id))
        db.session.commit()

    response = auth_client.post(
        "/fornecedores/novo",
        data={
            "name": "Duplicado",
            "phone": "(11) 99999-8888",
            "email": "outro@a.com",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Ja existe fornecedor com esse telefone ou email." in response.data

    with app.app_context():
        assert Supplier.query.filter_by(owner_user_id=owner_user_id).count() == 1


def test_suppliers_csv_export(auth_client, app):
    owner_user_id = _member_owner_id(app)

    with app.app_context():
        db.session.add(Supplier(name="Fornecedor X", phone="+5511912345678", owner_user_id=owner_user_id))
        db.session.add(Supplier(name="Fornecedor Y", phone="+5511912345679", owner_user_id=owner_user_id))
        db.session.commit()

    response = auth_client.get("/fornecedores/exportar.csv")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.data.decode("utf-8-sig")
    assert "Fornecedor X" in body
    assert "Fornecedor Y" in body
    assert "nome,empresa,telefone,email,observacoes,criado_em" in body


def test_send_messages_ignores_supplier_without_contact(auth_client, app):
    owner_user_id = _member_owner_id(app)

    with app.app_context():
        valid = Supplier(name="Valido", phone="+5511911111111", owner_user_id=owner_user_id)
        invalid = Supplier(name="Invalido", phone=None, email=None, owner_user_id=owner_user_id)
        template = MessageTemplate(name="Padrao", body="Ola {nome}", owner_user_id=owner_user_id)
        db.session.add_all([valid, invalid, template])
        db.session.commit()
        valid_id = valid.id
        invalid_id = invalid.id
        template_id = template.id

    response = auth_client.post(
        "/mensagens",
        data={
            "action": "send_messages",
            "template_id": str(template_id),
            "supplier_ids": [str(valid_id), str(invalid_id)],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Mensagens registradas: 1. Ignoradas: 1." in response.data

    with app.app_context():
        assert MessageHistory.query.filter_by(owner_user_id=owner_user_id).count() == 1


def test_send_messages_redirects_back_to_messages_page(auth_client, app):
    owner_user_id = _member_owner_id(app)

    with app.app_context():
        supplier = Supplier(name="Valido", phone="+5511911111111", owner_user_id=owner_user_id)
        template = MessageTemplate(name="Padrao", body="Ola {nome}", owner_user_id=owner_user_id)
        db.session.add_all([supplier, template])
        db.session.commit()
        supplier_id = supplier.id
        template_id = template.id

    response = auth_client.post(
        "/mensagens",
        data={
            "action": "send_messages",
            "template_id": str(template_id),
            "supplier_ids": [str(supplier_id)],
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/mensagens")


def test_send_messages_returns_json_for_xml_http_request(auth_client, app):
    owner_user_id = _member_owner_id(app)

    with app.app_context():
        supplier = Supplier(name="Valido", phone="+5511911111111", owner_user_id=owner_user_id)
        template = MessageTemplate(name="Padrao", body="Ola {nome}", owner_user_id=owner_user_id)
        db.session.add_all([supplier, template])
        db.session.commit()
        supplier_id = supplier.id
        template_id = template.id

    response = auth_client.post(
        "/mensagens",
        data={
            "action": "send_messages",
            "template_id": str(template_id),
            "supplier_ids": [str(supplier_id)],
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["ok"] is True
    assert payload["category"] == "success"
    assert "Mensagens registradas: 1. Ignoradas: 0." in payload["message"]


def test_send_messages_with_pywhatkit_records_delivery_status(auth_client, app, monkeypatch):
    owner_user_id = _member_owner_id(app)

    class _ConnectedManager:
        def __init__(self):
            self.state = {
                "status": "connected",
                "message": "Conectado ao WhatsApp.",
                "qr_code": None,
                "running": True,
                "profile_dir": f"instance/whatsapp_sessions/user_{owner_user_id}_usuario_comum",
                "session_persistent": True,
                "session_saved": True,
            }

        def start(self):
            return None

        def stop(self):
            self.state["status"] = "disconnected"
            self.state["running"] = False

        def get_state(self):
            return dict(self.state)

    with app.app_context():
        app.config["WHATSAPP_PROVIDER"] = "pywhatkit"
        app.extensions["whatsapp_managers"] = {_member_whatsapp_manager_key(app): _ConnectedManager()}
        supplier = Supplier(name="Com WhatsApp", phone="+5511911111111", owner_user_id=owner_user_id)
        template = MessageTemplate(name="Padrao", body="Ola {nome}", owner_user_id=owner_user_id)
        db.session.add_all([supplier, template])
        db.session.commit()
        supplier_id = supplier.id
        template_id = template.id

    sent_calls: list[tuple[str, str]] = []

    def _fake_send(phone: str, message: str, *args, **kwargs):
        sent_calls.append((phone, message))
        return True, None

    monkeypatch.setattr("orcamentos.routes.send_whatsapp_message", _fake_send)

    response = auth_client.post(
        "/mensagens",
        data={
            "action": "send_messages",
            "template_id": str(template_id),
            "supplier_ids": [str(supplier_id)],
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Envio via PyWhatKit: 1 enviadas, 0 com falha, 0 ignoradas." in response.data
    assert len(sent_calls) == 1
    assert sent_calls[0][0] == "+5511911111111"

    with app.app_context():
        history = MessageHistory.query.filter_by(owner_user_id=owner_user_id).first()
        assert history is not None
        assert history.status == "enviado"


def test_send_messages_with_pywhatkit_blocks_when_not_connected(auth_client, app):
    owner_user_id = _member_owner_id(app)

    class _DisconnectedManager:
        def __init__(self):
            self.state = {
                "status": "disconnected",
                "message": "WhatsApp desconectado.",
                "qr_code": None,
                "running": False,
                "profile_dir": f"instance/whatsapp_sessions/user_{owner_user_id}_usuario_comum",
                "session_persistent": True,
                "session_saved": False,
            }

        def start(self):
            self.state["status"] = "waiting_qr"
            self.state["message"] = "Escaneie o QR Code com seu celular."
            self.state["running"] = True

        def stop(self):
            self.state["status"] = "disconnected"
            self.state["message"] = "WhatsApp desconectado."
            self.state["running"] = False

        def get_state(self):
            return dict(self.state)

    with app.app_context():
        app.config["WHATSAPP_PROVIDER"] = "pywhatkit"
        app.extensions["whatsapp_managers"] = {_member_whatsapp_manager_key(app): _DisconnectedManager()}
        supplier = Supplier(name="Sem Conexao", phone="+5511911111111", owner_user_id=owner_user_id)
        template = MessageTemplate(name="Padrao", body="Ola {nome}", owner_user_id=owner_user_id)
        db.session.add_all([supplier, template])
        db.session.commit()
        supplier_id = supplier.id
        template_id = template.id

    response = auth_client.post(
        "/mensagens",
        data={
            "action": "send_messages",
            "template_id": str(template_id),
            "supplier_ids": [str(supplier_id)],
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Escaneie o QR Code com seu celular." in response.data

    with app.app_context():
        assert MessageHistory.query.filter_by(owner_user_id=owner_user_id).count() == 0


def test_delete_template_keeps_history_and_unlinks_reference(auth_client, app):
    owner_user_id = _member_owner_id(app)

    with app.app_context():
        supplier = Supplier(name="Fornecedor", phone="+5511911111111", owner_user_id=owner_user_id)
        template = MessageTemplate(name="Remover", body="Ola {nome}", owner_user_id=owner_user_id)
        db.session.add_all([supplier, template])
        db.session.commit()

        history = MessageHistory(
            supplier_id=supplier.id,
            template_id=template.id,
            content="Mensagem",
            status="simulado",
            owner_user_id=owner_user_id,
        )
        db.session.add(history)
        db.session.commit()
        template_id = template.id
        history_id = history.id

    response = auth_client.post(
        f"/mensagens/templates/{template_id}/excluir",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Template removido." in response.data

    with app.app_context():
        assert MessageTemplate.query.get(template_id) is None
        remaining = MessageHistory.query.get(history_id)
        assert remaining is not None
        assert remaining.template_id is None


def test_tenant_data_isolation_between_two_users(auth_client, app):
    owner_user_id = _member_owner_id(app)

    with app.app_context():
        other = User(username="outro_user", email="outro@teste.com", is_admin=False)
        other.set_password("123456")
        db.session.add(other)
        db.session.commit()
        other_id = other.id

        db.session.add(Supplier(name="So do outro", phone="+5511910000000", owner_user_id=other_id))
        db.session.commit()

    response = auth_client.get("/fornecedores")
    assert response.status_code == 200
    assert b"So do outro" not in response.data


def test_messages_empty_state_shows_supplier_hint(auth_client):
    response = auth_client.get("/mensagens")

    assert response.status_code == 200
    assert b"Para quem ja foi enviado" not in response.data
    assert b"Nenhum envio registrado ainda." not in response.data
    assert b"Cadastre fornecedores para enviar mensagens." in response.data


def test_dashboard_shows_whatsapp_connect_warning_when_disconnected(auth_client, app):
    app.config["WHATSAPP_PROVIDER"] = "pywhatkit"
    app.extensions["whatsapp_managers"] = {_member_whatsapp_manager_key(app): _FakeWhatsAppManager()}

    response = auth_client.get("/")

    assert response.status_code == 200
    assert b"WhatsApp desconectado." in response.data
    assert b"Conectar WhatsApp" in response.data


def test_settings_page_shows_login_instructions(auth_client):
    response = auth_client.get("/configuracoes")

    assert response.status_code == 200
    assert b"Configuracoes" in response.data
    assert b"Como fazer login no WhatsApp" in response.data
    assert b"Iniciar login WhatsApp" in response.data
    assert b"Teste de envio (somente desenvolvimento)" in response.data


def test_settings_hides_test_panel_outside_development(auth_client, app):
    app.config["APP_ENV"] = "production"

    response = auth_client.get("/configuracoes")

    assert response.status_code == 200
    assert b"Como fazer login no WhatsApp" in response.data
    assert b"Teste de envio (somente desenvolvimento)" not in response.data


def test_whatsapp_test_endpoint_blocked_outside_development(auth_client, app):
    app.config["APP_ENV"] = "production"
    app.config["WHATSAPP_PROVIDER"] = "pywhatkit"

    response = auth_client.post(
        "/configuracoes/whatsapp/teste",
        data={"phone": "+5511999999999", "message": "teste"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Teste de envio disponivel apenas no ambiente development." in response.data


def test_subscription_page_and_checkout_flow(auth_client, app):
    page_response = auth_client.get("/assinatura")
    assert page_response.status_code == 200
    assert b"Assinatura SaaS" in page_response.data
    assert b"Starter" in page_response.data
    assert b"Pro" in page_response.data
    assert b"Business" in page_response.data

    create_response = auth_client.post(
        "/assinatura/comprar",
        data={"plan_key": "pro"},
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert b"Pedido do plano Pro criado." in create_response.data

    with app.app_context():
        user = User.query.filter_by(username="usuario_comum").first()
        assert user is not None
        order = SubscriptionOrder.query.filter_by(user_id=user.id, status="pending").first()
        assert order is not None
        assert order.plan_name == "Pro"
        assert order.amount_cents == 9900
        order_id = order.id

    webhook_response = auth_client.post(
        "/webhooks/pagamento",
        json={"order_id": order_id, "status": "paid"},
        headers={"X-Webhook-Token": app.config["PAYMENT_WEBHOOK_TOKEN"]},
    )
    assert webhook_response.status_code == 200

    with app.app_context():
        user = User.query.filter_by(username="usuario_comum").first()
        assert user is not None
        assert user.subscription_status == "active"
        assert user.subscription_expires_at is not None
        order = SubscriptionOrder.query.get(order_id)
        assert order is not None
        assert order.status == "paid"
        assert order.paid_at is not None


def test_subscription_requires_member_login(client):
    response = client.get("/assinatura", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_subscription_status_endpoint(auth_client):
    response = auth_client.get("/assinatura/status")
    assert response.status_code == 200
    assert "subscription_active" in response.json
    assert "pending_orders_count" in response.json


class _FakeWhatsAppManager:
    def __init__(self):
        self.state = {
            "status": "disconnected",
            "message": "WhatsApp desconectado.",
            "qr_code": None,
            "running": False,
            "profile_dir": "instance/whatsapp_sessions/user_2_usuario_comum",
            "session_persistent": True,
            "session_saved": False,
        }

    def start(self):
        self.state = {
            "status": "waiting_qr",
            "message": "Escaneie o QR Code com seu celular.",
            "qr_code": "data:image/png;base64,abc",
            "running": True,
            "profile_dir": "instance/whatsapp_sessions/user_2_usuario_comum",
            "session_persistent": True,
            "session_saved": True,
        }

    def stop(self):
        self.state = {
            "status": "disconnected",
            "message": "WhatsApp desconectado.",
            "qr_code": None,
            "running": False,
            "profile_dir": "instance/whatsapp_sessions/user_2_usuario_comum",
            "session_persistent": True,
            "session_saved": True,
        }

    def get_state(self):
        return dict(self.state)


def test_whatsapp_status_and_session_actions(auth_client, app):
    with app.app_context():
        auth_user = User.query.filter_by(username="usuario_comum").first()
        assert auth_user is not None
        manager_key = f"user_{auth_user.id}_usuario_comum"

    fake_manager = _FakeWhatsAppManager()
    app.extensions["whatsapp_managers"] = {manager_key: fake_manager}

    status_response = auth_client.get("/configuracoes/whatsapp/status")
    assert status_response.status_code == 200
    assert status_response.json["status"] == "disconnected"
    assert status_response.json["session_persistent"] is True
    assert "profile_dir" in status_response.json

    start_response = auth_client.post("/configuracoes/whatsapp/iniciar")
    assert start_response.status_code == 200
    assert start_response.json["status"] == "waiting_qr"
    assert start_response.json["qr_code"].startswith("data:image/png;base64,")
    assert start_response.json["session_saved"] is True

    stop_response = auth_client.post("/configuracoes/whatsapp/parar")
    assert stop_response.status_code == 200
    assert stop_response.json["status"] == "disconnected"


def test_register_creates_regular_user_and_logs_in(client, app):
    response = client.post(
        "/cadastro",
        data={
            "username": "rafa",
            "email": "rafa@teste.com",
            "password": "123456",
            "confirm_password": "123456",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Conta criada com sucesso." in response.data

    with app.app_context():
        user = User.query.filter_by(username="rafa").first()
        assert user is not None
        assert user.is_admin is False


def test_master_admin_credentials_exist(app):
    with app.app_context():
        master = User.query.filter_by(username="Usuario_Master").first()
        assert master is not None
        assert master.is_admin is True
        assert master.check_password("load_usuario@_master") is True


def test_login_redirects_when_credentials_are_invalid(client, app):
    with app.app_context():
        user = User(username="joao", email="joao@teste.com", is_admin=False)
        user.set_password("123456")
        db.session.add(user)
        db.session.commit()

    response = client.post(
        "/login",
        data={"username": "joao", "password": "errada"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Credenciais invalidas." in response.data


def test_suspended_member_cannot_login(client, app):
    with app.app_context():
        user = User(username="suspenso", email="suspenso@teste.com", is_admin=False, subscription_status="suspended")
        user.set_password("123456")
        db.session.add(user)
        db.session.commit()

    response = client.post(
        "/login",
        data={"username": "suspenso", "password": "123456"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Sua conta esta suspensa. Fale com o suporte." in response.data


def test_common_login_rejects_admin_credentials(client):
    response = client.post(
        "/login",
        data={"username": "Usuario_Master", "password": "load_usuario@_master"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Usuario administrativo deve acessar /admin/login." in response.data


def test_admin_panel_requires_admin(admin_client):
    response = admin_client.get("/admin/usuarios")
    assert response.status_code == 200
    assert b"Usuarios cadastrados" in response.data


def test_admin_has_access_to_database_page(admin_client):
    response = admin_client.get("/admin/banco")
    assert response.status_code == 200
    assert b"Banco de dados" in response.data


def test_non_admin_cannot_access_admin_panel(auth_client):
    response = auth_client.get("/admin/usuarios", follow_redirects=False)
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def test_admin_cannot_access_member_area(admin_client):
    response = admin_client.get("/fornecedores", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_protected_routes_redirect_when_not_logged(client):
    response = client.get("/fornecedores", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_admin_routes_redirect_to_admin_login_when_not_logged(client):
    response = client.get("/admin/usuarios", follow_redirects=False)
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def test_admin_login_rejects_non_admin_credentials(client, app):
    with app.app_context():
        user = User(username="comum_teste", email="comum@teste.com", is_admin=False)
        user.set_password("123456")
        db.session.add(user)
        db.session.commit()

    response = client.post(
        "/admin/login",
        data={"username": "comum_teste", "password": "123456"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Credenciais administrativas invalidas." in response.data


def test_member_and_admin_can_stay_logged_at_same_time(client, app):
    with app.app_context():
        now = server_now()
        member = User(
            username="duplo",
            email="duplo@teste.com",
            is_admin=False,
            subscription_status="active",
            subscription_started_at=now,
            subscription_last_payment_at=now,
            subscription_expires_at=now + timedelta(days=30),
        )
        member.set_password("123456")
        db.session.add(member)
        db.session.commit()
        member_id = member.id

        admin = User.query.filter_by(username="Usuario_Master").first()
        assert admin is not None
        admin_id = admin.id

    with client.session_transaction() as session_store:
        session_store["member_user_id"] = member_id
        session_store["admin_user_id"] = admin_id

    member_response = client.get("/fornecedores")
    assert member_response.status_code == 200

    admin_response = client.get("/admin/usuarios")
    assert admin_response.status_code == 200


def test_admin_dashboard_renders_new_sections(admin_client):
    response = admin_client.get("/admin")

    assert response.status_code == 200
    assert b"Painel administrativo" in response.data
    assert b"Financeiro e assinaturas" in response.data
    assert b"Funil de ativacao" in response.data
    assert b"Saude de envios" in response.data
    assert b"Moderacao rapida" in response.data
    assert b"Auditoria administrativa" in response.data
    assert b"Saude do banco" in response.data
    assert b"Busca global e suporte" in response.data
    assert b"Visao rapida de suporte" in response.data


def test_admin_moderation_routes_create_audit_logs(admin_client, app):
    with app.app_context():
        now = server_now()
        user = User(
            username="moderado",
            email="moderado@teste.com",
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

    suspend_response = admin_client.post(f"/admin/usuarios/{user_id}/suspender", follow_redirects=True)
    assert suspend_response.status_code == 200
    assert b"Usuario suspenso com sucesso." in suspend_response.data

    with app.app_context():
        db_user = User.query.get(user_id)
        assert db_user is not None
        assert db_user.subscription_status == "suspended"
        assert AdminAuditLog.query.filter_by(action="suspend_user", target_user_id=user_id).count() == 1

    force_logout_response = admin_client.post(f"/admin/usuarios/{user_id}/forcar-logout", follow_redirects=True)
    assert force_logout_response.status_code == 200
    assert b"Logout forcado aplicado." in force_logout_response.data

    with app.app_context():
        forced_map = app.extensions.get("forced_member_reauth", {})
        assert user_id in forced_map
        assert AdminAuditLog.query.filter_by(action="force_logout", target_user_id=user_id).count() == 1

    reactivate_response = admin_client.post(f"/admin/usuarios/{user_id}/reativar", follow_redirects=True)
    assert reactivate_response.status_code == 200
    assert b"Usuario reativado." in reactivate_response.data

    with app.app_context():
        db_user = User.query.get(user_id)
        assert db_user is not None
        assert db_user.subscription_status == "active"
        assert AdminAuditLog.query.filter_by(action="reactivate_user", target_user_id=user_id).count() == 1


def test_admin_reset_whatsapp_session_removes_profile_directory(admin_client, app):
    class _FakeManager:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    with app.app_context():
        user = User(username="wareset", email="wareset@teste.com", is_admin=False, subscription_status="inactive")
        user.set_password("123456")
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        manager_key = f"user_{user.id}_wareset"

        profile_dir = Path(app.instance_path) / "whatsapp_sessions" / manager_key
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "test.txt").write_text("abc", encoding="utf-8")

        fake_manager = _FakeManager()
        app.extensions["whatsapp_managers"] = {manager_key: fake_manager}

    response = admin_client.post(f"/admin/usuarios/{user_id}/resetar-whatsapp", follow_redirects=True)
    assert response.status_code == 200
    assert b"Sessao WhatsApp do usuario foi resetada." in response.data

    with app.app_context():
        assert not profile_dir.exists()
        assert app.extensions.get("whatsapp_managers", {}).get(manager_key) is None
        assert AdminAuditLog.query.filter_by(action="reset_whatsapp_session", target_user_id=user_id).count() == 1


def test_admin_database_page_includes_audit_table_count(admin_client, app):
    with app.app_context():
        admin = User.query.filter_by(username="Usuario_Master").first()
        assert admin is not None
        db.session.add(AdminAuditLog(admin_user_id=admin.id, action="sample_action", details="ok"))
        db.session.commit()

    response = admin_client.get("/admin/banco")

    assert response.status_code == 200
    assert b"admin_audit_logs" in response.data


def test_admin_dashboard_global_search_and_support_panel(admin_client, app):
    with app.app_context():
        user = User(username="support_user", email="support@teste.com", is_admin=False, subscription_status="inactive")
        user.set_password("123456")
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        db.session.add(Supplier(name="Fornecedor Support", phone="+5511999991234", owner_user_id=user.id))
        db.session.commit()

    response = admin_client.get(f"/admin?q=support&support_user_id={user_id}")

    assert response.status_code == 200
    assert b"Fornecedor Support" in response.data
    assert b"support_user" in response.data
    assert b"Visao rapida de suporte" in response.data


