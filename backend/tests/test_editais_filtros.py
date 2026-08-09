"""
Testes dos filtros de faixa de valor (valor_min/valor_max) e busca por item
(busca_item) em GET /api/editais. Banco sqlite em memória, sem HTTP — chama
a função da rota diretamente com os parâmetros já resolvidos (o jeito que o
FastAPI resolveria via Query(...), sem depender da injeção de dependência).
Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import listar_editais
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


def _edital_com_match(db, usuario, id_externo, valor_estimado=None, itens=None):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste",
                objeto="Aquisicao", uf="SP", valor_estimado=valor_estimado)
    db.add(ed)
    db.commit()
    for numero, descricao in enumerate(itens or [], start=1):
        db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao=descricao))
    db.add(Match(usuario_id=usuario.id, edital_id=ed.id, score=0.5, nivel="medio"))
    db.commit()
    return ed


def _listar(db, user, **kwargs):
    padrao = dict(nivel=None, uf=None, status=None, vista="ativos",
                  apenas_nao_lidos=False, apenas_interessantes=False, hoje=False,
                  tipo="todos", valor_min=None, valor_max=None, busca_item=None,
                  pagina=1, por_pagina=50)
    padrao.update(kwargs)
    return listar_editais(user=user, db=db, **padrao)


def test_valor_min_exclui_editais_abaixo_do_piso():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", valor_estimado=1000.0)
    _edital_com_match(db, u, "ed2", valor_estimado=50000.0)

    r = _listar(db, u, valor_min=10000)
    assert r["total"] == 1
    assert r["resultados"][0]["valor_estimado"] == 50000.0


def test_valor_max_exclui_editais_acima_do_teto():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", valor_estimado=1000.0)
    _edital_com_match(db, u, "ed2", valor_estimado=50000.0)

    r = _listar(db, u, valor_max=10000)
    assert r["total"] == 1
    assert r["resultados"][0]["valor_estimado"] == 1000.0


def test_faixa_de_valor_combinada_min_e_max():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", valor_estimado=1000.0)
    _edital_com_match(db, u, "ed2", valor_estimado=25000.0)
    _edital_com_match(db, u, "ed3", valor_estimado=90000.0)

    r = _listar(db, u, valor_min=10000, valor_max=50000)
    assert r["total"] == 1
    assert r["resultados"][0]["valor_estimado"] == 25000.0


def test_edital_sem_valor_estimado_fica_de_fora_quando_ha_filtro_de_valor():
    """Edital sem valor cadastrado não pode ser confirmado como "dentro da
    faixa" — exclui em vez de mostrar sem checagem nenhuma."""
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", valor_estimado=None)

    r = _listar(db, u, valor_min=100)
    assert r["total"] == 0


def test_busca_item_so_mostra_editais_que_pedem_o_item_buscado():
    db = _sessao()
    u = _usuario(db)
    ed1 = _edital_com_match(db, u, "ed1", itens=["Grampeador de mesa 26/6", "Papel A4"])
    _edital_com_match(db, u, "ed2", itens=["Caneta esferografica azul"])

    r = _listar(db, u, busca_item="grampeador")
    assert r["total"] == 1
    assert r["resultados"][0]["edital_id"] == ed1.id


def test_busca_item_case_insensitive():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", itens=["GRAMPEADOR METAL 20 FOLHAS"])

    r = _listar(db, u, busca_item="grampeador")
    assert r["total"] == 1


def test_busca_item_sem_resultado_quando_nenhum_item_bate():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", itens=["Papel A4"])

    r = _listar(db, u, busca_item="grampeador")
    assert r["total"] == 0


def test_busca_item_em_branco_nao_filtra_nada():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", itens=["Papel A4"])

    r = _listar(db, u, busca_item="   ")
    assert r["total"] == 1
