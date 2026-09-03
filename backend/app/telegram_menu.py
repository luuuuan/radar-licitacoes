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

Cada uma dessas flags do Telegram, por sua vez, existe em PAR — uma pro
contato principal (Usuario.telegram_chat_id) e uma pro 2º contato
(Usuario.telegram_chat_id_2, ex.: sócio) -- ver "slot" nas funções abaixo.
Achado real: antes de existir esse par, o 1º contato a tocar num botão
marcava o item como visto pra conta inteira, e o outro contato tocando no
MESMO botão via a lista vazia — o item nunca chegava até ele de verdade,
mesmo ele nunca tendo recebido nada.
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


def _slot_do_chat(usuario: Usuario, chat_id: str) -> int:
    """1 = contato principal, 2 = contato adicional. Resolve a partir do
    chat_id que efetivamente tocou no botão (quem chama já achou o Usuario
    a partir desse mesmo chat_id, então sempre bate com um dos dois)."""
    return 2 if chat_id and chat_id == usuario.telegram_chat_id_2 else 1


def _pendentes_forte(db: Session, usuario: Usuario, slot: int = 1) -> list[tuple[Match, Edital]]:
    campo = Match.notificado if slot == 1 else Match.notificado_2
    q = (select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
         .where(Match.usuario_id == usuario.id, Match.nivel == "forte",
                campo == False))  # noqa: E712
    return db.execute(q).all()


def _pendentes_prazo(db: Session, usuario: Usuario, slot: int = 1) -> list[tuple[Match, Edital]]:
    hoje = date.today()
    campo = Match.prazo_avisado_telegram if slot == 1 else Match.prazo_avisado_telegram_2
    q = (select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
         .where(Match.usuario_id == usuario.id, campo == False)  # noqa: E712
         .where(Edital.data_encerramento.is_not(None))
         .where(or_(Match.interessante == True, Match.nivel == "forte")))  # noqa: E712
    resultado = []
    for m, ed in db.execute(q).all():
        dias = (ed.data_encerramento - hoje).days
        if 0 <= dias <= settings.LEMBRETE_PRAZO_DIAS:
            resultado.append((m, ed))
    return resultado


def _pendentes_abertura(db: Session, usuario: Usuario, slot: int = 1) -> list[tuple[Match, Edital]]:
    if not usuario.avisar_abertura:
        return []
    hoje = date.today()
    campo = Match.abertura_avisada_telegram if slot == 1 else Match.abertura_avisada_telegram_2
    q = (select(Match, Edital).join(Edital, Match.edital_id == Edital.id)
         .where(Match.usuario_id == usuario.id, campo == False,  # noqa: E712
                Match.nivel == "forte")
         .where(Edital.data_abertura.is_not(None), Edital.data_abertura >= hoje))
    resultado = []
    for m, ed in db.execute(q).all():
        dias = (ed.data_abertura - hoje).days
        if dias <= max(0, usuario.dias_antecedencia):
            resultado.append((m, ed))
    return resultado


def _pendentes_documento(db: Session, usuario: Usuario, slot: int = 1) -> list[Documento]:
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
        avisado = d.avisado_para_telegram if slot == 1 else d.avisado_para_telegram_2
        if avisado == d.data_validade:
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


def _marcar_visto(categoria: str, registro, slot: int = 1) -> None:
    sufixo = "" if slot == 1 else "_2"
    if categoria == "forte":
        setattr(registro, f"notificado{sufixo}", True)
    elif categoria == "prazo":
        setattr(registro, f"prazo_avisado_telegram{sufixo}", True)
    elif categoria == "abertura":
        setattr(registro, f"abertura_avisada_telegram{sufixo}", True)
    elif categoria == "documento":
        setattr(registro, f"avisado_para_telegram{sufixo}", registro.data_validade)


def contar_pendentes(db: Session, usuario: Usuario, slot: int = 1) -> dict[str, int]:
    return {chave: len(buscar(db, usuario, slot)) for chave, (_, _, buscar, _) in CATEGORIAS.items()}


def enviar_resumo(db: Session, usuario: Usuario) -> bool:
    """Manda o menu com botões (uma linha por categoria com pendência) pra
    cada contato de Telegram vinculado a este usuário — com a CONTAGEM
    própria daquele contato (um pode já ter visto mais categorias que o
    outro). Não manda nada pra um contato sem nada pendente pra ele."""
    if not usuario.notif_telegram:
        return False
    enviou = False
    for slot, chat_id in ((1, usuario.telegram_chat_id), (2, usuario.telegram_chat_id_2)):
        if not chat_id:
            continue
        contagens = contar_pendentes(db, usuario, slot)
        pendentes = {k: v for k, v in contagens.items() if v > 0}
        if not pendentes:
            continue
        botoes = [(f"{CATEGORIAS[k][0]} {CATEGORIAS[k][1]} ({v})", f"radar:{k}")
                 for k, v in pendentes.items()]
        total = sum(pendentes.values())
        titulo = f"📋 Você tem {total} aviso(s) novo(s)"
        corpo = "Escolha o que quer ver agora:"
        enviou = _tg.enviar_menu(chat_id, titulo, corpo, botoes) or enviou
    return enviou


def mostrar_categoria(db: Session, usuario: Usuario, categoria: str, chat_id: str) -> int:
    """Envia os editais/documentos pendentes daquela categoria (uma mensagem
    por item, como já era) e marca todos como vistos PARA ESTE CONTATO.
    Retorna quantos enviou. Chamado a partir do toque no botão
    (callback_query) -- `chat_id` é o chat que TOCOU (pode ser o 1º ou o 2º
    contato do usuário); manda a resposta pra ele especificamente, não pra
    um contato fixo, senão o 2º contato tocando no botão faria a resposta
    ir pro 1º."""
    cfg = CATEGORIAS.get(categoria)
    if not cfg:
        return 0
    slot = _slot_do_chat(usuario, chat_id)
    emoji, label, buscar, tipo = cfg
    pendentes = buscar(db, usuario, slot)
    if not pendentes:
        return 0

    titulo_msg = f"{emoji} {label}"
    enviados = 0
    if tipo == "match":
        for m, ed in pendentes:
            nivel = "forte" if categoria == "forte" else m.nivel
            it = formato.item_edital(ed, nivel=nivel)
            tit, corpo, link = formato.telegram_item(titulo_msg, it)
            # só marca como visto se o envio realmente deu certo -- achado
            # real: uma falha passageira do Telegram (rate limit, rede,
            # usuário bloqueou o bot) marcava o item como notificado do
            # mesmo jeito, e ele nunca mais era oferecido de novo (não tem
            # retry) mesmo sem ter sido entregue de verdade.
            if _tg.enviar_para_chat(chat_id, tit, corpo, botao_url=link):
                _marcar_visto(categoria, m, slot)
                enviados += 1
    else:  # documento
        hoje = date.today()
        for d in pendentes:
            dias = (d.data_validade - hoje).days
            situacao = f"VENCIDO há {abs(dias)} dia(s)" if dias < 0 else f"vence em {dias} dia(s)"
            # _esc (mesmo escape usado na formatação de match, formato.py) --
            # achado real: esse ramo montava o corpo em HTML (enviar_para_chat
            # usa parse_mode="HTML") sem escapar nome/emissor/observação, que
            # são texto livre digitado pelo usuário -- um "<"/">"/"&" aí
            # quebra o parse do Telegram (400), e o envio falha mesmo sendo
            # um erro nosso, não do Telegram.
            corpo = (f"Emissor: {formato._esc(d.orgao_emissor) or '-'}\n"
                    f"Validade: {d.data_validade} ({situacao})"
                    + (f"\nObs.: {formato._esc(d.observacao)}" if d.observacao else ""))
            if _tg.enviar_para_chat(chat_id, f"{emoji} {formato._esc(d.nome)}", corpo,
                                    botao_url=d.link or None, botao_texto="Abrir documento"):
                _marcar_visto(categoria, d, slot)
                enviados += 1

    db.commit()
    return enviados
