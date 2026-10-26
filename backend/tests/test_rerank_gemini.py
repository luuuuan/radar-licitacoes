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


# ---------------------------------------------------------------------------
# Achado real (auditoria de performance pedida pelo usuário): o catálogo
# formatado era só TRUNCADO NO TEXTO (_MAX_CATALOGO_RERANK_GEMINI chars),
# mas o prompt pedia `n=len(documentos)` scores -- com catálogo grande, o
# Gemini via só uma fração dos produtos mas era instruído a devolver score
# pra TODOS, inventando o resto pra obedecer ao formato pedido.
# ---------------------------------------------------------------------------

def _prompt_enviado(mock_post) -> str:
    import json as _json
    corpo = _json.loads(mock_post.call_args.kwargs["data"])
    return corpo["contents"][0]["parts"][0]["text"]


def test_catalogo_grande_completa_o_restante_com_zero_nao_inventa_score(monkeypatch):
    import json as _json
    import re
    import app.analise_edital as ae
    documentos = [f"produto numero {i} " + "x" * 300 for i in range(200)]

    def _fake_post(url, data=None, **kw):
        texto = _json.loads(data)["contents"][0]["parts"][0]["text"]
        n = int(re.search(r"exatamente (\d+) números", texto).group(1))
        assert n < len(documentos)   # o orçamento realmente cortou a lista
        return _resposta_ok('{"scores": [' + ", ".join(["0.9"] * n) + ']}')

    monkeypatch.setattr(ae.requests, "post", _fake_post)
    r = rerank_gemini("item", documentos, api_key="fake-key")

    assert r is not None
    assert len(r) == len(documentos)
    n_avaliados = sum(1 for s in r if s != 0.0)
    assert n_avaliados < len(documentos)
    assert all(s == 0.9 for s in r[:n_avaliados])
    assert all(s == 0.0 for s in r[n_avaliados:])   # resto nunca avaliado -- 0.0, nunca inventado


def test_catalogo_pequeno_nao_trunca_nada():
    with patch("app.analise_edital.requests.post",
              return_value=_resposta_ok('{"scores": [0.9, 0.1, 0.5]}')) as mock_post:
        r = rerank_gemini("item", ["a", "b", "c"], api_key="fake-key")
        prompt = _prompt_enviado(mock_post)
    assert r == [0.9, 0.1, 0.5]
    assert "exatamente 3 números" in prompt
