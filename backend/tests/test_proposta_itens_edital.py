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


def test_itens_da_proposta_saem_ordenados_pelo_numero_do_item():
    """Pedido do usuário: a ordem dos itens (tela e PDF) segue o número do
    item no edital, não a ordem em que foram adicionados à proposta --
    Proposta.itens é uma lista JSON na ordem de inserção, que não bate
    necessariamente com a ordem numérica."""
    ed = _edital([
        ItemEdital(numero=1, descricao="Item 1", quantidade=1, valor_unitario=1.0),
        ItemEdital(numero=5, descricao="Item 5", quantidade=1, valor_unitario=1.0),
        ItemEdital(numero=3, descricao="Item 3", quantidade=1, valor_unitario=1.0),
    ])
    # adicionados fora de ordem: 5, depois 1, depois 3
    prop = Proposta(edital_id=1, itens=[
        {"numero": 5, "descricao": "Item 5", "quantidade": 1, "custo_unit": 0, "preco_unit": 5.0},
        {"numero": 1, "descricao": "Item 1", "quantidade": 1, "custo_unit": 0, "preco_unit": 1.0},
        {"numero": 3, "descricao": "Item 3", "quantidade": 1, "custo_unit": 0, "preco_unit": 3.0},
    ])
    payload = _proposta_payload(ed, prop)
    assert [i["numero"] for i in payload["itens"]] == [1, 3, 5]


def test_item_sem_numero_vai_pro_fim_mantendo_ordem_relativa():
    """Item sem número válido (digitado à mão, ou proposta salva antes dessa
    referência existir) não pode quebrar a ordenação -- vai pro fim."""
    ed = _edital([
        ItemEdital(numero=1, descricao="Item 1", quantidade=1, valor_unitario=1.0),
        ItemEdital(numero=2, descricao="Item 2", quantidade=1, valor_unitario=1.0),
    ])
    prop = Proposta(edital_id=1, itens=[
        {"numero": 2, "descricao": "Item 2", "quantidade": 1, "custo_unit": 0, "preco_unit": 2.0},
        {"descricao": "Item digitado à mão", "quantidade": 1, "custo_unit": 0, "preco_unit": 9.0},
        {"numero": 1, "descricao": "Item 1", "quantidade": 1, "custo_unit": 0, "preco_unit": 1.0},
    ])
    payload = _proposta_payload(ed, prop)
    assert [i.get("numero") for i in payload["itens"]] == [1, 2, None]


def test_proposta_com_itens_esvaziada_de_proposito_nao_volta_pro_esqueleto():
    """Achado real (usuário reportou item excluído da cotação reaparecendo):
    Proposta.itens=[] (usuário removeu TODOS os itens da cotação/proposta)
    é um estado válido e proposital -- "if prop and prop.itens" (lista
    vazia é falsy) tratava isso igual a "nunca salvou proposta nenhuma" e
    devolvia o esqueleto com TODOS os itens do edital de volta, inclusive
    os que o usuário tinha acabado de tirar. "existe" continuava True (a
    linha de Proposta existe, só sem itens), uma combinação inconsistente:
    diz que existe mas mostra dado que não é o que foi salvo."""
    ed = _edital([
        ItemEdital(numero=1, descricao="Papel A4", quantidade=100, valor_unitario=25.0),
        ItemEdital(numero=2, descricao="Caneta esferográfica", quantidade=50, valor_unitario=1.5),
    ])
    prop = Proposta(edital_id=1, itens=[])
    payload = _proposta_payload(ed, prop)
    assert payload["itens"] == []
    assert payload["existe"] is True


def test_proposta_nunca_salva_continua_usando_esqueleto():
    """Contraste com o teste acima: prop=None (nunca salvou nada) é o único
    caso que deve cair no esqueleto com todos os itens do edital."""
    ed = _edital([
        ItemEdital(numero=1, descricao="Papel A4", quantidade=100, valor_unitario=25.0),
    ])
    payload = _proposta_payload(ed, prop=None)
    assert [i["numero"] for i in payload["itens"]] == [1]
    assert payload["existe"] is False


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


# ---- Achado real: proposta já salva exportava/mostrava a descrição
# CONGELADA de quando o item foi adicionado — se completar_descricao_itens()
# melhorasse o texto depois (PNCP vinha cortado, o documento oficial do
# edital tem a versão completa), a proposta continuava com a versão velha,
# cortada. A descrição atual do ItemEdital agora sempre prevalece. ----

def test_descricao_da_proposta_e_atualizada_a_partir_do_item_edital_atual():
    ed = _edital([
        ItemEdital(numero=24, descricao="PAPEL A4, CAIXA COM 10 RESMAS DE 500 FOLHAS CADA",
                  quantidade=10, valor_unitario=286.93),
    ])
    prop = Proposta(edital_id=1, itens=[
        {"numero": 24, "descricao": "PAPEL A4 210 X 297 75G/M", "quantidade": 10,
         "custo_unit": 250.0, "preco_unit": 286.93},
    ])
    payload = _proposta_payload(ed, prop)
    assert payload["itens"][0]["descricao"] == "PAPEL A4, CAIXA COM 10 RESMAS DE 500 FOLHAS CADA"
    # o resto do item (valores negociados pelo usuário) não muda
    assert payload["itens"][0]["custo_unit"] == 250.0


def test_item_sem_numero_mantem_descricao_salva_sem_alteracao():
    """Proposta salva antes do campo "numero" existir nos itens, ou
    descrição digitada à mão — sem "numero" não tem contra o que atualizar,
    fica como estava."""
    ed = _edital([ItemEdital(numero=1, descricao="Descrição atual do edital")])
    prop = Proposta(edital_id=1, itens=[
        {"descricao": "Descrição digitada pelo usuário", "quantidade": 1, "custo_unit": 0, "preco_unit": 0},
    ])
    payload = _proposta_payload(ed, prop)
    assert payload["itens"][0]["descricao"] == "Descrição digitada pelo usuário"


def test_esqueleto_sem_proposta_salva_inclui_numero_do_item():
    """Achado real: o esqueleto (proposta nunca salva, prop=None) não trazia
    "numero" nenhum -- a coluna "Nº" da proposta/PDF ficava sempre vazia
    pra quem exportasse sem antes ter salvo a proposta explicitamente."""
    ed = _edital([ItemEdital(numero=24, descricao="Papel A4", quantidade=10, valor_unitario=25.0)])
    payload = _proposta_payload(ed, prop=None)
    assert payload["itens"][0]["numero"] == 24


def test_numero_que_nao_bate_com_item_do_edital_mantem_descricao_salva():
    """Item removido do edital depois de já estar na proposta (ou número
    inválido) — sem correspondência real, não tem o que atualizar."""
    ed = _edital([ItemEdital(numero=1, descricao="Outro item")])
    prop = Proposta(edital_id=1, itens=[
        {"numero": 99, "descricao": "Item que não existe mais no edital",
         "quantidade": 1, "custo_unit": 0, "preco_unit": 0},
    ])
    payload = _proposta_payload(ed, prop)
    assert payload["itens"][0]["descricao"] == "Item que não existe mais no edital"
