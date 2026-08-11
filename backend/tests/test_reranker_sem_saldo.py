"""
Achado real em produção: sem saldo na DeepInfra, o reranker recebe 402 em
TODO item de TODO edital durante um recálculo/coleta — sem o disjuntor
pausar (só pausava em 429), isso gerava uma chamada de rede + uma linha de
log por item, atrasando a rodada inteira à toa, já sabendo de antemão que
ia falhar de novo. rerank() agora trata 402 igual a 429: pausa a chave
pelo mesmo cooldown, e chamadas seguintes nem tentam a rede.
Rode com:  cd backend && pytest
"""
from unittest.mock import patch, MagicMock

from app.matching import embeddings as emb


def _resposta(status_code, texto=""):
    r = MagicMock()
    r.status_code = status_code
    r.text = texto
    return r


def test_402_pausa_a_chave_igual_ao_429():
    emb._bloqueado_ate.clear()
    with patch("app.matching.embeddings.requests.post",
              return_value=_resposta(402, '{"detail":{"error":"sem saldo"}}')):
        r = emb.rerank("item", ["produto a"], api_key="chave-sem-saldo")

    assert r is None
    assert emb.ia_bloqueada("chave-sem-saldo") is True
    assert emb.segundos_para_liberar("chave-sem-saldo") > 0


def test_chamada_seguinte_apos_402_nao_bate_na_rede():
    emb._bloqueado_ate.clear()
    chamadas = {"n": 0}
    def _post(*a, **kw):
        chamadas["n"] += 1
        return _resposta(402, '{"detail":{"error":"sem saldo"}}')
    with patch("app.matching.embeddings.requests.post", side_effect=_post):
        emb.rerank("item 1", ["produto a"], api_key="chave-sem-saldo-2")
        emb.rerank("item 2", ["produto a"], api_key="chave-sem-saldo-2")
        emb.rerank("item 3", ["produto a"], api_key="chave-sem-saldo-2")

    assert chamadas["n"] == 1   # só a 1ª realmente tentou a rede


def test_402_nao_afeta_outra_chave():
    emb._bloqueado_ate.clear()
    with patch("app.matching.embeddings.requests.post",
              return_value=_resposta(402)):
        emb.rerank("item", ["produto a"], api_key="chave-a")

    assert emb.ia_bloqueada("chave-a") is True
    assert emb.ia_bloqueada("chave-b") is False


def test_200_continua_funcionando_normalmente():
    emb._bloqueado_ate.clear()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"scores": [0.9, 0.1]}
    with patch("app.matching.embeddings.requests.post", return_value=resp):
        r = emb.rerank("item", ["produto a", "produto b"], api_key="chave-ok")

    assert r == [0.9, 0.1]
    assert emb.ia_bloqueada("chave-ok") is False
