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
from contextlib import asynccontextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .config import settings
from .database import get_session, init_db, SessionLocal
from .models import Produto, Edital, Match, RegraExclusao, LogColeta, Documento
from .service import processar_coleta
from .catalogo import catmat


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Radar de Licitações", version="1.1", lifespan=lifespan)

# Caminhos liberados sem autenticação Basic (health/keep-alive e cron com chave própria)
_ROTAS_PUBLICAS = {"/health", "/api/coletar-cron"}


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Protege tudo com HTTP Basic quando BASIC_AUTH_USER/PASS estão definidos."""
    async def dispatch(self, request, call_next):
        if not (settings.BASIC_AUTH_USER and settings.BASIC_AUTH_PASS):
            return await call_next(request)
        if request.url.path in _ROTAS_PUBLICAS:
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                if (secrets.compare_digest(user, settings.BASIC_AUTH_USER) and
                        secrets.compare_digest(pwd, settings.BASIC_AUTH_PASS)):
                    return await call_next(request)
            except Exception:
                pass
        return Response("Autenticação necessária", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Radar de Licitacoes"'})


app.add_middleware(BasicAuthMiddleware)

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
        "fornecedor_nome": p.fornecedor_nome, "fornecedor_contato": p.fornecedor_contato,
        "fornecedor_site": p.fornecedor_site,
    }


@app.get("/api/produtos")
def listar_produtos(
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_session),
):
    total = db.scalar(select(func.count(Produto.id))) or 0
    produtos = db.execute(
        select(Produto).order_by(Produto.id.desc())
        .limit(por_pagina).offset((pagina - 1) * por_pagina)
    ).scalars().all()
    return {
        "total": total, "pagina": pagina, "por_pagina": por_pagina,
        "paginas": (total + por_pagina - 1) // por_pagina,
        "resultados": [_produto_dict(p) for p in produtos],
    }


@app.get("/api/produtos/{produto_id}")
def obter_produto(produto_id: int, db: Session = Depends(get_session)):
    p = db.get(Produto, produto_id)
    if not p:
        raise HTTPException(404, "Produto não encontrado")
    return _produto_dict(p)


@app.post("/api/produtos")
def criar_produto(dados: ProdutoIn, db: Session = Depends(get_session)):
    p = Produto(**dados.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"id": p.id}


@app.put("/api/produtos/{produto_id}")
def atualizar_produto(produto_id: int, dados: ProdutoIn, db: Session = Depends(get_session)):
    p = db.get(Produto, produto_id)
    if not p:
        raise HTTPException(404, "Produto não encontrado")
    for campo, valor in dados.model_dump().items():
        setattr(p, campo, valor)
    db.commit()
    return {"ok": True, "id": p.id}


@app.delete("/api/produtos/{produto_id}")
def remover_produto(produto_id: int, db: Session = Depends(get_session)):
    p = db.get(Produto, produto_id)
    if not p:
        raise HTTPException(404, "Produto não encontrado")
    db.delete(p)
    db.commit()
    return {"ok": True}


# --------------------------- Editais / Matches ------------------------ #
@app.get("/api/editais")
def listar_editais(
    nivel: str | None = Query(None),
    uf: str | None = Query(None),
    status: str | None = Query(None),
    vista: str = Query("ativos", pattern="^(ativos|encerrados|todos)$"),
    apenas_nao_lidos: bool = Query(False),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_session),
):
    hoje = date.today()
    base = select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
    filtro = []
    if nivel:
        filtro.append(Match.nivel == nivel)
    if uf:
        filtro.append(Edital.uf == uf.upper())
    if status:
        filtro.append(Match.status == status)
    if apenas_nao_lidos:
        filtro.append(Match.lido == False)  # noqa: E712

    if vista == "ativos":
        # ainda dentro do prazo (sem data ou data >= hoje)
        filtro.append((Edital.data_encerramento.is_(None)) |
                      (Edital.data_encerramento >= hoje))
    elif vista == "encerrados":
        # prazo passou E eu participei (proposta enviada / ganho / perdido)
        filtro.append(Edital.data_encerramento < hoje)
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


@app.post("/api/editais/{match_id}/marcar")
def marcar(match_id: int, dados: MarcarIn, db: Session = Depends(get_session)):
    m = db.get(Match, match_id)
    if not m:
        raise HTTPException(404, "Match não encontrado")
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
def mudar_status(match_id: int, dados: StatusIn, db: Session = Depends(get_session)):
    if dados.status not in STATUS_VALIDOS:
        raise HTTPException(400, f"Status inválido. Use um de: {', '.join(sorted(STATUS_VALIDOS))}")
    m = db.get(Match, match_id)
    if not m:
        raise HTTPException(404, "Match não encontrado")
    m.status = dados.status
    db.commit()
    return {"ok": True}
def edital_detalhe(edital_id: int, db: Session = Depends(get_session)):
    """Detalhes do edital: cada item com o valor pedido pelo órgão, o produto
    compatível do seu catálogo, seu preço, a margem e os dados do fornecedor."""
    ed = db.get(Edital, edital_id)
    if not ed:
        raise HTTPException(404, "Edital não encontrado")
    match = db.execute(select(Match).where(Match.edital_id == edital_id)).scalar_one_or_none()

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
        if prod and it.valor_unitario is not None and prod.preco_custo is not None:
            margem = round(it.valor_unitario - prod.preco_custo, 2)
            if it.valor_unitario:
                margem_pct = round(margem / it.valor_unitario * 100, 1)
        itens.append({
            "numero": it.numero, "descricao": it.descricao,
            "valor_orgao": it.valor_unitario, "quantidade": it.quantidade,
            "compativel": prod is not None,
            "margem": margem, "margem_pct": margem_pct,
            "produto": None if not prod else {
                "id": prod.id, "descricao": prod.descricao,
                "preco_custo": prod.preco_custo, "preco_venda": prod.preco_venda,
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
def listar_regras(db: Session = Depends(get_session)):
    regras = db.execute(select(RegraExclusao)).scalars().all()
    return [{"id": r.id, "tipo": r.tipo, "valor": r.valor, "ativo": r.ativo} for r in regras]


@app.post("/api/regras")
def criar_regra(dados: RegraIn, db: Session = Depends(get_session)):
    r = RegraExclusao(tipo=dados.tipo, valor=dados.valor)
    db.add(r)
    db.commit()
    return {"id": r.id}


@app.delete("/api/regras/{regra_id}")
def remover_regra(regra_id: int, db: Session = Depends(get_session)):
    r = db.get(RegraExclusao, regra_id)
    if r:
        db.delete(r)
        db.commit()
    return {"ok": True}


# --------------------------- Coleta / Logs / Resumo ------------------- #
def _rodar_coleta_bg():
    db = SessionLocal()
    try:
        processar_coleta(db)
        # após coletar, verifica prazos encerrando e documentos vencendo
        from .lembretes import verificar_todos
        verificar_todos(db)
    finally:
        db.close()


@app.post("/api/coletar")
def coletar_agora(bg: BackgroundTasks):
    bg.add_task(_rodar_coleta_bg)
    return {"ok": True, "mensagem": "Coleta iniciada em segundo plano."}


@app.post("/api/recalcular")
def recalcular(db: Session = Depends(get_session)):
    """Reavalia os editais já coletados contra o catálogo atual (corrige
    resultados defasados depois de adicionar/remover produtos)."""
    from .service import recalcular_matches
    return recalcular_matches(db)


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
def coleta_status(db: Session = Depends(get_session)):
    """Estado da coleta para o indicador do dashboard."""
    ultimo = db.execute(
        select(LogColeta).order_by(LogColeta.id.desc()).limit(1)
    ).scalar_one_or_none()
    if not ultimo:
        return {"estado": "nunca"}

    agora = datetime.utcnow()
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
def logs(db: Session = Depends(get_session)):
    regs = db.execute(select(LogColeta).order_by(LogColeta.id.desc()).limit(30)).scalars().all()
    return [{
        "id": l.id, "fonte": l.fonte,
        "iniciado_em": _brt(l.iniciado_em),
        "finalizado_em": _brt(l.finalizado_em),
        "editais_novos": l.editais_novos, "editais_vistos": l.editais_vistos,
        "matches_fortes": l.matches_fortes, "erro": l.erro,
    } for l in regs]


@app.get("/api/resumo")
def resumo(db: Session = Depends(get_session)):
    total_prod = db.scalar(select(func.count(Produto.id))) or 0
    total_editais = db.scalar(select(func.count(Edital.id))) or 0
    por_nivel = dict(db.execute(
        select(Match.nivel, func.count(Match.id)).group_by(Match.nivel)
    ).all())
    nao_lidos = db.scalar(select(func.count(Match.id)).where(Match.lido == False)) or 0  # noqa: E712
    return {
        "produtos": total_prod, "editais": total_editais,
        "fortes": por_nivel.get("forte", 0), "medios": por_nivel.get("medio", 0),
        "fracos": por_nivel.get("fraco", 0), "nao_lidos": nao_lidos,
    }


@app.get("/api/export.csv")
def export_csv(nivel: str | None = None, db: Session = Depends(get_session)):
    q = select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
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
def listar_documentos(db: Session = Depends(get_session)):
    docs = db.execute(select(Documento).order_by(Documento.data_validade.asc())).scalars().all()
    hoje = date.today()
    return [{
        "id": d.id, "nome": d.nome, "orgao_emissor": d.orgao_emissor,
        "data_validade": d.data_validade.isoformat(),
        "dias_para_vencer": (d.data_validade - hoje).days,
        "observacao": d.observacao, "ativo": d.ativo,
    } for d in docs]


@app.post("/api/documentos")
def criar_documento(dados: DocumentoIn, db: Session = Depends(get_session)):
    d = Documento(**dados.model_dump())
    db.add(d)
    db.commit()
    return {"id": d.id}


@app.put("/api/documentos/{doc_id}")
def atualizar_documento(doc_id: int, dados: DocumentoIn, db: Session = Depends(get_session)):
    d = db.get(Documento, doc_id)
    if not d:
        raise HTTPException(404, "Documento não encontrado")
    for campo, valor in dados.model_dump().items():
        setattr(d, campo, valor)
    d.avisado_para = None  # validade mudou -> permite avisar de novo
    db.commit()
    return {"ok": True}


@app.delete("/api/documentos/{doc_id}")
def remover_documento(doc_id: int, db: Session = Depends(get_session)):
    d = db.get(Documento, doc_id)
    if d:
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
def obter_config(db: Session = Depends(get_session)):
    from . import configuracoes
    from .matching.embeddings import ia_disponivel
    dados = configuracoes.todas(db)
    dados["IA_DISPONIVEL"] = "1" if ia_disponivel() else "0"  # chave configurada?
    return dados


@app.post("/api/config")
def salvar_config(dados: ConfigIn, db: Session = Depends(get_session)):
    from . import configuracoes
    for chave, valor in dados.model_dump().items():
        if valor is not None:
            configuracoes.definir(db, chave, valor.strip())
    return {"ok": True, "config": configuracoes.todas(db)}


# --------------------------- Inteligência de preço -------------------- #
@app.get("/api/inteligencia-preco")
def inteligencia_preco(db: Session = Depends(get_session)):
    """Para cada produto, estatísticas dos valores estimados dos editais já
    coletados em que ele apareceu como compatível. Dá uma referência de mercado
    com base no histórico que o próprio sistema acumulou.

    Obs.: usa o valor ESTIMADO do edital (não o preço homologado do vencedor —
    isso exigiria puxar os resultados/atas do PNCP, um passo futuro)."""
    produtos = db.execute(select(Produto)).scalars().all()
    saida = []
    for p in produtos:
        # editais cujos matches citam este produto no detalhe
        q = select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
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


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
