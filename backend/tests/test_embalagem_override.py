"""
Checkbox "Preço do órgão já é da embalagem inteira (não dividir)" na aba
Itens/margem -- achado real reportado pelo usuário (edital 156310, item 9):
trocou o produto do item e o app dividiu o custo pela quantidade de itens
por unidade (embalagem) do produto, quando pra ESTE item o valor do órgão
já deveria ser comparado direto, sem dividir. A detecção automática
(unidadeMedida/descrição do PNCP, ver _custo_e_margem) não reconhece
embalagem em todo caso -- este override manual cobre o que sobra.

Fica em tabela própria (ItemEmbalagemOverride), via POST /api/editais/{id}/
itens/{numero}/embalagem -- mesmo motivo de CotacaoPreco (test_preco_cotacao.py):
Match.detalhe é reconstruído a cada recálculo e preserva só uma lista fixa
de campos, então um override guardado ali seria apagado em silêncio.

Rode com:  cd backend && pytest
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.main import (
    definir_embalagem_item, edital_detalhe, EmbalagemItemIn, _embalagem_override_por_numero,
)
from app.models import Base, Usuario, Edital, ItemEdital, Match, Produto, ItemEmbalagemOverride


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


def _edital_com_item(db, numero=9, valor_unitario=50.0):
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao="Papel A4 75g",
                      quantidade=10, valor_unitario=valor_unitario))
    db.commit()
    return ed


def test_definir_embalagem_item_cria_registro_quando_nao_existe():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)

    r = definir_embalagem_item(ed.id, 9, EmbalagemItemIn(sem_divisao=True), user=u, db=db)

    assert r == {"ok": True}
    o = db.execute(select(ItemEmbalagemOverride).where(ItemEmbalagemOverride.edital_id == ed.id)
                   .where(ItemEmbalagemOverride.usuario_id == u.id)).scalar_one()
    assert o.numero_item == 9
    assert o.sem_divisao is True


def test_definir_embalagem_item_atualiza_registro_existente():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)
    definir_embalagem_item(ed.id, 9, EmbalagemItemIn(sem_divisao=True), user=u, db=db)

    definir_embalagem_item(ed.id, 9, EmbalagemItemIn(sem_divisao=False), user=u, db=db)

    linhas = db.execute(select(ItemEmbalagemOverride).where(ItemEmbalagemOverride.edital_id == ed.id)
                        .where(ItemEmbalagemOverride.usuario_id == u.id)).scalars().all()
    assert len(linhas) == 1
    assert linhas[0].sem_divisao is False


def test_definir_embalagem_item_sobrevive_a_reconstrucao_do_match():
    """O bug que motivou a tabela própria: recalcular o Match (que
    reconstrói Match.detalhe do zero, preservando só uma lista fixa de
    campos) não pode apagar o override -- porque ele nem mora lá."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db)
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio",
                detalhe={"itens": [{"item": 9, "confianca": "media"}]}))
    db.commit()

    definir_embalagem_item(ed.id, 9, EmbalagemItemIn(sem_divisao=True), user=u, db=db)

    match = db.execute(select(Match).where(Match.edital_id == ed.id)).scalar_one()
    match.detalhe = {"itens": [{"item": 9, "confianca": "media"}]}
    db.commit()

    assert _embalagem_override_por_numero(ed.id, u, db) == {9: True}


def test_definir_embalagem_item_nao_mexe_em_outros_itens():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Item 1", quantidade=10, valor_unitario=50.0))
    db.add(ItemEdital(edital_id=ed.id, numero=2, descricao="Item 2", quantidade=5, valor_unitario=20.0))
    db.commit()

    definir_embalagem_item(ed.id, 1, EmbalagemItemIn(sem_divisao=True), user=u, db=db)

    por_item = _embalagem_override_por_numero(ed.id, u, db)
    assert por_item == {1: True}


def test_definir_embalagem_item_inexistente_no_edital_404():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, numero=9)

    with pytest.raises(Exception) as exc:
        definir_embalagem_item(ed.id, 999, EmbalagemItemIn(sem_divisao=True), user=u, db=db)
    assert getattr(exc.value, "status_code", None) == 404


def test_definir_embalagem_item_edital_inexistente_404():
    db = _sessao()
    u = _usuario(db)
    with pytest.raises(Exception) as exc:
        definir_embalagem_item(999, 1, EmbalagemItemIn(sem_divisao=True), user=u, db=db)
    assert getattr(exc.value, "status_code", None) == 404


def test_definir_embalagem_item_nao_vaza_entre_contas():
    db = _sessao()
    u1 = _usuario(db)
    u2 = Usuario(nome="Outro", email="outro@t.com", senha_hash="x")
    db.add(u2)
    db.commit()
    ed = _edital_com_item(db)

    definir_embalagem_item(ed.id, 9, EmbalagemItemIn(sem_divisao=True), user=u1, db=db)

    assert _embalagem_override_por_numero(ed.id, u2, db) == {}
    assert _embalagem_override_por_numero(ed.id, u1, db) == {9: True}


# --------- edital_detalhe reflete o override na margem --------- #

def test_edital_detalhe_aplica_override_e_para_de_dividir():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, numero=9, valor_unitario=24.50)
    prod = Produto(usuario_id=u.id, descricao="Papel A4 75g", preco_custo=25.0, itens_por_unidade=500)
    db.add(prod)
    db.commit()
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte", detalhe={
        "itens": [{"item": 9, "produto_id": prod.id, "confianca": "alta"}]
    }))
    db.commit()

    # antes do override: detecção automática não acha nenhum sinal de
    # embalagem (unidadeMedida/descrição ausentes) e divide em silêncio
    antes = edital_detalhe(ed.id, user=u, db=db)
    item_antes = next(i for i in antes["itens"] if i["numero"] == 9)
    assert item_antes["custo_comparavel"] == pytest.approx(25.0 / 500, abs=1e-4)
    assert item_antes["sem_divisao_embalagem"] is False

    definir_embalagem_item(ed.id, 9, EmbalagemItemIn(sem_divisao=True), user=u, db=db)

    depois = edital_detalhe(ed.id, user=u, db=db)
    item_depois = next(i for i in depois["itens"] if i["numero"] == 9)
    assert item_depois["custo_comparavel"] == 25.0
    assert item_depois["sem_divisao_embalagem"] is True
    assert item_depois["alerta_embalagem"] is False
