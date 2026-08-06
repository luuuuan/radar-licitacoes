"""
Testes do motor de correspondência (sem banco, sem HTTP — o reranker é
mockado). Rode com:  cd backend && pytest
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


@pytest.fixture(autouse=True)
def _deepinfra_configurada(monkeypatch):
    """Todo teste deste arquivo assume DEEPINFRA_API_KEY configurada (o
    reranker em si é sempre mockado — nenhuma chamada de rede real)."""
    monkeypatch.setattr(settings, "DEEPINFRA_API_KEY", "fake-key")


def _mockar_reranker(monkeypatch, scores_por_indice: dict[int, float], default: float = 0.0):
    """Substitui a chamada real ao reranker por uma função que devolve os
    scores controlados por índice de produto (mesma ordem de `produtos`).
    `scores_por_indice` cobre só os produtos com sinal — o resto usa
    `default` (0.0, "sem relação nenhuma", combinando com o que a API real
    devolve em casos sem relação: sempre <=0.014 nos testes reais)."""
    def _fake_rerank(query, documentos, timeout=30, api_key=None, tentativas=2):
        return [scores_por_indice.get(i, default) for i in range(len(documentos))]
    monkeypatch.setattr(engine_mod, "_rerank", _fake_rerank)


def _engine(catalogo, monkeypatch, scores_por_indice: dict[int, float], default: float = 0.0):
    _mockar_reranker(monkeypatch, scores_por_indice, default)
    return MatchingEngine(catalogo, usar_ia=True)


def test_codigo_ncm_exato_bate(monkeypatch):
    catalogo = _catalogo()
    eng = _engine(catalogo, monkeypatch, {0: 0.95})   # produto 1 (índice 0) corrobora bem
    r = eng.avaliar("Aquisição de papel", [ItemEdt(1, "Papel branco", ncm="4802.55.90")])
    assert r.nivel == "forte"
    assert r.detalhe[0]["motivo"].startswith("código NCM")
    assert r.detalhe[0]["produto_id"] == 1


def test_codigo_catmat_exato_bate(monkeypatch):
    catalogo = _catalogo()
    eng = _engine(catalogo, monkeypatch, {1: 0.97})   # produto 2 (índice 1) corrobora bem
    r = eng.avaliar("Material", [ItemEdt(1, "Caneta qualquer", catalogo_codigo="279317")])
    assert r.detalhe[0]["produto_id"] == 2
    assert r.detalhe[0]["score_item"] == 1.0


def test_reranker_encontra_match_semantico(monkeypatch):
    catalogo = _catalogo()
    eng = _engine(catalogo, monkeypatch, {2: 0.93})   # produto 3 (álcool, índice 2)
    r = eng.avaliar("Higiene", [ItemEdt(1, "Álcool em gel 70%, frasco 500ml com válvula pump")])
    assert r.itens_compativeis == 1
    assert r.detalhe[0]["produto_id"] == 3
    assert r.nivel in ("medio", "forte")


def test_item_irrelevante_nao_bate(monkeypatch):
    catalogo = _catalogo()
    eng = _engine(catalogo, monkeypatch, {})   # nada corrobora — tudo fica no default 0.0
    r = eng.avaliar("Obra", [ItemEdt(1, "Serviço de pavimentação asfáltica e drenagem")])
    assert r.itens_compativeis == 0
    assert r.nivel == "fraco"


def test_edital_grande_um_item_fraco_nao_e_forte(monkeypatch):
    """40 itens irrelevantes + 1 que bate razoavelmente (produto 1, "papel")
    — o agregado não pode virar "forte" só por causa desse único item."""
    catalogo = _catalogo()
    def _fake_rerank(query, documentos, timeout=30, api_key=None, tentativas=2):
        if "papel" in query.lower():
            return [0.6, 0.0, 0.0]
        return [0.0, 0.0, 0.0]
    monkeypatch.setattr(engine_mod, "_rerank", _fake_rerank)
    eng = MatchingEngine(catalogo, usar_ia=True)
    itens = [ItemEdt(i, f"Item irrelevante numero {i} sobre engenharia civil") for i in range(40)]
    itens.append(ItemEdt(99, "papel"))
    r = eng.avaliar("Edital grande", itens)
    assert r.nivel != "forte"


def test_sem_deepinfra_configurada_nao_bate_nada(monkeypatch):
    """Sem a chave (ou reranker indisponível), o motor fica só no código
    exato sem corroboração — ou seja, não bate nada, mas não quebra."""
    monkeypatch.setattr(settings, "DEEPINFRA_API_KEY", "")
    eng = MatchingEngine(_catalogo(), usar_ia=True)
    assert eng.usar_ia is False
    r = eng.avaliar("Aquisição de papel", [ItemEdt(1, "Papel branco", ncm="4802.55.90")])
    assert r.itens_compativeis == 0
    assert r.detalhe == []


def test_reranker_indisponivel_nao_quebra(monkeypatch):
    """Chamada ao reranker falhando (rede, HTTP, etc.) devolve None — o
    motor trata como "sem sinal disponível", não deixa a exceção subir."""
    monkeypatch.setattr(engine_mod, "_rerank", lambda *a, **kw: None)
    eng = MatchingEngine(_catalogo(), usar_ia=True)
    r = eng.avaliar("Aquisição de papel", [ItemEdt(1, "Papel branco", ncm="4802.55.90")])
    assert r.itens_compativeis == 0
    assert r.detalhe == []


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
# Corroboração de código exato pelo reranker — casos reais de produção que
# motivaram a regra (código fiscal amplo não garante ser o MESMO produto).
# ---------------------------------------------------------------------------
def test_codigo_exato_com_texto_relacionado_e_confianca_alta(monkeypatch):
    """Código bate E o reranker corrobora — confia automático."""
    catalogo = _catalogo()
    eng = _engine(catalogo, monkeypatch, {0: 0.9})
    r = eng.avaliar("Aquisição de papel", [ItemEdt(1, "Papel branco", ncm="4802.55.90")])
    assert r.detalhe[0]["confianca"] == "alta"
    assert r.detalhe[0]["produto_id"] == 1


def test_codigo_exato_sem_relacao_textual_nao_vira_candidato_nenhum(monkeypatch):
    """Caso real de produção — RECORRENTE: item "Álcool Etílico ... gel ...
    70% v/v" batia por NCM idêntico com "Desinfetante 5 litros Lavanda" —
    mesmo código fiscal (categoria ampla de produto de limpeza/higiene), mas
    os produtos não têm nada a ver. Testado contra a API REAL do reranker
    (não só mockado aqui): esse par pontuou 0.0014 — bem abaixo do piso.
    Sem corroboração nenhuma, o código exato nem vira candidato — cai pro
    fluxo normal, que aqui não acha nada (catálogo genuinamente não tem
    produto de álcool)."""
    catalogo = [ProdutoCat(id=1, descricao="Desinfetante 5 litros Lavanda 9007 Urca",
                           ncm="34029090", palavras_chave="desinfetante, lavanda, limpeza")]
    eng = _engine(catalogo, monkeypatch, {0: 0.0014})   # score real observado no teste ao vivo
    r = eng.avaliar("Material de limpeza e higiene", [ItemEdt(
        1, "Álcool Etílico composição básica: com emoliente, forma farmacêutica: gel, "
           "teor alcoólico: 70% v/v", ncm="3402.90.90")])
    assert r.detalhe == []
    assert r.itens_compativeis == 0


def test_codigo_exato_sem_corroboracao_nao_ofusca_match_textual_real(monkeypatch):
    """Quando o código exato bate errado (sem corroboração) mas o catálogo
    TEM um produto de verdade compatível em outro lugar, esse produto real
    tem que vencer — o código exato sem suporte nenhum não pode nem
    aparecer como candidato, muito menos ofuscar o match certo."""
    catalogo = [
        ProdutoCat(id=1, descricao="Desinfetante 5 litros Lavanda 9007 Urca", ncm="34029090",
                   palavras_chave="desinfetante lavanda, desinfetante piso"),
        ProdutoCat(id=2, descricao="Álcool Etílico Gel 70% Antisséptico 500ml",
                   palavras_chave="alcool gel, antisseptico, alcool 70"),
    ]
    eng = _engine(catalogo, monkeypatch, {0: 0.001, 1: 0.97})
    r = eng.avaliar("Aquisição de álcool", [ItemEdt(
        1, "Álcool Etílico tipo: hidratado, teor alcoólico: 70% ( 70°gl), "
           "apresentação: glicerinado, líquido", ncm="3402.90.90")])
    item = r.detalhe[0]
    assert item["produto_id"] == 2
    assert item["confianca"] == "alta"
    assert all(c["produto_id"] != 1 for c in item["candidatos"])


def test_codigo_catmat_exato_sem_corroboracao_textual_nao_vira_candidato(monkeypatch):
    """"Pasta L" (pasta de arquivo de escritório) x "CIMENTO de hidroxido de
    calcio" (material odontológico) compartilhando um CATMAT — falso
    positivo clássico de código fiscal amplo. Cai pro fluxo normal, que
    aqui não acha nada."""
    catalogo = [ProdutoCat(id=1, descricao="Pasta L", catmat="150123")]
    eng = _engine(catalogo, monkeypatch, {0: 0.0})
    r = eng.avaliar("Material odontológico",
                     [ItemEdt(1, "CIMENTO de hidroxido de calcio", catalogo_codigo="150123")])
    assert r.nivel == "fraco"
    assert r.detalhe == []


def test_candidatos_top3_ordenados_por_score(monkeypatch):
    catalogo = _catalogo()
    eng = _engine(catalogo, monkeypatch, {0: 0.4, 1: 0.9, 2: 0.35})
    r = eng.avaliar("Objeto", [ItemEdt(1, "algo")])
    candidatos = r.detalhe[0]["candidatos"]
    assert [c["produto_id"] for c in candidatos] == [2, 1, 3]
    assert candidatos[0]["score"] == 0.9


def test_candidato_abaixo_do_piso_de_sugestao_nao_aparece(monkeypatch):
    """Ruído do reranker em produto sem relação real (nunca exatamente 0)
    não pode aparecer como opção de "trocar produto"."""
    catalogo = _catalogo()
    eng = _engine(catalogo, monkeypatch, {0: 0.9, 1: 0.05})
    r = eng.avaliar("Objeto", [ItemEdt(1, "algo")])
    candidatos = r.detalhe[0]["candidatos"]
    assert all(c["produto_id"] != 2 for c in candidatos)
