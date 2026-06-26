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
from fastapi import Depends, HTTPException, Request
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
def criar_token(usuario_id: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=settings.TOKEN_EXPIRA_HORAS)
    payload = {"sub": str(usuario_id), "exp": exp}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_ALG)


def _id_do_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[_ALG])
        return int(payload.get("sub"))
    except (jwt.PyJWTError, ValueError, TypeError):
        return None


# ---------------- Dependências ----------------
def get_current_user(request: Request,
                     db: Session = Depends(get_session)) -> Usuario:
    """Usuário logado (via cookie de sessão). 401 se não autenticado;
    403 se o e-mail ainda não foi verificado."""
    token = request.cookies.get(COOKIE_NOME)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    uid = _id_do_token(token) if token else None
    if not uid:
        raise HTTPException(401, "Não autenticado")
    user = db.get(Usuario, uid)
    if not user or not user.ativo:
        raise HTTPException(401, "Sessão inválida")
    if not user.email_verificado:
        raise HTTPException(403, "E-mail ainda não verificado")
    return user
