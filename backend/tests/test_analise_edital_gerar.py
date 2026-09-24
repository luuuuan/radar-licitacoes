"""
Testes da retentativa de _gerar() (chamada ao Gemini) em falha transiente
— achado real: usuário clicando "Realizar nova análise" caía direto em
"não foi possível conectar" numa falha passageira de rede, sem segunda
chance (diferente do PNCPConnector, que já retentava). Rode com:
cd backend && pytest
"""
import json
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

    assert status == "http_400:bad request"
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
    # prefixo "groq_": o erro final veio da Groq (última tentativa), não do
    # Gemini -- achado real: sem essa marca, o front não tinha como saber
    # que o 429/5xx que efetivamente chegou até o usuário era de um
    # provedor à parte (cota compartilhada), e mostrava uma mensagem que
    # dava a entender que era a cota GRATUITA PESSOAL do Gemini do usuário
    # que tinha estourado -- ver _msgErroIA no index.html.
    assert status == "groq_http_500"


def test_gerar_groq_com_429_tambem_ganha_o_prefixo_groq(monkeypatch):
    """Achado real (usuário relatou mensagem em tela incoerente com o log
    do Railway): o caso mais comum de erro final vindo da Groq é 429 (cota
    compartilhada do provedor), não 500 -- precisa do mesmo prefixo."""
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")

    def _post(url, **kw):
        if "generativelanguage" in url:
            return MagicMock(status_code=503, text="sobrecarregado")
        return MagicMock(status_code=429, text="groq rate limit")

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _gerar("prompt", api_key="fake-key")

    assert txt is None
    assert status == "groq_http_429"


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

    assert status == "http_400:bad request"
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


# --------- Mistral: 2º provedor de fallback, tentado ANTES do Groq --------- #
# (ver achado real em settings.MISTRAL_MODELO_TEXTO sobre por que essa
# ordem e por que esse modelo específico).

def test_gerar_cai_pra_mistral_quando_os_2_modelos_gemini_esgotam(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "modelo-fallback")
    monkeypatch.setattr("app.analise_edital.settings.MISTRAL_API_KEY", "mistral-fake-key")
    urls_chamadas = []

    def _post(url, headers=None, **kw):
        urls_chamadas.append(url)
        if "generativelanguage" in url:
            return MagicMock(status_code=503, text="sobrecarregado")
        assert headers["Authorization"] == "Bearer mistral-fake-key"
        return _resposta_groq_ok('{"veio_da_mistral": true}')

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "ok"
    assert txt == '{"veio_da_mistral": true}'
    assert sum("generativelanguage" in u for u in urls_chamadas) == 4
    assert sum(u == "https://api.mistral.ai/v1/chat/completions" for u in urls_chamadas) == 1


def test_gerar_sem_mistral_key_pula_direto_pro_groq(monkeypatch):
    """Sem MISTRAL_API_KEY configurada, _chamar_mistral devolve
    "sem_chave_mistral" sem chamar rede -- a cadeia pula direto pro Groq,
    mesmo comportamento que já existia pra "sem_chave_groq"."""
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")
    monkeypatch.setattr("app.analise_edital.settings.MISTRAL_API_KEY", "")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")
    urls_chamadas = []

    def _post(url, **kw):
        urls_chamadas.append(url)
        if "generativelanguage" in url:
            return MagicMock(status_code=503, text="sobrecarregado")
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "ok"
    assert txt == '{"veio_do_groq": true}'
    assert "https://api.mistral.ai/v1/chat/completions" not in urls_chamadas
    assert urls_chamadas.count("https://api.groq.com/openai/v1/chat/completions") == 1


def test_gerar_mistral_dando_certo_nunca_chama_groq(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")
    monkeypatch.setattr("app.analise_edital.settings.MISTRAL_API_KEY", "mistral-fake-key")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")
    urls_chamadas = []

    def _post(url, **kw):
        urls_chamadas.append(url)
        if "generativelanguage" in url:
            return MagicMock(status_code=503, text="sobrecarregado")
        if "mistral" in url:
            return _resposta_groq_ok('{"veio_da_mistral": true}')
        raise AssertionError("não devia ter batido na Groq -- Mistral já resolveu")

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _gerar("prompt", api_key="fake-key")

    assert txt == '{"veio_da_mistral": true}'
    assert "https://api.groq.com/openai/v1/chat/completions" not in urls_chamadas


def test_gerar_mistral_falha_ainda_tenta_groq_mesmo_com_erro_nao_transiente(monkeypatch):
    """Achado do desenho da cadeia: Mistral e Groq são provedores SEM
    relação nenhuma entre si -- mesmo uma falha da Mistral que não parece
    "transiente" (aqui, um 400 qualquer) não deve impedir de tentar o
    Groq depois. Diferente da regra entre os 2 modelos GEMINI (onde um 4xx
    que não é 429/404 interrompe a cadeia ali mesmo, ver
    test_gerar_nao_tenta_groq_em_outro_4xx_que_nao_429)."""
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")
    monkeypatch.setattr("app.analise_edital.settings.MISTRAL_API_KEY", "mistral-fake-key")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")

    def _post(url, **kw):
        if "generativelanguage" in url:
            return MagicMock(status_code=503, text="sobrecarregado")
        if "mistral" in url:
            return MagicMock(status_code=400, text="bad request da mistral")
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _gerar("prompt", api_key="fake-key")

    assert status == "ok"
    assert txt == '{"veio_do_groq": true}'


def test_gerar_mistral_e_groq_falham_prefixo_groq_vence_por_ser_o_ultimo(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")
    monkeypatch.setattr("app.analise_edital.settings.MISTRAL_API_KEY", "mistral-fake-key")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")

    def _post(url, **kw):
        if "generativelanguage" in url:
            return MagicMock(status_code=503, text="sobrecarregado")
        if "mistral" in url:
            return MagicMock(status_code=503, text="mistral fora do ar")
        return MagicMock(status_code=429, text="groq rate limit")

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _gerar("prompt", api_key="fake-key")

    assert txt is None
    assert status == "groq_http_429"   # Groq é sempre o último a falar, mesmo com Mistral no meio


def test_gerar_mistral_falha_e_groq_sem_chave_mantem_prefixo_mistral(monkeypatch):
    """Espelha test_gerar_sem_groq_key_nao_chama_a_api_da_groq -- "sem
    chave" nunca deve SOBRESCREVER o erro real do estágio anterior."""
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO", "modelo-principal")
    monkeypatch.setattr("app.analise_edital.settings.IA_MODELO_TEXTO_FALLBACK", "")
    monkeypatch.setattr("app.analise_edital.settings.MISTRAL_API_KEY", "mistral-fake-key")
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "")

    def _post(url, **kw):
        if "generativelanguage" in url:
            return MagicMock(status_code=503, text="sobrecarregado")
        return MagicMock(status_code=500, text="mistral fora do ar")

    with patch("app.analise_edital.requests.post", side_effect=_post) as mock_post:
        txt, status = _gerar("prompt", api_key="fake-key")

    assert txt is None
    assert status == "mistral_http_500"
    assert "https://api.groq.com/openai/v1/chat/completions" not in [
        c.args[0] if c.args else c.kwargs.get("url") for c in mock_post.call_args_list]


def test_chamar_mistral_sem_chave_nao_chama_rede(monkeypatch):
    from app.analise_edital import _chamar_mistral
    monkeypatch.setattr("app.analise_edital.settings.MISTRAL_API_KEY", "")
    with patch("app.analise_edital.requests.post") as mock_post:
        txt, status = _chamar_mistral("prompt", timeout=10, tentativas=2)
    assert txt is None
    assert status == "sem_chave_mistral"
    mock_post.assert_not_called()


def test_chamar_mistral_manda_max_tokens_e_response_format_json():
    from app.analise_edital import _chamar_mistral, _MISTRAL_MAX_TOKENS_RESPOSTA
    with patch("app.analise_edital.settings.MISTRAL_API_KEY", "mistral-fake-key"), \
         patch("app.analise_edital.requests.post", return_value=_resposta_groq_ok()) as mock_post:
        _chamar_mistral("prompt de teste", timeout=10, tentativas=1)
    corpo = mock_post.call_args.kwargs.get("data") or mock_post.call_args.kwargs.get("json")
    if isinstance(corpo, (bytes, bytearray)):
        corpo = json.loads(corpo.decode("utf-8"))
    assert corpo["max_tokens"] == _MISTRAL_MAX_TOKENS_RESPOSTA
    assert corpo["response_format"] == {"type": "json_object"}
    assert corpo["model"] == "ministral-8b-latest"


# --------- prompt grande demais estoura o corpo da requisição na Groq -- #
# achado real #1: HTTP 413 num edital de 350 itens, "Limit 8000, Requested
# 11382" -- tier gratuito da Groq, 8000 tokens/minuto POR REQUISIÇÃO,
# esperar e tentar de novo com o mesmo tamanho bate no mesmo erro pra
# sempre -- só resolve truncando o que é mandado.
# achado real #2 (edital 134686, já no modelo com TPM bem maior): 413 de
# novo, mas SEM número de TPM na mensagem ("Request Entity Too Large" /
# code "request_too_large") -- na doc da Groq, 413 é um erro PRÓPRIO,
# separado do 429 (que é o de TPM). É o corpo bruto (bytes) estourando um
# teto à parte, não tokens -- e o corte antigo media CARACTERES, não
# bytes (texto em português com acento pode virar 2 bytes/caractere em
# UTF-8). _chamar_groq agora corta por BYTES UTF-8 reais.

def _corpo_de(data) -> dict:
    """_chamar_groq manda o corpo pré-serializado via `data=` (bytes), não
    `json=` (ver ensure_ascii=False em _post_com_retry) -- decodifica de
    volta pra inspecionar nos testes, igual o `json=` fazia sozinho antes."""
    return json.loads(data.decode("utf-8"))


def test_chamar_groq_trunca_prompt_grande_antes_de_mandar(monkeypatch):
    from app.analise_edital import _chamar_groq, _GROQ_LIMITE_PROMPT_BYTES
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")
    prompt_grande = "x" * (_GROQ_LIMITE_PROMPT_BYTES + 5000)   # 1 byte/char (ascii)
    prompts_recebidos = []

    def _post(url, data=None, **kw):
        prompts_recebidos.append(_corpo_de(data)["messages"][0]["content"])
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _chamar_groq(prompt_grande, timeout=60, tentativas=1)

    assert status == "ok"
    assert len(prompts_recebidos[0]) == _GROQ_LIMITE_PROMPT_BYTES   # cortado, não os 5000 a mais


def test_chamar_groq_trunca_por_bytes_utf8_nao_por_caracteres(monkeypatch):
    """Pedido do usuário (edital 134686): texto em português denso de
    acento pode ter menos caracteres que bytes -- o corte tem que respeitar
    o tamanho REAL do corpo (bytes UTF-8), não a contagem de caracteres do
    Python, senão o corte antigo (baseado em caracteres) deixa passar um
    corpo maior do que o teto pretendia."""
    from app.analise_edital import _chamar_groq, _GROQ_LIMITE_PROMPT_BYTES
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")
    # "çã" = 2 caracteres, 4 bytes em UTF-8 -- bem mais denso que ascii.
    prompt_grande = "çã" * (_GROQ_LIMITE_PROMPT_BYTES // 2)
    prompts_recebidos = []

    def _post(url, data=None, **kw):
        prompts_recebidos.append(_corpo_de(data)["messages"][0]["content"])
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post):
        txt, status = _chamar_groq(prompt_grande, timeout=60, tentativas=1)

    assert status == "ok"
    enviado = prompts_recebidos[0]
    assert len(enviado.encode("utf-8")) <= _GROQ_LIMITE_PROMPT_BYTES
    # não pode ter quebrado um caractere multibyte no meio (decodificou ok
    # acima, sem UnicodeDecodeError -- é a própria asserção do teste).


def test_chamar_groq_manda_corpo_sem_inflar_acento_com_escape_unicode(monkeypatch):
    """Achado real (agente error-detective): requests.post(json=...) usa
    json.dumps ensure_ascii=True por padrão -- cada caractere acentuado
    vira um escape \\uXXXX de 6 bytes, 3x os 2 bytes reais em UTF-8. Isso
    destruía o corte por bytes (o corpo de VERDADE enviado podia ficar bem
    maior do que o medido antes de truncar). O corpo de verdade enviado
    tem que conter os bytes UTF-8 do caractere, não o escape."""
    from app.analise_edital import _chamar_groq
    monkeypatch.setattr("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key")
    corpos_brutos = []

    def _post(url, data=None, **kw):
        corpos_brutos.append(data)
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post):
        _chamar_groq("edital com acentuação: ção, não, é, ã", timeout=60, tentativas=1)

    enviado = corpos_brutos[0]
    assert b"\\u" not in enviado   # nada de escape unicode
    assert "ção".encode("utf-8") in enviado   # os bytes UTF-8 reais, direto no corpo


def test_truncar_utf8_nao_quebra_caractere_multibyte_no_meio():
    from app.analise_edital import _truncar_utf8
    # "á" = 2 bytes em UTF-8 -- corta exatamente no meio dele.
    texto = "x" * 9 + "á"
    resultado = _truncar_utf8(texto, max_bytes=10)
    assert resultado == "x" * 9   # o "á" quebrado inteiro fica de fora, não 1 byte dele
    assert len(resultado.encode("utf-8")) <= 10


def test_chamar_groq_nao_trunca_prompt_pequeno():
    from app.analise_edital import _chamar_groq
    prompt_pequeno = "prompt normal, bem menor que o limite"
    prompts_recebidos = []

    def _post(url, data=None, **kw):
        prompts_recebidos.append(_corpo_de(data)["messages"][0]["content"])
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post), \
         patch("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key"):
        txt, status = _chamar_groq(prompt_pequeno, timeout=60, tentativas=1)

    assert status == "ok"
    assert prompts_recebidos[0] == prompt_pequeno   # intacto


def test_chamar_groq_manda_max_tokens_pra_reservar_espaco_na_resposta():
    from app.analise_edital import _chamar_groq, _GROQ_MAX_TOKENS_RESPOSTA
    corpos_recebidos = []

    def _post(url, data=None, **kw):
        corpos_recebidos.append(_corpo_de(data))
        return _resposta_groq_ok()

    with patch("app.analise_edital.requests.post", side_effect=_post), \
         patch("app.analise_edital.settings.GROQ_API_KEY", "groq-fake-key"):
        _chamar_groq("prompt qualquer", timeout=60, tentativas=1)

    assert corpos_recebidos[0]["max_tokens"] == _GROQ_MAX_TOKENS_RESPOSTA
