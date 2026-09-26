"""
Achado real (usuário reportou, reproduzido ao vivo: abrir um edital novo,
trocar de aba, sair): _match_do_usuario_por_edital (chamada por
registrar_interacao/marcar/mudar_status) cria um Match na hora só pra
guardar lido/interessante/status/interagido_em em editais sem sinal
automático nenhum -- mas com nivel/score que NUNCA foram calculados de
verdade. Sem distinguir isso, um edital "sem análise automática" passava a
mostrar "Média compatibilidade" no card/cabeçalho só por o usuário ter
aberto a página, o que não faz sentido nenhum.

O motor de matching de verdade (service.py, _gerar_matches_usuario) sempre
grava Match.detalhe (mesmo pro nível "fraco" que sobrevive por engajamento)
-- detalhe None é o sinal confiável de "nunca avaliado de verdade", só
existe porque o próprio usuário interagiu. listar_editais()/edital_detalhe()
agora escondem match_id/score/nivel nesse caso (o card renderiza igual a um
edital sem match nenhum), mas continuam expondo lido/interessante/status
normalmente -- essas SIM refletem uma ação real do usuário.
Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import listar_editais, edital_detalhe, registrar_interacao, marcar, MarcarIn
from app.models import Base, Usuario, Edital, ItemEdital, Match


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    return u


def _edital(db, id_externo="ed1"):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Item de teste"))
    db.commit()
    return ed


def _listar(db, u):
    return listar_editais(nivel=None, uf=None, plataforma=None, modalidade=None, status=None,
                          vista="ativos", apenas_nao_lidos=False, apenas_interessantes=False,
                          hoje=False, tipo="todos", valor_min=None, valor_max=None,
                          data_de=None, data_ate=None, busca_item=None, todos_editais=False,
                          pagina=1, por_pagina=50, user=u, db=db)


def test_listar_editais_esconde_selo_quando_match_veio_so_de_interacao():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)

    registrar_interacao(ed.id, user=u, db=db)   # mesma chamada que abrir a pagina do edital dispara

    r = _listar(db, u)
    item = next(x for x in r["resultados"] if x["edital_id"] == ed.id)
    assert item["match_id"] is None
    assert item["score"] is None
    assert item["nivel"] is None


def test_listar_editais_esconde_selo_quando_match_veio_so_de_marcar_lido():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)

    marcar(ed.id, MarcarIn(lido=True), user=u, db=db)

    r = _listar(db, u)
    item = next(x for x in r["resultados"] if x["edital_id"] == ed.id)
    assert item["match_id"] is None
    assert item["score"] is None
    assert item["nivel"] is None
    assert item["lido"] is True   # a ação real do usuário continua refletida


def test_listar_editais_mostra_selo_quando_motor_avaliou_de_verdade():
    """Controle dos dois testes acima -- um Match com detalhe (só o motor de
    matching grava isso, ver service.py) continua mostrando nivel/score/
    match_id normalmente."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.8, nivel="forte",
                 detalhe={"itens": []}))
    db.commit()

    r = _listar(db, u)
    item = next(x for x in r["resultados"] if x["edital_id"] == ed.id)
    assert item["match_id"] is not None
    assert item["score"] == 0.8
    assert item["nivel"] == "forte"


def test_edital_detalhe_esconde_selo_quando_match_veio_so_de_interacao():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)

    registrar_interacao(ed.id, user=u, db=db)

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)
    assert r["edital"]["match_id"] is None
    assert r["edital"]["score"] is None
    assert r["edital"]["nivel"] is None


def test_edital_detalhe_mostra_selo_quando_motor_avaliou_de_verdade():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.6, nivel="medio",
                 detalhe={"itens": []}))
    db.commit()

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)
    assert r["edital"]["match_id"] is not None
    assert r["edital"]["score"] == 0.6
    assert r["edital"]["nivel"] == "medio"
