"""Canal de notificação por e-mail (Resend via API ou SMTP)."""
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

from .base import BaseNotifier
from ..config import settings

log = logging.getLogger("notificacoes.email")


def _enviar_brevo(destinatario: str, titulo: str, corpo: str) -> bool:
    """Envia via API do Brevo (HTTPS, porta 443 — funciona no Render).
    Não exige domínio verificado para enviar a qualquer destinatário."""
    remetente = settings.BREVO_FROM_EMAIL or settings.SMTP_FROM or settings.SMTP_USER
    if not remetente:
        log.warning("Brevo: BREVO_FROM_EMAIL não configurado.")
        return False
    try:
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": settings.BREVO_API_KEY,
                     "Content-Type": "application/json", "accept": "application/json"},
            json={"sender": {"email": remetente, "name": settings.BREVO_FROM_NOME},
                  "to": [{"email": destinatario}],
                  "subject": titulo, "textContent": corpo},
            timeout=15,
        )
        if r.status_code in (200, 201):
            return True
        log.warning("Brevo recusou (%s): %s", r.status_code, r.text[:300])
        return False
    except requests.RequestException as e:
        log.warning("Falha ao enviar via Brevo para %s: %s", destinatario, e)
        return False


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
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
                s.starttls()
                if settings.SMTP_USER:
                    s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                s.send_message(msg)
            return True
        except Exception as e:
            log.warning("Falha ao enviar e-mail: %s", e)
            return False


def smtp_configurado() -> bool:
    """True se há ALGUM canal de e-mail configurado (Brevo ou SMTP)."""
    if settings.BREVO_API_KEY and (settings.BREVO_FROM_EMAIL or settings.SMTP_USER):
        return True
    return bool(settings.SMTP_HOST and (settings.SMTP_FROM or settings.SMTP_USER))


def enviar_para(destinatario: str, titulo: str, corpo: str) -> bool:
    """Envia um e-mail para um destinatário específico. Usa o Brevo (API HTTPS)
    quando configurado; senão, cai para o SMTP."""
    if not destinatario:
        return False
    # 1) Brevo (recomendado no Render)
    if settings.BREVO_API_KEY:
        return _enviar_brevo(destinatario, titulo, corpo)
    # 2) SMTP (pode não funcionar no Render free — porta bloqueada)
    if not (settings.SMTP_HOST and (settings.SMTP_FROM or settings.SMTP_USER)):
        return False
    try:
        msg = MIMEMultipart()
        msg["Subject"] = titulo
        msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
        msg["To"] = destinatario
        msg.attach(MIMEText(corpo, "plain", "utf-8"))
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
            s.starttls()
            if settings.SMTP_USER:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:
        log.warning("Falha ao enviar e-mail para %s: %s", destinatario, e)
        return False
