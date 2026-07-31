"""
Teste do _melhor_por_keywords() do MatchingEngine: catálogo "enriquecido"
tende a cadastrar o mesmo conceito em várias granularidades sobrepostas nas
palavras_chave (ex.: "papel sulfite a4", "papel sulfite", "sulfite" e
"papel" separados) — sem deduplicar substrings, isso inflava o score contando
o mesmo sinal várias vezes. Rode com:  cd backend && pytest
"""
import pytest

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
    score, _, motivo, _n = engine._melhor_por_keywords(item.texto_busca())
    # 1 termo específico ("papel sulfit", "fraco") + bônus de +0.05 por
    # "papel" bare também bater como termo genérico de reforço (ver
    # _GENERICAS — "papel" sozinho virou genérico depois do bug real de
    # "Papel Não Clorado" casando com "Papel Photo Glossy" só por essa
    # palavra) — 0.35 + 0.05, não 3+ termos específicos inflados ("forte").
    assert score == pytest.approx(0.40), motivo
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
    score, _, motivo, _n = engine._melhor_por_keywords(item.texto_busca())
    assert score == 0.35, motivo  # 1 termo específico ("fraco"), não 2 ("médio")


def test_item_kit_gigante_nao_bate_por_coincidencia_de_2_termos():
    """Item 'kit'/'conjunto' que enumera dezenas de peças pode compartilhar
    2-3 termos com um produto completamente não relacionado, por pura
    coincidência (ex.: 'Fita Isolante' e 'Trena 3m' entre as peças de um kit
    de ferramentas batendo com uma fita adesiva de papelaria). 2 termos em
    mais de 100 não é sinal de match — a trava de anti-coincidência (que já
    existia pra 1 termo) precisa também considerar a proporção."""
    item_texto = (
        "KIT DE FERRAMENTAS COM MALETA 108 PECAS. Conteudo: 1 Chave Inglesa 6, "
        "1 Alicate Bomba Dagua 8, 1 Alicate Universal 6.1/2, 1 Chave Phillips PH2, "
        "1 Chave de Fenda SL5, 1 Punho Catraca, 1 Nivel Aluminio 300mm, "
        "1 Martelo 25mm 300g, 1 Arco de Serra 6, 1 Fita Isolante 5m, "
        "1 Estilete 18mm, 1 Chave Teste 100-250V, 1 Trena 3m, 2 Grampos Multiuso, "
        "6 Chaves Combinadas, 1 Adaptador de Soquete 1/4, 11 Soquetes 1/4, "
        "9 Soquetes 3/8, 1 Jogo com 10 Bits, 1 Jogo com 11 Chaves Allen. "
        "Garantia 120 meses."
    )
    produtos = [ProdutoCat(
        id=1, descricao="Fita Adesiva 12x50 Transparente PP Durex 9388 3m - 6RL",
        palavras_chave="fita adesiva, fita transparente, durex, 3m, fita",
    )]
    engine = MatchingEngine(produtos, usar_ia=False)
    item = ItemEdt(numero=1, descricao=item_texto)
    score, _, motivo = engine._score_item(item)
    assert score <= 0.34, motivo  # abaixo de LIMIAR_ITEM (0.35) -> nem vira candidato


def test_palavras_chave_genuinamente_distintas_ainda_somam():
    """Termos específicos que NÃO se sobrepõem (não são substring um do
    outro) continuam contando cada um — a dedup só remove redundância real."""
    produtos = [ProdutoCat(
        id=1, descricao="Caneta Esferográfica Azul",
        palavras_chave="caneta esferografica, azul, compactor",
    )]
    engine = MatchingEngine(produtos, usar_ia=False)
    item = ItemEdt(numero=1, descricao="Caneta esferografica azul compactor para escritorio")
    score, _, motivo, _n = engine._melhor_por_keywords(item.texto_busca())
    assert score == 0.66  # 3 termos específicos genuinamente distintos
