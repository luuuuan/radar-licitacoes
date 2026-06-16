"""
Lembretes automáticos:
- Prazos de editais interessantes/fortes que estão encerrando.
- Documentos de habilitação (certidões etc.) prestes a vencer.

Roda junto da coleta diária (uma vez por dia). Cada aviso é enviado só uma vez
por evento (controlado por flags no banco), para não virar spam.
"""
import logging
from datetime import date

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from .config import settings
from .models import Edital, Match, Documento
from . import notifications

log = logging.getLogger("lembretes")


def verificar_prazos(db: Session) -> int:
    """Avisa sobre editais interessantes/fortes encerrando em <= N dias."""
    limite = date.today()
    avisados = 0
    q = (select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
         .where(Match.prazo_avisado == False)              # noqa: E712
         .where(Edital.data_encerramento.is_not(None))
         .where(or_(Match.interessante == True, Match.nivel == "forte")))  # noqa: E712
    for match, ed in db.execute(q).all():
        dias = (ed.data_encerramento - limite).days
        if dias < 0:
            continue  # já encerrou
        if dias > settings.LEMBRETE_PRAZO_DIAS:
            continue  # ainda longe
        titulo = f"⏰ Prazo encerrando: {ed.orgao or 'Edital'} ({dias} dia(s))"
        corpo = (
            f"O prazo de propostas está acabando.\n\n"
            f"Órgão: {ed.orgao}\n"
            f"Objeto: {(ed.objeto or '')[:300]}\n"
            f"Encerra em: {ed.data_encerramento} (faltam {dias} dia(s))\n"
            f"Local: {ed.municipio or ''}/{ed.uf or ''}\n"
            f"Link: {ed.link}\n"
        )
        if notifications.enviar_aviso(titulo, corpo):
            match.prazo_avisado = True
            avisados += 1
    db.commit()
    if avisados:
        log.info("Lembretes de prazo enviados: %d", avisados)
    return avisados


def verificar_documentos(db: Session) -> int:
    """Avisa sobre documentos que vencem em <= N dias (uma vez por validade)."""
    hoje = date.today()
    avisados = 0
    docs = db.execute(
        select(Documento).where(Documento.ativo == True)  # noqa: E712
    ).scalars().all()
    for doc in docs:
        dias = (doc.data_validade - hoje).days
        if dias > settings.LEMBRETE_DOC_DIAS:
            continue  # ainda longe
        # evita repetir o aviso para a mesma validade
        if doc.avisado_para == doc.data_validade:
            continue
        if dias < 0:
            situacao = f"VENCIDO há {abs(dias)} dia(s)"
        else:
            situacao = f"vence em {dias} dia(s)"
        titulo = f"📄 Documento {situacao}: {doc.nome}"
        corpo = (
            f"Atenção a um documento de habilitação.\n\n"
            f"Documento: {doc.nome}\n"
            f"Emissor: {doc.orgao_emissor or '-'}\n"
            f"Validade: {doc.data_validade} ({situacao})\n"
            f"{('Obs.: ' + doc.observacao) if doc.observacao else ''}\n"
        )
        if notifications.enviar_aviso(titulo, corpo):
            doc.avisado_para = doc.data_validade
            avisados += 1
    db.commit()
    if avisados:
        log.info("Lembretes de documento enviados: %d", avisados)
    return avisados


def verificar_todos(db: Session) -> dict:
    return {
        "prazos": verificar_prazos(db),
        "documentos": verificar_documentos(db),
    }
