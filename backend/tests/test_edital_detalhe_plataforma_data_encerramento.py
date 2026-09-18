"""
Pedido do usuário: um card na página do edital com CNPJ, cidade/estado,
plataforma (sistema onde o pregão ocorre) e a data limite para envio de
proposta. CNPJ/cidade/estado já vinham em GET /api/editais/{id}/detalhe;
plataforma e data_encerramento (fim do recebimento de propostas) já eram
coletados e salvos em Edital, mas não estavam nesse payload -- sem isso o
card não tinha de onde puxar esses dois campos. Rode com:
cd backend && pytest
"""
import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import edital_detalhe
from app.models import Base, Usuario, Edital


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


def test_detalhe_inclui_plataforma_e_data_encerramento():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao",
               uf="SP", municipio="Campinas", plataforma="ComprasNet",
               link_sistema_origem="https://comprasnet.gov.br/pregao/123",
               data_encerramento=datetime.date(2026, 10, 15))
    db.add(ed)
    db.commit()

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)

    assert r["edital"]["plataforma"] == "ComprasNet"
    assert r["edital"]["link_sistema_origem"] == "https://comprasnet.gov.br/pregao/123"
    # T00:00:00: data_encerramento agora guarda hora (ver _parse_data_hora
    # em connectors/pncp.py) -- isoformat() de datetime inclui a hora.
    assert r["edital"]["data_encerramento"] == "2026-10-15T00:00:00"
    assert r["edital"]["municipio"] == "Campinas"
    assert r["edital"]["uf"] == "SP"


def test_detalhe_plataforma_e_data_encerramento_none_quando_nao_coletados():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)

    assert r["edital"]["plataforma"] is None
    assert r["edital"]["link_sistema_origem"] is None
    assert r["edital"]["data_encerramento"] is None


# --------- status_prazo: "Recebendo proposta" entre início e fim --------- #

def test_detalhe_status_prazo_recebendo_quando_dentro_da_janela():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP",
               data_abertura=datetime.date.today() - datetime.timedelta(days=2),
               data_encerramento=datetime.date.today() + datetime.timedelta(days=5))
    db.add(ed)
    db.commit()

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)

    assert r["edital"]["status_prazo"] == "recebendo"


def test_detalhe_status_prazo_aguardando_antes_da_abertura():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP",
               data_abertura=datetime.date.today() + datetime.timedelta(days=3))
    db.add(ed)
    db.commit()

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)

    assert r["edital"]["status_prazo"] == "aguardando"


def test_detalhe_status_prazo_encerrado_depois_do_fim():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP",
               data_abertura=datetime.date.today() - datetime.timedelta(days=20),
               data_encerramento=datetime.date.today() - datetime.timedelta(days=1))
    db.add(ed)
    db.commit()

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)

    assert r["edital"]["status_prazo"] == "encerrado"
