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


# --------------------------------------------------------------------- #
# Achado real (usuário reportou, catálogo de 3600 produtos): a DeepInfra
# passou a rejeitar (HTTP 422, "queries... at most 1024 items") quando
# `documentos` passa de _RERANKER_MAX_DOCUMENTOS -- o broadcast de 1 query
# contra N documentos é validado do lado deles como se "queries" tivesse N
# itens. Catálogo de 3600 produtos falhava em TODA chamada, SEMPRE,
# degradando o motor pra só TF-IDF (reranker sempre None) sem nenhum aviso
# visível no app.
# --------------------------------------------------------------------- #

def test_catalogo_grande_divide_em_lotes_dentro_do_teto():
    emb._bloqueado_ate.clear()
    corpos_recebidos = []

    def _post(url, json=None, **kw):
        corpos_recebidos.append(json)
        n = len(json["documents"])
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"scores": [0.5] * n}
        return resp

    documentos = [f"produto {i}" for i in range(emb._RERANKER_MAX_DOCUMENTOS + 500)]
    with patch("app.matching.embeddings.requests.post", side_effect=_post):
        r = emb.rerank("item", documentos, api_key="chave-catalogo-grande")

    # nenhuma chamada excedeu o teto de documentos por request
    assert all(len(c["documents"]) <= emb._RERANKER_MAX_DOCUMENTOS for c in corpos_recebidos)
    assert len(corpos_recebidos) == 2   # 1024 + 500 -- 2 lotes
    # scores concatenados na MESMA ordem/tamanho do catálogo inteiro
    assert r == [0.5] * len(documentos)
    # cada lote manda a MESMA query (broadcast), nunca mais de uma por chamada
    assert all(c["queries"] == ["item"] for c in corpos_recebidos)


def test_catalogo_grande_lote_que_falha_invalida_o_item_inteiro():
    emb._bloqueado_ate.clear()
    chamadas = {"n": 0}

    def _post(url, json=None, **kw):
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"scores": [0.5] * len(json["documents"])}
            return resp
        # 2º lote falha -- achado real que motivou o disjuntor (ver testes
        # acima): não vale devolver scores PARCIAIS do catálogo, o item
        # inteiro fica sem sinal confiável, igual qualquer outra falha.
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "erro"
        return resp

    documentos = [f"produto {i}" for i in range(emb._RERANKER_MAX_DOCUMENTOS + 500)]
    with patch("app.matching.embeddings.requests.post", side_effect=_post), \
         patch("app.matching.embeddings.time.sleep"):
        r = emb.rerank("item", documentos, api_key="chave-catalogo-grande-2", tentativas=1)

    assert r is None


def test_catalogo_dentro_do_teto_continua_numa_chamada_so():
    emb._bloqueado_ate.clear()
    chamadas = []

    def _post(url, json=None, **kw):
        chamadas.append(json)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"scores": [0.1] * len(json["documents"])}
        return resp

    documentos = [f"produto {i}" for i in range(emb._RERANKER_MAX_DOCUMENTOS)]
    with patch("app.matching.embeddings.requests.post", side_effect=_post):
        r = emb.rerank("item", documentos, api_key="chave-catalogo-no-teto")

    assert len(chamadas) == 1
    assert r == [0.1] * len(documentos)
