class SafeMap(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_message(template_body: str, supplier_data: dict[str, str], extra_data: dict[str, str] | None = None) -> str:
    payload = SafeMap(
        {
            "nome": supplier_data.get("name", ""),
            "empresa": supplier_data.get("company", ""),
            "telefone": supplier_data.get("phone", ""),
            "email": supplier_data.get("email", ""),
        }
    )
    if extra_data:
        payload.update(extra_data)

    try:
        return template_body.format_map(payload)
    except Exception:
        return template_body
