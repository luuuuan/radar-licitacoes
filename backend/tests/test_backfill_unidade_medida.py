"""
Testes de _backfill_unidade_medida() — busca no PNCP a unidadeMedida de
itens já coletados antes desse campo existir (achado real: causava cálculo
de margem errado quando o órgão já cota o preço na mesma embalagem do
produto do catálogo — ver test_main_helpers.py). Sem rede — requests.get é
mockado. Rode com:  cd backend && pytest
"""
from unittest.mock import MagicMock, patch

from app.main import _backfill_unidade_medida
from app.models import Edital, ItemEdital


def _edital(**kwargs):
    base = dict(id=1, fonte="PNCP", id_externo="12345678000199-1-000028/2026",
               cnpj_orgao="12345678000199", objeto="Aquisição de papel", orgao="Órgão Teste")
    base.update(kwargs)
    return Edital(**base)


def _resposta_ok(itens_pncp):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = itens_pncp
    return r


def test_atualiza_item_sem_unidade_medida():
    ed = _edital()
    ed.itens = [ItemEdital(numero=1, descricao="Papel A4", unidade_medida=None)]
    resposta = _resposta_ok([{"numeroItem": 1, "unidadeMedida": "Embalagem 500 FL"}])
    with patch("app.main.requests.get", return_value=resposta):
        n = _backfill_unidade_medida(ed)
    assert n == 1
    assert ed.itens[0].unidade_medida == "Embalagem 500 FL"


def test_nao_sobrescreve_item_que_ja_tem_unidade_medida():
    ed = _edital()
    ed.itens = [ItemEdital(numero=1, descricao="Papel A4", unidade_medida="Unidade")]
    resposta = _resposta_ok([{"numeroItem": 1, "unidadeMedida": "Embalagem 500 FL"}])
    with patch("app.main.requests.get", return_value=resposta):
        n = _backfill_unidade_medida(ed)
    assert n == 0
    assert ed.itens[0].unidade_medida == "Unidade"


def test_casa_por_numero_do_item_nao_por_ordem():
    ed = _edital()
    ed.itens = [
        ItemEdital(numero=5, descricao="Item 5", unidade_medida=None),
        ItemEdital(numero=2, descricao="Item 2", unidade_medida=None),
    ]
    resposta = _resposta_ok([
        {"numeroItem": 2, "unidadeMedida": "Caixa 12 UN"},
        {"numeroItem": 5, "unidadeMedida": "Unidade"},
    ])
    with patch("app.main.requests.get", return_value=resposta):
        _backfill_unidade_medida(ed)
    assert ed.itens[0].unidade_medida == "Unidade"
    assert ed.itens[1].unidade_medida == "Caixa 12 UN"


def test_falha_de_rede_nao_quebra_retorna_zero():
    import requests
    ed = _edital()
    ed.itens = [ItemEdital(numero=1, descricao="Papel A4", unidade_medida=None)]
    with patch("app.main.requests.get", side_effect=requests.RequestException("timeout")):
        n = _backfill_unidade_medida(ed)
    assert n == 0
    assert ed.itens[0].unidade_medida is None


def test_http_erro_retorna_zero_sem_quebrar():
    ed = _edital()
    ed.itens = [ItemEdital(numero=1, descricao="Papel A4", unidade_medida=None)]
    resposta = MagicMock(status_code=500)
    with patch("app.main.requests.get", return_value=resposta):
        n = _backfill_unidade_medida(ed)
    assert n == 0


def test_edital_sem_referencia_pncp_valida_retorna_zero():
    ed = _edital(id_externo="formato-invalido-sem-barra", cnpj_orgao=None)
    ed.itens = [ItemEdital(numero=1, descricao="Papel A4", unidade_medida=None)]
    with patch("app.main.requests.get") as mock_get:
        n = _backfill_unidade_medida(ed)
    assert n == 0
    assert not mock_get.called
