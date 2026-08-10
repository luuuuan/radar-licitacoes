"""
Testes de rerank_gemini (analise_edital.py) — alternativa ao reranker da
DeepInfra, usando o Gemini (chave própria do usuário) pra pontuar o
catálogo contra um item de edital. Mesmo contrato de retorno do
matching/embeddings.rerank: lista de scores na mesma ordem de `documentos`,
ou None em qualquer falha. Rode com: cd backend && pytest
"""
from unittest.mock import patch, MagicMock

from app.analise_edital import rerank_gemini


def _resposta_ok(texto):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": texto}]}}]}
    return r


def test_sem_api_key_retorna_none():
    assert rerank_gemini("item", ["produto a", "produto b"], api_key=None) is None


def test_sem_documentos_retorna_none():
    assert rerank_gemini("item", [], api_key="fake-key") is None


def test_resposta_valida_retorna_scores_na_mesma_ordem():
    with patch("app.analise_edital.requests.post",
              return_value=_resposta_ok('{"scores": [0.9, 0.1, 0.5]}')):
        r = rerank_gemini("item", ["a", "b", "c"], api_key="fake-key")
    assert r == [0.9, 0.1, 0.5]


def test_quantidade_de_scores_diferente_do_catalogo_retorna_none():
    with patch("app.analise_edital.requests.post",
              return_value=_resposta_ok('{"scores": [0.9, 0.1]}')):
        r = rerank_gemini("item", ["a", "b", "c"], api_key="fake-key")
    assert r is None


def test_json_invalido_retorna_none():
    with patch("app.analise_edital.requests.post",
              return_value=_resposta_ok("isso não é JSON")):
        r = rerank_gemini("item", ["a", "b"], api_key="fake-key")
    assert r is None


def test_scores_fora_da_faixa_sao_limitados_entre_0_e_1():
    with patch("app.analise_edital.requests.post",
              return_value=_resposta_ok('{"scores": [1.5, -0.3]}')):
        r = rerank_gemini("item", ["a", "b"], api_key="fake-key")
    assert r == [1.0, 0.0]


def test_http_erro_retorna_none():
    r = MagicMock()
    r.status_code = 500
    r.text = "erro"
    with patch("app.analise_edital.requests.post", return_value=r), \
         patch("app.analise_edital.time.sleep", lambda s: None):
        assert rerank_gemini("item", ["a"], api_key="fake-key", tentativas=1) is None
