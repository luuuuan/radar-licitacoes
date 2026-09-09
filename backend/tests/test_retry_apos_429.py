"""
Achado real (pedido do usuário, edital 122390): comparar_catalogo_usuario
processa itens em lotes (_TAMANHO_LOTE_COMPARACAO) -- cada lote chama
_gerar() por conta própria. Com a cota diária do Gemini esgotada, todo
lote caía direto na Groq, e os lotes rodando em sequência rápida (sem
pausa nenhuma) empurravam a Groq pro mesmo limite de tokens/minuto (TPM)
repetidas vezes, já que a janela é de TEMPO (60s), não uma cota fixa que
só volta no dia seguinte. `_retry_after_segundos`/`_post_com_retry` agora
esperam o tempo que o PRÓPRIO provedor sugere (header Retry-After, ou a
frase "please try again in Xs" que a Groq usa) antes de tentar de novo --
só quando esse valor é confiável (positivo e dentro de um teto, ver
_RETRY_APOS_429_TETO_S) -- nunca um valor chutado, porque um 429 por cota
esgotada (não por janela de tempo, ex.: Gemini free tier no dia) não
melhora só esperando um pouco. Rode com:  cd backend && pytest
"""
from unittest.mock import MagicMock, patch

from app.analise_edital import _post_com_retry, _retry_after_segundos, _RETRY_APOS_429_TETO_S


def _resp(status_code=429, text="rate limit", headers=None):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.headers = headers or {}
    return r


# --------- _retry_after_segundos --------- #

def test_retry_after_via_header_dentro_do_teto():
    r = _resp(headers={"Retry-After": "5"})
    assert _retry_after_segundos(r) == 5.0


def test_retry_after_via_header_acima_do_teto_ignora():
    r = _resp(headers={"Retry-After": str(_RETRY_APOS_429_TETO_S + 30)})
    assert _retry_after_segundos(r) is None


def test_retry_after_via_texto_da_groq():
    r = _resp(text=("Rate limit reached for model `openai/gpt-oss-120b` ... "
                    "Limit 8000, Used 7266, Requested 1200. Please try again in 4.5s."))
    assert _retry_after_segundos(r) == 4.5


def test_retry_after_via_texto_acima_do_teto_ignora():
    r = _resp(text="Please try again in 120s.")
    assert _retry_after_segundos(r) is None


def test_sem_nenhum_sinal_devolve_none():
    r = _resp(text="quota exceeded, please check your plan and billing details")
    assert _retry_after_segundos(r) is None


def test_retry_after_invalido_no_header_nao_quebra():
    r = _resp(headers={"Retry-After": "não é número"})
    assert _retry_after_segundos(r) is None


# --------- _post_com_retry: espera e tenta de novo só com sinal confiável --------- #

def _extrai(d):
    return d["ok"]


def test_espera_o_tempo_sugerido_e_tenta_de_novo(monkeypatch):
    esperas = []
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: esperas.append(s))
    respostas = [
        _resp(429, "Please try again in 3.2s.", headers={}),
        MagicMock(status_code=200, json=lambda: {"ok": "sucesso"}),
    ]
    with patch("app.analise_edital.requests.post", side_effect=respostas) as mock_post:
        txt, st = _post_com_retry("http://x", {}, {}, 10, 2, _extrai, "teste")
    assert st == "ok"
    assert txt == "sucesso"
    assert mock_post.call_count == 2
    assert esperas == [3.2]


def test_nao_espera_sem_sinal_confiavel(monkeypatch):
    esperas = []
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: esperas.append(s))
    with patch("app.analise_edital.requests.post",
              return_value=_resp(429, "rate limit")) as mock_post:
        txt, st = _post_com_retry("http://x", {}, {}, 10, 2, _extrai, "teste")
    assert st == "http_429"
    assert mock_post.call_count == 1   # não desperdiça tentativa sem saber se ajuda
    assert esperas == []


def test_nao_espera_quando_ja_e_a_ultima_tentativa(monkeypatch):
    """Mesmo com sinal confiável, respeita o orçamento de `tentativas` --
    não estica o trabalho além do combinado."""
    esperas = []
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: esperas.append(s))
    with patch("app.analise_edital.requests.post",
              return_value=_resp(429, "Please try again in 2s.")) as mock_post:
        txt, st = _post_com_retry("http://x", {}, {}, 10, 1, _extrai, "teste")
    assert st == "http_429"
    assert mock_post.call_count == 1
    assert esperas == []


def test_falha_de_novo_apos_esperar_devolve_erro(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    with patch("app.analise_edital.requests.post",
              return_value=_resp(429, "Please try again in 2s.")) as mock_post:
        txt, st = _post_com_retry("http://x", {}, {}, 10, 2, _extrai, "teste")
    assert st == "http_429"
    assert mock_post.call_count == 2   # esperou e tentou de novo, mas falhou outra vez
