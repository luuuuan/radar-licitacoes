"""Canal de notificação por Telegram."""
import logging
import requests

from .base import BaseNotifier
from ..config import settings

log = logging.getLogger("notificacoes.telegram")


class TelegramNotifier(BaseNotifier):
    nome = "telegram"

    def disponivel(self) -> bool:
        return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)

    def enviar(self, titulo: str, corpo: str) -> bool:
        if not self.disponivel():
            return False
        try:
            url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
            r = requests.post(url, json={
                "chat_id": settings.TELEGRAM_CHAT_ID,
                "text": f"{titulo}\n\n{corpo}",
                "disable_web_page_preview": False,
            }, timeout=20)
            return r.status_code == 200
        except Exception as e:
            log.warning("Falha ao enviar Telegram: %s", e)
            return False


def enviar_para_chat(chat_id: str, titulo: str, corpo: str,
                     botao_url: str | None = None, botao_texto: str = "Abrir edital") -> bool:
    """Envia uma mensagem para um chat específico (Telegram do próprio usuário).
    Aceita HTML (negrito etc.) e um botão opcional com link.
    Usa o bot global do sistema (TELEGRAM_BOT_TOKEN)."""
    if not (settings.TELEGRAM_BOT_TOKEN and chat_id):
        return False
    try:
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": f"{titulo}\n\n{corpo}" if titulo else corpo,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if botao_url:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": botao_texto, "url": botao_url}]]
            }
        r = requests.post(url, json=payload, timeout=20)
        return r.status_code == 200
    except Exception as e:
        log.warning("Falha ao enviar Telegram para %s: %s", chat_id, e)
        return False


def enviar_menu(chat_id: str, titulo: str, corpo: str, botoes: list[tuple[str, str]]) -> bool:
    """Mensagem com botões de CALLBACK (não de link) — ao tocar, o Telegram
    manda um `callback_query` pro webhook em vez de só abrir uma URL.
    `botoes`: lista de (texto_do_botão, callback_data). Cada botão numa
    linha própria (menu vertical, mais fácil de ler que lado a lado)."""
    if not (settings.TELEGRAM_BOT_TOKEN and chat_id):
        return False
    try:
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": f"{titulo}\n\n{corpo}" if titulo else corpo,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [[{"text": texto, "callback_data": dado}] for texto, dado in botoes]
            },
        }
        r = requests.post(url, json=payload, timeout=20)
        return r.status_code == 200
    except Exception as e:
        log.warning("Falha ao enviar menu Telegram para %s: %s", chat_id, e)
        return False


def responder_callback(callback_query_id: str, texto: str | None = None) -> None:
    """Confirma o toque no botão pro Telegram (senão o botão fica "carregando"
    pro usuário até dar timeout). Não precisa de resposta bem-sucedida — é
    só cosmético, uma falha aqui não deve derrubar o resto do fluxo."""
    if not (settings.TELEGRAM_BOT_TOKEN and callback_query_id):
        return
    try:
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id}
        if texto:
            payload["text"] = texto
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.warning("Falha ao responder callback Telegram: %s", e)
