"""
Autenticação e segurança (multiusuário).

- Senhas: hash com bcrypt (nunca em texto puro).
- Sessão: JWT assinado com SECRET_KEY, entregue em cookie httpOnly.
- Dados sensíveis (chave Gemini, CPF/CNPJ, endereço): cifrados com Fernet
  (AES) a partir de uma chave derivada de APP_ENCRYPTION_KEY/SECRET_KEY.
- Força de senha: regra mínima validada no cadastro.
"""
from __future__ import annotations
import base64
import hashlib
from functools import lru_cache
import re
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_session
from .models import Usuario

_ALG = "HS256"
COOKIE_NOME = "radar_sessao"


# ---------------- Senha ----------------
def hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def conferir_senha(senha: str, hash_: str) -> bool:
    try:
        return bcrypt.checkpw(senha.encode("utf-8"), hash_.encode("utf-8"))
    except (ValueError, AttributeError):
        return False


def validar_forca_senha(senha: str) -> str | None:
    """Retorna uma mensagem de erro se a senha for fraca, ou None se for forte."""
    if len(senha) < 8:
        return "A senha deve ter pelo menos 8 caracteres."
    if not re.search(r"[A-Za-z]", senha):
        return "A senha deve conter ao menos uma letra."
    if not re.search(r"\d", senha):
        return "A senha deve conter ao menos um número."
    if not re.search(r"[^A-Za-z0-9]", senha):
        return "A senha deve conter ao menos um símbolo (ex.: !@#$%)."
    return None


# ---------------- Criptografia de dados sensíveis ----------------
@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    base = settings.APP_ENCRYPTION_KEY or settings.SECRET_KEY
    chave = base64.urlsafe_b64encode(hashlib.sha256(base.encode()).digest())
    return Fernet(chave)


def cifrar(texto: str | None) -> str | None:
    if not texto:
        return None
    return _fernet().encrypt(texto.encode("utf-8")).decode("utf-8")


def decifrar(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


# ---------------- Token de sessão (JWT) ----------------
# A sessão expira por dois motivos independentes: inatividade (TOKEN_IDLE_HORAS
# -- "exp" do token, renovado a cada requisição autenticada, ver
# _renovar_cookie_se_preciso) e um teto absoluto (TOKEN_EXPIRA_HORAS, contado
# a partir do login e nunca esticado -- "exp_abs", carregado sem mudança de
# token em token). Sem o teto absoluto, um usuário ativo todo dia nunca
# precisaria logar de novo; sem a inatividade, uma aba esquecida aberta num
# computador compartilhado continuaria valendo por até 7 dias.
def criar_token(usuario_id: int, exp_abs: float | None = None) -> str:
    agora = datetime.now(timezone.utc)
    if exp_abs is None:
        exp_abs = (agora + timedelta(hours=settings.TOKEN_EXPIRA_HORAS)).timestamp()
    exp = min(agora + timedelta(hours=settings.TOKEN_IDLE_HORAS),
             datetime.fromtimestamp(exp_abs, tz=timezone.utc))
    payload = {"sub": str(usuario_id), "exp": exp, "exp_abs": exp_abs}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_ALG)


def _payload_do_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[_ALG])
    except (jwt.PyJWTError, ValueError, TypeError):
        return None


def cookie_sessao(resp: Response, usuario_id: int, exp_abs: float | None = None):
    token = criar_token(usuario_id, exp_abs)
    seguro = settings.APP_BASE_URL.startswith("https")
    resp.set_cookie(COOKIE_NOME, token, httponly=True, samesite="lax",
                    secure=seguro, max_age=settings.TOKEN_IDLE_HORAS * 3600,
                    path="/")


def _renovar_cookie_se_preciso(resp: Response, uid: int, payload: dict):
    """Reemite o cookie a cada requisição autenticada, deslizando a janela
    de inatividade por mais TOKEN_IDLE_HORAS -- sem nunca passar do teto
    absoluto "exp_abs" (carregado como veio, não recalculado)."""
    exp_abs = payload.get("exp_abs")
    if not isinstance(exp_abs, (int, float)):
        # token antigo, de antes desse campo existir (sessão aberta num
        # deploy anterior) -- não estica indevidamente sob a regra nova:
        # trata o teto como se fosse agora, então essa é a última renovação
        # que vale (a próxima chamada já vem com exp no passado e pede
        # login de novo). A requisição atual, em si, continua valendo --
        # o "exp" original do token, validado logo acima por jwt.decode,
        # ainda estava no futuro.
        exp_abs = datetime.now(timezone.utc).timestamp()
    cookie_sessao(resp, uid, exp_abs)


# ---------------- Dependências ----------------
def get_current_user(request: Request, response: Response,
                     db: Session = Depends(get_session)) -> Usuario:
    """Usuário logado (via cookie de sessão). 401 se não autenticado;
    403 se o e-mail ainda não foi verificado. Renova a sessão por
    inatividade a cada chamada (ver _renovar_cookie_se_preciso)."""
    token = request.cookies.get(COOKIE_NOME)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    payload = _payload_do_token(token) if token else None
    uid = int(payload["sub"]) if payload else None
    if not uid:
        raise HTTPException(401, "Não autenticado")
    user = db.get(Usuario, uid)
    if not user or not user.ativo:
        raise HTTPException(401, "Sessão inválida")
    if not user.email_verificado:
        raise HTTPException(403, "E-mail ainda não verificado")
    _renovar_cookie_se_preciso(response, uid, payload)
    return user
