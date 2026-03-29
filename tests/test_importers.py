from orcamentos.models import Supplier
from orcamentos.services.importers import import_suppliers_from_rows, infer_column_mapping, parse_rows_from_file


def test_import_suppliers_deduplicates_existing_and_batch():
    existing = [Supplier(name="A", phone="+5511999990000", email="x@x.com")]
    rows = [
        {"nome": "Fornecedor 1", "telefone": "(11) 99999-0000", "email": "novo@x.com"},
        {"nome": "Fornecedor 2", "telefone": "(11) 98888-7777", "email": "novo@x.com"},
        {"nome": "Fornecedor 3", "telefone": "(11) 95555-4444", "email": "ok@x.com"},
    ]

    created, result = import_suppliers_from_rows(rows, existing)

    assert result.imported == 1
    assert result.skipped == 2
    assert len(created) == 1
    assert created[0].name == "Fornecedor 3"
    assert created[0].phone == "+5511955554444"


def test_import_suppliers_requires_name_and_contact():
    rows = [
        {"nome": "", "telefone": "11999990000"},
        {"nome": "Sem contato", "telefone": "", "email": ""},
    ]
    created, result = import_suppliers_from_rows(rows, [])

    assert created == []
    assert result.imported == 0
    assert result.skipped == 2
    assert len(result.errors) == 2


def test_parse_rows_invalid_extension_raises():
    try:
        parse_rows_from_file("fornecedores.txt", b"abc")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_infer_column_mapping_by_header_variations():
    rows = [
        {
            "Contato principal": "Ana Souza",
            "Razao Social": "Alpha Ltda",
            "Whats App": "(11) 98888-7777",
            "E-mail comercial": "ana@alpha.com",
            "Anotacoes": "Prioridade alta",
        }
    ]
    mapping = infer_column_mapping(rows)

    assert mapping["name"] == "Contato principal"
    assert mapping["company"] == "Razao Social"
    assert mapping["phone"] == "Whats App"
    assert mapping["email"] == "E-mail comercial"
    assert mapping["notes"] == "Anotacoes"


def test_import_suppliers_handles_generic_headers_using_value_heuristics():
    rows = [
        {"col_a": "Joao Silva", "col_b": "joao@forn.com", "col_c": "(11) 91234-5678", "col_d": "Fornecedor A"},
        {"col_a": "Maria Lima", "col_b": "maria@forn.com", "col_c": "(11) 99888-7777", "col_d": "Fornecedor B"},
    ]

    created, result = import_suppliers_from_rows(rows, [])

    assert result.imported == 2
    assert result.skipped == 0
    assert created[0].name == "Joao Silva"
    assert created[0].email == "joao@forn.com"
    assert created[0].phone == "+5511912345678"
