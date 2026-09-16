"""
Pedido do usuário: o "Valor unit. (venda)" da aba Cotação serve só pra
ACOMPANHAR o pregão (planilha exportada, "até quanto posso ofertar") --
não deve mudar nem ser mudado pelo valor de verdade da Proposta (antes,
os dois eram literalmente o mesmo campo, Proposta.itens[].preco_unit).

Fica em tabela própria (CotacaoPreco), via POST /api/editais/{id}/itens/
{numero}/preco-cotacao -- NÃO em Match.detalhe: achado real (auditoria do
agente architect-reviewer) é que Match é reconstruído a cada POST
/api/recalcular por _mesclar_confirmacoes_manuais (service.py), que só
preserva uma lista fixa de campos -- um preco_cotacao guardado ali seria
apagado em silêncio no próximo recálculo. Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.main import definir_preco_cotacao, edital_detalhe, PrecoCotacaoIn, _preco_cotacao_por_numero
from app.models import Base, Usuario, Edital, ItemEdital, Match, Produto, Proposta, CotacaoPreco


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


def _edital_com_item(db, numero=1):
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao="Papel A4 75g",
                      quantidade=10, valor_unitario=50.0))
    db.commit()
    return ed


def test_definir_preco_cotacao_cria_registro_quando_nao_existe():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)

    r = definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=80.0), user=u, db=db)

    assert r == {"ok": True}
    cp = db.execute(select(CotacaoPreco).where(CotacaoPreco.edital_id == ed.id)
                    .where(CotacaoPreco.usuario_id == u.id)).scalar_one()
    assert cp.numero_item == 1
    assert cp.valor == 80.0


def test_definir_preco_cotacao_atualiza_registro_existente():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)
    definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=80.0), user=u, db=db)

    definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=99.0), user=u, db=db)

    linhas = db.execute(select(CotacaoPreco).where(CotacaoPreco.edital_id == ed.id)
                        .where(CotacaoPreco.usuario_id == u.id)).scalars().all()
    assert len(linhas) == 1
    assert linhas[0].valor == 99.0


def test_definir_preco_cotacao_nao_mexe_em_confirmacao_de_produto_no_match():
    """Editar o preço de acompanhamento não pode derrubar uma confirmação
    de produto já feita em Match.detalhe (nem o contrário) -- são dois
    registros totalmente independentes agora."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)
    prod = Produto(usuario_id=u.id, descricao="Papel A4")
    db.add(prod)
    db.commit()
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte", detalhe={
        "itens": [{"item": 1, "produto_id": prod.id, "confianca": "alta", "confirmado_manualmente": True}]
    }))
    db.commit()

    definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=80.0), user=u, db=db)

    match = db.execute(select(Match).where(Match.edital_id == ed.id)).scalar_one()
    item = match.detalhe["itens"][0]
    assert item["produto_id"] == prod.id
    assert item["confirmado_manualmente"] is True
    assert "preco_cotacao" not in item


def test_definir_preco_cotacao_sobrevive_a_reconstrucao_do_match():
    """O bug que motivou a mudança: recalcular o Match (que reconstrói
    Match.detalhe do zero, preservando só um whitelist de campos) não pode
    apagar o preço de acompanhamento -- porque ele nem mora lá."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio",
                detalhe={"itens": [{"item": 1, "confianca": "media"}]}))
    db.commit()

    definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=80.0), user=u, db=db)

    # simula o que _mesclar_confirmacoes_manuais faz num recálculo:
    # reconstrói Match.detalhe do zero, sem nenhum preco_cotacao (ele nunca
    # existiu lá pra começo de conversa, na versão nova).
    match = db.execute(select(Match).where(Match.edital_id == ed.id)).scalar_one()
    match.detalhe = {"itens": [{"item": 1, "confianca": "media"}]}
    db.commit()

    precos = _preco_cotacao_por_numero(ed.id, u, db)
    assert precos == {1: 80.0}


def test_definir_preco_cotacao_nao_mexe_em_outros_itens():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Item 1", quantidade=10, valor_unitario=50.0))
    db.add(ItemEdital(edital_id=ed.id, numero=2, descricao="Item 2", quantidade=5, valor_unitario=20.0))
    db.commit()
    definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=10.0), user=u, db=db)
    definir_preco_cotacao(ed.id, 2, PrecoCotacaoIn(valor=20.0), user=u, db=db)

    definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=99.0), user=u, db=db)

    por_item = _preco_cotacao_por_numero(ed.id, u, db)
    assert por_item == {1: 99.0, 2: 20.0}


def test_definir_preco_cotacao_item_inexistente_no_edital_404():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, numero=1)

    try:
        definir_preco_cotacao(ed.id, 999, PrecoCotacaoIn(valor=10.0), user=u, db=db)
        assert False, "deveria ter levantado 404"
    except Exception as e:
        assert getattr(e, "status_code", None) == 404


def test_definir_preco_cotacao_edital_inexistente_404():
    db = _sessao()
    u = _usuario(db)
    try:
        definir_preco_cotacao(999, 1, PrecoCotacaoIn(valor=10.0), user=u, db=db)
        assert False, "deveria ter levantado 404"
    except Exception as e:
        assert getattr(e, "status_code", None) == 404


def test_definir_preco_cotacao_e_por_usuario_nao_vaza_entre_contas():
    db = _sessao()
    u1 = _usuario(db)
    u2 = Usuario(nome="Outro", email="outro@t.com", senha_hash="x")
    db.add(u2)
    db.commit()
    ed = _edital_com_item(db)

    definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=80.0), user=u1, db=db)

    assert _preco_cotacao_por_numero(ed.id, u2, db) == {}
    assert _preco_cotacao_por_numero(ed.id, u1, db) == {1: 80.0}


# --------- edital_detalhe expõe preco_cotacao --------- #

def test_edital_detalhe_inclui_preco_cotacao():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)
    definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=42.0), user=u, db=db)

    r = edital_detalhe(ed.id, user=u, db=db)

    assert r["itens"][0]["preco_cotacao"] == 42.0


def test_edital_detalhe_preco_cotacao_none_quando_nao_definido():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)

    r = edital_detalhe(ed.id, user=u, db=db)

    assert r["itens"][0]["preco_cotacao"] is None


def test_edital_detalhe_preco_cotacao_cai_pro_preco_unit_da_proposta_por_continuidade():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)
    db.add(Proposta(edital_id=ed.id, usuario_id=u.id, itens=[
        {"numero": 1, "descricao": "Papel A4 75g", "quantidade": 10, "custo_unit": 0, "preco_unit": 65.0},
    ]))
    db.commit()

    r = edital_detalhe(ed.id, user=u, db=db)

    assert r["itens"][0]["preco_cotacao"] == 65.0


# --------- _preco_cotacao_por_numero (função pura) --------- #

def test_preco_cotacao_por_numero_prioriza_registro_proprio_sobre_proposta():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)
    definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=30.0), user=u, db=db)
    db.add(Proposta(edital_id=ed.id, usuario_id=u.id, itens=[
        {"numero": 1, "descricao": "x", "quantidade": 10, "custo_unit": 0, "preco_unit": 999.0},
    ]))
    db.commit()

    resultado = _preco_cotacao_por_numero(ed.id, u, db)

    assert resultado == {1: 30.0}


def test_preco_cotacao_por_numero_fallback_da_proposta_e_write_through():
    """O fallback pro preco_unit da Proposta não pode ser uma cópia AO VIVO
    pra sempre -- assim que usado, tem que virar um valor próprio,
    persistido, senão editar a Proposta depois continuaria mudando o valor
    da Cotação por baixo dos panos (o próprio bug que motivou a
    separação)."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)
    db.add(Proposta(edital_id=ed.id, usuario_id=u.id, itens=[
        {"numero": 1, "descricao": "x", "quantidade": 10, "custo_unit": 0, "preco_unit": 65.0},
    ]))
    db.commit()

    resultado = _preco_cotacao_por_numero(ed.id, u, db)
    assert resultado == {1: 65.0}

    # muda a Proposta DEPOIS -- não pode mais afetar o valor da Cotação,
    # porque o fallback já virou um registro próprio na 1ª leitura acima.
    prop = db.execute(select(Proposta).where(Proposta.edital_id == ed.id)).scalar_one()
    prop.itens = [{"numero": 1, "descricao": "x", "quantidade": 10, "custo_unit": 0, "preco_unit": 999.0}]
    db.commit()

    assert _preco_cotacao_por_numero(ed.id, u, db) == {1: 65.0}
    cp = db.execute(select(CotacaoPreco).where(CotacaoPreco.edital_id == ed.id)
                    .where(CotacaoPreco.usuario_id == u.id)).scalar_one()
    assert cp.numero_item == 1
    assert cp.valor == 65.0
