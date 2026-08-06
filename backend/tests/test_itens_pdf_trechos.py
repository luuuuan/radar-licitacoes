"""
Testes de _selecionar_trechos_relevantes() — em vez de mandar o PDF inteiro
pro modelo de chat ler (lento/instável em editais grandes, testado contra a
API real: o mesmo prompt de ~150k chars ora respondia em segundos, ora
estourava o timeout), usa embeddings (BGE-M3/DeepInfra) pra achar só os
trechos parecidos com cada item antes de montar o prompt. Sem rede — a
chamada de embeddings é mockada. Rode com: cd backend && pytest
"""
from unittest.mock import patch

from app import itens_pdf as ip


def _texto_com_n_chunks(n, marcador="X"):
    """Monta um texto com N chunks de _CHUNK_TAM chars, cada um com um
    marcador único (ex.: 'CHUNK2') no início, pra identificar depois qual
    chunk sobreviveu à seleção."""
    partes = []
    for i in range(n):
        marca = f"CHUNK{i}-{marcador} "
        partes.append((marca + "conteúdo " * 400)[:ip._CHUNK_TAM])
    return "".join(partes)


def test_texto_curto_nao_aciona_embeddings_e_volta_como_esta():
    texto = "texto pequeno, cabe tudo" * 10
    with patch("app.itens_pdf.embeddings_deepinfra") as mock_emb:
        r = ip._selecionar_trechos_relevantes(texto, [{"numero": 1, "descricao": "x"}], api_key="fake")
    assert r == texto[:ip._JANELA_MAX_CHARS]
    assert not mock_emb.called


def test_seleciona_chunk_mais_parecido_e_vizinhos(monkeypatch):
    monkeypatch.setattr(ip, "_CHUNK_TOP_K", 1)   # só o melhor chunk por item — resultado determinístico
    texto = _texto_com_n_chunks(5)

    def _emb_fake(textos, timeout=30, api_key=None):
        # 5 chunks + 1 query. Vetores one-hot: chunk 2 "combina" perfeitamente com a query.
        vetores_chunks = [[1 if j == i else 0 for j in range(5)] for i in range(5)]
        vetor_query = [0, 0, 1, 0, 0]
        return vetores_chunks + [vetor_query]

    with patch("app.itens_pdf.embeddings_deepinfra", side_effect=_emb_fake):
        r = ip._selecionar_trechos_relevantes(texto, [{"numero": 15, "descricao": "algo"}], api_key="fake")

    assert "CHUNK1-" in r and "CHUNK2-" in r and "CHUNK3-" in r   # melhor match (2) + vizinhos (1, 3)
    assert "CHUNK0-" not in r and "CHUNK4-" not in r


def test_embeddings_indisponiveis_cai_pro_corte_simples(monkeypatch):
    texto = _texto_com_n_chunks(5)
    with patch("app.itens_pdf.embeddings_deepinfra", return_value=[None] * 6):
        r = ip._selecionar_trechos_relevantes(texto, [{"numero": 1, "descricao": "x"}], api_key="fake")
    assert r == texto[:ip._JANELA_MAX_CHARS]
