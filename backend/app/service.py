"""
Serviço de orquestração: coleta -> persiste -> casa com o catálogo ->
pontua -> notifica. É chamado pela tarefa diária (Celery) e pelos scripts.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from .models import utcnow

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, OperationalError
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

# histórico de coletas é log operacional, não dado de negócio — não precisa
# ser guardado para sempre (a tela já só mostra as mais recentes)
RETENCAO_LOGS_DIAS = 15

# quanto tempo um checkpoint de recálculo interrompido ainda é confiável pra
# retomar (ver _gerar_matches_usuario) — passado isso, a próxima rodada
# começa do zero de novo, por precaução (catálogo pode ter mudado).
RECALCULO_CHECKPOINT_VALIDADE = timedelta(hours=12)


def purgar_logs_antigos(db: Session) -> int:
    limite = utcnow() - timedelta(days=RETENCAO_LOGS_DIAS)
    resultado = db.execute(delete(LogColeta).where(LogColeta.iniciado_em < limite))
    db.commit()
    return resultado.rowcount or 0


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
            valor_unitario=it.valor_unitario, unidade_medida=it.unidade_medida,
        ))
    db.add(ed)
    db.flush()
    return ed


def _usuarios_ativos(db: Session):
    return db.execute(select(Usuario).where(Usuario.ativo == True)).scalars().all()  # noqa: E712


def _mesclar_confirmacoes_manuais(detalhe_novo: list[dict], detalhe_antigo: list[dict] | None,
                                  produtos_validos_ids: set[int]) -> list[dict]:
    """Preserva itens que o usuário confirmou manualmente (via o endpoint de
    confirmação) através de um recálculo novo — sem isso, `Match.detalhe`
    sendo sobrescrito por inteiro a cada rodada apagaria a escolha do
    usuário sem avisar. Só preserva quando o produto confirmado ainda existe
    no catálogo; se foi excluído, cai de volta pra sugestão fresca do motor
    (item some do dict de confirmados, resultado_novo passa direto)."""
    if not detalhe_antigo:
        return detalhe_novo
    confirmados = {
        it["item"]: it for it in detalhe_antigo
        if it.get("confirmado_manualmente") and (
            it.get("produto_id") is None or it.get("produto_id") in produtos_validos_ids)
    }
    if not confirmados:
        return detalhe_novo
    indice_por_numero = {it["item"]: i for i, it in enumerate(detalhe_novo)}
    resultado = list(detalhe_novo)
    for numero, item_confirmado in confirmados.items():
        if numero in indice_por_numero:
            resultado[indice_por_numero[numero]] = item_confirmado
        else:
            # o motor não achou mais candidata nenhuma pra esse item (ex.:
            # catálogo mudou bastante) — mesmo assim, o que o usuário
            # confirmou continua valendo até ele mudar de ideia.
            resultado.append(item_confirmado)
    return resultado


def _match_engajado(m: Match) -> bool:
    """True quando o usuário mexeu neste Match por conta própria (marcou
    lido/interessante, mudou o status do pipeline, ou confirmou manualmente
    algum item) — mesmo que o motor automático não ache sinal nenhum agora
    (nivel "fraco"). Usado pra recálculo não apagar silenciosamente um
    Match que só existe porque o usuário criou na mão (ver
    _match_do_usuario_por_edital/confirmar_item_edital em main.py), que sem
    essa checagem seria recriado com score=0/nivel="medio" e IMEDIATAMENTE
    apagado no próximo recálculo, perdendo status/lido/interessante/
    confirmações — achado real ao liberar os botões de acompanhamento pra
    editais sem Match automático."""
    if m.status != "novo" or m.lido or m.interessante:
        return True
    itens = (m.detalhe or {}).get("itens") or []
    return any(it.get("confirmado_manualmente") for it in itens)


def _gerar_matches_usuario(db: Session, usuario, recalcular_todos: bool = False,
                           forcar_usar_ia: bool | None = None, progresso=None,
                           deve_cancelar=None, modelo_reranker: str | None = None) -> dict:
    """Gera/atualiza os matches de UM usuário contra os editais coletados,
    usando o catálogo e as regras de exclusão dele. Isolado por usuário.

    deve_cancelar: callable() -> bool, checado periodicamente (mesma cadência
    dos commits parciais) — permite abortar uma rodada longa a pedido do
    usuário sem perder o que já foi processado até ali.
    modelo_reranker: sobrescreve o modelo padrão da DeepInfra pra essa rodada
    — seletor experimental do recálculo (main.py), restrito a uma conta."""
    deve_cancelar = deve_cancelar or (lambda: False)
    catalogo = _carregar_catalogo(db, usuario.id)
    produtos_validos_ids = {p.id for p in catalogo}
    termos_excl, categorias_excl = _carregar_exclusoes(db, usuario.id)
    from . import configuracoes as cfg
    usar_ia = cfg.obter(db, "IA_ATIVA") == "1"
    if forcar_usar_ia is not None:   # recálculo pode forçar sem IA (rápido)
        usar_ia = forcar_usar_ia
    gemini_api_key = None
    if modelo_reranker == "gemini":
        from . import auth as _auth
        gemini_api_key = _auth.decifrar(usuario.gemini_key_cifrada)
    engine = MatchingEngine(catalogo, usar_ia=usar_ia, modelo_reranker=modelo_reranker,
                            gemini_api_key=gemini_api_key)

    # Só considera editais que aparecem em ALGUM lugar da tela: ativos (prazo em
    # aberto) ou encerrados que o usuário está acompanhando (proposta enviada/
    # ganho/perdido). Editais encerrados sem acompanhamento não aparecem em
    # nenhuma aba — reavaliá-los é gasto (tempo e cota de IA) sem efeito visível.
    sub_acompanhados = select(Match.edital_id).where(
        Match.usuario_id == usuario.id,
        Match.status.in_(("proposta_enviada", "ganho", "perdido")),
    )
    hoje = date.today()
    query_editais = select(Edital).where(
        (Edital.data_encerramento.is_(None))
        | (Edital.data_encerramento >= hoje)
        | (Edital.id.in_(sub_acompanhados))
    )
    # retoma de um checkpoint de uma rodada completa (recalcular_todos)
    # interrompida antes de terminar — só se ainda estiver "fresco" (o
    # catálogo pode ter mudado desde então; RECALCULO_CHECKPOINT_VALIDADE é
    # o quanto confiamos que não mudou o bastante pra invalidar o que já foi
    # processado). Sem isso, todo redeploy/queda no meio de um recálculo
    # grande faz a próxima rodada regastar cota de IA nos editais já feitos.
    retomando = (
        recalcular_todos
        and usuario.recalculo_checkpoint_em is not None
        and usuario.recalculo_checkpoint_edital_id is not None
        and (utcnow() - usuario.recalculo_checkpoint_em) <= RECALCULO_CHECKPOINT_VALIDADE
    )
    if retomando:
        cp_dt = usuario.recalculo_checkpoint_coletado_em
        cp_id = usuario.recalculo_checkpoint_edital_id
        # mesma ordem (coletado_em desc, id desc como desempate) — mantém só
        # o que ainda NÃO foi processado na rodada anterior
        query_editais = query_editais.where(
            (Edital.coletado_em < cp_dt)
            | ((Edital.coletado_em == cp_dt) & (Edital.id < cp_id))
        )
        log.info("Recálculo do usuário %s retomando do checkpoint (edital_id=%s, %s)",
                usuario.id, cp_id, usuario.recalculo_checkpoint_em)
    # mais relevantes primeiro (se a cota de IA acabar, os melhores já foram feitos)
    editais = db.execute(
        query_editais.order_by(Edital.coletado_em.desc(), Edital.id.desc())
    ).scalars().all()
    total = len(editais)
    if progresso:
        progresso(0, total)

    # carrega de uma vez os matches que o usuário já tem (mais rápido que consultar
    # um a um, e evita tentar criar duplicata na mesma rodada)
    existentes_map = {
        m.edital_id: m for m in db.execute(
            select(Match).where(Match.usuario_id == usuario.id)
        ).scalars().all()
    }

    resumo = {"editais": 0, "atualizados": 0, "fortes": 0}
    novos_fortes: list[Edital] = []   # editais dos matches fortes recém-criados
    # retrato do que já foi de fato persistido (o commit em lote pode falhar
    # e ser desfeito por rollback — sem isso, o resumo/aviso reportaria
    # progresso que na verdade nunca chegou a salvar no banco).
    resumo_commitado = dict(resumo)
    novos_fortes_commitados: list[Edital] = []
    ultimo_visto: Edital | None = None   # pra gravar o checkpoint junto do commit parcial
    for _i, ed in enumerate(editais):
        if progresso and _i % 50 == 0:
            progresso(_i, total)
        # commits parciais: com dezenas de milhares de editais isso pode levar
        # minutos/horas rodando numa única transação. Sem isso, qualquer soluço
        # de conexão com o banco no meio do caminho perde TODO o progresso.
        if _i > 0 and _i % 200 == 0:
            if recalcular_todos and ultimo_visto is not None:
                usuario.recalculo_checkpoint_edital_id = ultimo_visto.id
                usuario.recalculo_checkpoint_coletado_em = ultimo_visto.coletado_em
                usuario.recalculo_checkpoint_em = utcnow()
            try:
                db.commit()
                resumo_commitado = dict(resumo)
                novos_fortes_commitados = list(novos_fortes)
            except IntegrityError:
                db.rollback()
                resumo, novos_fortes = dict(resumo_commitado), list(novos_fortes_commitados)
                log.warning("Conflito de matches em paralelo (usuário %s) — ignorado neste lote.",
                            usuario.id)
            except OperationalError:
                # a conexão com o banco caiu no meio da rodada (rodadas longas,
                # com chamadas de IA intercaladas, dão tempo da conexão expirar
                # do lado do provedor). O commit falho é desfeito por completo
                # pelo rollback — só o que foi commitado em lotes ANTERIORES
                # está realmente salvo, então o resumo volta pra esse ponto.
                db.rollback()
                resumo, novos_fortes = resumo_commitado, novos_fortes_commitados
                log.warning("Conexão com o banco caiu durante os matches do usuário %s "
                           "— parando nesta rodada com o progresso já salvo.", usuario.id)
                break
            # cancelamento a pedido do usuário — mesmo ponto dos commits
            # parciais, então o que já foi salvo continua salvo (só não
            # processa o resto da fila).
            if deve_cancelar():
                resumo["cancelado"] = True
                log.info("Recálculo do usuário %s cancelado a pedido — %s/%s editais processados.",
                         usuario.id, _i, total)
                break
        ultimo_visto = ed
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

        # Confirmação manual (ver confirmar_item_edital em main.py) é um
        # sinal MAIS forte que o palpite fresco do reranker — o usuário
        # olhou e disse "é este produto". Achado real: um edital com 9
        # itens confirmados manualmente continuava "Baixa Compatibilidade
        # · 0", porque o score AGREGADO usava só o palpite do motor, cego
        # às confirmações (que só apareciam no detalhe por item). Mescla
        # ANTES de decidir "fraco", com score 1.0 pros itens confirmados,
        # e recalcula o agregado a partir daí — assim uma confirmação
        # pode tirar o edital de "fraco".
        detalhe_antigo = existente.detalhe.get("itens") if (existente and existente.detalhe) else None
        detalhe_final = _mesclar_confirmacoes_manuais(resultado.detalhe, detalhe_antigo, produtos_validos_ids)
        # parte de TODO item avaliado (resultado.scores_por_item), não só do
        # `detalhe` filtrado (que já exclui item de score baixo) — senão um
        # item de confiança baixa some da conta de "fração do edital" só por
        # não ter entrado no detalhe, mudando o agregado de itens que NEM
        # foram confirmados.
        scores_por_numero = {it["item"]: it["score_item"] for it in resultado.scores_por_item}
        for it in detalhe_final:
            if it.get("confirmado_manualmente"):
                scores_por_numero[it["item"]] = 1.0
        score_final, nivel_final, compativeis_final = MatchingEngine.agregar_de_scores(
            list(scores_por_numero.values()))

        # não guardamos matches "fracos" (baixa compatibilidade): entopem a base
        # e não representam oportunidade real. Se um match existente virou fraco
        # (ex.: no recálculo após mudar o catálogo), removemos — A MENOS que o
        # usuário já tenha se engajado com ele (ver _match_engajado): aí só
        # atualiza score/nivel pra refletir a realidade, mas preserva a linha.
        if nivel_final == "fraco":
            if existente:
                if _match_engajado(existente):
                    existente.score = score_final
                    existente.nivel = nivel_final
                    existente.itens_compativeis = compativeis_final
                    existente.detalhe = {"itens": detalhe_final}
                else:
                    db.delete(existente)
                    existentes_map.pop(ed.id, None)
            continue
        m = existente or Match(edital_id=ed.id, usuario_id=usuario.id)
        if existente is None:
            db.add(m)
            existentes_map[ed.id] = m   # evita recriar na mesma rodada
        m.score = score_final
        m.nivel = nivel_final
        m.itens_compativeis = compativeis_final
        m.detalhe = {"itens": detalhe_final}
        resumo["atualizados"] += 1
        if nivel_final == "forte":
            resumo["fortes"] += 1
            if era_novo:   # só avisa de oportunidades NOVAS (não no recálculo)
                novos_fortes.append(ed)
    else:
        # "else" do for: só roda se o loop terminou a lista inteira sem
        # nenhum "break" (cancelamento ou queda de conexão) — ou seja, a
        # rodada completou de verdade. Limpa o checkpoint: a PRÓXIMA vez que
        # alguém pedir um recálculo completo deve ser do zero de novo (reflete
        # o catálogo mais atual), não retomar um checkpoint de uma rodada que
        # já terminou.
        if recalcular_todos:
            usuario.recalculo_checkpoint_edital_id = None
            usuario.recalculo_checkpoint_coletado_em = None
            usuario.recalculo_checkpoint_em = None
    try:
        db.commit()
    except IntegrityError:
        # rede de segurança: se outra coleta criou os mesmos matches em paralelo,
        # desfaz e não derruba o processo (a próxima coleta completa o que faltar)
        db.rollback()
        log.warning("Conflito de matches em paralelo (usuário %s) — ignorado.", usuario.id)
        return {"editais": 0, "atualizados": 0, "fortes": 0}
    except OperationalError:
        # conexão caiu bem no commit final — o rollback desfaz essa última
        # leva pendente, então o resumo/aviso voltam pro que foi de fato
        # commitado nos lotes anteriores (o resto já está salvo no banco).
        db.rollback()
        resumo, novos_fortes = resumo_commitado, novos_fortes_commitados
        log.warning("Conexão com o banco caiu ao finalizar os matches do usuário %s "
                   "— mantendo o progresso já commitado nesta rodada.", usuario.id)

    if novos_fortes and not recalcular_todos:
        _notificar_usuario(usuario, novos_fortes)
    if progresso:
        progresso(total, total)
    return resumo


def notificar_usuario_lote(usuario, titulo: str, intro: str, itens: list[dict],
                           canais: tuple[str, ...] = ("email", "telegram")) -> bool:
    """Envia UM aviso agrupado com vários editais.
    - E-mail: um único e-mail com todos os editais em cartões (botão 'Abrir edital').
    - Telegram: uma mensagem por edital (com botão), pois fica mais legível no app.
    `canais`: quais mandar — o Telegram tem um menu interativo próprio
    (telegram_menu.py) que decide sozinho quando mandar cada categoria, então
    quem já usa o menu passa canais=("email",) pra não duplicar o aviso lá."""
    if not itens:
        return False
    enviou = False
    try:
        if "email" in canais and usuario.notif_email and usuario.email:
            html = formato.email_html(titulo, intro, itens)
            texto = formato.email_texto(intro, itens)
            enviou = email_mod.enviar_para(usuario.email, titulo, texto, html=html) or enviou
        chats = [c for c in (usuario.telegram_chat_id, usuario.telegram_chat_id_2) if c]
        if "telegram" in canais and usuario.notif_telegram and chats:
            for it in itens:
                tit, corpo, link = formato.telegram_item(titulo, it)
                for chat_id in chats:
                    telegram_mod.enviar_para_chat(chat_id, tit, corpo, botao_url=link)
            enviou = True
    except Exception:
        log.exception("Falha ao notificar (lote) usuário %s", usuario.id)
    return enviou


def _notificar_usuario(usuario, fortes: list[Edital]):
    """Avisa o usuário por e-mail sobre novas oportunidades de alta
    compatibilidade — agrupado. O Telegram NÃO manda nada aqui: o menu
    interativo (telegram_menu.py) cuida disso sozinho, com base na flag
    Match.notificado — inclusive pra matches que viraram fortes numa
    recálculo, não só nos criados nesta coleta."""
    itens = [formato.item_edital(ed, nivel="forte") for ed in fortes]
    n = len(itens)
    titulo = (f"🎯 {n} novas oportunidades de alta compatibilidade"
              if n > 1 else "🎯 Nova oportunidade de alta compatibilidade")
    intro = "Encontramos no Minha Licitação editais que combinam bem com os seus produtos."
    notificar_usuario_lote(usuario, titulo, intro, itens, canais=("email",))


# Poucas threads de propósito: cada usuário pode disparar chamadas de IA
# (reranking de confiança média) e consultas ao banco — concorrência alta
# demais só estressaria a cota de IA e o pool de conexões do Postgres sem
# ganho real de velocidade (o gargalo real de cada usuário é I/O — banco e,
# às vezes, IA — não CPU, então poucas threads já ajudam bastante).
_MAX_THREADS_COLETA = 4


def _gerar_matches_usuario_isolado(usuario_id: int, fonte_label: str, inicio: datetime,
                                   deve_cancelar) -> dict:
    """Roda _gerar_matches_usuario com a SUA PRÓPRIA sessão de banco — cada
    worker da ThreadPoolExecutor precisa da sua: uma Session do SQLAlchemy
    não é segura pra usar de mais de uma thread ao mesmo tempo. Mesmo
    tratamento de erro por usuário que o laço sequencial já tinha (uma falha
    aqui não pode derrubar os outros usuários da rodada)."""
    from .database import SessionLocal
    db = SessionLocal()
    try:
        u = db.get(Usuario, usuario_id)
        if not u:
            return {"fortes": 0}
        try:
            r = _gerar_matches_usuario(db, u, recalcular_todos=False, deve_cancelar=deve_cancelar)
            db.add(LogColeta(
                usuario_id=u.id, fonte=fonte_label, origem="cron",
                iniciado_em=inicio, finalizado_em=utcnow(),
                editais_vistos=r.get("editais", 0),
                editais_novos=r.get("atualizados", 0),
                matches_fortes=r.get("fortes", 0),
            ))
            db.commit()
            return {"fortes": r.get("fortes", 0)}
        except Exception as e:
            log.exception("Falha ao gerar matches do usuário %s", usuario_id)
            try:
                db.rollback()
                db.add(LogColeta(
                    usuario_id=usuario_id, fonte=fonte_label, origem="cron",
                    iniciado_em=inicio, finalizado_em=utcnow(), erro=str(e)[:500],
                ))
                db.commit()
            except Exception:
                log.exception("Também falhou ao registrar o erro do usuário %s "
                              "(provável conexão com o banco indisponível).", usuario_id)
                db.rollback()
            return {"fortes": 0}
    finally:
        db.close()


def _gerar_matches_varios_usuarios(alvos: list, fonte_label: str, inicio: datetime,
                                   deve_cancelar, progresso_fase=None) -> int:
    """Processa vários usuários em paralelo (pool de threads, cada uma com
    sua própria sessão de banco) em vez de um de cada vez. Achado real: numa
    coleta com mais de um usuário, essa etapa sequencial era o maior tempo
    gasto DEPOIS de já ter terminado de buscar no PNCP — o indicador do
    dashboard continuava mostrando só "coleta em andamento", parecendo uma
    trava. deve_cancelar já é cooperativo dentro de _gerar_matches_usuario
    (commits parciais a cada 200 editais) — aqui só evita SUBMETER usuários
    novos depois do pedido de cancelamento; os que já estão rodando terminam
    normalmente (param sozinhos no próprio checkpoint interno)."""
    total = len(alvos)
    feitos = 0
    fortes_total = 0
    with ThreadPoolExecutor(max_workers=min(_MAX_THREADS_COLETA, total)) as executor:
        futuros = {}
        for u in alvos:
            if deve_cancelar():
                break
            futuro = executor.submit(_gerar_matches_usuario_isolado, u.id, fonte_label, inicio, deve_cancelar)
            futuros[futuro] = u.id
        for futuro in as_completed(futuros):
            try:
                r = futuro.result()
                fortes_total += r.get("fortes", 0)
            except Exception:
                log.exception("Worker de matches falhou por completo pro usuário %s", futuros[futuro])
            feitos += 1
            if progresso_fase:
                progresso_fase("compatibilidade", feitos, total)
    return fortes_total


def processar_coleta(db: Session, conectores: list[BaseConnector] | None = None,
                     usuario_id: int | None = None, deve_cancelar=None,
                     progresso_fase=None) -> dict:
    """Coleta editais do PNCP (compartilhados) e gera matches.
    - usuario_id definido: gera matches só para esse usuário (coleta manual).
    - usuario_id None: gera para todos os usuários já ativos (cron diário).
    - deve_cancelar: callable() -> bool, permite abortar a pedido do usuário
      (checado na gravação dos editais coletados e repassado pro cálculo de
      matches). A busca em si no PNCP (conector.coletar()) não é
      interrompível no meio — só o que vem DEPOIS dela.
    - progresso_fase: callable(fase, feitos, total) opcional, chamado ao
      trocar de etapa ("buscando" -> "compatibilidade") e a cada usuário
      concluído — alimenta o indicador de fase em /api/coleta/status (achado
      real: o indicador só dizia "coleta em andamento" do início ao fim, sem
      distinguir "ainda buscando no PNCP" de "já terminou de buscar, agora
      calculando compatibilidade pro catálogo de cada usuário" — parecia uma
      trava mesmo quando só estava processando usuário por usuário)."""
    deve_cancelar = deve_cancelar or (lambda: False)
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

    # coleta manual: cria já um registro "em andamento" PARA ESTE usuário, senão
    # o indicador do dashboard (que filtra por usuario_id) não enxerga nada
    # rodando enquanto a coleta em si (abaixo) ainda não terminou — ficando
    # preso mostrando a última coleta antiga (ou "nunca") até tudo finalizar.
    log_usuario = None
    if usuario_id is not None:
        log_usuario = LogColeta(usuario_id=usuario_id, fonte="coleta", origem="manual", iniciado_em=utcnow())
        db.add(log_usuario)
        db.commit()

    if progresso_fase:
        progresso_fase("buscando", 0, len(conectores))
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
        cancelado = False
        try:
            for _i, ec in enumerate(coletados):
                # checa a cada 100 (não a cada item — deve_cancelar() pode
                # envolver um lock, não vale a pena pagar isso item a item)
                if _i > 0 and _i % 100 == 0 and deve_cancelar():
                    cancelado = True
                    log.info("Coleta cancelada a pedido — %s/%s itens do conector %s processados.",
                             _i, len(coletados), conector.nome)
                    break
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
            if cancelado:
                log_coleta.erro = "cancelado"
            db.commit()
        if cancelado:
            if log_usuario is not None:
                log_usuario.erro = "cancelado"
                log_usuario.finalizado_em = utcnow()
                db.commit()
            return resumo

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
    if progresso_fase:
        progresso_fase("compatibilidade", 0, len(alvos))

    # cron com mais de 1 usuário: essa é a etapa que mais demora numa coleta
    # com vários usuários (a busca no PNCP acima já rodou uma vez só,
    # compartilhada) — processa em paralelo em vez de um usuário de cada vez.
    # Coleta manual (usuario_id definido, sempre 1 usuário em alvos) continua
    # sequencial: não tem o que paralelizar, e log_usuario (registro "em
    # andamento" ESPECÍFICO da coleta manual) só faz sentido nesse caminho.
    if usuario_id is None and len(alvos) > 1:
        resumo["fortes"] = resumo.get("fortes", 0) + _gerar_matches_varios_usuarios(
            alvos, fonte_label, inicio_user, deve_cancelar, progresso_fase)
    else:
        for u in alvos:
            if deve_cancelar():
                if log_usuario is not None and log_usuario.finalizado_em is None:
                    log_usuario.erro = "cancelado"
                    log_usuario.finalizado_em = utcnow()
                    db.commit()
                break
            try:
                r = _gerar_matches_usuario(db, u, recalcular_todos=False, deve_cancelar=deve_cancelar)
                resumo["fortes"] = resumo.get("fortes", 0) + r["fortes"]
                # registra no Histórico DESTE usuário o que entrou na conta dele
                if log_usuario is not None and u.id == usuario_id:
                    # coleta manual: atualiza o próprio registro "em andamento"
                    # em vez de criar um segundo, pra não duplicar o histórico
                    log_usuario.fonte = fonte_label
                    log_usuario.finalizado_em = utcnow()
                    log_usuario.editais_vistos = r.get("editais", 0)
                    log_usuario.editais_novos = r.get("atualizados", 0)
                    log_usuario.matches_fortes = r.get("fortes", 0)
                    if r.get("cancelado"):
                        log_usuario.erro = "cancelado"
                else:
                    db.add(LogColeta(
                        usuario_id=u.id, fonte=fonte_label, origem="cron",
                        iniciado_em=inicio_user, finalizado_em=utcnow(),
                        editais_vistos=r.get("editais", 0),
                        editais_novos=r.get("atualizados", 0),
                        matches_fortes=r.get("fortes", 0),
                    ))
                db.commit()
                if progresso_fase:
                    progresso_fase("compatibilidade", alvos.index(u) + 1, len(alvos))
            except Exception as e:
                log.exception("Falha ao gerar matches do usuário %s", u.id)
                # a própria recuperação do erro (rollback + salvar o erro no log)
                # também pode falhar se foi a conexão com o banco que caiu — sem
                # este try/except aqui dentro, essa segunda falha escapa e derruba
                # a coleta inteira, deixando todo mundo depois de u sem log fechado.
                try:
                    db.rollback()
                    if log_usuario is not None and u.id == usuario_id:
                        log_usuario.erro = str(e)[:500]
                        log_usuario.finalizado_em = utcnow()
                        db.commit()
                except Exception:
                    log.exception("Também falhou ao registrar o erro do usuário %s "
                                  "(provável conexão com o banco indisponível) — seguindo "
                                  "para o próximo usuário.", u.id)
                    db.rollback()

    purgar_logs_antigos(db)
    log.info("Coleta concluída: %s", resumo)
    return resumo


def recalcular_matches(db: Session, usuario_id: int, usar_ia: bool | None = None,
                       progresso=None, deve_cancelar=None,
                       modelo_reranker: str | None = None) -> dict:
    """Reavalia todos os editais contra o catálogo ATUAL do usuário.
    - usar_ia=None: respeita a configuração; usar_ia=False: força recálculo só por
      texto (rápido, sem gastar cota — recomendado para a base inteira).
    - progresso(feito, total): callback opcional para reportar andamento.
    - deve_cancelar: callable() -> bool, permite abortar a pedido do usuário.
    - modelo_reranker: sobrescreve o modelo padrão da DeepInfra (seletor
      experimental, ver main.py — restrito a uma conta por enquanto)."""
    u = db.get(Usuario, usuario_id)
    if not u:
        return {"editais": 0, "atualizados": 0, "fortes": 0}
    return _gerar_matches_usuario(db, u, recalcular_todos=True,
                                  forcar_usar_ia=usar_ia, progresso=progresso,
                                  deve_cancelar=deve_cancelar,
                                  modelo_reranker=modelo_reranker)
