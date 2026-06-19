"""Canal de notificação por e-mail (SMTP)."""
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from .base import BaseNotifier
from ..config import settings

log = logging.getLogger("notificacoes.email")


class EmailNotifier(BaseNotifier):
    nome = "email"

    def disponivel(self) -> bool:
        return bool(settings.SMTP_HOST and settings.NOTIFICAR_EMAIL)

    def enviar(self, titulo: str, corpo: str) -> bool:
        if not self.disponivel():
            return False
        try:
            msg = MIMEMultipart()
            msg["Subject"] = titulo
            msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
            msg["To"] = settings.NOTIFICAR_EMAIL
            msg.attach(MIMEText(corpo, "plain", "utf-8"))
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as s:
                s.starttls()
                if settings.SMTP_USER:
                    s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                s.send_message(msg)
            return True
        except Exception as e:
            log.warning("Falha ao enviar e-mail: %s", e)
            return False
