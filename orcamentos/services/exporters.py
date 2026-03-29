import csv
import io
from collections.abc import Iterable

from ..models import MessageHistory, Supplier


def suppliers_to_csv_bytes(rows: Iterable[Supplier]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "nome", "empresa", "telefone", "email", "observacoes", "criado_em"])
    for item in rows:
        writer.writerow(
            [
                item.id,
                item.name,
                item.company or "",
                item.phone or "",
                item.email or "",
                item.notes or "",
                item.created_at.isoformat(timespec="seconds"),
            ]
        )
    return output.getvalue().encode("utf-8-sig")


def history_to_csv_bytes(rows: Iterable[MessageHistory]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "enviado_em", "fornecedor", "template", "status", "mensagem"])
    for item in rows:
        writer.writerow(
            [
                item.id,
                item.sent_at.isoformat(timespec="seconds"),
                item.supplier.name if item.supplier else "",
                item.template.name if item.template else "customizada",
                item.status,
                item.content,
            ]
        )
    return output.getvalue().encode("utf-8-sig")
