"""
Testes de POST /api/produtos/importar casando pela coluna "id" (quando
presente) em vez de só pela descrição. Achado real: reimportar o catálogo
exportado depois de corrigir/renomear uma descrição criava um produto NOVO
em vez de atualizar o original -- a descrição antiga não batia com nada.
Rode com:  cd backend && pytest
"""
import asyncio
import io

import openpyxl
from fastapi import UploadFile
from starlette.datastructures import Headers
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from app.main import importar_produtos
from app.models import Base, Usuario, Produto


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db, email="empresa@t.com"):
    u = Usuario(nome="Empresa Teste", email=email, senha_hash="x")
    db.add(u)
    db.commit()
    return u


def _upload_planilha(linhas, cabec=("id", "descricao", "preco_custo", "preco_venda")):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(cabec))
    for linha in linhas:
        ws.append(list(linha))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return UploadFile(buf, filename="produtos.xlsx", headers=Headers({
        "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}))


def test_importa_por_id_atualiza_mesmo_com_descricao_diferente():
    db = _sessao()
    u = _usuario(db)
    p = Produto(usuario_id=u.id, descricao="Papel A4 75g", preco_custo=10.0)
    db.add(p)
    db.commit()

    arquivo = _upload_planilha([(p.id, "Papel A4 75g branco (corrigido)", "12,50", "20,00")])
    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))

    assert resultado == {"status": "ok", "criados": 0, "atualizados": 1, "ignorados": 0, "erros": []}
    total = db.scalar(select(func.count(Produto.id)).where(Produto.usuario_id == u.id))
    assert total == 1   # não duplicou
    db.refresh(p)
    assert p.descricao == "Papel A4 75g branco (corrigido)"
    assert p.preco_custo == 12.5


def test_importa_por_id_inexistente_nao_cria_duplicado_e_reporta_erro():
    db = _sessao()
    u = _usuario(db)
    arquivo = _upload_planilha([(99999, "Produto qualquer", "10,00", "20,00")])
    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))

    assert resultado["criados"] == 0
    assert resultado["atualizados"] == 0
    assert resultado["ignorados"] == 1
    assert "id de produto '99999' não encontrado" in resultado["erros"][0]
    assert db.scalar(select(func.count(Produto.id))) == 0


def test_importa_por_id_de_outro_usuario_nao_atualiza():
    db = _sessao()
    u1 = _usuario(db, "u1@t.com")
    u2 = _usuario(db, "u2@t.com")
    alheio = Produto(usuario_id=u2.id, descricao="Produto do outro usuário", preco_custo=1.0)
    db.add(alheio)
    db.commit()

    arquivo = _upload_planilha([(alheio.id, "Tentando sobrescrever", "999,00", "999,00")])
    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u1, db=db))

    assert resultado["atualizados"] == 0
    assert resultado["ignorados"] == 1
    db.refresh(alheio)
    assert alheio.descricao == "Produto do outro usuário"   # não foi tocado
    assert alheio.preco_custo == 1.0


def test_importa_por_id_com_formato_invalido_reporta_erro():
    db = _sessao()
    u = _usuario(db)
    arquivo = _upload_planilha([("abc", "Produto qualquer", "10,00", "20,00")])
    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))

    assert resultado["criados"] == 0
    assert resultado["ignorados"] == 1
    assert "id de produto 'abc' inválido" in resultado["erros"][0]


def test_importa_sem_coluna_id_continua_casando_pela_descricao():
    """Planilha externa (ex.: lista de fornecedor) nunca vai ter a coluna
    id -- comportamento de sempre (casar pela descrição) continua intacto."""
    db = _sessao()
    u = _usuario(db)
    db.add(Produto(usuario_id=u.id, descricao="Produto Existente", preco_custo=5.0))
    db.commit()

    arquivo = _upload_planilha(
        [("Produto Existente", "99,00", "150,00"), ("Produto Novo", "1,00", "2,00")],
        cabec=("descricao", "preco_custo", "preco_venda"),
    )
    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))

    assert resultado["criados"] == 1
    assert resultado["atualizados"] == 1
    assert resultado["erros"] == []


def test_importa_com_coluna_id_mas_linha_em_branco_casa_pela_descricao():
    """Dentro de uma planilha com a coluna id (ex.: catálogo exportado e
    editado), uma linha NOVA adicionada pelo usuário fica sem id -- tem que
    continuar caindo no fluxo normal de criar/casar por descrição, não ser
    tratada como erro."""
    db = _sessao()
    u = _usuario(db)
    p = Produto(usuario_id=u.id, descricao="Produto Existente", preco_custo=5.0)
    db.add(p)
    db.commit()

    arquivo = _upload_planilha([
        (p.id, "Produto Existente", "99,00", "150,00"),
        ("", "Produto Novo Adicionado na Planilha", "1,00", "2,00"),
    ])
    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))

    assert resultado["atualizados"] == 1
    assert resultado["criados"] == 1
    assert resultado["erros"] == []
