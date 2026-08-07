"""
Testes de funções puras de main.py usadas tanto por /detalhe quanto pela
comparação de catálogo por IA (custo/margem, validação técnica). Rode com:
cd backend && pytest
"""
import pytest

from app.models import Produto
from app.main import _custo_e_margem, _qtd_embalagem_pncp, _validacao_tecnica_json


def _produto(**kwargs):
    base = dict(id=1, descricao="Produto teste")
    base.update(kwargs)
    return Produto(**base)


def test_custo_e_margem_calcula_direto_sem_embalagem():
    p = _produto(preco_custo=10.0, itens_por_unidade=None)
    r = _custo_e_margem(15.0, p)
    assert r["custo_comparavel"] == 10.0
    assert r["margem"] == 5.0
    assert r["margem_pct"] == 33.3
    assert r["alerta_unidade"] is False


def test_custo_e_margem_divide_pela_embalagem():
    # produto vendido em resma de 500 folhas — custo precisa ir pra base
    # "por folha" pra comparar com o valor unitário do item (por folha)
    p = _produto(preco_custo=25.0, itens_por_unidade=500)
    r = _custo_e_margem(0.10, p)
    assert r["custo_comparavel"] == 0.05
    assert r["margem"] == 0.05


def test_custo_e_margem_sem_dado_suficiente_retorna_none():
    p = _produto(preco_custo=None, itens_por_unidade=None)
    r = _custo_e_margem(15.0, p)
    assert r["margem"] is None and r["custo_comparavel"] is None
    r2 = _custo_e_margem(None, _produto(preco_custo=10.0))
    assert r2["margem"] is None


def test_custo_e_margem_marca_alerta_unidade_quando_absurda():
    p = _produto(preco_custo=1000.0, itens_por_unidade=None)
    r = _custo_e_margem(1.0, p)
    assert r["alerta_unidade"] is True


def test_qtd_embalagem_pncp_extrai_numero():
    assert _qtd_embalagem_pncp("Embalagem 500 FL") == 500
    assert _qtd_embalagem_pncp("Unidade") is None
    assert _qtd_embalagem_pncp(None) is None
    assert _qtd_embalagem_pncp("Caixa com 12 UN") == 12


def test_custo_e_margem_nao_divide_quando_orgao_ja_cota_por_embalagem():
    """Caso real de produção: edital de papel A4 cotava R$24,50 por RESMA
    de 500 folhas (unidadeMedida="Embalagem 500 FL" do PNCP), igual à
    embalagem do produto do catálogo (itens_por_unidade=500, R$29,57 por
    resma). Dividir o custo do catálogo por 500 comparava preço por resma
    com preço por folha — "margem" de 99,8% fictícia, quando na real era
    prejuízo (24,50 < 29,57 por resma)."""
    p = _produto(preco_custo=29.57, itens_por_unidade=500)
    r = _custo_e_margem(24.50, p, unidade_medida_item="Embalagem 500 FL")
    assert r["custo_comparavel"] == 29.57
    assert r["margem"] == pytest.approx(24.50 - 29.57, abs=1e-2)
    assert r["margem_pct"] < 0   # é prejuízo, não 99,8% de lucro
    assert r["alerta_unidade"] is False


def test_custo_e_margem_sem_unidade_medida_mantem_comportamento_anterior():
    """Sem unidadeMedida (item coletado antes desse campo existir, ou fonte
    que não fornece), continua dividindo como sempre — mesmo comportamento
    de test_custo_e_margem_divide_pela_embalagem, só que passando o novo
    parâmetro como None explicitamente."""
    p = _produto(preco_custo=25.0, itens_por_unidade=500)
    r = _custo_e_margem(0.10, p, unidade_medida_item=None)
    assert r["custo_comparavel"] == 0.05


def test_custo_e_margem_embalagens_de_tamanhos_diferentes_marca_alerta():
    """Órgão cota por caixa de 12, produto vendido em pacote de 24 — nem
    dividir nem comparar direto é confiável; sinaliza pro usuário conferir."""
    p = _produto(preco_custo=48.0, itens_por_unidade=24)
    r = _custo_e_margem(2.50, p, unidade_medida_item="Caixa com 12 UN")
    assert r["alerta_unidade"] is True


def test_validacao_tecnica_fita_adesiva_largura_divergente_nao_passa_silenciosa():
    """Caso real reportado: edital pede fita "largura: 50" (mm) e a IA
    sugeriu uma fita "48mm x 50m" — categoria certa, medida errada. A
    validação determinística tem que sinalizar isso (crítica ou aviso),
    nunca classificar como 'Atende' pleno sem ressalva."""
    item_desc = ("Fita Adesiva Embalagem material: polipropileno, comprimento: 50, largura: 50, "
                "aplicação: empacotamento em geral, características adicionais: transparente")
    p = _produto(descricao="Fita Adesiva Transparente Hot Melt 48mm x 50m", preco_custo=8.0)
    vt = _validacao_tecnica_json(item_desc, p, 1.0)
    assert vt is not None
    assert vt["classificacao"] != "Atende"
    assert vt["criticas"] or vt["avisos"]


def test_validacao_tecnica_sem_nada_verificavel_retorna_none():
    p = _produto(descricao="Serviço de consultoria genérica")
    vt = _validacao_tecnica_json("Serviço de consultoria", p, 1.0)
    assert vt is None
