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


def test_gerar_falha_de_rede_persistente_esgota_tentativas_dos_2_modelos(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    with patch("app.analise_edital.requests.post",
              side_effect=requests.exceptions.Timeout("sem resposta")) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")
    assert txt is None
    assert status.startswith("rede:")
    # tentativas=2 (padrão) no modelo principal + 2 no fallback -- rede
    # persistente é tratada como "pode ser sobrecarga", então tenta o
    # modelo de fallback antes de desistir de vez (ver eh_transiente).
    assert mock_post.call_count == 4


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


# --------- fallback pro modelo secundário quando o principal esgota --------- #
# em 5xx/rede (sobrecarga) --------- #
# Achado real: 503 ("modelo sobrecarregado") no modelo principal
# acontecendo com frequência. Ao esgotar as tentativas nele, _gerar() tenta
# 1x IA_MODELO_TEXTO_FALLBACK (mesma chave) antes de desistir de vez.

def test_gerar_usa_fallback_quando_modelo_principal_esgota_em_503(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "modelo-fallback")
    urls_chamadas = []

    def _post(url, **kw):
        urls_chamadas.append(url)
        if "modelo-principal" in url:
            return MagicMock(status_code=503, text="sobrecarregado")
        return _resposta_ok('{"veio_do_fallback": true}')

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "ok"
    assert txt == '{"veio_do_fallback": true}'
    # 2 tentativas no principal (esgotou) + 1 no fallback (deu certo de cara)
    assert sum("modelo-principal" in u for u in urls_chamadas) == 2
    assert sum("modelo-fallback" in u for u in urls_chamadas) == 1


def test_gerar_nao_usa_fallback_em_429_ou_outro_4xx(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "modelo-fallback")

    with patch("app.analise_edital.requests.post",
              return_value=MagicMock(status_code=429, text="rate limit")) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "http_429"
    assert mock_post.call_count == 1   # nem tentou o fallback


def test_gerar_sem_fallback_configurado_nao_tenta_segundo_modelo(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")

    with patch("app.analise_edital.requests.post",
              return_value=MagicMock(status_code=503, text="sobrecarregado")) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "http_503"
    assert mock_post.call_count == 2   # só as tentativas do modelo principal
