"""
Achado real: a IA extraía CNPJ do órgão lendo o texto do PDF (sujeito a
erro/omissão), embora o dado já viesse certo e estruturado do PNCP
(Edital.cnpj_orgao, coletado direto do campo orgaoEntidade.cnpj). GET
/api/editais/{id}/detalhe passa a devolver esse campo, pra tela de Análise
por IA mostrar o CNPJ de verdade em vez de depender da IA adivinhar. Rode
com:  cd backend && pytest
"""
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


def test_detalhe_inclui_cnpj_do_orgao():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste",
               cnpj_orgao="46384111000140", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)

    assert r["edital"]["cnpj_orgao"] == "46384111000140"


def test_detalhe_cnpj_orgao_none_quando_nao_coletado():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)

    assert r["edital"]["cnpj_orgao"] is None
