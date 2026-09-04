"""
Achado real (pedido do usuário): ItemEdital.unidade_medida já é coletado
do PNCP e usado internamente pro cálculo de margem (ver _custo_e_margem),
mas nunca era devolvido pro front -- o usuário não tinha como ver, no
card do item, se o órgão está pedindo em caixa, unidade, pacote etc.
GET /api/editais/{id}/detalhe passa a incluir esse campo por item. Rode
com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import edital_detalhe
from app.models import Base, Usuario, Edital, ItemEdital


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


def test_detalhe_inclui_unidade_medida_do_item():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Caneta esferográfica azul",
                      unidade_medida="CAIXA", valor_unitario=10.0, quantidade=5))
    db.commit()

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)

    assert len(r["itens"]) == 1
    assert r["itens"][0]["unidade_medida"] == "CAIXA"


def test_detalhe_unidade_medida_none_quando_nao_coletada():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Caneta esferográfica azul"))
    db.commit()

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)

    assert r["itens"][0]["unidade_medida"] is None
