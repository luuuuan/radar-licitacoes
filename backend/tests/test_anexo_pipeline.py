"""
Testes do anexo de documento no card do Pipeline -- pedido do usuário:
diferente do Cofre de Documentos (documentos de habilitação da EMPRESA,
reusados em todo edital), este é por EDITAL (proposta assinada, recibo de
entrega, certidão pedida na hora...). v2 (ajuste pedido pelo usuário depois
de ver o protótipo): VÁRIOS anexos por edital, não mais 1 só substituindo o
anterior -- o card mostra um botão "Ver anexos (N)" que abre um modal com a
lista completa, cada um com baixar/remover. Mesmo padrão de teste do Cofre
(ver test_documentos.py): chama as funções das rotas direto (sem HTTP); foco
em isolamento entre usuários. Rode com:  cd backend && pytest
"""
import asyncio
import io

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from app import auth
from app.main import (anexar_arquivo_pipeline, listar_anexos_pipeline, remover_anexo_pipeline,
                      baixar_anexo_pipeline, pipeline)
from app.models import Base, Usuario, Edital, Match, AnexoPipeline


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


def _edital(db, id_externo="ed1", orgao="Orgao Teste"):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao=orgao, objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    return ed


def _upload(conteudo=b"conteudo qualquer", nome="proposta.pdf", tipo="application/pdf"):
    return UploadFile(io.BytesIO(conteudo), filename=nome, headers=Headers({"content-type": tipo}))


def _anexar(db, ed, user, **kwargs):
    kwargs.setdefault("arquivo", _upload())
    return asyncio.run(anexar_arquivo_pipeline(ed.id, user=user, db=db, **kwargs))


# --------------------------- Upload (vários por edital) ------------------ #

def test_anexar_cria_registro_quando_nao_existe():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)

    r = _anexar(db, ed, u, arquivo=_upload(nome="Proposta_PE_022.pdf"))

    assert r["nome_original"] == "Proposta_PE_022.pdf"
    assert r["tamanho"] == len(b"conteudo qualquer")
    assert "id" in r and "criado_em" in r
    a = db.execute(select(AnexoPipeline).where(AnexoPipeline.edital_id == ed.id)).scalar_one()
    assert a.usuario_id == u.id
    assert a.nome_original == "Proposta_PE_022.pdf"


def test_anexar_de_novo_acumula_em_vez_de_substituir():
    """Achado real (usuário pediu depois de ver o protótipo): v1 substituía
    o anexo anterior; agora cada upload ADICIONA um novo -- o modal "Ver
    anexos" é o lugar certo pra ter vários (proposta + recibo + certidão)."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    _anexar(db, ed, u, arquivo=_upload(b"versao 1", nome="v1.pdf"))

    _anexar(db, ed, u, arquivo=_upload(b"versao 2", nome="v2.pdf"))

    total = db.scalar(select(func.count(AnexoPipeline.id)).where(AnexoPipeline.edital_id == ed.id))
    assert total == 2   # acumulou, não substituiu
    nomes = {a.nome_original for a in db.execute(
        select(AnexoPipeline).where(AnexoPipeline.edital_id == ed.id)).scalars()}
    assert nomes == {"v1.pdf", "v2.pdf"}


def test_anexar_edital_inexistente_404():
    db = _sessao()
    u = _usuario(db)
    with pytest.raises(HTTPException) as exc:
        _anexar(db, type("F", (), {"id": 99999})(), u)
    assert exc.value.status_code == 404


def test_anexar_rejeita_tipo_nao_permitido():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    with pytest.raises(HTTPException) as exc:
        _anexar(db, ed, u, arquivo=_upload(nome="virus.exe", tipo="application/x-msdownload"))
    assert exc.value.status_code == 400


def test_anexar_aceita_docx_e_xlsx():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)

    r1 = _anexar(db, ed, u, arquivo=_upload(
        nome="proposta.docx", tipo="application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    r2 = _anexar(db, ed, u, arquivo=_upload(
        nome="cotacao.xlsx", tipo="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))

    assert r1["nome_original"] == "proposta.docx"
    assert r2["nome_original"] == "cotacao.xlsx"


def test_anexar_rejeita_arquivo_maior_que_15mb():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    grande = b"x" * (15 * 1024 * 1024 + 1)
    with pytest.raises(HTTPException) as exc:
        _anexar(db, ed, u, arquivo=_upload(grande))
    assert exc.value.status_code == 400
    assert "grande" in exc.value.detail.lower()


# --------------------------- Listagem (modal "Ver anexos") ------------------ #

def test_listar_anexos_devolve_todos_do_edital_em_ordem_de_criacao():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    _anexar(db, ed, u, arquivo=_upload(nome="primeiro.pdf"))
    _anexar(db, ed, u, arquivo=_upload(nome="segundo.pdf"))

    r = listar_anexos_pipeline(ed.id, user=u, db=db)

    nomes = [a["nome_original"] for a in r["anexos"]]
    assert nomes == ["primeiro.pdf", "segundo.pdf"]


def test_listar_anexos_vazio_quando_nenhum():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    assert listar_anexos_pipeline(ed.id, user=u, db=db) == {"anexos": []}


def test_listar_anexos_nao_mostra_de_outro_usuario():
    db = _sessao()
    dono = _usuario(db, "dono@t.com")
    invasor = _usuario(db, "invasor@t.com")
    ed = _edital(db)
    _anexar(db, ed, dono)

    assert listar_anexos_pipeline(ed.id, user=invasor, db=db) == {"anexos": []}


# --------------------------- Isolamento entre usuários ------------------ #

def test_usuario_nao_baixa_anexo_de_outro_usuario():
    db = _sessao()
    dono = _usuario(db, "dono@t.com")
    invasor = _usuario(db, "invasor@t.com")
    ed = _edital(db)
    criado = _anexar(db, ed, dono, arquivo=_upload(b"segredo do dono"))

    with pytest.raises(HTTPException) as exc:
        baixar_anexo_pipeline(ed.id, criado["id"], user=invasor, db=db)
    assert exc.value.status_code == 404

    resp = baixar_anexo_pipeline(ed.id, criado["id"], user=dono, db=db)
    assert resp.body == b"segredo do dono"


def test_baixar_anexo_id_de_outro_edital_404():
    """O id do anexo é global -- bate a URL inteira (edital_id + anexo_id),
    não só o anexo_id, senão um link de outro edital "funcionaria" aqui."""
    db = _sessao()
    u = _usuario(db)
    ed1 = _edital(db, "ed1")
    ed2 = _edital(db, "ed2")
    criado = _anexar(db, ed1, u)

    with pytest.raises(HTTPException) as exc:
        baixar_anexo_pipeline(ed2.id, criado["id"], user=u, db=db)
    assert exc.value.status_code == 404


def test_usuario_nao_remove_anexo_de_outro_usuario():
    db = _sessao()
    dono = _usuario(db, "dono@t.com")
    invasor = _usuario(db, "invasor@t.com")
    ed = _edital(db)
    criado = _anexar(db, ed, dono)

    remover_anexo_pipeline(ed.id, criado["id"], user=invasor, db=db)   # não levanta erro, só não acha nada do invasor

    a = db.get(AnexoPipeline, criado["id"])
    assert a is not None and a.usuario_id == dono.id   # continua lá, intacto


def test_dois_usuarios_podem_anexar_no_mesmo_edital_sem_conflito():
    db = _sessao()
    u1 = _usuario(db, "u1@t.com")
    u2 = _usuario(db, "u2@t.com")
    ed = _edital(db)
    c1 = _anexar(db, ed, u1, arquivo=_upload(b"arquivo do u1", nome="u1.pdf"))
    c2 = _anexar(db, ed, u2, arquivo=_upload(b"arquivo do u2", nome="u2.pdf"))

    assert baixar_anexo_pipeline(ed.id, c1["id"], user=u1, db=db).body == b"arquivo do u1"
    assert baixar_anexo_pipeline(ed.id, c2["id"], user=u2, db=db).body == b"arquivo do u2"
    assert len(listar_anexos_pipeline(ed.id, user=u1, db=db)["anexos"]) == 1
    assert len(listar_anexos_pipeline(ed.id, user=u2, db=db)["anexos"]) == 1


def test_baixar_anexo_com_falha_de_decifra_da_erro_claro_em_vez_de_500_cru(monkeypatch):
    """Mesmo achado real já corrigido pro Cofre de Documentos (auditoria de
    segurança): decifrar devolvendo None não pode virar TypeError cru."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    criado = _anexar(db, ed, u)
    monkeypatch.setattr(auth, "decifrar", lambda token: None)
    with pytest.raises(HTTPException) as exc:
        baixar_anexo_pipeline(ed.id, criado["id"], user=u, db=db)
    assert exc.value.status_code == 500


# --------------------------- Remoção ------------------ #

def test_remover_anexo_apaga_so_aquele_sem_afetar_os_outros():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    c1 = _anexar(db, ed, u, arquivo=_upload(nome="fica.pdf"))
    c2 = _anexar(db, ed, u, arquivo=_upload(nome="remove.pdf"))

    remover_anexo_pipeline(ed.id, c2["id"], user=u, db=db)

    restantes = listar_anexos_pipeline(ed.id, user=u, db=db)["anexos"]
    assert len(restantes) == 1
    assert restantes[0]["nome_original"] == "fica.pdf"
    assert db.get(AnexoPipeline, c1["id"]) is not None


def test_remover_anexo_inexistente_nao_da_erro():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    r = remover_anexo_pipeline(ed.id, 99999, user=u, db=db)   # nunca existiu -- não pode quebrar
    assert r == {"ok": True}


# --------------------------- GET /api/pipeline expõe a contagem ------------------ #

def test_pipeline_expoe_contagem_de_anexos_do_card():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, orgao="Orgao Com Anexo")
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte", status="vou_participar"))
    db.commit()
    _anexar(db, ed, u, arquivo=_upload(nome="Proposta.pdf"))
    _anexar(db, ed, u, arquivo=_upload(nome="Recibo.pdf"))

    r = pipeline(user=u, db=db)

    card = r["colunas"]["vou_participar"][0]
    assert card["anexos_count"] == 2


def test_pipeline_contagem_zero_quando_nao_tem_anexo():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte", status="vou_participar"))
    db.commit()

    r = pipeline(user=u, db=db)

    assert r["colunas"]["vou_participar"][0]["anexos_count"] == 0


def test_pipeline_nao_mistura_contagem_de_anexos_entre_usuarios():
    db = _sessao()
    u1 = _usuario(db, "u1@t.com")
    u2 = _usuario(db, "u2@t.com")
    ed = _edital(db)
    db.add(Match(usuario_id=u1.id, edital_id=ed.id, score=0.9, nivel="forte", status="vou_participar"))
    db.add(Match(usuario_id=u2.id, edital_id=ed.id, score=0.9, nivel="forte", status="vou_participar"))
    db.commit()
    _anexar(db, ed, u1, arquivo=_upload(nome="do_u1.pdf"))

    r2 = pipeline(user=u2, db=db)

    assert r2["colunas"]["vou_participar"][0]["anexos_count"] == 0
