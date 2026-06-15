"""Notificações por e-mail (SMTP) e Telegram."""
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

from ..config import settings

log = logging.getLogger("notificacoes")


def _dias_restantes(data_enc) -> str:
    if not data_enc:
        return "prazo não informado"
    from datetime import date
    dias = (data_enc - date.today()).days
    if dias < 0:
        return "encerrado"
    return f"faltam {dias} dia(s)"


def montar_mensagem(edital, match) -> tuple[str, str]:
    titulo = f"[{match.nivel.upper()}] {edital.orgao or 'Órgão'} — {edital.uf or ''}"
    valor = f"R$ {edital.valor_estimado:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") \
        if edital.valor_estimado else "não informado"
    corpo = (
        f"Edital compatível encontrado ({edital.fonte})\n\n"
        f"Órgão: {edital.orgao}\n"
        f"Objeto: {(edital.objeto or '')[:400]}\n"
        f"Modalidade: {edital.modalidade}\n"
        f"Local: {edital.municipio or ''}/{edital.uf or ''}\n"
        f"Valor estimado: {valor}\n"
        f"Itens compatíveis: {match.itens_compativeis}\n"
        f"Pontuação: {match.score} ({match.nivel})\n"
        f"Encerramento das propostas: {edital.data_encerramento} ({_dias_restantes(edital.data_encerramento)})\n"
        f"Link: {edital.link}\n"
    )
    return titulo, corpo


def enviar_email(assunto: str, corpo: str) -> bool:
    if not (settings.SMTP_HOST and settings.NOTIFICAR_EMAIL):
        log.info("E-mail não configurado; pulando.")
        return False
    try:
        msg = MIMEMultipart()
        msg["Subject"] = assunto
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


def enviar_telegram(texto: str) -> bool:
    if not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID):
        return False
    try:
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": settings.TELEGRAM_CHAT_ID,
            "text": texto,
            "disable_web_page_preview": False,
        }, timeout=20)
        return r.status_code == 200
    except Exception as e:
        log.warning("Falha ao enviar Telegram: %s", e)
        return False


def notificar(edital, match) -> bool:
    """Dispara e-mail e Telegram. Retorna True se ao menos um foi enviado."""
    titulo, corpo = montar_mensagem(edital, match)
    ok_email = enviar_email(titulo, corpo)
    ok_tg = enviar_telegram(f"{titulo}\n\n{corpo}")
    return ok_email or ok_tg
