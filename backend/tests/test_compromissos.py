"""
GET /api/compromissos: calendário de compromissos num intervalo qualquer
(semana ou mês -- quem decide o intervalo é o front) juntando documentos de
habilitação vencendo + editais que o usuário marcou "vou participar" com
prazo final (data_encerramento -- não data_abertura, que no PNCP é
dataAberturaProposta e quase sempre já passou; mesmo achado real de
test_agenda.py). Rode com:  cd backend && pytest
"""
import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import compromissos
from app.models import Base, Usuario, Edital, Match, Documento


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


def _edital_vou_participar(db, usuario, id_externo, data_encerramento):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste",
                objeto="Aquisicao", uf="SP", data_encerramento=data_encerramento)
    db.add(ed)
    db.commit()
    db.add(Match(usuario_id=usuario.id, edital_id=ed.id, score=0.8, nivel="forte",
                 status="vou_participar"))
    db.commit()
    return ed


def _documento(db, usuario, nome, data_validade, ativo=True):
    d = Documento(usuario_id=usuario.id, nome=nome, data_validade=data_validade, ativo=ativo)
    db.add(d)
    db.commit()
    return d


def test_junta_documento_e_edital_no_intervalo():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_vou_participar(db, u, "ed1", datetime.date(2026, 8, 20))
    doc = _documento(db, u, "CND FGTS", datetime.date(2026, 8, 22))

    r = compromissos(inicio=datetime.date(2026, 8, 18), fim=datetime.date(2026, 8, 24), user=u, db=db)

    assert len(r["compromissos"]) == 2
    tipos = {c["tipo"] for c in r["compromissos"]}
    assert tipos == {"documento", "edital"}
    edital_c = next(c for c in r["compromissos"] if c["tipo"] == "edital")
    doc_c = next(c for c in r["compromissos"] if c["tipo"] == "documento")
    assert edital_c["edital_id"] == ed.id
    assert doc_c["documento_id"] == doc.id


def test_ordena_por_data():
    db = _sessao()
    u = _usuario(db)
    _documento(db, u, "Depois", datetime.date(2026, 8, 23))
    _edital_vou_participar(db, u, "ed1", datetime.date(2026, 8, 19))

    r = compromissos(inicio=datetime.date(2026, 8, 18), fim=datetime.date(2026, 8, 24), user=u, db=db)

    assert [c["data"] for c in r["compromissos"]] == ["2026-08-19", "2026-08-23"]


def test_edital_com_status_diferente_de_vou_participar_fica_de_fora():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao", objeto="X", uf="SP",
               data_encerramento=datetime.date(2026, 8, 20))
    db.add(ed)
    db.commit()
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.8, nivel="forte", status="novo"))
    db.commit()

    r = compromissos(inicio=datetime.date(2026, 8, 18), fim=datetime.date(2026, 8, 24), user=u, db=db)

    assert r["compromissos"] == []


def test_documento_inativo_fica_de_fora():
    db = _sessao()
    u = _usuario(db)
    _documento(db, u, "Vencido e arquivado", datetime.date(2026, 8, 20), ativo=False)

    r = compromissos(inicio=datetime.date(2026, 8, 18), fim=datetime.date(2026, 8, 24), user=u, db=db)

    assert r["compromissos"] == []


def test_fora_do_intervalo_fica_de_fora():
    db = _sessao()
    u = _usuario(db)
    _edital_vou_participar(db, u, "ed1", datetime.date(2026, 9, 1))
    _documento(db, u, "Doc", datetime.date(2026, 7, 1))

    r = compromissos(inicio=datetime.date(2026, 8, 1), fim=datetime.date(2026, 8, 31), user=u, db=db)

    assert r["compromissos"] == []


def test_so_retorna_do_proprio_usuario():
    db = _sessao()
    u1 = _usuario(db, email="u1@t.com")
    u2 = _usuario(db, email="u2@t.com")
    _edital_vou_participar(db, u1, "ed1", datetime.date(2026, 8, 20))
    _documento(db, u1, "Doc", datetime.date(2026, 8, 21))

    r = compromissos(inicio=datetime.date(2026, 8, 18), fim=datetime.date(2026, 8, 24), user=u2, db=db)

    assert r["compromissos"] == []


def test_dias_com_compromisso_sem_duplicar_data_repetida():
    db = _sessao()
    u = _usuario(db)
    _edital_vou_participar(db, u, "ed1", datetime.date(2026, 8, 20))
    _documento(db, u, "Doc", datetime.date(2026, 8, 20))

    r = compromissos(inicio=datetime.date(2026, 8, 18), fim=datetime.date(2026, 8, 24), user=u, db=db)

    assert r["dias_com_compromisso"] == ["2026-08-20"]


def test_intervalo_com_fim_antes_do_inicio_da_400():
    db = _sessao()
    u = _usuario(db)
    with pytest.raises(HTTPException) as exc:
        compromissos(inicio=datetime.date(2026, 8, 24), fim=datetime.date(2026, 8, 18), user=u, db=db)
    assert exc.value.status_code == 400


def test_intervalo_maior_que_o_limite_da_400():
    db = _sessao()
    u = _usuario(db)
    with pytest.raises(HTTPException) as exc:
        compromissos(inicio=datetime.date(2026, 1, 1), fim=datetime.date(2026, 12, 31), user=u, db=db)
    assert exc.value.status_code == 400


def test_intervalo_de_um_mes_e_aceito():
    """Sanity: um mês inteiro (uso real do card em modo "Mês") não deve
    bater no limite de custo."""
    db = _sessao()
    u = _usuario(db)
    r = compromissos(inicio=datetime.date(2026, 8, 1), fim=datetime.date(2026, 8, 31), user=u, db=db)
    assert r["compromissos"] == []
