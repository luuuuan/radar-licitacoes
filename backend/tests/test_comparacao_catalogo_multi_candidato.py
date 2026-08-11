"""
Pedido do usuário: a comparação de catálogo por IA pode sugerir até 2
produtos genuinamente compatíveis pro mesmo item (não só 1) — o front
mostra um modal pra escolher qual vai pra cotação, com custo/margem dos
dois. Este teste cobre o enriquecimento em `_anexar_comparacao_catalogo_ia`
(app/main.py): cada candidato ganha seu próprio produto/custo/margem/
validação técnica, e um candidato cujo produto foi excluído do catálogo
entre a chamada da IA e agora é descartado sem derrubar os outros.
Rode com:  cd backend && pytest
"""
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import _anexar_comparacao_catalogo_ia
from app.models import Base, Usuario, Edital, ItemEdital, Produto


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _semear(db):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    p1 = Produto(usuario_id=u.id, descricao="Caneta azul BIC", preco_custo=1.0)
    p2 = Produto(usuario_id=u.id, descricao="Caneta azul Pilot", preco_custo=1.5)
    db.add_all([p1, p2])
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", uf="SP",
               objeto="Aquisicao de material de escritorio")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Caneta esferografica azul",
                      valor_unitario=3.0))
    db.commit()
    return u, ed, p1, p2


def test_dois_candidatos_ganham_produto_custo_e_margem_proprios():
    db = _sessao()
    u, ed, p1, p2 = _semear(db)
    resultado = {"status": "ok", "objeto": ed.objeto}
    with patch("app.analise_edital.comparar_catalogo_usuario") as mock_comparar:
        mock_comparar.return_value = {"status": "ok", "itens": [
            {"numero": 1, "candidatos": [
                {"produto_id": p1.id, "justificativa": "melhor opção"},
                {"produto_id": p2.id, "justificativa": "também compatível"},
            ]},
        ]}
        out = _anexar_comparacao_catalogo_ia(resultado, ed, u, db, "fake-key")

    itens = out["comparacao_catalogo_ia"]["itens"]
    assert len(itens) == 1
    candidatos = itens[0]["candidatos"]
    assert len(candidatos) == 2
    assert candidatos[0]["produto_id"] == p1.id
    assert candidatos[0]["produto"]["descricao"] == "Caneta azul BIC"
    assert candidatos[0]["margem"] == 2.0   # 3.0 (órgão) - 1.0 (custo)
    assert candidatos[1]["produto_id"] == p2.id
    assert candidatos[1]["margem"] == 1.5   # 3.0 - 1.5


def test_candidato_com_produto_excluido_do_catalogo_e_descartado_sem_derrubar_o_outro():
    db = _sessao()
    u, ed, p1, p2 = _semear(db)
    produto_excluido_id = p2.id + 999   # nunca existiu / foi apagado depois da chamada da IA
    resultado = {"status": "ok", "objeto": ed.objeto}
    with patch("app.analise_edital.comparar_catalogo_usuario") as mock_comparar:
        mock_comparar.return_value = {"status": "ok", "itens": [
            {"numero": 1, "candidatos": [
                {"produto_id": p1.id, "justificativa": "ok"},
                {"produto_id": produto_excluido_id, "justificativa": "produto que já sumiu"},
            ]},
        ]}
        out = _anexar_comparacao_catalogo_ia(resultado, ed, u, db, "fake-key")

    itens = out["comparacao_catalogo_ia"]["itens"]
    assert len(itens) == 1
    assert len(itens[0]["candidatos"]) == 1
    assert itens[0]["candidatos"][0]["produto_id"] == p1.id


def test_item_sem_nenhum_candidato_valido_fica_de_fora():
    db = _sessao()
    u, ed, p1, p2 = _semear(db)
    resultado = {"status": "ok", "objeto": ed.objeto}
    with patch("app.analise_edital.comparar_catalogo_usuario") as mock_comparar:
        mock_comparar.return_value = {"status": "ok", "itens": [
            {"numero": 1, "candidatos": [{"produto_id": p1.id + 999, "justificativa": "x"}]},
        ]}
        out = _anexar_comparacao_catalogo_ia(resultado, ed, u, db, "fake-key")

    assert out["comparacao_catalogo_ia"]["itens"] == []
