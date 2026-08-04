"""
Testes de funções puras de main.py usadas tanto por /detalhe quanto pela
comparação de catálogo por IA (custo/margem, validação técnica). Rode com:
cd backend && pytest
"""
from app.models import Produto
from app.main import _custo_e_margem, _validacao_tecnica_json


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
