"""
Achado real (auditoria de arquitetura pedida pelo usuário): _cache_deepinfra
era um dict comum, sem teto nem expurgo -- um processo de vida longa só
cresce, nunca encolhe (já foi causa raiz de uma migração anterior por OOM).
Agora é um _CacheLimitado (app/matching/embeddings.py): descarta a entrada
menos recentemente usada ao passar do teto. Rode com: cd backend && pytest
"""
from unittest.mock import patch

from app.matching import embeddings as emb
from app.matching.embeddings import _CacheLimitado


def test_cache_limitado_descarta_a_mais_antiga_ao_passar_do_teto():
    c = _CacheLimitado(maxsize=2)
    c["a"] = [1.0]
    c["b"] = [2.0]
    c["c"] = [3.0]   # passa do teto -- "a" (a mais antiga) sai
    assert list(c.keys()) == ["b", "c"]
    assert "a" not in c


def test_cache_limitado_get_atualiza_recencia():
    """Acessar "a" via get() conta como uso recente -- ao inserir uma 3ª
    chave, quem sai é a que ninguém tocou há mais tempo ("b"), não "a"."""
    c = _CacheLimitado(maxsize=2)
    c["a"] = [1.0]
    c["b"] = [2.0]
    c.get("a")         # "a" volta a ser a mais recente
    c["c"] = [3.0]      # agora "b" é a mais antiga -- ela sai, não "a"
    assert "a" in c
    assert "b" not in c
    assert "c" in c


def test_cache_limitado_get_de_chave_ausente_nao_quebra():
    c = _CacheLimitado(maxsize=2)
    assert c.get("nao-existe") is None
    assert c.get("nao-existe", "padrao") == "padrao"


def test_cache_limitado_sobrescrever_chave_existente_nao_perde_entrada():
    c = _CacheLimitado(maxsize=2)
    c["a"] = [1.0]
    c["a"] = [9.0]   # mesma chave, valor novo -- não deve "contar como 2ª entrada"
    assert len(c) == 1
    assert c["a"] == [9.0]


# --------------------------- integração com embeddings_deepinfra --------- #

def _resposta_ok(vetores: list[list[float]]):
    class _Resp:
        status_code = 200
        def json(self):
            return {"data": [{"embedding": v} for v in vetores]}
    return _Resp()


def test_embeddings_deepinfra_evita_novas_chamadas_apos_eviction_mas_continua_correto(monkeypatch):
    """O cache tem teto baixo pra este teste -- ao evictar uma entrada
    antiga, o texto correspondente simplesmente é re-buscado na próxima
    vez (comportamento correto, só perde o hit de cache -- não quebra)."""
    monkeypatch.setattr(emb, "_cache_deepinfra", _CacheLimitado(maxsize=2))
    monkeypatch.setattr(emb.settings, "DEEPINFRA_API_KEY", "fake-key")
    chamadas = []

    def _fake_post(url, json=None, **kw):
        chamadas.append(list(json["input"]))
        return _resposta_ok([[float(len(t))] for t in json["input"]])

    with patch("app.matching.embeddings.requests.post", side_effect=_fake_post):
        emb.embeddings_deepinfra(["aa", "bb"])   # enche o cache (teto=2)
        assert len(chamadas) == 1

        emb.embeddings_deepinfra(["cc"])   # evicta "aa" (a mais antiga) pra abrir espaço
        assert len(chamadas) == 2
        assert emb._cache_deepinfra.get("bb") == [2.0]   # "bb" sobrevive, não foi tocada

        r = emb.embeddings_deepinfra(["aa"])   # "aa" não está mais no cache -- busca de novo
        assert chamadas[-1] == ["aa"]
        assert r == [[2.0]]   # resultado correto, só custou uma chamada extra (perdeu o hit, não o dado)
