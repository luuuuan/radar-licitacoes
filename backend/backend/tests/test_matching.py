"""
Testes do motor de correspondência (sem banco, sem HTTP).
Rode com:  cd backend && pytest
"""
from app.matching.engine import (
    MatchingEngine, ProdutoCat, ItemEdt, aplicar_regras_exclusao, normalizar, so_digitos,
)


def _catalogo():
    return [
        ProdutoCat(id=1, descricao="Papel sulfite A4 75g resma branca",
                   ncm="48025590", palavras_chave="papel a4, sulfite, resma"),
        ProdutoCat(id=2, descricao="Caneta esferográfica azul",
                   catmat="279317", palavras_chave="caneta, esferografica"),
        ProdutoCat(id=3, descricao="Álcool em gel 70% antisséptico 500ml",
                   palavras_chave="alcool gel, antisseptico"),
    ]


def test_codigo_ncm_exato_bate():
    eng = MatchingEngine(_catalogo())
    r = eng.avaliar("Aquisição de papel", [ItemEdt(1, "Papel branco", ncm="4802.55.90")])
    assert r.nivel == "forte"
    assert r.detalhe[0]["motivo"].startswith("código NCM")


def test_codigo_catmat_exato_bate():
    eng = MatchingEngine(_catalogo())
    r = eng.avaliar("Material", [ItemEdt(1, "Caneta qualquer", catalogo_codigo="279317")])
    assert r.detalhe[0]["produto_id"] == 2
    assert r.detalhe[0]["score_item"] == 1.0


def test_similaridade_textual_bate():
    eng = MatchingEngine(_catalogo())
    r = eng.avaliar("Higiene", [ItemEdt(1, "Álcool em gel 70%, frasco 500ml com válvula pump")])
    assert r.itens_compativeis == 1
    assert r.nivel in ("medio", "forte")


def test_palavra_chave_bate():
    eng = MatchingEngine(_catalogo())
    r = eng.avaliar("Expediente", [ItemEdt(1, "Resma de papel tamanho A4 sulfite")])
    assert r.itens_compativeis == 1


def test_item_irrelevante_nao_bate():
    eng = MatchingEngine(_catalogo())
    r = eng.avaliar("Obra", [ItemEdt(1, "Serviço de pavimentação asfáltica e drenagem")])
    assert r.itens_compativeis == 0
    assert r.nivel == "fraco"


def test_edital_grande_um_item_fraco_nao_e_forte():
    eng = MatchingEngine(_catalogo())
    itens = [ItemEdt(i, f"Item irrelevante numero {i} sobre engenharia civil") for i in range(40)]
    itens.append(ItemEdt(99, "papel"))
    r = eng.avaliar("Edital grande", itens)
    assert r.nivel != "forte"


def test_regra_exclusao_por_termo():
    ignora = aplicar_regras_exclusao(
        "Contratação de empresa de engenharia para reforma", [],
        termos=["engenharia"], categoria_pncp=None, categorias_excluidas=[])
    assert ignora is True


def test_regra_exclusao_por_categoria():
    ignora = aplicar_regras_exclusao(
        "Qualquer objeto", [], termos=[], categoria_pncp="9",
        categorias_excluidas=["9"])
    assert ignora is True


def test_normalizar_remove_acentos_e_pontuacao():
    assert normalizar("Álcool 70%, em GEL!") == "alcool 70 em gel"


def test_so_digitos():
    assert so_digitos("4802.55.90") == "48025590"
