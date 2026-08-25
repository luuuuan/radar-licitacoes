"""
Testes do cofre de documentos de habilitação (/api/documentos) — chama as
funções das rotas direto (sem HTTP), mesmo padrão dos outros testes de
main.py (ver test_exportar_proposta_pdf.py). Foco principal: isolamento
entre usuários — usuário A não pode baixar, editar nem excluir documento
de usuário B (404, não o arquivo) — e a extração de validade por IA no
cadastro (só isso: sem julgamento de apto/inapto). Rode com:
cd backend && pytest
"""
import asyncio
import io
from datetime import date

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import analise_edital
from app import auth
from app.main import (criar_documento, atualizar_documento, remover_documento,
                      baixar_arquivo_documento, listar_documentos)
from app.models import Base, Usuario, Documento


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


def _upload(conteudo=b"conteudo qualquer do certificado", nome="certidao.pdf",
           tipo="application/pdf"):
    return UploadFile(io.BytesIO(conteudo), filename=nome, headers=Headers({"content-type": tipo}))


def _criar(db, user, **kwargs):
    kwargs.setdefault("nome", "CND Federal")
    kwargs.setdefault("orgao_emissor", None)
    kwargs.setdefault("data_validade", date(2099, 1, 1))
    kwargs.setdefault("link", None)
    kwargs.setdefault("observacao", None)
    # chamada direta (sem HTTP) não passa pela resolução de Form() do FastAPI
    # -- sem isso explícito, o parâmetro fica com o objeto Form(False) em vez
    # do bool, que é truthy e quebraria a lógica de "sem_validade".
    kwargs.setdefault("sem_validade", False)
    kwargs.setdefault("arquivo", _upload())
    return asyncio.run(criar_documento(user=user, db=db, **kwargs))


# --------------------------- Isolamento entre usuários ------------------ #

def test_usuario_nao_baixa_arquivo_de_documento_de_outro_usuario():
    db = _sessao()
    dono = _usuario(db, "dono@t.com")
    invasor = _usuario(db, "invasor@t.com")
    criado = _criar(db, dono, arquivo=_upload(b"segredo do dono"))

    with pytest.raises(HTTPException) as exc:
        baixar_arquivo_documento(criado["id"], user=invasor, db=db)
    assert exc.value.status_code == 404

    # o dono continua conseguindo baixar o próprio arquivo, com o conteúdo certo
    resp = baixar_arquivo_documento(criado["id"], user=dono, db=db)
    assert resp.body == b"segredo do dono"


def test_usuario_nao_edita_documento_de_outro_usuario():
    db = _sessao()
    dono = _usuario(db, "dono2@t.com")
    invasor = _usuario(db, "invasor2@t.com")
    criado = _criar(db, dono, nome="CND Federal")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(atualizar_documento(
            criado["id"], nome="Nome trocado pelo invasor", orgao_emissor=None,
            data_validade=date(2030, 1, 1), link=None, observacao=None, sem_validade=False, arquivo=None,
            user=invasor, db=db))
    assert exc.value.status_code == 404

    # nada mudou no documento do dono
    d = db.get(Documento, criado["id"])
    assert d.nome == "CND Federal"


def test_usuario_nao_exclui_documento_de_outro_usuario():
    db = _sessao()
    dono = _usuario(db, "dono3@t.com")
    invasor = _usuario(db, "invasor3@t.com")
    criado = _criar(db, dono)

    with pytest.raises(HTTPException) as exc:
        remover_documento(criado["id"], user=invasor, db=db)
    assert exc.value.status_code == 404
    assert db.get(Documento, criado["id"]) is not None   # continua existindo


def test_listar_documentos_so_traz_os_do_proprio_usuario():
    db = _sessao()
    u1 = _usuario(db, "u1@t.com")
    u2 = _usuario(db, "u2@t.com")
    _criar(db, u1, nome="Doc do u1 - A")
    _criar(db, u1, nome="Doc do u1 - B")
    _criar(db, u2, nome="Doc do u2")

    docs_u2 = listar_documentos(user=u2, db=db)
    assert [d["nome"] for d in docs_u2] == ["Doc do u2"]


def test_baixar_documento_inexistente_da_404_sem_vazar_diferenca():
    db = _sessao()
    u = _usuario(db)
    with pytest.raises(HTTPException) as exc:
        baixar_arquivo_documento(99999, user=u, db=db)
    assert exc.value.status_code == 404


# --------------------------- Upload obrigatório / arquivo cifrado ------- #

def test_criar_documento_sem_arquivo_e_rejeitado():
    db = _sessao()
    u = _usuario(db)
    with pytest.raises(HTTPException) as exc:
        _criar(db, u, arquivo=None)
    assert exc.value.status_code == 400


def test_arquivo_fica_cifrado_no_banco_nao_em_texto_puro():
    db = _sessao()
    u = _usuario(db)
    criado = _criar(db, u, arquivo=_upload(b"dado sensivel do certificado"))
    d = db.get(Documento, criado["id"])
    assert d.arquivo_cifrado is not None
    assert b"dado sensivel do certificado" not in d.arquivo_cifrado.encode()
    # mas decifra de volta pro conteúdo original
    assert auth.decifrar(d.arquivo_cifrado) is not None


def test_editar_documento_sem_novo_arquivo_mantem_o_arquivo_anterior():
    db = _sessao()
    u = _usuario(db)
    criado = _criar(db, u, arquivo=_upload(b"arquivo original"))
    asyncio.run(atualizar_documento(
        criado["id"], nome="CND Federal (renovada)", orgao_emissor=None,
        data_validade=date(2030, 1, 1), link=None, observacao=None, sem_validade=False, arquivo=None,
        user=u, db=db))
    resp = baixar_arquivo_documento(criado["id"], user=u, db=db)
    assert resp.body == b"arquivo original"


def test_editar_documento_com_novo_arquivo_substitui_o_anterior():
    db = _sessao()
    u = _usuario(db)
    criado = _criar(db, u, arquivo=_upload(b"arquivo velho"))
    asyncio.run(atualizar_documento(
        criado["id"], nome="CND Federal", orgao_emissor=None,
        data_validade=date(2030, 1, 1), link=None, observacao=None, sem_validade=False,
        arquivo=_upload(b"arquivo novo"), user=u, db=db))
    resp = baixar_arquivo_documento(criado["id"], user=u, db=db)
    assert resp.body == b"arquivo novo"


# --------------------------- Extração de validade por IA ---------------- #

def test_criar_sem_validade_usa_ia_pra_extrair(monkeypatch):
    monkeypatch.setattr(analise_edital, "extrair_texto_upload", lambda *a, **k: "texto do certificado")
    monkeypatch.setattr(analise_edital, "extrair_validade_documento",
                        lambda texto, api_key=None: date(2026, 3, 1))
    db = _sessao()
    u = _usuario(db)
    u.gemini_key_cifrada = auth.cifrar("chave-fake")
    db.commit()

    criado = _criar(db, u, data_validade=None)
    assert criado["data_validade"] == "2026-03-01"
    d = db.get(Documento, criado["id"])
    assert d.data_validade == date(2026, 3, 1)


def test_criar_sem_validade_e_sem_chave_ia_pede_manual():
    """Sem chave Gemini configurada, a extração automática não roda -- não
    inventa data nenhuma, devolve 422 pedindo pra digitar manualmente."""
    db = _sessao()
    u = _usuario(db)   # sem gemini_key_cifrada
    with pytest.raises(HTTPException) as exc:
        _criar(db, u, data_validade=None)
    assert exc.value.status_code == 422


def test_criar_sem_validade_ia_nao_identifica_pede_manual(monkeypatch):
    monkeypatch.setattr(analise_edital, "extrair_texto_upload", lambda *a, **k: "texto sem data nenhuma")
    monkeypatch.setattr(analise_edital, "extrair_validade_documento",
                        lambda texto, api_key=None: None)
    db = _sessao()
    u = _usuario(db)
    u.gemini_key_cifrada = auth.cifrar("chave-fake")
    db.commit()

    with pytest.raises(HTTPException) as exc:
        _criar(db, u, data_validade=None)
    assert exc.value.status_code == 422


def test_criar_com_sem_validade_nao_chama_ia_nem_pede_data(monkeypatch):
    """Documento sem vencimento (ex.: contrato social) -- não tenta extrair
    validade por IA nem exige data nenhuma, mesmo sem chave Gemini."""
    chamou = []
    monkeypatch.setattr(analise_edital, "extrair_texto_upload", lambda *a, **k: "texto")
    monkeypatch.setattr(analise_edital, "extrair_validade_documento",
                        lambda *a, **k: chamou.append(1) or date(2020, 1, 1))
    db = _sessao()
    u = _usuario(db)   # sem gemini_key_cifrada -- não pode ser o motivo de funcionar

    criado = _criar(db, u, data_validade=None, sem_validade=True)
    assert criado["data_validade"] is None
    assert chamou == []
    d = db.get(Documento, criado["id"])
    assert d.data_validade is None


def test_criar_com_sem_validade_ignora_data_digitada_junto():
    """Se por algum motivo vier data E sem_validade=true no mesmo request,
    sem_validade vence -- o documento fica mesmo sem vencimento."""
    db = _sessao()
    u = _usuario(db)
    criado = _criar(db, u, data_validade=date(2030, 1, 1), sem_validade=True)
    assert criado["data_validade"] is None


def test_editar_com_sem_validade_zera_a_data():
    db = _sessao()
    u = _usuario(db)
    criado = _criar(db, u, data_validade=date(2030, 1, 1))
    asyncio.run(atualizar_documento(
        criado["id"], nome="CND Federal", orgao_emissor=None,
        data_validade=None, link=None, observacao=None, sem_validade=True,
        arquivo=None, user=u, db=db))
    d = db.get(Documento, criado["id"])
    assert d.data_validade is None


def test_editar_sem_data_e_sem_marcar_sem_validade_da_erro():
    db = _sessao()
    u = _usuario(db)
    criado = _criar(db, u)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(atualizar_documento(
            criado["id"], nome="CND Federal", orgao_emissor=None,
            data_validade=None, link=None, observacao=None, sem_validade=False,
            arquivo=None, user=u, db=db))
    assert exc.value.status_code == 400


def test_listar_documentos_sem_validade_nao_quebra_e_retorna_none():
    db = _sessao()
    u = _usuario(db)
    _criar(db, u, data_validade=None, sem_validade=True)
    docs = listar_documentos(user=u, db=db)
    assert docs[0]["data_validade"] is None
    assert docs[0]["dias_para_vencer"] is None


def test_criar_com_validade_digitada_nao_chama_ia(monkeypatch):
    """Se o usuário já digitou a validade, a extração por IA nem é
    chamada -- evita gasto de IA desnecessário."""
    chamou = []
    monkeypatch.setattr(analise_edital, "extrair_texto_upload", lambda *a, **k: "texto")
    monkeypatch.setattr(analise_edital, "extrair_validade_documento",
                        lambda *a, **k: chamou.append(1) or date(2020, 1, 1))
    db = _sessao()
    u = _usuario(db)

    criado = _criar(db, u, data_validade=date(2028, 5, 20))
    assert criado["data_validade"] == "2028-05-20"
    assert chamou == []
