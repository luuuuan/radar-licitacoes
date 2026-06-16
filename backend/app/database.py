"""Conexão com o banco e criação de tabelas."""
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .config import settings
from .models import Base


def _sanitizar_url(url: str) -> str:
    """Remove parâmetros que o psycopg2 não entende (ex.: pgbouncer, connection_limit),
    comuns em strings do pooler do Supabase copiadas da aba 'ORM'/transação."""
    incompativeis = {"pgbouncer", "connection_limit"}
    partes = urlsplit(url)
    if partes.query:
        mantidos = [(k, v) for k, v in parse_qsl(partes.query, keep_blank_values=True)
                    if k not in incompativeis]
        url = urlunsplit((partes.scheme, partes.netloc, partes.path,
                          urlencode(mantidos), partes.fragment))
    return url


engine = create_engine(
    _sanitizar_url(settings.DATABASE_URL),
    pool_pre_ping=True,   # evita conexões mortas no pooler do Supabase
    pool_recycle=1800,    # recicla conexões a cada 30 min
)
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
    _migrar_colunas_novas()


# Colunas adicionadas após a 1ª versão. Como o create_all não altera tabelas
# existentes, garantimos que elas existam (idempotente) a cada subida.
_COLUNAS_NOVAS = {
    "produtos": [
        ("preco_custo", "DOUBLE PRECISION"),
        ("preco_venda", "DOUBLE PRECISION"),
        ("fornecedor_nome", "VARCHAR(160)"),
        ("fornecedor_contato", "VARCHAR(160)"),
        ("fornecedor_site", "VARCHAR(255)"),
    ],
}


def _migrar_colunas_novas() -> None:
    eh_sqlite = engine.url.get_backend_name() == "sqlite"
    with engine.connect() as conn:
        for tabela, colunas in _COLUNAS_NOVAS.items():
            for nome, tipo in colunas:
                try:
                    if eh_sqlite:
                        # SQLite não tem IF NOT EXISTS para coluna; ignora se já existe
                        conn.execute(text(f'ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}'))
                    else:
                        conn.execute(text(
                            f'ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS {nome} {tipo}'
                        ))
                    conn.commit()
                except Exception:
                    conn.rollback()  # coluna já existe ou banco não suporta


def get_session():
    """Dependency do FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
