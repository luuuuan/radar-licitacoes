"""
Testes do motor de correspondência (sem banco, sem HTTP).
Rode com:  cd backend && pytest
"""
import pytest

from app.config import settings
from app.matching import engine as engine_mod
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


# ---------------------------------------------------------------------------
# Combinação do score textual com a IA semântica (embeddings mockados —
# sem chamada de rede real).
# ---------------------------------------------------------------------------
def _ligar_ia_falsa(monkeypatch, ia_score, ia_prod):
    """Liga o modo IA e substitui a geração de embeddings e o cálculo de
    similaridade semântica por valores controlados, sem tocar na rede."""
    monkeypatch.setattr(engine_mod, "ia_disponivel", lambda key: True)
    monkeypatch.setattr(
        engine_mod, "_ia_embeddings",
        lambda textos, timeout=30, api_key=None: [[1.0]] * len(textos))
    monkeypatch.setattr(
        MatchingEngine, "_ia_score_item",
        lambda self, item_emb: (ia_score, ia_prod))


def test_ia_sem_sinal_nao_penaliza_score_textual(monkeypatch):
    """Quando a IA não confirma nada (cosseno abaixo do IA_FLOOR -> ia_sc=0),
    isso é "sem opinião", não "sinal negativo" — não deve derrubar um score
    textual que já era bom."""
    catalogo = _catalogo()
    monkeypatch.setattr(MatchingEngine, "_score_item",
                         lambda self, item: (0.5, catalogo[0], "similaridade textual"))
    _ligar_ia_falsa(monkeypatch, ia_score=0.0, ia_prod=None)

    eng = MatchingEngine(catalogo, usar_ia=True, gemini_key="fake-key")
    r = eng.avaliar("Objeto", [ItemEdt(1, "qualquer coisa")])

    assert r.detalhe[0]["score_item"] == pytest.approx(0.5, abs=1e-3)
    assert r.detalhe[0]["motivo"] == "similaridade textual"


def test_ia_com_sinal_combina_score(monkeypatch):
    """Quando a IA tem sinal (ia_sc > 0), ela deve continuar sendo combinada
    com o score textual pela média ponderada de IA_PESO, podendo inclusive
    trocar o produto sugerido."""
    catalogo = _catalogo()
    produto_ia = catalogo[1]
    monkeypatch.setattr(MatchingEngine, "_score_item",
                         lambda self, item: (0.5, catalogo[0], "similaridade textual"))
    _ligar_ia_falsa(monkeypatch, ia_score=0.8, ia_prod=produto_ia)

    eng = MatchingEngine(catalogo, usar_ia=True, gemini_key="fake-key")
    r = eng.avaliar("Objeto", [ItemEdt(1, "qualquer coisa")])

    esperado = 0.5 * (1 - settings.IA_PESO) + 0.8 * settings.IA_PESO
    assert r.detalhe[0]["score_item"] == pytest.approx(esperado, abs=1e-3)
    assert r.detalhe[0]["produto_id"] == produto_ia.id
    assert "IA" in r.detalhe[0]["motivo"]
