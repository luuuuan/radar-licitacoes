"""
Achado real: GET /api/resumo já contava quantos editais abrem sessão hoje
(do_dia), mas não somava quanto isso representa em valor estimado — usado
pelo card "Editais do dia" do dashboard. Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import resumo
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


def _edital_com_match(db, usuario, id_externo, valor_estimado=None, data_abertura=None,
                      data_encerramento=None):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste",
                objeto="Aquisicao", uf="SP", valor_estimado=valor_estimado,
                data_abertura=data_abertura, data_encerramento=data_encerramento)
    db.add(ed)
    db.commit()
    db.add(Match(usuario_id=usuario.id, edital_id=ed.id, score=0.5, nivel="medio"))
    db.commit()
    return ed


def test_valor_do_dia_soma_so_os_editais_que_abrem_hoje():
    import datetime
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.date.today()
    amanha = hoje + datetime.timedelta(days=1)
    _edital_com_match(db, u, "ed1", valor_estimado=1000.0, data_abertura=hoje)
    _edital_com_match(db, u, "ed2", valor_estimado=5000.0, data_abertura=hoje)
    _edital_com_match(db, u, "ed3", valor_estimado=99999.0, data_abertura=amanha)

    r = resumo(user=u, db=db)

    assert r["do_dia"] == 2
    assert r["valor_do_dia"] == 6000.0


def test_valor_do_dia_zero_quando_nenhum_edital_abre_hoje():
    db = _sessao()
    u = _usuario(db)

    r = resumo(user=u, db=db)

    assert r["do_dia"] == 0
    assert r["valor_do_dia"] == 0


def test_valor_do_dia_ignora_edital_sem_valor_estimado():
    import datetime
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.date.today()
    _edital_com_match(db, u, "ed1", valor_estimado=None, data_abertura=hoje)
    _edital_com_match(db, u, "ed2", valor_estimado=2000.0, data_abertura=hoje)

    r = resumo(user=u, db=db)

    assert r["do_dia"] == 2
    assert r["valor_do_dia"] == 2000.0


def test_editais_ativos_conta_edital_com_abertura_passada_mas_encerramento_futuro():
    """Achado real (edital 127082, reportado pelo usuário): data_abertura no
    passado não significa que a janela de propostas fechou -- se
    data_encerramento (prazo final no PNCP) está no futuro, o edital
    continua ativo. Ver _dias_restantes_edital em app/main.py."""
    import datetime
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.date.today()
    _edital_com_match(db, u, "ed1",
                      data_abertura=hoje - datetime.timedelta(days=5),
                      data_encerramento=hoje + datetime.timedelta(days=10))

    r = resumo(user=u, db=db)

    assert r["editais"] == 1


def test_editais_ativos_nao_conta_edital_com_encerramento_passado():
    import datetime
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.date.today()
    _edital_com_match(db, u, "ed1",
                      data_abertura=hoje - datetime.timedelta(days=20),
                      data_encerramento=hoje - datetime.timedelta(days=5))

    r = resumo(user=u, db=db)

    assert r["editais"] == 0
