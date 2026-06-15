"""
Serviço de orquestração: coleta -> persiste -> casa com o catálogo ->
pontua -> notifica. É chamado pela tarefa diária (Celery) e pelos scripts.
"""
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings, parse_csv_str
from .models import Produto, Edital, ItemEdital, Match, RegraExclusao, LogColeta
from .connectors.base import BaseConnector, EditalColetado
from .connectors.pncp import PNCPConnector
from .matching.engine import (
    MatchingEngine, ProdutoCat, ItemEdt, aplicar_regras_exclusao,
)
from . import notifications

log = logging.getLogger("servico")

NIVEIS_ORDEM = {"fraco": 0, "medio": 1, "forte": 2}


def _carregar_catalogo(db: Session) -> list[ProdutoCat]:
    produtos = db.execute(select(Produto).where(Produto.ativo == True)).scalars().all()  # noqa: E712
    return [ProdutoCat(
        id=p.id, descricao=p.descricao, ncm=p.ncm or "", cest=p.cest or "",
        ean=p.ean or "", catmat=p.catmat or "", catser=p.catser or "",
        palavras_chave=p.palavras_chave or "",
    ) for p in produtos]


def _carregar_exclusoes(db: Session) -> tuple[list[str], list[str]]:
    regras = db.execute(select(RegraExclusao).where(RegraExclusao.ativo == True)).scalars().all()  # noqa: E712
    termos = [r.valor for r in regras if r.tipo == "termo"]
    categorias = [r.valor for r in regras if r.tipo == "categoria"]
    return termos, categorias


def _persistir_edital(db: Session, ec: EditalColetado) -> Edital | None:
    """Cria o edital se ainda não existe. Retorna o objeto novo, ou None se já existia."""
    existe = db.execute(
        select(Edital).where(Edital.fonte == ec.fonte, Edital.id_externo == ec.id_externo)
    ).scalar_one_or_none()
    if existe:
        return None

    ed = Edital(
        fonte=ec.fonte, id_externo=ec.id_externo, orgao=ec.orgao,
        cnpj_orgao=ec.cnpj_orgao, objeto=ec.objeto, modalidade=ec.modalidade,
        uf=ec.uf, municipio=ec.municipio, valor_estimado=ec.valor_estimado,
        data_publicacao=ec.data_publicacao, data_abertura=ec.data_abertura,
        data_encerramento=ec.data_encerramento, link=ec.link, raw=ec.raw,
    )
    for it in ec.itens:
        ed.itens.append(ItemEdital(
            numero=it.numero, descricao=it.descricao,
            material_ou_servico=it.material_ou_servico, ncm=it.ncm,
            catalogo_codigo=it.catalogo_codigo, quantidade=it.quantidade,
            valor_unitario=it.valor_unitario,
        ))
    db.add(ed)
    db.flush()
    return ed


def processar_coleta(db: Session, conectores: list[BaseConnector] | None = None) -> dict:
    """Executa a coleta completa e retorna um resumo."""
    if conectores is None:
        conectores = [PNCPConnector()]

    catalogo = _carregar_catalogo(db)
    if not catalogo:
        log.warning("Catálogo vazio — cadastre produtos antes de coletar.")
    engine = MatchingEngine(catalogo)
    termos_excl, categorias_excl = _carregar_exclusoes(db)
    nivel_min = NIVEIS_ORDEM.get(settings.NOTIFICAR_NIVEL_MINIMO, 2)

    resumo = {"novos": 0, "vistos": 0, "fortes": 0, "notificados": 0}

    for conector in conectores:
        log_coleta = LogColeta(fonte=conector.nome, iniciado_em=datetime.utcnow())
        db.add(log_coleta)
        db.flush()
        try:
            coletados = conector.coletar()
        except Exception as e:  # não derruba os outros conectores
            log.exception("Erro no conector %s", conector.nome)
            log_coleta.erro = str(e)[:500]
            log_coleta.finalizado_em = datetime.utcnow()
            db.commit()
            continue

        for ec in coletados:
            resumo["vistos"] += 1

            # regras de exclusão (antes de persistir)
            itens_edt = [ItemEdt(numero=i.numero, descricao=i.descricao,
                                 ncm=i.ncm or "", catalogo_codigo=i.catalogo_codigo or "")
                         for i in ec.itens]
            if aplicar_regras_exclusao(ec.objeto or "", itens_edt,
                                       termos_excl, ec.categoria_pncp, categorias_excl):
                continue

            ed = _persistir_edital(db, ec)
            if ed is None:
                continue  # já existia
            resumo["novos"] += 1

            if not catalogo:
                continue

            resultado = engine.avaliar(ec.objeto or "", itens_edt)
            match = Match(
                edital_id=ed.id, score=resultado.score, nivel=resultado.nivel,
                itens_compativeis=resultado.itens_compativeis,
                detalhe={"itens": resultado.detalhe},
            )
            db.add(match)
            db.flush()

            if resultado.nivel == "forte":
                resumo["fortes"] += 1

            # notificação
            if NIVEIS_ORDEM[resultado.nivel] >= nivel_min:
                if notifications.notificar(ed, match):
                    match.notificado = True
                    resumo["notificados"] += 1

            db.commit()

        log_coleta.finalizado_em = datetime.utcnow()
        log_coleta.editais_novos = resumo["novos"]
        log_coleta.editais_vistos = resumo["vistos"]
        log_coleta.matches_fortes = resumo["fortes"]
        db.commit()

    log.info("Coleta concluída: %s", resumo)
    return resumo
