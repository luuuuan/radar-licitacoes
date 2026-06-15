"""Conexão com o banco e criação de tabelas."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .config import settings
from .models import Base

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """Cria a extensão pgvector (se disponível) e todas as tabelas."""
    with engine.connect() as conn:
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        except Exception:
            # pgvector é opcional para o MVP (matching textual funciona sem ele)
            conn.rollback()
    Base.metadata.create_all(engine)


def get_session():
    """Dependency do FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
