"""
Testes da retentativa de itens_pdf._gerar() (chamada ao DeepInfra) em falha
transiente — achado real: testes manuais contra a API mostraram o MESMO
prompt ora respondendo em segundos, ora estourando o timeout (instabilidade
do lado da DeepInfra, não do tamanho do prompt). Mesmo padrão já usado em
analise_edital.py._gerar(). Rode com: cd backend && pytest
"""
from unittest.mock import patch, MagicMock

import requests

from app.itens_pdf import _gerar


def _resposta_ok(texto='{"itens": []}'):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"choices": [{"message": {"content": texto}}]}
    return r


def test_gerar_retenta_apos_timeout_e_da_certo_na_segunda(monkeypatch):
    monkeypatch.setattr("app.itens_pdf.time.sleep", lambda s: None)
    chamadas = {"n": 0}
    def _post(*a, **kw):
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            raise requests.exceptions.ReadTimeout("sem resposta")
        return _resposta_ok()
    with patch("app.itens_pdf.requests.post", side_effect=_post):
        txt, status = _gerar("prompt", api_key="fake-key")
    assert status == "ok"
    assert chamadas["n"] == 2


def test_gerar_falha_persistente_esgota_tentativas(monkeypatch):
    monkeypatch.setattr("app.itens_pdf.time.sleep", lambda s: None)
    with patch("app.itens_pdf.requests.post",
              side_effect=requests.exceptions.ReadTimeout("sem resposta")) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")
    assert txt is None
    assert status.startswith("rede:")
    assert mock_post.call_count == 2   # tentativas=2 (padrão) — não fica tentando pra sempre


def test_gerar_retenta_em_5xx_mas_nao_em_429(monkeypatch):
    monkeypatch.setattr("app.itens_pdf.time.sleep", lambda s: None)
    respostas = [MagicMock(status_code=503, text="sobrecarregado"), _resposta_ok()]
    with patch("app.itens_pdf.requests.post", side_effect=respostas) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")
    assert status == "ok"
    assert mock_post.call_count == 2

    r429 = MagicMock(status_code=429, text="rate limit")
    with patch("app.itens_pdf.requests.post", return_value=r429) as mock_post2:
        txt2, status2 = _gerar("prompt", api_key="fake-key")
    assert status2 == "http_429"
    assert mock_post2.call_count == 1   # 429 não retenta
