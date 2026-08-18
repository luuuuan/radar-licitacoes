"""
Menu interativo do Telegram: em vez de despejar todos os avisos de uma vez
(alta compatibilidade + prazo encerrando + abrindo em breve + documentos a
vencer, tudo junto), manda um resumo com botões e só envia os editais/
documentos de uma categoria quando o usuário toca no botão correspondente.
Depois de mostrar, manda o resumo de novo com o que ainda restar.

O e-mail continua exatamente como era (imediato, agrupado por categoria,
sem menu) — por isso cada categoria tem uma flag de "já avisei" PRÓPRIA do
Telegram (ex.: `prazo_avisado_telegram`), separada da flag usada pelo
e-mail (`prazo_avisado`). Se fosse a mesma flag, o e-mail (que roda na hora)
marcaria o item como visto antes do usuário nunca ter tocado no botão do
Telegram, e ele sumiria do menu sem o usuário ter escolhido ver.
"""
from __future__ import annotations
import logging
from datetime import date

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from .config import settings
from .models import Match, Edital, Documento, Usuario
from .notifications import telegram as _tg, formato

log = logging.getLogger("telegram_menu")


def _pendentes_forte(db: Session, usuario: Usuario) -> list[tuple[Match, Edital]]:
    q = (select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
         .where(Match.usuario_id == usuario.id, Match.nivel == "forte",
                Match.notificado == False))  # noqa: E712
    return db.execute(q).all()


def _pendentes_prazo(db: Session, usuario: Usuario) -> list[tuple[Match, Edital]]:
    hoje = date.today()
    q = (select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
         .where(Match.usuario_id == usuario.id, Match.prazo_avisado_telegram == False)  # noqa: E712
         .where(Edital.data_encerramento.is_not(None))
         .where(or_(Match.interessante == True, Match.nivel == "forte")))  # noqa: E712
    resultado = []
    for m, ed in db.execute(q).all():
        dias = (ed.data_encerramento - hoje).days
        if 0 <= dias <= settings.LEMBRETE_PRAZO_DIAS:
            resultado.append((m, ed))
    return resultado


def _pendentes_abertura(db: Session, usuario: Usuario) -> list[tuple[Match, Edital]]:
    if not usuario.avisar_abertura:
        return []
    hoje = date.today()
    q = (select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
         .where(Match.usuario_id == usuario.id, Match.abertura_avisada_telegram == False,  # noqa: E712
                Match.nivel == "forte")
         .where(Edital.data_abertura.is_not(None), Edital.data_abertura >= hoje))
    resultado = []
    for m, ed in db.execute(q).all():
        dias = (ed.data_abertura - hoje).days
        if dias <= max(0, usuario.dias_antecedencia):
            resultado.append((m, ed))
    return resultado


def _pendentes_documento(db: Session, usuario: Usuario) -> list[Documento]:
    hoje = date.today()
    docs = db.execute(
        select(Documento).where(Documento.usuario_id == usuario.id, Documento.ativo == True)  # noqa: E712
    ).scalars().all()
    resultado = []
    for d in docs:
        if d.data_validade is None:
            continue
        dias = (d.data_validade - hoje).days
        if dias > settings.LEMBRETE_DOC_DIAS:
            continue
        if d.avisado_para_telegram == d.data_validade:
            continue
        resultado.append(d)
    return resultado


# categoria -> (emoji, rótulo, função que busca pendentes, tipo de registro)
CATEGORIAS = {
    "forte": ("🎯", "Alta compatibilidade", _pendentes_forte, "match"),
    "prazo": ("⏰", "Prazo encerrando", _pendentes_prazo, "match"),
    "abertura": ("📢", "Abrindo em breve", _pendentes_abertura, "match"),
    "documento": ("📄", "Documentos a vencer", _pendentes_documento, "documento"),
}


def _marcar_visto(categoria: str, registro) -> None:
    if categoria == "forte":
        registro.notificado = True
    elif categoria == "prazo":
        registro.prazo_avisado_telegram = True
    elif categoria == "abertura":
        registro.abertura_avisada_telegram = True
    elif categoria == "documento":
        registro.avisado_para_telegram = registro.data_validade


def contar_pendentes(db: Session, usuario: Usuario) -> dict[str, int]:
    return {chave: len(buscar(db, usuario)) for chave, (_, _, buscar, _) in CATEGORIAS.items()}


def _chats_telegram(usuario: Usuario) -> list[str]:
    """Contatos de Telegram vinculados a este usuário (1 ou 2 -- ver
    Usuario.telegram_chat_id_2, um 2º contato que recebe os mesmos avisos)."""
    return [c for c in (usuario.telegram_chat_id, usuario.telegram_chat_id_2) if c]


def enviar_resumo(db: Session, usuario: Usuario) -> bool:
    """Manda o menu com botões (uma linha por categoria com pendência) pra
    TODOS os contatos de Telegram vinculados a este usuário. Não manda nada
    se o usuário não tem nenhum Telegram conectado ou não há absolutamente
    nada pendente em nenhuma categoria."""
    chats = _chats_telegram(usuario)
    if not (usuario.notif_telegram and chats):
        return False
    contagens = contar_pendentes(db, usuario)
    pendentes = {k: v for k, v in contagens.items() if v > 0}
    if not pendentes:
        return False
    botoes = [(f"{CATEGORIAS[k][0]} {CATEGORIAS[k][1]} ({v})", f"radar:{k}")
             for k, v in pendentes.items()]
    total = sum(pendentes.values())
    titulo = f"📋 Você tem {total} aviso(s) novo(s)"
    corpo = "Escolha o que quer ver agora:"
    enviou = False
    for chat_id in chats:
        enviou = _tg.enviar_menu(chat_id, titulo, corpo, botoes) or enviou
    return enviou


def mostrar_categoria(db: Session, usuario: Usuario, categoria: str, chat_id: str) -> int:
    """Envia os editais/documentos pendentes daquela categoria (uma mensagem
    por item, como já era) e marca todos como vistos. Retorna quantos
    enviou. Chamado a partir do toque no botão (callback_query) -- `chat_id`
    é o chat que TOCOU (pode ser o 1º ou o 2º contato do usuário); manda a
    resposta pra ele especificamente, não pra um contato fixo, senão o 2º
    contato tocando no botão faria a resposta ir pro 1º."""
    cfg = CATEGORIAS.get(categoria)
    if not cfg:
        return 0
    emoji, label, buscar, tipo = cfg
    pendentes = buscar(db, usuario)
    if not pendentes:
        return 0

    titulo_msg = f"{emoji} {label}"
    if tipo == "match":
        for m, ed in pendentes:
            nivel = "forte" if categoria == "forte" else m.nivel
            it = formato.item_edital(ed, nivel=nivel)
            tit, corpo, link = formato.telegram_item(titulo_msg, it)
            _tg.enviar_para_chat(chat_id, tit, corpo, botao_url=link)
            _marcar_visto(categoria, m)
    else:  # documento
        hoje = date.today()
        for d in pendentes:
            dias = (d.data_validade - hoje).days
            situacao = f"VENCIDO há {abs(dias)} dia(s)" if dias < 0 else f"vence em {dias} dia(s)"
            corpo = (f"Emissor: {d.orgao_emissor or '-'}\nValidade: {d.data_validade} ({situacao})"
                    + (f"\nObs.: {d.observacao}" if d.observacao else ""))
            _tg.enviar_para_chat(chat_id, f"{emoji} {d.nome}", corpo,
                                 botao_url=d.link or None, botao_texto="Abrir documento")
            _marcar_visto(categoria, d)

    db.commit()
    return len(pendentes)
