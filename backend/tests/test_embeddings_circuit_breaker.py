"""
Teste de regressão: o disjuntor de cota do Gemini (embeddings) deve ser
isolado POR CHAVE/usuário. Antes do fix, era uma variável global única e
um usuário estourando a cota dele pausava a IA semântica pra todo mundo.

Rode com:  cd backend && pytest
"""
from app.matching import embeddings as emb


def test_disjuntor_e_isolado_por_chave():
    chave_a = "chave-do-usuario-a"
    chave_b = "chave-do-usuario-b"

    # garante estado limpo (módulo é compartilhado entre testes)
    emb._bloqueado_ate.clear()

    assert not emb.ia_bloqueada(chave_a)
    assert not emb.ia_bloqueada(chave_b)

    # usuário A estoura a cota (simula resposta 429 do Gemini)
    emb._pausar(chave_a, emb._COOLDOWN_429, "teste: cota estourada")

    assert emb.ia_bloqueada(chave_a) is True
    # usuário B precisa continuar livre — este é o bug que foi corrigido
    assert emb.ia_bloqueada(chave_b) is False
    assert emb.segundos_para_liberar(chave_a) > 0
    assert emb.segundos_para_liberar(chave_b) == 0


def test_sem_chave_nunca_bloqueia():
    emb._bloqueado_ate.clear()
    assert emb.ia_bloqueada(None) is False
    assert emb.segundos_para_liberar(None) == 0
