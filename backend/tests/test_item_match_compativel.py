"""
Achado real (auditoria de arquitetura pedida pelo usuário): a regra "este
item do Match.detalhe é um match confirmado de verdade" (confiança alta OU
confirmado manualmente, com fallback pra dado legado sem "confianca") estava
reimplementada em 5 lugares -- lista de editais, /detalhe, cotação/proposta
(_produtos_confirmados_por_numero) e Inteligência de Preço (duas rotas) --
e só a cópia da lista de editais tinha o fallback de dado legado. Um match
antigo (criado antes do campo "confianca" existir, ainda não recalculado)
contava como compatível no card da lista mas NÃO contava em cotação/
proposta/Inteligência de Preço: resultado divergente pro MESMO match.
Consolidado em app.main._item_match_compativel; estes testes cobrem a regra
em si e a consistência entre as rotas que passaram a chamá-la.
Rode com: cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import _item_match_compativel, _produtos_confirmados_por_numero, inteligencia_preco_editais
from app.models import Base, Usuario, Produto, Edital, ItemEdital, Match


# --------------------------- _item_match_compativel --------------------- #

def test_confianca_alta_e_compativel():
    assert _item_match_compativel({"produto_id": 1, "confianca": "alta"}) is True


def test_confianca_media_sem_confirmacao_nao_e_compativel():
    assert _item_match_compativel({"produto_id": 1, "confianca": "media"}) is False


def test_confianca_media_confirmada_manualmente_e_compativel():
    assert _item_match_compativel(
        {"produto_id": 1, "confianca": "media", "confirmado_manualmente": True}) is True


def test_sem_produto_id_nunca_e_compativel_mesmo_com_confianca_alta():
    assert _item_match_compativel({"produto_id": None, "confianca": "alta"}) is False


def test_dado_legado_sem_confianca_mas_com_produto_e_compativel():
    """O fallback que só existia numa das 5 cópias originais: item de uma
    rodada anterior à existência do campo "confianca" -- ainda não
    recalculado -- continua contando como compatível."""
    assert _item_match_compativel({"produto_id": 1}) is True


def test_dado_legado_sem_produto_id_nao_e_compativel():
    assert _item_match_compativel({}) is False


# --------- consistência entre rotas (o bug de verdade: resultado --------- #
# --------- divergente pro MESMO match, dependendo da tela) --------- #

def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_produtos_confirmados_por_numero_inclui_item_legado_sem_confianca():
    db = _sessao()
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    p = Produto(usuario_id=u.id, descricao="Papel A4")
    db.add(p)
    db.commit()
    ed = Edital(fonte="PNCP", id_externo="e1", orgao="Orgao", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    # item SEM "confianca" -- dado de antes desse campo existir, nunca recalculado
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=1.0, nivel="forte",
                detalhe={"itens": [{"item": 1, "produto_id": p.id}]}))
    db.commit()

    mapa = _produtos_confirmados_por_numero(ed.id, u, db)

    assert mapa.get(1) is not None and mapa[1].id == p.id


def test_inteligencia_preco_editais_inclui_item_legado_sem_confianca():
    db = _sessao()
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    p = Produto(usuario_id=u.id, descricao="Papel A4")
    db.add(p)
    db.commit()
    ed = Edital(fonte="PNCP", id_externo="e1", orgao="Orgao", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Papel A4", valor_unitario=10.0))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=1.0, nivel="forte",
                detalhe={"itens": [{"item": 1, "produto_id": p.id}]}))
    db.commit()

    referencias = inteligencia_preco_editais(p.id, user=u, db=db)

    assert len(referencias) == 1
