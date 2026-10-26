"""
Achado real (auditoria do agente code-reviewer): o try/except por linha em
importar_produtos (POST /api/produtos/importar) não pegava erro de banco de
verdade -- db.add()/setattr() só enfileiram a mudança, o INSERT/UPDATE real
só acontecia no autoflush da PRÓXIMA linha (fora do try) ou no commit final.
Uma única linha malformada derrubava a importação INTEIRA (nada commitado,
nem as linhas boas), em vez de reportar só aquela linha e manter o resto.
Rode com:  cd backend && pytest
"""
import asyncio
import io
import zipfile

import openpyxl
import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.main import importar_produtos, _ler_upload_limitado
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


def _flush_com_erro_para(db, descricao_ruim):
    """Fake flush que só levanta erro quando o objeto Produto PENDENTE nesta
    sessão (novo ou modificado) tem a descrição alvo -- não conta chamadas
    de autoflush disparadas por outras queries (ex.: a busca de fornecedor
    no início da função) que não têm nada a ver com a linha problemática."""
    original = db.flush

    def _fake(*a, **kw):
        for obj in list(db.new) + list(db.dirty):
            if isinstance(obj, Produto) and obj.descricao == descricao_ruim:
                raise RuntimeError("erro de banco simulado")
        return original(*a, **kw)

    return _fake


def _upload_planilha(linhas):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["descricao", "preco_custo", "preco_venda"])
    for linha in linhas:
        ws.append(list(linha))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return UploadFile(buf, filename="produtos.xlsx", headers=Headers({
        "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}))


def _upload_planilha_com_link(linhas):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["descricao", "preco_custo", "link_produto"])
    for linha in linhas:
        ws.append(list(linha))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return UploadFile(buf, filename="produtos.xlsx", headers=Headers({
        "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}))


def test_importar_grava_link_produto():
    db = _sessao()
    u = _usuario(db)
    arquivo = _upload_planilha_com_link([
        ("Papel A4", "10,00", "https://fornecedor.com/produto/a4"),
    ])
    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))
    assert resultado["criados"] == 1
    produto = db.execute(select(Produto).where(Produto.descricao == "Papel A4")).scalar_one()
    assert produto.link_produto == "https://fornecedor.com/produto/a4"


def test_importar_cria_produtos_normalmente():
    db = _sessao()
    u = _usuario(db)
    arquivo = _upload_planilha([("Papel A4", "10,00", "20,00"), ("Caneta Azul", "1,00", "2,50")])
    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))
    assert resultado["criados"] == 2
    assert resultado["erros"] == []


def test_linha_com_erro_de_banco_nao_derruba_as_outras_linhas_da_planilha():
    """O achado principal: força um erro real de banco (via monkeypatch no
    flush) só na 2ª linha -- as linhas 1ª e 3ª têm que continuar criadas e
    COMMITADAS, com só a 2ª reportada em "erros"."""
    db = _sessao()
    u = _usuario(db)
    arquivo = _upload_planilha([
        ("Produto A", "10,00", "20,00"),
        ("Produto Ruim", "10,00", "20,00"),
        ("Produto C", "10,00", "20,00"),
    ])

    flush_original = db.flush
    db.flush = _flush_com_erro_para(db, "Produto Ruim")

    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))

    assert resultado["criados"] == 2
    assert len(resultado["erros"]) == 1
    assert "linha 3" in resultado["erros"][0]   # linha 2 é o cabeçalho -> "Produto Ruim" é a linha 3

    db.flush = flush_original
    nomes = set(db.execute(select(Produto.descricao)).scalars().all())
    assert nomes == {"Produto A", "Produto C"}


def test_atualizar_produto_existente_com_erro_nao_derruba_as_outras_linhas():
    db = _sessao()
    u = _usuario(db)
    db.add(Produto(usuario_id=u.id, descricao="Produto Existente", preco_custo=5.0))
    db.commit()

    arquivo = _upload_planilha([
        ("Produto Novo", "10,00", "20,00"),
        ("Produto Existente", "99,00", "150,00"),   # vai atualizar
    ])

    flush_original = db.flush
    db.flush = _flush_com_erro_para(db, "Produto Novo")

    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))

    assert resultado["criados"] == 0
    assert resultado["atualizados"] == 1
    assert len(resultado["erros"]) == 1

    db.flush = flush_original
    existente = db.execute(select(Produto).where(Produto.descricao == "Produto Existente")).scalar_one()
    assert existente.preco_custo == 99.0


# ---------------------------------------------------------------------------
# Achados reais (auditoria de segurança pedida pelo usuário): o tamanho do
# upload só era checado DEPOIS de ler o arquivo inteiro pra memória, e nada
# protegia contra "zip bomb" (um .xlsx é um zip -- um arquivo pequeno pode
# anunciar um conteúdo descompactado gigante).
# ---------------------------------------------------------------------------

def test_ler_upload_limitado_aceita_dentro_do_limite():
    conteudo = b"x" * 100
    arquivo = UploadFile(io.BytesIO(conteudo), filename="pequeno.bin")
    lido = asyncio.run(_ler_upload_limitado(arquivo, limite_bytes=1000, mensagem_erro="grande demais"))
    assert lido == conteudo


def test_ler_upload_limitado_rejeita_acima_do_limite():
    conteudo = b"x" * 2000
    arquivo = UploadFile(io.BytesIO(conteudo), filename="grande.bin")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_ler_upload_limitado(arquivo, limite_bytes=1000, mensagem_erro="grande demais"))
    assert exc.value.status_code == 400
    assert exc.value.detail == "grande demais"


def test_ler_upload_limitado_nao_le_alem_do_necessario_pra_detectar_excesso():
    """Lê em blocos -- não precisa consumir o arquivo inteiro antes de
    perceber que já passou do limite (a defesa real contra alocar um
    upload gigante por completo antes de rejeitar)."""
    conteudo = b"x" * (50 * 1024 * 1024)   # 50 MB
    arquivo = UploadFile(io.BytesIO(conteudo), filename="grande.bin")
    with pytest.raises(HTTPException):
        asyncio.run(_ler_upload_limitado(arquivo, limite_bytes=1024, mensagem_erro="grande demais"))
    # só devia ter lido 1 bloco (1 MB) antes de already passar do limite de 1 KB
    assert arquivo.file.tell() <= 1024 * 1024


def test_importar_produtos_rejeita_planilha_maior_que_o_limite():
    db = _sessao()
    u = _usuario(db)
    conteudo_grande = b"0" * (11 * 1024 * 1024)   # 11 MB > limite de 10 MB
    arquivo = UploadFile(io.BytesIO(conteudo_grande), filename="grande.xlsx")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))
    assert exc.value.status_code == 400
    assert "grande" in exc.value.detail.lower()


def test_importar_produtos_rejeita_zip_bomb():
    """Arquivo pequeno (cabe no limite de 10 MB comprimido) que anuncia um
    conteúdo descompactado bem maior que _TAMANHO_MAX_DESCOMPACTADO_PLANILHA
    -- rejeitado antes de o openpyxl chegar a abrir."""
    db = _sessao()
    u = _usuario(db)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"0" * (250 * 1024 * 1024))
    conteudo = buf.getvalue()
    assert len(conteudo) < 1 * 1024 * 1024   # comprime pra bem menos que 1 MB
    arquivo = UploadFile(io.BytesIO(conteudo), filename="bomba.xlsx")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))
    assert exc.value.status_code == 400
