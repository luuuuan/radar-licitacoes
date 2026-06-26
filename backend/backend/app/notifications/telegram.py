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
