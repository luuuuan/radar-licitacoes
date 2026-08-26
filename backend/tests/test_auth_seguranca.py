"""
Achados da auditoria do agente debugger em app/auth.py e app/main.py:

1. Login vazava timing -- e-mail inexistente respondia mais rápido que
   e-mail certo com senha errada, porque `if not u or not conferir_senha(...)`
   nunca chamava bcrypt quando o usuário não existe (curto-circuito do "or").
   Dava pra enumerar e-mails cadastrados só pelo tempo de resposta.
2. Link de verificação de e-mail nunca expirava (diferente do link de
   redefinir senha, que expira em 1h) -- adicionado expira em 24h + um jeito
   de pedir um novo (POST /api/auth/reenviar-verificacao), senão expirar sem
   ter como pedir de novo seria pior que não expirar.

Banco sqlite em memória, sem HTTP. Rode com:  cd backend && pytest
"""
from datetime import timedelta

import pytest
from fastapi import BackgroundTasks, HTTPException, Response
from starlette.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth as _auth
from app.main import (
    auth_login, auth_verificar, auth_reenviar_verificacao,
    LoginIn, ReenviarVerificacaoIn, _utcnow_main,
)
from app.models import Base, Usuario


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _req(ip="1.2.3.4"):
    return Request({"type": "http", "headers": [], "client": (ip, 12345)})


def _usuario(db, email="teste@teste.com", senha="Senha123!", **kw):
    u = Usuario(nome="Teste", email=email, senha_hash=_auth.hash_senha(senha), **kw)
    db.add(u)
    db.commit()
    return u


# --------- timing side-channel no login --------- #

def test_login_chama_conferir_senha_mesmo_com_email_inexistente(monkeypatch):
    """Sem isso, e-mail desconhecido nunca chamava bcrypt (curto-circuito do
    "or"), respondendo mensuravelmente mais rápido que um e-mail certo com
    senha errada -- dava pra enumerar contas pelo tempo de resposta."""
    db = _sessao()
    chamadas = []
    original = _auth.conferir_senha
    monkeypatch.setattr(_auth, "conferir_senha",
                        lambda senha, hash_: chamadas.append(hash_) or original(senha, hash_))

    with pytest.raises(HTTPException) as exc:
        auth_login(LoginIn(email="nao-existe@teste.com", senha="qualquer"), _req(), Response(), db)

    assert exc.value.status_code == 401
    assert len(chamadas) == 1
    assert chamadas[0] == _auth.HASH_FANTASMA


def test_login_com_email_existente_confere_contra_o_hash_de_verdade(monkeypatch):
    db = _sessao()
    u = _usuario(db, email="real@teste.com", senha="SenhaCerta1!")
    chamadas = []
    original = _auth.conferir_senha
    monkeypatch.setattr(_auth, "conferir_senha",
                        lambda senha, hash_: chamadas.append(hash_) or original(senha, hash_))

    with pytest.raises(HTTPException):
        auth_login(LoginIn(email="real@teste.com", senha="errada"), _req(), Response(), db)

    assert chamadas == [u.senha_hash]


def test_hash_fantasma_e_um_hash_bcrypt_valido_mas_de_senha_nenhuma():
    assert _auth.conferir_senha("qualquer coisa", _auth.HASH_FANTASMA) is False


# --------- expiração do link de verificação de e-mail --------- #

def test_verificar_com_token_expirado_da_400_com_mensagem_de_expirado():
    db = _sessao()
    u = _usuario(db, email_verificado=False)
    u.token_verificacao = "tok-expirado"
    u.token_verificacao_expira = _utcnow_main() - timedelta(hours=1)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        auth_verificar(token="tok-expirado", db=db)
    assert exc.value.status_code == 400
    assert "expirado" in exc.value.detail.lower()
    # o token expirado não pode ter verificado o e-mail
    db.refresh(u)
    assert u.email_verificado is False


def test_verificar_com_token_valido_dentro_do_prazo_funciona():
    db = _sessao()
    u = _usuario(db, email_verificado=False)
    u.token_verificacao = "tok-valido"
    u.token_verificacao_expira = _utcnow_main() + timedelta(hours=1)
    db.commit()

    r = auth_verificar(token="tok-valido", db=db)

    assert r == {"ok": True}
    db.refresh(u)
    assert u.email_verificado is True
    assert u.token_verificacao is None
    assert u.token_verificacao_expira is None


def test_verificar_sem_data_de_expiracao_continua_funcionando():
    """Contas antigas (de antes desse campo existir) têm token_verificacao_
    expira=None -- não pode virar "sempre expirado" retroativamente."""
    db = _sessao()
    u = _usuario(db, email_verificado=False)
    u.token_verificacao = "tok-sem-expiracao"
    db.commit()

    r = auth_verificar(token="tok-sem-expiracao", db=db)

    assert r == {"ok": True}


def test_reenviar_verificacao_gera_novo_token_e_reenvia(monkeypatch):
    db = _sessao()
    u = _usuario(db, email="pendente@teste.com", email_verificado=False)
    u.token_verificacao = "token-antigo"
    u.token_verificacao_expira = _utcnow_main() - timedelta(hours=1)   # já expirado
    db.commit()
    token_antigo = u.token_verificacao

    monkeypatch.setattr("app.main._email_mod.smtp_configurado", lambda: True)
    enviados = []
    monkeypatch.setattr("app.main._email_mod.enviar_para",
                        lambda *a, **k: enviados.append((a, k)))

    r = auth_reenviar_verificacao(ReenviarVerificacaoIn(email="pendente@teste.com"),
                                  _req(), BackgroundTasks(), db)

    assert r["ok"] is True
    db.refresh(u)
    assert u.token_verificacao is not None
    assert u.token_verificacao != token_antigo
    assert u.token_verificacao_expira > _utcnow_main()


def test_reenviar_verificacao_conta_ja_verificada_nao_gera_token_novo(monkeypatch):
    db = _sessao()
    u = _usuario(db, email="ja-verificado@teste.com", email_verificado=True)
    db.commit()
    monkeypatch.setattr("app.main._email_mod.smtp_configurado", lambda: True)

    r = auth_reenviar_verificacao(ReenviarVerificacaoIn(email="ja-verificado@teste.com"),
                                  _req(), BackgroundTasks(), db)

    assert r["ok"] is True   # resposta genérica, não denuncia que já tava verificado
    db.refresh(u)
    assert u.token_verificacao is None


def test_reenviar_verificacao_email_inexistente_nao_quebra(monkeypatch):
    db = _sessao()
    monkeypatch.setattr("app.main._email_mod.smtp_configurado", lambda: True)
    r = auth_reenviar_verificacao(ReenviarVerificacaoIn(email="nao-existe@teste.com"),
                                  _req(), BackgroundTasks(), db)
    assert r["ok"] is True
