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


def test_gerar_retenta_em_5xx_mas_nao_dentro_do_mesmo_modelo_em_429(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    respostas = []
    r503 = MagicMock(status_code=503, text="sobrecarregado")
    respostas.append(r503)
    respostas.append(_resposta_ok())
    with patch("app.analise_edital.requests.post", side_effect=respostas) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")
    assert status == "ok"
    assert mock_post.call_count == 2

    # 429 não retenta DENTRO do mesmo modelo (mensagem própria já existe pra
    # esse caso) -- isola sem fallback/Groq configurados pra testar só essa
    # parte; a troca de modelo/provedor em 429 tem testes dedicados abaixo.
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "")
    r429 = MagicMock(status_code=429, text="rate limit")
    with patch("app.analise_edital.requests.post", return_value=r429) as mock_post2:
        txt2, status2 = _gerar("prompt", api_key="fake-key")
    assert status2 == "http_429"
    assert mock_post2.call_count == 1   # 1 tentativa só nesse modelo (sem retentar 429)


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


def test_gerar_usa_fallback_em_429_tambem(monkeypatch):
    """Pedido do usuário: 429 também troca de modelo/provedor, não só
    5xx/rede -- cota costuma ser por projeto, mas não necessariamente é a
    MESMA entre modelos diferentes (Gemini principal x fallback) e
    certamente não é a mesma entre provedores diferentes (Gemini x Groq)."""
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "modelo-fallback")

    with patch("app.analise_edital.requests.post",
              return_value=MagicMock(status_code=429, text="rate limit")) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "http_429"
    # 1 tentativa em cada modelo (429 não retenta dentro do mesmo modelo,
    # mas troca pro próximo) -- principal + fallback
    assert mock_post.call_count == 2


def test_gerar_nao_usa_fallback_em_outro_4xx_que_nao_429(monkeypatch):
    """400 (bad request) não é erro de limite/sobrecarga -- trocar de
    modelo não costuma ajudar (é a mesma requisição malformada), então
    continua sem trocar."""
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "modelo-fallback")

    with patch("app.analise_edital.requests.post",
              return_value=MagicMock(status_code=400, text="bad request")) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "http_400"
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


# --------- último recurso: Groq (provedor diferente) quando os 2 --------- #
# modelos Gemini esgotam --------- #

def _resposta_groq_ok(texto='{"veio_do_groq": true}'):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"choices": [{"message": {"content": texto}}]}
    return r


def test_gerar_cai_pro_groq_quando_os_2_modelos_gemini_esgotam(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "modelo-fallback")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")
    urls_chamadas = []

    def _post(url, headers=None, **kw):
        urls_chamadas.append(url)
        if "generativelanguage" in url:
            return MagicMock(status_code=503, text="sobrecarregado")
        assert headers["Authorization"] == "Bearer groq-fake-key"
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "ok"
    assert txt == '{"veio_do_groq": true}'
    # 2 tentativas em cada modelo Gemini (esgotaram) + 1 no Groq (deu certo)
    assert sum("generativelanguage" in u for u in urls_chamadas) == 4
    assert sum(u == "https://api.groq.com/openai/v1/chat/completions" for u in urls_chamadas) == 1


def test_gerar_sem_groq_key_nao_chama_a_api_da_groq(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "modelo-fallback")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "")

    with patch("app.analise_edital.requests.post",
              return_value=MagicMock(status_code=503, text="sobrecarregado")) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "http_503"   # erro do último modelo Gemini, não "sem_chave_groq"
    assert mock_post.call_count == 4   # só os 2 modelos Gemini, nunca bateu na Groq


def test_gerar_groq_tambem_falha_devolve_erro_da_groq(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")

    def _post(url, **kw):
        if "generativelanguage" in url:
            return MagicMock(status_code=503, text="sobrecarregado")
        return MagicMock(status_code=500, text="groq fora do ar")

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _gerar("prompt", api_key="fake-key")

    assert txt is None
    assert status == "http_500"   # erro da Groq (última tentativa), não do Gemini


def test_gerar_tenta_groq_em_429_do_gemini(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")

    def _post(url, **kw):
        if "generativelanguage" in url:
            return MagicMock(status_code=429, text="rate limit")
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "ok"
    assert txt == '{"veio_do_groq": true}'
    assert mock_post.call_count == 2   # 1 no Gemini (429, sem retentar) + 1 na Groq (deu certo)


def test_gerar_nao_tenta_groq_em_outro_4xx_que_nao_429(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")

    with patch("app.analise_edital.requests.post",
              return_value=MagicMock(status_code=400, text="bad request")) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "http_400"
    assert mock_post.call_count == 1   # nem o Gemini retentou, nem foi pra Groq


# --------- 404 (modelo desativado/renomeado pelo provedor) também troca --------- #
# de modelo/provedor --------- #
# Achado real em produção: gemini-2.5-flash (configurado como
# IA_MODELO_TEXTO_FALLBACK) parou de responder pra contas novas com HTTP
# 404 antes da data de desligamento anunciada. Sem tratar 404 como "tenta
# o próximo", isso travava a cadeia no fallback, sem nunca chegar no Groq.

def test_gerar_troca_de_modelo_em_404(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-desativado")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "modelo-fallback")

    def _post(url, **kw):
        if "modelo-desativado" in url:
            return MagicMock(status_code=404, text="model no longer available")
        return _resposta_ok('{"veio_do_fallback": true}')

    with patch("app.analise_edital.requests.post", side_effect=_post) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "ok"
    assert txt == '{"veio_do_fallback": true}'
    assert mock_post.call_count == 2   # 1 no modelo desativado (404, sem retentar) + 1 no fallback


def test_gerar_tenta_groq_quando_os_2_modelos_gemini_dao_404(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-desativado")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")

    def _post(url, **kw):
        if "generativelanguage" in url:
            return MagicMock(status_code=404, text="model no longer available")
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "ok"
    assert txt == '{"veio_do_groq": true}'
    assert mock_post.call_count == 2   # 1 no Gemini (404, sem retentar) + 1 na Groq


# --------- prompt grande demais estoura o TPM da Groq (achado real: --------- #
# HTTP 413 num edital de 350 itens, "Limit 8000, Requested 11382") --------- #
# tier gratuito da Groq: 8000 tokens/minuto é POR REQUISIÇÃO, esperar e
# tentar de novo com o mesmo tamanho bate no mesmo erro pra sempre -- só
# resolve truncando o que é mandado (_chamar_groq corta antes de enviar).

def test_chamar_groq_trunca_prompt_grande_antes_de_mandar(monkeypatch):
    from app.analise_edital import _chamar_groq, _GROQ_LIMITE_PROMPT_CHARS
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")
    prompt_grande = "x" * (_GROQ_LIMITE_PROMPT_CHARS + 5000)
    prompts_recebidos = []

    def _post(url, json=None, **kw):
        prompts_recebidos.append(json["messages"][0]["content"])
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _chamar_groq(prompt_grande, timeout=60, tentativas=1)

    assert status == "ok"
    assert len(prompts_recebidos[0]) == _GROQ_LIMITE_PROMPT_CHARS   # cortado, não os 5000 a mais


def test_chamar_groq_nao_trunca_prompt_pequeno():
    from app.analise_edital import _chamar_groq
    prompt_pequeno = "prompt normal, bem menor que o limite"
    prompts_recebidos = []

    def _post(url, json=None, **kw):
        prompts_recebidos.append(json["messages"][0]["content"])
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post), \
         patch("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key"):
        txt, status = _chamar_groq(prompt_pequeno, timeout=60, tentativas=1)

    assert status == "ok"
    assert prompts_recebidos[0] == prompt_pequeno   # intacto


def test_chamar_groq_manda_max_tokens_pra_reservar_espaco_na_resposta():
    from app.analise_edital import _chamar_groq, _GROQ_MAX_TOKENS_RESPOSTA
    corpos_recebidos = []

    def _post(url, json=None, **kw):
        corpos_recebidos.append(json)
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post), \
         patch("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key"):
        _chamar_groq("prompt qualquer", timeout=60, tentativas=1)

    assert corpos_recebidos[0]["max_tokens"] == _GROQ_MAX_TOKENS_RESPOSTA
