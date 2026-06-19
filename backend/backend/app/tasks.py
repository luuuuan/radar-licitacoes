"""Configuração do Celery + agendamento da coleta diária (Celery Beat)."""
from celery import Celery
from celery.schedules import crontab

from .config import settings

celery = Celery("radar", broker=settings.REDIS_URL, backend=settings.REDIS_URL)
celery.conf.timezone = "America/Sao_Paulo"

# Coleta automática todo dia às 06:00 (horário de Brasília)
celery.conf.beat_schedule = {
    "coleta-diaria": {
        "task": "app.tasks.coleta_diaria",
        "schedule": crontab(hour=6, minute=0),
    },
}


@celery.task(name="app.tasks.coleta_diaria")
def coleta_diaria():
    """Tarefa agendada: roda a coleta completa em todos os conectores ativos."""
    from .database import SessionLocal, init_db
    from .service import processar_coleta
    init_db()
    db = SessionLocal()
    try:
        return processar_coleta(db)
    finally:
        db.close()
