"""
Testes de _proposta_payload() — o campo "itens_edital" expõe TODOS os itens
que o edital pede (não só os já incluídos na proposta), pra o front montar
o modal de "adicionar item" restrito ao que o edital de fato pede, em vez
de deixar digitar qualquer descrição livre. Sem rede, sem banco. Rode com:
cd backend && pytest
"""
from app.main import _proposta_payload
from app.models import Edital, ItemEdital, Proposta


def _edital(itens):
    ed = Edital(id=1, fonte="PNCP", id_externo="1-000001/2026",
                cnpj_orgao="12345678000199", objeto="Aquisição de material",
                orgao="Órgão Teste")
    ed.itens = itens
    return ed


def test_itens_edital_traz_todos_os_itens_do_edital_independente_da_proposta():
    ed = _edital([
        ItemEdital(numero=1, descricao="Papel A4", quantidade=100, valor_unitario=25.0),
        ItemEdital(numero=2, descricao="Caneta esferográfica", quantidade=50, valor_unitario=1.5),
    ])
    payload = _proposta_payload(ed, prop=None)
    assert payload["itens_edital"] == [
        {"numero": 1, "descricao": "Papel A4", "quantidade": 100, "valor_unitario": 25.0},
        {"numero": 2, "descricao": "Caneta esferográfica", "quantidade": 50, "valor_unitario": 1.5},
    ]


def test_itens_edital_continua_completo_mesmo_com_proposta_ja_salva_com_menos_itens():
    """A proposta salva pode ter menos itens que o edital (usuário excluiu um
    da proposta) — itens_edital não pode encolher junto, senão o modal de
    "adicionar item" nunca mostraria de volta o que foi removido."""
    ed = _edital([
        ItemEdital(numero=1, descricao="Papel A4", quantidade=100, valor_unitario=25.0),
        ItemEdital(numero=2, descricao="Caneta esferográfica", quantidade=50, valor_unitario=1.5),
    ])
    prop = Proposta(edital_id=1, itens=[
        {"descricao": "Papel A4", "quantidade": 100, "custo_unit": 20.0, "preco_unit": 25.0},
    ])
    payload = _proposta_payload(ed, prop)
    assert len(payload["itens"]) == 1
    assert len(payload["itens_edital"]) == 2


def test_item_sem_quantidade_ou_valor_vira_zero_em_vez_de_none():
    ed = _edital([ItemEdital(numero=1, descricao="Item sem preço definido")])
    payload = _proposta_payload(ed, prop=None)
    assert payload["itens_edital"] == [
        {"numero": 1, "descricao": "Item sem preço definido", "quantidade": 0, "valor_unitario": 0},
    ]
