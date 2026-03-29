# App Orcamentos

MVP de plataforma web para:

- cadastro de fornecedores (CRUD)
- importacao de contatos (CSV e XLSX)
- templates de mensagens com placeholders
- envio em lote (simulado ou real via PyWhatKit)
- login do WhatsApp Web por sessao Playwright com QR Code
- dashboard com metricas e atividades recentes
- autenticacao com login/cadastro
- painel de administrador com listagem de usuarios
- admin com acesso apenas a: dashboard de usuarios, usuarios cadastrados/renovados, remocao de usuarios e visao do banco
- login admin separado em `/admin/login` (login comum nao acessa rotas admin)
- validacao e deduplicacao de fornecedores por telefone/email
- paginacao e filtros em listas
- exportacao CSV de fornecedores e historico
- configuracao de provedor WhatsApp por ambiente (`simulado` ou `pywhatkit`)

## Requisitos

- Python 3.11+

## Como executar

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
python main.py
```

Acesse: `http://127.0.0.1:5000`

Para usar envio real por WhatsApp com PyWhatKit:

```bash
pip install pywhatkit
```

No `.env`, configure:

```env
WHATSAPP_PROVIDER=pywhatkit
WHATSAPP_PYWHATKIT_WAIT_TIME=15
WHATSAPP_PYWHATKIT_CLOSE_TIME=3
WHATSAPP_PYWHATKIT_CLOSE_TAB=True
WHATSAPP_SEND_MIN_INTERVAL_SECONDS=1.0
WHATSAPP_SEND_MAX_INTERVAL_SECONDS=1.8
WHATSAPP_SEND_BURST_SIZE=10
WHATSAPP_SEND_BURST_PAUSE_MIN_SECONDS=6.0
WHATSAPP_SEND_BURST_PAUSE_MAX_SECONDS=10.0
```

Com `WHATSAPP_PROVIDER=simulado`, o app apenas registra no historico sem envio real.

Fluxo no app:

1. Abra `Configuracoes` e clique em `Iniciar login WhatsApp` (Playwright).
2. Escaneie o QR Code e aguarde status `Conectado`.
3. Envie mensagens na tela `Mensagens` usando o provedor PyWhatKit.

Fuso horario do servidor: configure `APP_TIMEZONE` no `.env` (ex.: `America/Manaus`).

## Banco de dados

Por padrao usa SQLite (`orcamentos.db`).

Para PostgreSQL, configure:

```bash
set DATABASE_URL=postgresql+psycopg://usuario:senha@localhost:5432/orcamentos
```

## Testes

```bash
pip install -e ".[dev]"
pytest -q
```

## Placeholders de mensagem

- `{nome}`
- `{empresa}`
- `{telefone}`
- `{email}`
- `{produto}`
