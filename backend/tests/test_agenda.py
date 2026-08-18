"""
GET /api/agenda: sessões de disputa (data_encerramento -- prazo final de
propostas, que é quando a sessão de disputa acontece de fato em pregão
eletrônico) dos editais com match do usuário, numa janela de uma semana
(domingo a sábado), navegável por offset (semanas a partir da atual).

Achado real: usava data_abertura (dataAberturaProposta no PNCP -- quando a
janela de propostas ABRE, normalmente logo após a publicação, quase sempre
já passado quando o usuário está olhando o edital) em vez de
data_encerramento (o mesmo campo que já vira "dias_restantes"/"faltam X
dias" em toda a tela) -- um edital com prazo em 15 dias aparecia no
calendário com uma data já vencida. Rode com:  cd backend && pytest
"""
import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import agenda
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


def _edital_com_match(db, usuario, id_externo, data_encerramento, valor_estimado=None):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste",
                objeto="Aquisicao", uf="SP", valor_estimado=valor_estimado,
                data_encerramento=data_encerramento)
    db.add(ed)
    db.commit()
    db.add(Match(usuario_id=usuario.id, edital_id=ed.id, score=0.5, nivel="medio"))
    db.commit()
    return ed


def _inicio_semana(offset=0):
    """Mesma fórmula usada em app.main.agenda — domingo da semana em questão."""
    hoje = datetime.date.today()
    return hoje - datetime.timedelta(days=(hoje.weekday() + 1) % 7) + datetime.timedelta(weeks=offset)


def test_semana_vai_de_domingo_a_sabado():
    db = _sessao()
    u = _usuario(db)
    inicio = _inicio_semana()

    r = agenda(offset=0, user=u, db=db)

    assert r["inicio"] == inicio.isoformat()
    assert r["fim"] == (inicio + datetime.timedelta(days=6)).isoformat()
    assert len(r["dias"]) == 7


def test_so_traz_editais_com_prazo_final_dentro_da_semana():
    db = _sessao()
    u = _usuario(db)
    inicio = _inicio_semana()
    dentro = _edital_com_match(db, u, "ed-dentro", data_encerramento=inicio + datetime.timedelta(days=2))
    _edital_com_match(db, u, "ed-antes", data_encerramento=inicio - datetime.timedelta(days=1))
    _edital_com_match(db, u, "ed-depois", data_encerramento=inicio + datetime.timedelta(days=7))

    r = agenda(offset=0, user=u, db=db)

    assert len(r["sessoes"]) == 1
    assert r["sessoes"][0]["edital_id"] == dentro.id


def test_offset_navega_pra_semana_anterior_e_seguinte():
    db = _sessao()
    u = _usuario(db)
    ed_passada = _edital_com_match(db, u, "ed-passada",
        data_encerramento=_inicio_semana(-1) + datetime.timedelta(days=1))
    ed_futura = _edital_com_match(db, u, "ed-futura",
        data_encerramento=_inicio_semana(1) + datetime.timedelta(days=1))

    r_passada = agenda(offset=-1, user=u, db=db)
    r_futura = agenda(offset=1, user=u, db=db)
    r_atual = agenda(offset=0, user=u, db=db)

    assert [s["edital_id"] for s in r_passada["sessoes"]] == [ed_passada.id]
    assert [s["edital_id"] for s in r_futura["sessoes"]] == [ed_futura.id]
    assert r_atual["sessoes"] == []


def test_so_retorna_match_do_proprio_usuario():
    db = _sessao()
    u1 = _usuario(db, email="u1@t.com")
    u2 = _usuario(db, email="u2@t.com")
    inicio = _inicio_semana()
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao", objeto="X", uf="SP",
                data_encerramento=inicio + datetime.timedelta(days=1))
    db.add(ed)
    db.commit()
    db.add(Match(usuario_id=u1.id, edital_id=ed.id, score=0.5, nivel="medio"))
    db.commit()

    r = agenda(offset=0, user=u2, db=db)

    assert r["sessoes"] == []


def test_dias_marca_tem_sessao_apenas_no_dia_certo():
    db = _sessao()
    u = _usuario(db)
    inicio = _inicio_semana()
    _edital_com_match(db, u, "ed1", data_encerramento=inicio + datetime.timedelta(days=3))

    r = agenda(offset=0, user=u, db=db)

    assert r["dias"][3]["tem_sessao"] is True
    assert all(not d["tem_sessao"] for i, d in enumerate(r["dias"]) if i != 3)


def test_edital_sem_sessao_na_semana_nao_aparece():
    db = _sessao()
    u = _usuario(db)
    inicio = _inicio_semana()
    _edital_com_match(db, u, "ed1", data_encerramento=inicio + datetime.timedelta(days=30))

    r = agenda(offset=0, user=u, db=db)

    assert r["sessoes"] == []
    assert all(not d["tem_sessao"] for d in r["dias"])


def test_sessao_expoe_data_sessao_igual_ao_prazo_final():
    db = _sessao()
    u = _usuario(db)
    inicio = _inicio_semana()
    prazo = inicio + datetime.timedelta(days=2)
    _edital_com_match(db, u, "ed1", data_encerramento=prazo)

    r = agenda(offset=0, user=u, db=db)

    assert r["sessoes"][0]["data_sessao"] == prazo.isoformat()
