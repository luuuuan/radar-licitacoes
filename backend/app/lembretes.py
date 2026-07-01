"""
Lembretes automáticos, agrupados por usuário e por tipo:
- Aberturas: editais de alta compatibilidade que vão ABRIR em breve (X dias, escolha do usuário).
- Prazos: editais interessantes/compatíveis com proposta ENCERRANDO.
- Documentos: certidões/documentos de habilitação prestes a vencer.

Roda junto da coleta. Cada TIPO vira UM aviso agrupado (um e-mail com todos os
editais daquele tipo; no Telegram, uma mensagem por edital com botão).
Cada item é avisado só uma vez (flags no banco), para não virar spam.
"""
import logging
from datetime import date

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from .config import settings
from .models import Edital, Match, Documento, Usuario
from .service import notificar_usuario_lote, notificar_usuario_msg

log = logging.getLogger("lembretes")


def _item_edital(ed: Edital) -> dict:
    return {
        "objeto": ed.objeto, "orgao": ed.orgao,
        "municipio": ed.municipio, "uf": ed.uf, "link": ed.link,
        "abertura": str(ed.data_abertura) if ed.data_abertura else "",
        "encerramento": str(ed.data_encerramento) if ed.data_encerramento else "",
    }


def verificar_aberturas(db: Session) -> int:
    """Agrupa, por usuário, os editais de alta compatibilidade que vão abrir dentro
    da janela de dias escolhida por ele. Envia UM aviso agrupado por usuário."""
    hoje = date.today()
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
        dias = (ed.data_abertura - hoje).days
        if dias > max(0, usuario.dias_antecedencia):
            continue
        por_usuario.setdefault(usuario.id, []).append(_item_edital(ed))
        marcados.setdefault(usuario.id, []).append(match)

    enviados = 0
    for uid, itens in por_usuario.items():
        usuario = db.get(Usuario, uid)
        titulo = (f"📢 {len(itens)} edital(is) vão abrir em breve"
                  if len(itens) > 1 else "📢 Um edital vai abrir em breve")
        intro = "Editais compatíveis com seus produtos que vão abrir em breve — dá tempo de preparar a documentação."
        if notificar_usuario_lote(usuario, titulo, intro, itens):
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
    hoje = date.today()
    q = (select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
         .where(Match.prazo_avisado == False)              # noqa: E712
         .where(Edital.data_encerramento.is_not(None))
         .where(Match.usuario_id.is_not(None))
         .where(or_(Match.interessante == True, Match.nivel == "forte")))  # noqa: E712
    por_usuario: dict[int, list] = {}
    marcados: dict[int, list] = {}
    for match, ed in db.execute(q).all():
        dias = (ed.data_encerramento - hoje).days
        if dias < 0 or dias > settings.LEMBRETE_PRAZO_DIAS:
            continue
        usuario = db.get(Usuario, match.usuario_id)
        if not usuario or not usuario.ativo:
            continue
        por_usuario.setdefault(usuario.id, []).append(_item_edital(ed))
        marcados.setdefault(usuario.id, []).append(match)

    enviados = 0
    for uid, itens in por_usuario.items():
        usuario = db.get(Usuario, uid)
        titulo = (f"⏰ {len(itens)} edital(is) com prazo encerrando"
                  if len(itens) > 1 else "⏰ Um edital com prazo encerrando")
        intro = "O prazo de envio de propostas está acabando nestes editais."
        if notificar_usuario_lote(usuario, titulo, intro, itens):
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
    hoje = date.today()
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
        if notificar_usuario_lote(usuario, titulo, intro, itens):
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
