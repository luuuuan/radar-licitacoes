"""
Teste do _melhor_por_keywords() do MatchingEngine: catálogo "enriquecido"
tende a cadastrar o mesmo conceito em várias granularidades sobrepostas nas
palavras_chave (ex.: "papel sulfite a4", "papel sulfite", "sulfite" e
"papel" separados) — sem deduplicar substrings, isso inflava o score contando
o mesmo sinal várias vezes. Rode com:  cd backend && pytest
"""
from app.matching.engine import MatchingEngine, ItemEdt, ProdutoCat


def test_palavras_chave_redundantes_nao_inflam_o_score():
    """4 entradas de palavras_chave que descrevem o MESMO conceito ("papel
    sulfite" em granularidades diferentes) não podem contar como 4 termos
    específicos — só a mais específica que bateu deve valer."""
    produtos = [ProdutoCat(
        id=1, descricao="Papel A4 75 g/m2 500 Folhas Branco 3502 Chamex",
        palavras_chave="papel sulfite a4, papel branco a4, papel sulfite, papel 75g, folha a4, papel a4, sulfite, papel, resma",
    )]
    engine = MatchingEngine(produtos, usar_ia=False)
    item = ItemEdt(numero=1, descricao=(
        "Livro Ata material: papel sulfite, quantidade folhas: 100, "
        "comprimento: 220, largura: 330, características adicionais: "
        "vertical, capa dura, folhas brancas pauta"
    ))
    score, _, motivo = engine._melhor_por_keywords(item.texto_busca())
    assert score == 0.35, motivo  # 1 termo específico ("fraco"), não 3+ ("forte")
    assert "3" not in motivo


def test_palavras_chave_singular_plural_nao_conta_duas_vezes():
    """'grampo' e 'grampos' (singular/plural, entradas separadas) stemizam
    pro mesmo radical — item que só menciona 'grampo' de passagem (ex.:
    caderno com 'ACABAMENTO GRAMPO') não pode contar como 2 termos batendo
    num produto de grampo de grampeador."""
    produtos = [ProdutoCat(
        id=1, descricao="Grampo para Grampeador 26/6 Galvanizado 2094 Maxprint - 5000UN",
        palavras_chave="grampo para grampeador, grampeador de mesa, grampo galvanizado, "
                       "grampeador manual, grampeador metal, grampo 26/6, grampeador, grampos, grampo",
    )]
    engine = MatchingEngine(produtos, usar_ia=False)
    item = ItemEdt(numero=1, descricao=(
        "CADERNO PORTFÓLIO DO ALUNO - MEDINDO 26,5X31 CM - ACABAMENTO GRAMPO - TIRAGEM 1000 UNIDADES"
    ))
    score, _, motivo = engine._melhor_por_keywords(item.texto_busca())
    assert score == 0.35, motivo  # 1 termo específico ("fraco"), não 2 ("médio")


def test_palavras_chave_genuinamente_distintas_ainda_somam():
    """Termos específicos que NÃO se sobrepõem (não são substring um do
    outro) continuam contando cada um — a dedup só remove redundância real."""
    produtos = [ProdutoCat(
        id=1, descricao="Caneta Esferográfica Azul",
        palavras_chave="caneta esferografica, azul, compactor",
    )]
    engine = MatchingEngine(produtos, usar_ia=False)
    item = ItemEdt(numero=1, descricao="Caneta esferografica azul compactor para escritorio")
    score, _, motivo = engine._melhor_por_keywords(item.texto_busca())
    assert score == 0.66  # 3 termos específicos genuinamente distintos
