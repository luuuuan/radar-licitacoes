"""
Testes de POST /api/editais/{edital_id}/marcar e /status. Banco sqlite em
memória, sem HTTP — chama as funções da rota diretamente (mesmo padrão de
test_confirmar_item_edital.py).

Pedido do usuário: mostrar os botões de acompanhamento (status/lido/
interessante) mesmo em editais sem Match automático (comum agora que a
busca por item acha editais "sem_match" — ver test_editais_filtros.py).
Antes, esses endpoints eram chaveados por match_id e davam 404 quando não
havia Match nenhum; agora são chaveados por edital_id e criam o Match na
hora, mesmo padrão já usado em confirmar_item_edital.

Rode com:  cd backend && pytest
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import marcar, mudar_status, MarcarIn, StatusIn
from app.models import Base, Usuario, Edital, Match


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


def _edital(db):
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    return ed


def test_marcar_lido_sem_match_existente_cria_o_match():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    # nenhum Match criado de propósito

    r = marcar(ed.id, MarcarIn(lido=True), user=u, db=db)

    assert r == {"ok": True}
    match = db.query(Match).filter(Match.edital_id == ed.id, Match.usuario_id == u.id).first()
    assert match is not None
    assert match.lido is True


def test_marcar_interessante_sem_match_existente_cria_o_match():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)

    marcar(ed.id, MarcarIn(interessante=True), user=u, db=db)

    match = db.query(Match).filter(Match.edital_id == ed.id, Match.usuario_id == u.id).first()
    assert match.interessante is True


def test_marcar_reaproveita_match_ja_existente_em_vez_de_duplicar():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    match = Match(edital_id=ed.id, usuario_id=u.id, score=0.8, nivel="forte")
    db.add(match)
    db.commit()

    marcar(ed.id, MarcarIn(lido=True), user=u, db=db)

    total = db.query(Match).filter(Match.edital_id == ed.id, Match.usuario_id == u.id).count()
    assert total == 1
    db.refresh(match)
    assert match.lido is True
    assert match.nivel == "forte"   # não mexeu no que já existia


def test_marcar_edital_inexistente_da_404():
    db = _sessao()
    u = _usuario(db)
    with pytest.raises(HTTPException) as exc:
        marcar(999999, MarcarIn(lido=True), user=u, db=db)
    assert exc.value.status_code == 404


def test_mudar_status_sem_match_existente_cria_o_match():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)

    r = mudar_status(ed.id, StatusIn(status="vou_participar"), user=u, db=db)

    assert r == {"ok": True}
    match = db.query(Match).filter(Match.edital_id == ed.id, Match.usuario_id == u.id).first()
    assert match.status == "vou_participar"


def test_mudar_status_invalido_continua_400_mesmo_sem_match():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)

    with pytest.raises(HTTPException) as exc:
        mudar_status(ed.id, StatusIn(status="nao_existe"), user=u, db=db)
    assert exc.value.status_code == 400
    # não deve ter criado Match nenhum num status inválido
    assert db.query(Match).filter(Match.edital_id == ed.id).count() == 0


def test_mudar_status_edital_inexistente_da_404():
    db = _sessao()
    u = _usuario(db)
    with pytest.raises(HTTPException) as exc:
        mudar_status(999999, StatusIn(status="vou_participar"), user=u, db=db)
    assert exc.value.status_code == 404


def test_mudar_status_grava_quando_a_mudanca_aconteceu():
    """status_atualizado_em alimenta o filtro por mês do card "Editais
    ganhos" -- sem isso não tinha como saber QUANDO virou "ganho", só o
    status atual."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)

    mudar_status(ed.id, StatusIn(status="ganho"), user=u, db=db)

    match = db.query(Match).filter(Match.edital_id == ed.id, Match.usuario_id == u.id).first()
    assert match.status_atualizado_em is not None
