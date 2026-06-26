"""
Lembretes automáticos:
- Prazos de editais interessantes/fortes que estão encerrando.
- Documentos de habilitação (certidões etc.) prestes a vencer.

Roda junto da coleta. Cada aviso vai para o DONO (usuário) do match/documento,
pelos canais que ele ativou no perfil. Cada aviso é enviado só uma vez por
evento (flags no banco), para não virar spam.
"""
import logging
from datetime import date

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from .config import settings
from .models import Edital, Match, Documento, Usuario
from .service import notificar_usuario_msg

log = logging.getLogger("lembretes")


def verificar_prazos(db: Session) -> int:
    """Avisa cada usuário sobre editais interessantes/fortes encerrando em <= N dias."""
    hoje = date.today()
    avisados = 0
    q = (select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
         .where(Match.prazo_avisado == False)              # noqa: E712
         .where(Edital.data_encerramento.is_not(None))
         .where(Match.usuario_id.is_not(None))
         .where(or_(Match.interessante == True, Match.nivel == "forte")))  # noqa: E712
    for match, ed in db.execute(q).all():
        dias = (ed.data_encerramento - hoje).days
        if dias < 0 or dias > settings.LEMBRETE_PRAZO_DIAS:
            continue
        usuario = db.get(Usuario, match.usuario_id)
        if not usuario or not usuario.ativo:
            continue
        titulo = f"⏰ Prazo encerrando: {ed.orgao or 'Edital'} ({dias} dia(s))"
        corpo = (
            f"O prazo de propostas está acabando.\n\n"
            f"Órgão: {ed.orgao}\n"
            f"Objeto: {(ed.objeto or '')[:300]}\n"
            f"Encerra em: {ed.data_encerramento} (faltam {dias} dia(s))\n"
            f"Local: {ed.municipio or ''}/{ed.uf or ''}\n"
            f"Link: {ed.link or ''}\n"
        )
        if notificar_usuario_msg(usuario, titulo, corpo):
            match.prazo_avisado = True
            avisados += 1
    db.commit()
    if avisados:
        log.info("Lembretes de prazo enviados: %d", avisados)
    return avisados


def verificar_documentos(db: Session) -> int:
    """Avisa cada usuário sobre os PRÓPRIOS documentos vencendo em <= N dias."""
    hoje = date.today()
    avisados = 0
    docs = db.execute(
        select(Documento).where(Documento.ativo == True)  # noqa: E712
    ).scalars().all()
    for doc in docs:
        if doc.data_validade is None:
            continue
        dias = (doc.data_validade - hoje).days
        if dias > settings.LEMBRETE_DOC_DIAS:
            continue  # ainda longe
        if doc.avisado_para == doc.data_validade:
            continue  # já avisou para esta validade
        usuario = db.get(Usuario, doc.usuario_id) if doc.usuario_id else None
        if not usuario or not usuario.ativo:
            continue
        situacao = (f"VENCIDO há {abs(dias)} dia(s)" if dias < 0
                    else f"vence em {dias} dia(s)")
        titulo = f"📄 Documento {doc.nome} — {situacao}"
        corpo = (
            f"Atenção a um documento de habilitação.\n\n"
            f"Documento: {doc.nome}\n"
            f"Emissor: {doc.orgao_emissor or '-'}\n"
            f"Validade: {doc.data_validade} ({situacao})\n"
            f"{('Link: ' + doc.link + chr(10)) if doc.link else ''}"
            f"{('Obs.: ' + doc.observacao) if doc.observacao else ''}\n"
        )
        if notificar_usuario_msg(usuario, titulo, corpo):
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
