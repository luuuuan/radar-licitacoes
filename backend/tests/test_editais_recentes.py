"""
POST /api/editais/{id}/interacao (registra navegação entre abas de um edital
aberto) + GET /api/editais/recentes (card "Analisados recentemente" do
painel Início). Banco sqlite em memória, sem HTTP. Rode com:
cd backend && pytest
"""
import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import registrar_interacao, editais_recentes
from app.models import Base, Usuario, Edital, Match


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db, email="t@t.com"):
    u = Usuario(nome="Teste", email=email, senha_hash="x")
    db.add(u)
    db.commit()
    return u


def _edital(db, id_externo):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste",
               objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    return ed


# --------- POST /api/editais/{id}/interacao --------- #

def test_registrar_interacao_cria_match_quando_nao_existe():
    """Edital sem sinal nenhum pro motor automático (sem Match) -- mesmo
    padrão de marcar/status: cria na hora."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, "ed1")

    registrar_interacao(ed.id, user=u, db=db)

    m = db.query(Match).filter(Match.edital_id == ed.id, Match.usuario_id == u.id).first()
    assert m is not None
    assert m.interagido_em is not None


def test_registrar_interacao_atualiza_match_existente():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, "ed1")
    m = Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio")
    db.add(m)
    db.commit()
    assert m.interagido_em is None

    registrar_interacao(ed.id, user=u, db=db)

    db.refresh(m)
    assert m.interagido_em is not None


def test_registrar_interacao_edital_inexistente_da_404():
    db = _sessao()
    u = _usuario(db)
    with pytest.raises(HTTPException) as exc:
        registrar_interacao(999, user=u, db=db)
    assert exc.value.status_code == 404


def test_registrar_interacao_nao_mexe_no_match_de_outro_usuario():
    db = _sessao()
    u1 = _usuario(db, email="u1@t.com")
    u2 = _usuario(db, email="u2@t.com")
    ed = _edital(db, "ed1")
    m1 = Match(usuario_id=u1.id, edital_id=ed.id, score=0.5, nivel="medio")
    db.add(m1)
    db.commit()

    registrar_interacao(ed.id, user=u2, db=db)

    db.refresh(m1)
    assert m1.interagido_em is None   # o de u1 continua intocado
    m2 = db.query(Match).filter(Match.edital_id == ed.id, Match.usuario_id == u2.id).first()
    assert m2 is not None and m2.interagido_em is not None


# --------- GET /api/editais/recentes --------- #

def _match_interagido(db, usuario, id_externo, quando):
    ed = _edital(db, id_externo)
    m = Match(usuario_id=usuario.id, edital_id=ed.id, score=0.6, nivel="medio", interagido_em=quando)
    db.add(m)
    db.commit()
    return ed


def test_recentes_ordena_do_mais_novo_pro_mais_velho():
    db = _sessao()
    u = _usuario(db)
    agora = datetime.datetime.utcnow()
    ed_velho = _match_interagido(db, u, "ed-velho", agora - datetime.timedelta(hours=2))
    ed_novo = _match_interagido(db, u, "ed-novo", agora - datetime.timedelta(minutes=5))

    r = editais_recentes(user=u, db=db)

    assert [e["edital_id"] for e in r["editais"]] == [ed_novo.id, ed_velho.id]


def test_recentes_ignora_match_nunca_interagido():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, "ed1")
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio"))  # interagido_em=None
    db.commit()

    r = editais_recentes(user=u, db=db)

    assert r["editais"] == []


def test_recentes_so_retorna_do_proprio_usuario():
    db = _sessao()
    u1 = _usuario(db, email="u1@t.com")
    u2 = _usuario(db, email="u2@t.com")
    _match_interagido(db, u1, "ed1", datetime.datetime.utcnow())

    r = editais_recentes(user=u2, db=db)

    assert r["editais"] == []


def test_recentes_respeita_limite_customizado():
    db = _sessao()
    u = _usuario(db)
    agora = datetime.datetime.utcnow()
    for i in range(5):
        _match_interagido(db, u, f"ed{i}", agora - datetime.timedelta(minutes=i))

    r = editais_recentes(limite=2, user=u, db=db)

    assert len(r["editais"]) == 2


def test_recentes_limite_maximo_e_20_mesmo_pedindo_mais():
    db = _sessao()
    u = _usuario(db)
    agora = datetime.datetime.utcnow()
    for i in range(25):
        _match_interagido(db, u, f"ed{i}", agora - datetime.timedelta(minutes=i))

    r = editais_recentes(limite=999, user=u, db=db)

    assert len(r["editais"]) == 20
