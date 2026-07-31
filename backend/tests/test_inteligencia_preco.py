"""
Testes da normalização de preço por unidade da Inteligência de Preço
(sem banco, sem HTTP). Rode com:  cd backend && pytest
"""
from app.main import _valor_unitario_normalizado
from app.models import ItemEdital


def _item(descricao, valor):
    return ItemEdital(descricao=descricao, valor_unitario=valor)


def test_normaliza_caixa_com_n_unidades():
    """Caso real (auditoria em produção): "Envelope Kraft" variava de
    R$0,84 a R$164,80 porque alguns editais cotam "1 envelope" e outros
    "caixa com 250 unidades" — o valor bruto não distingue as duas escalas."""
    it = _item("Envelope saco pardo, formato 250 x 353 mm, kraft natural 90 g/m2, "
               "caixa com 250 unidades", 122.10)
    assert _valor_unitario_normalizado(it) == 122.10 / 250


def test_normaliza_abreviacao_und():
    it = _item("Envelope pardo 34x24 caixa com 250 und", 132.60)
    assert _valor_unitario_normalizado(it) == 132.60 / 250


def test_normaliza_parentetico():
    it = _item("ENVELOPE PARDO 22X32 (100 UND)", 75.73)
    assert _valor_unitario_normalizado(it) == 75.73 / 100


def test_normaliza_caixa_com_n_sem_palavra_unidade():
    """"Caixa com 100" sem "unidades"/"un" depois — comum quando o rótulo
    "caixa"/"pacote" já deixa implícito que o número é contagem de peças."""
    it = _item("Envelope 240x340, modelo ouro - Caixa com 100", 61.12)
    assert _valor_unitario_normalizado(it) == 61.12 / 100


def test_nao_normaliza_item_sem_embalagem_multipla():
    it = _item("ENVELOPE SACO KRAFT NATURAL 240X340 75G", 0.85)
    assert _valor_unitario_normalizado(it) == 0.85


def test_nao_normaliza_falso_positivo_com_n_dias():
    """"com N" sem palavra de contagem de peça (unidades/un/peças/folhas)
    logo depois não pode disparar — "garantia com 100 dias" não é embalagem
    de 100 peças."""
    it = _item("Garantia com 100 dias de cobertura", 5.0)
    assert _valor_unitario_normalizado(it) == 5.0


def test_sem_valor_continua_none():
    it = _item("Envelope caixa com 250 unidades", None)
    assert _valor_unitario_normalizado(it) is None
    it2 = _item("Envelope caixa com 250 unidades", 0)
    assert _valor_unitario_normalizado(it2) is None
