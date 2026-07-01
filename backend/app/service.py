"""
Serviço de orquestração: coleta -> persiste -> casa com o catálogo ->
pontua -> notifica. É chamado pela tarefa diária (Celery) e pelos scripts.
"""
import logging
from datetime import datetime
from .models import utcnow

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings, parse_csv_str
from .models import Produto, Edital, ItemEdital, Match, RegraExclusao, LogColeta, Usuario
from .connectors.base import BaseConnector, EditalColetado
from .connectors.pncp import PNCPConnector
from .matching.engine import (
    MatchingEngine, ProdutoCat, ItemEdt, aplicar_regras_exclusao,
)
from . import notifications
from .notifications import email as email_mod, telegram as telegram_mod
from .notifications import formato

log = logging.getLogger("servico")

NIVEIS_ORDEM = {"fraco": 0, "medio": 1, "forte": 2}


def _carregar_catalogo(db: Session, usuario_id: int) -> list[ProdutoCat]:
    produtos = db.execute(
        select(Produto).where(Produto.ativo == True)              # noqa: E712
        .where(Produto.usuario_id == usuario_id)
    ).scalars().all()
    return [ProdutoCat(
        id=p.id, descricao=p.descricao, ncm=p.ncm or "", cest=p.cest or "",
        ean=p.ean or "", catmat=p.catmat or "", catser=p.catser or "",
        palavras_chave=p.palavras_chave or "",
    ) for p in produtos]


def _carregar_exclusoes(db: Session, usuario_id: int) -> tuple[list[str], list[str]]:
    regras = db.execute(
        select(RegraExclusao).where(RegraExclusao.ativo == True)   # noqa: E712
        .where(RegraExclusao.usuario_id == usuario_id)
    ).scalars().all()
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


def _usuarios_ativos(db: Session):
    return db.execute(select(Usuario).where(Usuario.ativo == True)).scalars().all()  # noqa: E712


def _gerar_matches_usuario(db: Session, usuario, recalcular_todos: bool = False) -> dict:
    """Gera/atualiza os matches de UM usuário contra os editais coletados,
    usando o catálogo e as regras de exclusão dele. Isolado por usuário."""
    catalogo = _carregar_catalogo(db, usuario.id)
    termos_excl, categorias_excl = _carregar_exclusoes(db, usuario.id)
    from . import configuracoes as cfg
    from .auth import decifrar
    usar_ia = cfg.obter(db, "IA_ATIVA") == "1"
    gemini_key = decifrar(usuario.gemini_key_cifrada)   # chave do próprio usuário
    engine = MatchingEngine(catalogo, usar_ia=usar_ia, gemini_key=gemini_key)

    # mais relevantes primeiro (se a cota de IA acabar, os melhores já foram feitos)
    editais = db.execute(
        select(Edital).order_by(Edital.coletado_em.desc())
    ).scalars().all()

    # carrega de uma vez os matches que o usuário já tem (mais rápido que consultar
    # um a um, e evita tentar criar duplicata na mesma rodada)
    existentes_map = {
        m.edital_id: m for m in db.execute(
            select(Match).where(Match.usuario_id == usuario.id)
        ).scalars().all()
    }

    resumo = {"editais": 0, "atualizados": 0, "fortes": 0}
    novos_fortes = []   # (objeto, orgao, link) dos matches fortes recém-criados
    for ed in editais:
        existente = existentes_map.get(ed.id)
        if existente and not recalcular_todos:
            continue
        resumo["editais"] += 1

        itens_edt = [ItemEdt(numero=i.numero, descricao=i.descricao,
                             ncm=i.ncm or "", catalogo_codigo=i.catalogo_codigo or "")
                     for i in ed.itens]
        # exclusões do próprio usuário
        if aplicar_regras_exclusao(ed.objeto or "", itens_edt, termos_excl, None, categorias_excl):
            if existente:
                db.delete(existente)
            continue

        era_novo = existente is None
        resultado = engine.avaliar(ed.objeto or "", itens_edt)
        m = existente or Match(edital_id=ed.id, usuario_id=usuario.id)
        if existente is None:
            db.add(m)
            existentes_map[ed.id] = m   # evita recriar na mesma rodada
        m.score = resultado.score
        m.nivel = resultado.nivel
        m.itens_compativeis = resultado.itens_compativeis
        m.detalhe = {"itens": resultado.detalhe}
        resumo["atualizados"] += 1
        if resultado.nivel == "forte":
            resumo["fortes"] += 1
            if era_novo:   # só avisa de oportunidades NOVAS (não no recálculo)
                novos_fortes.append((ed.objeto or "Edital", ed.orgao or "", ed.link or ""))
    try:
        db.commit()
    except IntegrityError:
        # rede de segurança: se outra coleta criou os mesmos matches em paralelo,
        # desfaz e não derruba o processo (a próxima coleta completa o que faltar)
        db.rollback()
        log.warning("Conflito de matches em paralelo (usuário %s) — ignorado.", usuario.id)
        return {"editais": 0, "atualizados": 0, "fortes": 0}

    if novos_fortes and not recalcular_todos:
        _notificar_usuario(usuario, novos_fortes)
    return resumo


def notificar_usuario_msg(usuario, titulo: str, corpo: str) -> bool:
    """Envia uma mensagem simples para o usuário pelos canais que ele ativou."""
    enviou = False
    try:
        if usuario.notif_email and usuario.email:
            enviou = email_mod.enviar_para(usuario.email, titulo, corpo) or enviou
        if usuario.notif_telegram and usuario.telegram_chat_id:
            enviou = telegram_mod.enviar_para_chat(usuario.telegram_chat_id, titulo, corpo) or enviou
    except Exception:
        log.exception("Falha ao notificar usuário %s", usuario.id)
    return enviou


def notificar_usuario_lote(usuario, titulo: str, intro: str, itens: list[dict]) -> bool:
    """Envia UM aviso agrupado com vários editais.
    - E-mail: um único e-mail com todos os editais em cartões (botão 'Abrir edital').
    - Telegram: uma mensagem por edital (com botão), pois fica mais legível no app.
    """
    if not itens:
        return False
    enviou = False
    try:
        if usuario.notif_email and usuario.email:
            html = formato.email_html(titulo, intro, itens)
            texto = formato.email_texto(intro, itens)
            enviou = email_mod.enviar_para(usuario.email, titulo, texto, html=html) or enviou
        if usuario.notif_telegram and usuario.telegram_chat_id:
            for it in itens:
                tit, corpo, link = formato.telegram_item(titulo, it)
                telegram_mod.enviar_para_chat(usuario.telegram_chat_id, tit, corpo,
                                              botao_url=link)
            enviou = True
    except Exception:
        log.exception("Falha ao notificar (lote) usuário %s", usuario.id)
    return enviou


def _notificar_usuario(usuario, fortes: list[tuple]):
    """Avisa o usuário sobre novas oportunidades de alta compatibilidade — agrupado."""
    itens = [{"objeto": objeto, "orgao": orgao, "link": link}
             for objeto, orgao, link in fortes]
    n = len(itens)
    titulo = (f"🎯 {n} novas oportunidades de alta compatibilidade"
              if n > 1 else "🎯 Nova oportunidade de alta compatibilidade")
    intro = "Encontramos no Radar editais que combinam bem com os seus produtos."
    notificar_usuario_lote(usuario, titulo, intro, itens)


def processar_coleta(db: Session, conectores: list[BaseConnector] | None = None,
                     usuario_id: int | None = None) -> dict:
    """Coleta editais do PNCP (compartilhados) e gera matches.
    - usuario_id definido: gera matches só para esse usuário (coleta manual).
    - usuario_id None: gera para todos os usuários já ativos (cron diário)."""
    if conectores is None:
        from . import configuracoes as cfg
        conectores = [PNCPConnector(
            ufs=cfg.obter(db, "PNCP_UFS"),
            modalidades=cfg.obter(db, "PNCP_MODALIDADES"),
            horizonte=int(cfg.obter(db, "PNCP_HORIZONTE_DIAS") or settings.PNCP_HORIZONTE_DIAS),
        )]
        # fonte extra opcional: Portal da Transparência (licitações federais)
        from .connectors.transparencia import TransparenciaConnector
        transp = TransparenciaConnector(
            horizonte=int(cfg.obter(db, "PNCP_HORIZONTE_DIAS") or settings.PNCP_HORIZONTE_DIAS))
        if transp.disponivel():
            conectores.append(transp)

    resumo = {"novos": 0, "vistos": 0}

    for conector in conectores:
        log_coleta = LogColeta(fonte=conector.nome, iniciado_em=utcnow())
        db.add(log_coleta)
        db.commit()
        base = {"novos": resumo["novos"], "vistos": resumo["vistos"]}
        try:
            coletados = conector.coletar()
        except Exception as e:
            log.exception("Erro no conector %s", conector.nome)
            log_coleta.erro = str(e)[:500]
            log_coleta.finalizado_em = utcnow()
            db.commit()
            continue
        try:
            for ec in coletados:
                resumo["vistos"] += 1
                try:
                    ed = _persistir_edital(db, ec)
                    if ed is not None:
                        resumo["novos"] += 1
                        db.commit()
                except Exception:
                    log.exception("Falha ao gravar edital %s", getattr(ec, "id_externo", "?"))
                    db.rollback()
        finally:
            log_coleta.editais_vistos = resumo["vistos"] - base["vistos"]
            log_coleta.editais_novos = resumo["novos"] - base["novos"]
            log_coleta.finalizado_em = utcnow()
            db.commit()

    # gera matches: só para o usuário que pediu (manual), ou todos os ativos (cron)
    if usuario_id is not None:
        alvos = [u for u in [db.get(Usuario, usuario_id)] if u]
    else:
        # cron: atualiza apenas quem JÁ coletou alguma vez (tem ao menos 1 match),
        # para não semear editais em contas novas que ainda não buscaram
        ids_com_match = db.execute(
            select(Match.usuario_id).where(Match.usuario_id.isnot(None)).distinct()
        ).scalars().all()
        alvos = [db.get(Usuario, uid) for uid in ids_com_match]
        alvos = [u for u in alvos if u and u.ativo]
    # rótulo das fontes usadas (ex.: "PNCP" ou "PNCP + Transparência")
    fonte_label = " + ".join(c.nome.upper() for c in conectores)
    inicio_user = utcnow()
    for u in alvos:
        try:
            r = _gerar_matches_usuario(db, u, recalcular_todos=False)
            resumo["fortes"] = resumo.get("fortes", 0) + r["fortes"]
            # registra no Histórico DESTE usuário o que entrou na conta dele
            db.add(LogColeta(
                usuario_id=u.id, fonte=fonte_label,
                iniciado_em=inicio_user, finalizado_em=utcnow(),
                editais_vistos=r.get("editais", 0),
                editais_novos=r.get("atualizados", 0),
                matches_fortes=r.get("fortes", 0),
            ))
            db.commit()
        except Exception:
            log.exception("Falha ao gerar matches do usuário %s", u.id)
            db.rollback()

    log.info("Coleta concluída: %s", resumo)
    return resumo


def recalcular_matches(db: Session, usuario_id: int) -> dict:
    """Reavalia todos os editais contra o catálogo ATUAL do usuário informado."""
    u = db.get(Usuario, usuario_id)
    if not u:
        return {"editais": 0, "atualizados": 0, "fortes": 0}
    return _gerar_matches_usuario(db, u, recalcular_todos=True)
