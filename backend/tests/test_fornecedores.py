"""
Testes de Fornecedores: contagem de produtos vinculados e favoritar
(pedido do usuário -- "relatório de fornecedor: quantos produtos,
favoritar"). Sem HTTP, chama as rotas direto. Rode com:
cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import listar_fornecedores, alternar_favorito_fornecedor
from app.models import Base, Usuario, Fornecedor, Produto


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db, **over):
    u = Usuario(nome="Teste", email=over.pop("email", "t@t.com"), senha_hash="x")
    db.add(u)
    db.commit()
    return u


def test_lista_conta_produtos_vinculados_por_fornecedor():
    db = _sessao()
    u = _usuario(db)
    f1 = Fornecedor(usuario_id=u.id, nome="Fornecedor A")
    f2 = Fornecedor(usuario_id=u.id, nome="Fornecedor B")
    db.add_all([f1, f2])
    db.commit()
    db.add_all([
        Produto(usuario_id=u.id, descricao="Item 1", fornecedor_id=f1.id),
        Produto(usuario_id=u.id, descricao="Item 2", fornecedor_id=f1.id),
        Produto(usuario_id=u.id, descricao="Item 3", fornecedor_id=f2.id),
    ])
    db.commit()

    r = listar_fornecedores(user=u, db=db)
    por_nome = {f["nome"]: f for f in r}
    assert por_nome["Fornecedor A"]["produtos_count"] == 2
    assert por_nome["Fornecedor B"]["produtos_count"] == 1


def test_fornecedor_sem_produto_conta_zero():
    db = _sessao()
    u = _usuario(db)
    db.add(Fornecedor(usuario_id=u.id, nome="Sem produtos"))
    db.commit()
    r = listar_fornecedores(user=u, db=db)
    assert r[0]["produtos_count"] == 0


def test_produto_inativo_nao_conta():
    db = _sessao()
    u = _usuario(db)
    f = Fornecedor(usuario_id=u.id, nome="Fornecedor A")
    db.add(f)
    db.commit()
    db.add(Produto(usuario_id=u.id, descricao="Item removido", fornecedor_id=f.id, ativo=False))
    db.commit()
    r = listar_fornecedores(user=u, db=db)
    assert r[0]["produtos_count"] == 0


def test_produto_de_outro_usuario_nao_conta():
    """Isolamento entre usuários: dois fornecedores com o mesmo id
    numérico (bancos diferentes por usuário não existem aqui, mas o
    filtro por usuario_id no produto tem que valer mesmo assim)."""
    db = _sessao()
    u1 = _usuario(db, email="u1@t.com")
    u2 = _usuario(db, email="u2@t.com")
    f1 = Fornecedor(usuario_id=u1.id, nome="Fornecedor do usuário 1")
    db.add(f1)
    db.commit()
    # produto de outro usuário não pode contar pro fornecedor do usuário 1
    db.add(Produto(usuario_id=u2.id, descricao="Item de outro usuário", fornecedor_id=f1.id))
    db.commit()
    r = listar_fornecedores(user=u1, db=db)
    assert r[0]["produtos_count"] == 0


def test_favoritos_aparecem_primeiro():
    db = _sessao()
    u = _usuario(db)
    db.add_all([
        Fornecedor(usuario_id=u.id, nome="Zebra Papéis", favorito=False),
        Fornecedor(usuario_id=u.id, nome="Alfa Suprimentos", favorito=True),
    ])
    db.commit()
    r = listar_fornecedores(user=u, db=db)
    assert r[0]["nome"] == "Alfa Suprimentos"   # favorito primeiro, mesmo vindo depois no alfabeto
    assert r[0]["favorito"] is True
    assert r[1]["favorito"] is False


def test_alternar_favorito_liga_e_desliga():
    db = _sessao()
    u = _usuario(db)
    f = Fornecedor(usuario_id=u.id, nome="Fornecedor A")
    db.add(f)
    db.commit()

    r1 = alternar_favorito_fornecedor(f.id, user=u, db=db)
    assert r1["favorito"] is True
    db.refresh(f)
    assert f.favorito is True

    r2 = alternar_favorito_fornecedor(f.id, user=u, db=db)
    assert r2["favorito"] is False


def test_alternar_favorito_de_fornecedor_de_outro_usuario_da_404():
    from fastapi import HTTPException
    import pytest
    db = _sessao()
    u1 = _usuario(db, email="u1@t.com")
    u2 = _usuario(db, email="u2@t.com")
    f = Fornecedor(usuario_id=u1.id, nome="Fornecedor do usuário 1")
    db.add(f)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        alternar_favorito_fornecedor(f.id, user=u2, db=db)
    assert exc.value.status_code == 404
