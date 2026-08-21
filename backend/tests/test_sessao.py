"""
Sessão expira por dois motivos independentes (app/auth.py): inatividade
(TOKEN_IDLE_HORAS, renovada a cada requisição autenticada) e um teto
absoluto (TOKEN_EXPIRA_HORAS, contado a partir do login e nunca esticado).
Banco sqlite em memória, sem HTTP. Rode com:  cd backend && pytest
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth
from app.config import settings
from app.models import Base, Usuario


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db, ativo=True, verificado=True):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x",
               ativo=ativo, email_verificado=verificado)
    db.add(u)
    db.commit()
    return u


def _req_com_cookie(token: str | None) -> Request:
    headers = [(b"cookie", f"{auth.COOKIE_NOME}={token}".encode())] if token else []
    return Request({"type": "http", "headers": headers, "client": ("1.2.3.4", 1)})


def _cookie_da_resposta(resp: Response) -> str | None:
    for nome, valor in resp.raw_headers:
        if nome == b"set-cookie":
            return valor.decode()
    return None


def _token_do_cookie(cabecalho_set_cookie: str) -> str:
    # "radar_sessao=<token>; HttpOnly; ..." -- só precisamos do valor
    trecho = cabecalho_set_cookie.split(";")[0]
    return trecho.split("=", 1)[1]


# --------- auth.criar_token / _payload_do_token --------- #

def test_criar_token_login_novo_abre_teto_absoluto_de_7_dias():
    antes = datetime.now(timezone.utc)
    token = auth.criar_token(1)
    payload = auth._payload_do_token(token)
    exp_abs = datetime.fromtimestamp(payload["exp_abs"], tz=timezone.utc)
    esperado = antes + timedelta(hours=settings.TOKEN_EXPIRA_HORAS)
    assert abs((exp_abs - esperado).total_seconds()) < 5


def test_criar_token_login_novo_expira_por_inatividade_em_2h():
    antes = datetime.now(timezone.utc)
    token = auth.criar_token(1)
    payload = auth._payload_do_token(token)
    exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    esperado = antes + timedelta(hours=settings.TOKEN_IDLE_HORAS)
    assert abs((exp - esperado).total_seconds()) < 5


def test_renovacao_preserva_o_teto_absoluto_original():
    """O ponto central do desenho: renovar por atividade não deve esticar o
    teto de 7 dias -- só a janela de inatividade desliza."""
    token1 = auth.criar_token(1)
    exp_abs_original = auth._payload_do_token(token1)["exp_abs"]

    token2 = auth.criar_token(1, exp_abs=exp_abs_original)
    payload2 = auth._payload_do_token(token2)

    assert payload2["exp_abs"] == exp_abs_original


def test_renovacao_nao_ultrapassa_o_teto_quando_perto_do_limite():
    """Faltando menos que a janela de inatividade pro teto acabar, o "exp"
    do token renovado cai no teto, não em now+idle -- a sessão não desliza
    pra além do que o teto absoluto permite."""
    daqui_a_10min = (datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp()
    token = auth.criar_token(1, exp_abs=daqui_a_10min)
    payload = auth._payload_do_token(token)
    assert payload["exp"] == pytest.approx(daqui_a_10min, abs=1)


def test_payload_do_token_invalido_retorna_none():
    assert auth._payload_do_token("lixo-nao-e-jwt") is None


def test_payload_do_token_expirado_retorna_none():
    passado = datetime.now(timezone.utc) - timedelta(hours=1)
    token = jwt.encode({"sub": "1", "exp": passado, "exp_abs": passado.timestamp()},
                       settings.SECRET_KEY, algorithm="HS256")
    assert auth._payload_do_token(token) is None


# --------- auth.get_current_user (renovação de cookie) --------- #

def test_get_current_user_renova_o_cookie_mantendo_o_teto():
    db = _sessao()
    u = _usuario(db)
    token_original = auth.criar_token(u.id)
    exp_abs_original = auth._payload_do_token(token_original)["exp_abs"]

    resp = Response()
    resultado = auth.get_current_user(_req_com_cookie(token_original), resp, db)

    assert resultado.id == u.id
    novo_cookie = _cookie_da_resposta(resp)
    assert novo_cookie is not None
    novo_payload = auth._payload_do_token(_token_do_cookie(novo_cookie))
    assert novo_payload["exp_abs"] == exp_abs_original


def test_get_current_user_sem_cookie_da_401():
    db = _sessao()
    with pytest.raises(HTTPException) as exc:
        auth.get_current_user(_req_com_cookie(None), Response(), db)
    assert exc.value.status_code == 401


def test_get_current_user_com_token_expirado_da_401():
    db = _sessao()
    u = _usuario(db)
    passado = datetime.now(timezone.utc) - timedelta(minutes=1)
    token = jwt.encode({"sub": str(u.id), "exp": passado, "exp_abs": passado.timestamp()},
                       settings.SECRET_KEY, algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        auth.get_current_user(_req_com_cookie(token), Response(), db)
    assert exc.value.status_code == 401


def test_get_current_user_token_sem_exp_abs_ainda_autentica_essa_chamada():
    """Sessão aberta antes desse campo existir (migração) -- a chamada atual
    continua valendo (o "exp" original do token ainda está no futuro); só a
    renovação passa a não esticar mais o teto (ver auth._renovar_cookie_se_preciso)."""
    db = _sessao()
    u = _usuario(db)
    futuro = datetime.now(timezone.utc) + timedelta(days=3)
    token_antigo_formato = jwt.encode({"sub": str(u.id), "exp": futuro},
                                      settings.SECRET_KEY, algorithm="HS256")
    resultado = auth.get_current_user(_req_com_cookie(token_antigo_formato), Response(), db)
    assert resultado.id == u.id


def test_get_current_user_usuario_inativo_da_401():
    db = _sessao()
    u = _usuario(db, ativo=False)
    token = auth.criar_token(u.id)
    with pytest.raises(HTTPException) as exc:
        auth.get_current_user(_req_com_cookie(token), Response(), db)
    assert exc.value.status_code == 401


def test_get_current_user_email_nao_verificado_da_403():
    db = _sessao()
    u = _usuario(db, verificado=False)
    token = auth.criar_token(u.id)
    with pytest.raises(HTTPException) as exc:
        auth.get_current_user(_req_com_cookie(token), Response(), db)
    assert exc.value.status_code == 403
