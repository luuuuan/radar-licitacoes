"""
Testes da retentativa de _gerar() (chamada ao Gemini) em falha transiente
— achado real: usuário clicando "Realizar nova análise" caía direto em
"não foi possível conectar" numa falha passageira de rede, sem segunda
chance (diferente do PNCPConnector, que já retentava). Rode com:
cd backend && pytest
"""
from unittest.mock import patch, MagicMock

import requests

from app.analise_edital import _gerar


def _resposta_ok(texto='{"a": 1}'):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": texto}]}}]}
    return r


def test_gerar_retenta_apos_falha_de_rede_e_da_certo_na_segunda(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    chamadas = {"n": 0}
    def _post(*a, **kw):
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            raise requests.exceptions.ConnectionError("falhou")
        return _resposta_ok()
    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _gerar("prompt", api_key="fake-key")
    assert status == "ok"
    assert chamadas["n"] == 2


def test_gerar_falha_de_rede_persistente_esgota_tentativas(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    with patch("app.analise_edital.requests.post",
              side_effect=requests.exceptions.Timeout("sem resposta")) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")
    assert txt is None
    assert status.startswith("rede:")
    assert mock_post.call_count == 2   # tentativas=2 (padrão) — não fica tentando pra sempre


def test_gerar_retenta_em_5xx_mas_nao_em_429(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    respostas = []
    r503 = MagicMock(status_code=503, text="sobrecarregado")
    respostas.append(r503)
    respostas.append(_resposta_ok())
    with patch("app.analise_edital.requests.post", side_effect=respostas) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")
    assert status == "ok"
    assert mock_post.call_count == 2

    r429 = MagicMock(status_code=429, text="rate limit")
    with patch("app.analise_edital.requests.post", return_value=r429) as mock_post2:
        txt2, status2 = _gerar("prompt", api_key="fake-key")
    assert status2 == "http_429"
    assert mock_post2.call_count == 1   # 429 não retenta — mensagem própria já existe


def test_gerar_sem_chave_nao_chama_rede():
    with patch("app.analise_edital.requests.post") as mock_post:
        txt, status = _gerar("prompt", api_key=None)
    assert status == "sem_chave"
    assert mock_post.called is False
