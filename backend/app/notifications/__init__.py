"""
Notificações multi-canal.

Para adicionar um canal novo (WhatsApp, Slack, etc.), basta criar uma classe
que herda de BaseNotifier e registrá-la em NOTIFIERS abaixo. O notificar()
percorre todos os canais configurados.
"""
import logging
from datetime import date

from .base import BaseNotifier
from .email import EmailNotifier
from .telegram import TelegramNotifier

log = logging.getLogger("notificacoes")

# Canais disponíveis. Adicione novos aqui (ex.: WhatsAppNotifier()).
NOTIFIERS: list[BaseNotifier] = [EmailNotifier(), TelegramNotifier()]


def _fmt_data_hora(dt) -> str:
    """"2026-09-19 09:00:00" (str() puro de datetime) não é uma mensagem
    legível pro usuário -- pedido do usuário: mostrar a hora de início/fim
    de recebimento de propostas, mas formatada."""
    if not dt:
        return "não informado"
    return dt.strftime("%d/%m/%Y às %Hh%M")


def _dias_restantes(data_enc) -> str:
    if not data_enc:
        return "prazo não informado"
    # .date(): data_enc agora guarda hora (ver _parse_data_hora em
    # connectors/pncp.py) -- subtrair de date.today() direto quebraria com
    # TypeError; esta contagem é em DIAS, não precisa da hora exata.
    dias = (data_enc.date() - date.today()).days
    if dias < 0:
        return "encerrado"
    return f"faltam {dias} dia(s)"


def montar_mensagem(edital, match) -> tuple[str, str]:
    titulo = f"[{match.nivel.upper()}] {edital.orgao or 'Órgão'} — {edital.uf or ''}"
    if edital.valor_estimado:
        valor = f"R$ {edital.valor_estimado:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    else:
        valor = "não informado"
    corpo = (
        f"Edital compatível encontrado ({edital.fonte})\n\n"
        f"Órgão: {edital.orgao}\n"
        f"Objeto: {(edital.objeto or '')[:400]}\n"
        f"Modalidade: {edital.modalidade}\n"
        f"Local: {edital.municipio or ''}/{edital.uf or ''}\n"
        f"Valor estimado: {valor}\n"
        f"Itens compatíveis: {match.itens_compativeis}\n"
        f"Pontuação: {match.score} ({match.nivel})\n"
        f"Encerramento das propostas: {_fmt_data_hora(edital.data_encerramento)} "
        f"({_dias_restantes(edital.data_encerramento)})\n"
        f"Link: {edital.link}\n"
    )
    return titulo, corpo


def notificar(edital, match) -> bool:
    """Envia por todos os canais configurados. True se ao menos um enviou."""
    titulo, corpo = montar_mensagem(edital, match)
    enviou = False
    for canal in NOTIFIERS:
        if canal.disponivel() and canal.enviar(titulo, corpo):
            enviou = True
    return enviou


def enviar_aviso(titulo: str, corpo: str) -> bool:
    """Envio genérico (lembretes de prazo, documentos, etc.)."""
    enviou = False
    for canal in NOTIFIERS:
        if canal.disponivel() and canal.enviar(titulo, corpo):
            enviou = True
    return enviou
