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
from datetime import date

from fastapi import FastAPI, Depends, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .database import get_session, init_db, SessionLocal
from .models import Produto, Edital, Match, RegraExclusao, LogColeta
from .service import processar_coleta
from .catalogo import catmat

app = FastAPI(title="Radar de Licitações", version="1.0")

BASE_DIR = os.path.dirname(__file__)
# A pasta static fica em backend/static (um nível acima de backend/app)
STATIC_DIR = os.path.join(os.path.dirname(BASE_DIR), "static")


@app.on_event("startup")
def _startup():
    init_db()


# --------------------------- Schemas ---------------------------------- #
class ProdutoIn(BaseModel):
    descricao: str
    ncm: str | None = None
    cest: str | None = None
    ean: str | None = None
    catmat: str | None = None
    catser: str | None = None
    palavras_chave: str | None = None


class RegraIn(BaseModel):
    tipo: str = "termo"
    valor: str


class MarcarIn(BaseModel):
    lido: bool | None = None
    interessante: bool | None = None


# --------------------------- Produtos --------------------------------- #
@app.get("/api/produtos")
def listar_produtos(db: Session = Depends(get_session)):
    produtos = db.execute(select(Produto).order_by(Produto.id.desc())).scalars().all()
    return [{
        "id": p.id, "descricao": p.descricao, "ncm": p.ncm, "cest": p.cest,
        "ean": p.ean, "catmat": p.catmat, "catser": p.catser,
        "palavras_chave": p.palavras_chave, "ativo": p.ativo,
    } for p in produtos]


@app.post("/api/produtos")
def criar_produto(dados: ProdutoIn, db: Session = Depends(get_session)):
    p = Produto(**dados.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"id": p.id}


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
    apenas_nao_lidos: bool = Query(False),
    db: Session = Depends(get_session),
):
    q = select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
    if nivel:
        q = q.where(Match.nivel == nivel)
    if uf:
        q = q.where(Edital.uf == uf.upper())
    if apenas_nao_lidos:
        q = q.where(Match.lido == False)  # noqa: E712
    q = q.order_by(Match.score.desc(), Edital.data_encerramento.asc())

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
            "detalhe": match.detalhe,
        })
    return out


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
    finally:
        db.close()


@app.post("/api/coletar")
def coletar_agora(bg: BackgroundTasks):
    bg.add_task(_rodar_coleta_bg)
    return {"ok": True, "mensagem": "Coleta iniciada em segundo plano."}


@app.get("/api/logs")
def logs(db: Session = Depends(get_session)):
    regs = db.execute(select(LogColeta).order_by(LogColeta.id.desc()).limit(30)).scalars().all()
    return [{
        "id": l.id, "fonte": l.fonte,
        "iniciado_em": l.iniciado_em.isoformat() if l.iniciado_em else None,
        "finalizado_em": l.finalizado_em.isoformat() if l.finalizado_em else None,
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
):
    """Busca códigos CATMAT (material) ou CATSER (serviço) na API oficial
    de dados abertos do Compras.gov.br, ranqueados por relevância."""
    resultados = catmat.buscar(descricao, tipo=tipo)
    return {"total": len(resultados), "resultados": resultados}


# --------------------------- Dashboard estático ----------------------- #
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
