"""
Achado real (auditoria de segurança pedida pelo usuário, item 1 -- o único
vazamento de dado ENTRE usuários confirmado na auditoria): ProdutoIn.
fornecedor_id é um campo livre do corpo da requisição, e criar_produto/
atualizar_produto gravavam esse valor sem checar que o Fornecedor é do
próprio usuário. Daí exportar_produtos usava esse fornecedor_id pra buscar
o telefone sem filtrar por usuario_id -- um usuário conseguia, com a
própria conta, descobrir o telefone de um fornecedor de OUTRO tenant só
varrendo ids sequenciais (PUT produto apontando pro id + exportar.xlsx).

Rode com:  cd backend && pytest
"""
from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import criar_produto, atualizar_produto, exportar_produtos, ProdutoIn
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


def _produto_in(**over) -> ProdutoIn:
    dados = {"descricao": "Papel A4"}
    dados.update(over)
    return ProdutoIn(**dados)


def test_criar_produto_com_fornecedor_de_outro_usuario_da_404():
    db = _sessao()
    u1 = _usuario(db, email="u1@t.com")
    u2 = _usuario(db, email="u2@t.com")
    f_de_outro = Fornecedor(usuario_id=u2.id, nome="Fornecedor do usuário 2", telefone="(45) 99999-0000")
    db.add(f_de_outro)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        criar_produto(_produto_in(fornecedor_id=f_de_outro.id), user=u1, db=db)
    assert exc.value.status_code == 404
    assert db.query(Produto).count() == 0   # produto não foi criado


def test_criar_produto_com_fornecedor_proprio_funciona():
    db = _sessao()
    u = _usuario(db)
    f = Fornecedor(usuario_id=u.id, nome="Fornecedor do usuário")
    db.add(f)
    db.commit()

    r = criar_produto(_produto_in(fornecedor_id=f.id), user=u, db=db)
    p = db.get(Produto, r["id"])
    assert p.fornecedor_id == f.id


def test_criar_produto_sem_fornecedor_funciona():
    db = _sessao()
    u = _usuario(db)
    r = criar_produto(_produto_in(), user=u, db=db)
    assert db.get(Produto, r["id"]).fornecedor_id is None


def test_atualizar_produto_para_fornecedor_de_outro_usuario_da_404():
    db = _sessao()
    u1 = _usuario(db, email="u1@t.com")
    u2 = _usuario(db, email="u2@t.com")
    p = Produto(usuario_id=u1.id, descricao="Papel A4")
    f_de_outro = Fornecedor(usuario_id=u2.id, nome="Fornecedor do usuário 2")
    db.add_all([p, f_de_outro])
    db.commit()

    with pytest.raises(HTTPException) as exc:
        atualizar_produto(p.id, _produto_in(fornecedor_id=f_de_outro.id), user=u1, db=db)
    assert exc.value.status_code == 404
    db.refresh(p)
    assert p.fornecedor_id is None   # não foi alterado


def test_exportar_produtos_nao_vaza_telefone_de_fornecedor_de_outro_usuario():
    """Defesa em profundidade: mesmo que um fornecedor_id estranho exista
    (dado legado, ou qualquer caminho de escrita futuro que esqueça de
    chamar _validar_fornecedor_id), o export não pode devolver o telefone
    de um fornecedor que não é do usuário que está exportando."""
    db = _sessao()
    u1 = _usuario(db, email="u1@t.com")
    u2 = _usuario(db, email="u2@t.com")
    f_de_outro = Fornecedor(usuario_id=u2.id, nome="Fornecedor do usuário 2", telefone="(45) 98888-1111")
    db.add(f_de_outro)
    db.commit()
    # simula o estado que o IDOR produzia antes do fix (inserido direto no
    # banco, contornando a validação que agora existe na rota)
    p = Produto(usuario_id=u1.id, descricao="Papel A4", fornecedor_id=f_de_outro.id)
    db.add(p)
    db.commit()

    resposta = exportar_produtos(user=u1, db=db)
    # StreamingResponse expõe o conteúdo como async generator (mesmo
    # passando um iter([...]) síncrono -- o Starlette embrulha em
    # iterate_in_threadpool); drena com asyncio pra reabrir como arquivo.
    import asyncio
    import io
    import openpyxl

    async def _drenar(gen):
        return b"".join([chunk async for chunk in gen])

    conteudo = asyncio.run(_drenar(resposta.body_iterator))
    wb = openpyxl.load_workbook(io.BytesIO(conteudo))
    ws = wb.active
    telefones_na_planilha = [row[15] for row in ws.iter_rows(min_row=2, values_only=True)]
    assert "(45) 98888-1111" not in telefones_na_planilha
