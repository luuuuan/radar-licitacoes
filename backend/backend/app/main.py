"""
API FastAPI do Radar de Licitações + dashboard estático.

Rotas principais:
  GET  /api/produtos            lista catálogo
  POST /api/produtos            cadastra produto
  DEL  /api/produtos/{id}       remove produto
  GET  /api/editais             lista editais com match (filtros: nivel, uf, lido)
  POST /api/editais/{id}/marcar marca lido/interessante
  GET  /api/regras              lista regras de exclusão
  POST /api/regras              cria regra
  DEL  /api/regras/{id}         remove regra
  POST /api/coletar             dispara coleta manual (em background)
  GET  /api/export.csv          exporta matches em CSV
  GET  /api/logs                histórico de coletas
  GET  /api/resumo              KPIs do dashboard
"""
import csv
import io
import os
import base64
import secrets
import requests
from contextlib import asynccontextmanager
from datetime import date, datetime
from .models import utcnow as _utcnow_main
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, BackgroundTasks, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .config import settings
from .database import get_session, init_db, SessionLocal
from .models import Produto, Edital, Match, RegraExclusao, LogColeta, Documento, Proposta
from .service import processar_coleta
from .catalogo import catmat


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Radar de Licitações", version="2.0", lifespan=lifespan)

# Rotas liberadas sem login (auth, health, cron, página de login e estáticos)
_ROTAS_PUBLICAS = {"/health", "/api/coletar-cron", "/login", "/cadastro", "/verificar"}
_PREFIXOS_PUBLICOS = ("/api/auth/", "/static/", "/assets/")

BASE_DIR = os.path.dirname(__file__)
# A pasta static fica em backend/static (um nível acima de backend/app)
STATIC_DIR = os.path.join(os.path.dirname(BASE_DIR), "static")

BR_TZ = ZoneInfo("America/Sao_Paulo")


def _brt(dt: datetime | None) -> str | None:
    """Converte um datetime UTC (naive) para o horário de Brasília em ISO."""
    if not dt:
        return None
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(BR_TZ).isoformat()


@app.get("/health")
def health():
    return {"ok": True}


# =========================== AUTENTICAÇÃO ============================ #
import json as _json_auth
import secrets as _secrets_auth
from email_validator import validate_email, EmailNotValidError
from fastapi import Response as _Resp
from .models import Usuario
from . import auth as _auth
from .notifications import email as _email_mod


class CadastroIn(BaseModel):
    nome: str
    email: str
    senha: str
    documento: str | None = None       # CPF ou CNPJ


def _email_html_verificacao(nome: str, link: str) -> str:
    """E-mail de confirmação em HTML simples e sóbrio (melhora a entrega)."""
    return f"""\
<!DOCTYPE html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#f4f6f9;font-family:Arial,Helvetica,sans-serif;color:#1a2129">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:24px 0">
    <tr><td align="center">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0"
             style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden">
        <tr><td style="background:#1a2129;padding:20px 28px;color:#fff;font-size:18px;font-weight:bold">
          Radar de Licitações
        </td></tr>
        <tr><td style="padding:28px">
          <p style="margin:0 0 14px;font-size:15px">Olá, {nome}!</p>
          <p style="margin:0 0 20px;font-size:14px;line-height:1.5;color:#3b4654">
            Falta só um passo para ativar a sua conta. Clique no botão abaixo para
            confirmar o seu e-mail.
          </p>
          <p style="margin:0 0 24px;text-align:center">
            <a href="{link}" style="background:#2563eb;color:#fff;text-decoration:none;
               padding:12px 26px;border-radius:8px;font-size:14px;font-weight:bold;display:inline-block">
              Confirmar meu e-mail
            </a>
          </p>
          <p style="margin:0 0 8px;font-size:12px;color:#5b6770">
            Se o botão não funcionar, copie e cole este endereço no navegador:
          </p>
          <p style="margin:0 0 20px;font-size:12px;color:#2563eb;word-break:break-all">{link}</p>
          <p style="margin:0;font-size:12px;color:#94a3b8">
            Se você não criou esta conta, é só ignorar esta mensagem.
          </p>
        </td></tr>
      </table>
      <p style="margin:14px 0 0;font-size:11px;color:#94a3b8">Radar de Licitações</p>
    </td></tr>
  </table>
</body></html>"""


class LoginIn(BaseModel):
    email: str
    senha: str


def _set_cookie_sessao(resp: _Resp, usuario_id: int):
    token = _auth.criar_token(usuario_id)
    seguro = settings.APP_BASE_URL.startswith("https")
    resp.set_cookie(_auth.COOKIE_NOME, token, httponly=True, samesite="lax",
                    secure=seguro, max_age=settings.TOKEN_EXPIRA_HORAS * 3600,
                    path="/")


@app.post("/api/auth/cadastro")
def auth_cadastro(dados: CadastroIn, resp: _Resp, bg: BackgroundTasks,
                  db: Session = Depends(get_session)):
    # valida e-mail
    try:
        email = validate_email(dados.email, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        raise HTTPException(400, "E-mail inválido.")
    # força da senha
    erro = _auth.validar_forca_senha(dados.senha)
    if erro:
        raise HTTPException(400, erro)
    if not (dados.nome or "").strip():
        raise HTTPException(400, "Informe seu nome.")
    # e-mail único
    existe = db.execute(select(Usuario).where(Usuario.email == email)).scalars().first()
    if existe:
        raise HTTPException(409, "Já existe uma conta com este e-mail.")

    primeiro = db.scalar(select(func.count(Usuario.id))) == 0
    smtp_ok = _email_mod.smtp_configurado()

    u = Usuario(
        nome=dados.nome.strip(), email=email,
        senha_hash=_auth.hash_senha(dados.senha),
        doc_cifrado=_auth.cifrar((dados.documento or "").strip() or None),
        email_verificado=not smtp_ok,   # sem SMTP, libera direto; com SMTP, exige verificar
        token_verificacao=_secrets_auth.token_urlsafe(32) if smtp_ok else None,
    )
    db.add(u)
    db.flush()

    # o primeiro usuário "adota" os dados que já existiam (sem dono)
    if primeiro:
        for tabela in (Produto, Match, Documento, RegraExclusao, Proposta):
            db.query(tabela).filter(tabela.usuario_id.is_(None)).update(
                {tabela.usuario_id: u.id}, synchronize_session=False)

    db.commit()

    if smtp_ok:
        base = settings.APP_BASE_URL.rstrip("/")
        link = f"{base}/verificar?token={u.token_verificacao}"
        corpo = (f"Olá, {u.nome}!\n\nConfirme seu e-mail para ativar a sua conta no "
                 f"Radar de Licitações:\n{link}\n\n"
                 "Se você não criou esta conta, ignore esta mensagem.\n\n"
                 "— Radar de Licitações")
        html = _email_html_verificacao(u.nome, link)
        # envia em segundo plano: o cadastro responde na hora, sem esperar o e-mail
        bg.add_task(_email_mod.enviar_para, email,
                    "Confirme seu cadastro — Radar de Licitações", corpo, html)
        return {"ok": True, "verificar_email": True,
                "mensagem": "Enviamos um link de confirmação para o seu e-mail. "
                            "Confira também a caixa de spam."}

    _set_cookie_sessao(resp, u.id)
    return {"ok": True, "verificar_email": False}


@app.post("/api/auth/login")
def auth_login(dados: LoginIn, resp: _Resp, db: Session = Depends(get_session)):
    email = (dados.email or "").strip().lower()
    u = db.execute(select(Usuario).where(Usuario.email == email)).scalars().first()
    if not u or not _auth.conferir_senha(dados.senha, u.senha_hash):
        raise HTTPException(401, "E-mail ou senha incorretos.")
    if not u.ativo:
        raise HTTPException(403, "Conta desativada.")
    if not u.email_verificado:
        raise HTTPException(403, "Confirme seu e-mail antes de entrar. Verifique sua caixa de entrada.")
    _set_cookie_sessao(resp, u.id)
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(resp: _Resp):
    resp.delete_cookie(_auth.COOKIE_NOME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(user: Usuario = Depends(_auth.get_current_user)):
    return {"id": user.id, "nome": user.nome, "email": user.email,
            "documento": _auth.decifrar(user.doc_cifrado),
            "tem_gemini": bool(user.gemini_key_cifrada),
            "telegram_chat_id": user.telegram_chat_id or "",
            "notif_email": user.notif_email, "notif_telegram": user.notif_telegram}


@app.get("/api/auth/verificar")
def auth_verificar(token: str, db: Session = Depends(get_session)):
    u = db.execute(select(Usuario).where(Usuario.token_verificacao == token)).scalars().first()
    if not u:
        raise HTTPException(400, "Link de verificação inválido ou já usado.")
    u.email_verificado = True
    u.token_verificacao = None
    db.commit()
    return {"ok": True}


# =========================== PERFIL ============================ #
class PerfilIn(BaseModel):
    nome: str | None = None
    documento: str | None = None
    gemini_key: str | None = None       # "" limpa; None mantém
    telegram_chat_id: str | None = None
    notif_email: bool | None = None
    notif_telegram: bool | None = None
    endereco: dict | None = None        # {cep, logradouro, numero, bairro, cidade, uf, complemento}


@app.get("/api/perfil")
def obter_perfil(user: Usuario = Depends(_auth.get_current_user)):
    import json as _j
    end = _auth.decifrar(user.endereco_cifrado)
    try:
        endereco = _j.loads(end) if end else {}
    except ValueError:
        endereco = {}
    return {
        "nome": user.nome, "email": user.email,
        "documento": _auth.decifrar(user.doc_cifrado) or "",
        "tem_gemini": bool(user.gemini_key_cifrada),
        "telegram_chat_id": user.telegram_chat_id or "",
        "notif_email": user.notif_email, "notif_telegram": user.notif_telegram,
        "endereco": endereco,
    }


@app.post("/api/perfil")
def salvar_perfil(dados: PerfilIn, user: Usuario = Depends(_auth.get_current_user),
                  db: Session = Depends(get_session)):
    import json as _j
    if dados.nome is not None and dados.nome.strip():
        user.nome = dados.nome.strip()
    if dados.documento is not None:
        user.doc_cifrado = _auth.cifrar(dados.documento.strip() or None)
    # chave Gemini: None = manter; "" = remover; texto = cifrar e guardar
    if dados.gemini_key is not None:
        user.gemini_key_cifrada = _auth.cifrar(dados.gemini_key.strip() or None)
    if dados.telegram_chat_id is not None:
        user.telegram_chat_id = dados.telegram_chat_id.strip() or None
    if dados.notif_email is not None:
        user.notif_email = dados.notif_email
    if dados.notif_telegram is not None:
        user.notif_telegram = dados.notif_telegram
    if dados.endereco is not None:
        user.endereco_cifrado = _auth.cifrar(_j.dumps(dados.endereco, ensure_ascii=False))
    db.commit()
    return {"ok": True}


@app.get("/api/cep/{cep}")
def consultar_cep(cep: str, user: Usuario = Depends(_auth.get_current_user)):
    """Autopreenchimento de endereço pelo CEP (ViaCEP, gratuito)."""
    limpo = "".join(c for c in cep if c.isdigit())
    if len(limpo) != 8:
        raise HTTPException(400, "CEP deve ter 8 dígitos.")
    try:
        r = requests.get(f"https://viacep.com.br/ws/{limpo}/json/", timeout=10)
        dados = r.json()
    except Exception:
        raise HTTPException(502, "Não foi possível consultar o CEP agora.")
    if dados.get("erro"):
        raise HTTPException(404, "CEP não encontrado.")
    return {
        "cep": dados.get("cep", ""), "logradouro": dados.get("logradouro", ""),
        "bairro": dados.get("bairro", ""), "cidade": dados.get("localidade", ""),
        "uf": dados.get("uf", ""), "complemento": dados.get("complemento", ""),
    }


# ===================== VÍNCULO DO TELEGRAM (multiusuário) ===================== #
@app.get("/api/telegram/vinculo")
def telegram_vinculo(user: Usuario = Depends(_auth.get_current_user),
                     db: Session = Depends(get_session)):
    """Devolve o link para o usuário conectar o Telegram dele ao bot do Radar.
    Gera um código único na primeira vez."""
    if not user.telegram_codigo:
        user.telegram_codigo = _secrets_auth.token_urlsafe(8)
        db.commit()
    bot = settings.TELEGRAM_BOT_USERNAME
    disponivel = bool(settings.TELEGRAM_BOT_TOKEN and bot)
    link = f"https://t.me/{bot}?start={user.telegram_codigo}" if disponivel else ""
    return {
        "disponivel": disponivel,
        "bot": bot,
        "codigo": user.telegram_codigo,
        "link": link,
        "conectado": bool(user.telegram_chat_id),
    }


@app.post("/api/telegram/desvincular")
def telegram_desvincular(user: Usuario = Depends(_auth.get_current_user),
                         db: Session = Depends(get_session)):
    user.telegram_chat_id = None
    db.commit()
    return {"ok": True}


@app.post("/api/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, req: Request, db: Session = Depends(get_session)):
    """Recebe as mensagens do Telegram. Quando alguém manda /start CÓDIGO,
    vincula o chat_id daquele usuário. Protegido por um segredo na URL."""
    if not settings.TELEGRAM_WEBHOOK_SECRET or secret != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(404, "not found")
    try:
        update = await req.json()
    except Exception:
        return {"ok": True}
    msg = (update or {}).get("message") or {}
    texto = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if texto.startswith("/start") and chat_id:
        partes = texto.split(maxsplit=1)
        codigo = partes[1].strip() if len(partes) > 1 else ""
        if codigo:
            u = db.execute(select(Usuario).where(Usuario.telegram_codigo == codigo)).scalars().first()
            if u:
                u.telegram_chat_id = chat_id
                u.notif_telegram = True
                db.commit()
                from .notifications import telegram as _tg
                _tg.enviar_para_chat(
                    chat_id, "✅ Telegram conectado!",
                    f"Pronto, {u.nome}! Você vai receber aqui os avisos de novas "
                    "oportunidades do Radar de Licitações.")
                return {"ok": True}
        from .notifications import telegram as _tg
        _tg.enviar_para_chat(
            chat_id, "Radar de Licitações",
            "Para conectar, abra o link de vínculo na tela 'Meu perfil' do sistema.")
    return {"ok": True}


@app.post("/api/telegram/registrar-webhook")
def telegram_registrar_webhook(user: Usuario = Depends(_auth.get_current_user)):
    """Registra o webhook no Telegram (rodar uma vez após configurar o bot)."""
    if not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_WEBHOOK_SECRET and settings.APP_BASE_URL):
        raise HTTPException(400, "Configure TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET e APP_BASE_URL.")
    url = f"{settings.APP_BASE_URL.rstrip('/')}/api/telegram/webhook/{settings.TELEGRAM_WEBHOOK_SECRET}"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook",
            json={"url": url, "allowed_updates": ["message"]}, timeout=15)
        return {"ok": r.status_code == 200, "resposta": r.json()}
    except Exception as e:
        raise HTTPException(502, f"Falha ao registrar webhook: {e}")


# --------------------------- Schemas ---------------------------------- #
class ProdutoIn(BaseModel):
    descricao: str
    ncm: str | None = None
    cest: str | None = None
    ean: str | None = None
    catmat: str | None = None
    catser: str | None = None
    palavras_chave: str | None = None
    preco_custo: float | None = None
    preco_venda: float | None = None
    unidade_venda: str | None = None
    itens_por_unidade: float | None = None
    fornecedor_nome: str | None = None
    fornecedor_contato: str | None = None
    fornecedor_site: str | None = None


class RegraIn(BaseModel):
    tipo: str = "termo"
    valor: str


class MarcarIn(BaseModel):
    lido: bool | None = None
    interessante: bool | None = None


class DocumentoIn(BaseModel):
    nome: str
    orgao_emissor: str | None = None
    data_validade: date
    observacao: str | None = None


# --------------------------- Produtos --------------------------------- #
def _produto_dict(p: Produto) -> dict:
    return {
        "id": p.id, "descricao": p.descricao, "ncm": p.ncm, "cest": p.cest,
        "ean": p.ean, "catmat": p.catmat, "catser": p.catser,
        "palavras_chave": p.palavras_chave, "ativo": p.ativo,
        "preco_custo": p.preco_custo, "preco_venda": p.preco_venda,
        "unidade_venda": p.unidade_venda, "itens_por_unidade": p.itens_por_unidade,
        "fornecedor_nome": p.fornecedor_nome, "fornecedor_contato": p.fornecedor_contato,
        "fornecedor_site": p.fornecedor_site,
    }


@app.get("/api/produtos")
def listar_produtos(
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(100, ge=1, le=500),
    user: Usuario = Depends(_auth.get_current_user),
    db: Session = Depends(get_session),
):
    cond = Produto.usuario_id == user.id
    total = db.scalar(select(func.count(Produto.id)).where(cond)) or 0
    produtos = db.execute(
        select(Produto).where(cond).order_by(Produto.id.desc())
        .limit(por_pagina).offset((pagina - 1) * por_pagina)
    ).scalars().all()
    return {
        "total": total, "pagina": pagina, "por_pagina": por_pagina,
        "paginas": (total + por_pagina - 1) // por_pagina,
        "resultados": [_produto_dict(p) for p in produtos],
    }


@app.get("/api/produtos/modelo.xlsx")
def modelo_produtos(user: Usuario = Depends(_auth.get_current_user)):
    """Planilha-modelo para importação de produtos."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Produtos"
    cabec = ["descricao", "palavras_chave", "ncm", "ean", "catmat", "catser",
             "preco_custo", "preco_venda",
             "fornecedor_nome", "fornecedor_contato", "fornecedor_site"]
    ws.append(cabec)
    ws.append(["Papel A4 75g branco", "papel, a4, sulfite, resma", "4802.56.99",
               "7891234567890", "150123", "", "18,90", "24,50",
               "Distribuidora Exemplo", "(45) 99999-0000", "site.com.br"])
    ws.append(["Caneta esferográfica azul", "caneta, esferográfica, azul", "",
               "", "", "", "1,20", "2,00", "", "", ""])
    for col in ws.columns:
        larg = max(len(str(c.value or "")) for c in col) + 2
        ws.column_dimensions[col[0].column_letter].width = min(larg, 40)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=modelo_produtos.xlsx"})


def _produto_do_usuario(db, produto_id, user) -> Produto:
    p = db.get(Produto, produto_id)
    if not p or p.usuario_id != user.id:
        raise HTTPException(404, "Produto não encontrado")
    return p


@app.get("/api/produtos/{produto_id}")
def obter_produto(produto_id: int, user: Usuario = Depends(_auth.get_current_user),
                  db: Session = Depends(get_session)):
    return _produto_dict(_produto_do_usuario(db, produto_id, user))


@app.post("/api/produtos")
def criar_produto(dados: ProdutoIn, user: Usuario = Depends(_auth.get_current_user),
                  db: Session = Depends(get_session)):
    p = Produto(**dados.model_dump(), usuario_id=user.id)
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"id": p.id}


@app.put("/api/produtos/{produto_id}")
def atualizar_produto(produto_id: int, dados: ProdutoIn,
                      user: Usuario = Depends(_auth.get_current_user),
                      db: Session = Depends(get_session)):
    p = _produto_do_usuario(db, produto_id, user)
    for campo, valor in dados.model_dump().items():
        setattr(p, campo, valor)
    db.commit()
    return {"ok": True, "id": p.id}


# Colunas aceitas na planilha de importação (cabeçalho -> campo do produto)
_COLS_IMPORT = {
    "descricao": "descricao", "descrição": "descricao", "produto": "descricao",
    "palavras_chave": "palavras_chave", "palavras-chave": "palavras_chave",
    "palavras chave": "palavras_chave", "ncm": "ncm", "cest": "cest", "ean": "ean",
    "catmat": "catmat", "catser": "catser",
    "preco_custo": "preco_custo", "preço_custo": "preco_custo", "custo": "preco_custo",
    "preco_venda": "preco_venda", "preço_venda": "preco_venda", "venda": "preco_venda",
    "fornecedor_nome": "fornecedor_nome", "fornecedor": "fornecedor_nome",
    "fornecedor_contato": "fornecedor_contato", "fornecedor_site": "fornecedor_site",
}
_CAMPOS_NUM = {"preco_custo", "preco_venda"}


def _num_br(v):
    """Converte '18,90' / '1.234,56' / 18.9 em float; vazio -> None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("R$", "").replace(" ", "")
    if "," in s:                       # formato BR: ponto = milhar, vírgula = decimal
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


@app.post("/api/produtos/importar")
async def importar_produtos(arquivo: UploadFile = File(...),
                            user: Usuario = Depends(_auth.get_current_user),
                            db: Session = Depends(get_session)):
    """Importa produtos de uma planilha .xlsx. Atualiza quando a descrição já
    existe; caso contrário, cria. Retorna um resumo do que foi feito."""
    import openpyxl
    conteudo = await arquivo.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(conteudo), data_only=True, read_only=True)
    except Exception:
        raise HTTPException(400, "Arquivo inválido. Envie uma planilha .xlsx.")
    ws = wb.active
    linhas = ws.iter_rows(values_only=True)
    try:
        cabec = next(linhas)
    except StopIteration:
        return {"status": "vazio", "criados": 0, "atualizados": 0, "ignorados": 0, "erros": []}

    # mapeia índice de coluna -> campo do produto
    mapa = {}
    for i, nome in enumerate(cabec):
        chave = str(nome or "").strip().lower()
        if chave in _COLS_IMPORT:
            mapa[i] = _COLS_IMPORT[chave]
    if "descricao" not in mapa.values():
        raise HTTPException(400, "A planilha precisa de uma coluna 'descricao'.")

    criados = atualizados = ignorados = 0
    erros = []
    for n, linha in enumerate(linhas, start=2):
        if linha is None or all(c is None or str(c).strip() == "" for c in linha):
            continue
        dados = {}
        for i, campo in mapa.items():
            val = linha[i] if i < len(linha) else None
            if campo in _CAMPOS_NUM:
                dados[campo] = _num_br(val)
            else:
                dados[campo] = (str(val).strip() if val not in (None, "") else None)
        desc = dados.get("descricao")
        if not desc:
            ignorados += 1
            continue
        # atualizar se a descrição já existe NESTE usuário (case-insensitive)
        existente = db.execute(
            select(Produto).where(Produto.usuario_id == user.id)
            .where(func.lower(Produto.descricao) == desc.lower())
        ).scalars().first()
        try:
            if existente:
                for campo, valor in dados.items():
                    if valor is not None:           # só sobrescreve o que veio preenchido
                        setattr(existente, campo, valor)
                atualizados += 1
            else:
                db.add(Produto(**dados, usuario_id=user.id))
                criados += 1
        except Exception as e:
            erros.append(f"linha {n}: {e}")
    db.commit()
    return {"status": "ok", "criados": criados, "atualizados": atualizados,
            "ignorados": ignorados, "erros": erros[:20]}


@app.delete("/api/produtos/{produto_id}")
def remover_produto(produto_id: int, user: Usuario = Depends(_auth.get_current_user),
                    db: Session = Depends(get_session)):
    p = _produto_do_usuario(db, produto_id, user)
    db.delete(p)
    db.commit()
    return {"ok": True}


# --------------------------- Editais / Matches ------------------------ #
def _inicio_hoje_utc() -> datetime:
    """Início do dia de hoje no fuso de Brasília, convertido para UTC naïve
    (coletado_em é gravado em UTC). Serve para contar 'coletados hoje'."""
    tz = ZoneInfo("America/Sao_Paulo")
    agora = datetime.now(tz)
    inicio = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    return inicio.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


@app.get("/api/editais")
def listar_editais(
    nivel: str | None = Query(None),
    uf: str | None = Query(None),
    status: str | None = Query(None),
    vista: str = Query("ativos", pattern="^(ativos|encerrados|todos)$"),
    apenas_nao_lidos: bool = Query(False),
    hoje: bool = Query(False),
    tipo: str = Query("todos", pattern="^(todos|produtos|servicos)$"),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(50, ge=1, le=200),
    user: Usuario = Depends(_auth.get_current_user),
    db: Session = Depends(get_session),
):
    hoje_data = date.today()
    base = select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
    filtro = [Match.usuario_id == user.id]
    if nivel:
        filtro.append(Match.nivel == nivel)
    if uf:
        filtro.append(Edital.uf == uf.upper())
    if status:
        filtro.append(Match.status == status)
    if apenas_nao_lidos:
        filtro.append(Match.lido == False)  # noqa: E712
    if hoje:
        filtro.append(Edital.data_abertura == date.today())
    # tipo: editais que contêm ao menos um item do tipo escolhido (material/serviço)
    if tipo != "todos":
        from .models import ItemEdital
        prefixo = "m" if tipo == "produtos" else "s"
        sub = (select(ItemEdital.edital_id)
               .where(ItemEdital.edital_id == Edital.id)
               .where(func.lower(func.substr(func.coalesce(ItemEdital.material_ou_servico, ""), 1, 1)) == prefixo))
        filtro.append(sub.exists())

    if vista == "ativos":
        # ainda dentro do prazo (sem data ou data >= hoje)
        filtro.append((Edital.data_encerramento.is_(None)) |
                      (Edital.data_encerramento >= hoje_data))
    elif vista == "encerrados":
        # prazo passou E eu participei (proposta enviada / ganho / perdido)
        filtro.append(Edital.data_encerramento < hoje_data)
        filtro.append(Match.status.in_(["proposta_enviada", "ganho", "perdido"]))
    for f in filtro:
        base = base.where(f)

    total = db.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0

    ordem = (Match.score.desc(), Edital.data_encerramento.asc()) if vista == "ativos" \
        else (Edital.data_encerramento.desc(),)
    q = base.order_by(*ordem)
    q = q.limit(por_pagina).offset((pagina - 1) * por_pagina)

    out = []
    for match, ed in db.execute(q).all():
        dias = (ed.data_encerramento - date.today()).days if ed.data_encerramento else None
        out.append({
            "match_id": match.id, "edital_id": ed.id,
            "orgao": ed.orgao, "objeto": ed.objeto, "uf": ed.uf,
            "municipio": ed.municipio, "modalidade": ed.modalidade,
            "valor_estimado": ed.valor_estimado, "fonte": ed.fonte,
            "data_encerramento": ed.data_encerramento.isoformat() if ed.data_encerramento else None,
            "dias_restantes": dias, "link": ed.link,
            "score": match.score, "nivel": match.nivel,
            "itens_compativeis": match.itens_compativeis,
            "lido": match.lido, "interessante": match.interessante,
            "status": match.status,
            "detalhe": match.detalhe,
        })
    return {
        "total": total, "pagina": pagina, "por_pagina": por_pagina,
        "paginas": (total + por_pagina - 1) // por_pagina,
        "resultados": out,
    }


def _match_do_usuario(db, match_id, user) -> Match:
    m = db.get(Match, match_id)
    if not m or m.usuario_id != user.id:
        raise HTTPException(404, "Match não encontrado")
    return m


@app.post("/api/editais/{match_id}/marcar")
def marcar(match_id: int, dados: MarcarIn,
           user: Usuario = Depends(_auth.get_current_user),
           db: Session = Depends(get_session)):
    m = _match_do_usuario(db, match_id, user)
    if dados.lido is not None:
        m.lido = dados.lido
    if dados.interessante is not None:
        m.interessante = dados.interessante
    db.commit()
    return {"ok": True}


STATUS_VALIDOS = {"novo", "vou_participar", "proposta_enviada", "ganho", "perdido", "descartado"}


class StatusIn(BaseModel):
    status: str


@app.post("/api/editais/{match_id}/status")
def mudar_status(match_id: int, dados: StatusIn,
                 user: Usuario = Depends(_auth.get_current_user),
                 db: Session = Depends(get_session)):
    if dados.status not in STATUS_VALIDOS:
        raise HTTPException(400, f"Status inválido. Use um de: {', '.join(sorted(STATUS_VALIDOS))}")
    m = _match_do_usuario(db, match_id, user)
    m.status = dados.status
    db.commit()
    return {"ok": True}


@app.get("/api/editais/{edital_id}/detalhe")
def edital_detalhe(edital_id: int, user: Usuario = Depends(_auth.get_current_user),
                   db: Session = Depends(get_session)):
    """Detalhes do edital: cada item com o valor pedido pelo órgão, o produto
    compatível do seu catálogo, seu preço, a margem e os dados do fornecedor."""
    ed = db.get(Edital, edital_id)
    if not ed:
        raise HTTPException(404, "Edital não encontrado")
    match = db.execute(select(Match).where(Match.edital_id == edital_id)
                       .where(Match.usuario_id == user.id)).scalar_one_or_none()

    # item (número) -> produto_id, a partir do detalhe do match
    mapa: dict = {}
    if match and match.detalhe:
        for d in (match.detalhe.get("itens") or []):
            if d.get("item") is not None:
                mapa[d["item"]] = d.get("produto_id")
    prod_ids = {v for v in mapa.values() if v}
    produtos = {}
    if prod_ids:
        produtos = {p.id: p for p in db.execute(
            select(Produto).where(Produto.id.in_(prod_ids))).scalars()}

    itens = []
    for it in ed.itens:
        prod = produtos.get(mapa.get(it.numero))
        margem = margem_pct = None
        custo_comparavel = None
        alerta_unidade = False
        if prod and it.valor_unitario is not None and prod.preco_custo is not None:
            # custo na MESMA base do órgão: se o produto é vendido em embalagem
            # (ex.: resma = 500 folhas), divide o custo pela qtd por unidade.
            por_unid = prod.itens_por_unidade if (prod.itens_por_unidade or 0) > 0 else 1
            custo_comparavel = round(prod.preco_custo / por_unid, 4)
            margem = round(it.valor_unitario - custo_comparavel, 4)
            if it.valor_unitario:
                margem_pct = round(margem / it.valor_unitario * 100, 1)
            # se a margem ainda é absurda, provavelmente as unidades não batem
            if margem_pct is not None and (margem_pct < -300 or margem_pct > 300):
                alerta_unidade = True
        itens.append({
            "numero": it.numero, "descricao": it.descricao,
            "valor_orgao": it.valor_unitario, "quantidade": it.quantidade,
            "compativel": prod is not None,
            "margem": margem, "margem_pct": margem_pct,
            "custo_comparavel": custo_comparavel,
            "alerta_unidade": alerta_unidade,
            "produto": None if not prod else {
                "id": prod.id, "descricao": prod.descricao,
                "preco_custo": prod.preco_custo, "preco_venda": prod.preco_venda,
                "unidade_venda": prod.unidade_venda,
                "itens_por_unidade": prod.itens_por_unidade,
                "fornecedor_nome": prod.fornecedor_nome,
                "fornecedor_contato": prod.fornecedor_contato,
                "fornecedor_site": prod.fornecedor_site,
            },
        })
    itens.sort(key=lambda x: x["compativel"], reverse=True)

    dias = (ed.data_encerramento - date.today()).days if ed.data_encerramento else None
    return {
        "edital": {
            "id": ed.id, "orgao": ed.orgao, "objeto": ed.objeto,
            "modalidade": ed.modalidade, "uf": ed.uf, "municipio": ed.municipio,
            "valor_estimado": ed.valor_estimado, "fonte": ed.fonte, "link": ed.link,
            "data_encerramento": ed.data_encerramento.isoformat() if ed.data_encerramento else None,
            "dias_restantes": dias,
            "nivel": match.nivel if match else None,
            "score": match.score if match else None,
        },
        "itens": itens,
    }


# --------------------------- Regras de exclusão ----------------------- #
@app.get("/api/regras")
def listar_regras(user: Usuario = Depends(_auth.get_current_user),
                  db: Session = Depends(get_session)):
    regras = db.execute(select(RegraExclusao)
                        .where(RegraExclusao.usuario_id == user.id)).scalars().all()
    return [{"id": r.id, "tipo": r.tipo, "valor": r.valor, "ativo": r.ativo} for r in regras]


@app.post("/api/regras")
def criar_regra(dados: RegraIn, user: Usuario = Depends(_auth.get_current_user),
                db: Session = Depends(get_session)):
    r = RegraExclusao(tipo=dados.tipo, valor=dados.valor, usuario_id=user.id)
    db.add(r)
    db.commit()
    return {"id": r.id}


@app.delete("/api/regras/{regra_id}")
def remover_regra(regra_id: int, user: Usuario = Depends(_auth.get_current_user),
                  db: Session = Depends(get_session)):
    r = db.get(RegraExclusao, regra_id)
    if r and r.usuario_id == user.id:
        db.delete(r)
        db.commit()
    return {"ok": True}


# --------------------------- Coleta / Logs / Resumo ------------------- #
def _rodar_coleta_bg(usuario_id: int | None = None):
    db = SessionLocal()
    try:
        processar_coleta(db, usuario_id=usuario_id)
        # após coletar, verifica prazos encerrando e documentos vencendo
        from .lembretes import verificar_todos
        verificar_todos(db)
    finally:
        db.close()


@app.post("/api/coletar")
def coletar_agora(bg: BackgroundTasks, user: Usuario = Depends(_auth.get_current_user),
                  db: Session = Depends(get_session)):
    # precisa ter produtos cadastrados para a coleta fazer sentido
    tem_produtos = db.scalar(
        select(func.count(Produto.id)).where(Produto.usuario_id == user.id)) or 0
    if not tem_produtos:
        return {"ok": False, "sem_produtos": True,
                "mensagem": "Cadastre ao menos um produto antes de buscar editais."}
    # coleta manual gera matches só para quem clicou
    bg.add_task(_rodar_coleta_bg, user.id)
    return {"ok": True, "mensagem": "Coleta iniciada em segundo plano."}


@app.post("/api/recalcular")
def recalcular(user: Usuario = Depends(_auth.get_current_user),
               db: Session = Depends(get_session)):
    """Reavalia os editais já coletados contra o catálogo atual DESTE usuário."""
    from .service import recalcular_matches
    return recalcular_matches(db, usuario_id=user.id)


def _ref_pncp(ed: Edital):
    """Reconstrói (cnpj, ano, sequencial) a partir do numeroControlePNCP
    (formato: cnpj-tipo-sequencial/ano)."""
    try:
        esq, ano = ed.id_externo.rsplit("/", 1)
        partes = esq.split("-")
        cnpj = (ed.cnpj_orgao or partes[0]).strip()
        seq = int(partes[-1])
        return cnpj, int(ano), seq
    except Exception:
        return None


def _listar_arquivos_pncp(ed: Edital) -> dict:
    """Busca no PNCP os arquivos/anexos publicados para o edital."""
    ref = _ref_pncp(ed)
    if not ref:
        return {"status": "sem_ref", "arquivos": [], "portal": ed.link}
    cnpj, ano, seq = ref
    base = settings.PNCP_ITENS_BASE_URL.rstrip("/")
    url = f"{base}/v1/orgaos/{cnpj}/compras/{ano}/{seq}/arquivos"
    try:
        r = requests.get(url, timeout=30,
                         headers={"Accept": "application/json",
                                  "User-Agent": "RadarLicitacoes/1.0"})
    except requests.RequestException:
        return {"status": "erro_rede", "arquivos": [], "portal": ed.link}
    if r.status_code != 200:
        return {"status": f"http_{r.status_code}", "arquivos": [], "portal": ed.link}
    try:
        dados = r.json()
    except ValueError:
        return {"status": "resposta_invalida", "arquivos": [], "portal": ed.link}

    lista = dados if isinstance(dados, list) else (dados.get("data") or [])
    arquivos = []
    for a in lista:
        if not isinstance(a, dict):
            continue
        seq_doc = a.get("sequencialDocumento")
        arquivos.append({
            "titulo": a.get("titulo") or a.get("nomeArquivo")
                      or a.get("tipoDocumentoNome") or "Documento",
            "tipo": a.get("tipoDocumentoNome") or "",
            "url": a.get("url") or a.get("uri") or a.get("link")
                   or (f"{url}/{seq_doc}" if seq_doc is not None else None),
        })
    arquivos = [x for x in arquivos if x["url"]]
    return {"status": "ok" if arquivos else "vazio",
            "arquivos": arquivos, "portal": ed.link}


@app.get("/api/editais/{edital_id}/documentos")
def documentos_edital(edital_id: int, user: Usuario = Depends(_auth.get_current_user),
                      db: Session = Depends(get_session)):
    """Lista os arquivos/anexos do edital publicados no PNCP para download."""
    ed = db.get(Edital, edital_id)
    if not ed:
        raise HTTPException(404, "Edital não encontrado")
    return _listar_arquivos_pncp(ed)


@app.get("/api/editais/{edital_id}/analise")
def analise_edital(edital_id: int, forcar: bool = Query(False),
                   user: Usuario = Depends(_auth.get_current_user),
                   db: Session = Depends(get_session)):
    """Análise do edital por IA (resumo, exigências, prazos, pontos de atenção).
    Resultado fica em cache; use ?forcar=true para refazer."""
    from . import analise_edital as ia
    import json as _json
    ed = db.get(Edital, edital_id)
    if not ed:
        raise HTTPException(404, "Edital não encontrado")
    # análise já feita: mostra do cache (é leitura, não consome IA)
    if ed.analise_ia and not forcar:
        try:
            cache = _json.loads(ed.analise_ia)
            cache["cache"] = True
            return cache
        except ValueError:
            pass
    # para RODAR uma análise nova, exige a chave Gemini do próprio usuário
    chave = _auth.decifrar(user.gemini_key_cifrada)
    if not ia.ia_texto_disponivel(chave):
        return {"status": "sem_ia"}
    docs = _listar_arquivos_pncp(ed)
    resultado = ia.analisar(ed.objeto or "", docs.get("arquivos") or [], api_key=chave)
    if resultado.get("status") == "ok":
        ed.analise_ia = _json.dumps(resultado, ensure_ascii=False)
        ed.analise_em = datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
        db.commit()
    return resultado


@app.api_route("/api/coletar-cron", methods=["GET", "POST"])
def coletar_cron(bg: BackgroundTasks, request: Request):
    """Dispara a coleta de DENTRO do Render (que alcança o PNCP), chamado por um
    agendador externo (GitHub Actions). Protegido por CRON_SECRET, já que esta
    rota é isenta do login Basic."""
    if not settings.CRON_SECRET:
        raise HTTPException(503, "Cron desativado: defina CRON_SECRET no ambiente.")
    enviado = request.headers.get("X-Cron-Key") or request.query_params.get("chave") or ""
    if not secrets.compare_digest(enviado, settings.CRON_SECRET):
        raise HTTPException(403, "Chave inválida.")
    bg.add_task(_rodar_coleta_bg)
    return {"ok": True, "mensagem": "Coleta iniciada (cron)."}


@app.get("/api/coleta/status")
def coleta_status(user: Usuario = Depends(_auth.get_current_user),
                  db: Session = Depends(get_session)):
    """Estado da coleta para o indicador do dashboard."""
    ultimo = db.execute(
        select(LogColeta).order_by(LogColeta.id.desc()).limit(1)
    ).scalar_one_or_none()
    if not ultimo:
        return {"estado": "nunca"}

    agora = _utcnow_main()
    em_andamento = ultimo.finalizado_em is None
    travado = False
    if em_andamento and ultimo.iniciado_em:
        if (agora - ultimo.iniciado_em).total_seconds() > 1800:  # >30 min sem terminar
            em_andamento, travado = False, True

    # última coleta concluída (pode ser anterior à que está rodando)
    ultima_ok = ultimo if ultimo.finalizado_em else db.execute(
        select(LogColeta).where(LogColeta.finalizado_em.is_not(None))
        .order_by(LogColeta.id.desc()).limit(1)
    ).scalar_one_or_none()

    estado = "em_andamento" if em_andamento else ("travado" if travado else "ocioso")
    return {
        "estado": estado,
        "iniciada_ha_seg": int((agora - ultimo.iniciado_em).total_seconds())
            if em_andamento and ultimo.iniciado_em else None,
        "ultima_fim_seg": int((agora - ultima_ok.finalizado_em).total_seconds())
            if ultima_ok and ultima_ok.finalizado_em else None,
        "novos": ultima_ok.editais_novos if ultima_ok else None,
        "vistos": ultima_ok.editais_vistos if ultima_ok else None,
        "fortes": ultima_ok.matches_fortes if ultima_ok else None,
        "erro": ultima_ok.erro if ultima_ok else None,
    }


@app.get("/api/logs")
def logs(user: Usuario = Depends(_auth.get_current_user),
         db: Session = Depends(get_session)):
    regs = db.execute(select(LogColeta).order_by(LogColeta.id.desc()).limit(30)).scalars().all()
    return [{
        "id": l.id, "fonte": l.fonte,
        "iniciado_em": _brt(l.iniciado_em),
        "finalizado_em": _brt(l.finalizado_em),
        "editais_novos": l.editais_novos, "editais_vistos": l.editais_vistos,
        "matches_fortes": l.matches_fortes, "erro": l.erro,
    } for l in regs]


@app.get("/api/resumo")
def resumo(user: Usuario = Depends(_auth.get_current_user),
           db: Session = Depends(get_session)):
    hoje = date.today()
    ativo = (Edital.data_encerramento.is_(None)) | (Edital.data_encerramento >= hoje)
    meu = Match.usuario_id == user.id

    total_prod = db.scalar(
        select(func.count(Produto.id)).where(Produto.usuario_id == user.id)) or 0
    # editais ativos que ESTE usuário tem como match
    total_editais = db.scalar(
        select(func.count(Match.id)).join(Edital, Match.edital_id == Edital.id)
        .where(ativo).where(meu)
    ) or 0
    por_nivel = dict(db.execute(
        select(Match.nivel, func.count(Match.id))
        .join(Edital, Match.edital_id == Edital.id)
        .where(ativo).where(meu)
        .group_by(Match.nivel)
    ).all())
    nao_lidos = db.scalar(
        select(func.count(Match.id))
        .join(Edital, Match.edital_id == Edital.id)
        .where(ativo).where(meu).where(Match.lido == False)  # noqa: E712
    ) or 0
    do_dia = db.scalar(
        select(func.count(Match.id))
        .join(Edital, Match.edital_id == Edital.id)
        .where(ativo).where(meu).where(Edital.data_abertura == hoje)
    ) or 0
    return {
        "produtos": total_prod, "editais": total_editais,
        "fortes": por_nivel.get("forte", 0), "medios": por_nivel.get("medio", 0),
        "fracos": por_nivel.get("fraco", 0), "nao_lidos": nao_lidos,
        "do_dia": do_dia,
    }


class PropostaIn(BaseModel):
    itens: list[dict] = []
    observacoes: str | None = None


def _proposta_payload(ed: Edital, prop: Proposta | None) -> dict:
    if prop and prop.itens:
        itens = prop.itens
    else:
        # esqueleto a partir dos itens do edital
        itens = [{
            "descricao": it.descricao,
            "quantidade": it.quantidade or 0,
            "custo_unit": 0,
            "preco_unit": it.valor_unitario or 0,
        } for it in ed.itens]
    total_venda = sum((i.get("preco_unit") or 0) * (i.get("quantidade") or 0) for i in itens)
    total_custo = sum((i.get("custo_unit") or 0) * (i.get("quantidade") or 0) for i in itens)
    margem = total_venda - total_custo
    margem_pct = (margem / total_venda * 100) if total_venda else 0
    return {
        "edital_id": ed.id, "orgao": ed.orgao, "objeto": ed.objeto,
        "itens": itens, "observacoes": prop.observacoes if prop else "",
        "total_venda": round(total_venda, 2), "total_custo": round(total_custo, 2),
        "margem": round(margem, 2), "margem_pct": round(margem_pct, 1),
        "existe": prop is not None,
    }


@app.get("/api/editais/{edital_id}/proposta")
def obter_proposta(edital_id: int, user: Usuario = Depends(_auth.get_current_user),
                   db: Session = Depends(get_session)):
    ed = db.get(Edital, edital_id)
    if not ed:
        raise HTTPException(404, "Edital não encontrado")
    prop = db.execute(select(Proposta).where(Proposta.edital_id == edital_id)
                      .where(Proposta.usuario_id == user.id)).scalars().first()
    return _proposta_payload(ed, prop)


@app.post("/api/editais/{edital_id}/proposta")
def salvar_proposta(edital_id: int, dados: PropostaIn,
                    user: Usuario = Depends(_auth.get_current_user),
                    db: Session = Depends(get_session)):
    ed = db.get(Edital, edital_id)
    if not ed:
        raise HTTPException(404, "Edital não encontrado")
    prop = db.execute(select(Proposta).where(Proposta.edital_id == edital_id)
                      .where(Proposta.usuario_id == user.id)).scalars().first()
    if prop is None:
        prop = Proposta(edital_id=edital_id, usuario_id=user.id)
        db.add(prop)
    prop.itens = dados.itens
    prop.observacoes = dados.observacoes
    db.commit()
    db.refresh(prop)
    return _proposta_payload(ed, prop)


@app.get("/api/editais/{edital_id}/proposta.csv")
def exportar_proposta(edital_id: int, user: Usuario = Depends(_auth.get_current_user),
                      db: Session = Depends(get_session)):
    ed = db.get(Edital, edital_id)
    if not ed:
        raise HTTPException(404, "Edital não encontrado")
    prop = db.execute(select(Proposta).where(Proposta.edital_id == edital_id)
                      .where(Proposta.usuario_id == user.id)).scalars().first()
    p = _proposta_payload(ed, prop)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Descrição", "Quantidade", "Custo unit.", "Preço unit.", "Total venda", "Margem"])
    for it in p["itens"]:
        q = it.get("quantidade") or 0
        cu = it.get("custo_unit") or 0
        pu = it.get("preco_unit") or 0
        w.writerow([it.get("descricao", ""), q, f"{cu:.2f}", f"{pu:.2f}",
                    f"{pu * q:.2f}", f"{(pu - cu) * q:.2f}"])
    w.writerow([])
    w.writerow(["", "", "", "TOTAIS:", f"{p['total_venda']:.2f}", f"{p['margem']:.2f}"])
    buf.seek(0)
    nome = f"proposta_edital_{edital_id}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={nome}"})


@app.get("/api/export.csv")
def export_csv(nivel: str | None = None,
               user: Usuario = Depends(_auth.get_current_user),
               db: Session = Depends(get_session)):
    q = select(Match, Edital).join(Edital, Match.edital_id == Edital.id) \
        .where(Match.usuario_id == user.id)
    if nivel:
        q = q.where(Match.nivel == nivel)
    q = q.order_by(Match.score.desc())

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["nivel", "score", "itens_compativeis", "orgao", "uf", "municipio",
                "modalidade", "valor_estimado", "data_encerramento", "objeto", "link"])
    for m, ed in db.execute(q).all():
        w.writerow([m.nivel, m.score, m.itens_compativeis, ed.orgao, ed.uf,
                    ed.municipio, ed.modalidade, ed.valor_estimado,
                    ed.data_encerramento, (ed.objeto or "")[:300], ed.link])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=editais.csv"},
    )


# --------------------------- Catálogo CATMAT/CATSER ------------------- #
@app.get("/api/catmat")
def buscar_catmat(
    descricao: str = Query(..., min_length=2),
    tipo: str = Query("material", pattern="^(material|servico)$"),
    debug: bool = Query(False),
    user: Usuario = Depends(_auth.get_current_user),
):
    """Busca códigos CATMAT (material) ou CATSER (serviço) na API oficial
    de dados abertos do Compras.gov.br, ranqueados por relevância.
    Use ?debug=true para diagnosticar o que a API externa devolveu."""
    r = catmat.buscar(descricao, tipo=tipo, debug=debug)
    saida = {"status": r["status"], "total": len(r["itens"]), "resultados": r["itens"]}
    if "debug" in r:
        saida["debug"] = r["debug"]
    return saida


# --------------------------- Documentos (habilitação) ----------------- #
@app.get("/api/documentos")
def listar_documentos(user: Usuario = Depends(_auth.get_current_user),
                      db: Session = Depends(get_session)):
    docs = db.execute(select(Documento).where(Documento.usuario_id == user.id)
                      .order_by(Documento.data_validade.asc())).scalars().all()
    hoje = date.today()
    return [{
        "id": d.id, "nome": d.nome, "orgao_emissor": d.orgao_emissor,
        "data_validade": d.data_validade.isoformat(),
        "dias_para_vencer": (d.data_validade - hoje).days,
        "observacao": d.observacao, "ativo": d.ativo,
    } for d in docs]


@app.post("/api/documentos")
def criar_documento(dados: DocumentoIn, user: Usuario = Depends(_auth.get_current_user),
                    db: Session = Depends(get_session)):
    d = Documento(**dados.model_dump(), usuario_id=user.id)
    db.add(d)
    db.commit()
    return {"id": d.id}


def _documento_do_usuario(db, doc_id, user) -> Documento:
    d = db.get(Documento, doc_id)
    if not d or d.usuario_id != user.id:
        raise HTTPException(404, "Documento não encontrado")
    return d


@app.put("/api/documentos/{doc_id}")
def atualizar_documento(doc_id: int, dados: DocumentoIn,
                        user: Usuario = Depends(_auth.get_current_user),
                        db: Session = Depends(get_session)):
    d = _documento_do_usuario(db, doc_id, user)
    for campo, valor in dados.model_dump().items():
        setattr(d, campo, valor)
    d.avisado_para = None  # validade mudou -> permite avisar de novo
    db.commit()
    return {"ok": True}


@app.delete("/api/documentos/{doc_id}")
def remover_documento(doc_id: int, user: Usuario = Depends(_auth.get_current_user),
                      db: Session = Depends(get_session)):
    d = _documento_do_usuario(db, doc_id, user)
    db.delete(d)
    db.commit()
    return {"ok": True}


@app.post("/api/lembretes/verificar")
def verificar_lembretes(bg: BackgroundTasks):
    """Dispara a verificação de prazos e documentos manualmente."""
    def _run():
        db = SessionLocal()
        try:
            from .lembretes import verificar_todos
            verificar_todos(db)
        finally:
            db.close()
    bg.add_task(_run)
    return {"ok": True, "mensagem": "Verificação de lembretes iniciada."}


# --------------------------- Configurações ---------------------------- #
class ConfigIn(BaseModel):
    PNCP_UFS: str | None = None
    PNCP_MODALIDADES: str | None = None
    PNCP_HORIZONTE_DIAS: str | None = None
    IA_ATIVA: str | None = None


@app.get("/api/config")
def obter_config(user: Usuario = Depends(_auth.get_current_user),
                 db: Session = Depends(get_session)):
    from . import configuracoes
    from .matching.embeddings import ia_disponivel, ia_bloqueada, segundos_para_liberar
    dados = configuracoes.todas(db)
    chave_user = _auth.decifrar(user.gemini_key_cifrada)
    dados["IA_DISPONIVEL"] = "1" if ia_disponivel(chave_user) else "0"  # chave (do user ou global)?
    dados["IA_CHAVE_PROPRIA"] = "1" if chave_user else "0"             # usa chave própria?
    dados["IA_BLOQUEADA"] = "1" if ia_bloqueada() else "0"            # cota diária estourou?
    dados["IA_LIBERA_EM_MIN"] = str(round(segundos_para_liberar() / 60))
    return dados


@app.post("/api/config")
def salvar_config(dados: ConfigIn, user: Usuario = Depends(_auth.get_current_user),
                  db: Session = Depends(get_session)):
    from . import configuracoes
    for chave, valor in dados.model_dump().items():
        if valor is not None:
            configuracoes.definir(db, chave, valor.strip())
    return {"ok": True, "config": configuracoes.todas(db)}


# --------------------------- Inteligência de preço -------------------- #
@app.get("/api/inteligencia-preco")
def inteligencia_preco(user: Usuario = Depends(_auth.get_current_user),
                       db: Session = Depends(get_session)):
    """Para cada produto, estatísticas dos valores estimados dos editais já
    coletados em que ele apareceu como compatível. Dá uma referência de mercado
    com base no histórico que o próprio sistema acumulou.

    Obs.: usa o valor ESTIMADO do edital (não o preço homologado do vencedor —
    isso exigiria puxar os resultados/atas do PNCP, um passo futuro)."""
    produtos = db.execute(select(Produto).where(Produto.usuario_id == user.id)).scalars().all()
    saida = []
    for p in produtos:
        # editais cujos matches DESTE usuário citam este produto no detalhe
        q = select(Match, Edital).join(Edital, Match.edital_id == Edital.id) \
            .where(Match.usuario_id == user.id)
        valores = []
        for match, ed in db.execute(q).all():
            if ed.valor_estimado is None:
                continue
            itens = (match.detalhe or {}).get("itens", []) if match.detalhe else []
            if any(it.get("produto_id") == p.id for it in itens):
                valores.append(ed.valor_estimado)
        if not valores:
            continue
        valores.sort()
        n = len(valores)
        mediana = valores[n // 2] if n % 2 else (valores[n // 2 - 1] + valores[n // 2]) / 2
        saida.append({
            "produto_id": p.id, "descricao": p.descricao,
            "ocorrencias": n,
            "minimo": round(min(valores), 2),
            "mediana": round(mediana, 2),
            "media": round(sum(valores) / n, 2),
            "maximo": round(max(valores), 2),
            "preco_venda": p.preco_venda,
        })
    saida.sort(key=lambda x: x["ocorrencias"], reverse=True)
    return saida


# --------------------------- Dashboard estático ----------------------- #
@app.get("/")
def index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/login")
@app.get("/cadastro")
@app.get("/verificar")
def pagina_login():
    """Página única de login/cadastro/verificação (decide pela URL no JS)."""
    return FileResponse(
        os.path.join(STATIC_DIR, "login.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
