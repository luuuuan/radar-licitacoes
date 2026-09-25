"""
Lembretes automáticos, agrupados por usuário e por tipo:
- Aberturas: editais de alta compatibilidade que vão ABRIR em breve (X dias, escolha do usuário).
- Prazos: editais interessantes/compatíveis com proposta ENCERRANDO.
- Documentos: certidões/documentos de habilitação prestes a vencer.

Roda junto da coleta. Cada TIPO vira UM e-mail agrupado (todos os editais
daquele tipo, num cartão cada). Cada item é avisado só uma vez por e-mail
(flags no banco), para não virar spam.

O Telegram NÃO é mandado daqui — essas mesmas categorias (mais "alta
compatibilidade") viram um menu interativo à parte (telegram_menu.py),
com suas próprias flags de "já visto", pra o usuário escolher o que quer
ver em vez de receber tudo de uma vez. Ver _rodar_coleta_bg em main.py.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from .config import settings
from .models import Edital, Match, Documento, Usuario
from .service import notificar_usuario_lote
from .notifications.formato import item_edital as _item_edital

log = logging.getLogger("lembretes")

# achado real (agente error-detective, auditoria de notificações pedida pelo
# usuário, mesma classe já corrigida em main.py/_query_editais_filtrada e
# notificacoes()): produção roda em UTC sem TZ configurado no Dockerfile, e
# Edital.data_abertura/data_encerramento/Documento.data_validade são
# comparados contra "hoje" em hora de Brasília em todo o resto do app --
# date.today() aqui adiantava "hoje" em 3h todo dia entre 21h e meia-noite
# de Brasília, deslocando por 1 dia a janela de aviso das 3 funções deste
# arquivo (abertura/prazo/documento), igual ao bug já corrigido no sino de
# notificações in-app.
_BR_TZ = ZoneInfo("America/Sao_Paulo")


def _hoje_brasilia():
    return datetime.now(_BR_TZ).replace(tzinfo=None).date()


def verificar_aberturas(db: Session) -> int:
    """Agrupa, por usuário, os editais de alta compatibilidade que vão abrir dentro
    da janela de dias escolhida por ele. Envia UM aviso agrupado por usuário."""
    hoje = _hoje_brasilia()
    q = (select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
         .where(Match.abertura_avisada == False)           # noqa: E712
         .where(Match.nivel == "forte")
         .where(Match.usuario_id.is_not(None))
         .where(Edital.data_abertura.is_not(None))
         .where(Edital.data_abertura >= hoje))
    por_usuario: dict[int, list] = {}
    marcados: dict[int, list] = {}
    for match, ed in db.execute(q).all():
        usuario = db.get(Usuario, match.usuario_id)
        if not usuario or not usuario.ativo or not usuario.avisar_abertura:
            continue
        # .date(): data_abertura agora guarda hora (ver _parse_data_hora em
        # connectors/pncp.py) -- subtrair de "hoje" (date) direto quebraria
        # com TypeError; esta contagem é em DIAS, não precisa da hora exata.
        dias = (ed.data_abertura.date() - hoje).days
        if dias > max(0, usuario.dias_antecedencia):
            continue
        por_usuario.setdefault(usuario.id, []).append(_item_edital(ed, nivel="forte"))
        marcados.setdefault(usuario.id, []).append(match)

    enviados = 0
    for uid, itens in por_usuario.items():
        usuario = db.get(Usuario, uid)
        titulo = (f"📢 {len(itens)} edital(is) vão abrir em breve"
                  if len(itens) > 1 else "📢 Um edital vai abrir em breve")
        intro = "Editais compatíveis com seus produtos que vão abrir em breve — dá tempo de preparar a documentação."
        if notificar_usuario_lote(usuario, titulo, intro, itens, canais=("email",)):
            for m in marcados[uid]:
                m.abertura_avisada = True
            enviados += 1
    db.commit()
    if enviados:
        log.info("Avisos de abertura (agrupados) enviados para %d usuário(s)", enviados)
    return enviados


def verificar_prazos(db: Session) -> int:
    """Agrupa, por usuário, os editais interessantes/compatíveis com a proposta
    encerrando em <= N dias. Envia UM aviso agrupado por usuário."""
    hoje = _hoje_brasilia()
    q = (select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
         .where(Match.prazo_avisado == False)              # noqa: E712
         .where(Edital.data_encerramento.is_not(None))
         .where(Match.usuario_id.is_not(None))
         .where(or_(Match.interessante == True, Match.nivel == "forte")))  # noqa: E712
    por_usuario: dict[int, list] = {}
    marcados: dict[int, list] = {}
    for match, ed in db.execute(q).all():
        # .date(): ver mesmo achado em verificar_aberturas, acima.
        dias = (ed.data_encerramento.date() - hoje).days
        if dias < 0 or dias > settings.LEMBRETE_PRAZO_DIAS:
            continue
        usuario = db.get(Usuario, match.usuario_id)
        if not usuario or not usuario.ativo:
            continue
        por_usuario.setdefault(usuario.id, []).append(_item_edital(ed, nivel=match.nivel))
        marcados.setdefault(usuario.id, []).append(match)

    enviados = 0
    for uid, itens in por_usuario.items():
        usuario = db.get(Usuario, uid)
        titulo = (f"⏰ {len(itens)} edital(is) com prazo encerrando"
                  if len(itens) > 1 else "⏰ Um edital com prazo encerrando")
        intro = "O prazo de envio de propostas está acabando nestes editais."
        if notificar_usuario_lote(usuario, titulo, intro, itens, canais=("email",)):
            for m in marcados[uid]:
                m.prazo_avisado = True
            enviados += 1
    db.commit()
    if enviados:
        log.info("Avisos de prazo (agrupados) enviados para %d usuário(s)", enviados)
    return enviados


def verificar_documentos(db: Session) -> int:
    """Agrupa, por usuário, os documentos vencendo em <= N dias.
    Envia UM aviso agrupado por usuário."""
    hoje = _hoje_brasilia()
    docs = db.execute(
        select(Documento).where(Documento.ativo == True)  # noqa: E712
    ).scalars().all()
    por_usuario: dict[int, list] = {}
    marcados: dict[int, list] = {}
    for doc in docs:
        if doc.data_validade is None or not doc.usuario_id:
            continue
        dias = (doc.data_validade - hoje).days
        if dias > settings.LEMBRETE_DOC_DIAS:
            continue
        if doc.avisado_para == doc.data_validade:
            continue
        usuario = db.get(Usuario, doc.usuario_id)
        if not usuario or not usuario.ativo:
            continue
        situacao = (f"VENCIDO há {abs(dias)} dia(s)" if dias < 0
                    else f"vence em {dias} dia(s)")
        por_usuario.setdefault(usuario.id, []).append({
            "orgao": doc.nome,
            "objeto": f"Emissor: {doc.orgao_emissor or '-'}",
            "extra": f"Validade: {doc.data_validade} ({situacao})"
                     + (f" · Obs.: {doc.observacao}" if doc.observacao else ""),
            "link": doc.link or "",
        })
        marcados.setdefault(usuario.id, []).append((doc, doc.data_validade))

    enviados = 0
    for uid, itens in por_usuario.items():
        usuario = db.get(Usuario, uid)
        titulo = (f"📄 {len(itens)} documento(s) a vencer"
                  if len(itens) > 1 else "📄 Um documento a vencer")
        intro = "Atenção aos seus documentos de habilitação com validade próxima."
        if notificar_usuario_lote(usuario, titulo, intro, itens, canais=("email",)):
            for doc, validade in marcados[uid]:
                doc.avisado_para = validade
            enviados += 1
    db.commit()
    if enviados:
        log.info("Avisos de documento (agrupados) enviados para %d usuário(s)", enviados)
    return enviados


def verificar_todos(db: Session) -> dict:
    return {
        "aberturas": verificar_aberturas(db),
        "prazos": verificar_prazos(db),
        "documentos": verificar_documentos(db),
    }
