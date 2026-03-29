# Migracoes de Banco (Alembic)

Comandos principais:

```bash
alembic upgrade head
alembic revision --autogenerate -m "descricao"
```

Observacao:
- Este projeto ainda roda `db.create_all()` para compatibilidade local.
- Para ambientes de producao, use somente Alembic para evolucao de schema.
