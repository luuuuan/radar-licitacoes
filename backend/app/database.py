"""Conexão com o banco e criação de tabelas."""
import logging
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .config import settings
from .models import Base

log = logging.getLogger("database")


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
    "matches": [
        ("prazo_avisado", "BOOLEAN DEFAULT FALSE"),
        ("status", "VARCHAR(20) DEFAULT 'novo'"),
    ],
    "editais": [
        ("analise_ia", "TEXT"),
        ("analise_em", "TIMESTAMP"),
    ],
    "produtos_user": [("usuario_id", "INTEGER")],
}
# adiciona usuario_id às tabelas que passam a ser por-usuário
for _t in ("produtos", "matches", "documentos", "regras_exclusao", "propostas"):
    _COLUNAS_NOVAS.setdefault(_t, [])
    if ("usuario_id", "INTEGER") not in _COLUNAS_NOVAS[_t]:
        _COLUNAS_NOVAS[_t].append(("usuario_id", "INTEGER"))
_COLUNAS_NOVAS.pop("produtos_user", None)
_COLUNAS_NOVAS["usuarios"] = [
    ("telegram_codigo", "VARCHAR(32)"),
    ("avisar_abertura", "BOOLEAN DEFAULT TRUE"),
    ("dias_antecedencia", "INTEGER DEFAULT 2"),
]
_COLUNAS_NOVAS.setdefault("matches", [])
if ("abertura_avisada", "BOOLEAN DEFAULT FALSE") not in _COLUNAS_NOVAS["matches"]:
    _COLUNAS_NOVAS["matches"].append(("abertura_avisada", "BOOLEAN DEFAULT FALSE"))
_COLUNAS_NOVAS.setdefault("logs_coleta", [])
if ("usuario_id", "INTEGER") not in _COLUNAS_NOVAS["logs_coleta"]:
    _COLUNAS_NOVAS["logs_coleta"].append(("usuario_id", "INTEGER"))
_COLUNAS_NOVAS.setdefault("documentos", [])
if ("link", "VARCHAR(500)") not in _COLUNAS_NOVAS["documentos"]:
    _COLUNAS_NOVAS["documentos"].append(("link", "VARCHAR(500)"))
_COLUNAS_NOVAS.setdefault("produtos", [])
for _c in (("unidade_venda", "VARCHAR(20)"), ("itens_por_unidade", "FLOAT")):
    if _c not in _COLUNAS_NOVAS["produtos"]:
        _COLUNAS_NOVAS["produtos"].append(_c)


def _migrar_colunas_novas() -> None:
    """Migração leve: garante que colunas adicionadas após a 1ª versão existam.
    Não substitui um Alembic completo, mas é rastreável (loga o que adiciona) e
    suficiente para um projeto single-tenant. Se o schema crescer muito, migrar
    para Alembic é o próximo passo natural."""
    eh_sqlite = engine.url.get_backend_name() == "sqlite"
    with engine.connect() as conn:
        for tabela, colunas in _COLUNAS_NOVAS.items():
            for nome, tipo in colunas:
                try:
                    if eh_sqlite:
                        conn.execute(text(f'ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}'))
                    else:
                        conn.execute(text(
                            f'ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS {nome} {tipo}'
                        ))
                    conn.commit()
                    log.info("Migração: coluna %s.%s garantida", tabela, nome)
                except Exception as e:
                    conn.rollback()
                    msg = str(e).lower()
                    # silencioso só quando a coluna já existe; o resto é logado
                    if "exist" not in msg and "duplicate" not in msg:
                        log.warning("Migração %s.%s falhou: %s", tabela, nome, e)

        # Multiusuário: a unicidade de matches passa a ser (usuario_id, edital_id).
        # Remove a restrição antiga (só edital_id) e cria a nova, no Postgres.
        if not eh_sqlite:
            for sql in (
                "ALTER TABLE matches DROP CONSTRAINT IF EXISTS matches_edital_id_key",
                "ALTER TABLE matches ADD CONSTRAINT uq_match_user_edital "
                "UNIQUE (usuario_id, edital_id)",
            ):
                try:
                    conn.execute(text(sql))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    if "exist" not in str(e).lower() and "duplicate" not in str(e).lower():
                        log.warning("Migração de constraint de matches: %s", e)


def get_session():
    """Dependency do FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
