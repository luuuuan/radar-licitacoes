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
