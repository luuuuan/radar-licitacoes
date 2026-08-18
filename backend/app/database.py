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
    _migrar_indices_novos()


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
        ("itens_completados_em", "TIMESTAMP"),
        ("itens_completados_qtd", "INTEGER DEFAULT 0"),
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
    ("token_reset_senha", "VARCHAR(128)"),
    ("token_reset_expira", "TIMESTAMP"),
    ("recalculo_checkpoint_edital_id", "INTEGER"),
    ("recalculo_checkpoint_coletado_em", "TIMESTAMP"),
    ("recalculo_checkpoint_em", "TIMESTAMP"),
    ("dados_empresa_cifrado", "TEXT"),
    ("logo_base64", "TEXT"),
    ("versao_catalogo", "INTEGER DEFAULT 0"),
    ("versao_documentos", "INTEGER DEFAULT 0"),
    ("telegram_chat_id_2", "VARCHAR(64)"),
    ("telegram_codigo_2", "VARCHAR(32)"),
]
_COLUNAS_NOVAS.setdefault("matches", [])
for _c in (("abertura_avisada", "BOOLEAN DEFAULT FALSE"),
          ("prazo_avisado_telegram", "BOOLEAN DEFAULT FALSE"),
          ("abertura_avisada_telegram", "BOOLEAN DEFAULT FALSE")):
    if _c not in _COLUNAS_NOVAS["matches"]:
        _COLUNAS_NOVAS["matches"].append(_c)
_COLUNAS_NOVAS.setdefault("logs_coleta", [])
if ("usuario_id", "INTEGER") not in _COLUNAS_NOVAS["logs_coleta"]:
    _COLUNAS_NOVAS["logs_coleta"].append(("usuario_id", "INTEGER"))
if ("origem", "VARCHAR(10)") not in _COLUNAS_NOVAS["logs_coleta"]:
    _COLUNAS_NOVAS["logs_coleta"].append(("origem", "VARCHAR(10)"))
_COLUNAS_NOVAS.setdefault("documentos", [])
for _c in (("link", "VARCHAR(500)"), ("avisado_para_telegram", "DATE"), ("texto_extraido", "TEXT")):
    if _c not in _COLUNAS_NOVAS["documentos"]:
        _COLUNAS_NOVAS["documentos"].append(_c)
_COLUNAS_NOVAS.setdefault("produtos", [])
for _c in (("unidade_venda", "VARCHAR(20)"), ("itens_por_unidade", "FLOAT"),
           ("fornecedor_id", "INTEGER"), ("fabricante", "VARCHAR(160)"),
           ("marca", "VARCHAR(160)"), ("modelo", "VARCHAR(160)")):
    if _c not in _COLUNAS_NOVAS["produtos"]:
        _COLUNAS_NOVAS["produtos"].append(_c)
_COLUNAS_NOVAS.setdefault("itens_edital", [])
if ("unidade_medida", "VARCHAR(60)") not in _COLUNAS_NOVAS["itens_edital"]:
    _COLUNAS_NOVAS["itens_edital"].append(("unidade_medida", "VARCHAR(60)"))


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


def _migrar_indices_novos() -> None:
    """Índices adicionados após a 1ª versão (mesma lógica de _migrar_colunas_novas,
    mas pra CREATE INDEX — create_all() não mexe em tabela já existente, então
    uma coluna que ganha index=True no modelo não fica indexada sozinha em quem
    já tinha o banco criado antes). CREATE INDEX IF NOT EXISTS já é idempotente
    nos dois bancos (sqlite e postgres), sem precisar de branch por dialeto
    como em _migrar_colunas_novas.

    Achado real: itens_edital.edital_id nunca teve índice — é lido em quase
    toda tela de edital (detalhe, cotação, comparação por IA) e é o coração
    da busca por item (GET /api/editais?busca_item=...), que fazia um EXISTS
    correlacionado nessa tabela sem nenhum índice de apoio: varredura
    completa da tabela a cada consulta."""
    eh_sqlite = engine.url.get_backend_name() == "sqlite"
    indices = [
        "CREATE INDEX IF NOT EXISTS ix_itens_edital_edital_id ON itens_edital (edital_id)",
    ]
    with engine.connect() as conn:
        for sql in indices:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception as e:
                conn.rollback()
                log.warning("Migração de índice falhou (%s): %s", sql, e)

        if not eh_sqlite:
            # Busca por item usa LIKE '%termo%' (curinga no início — um índice
            # comum não ajuda nesse padrão). pg_trgm com GIN faz esse tipo de
            # busca por substring ser rápido de verdade; só existe no Postgres,
            # por isso fora do loop acima (sqlite do dev/testes não precisa,
            # o volume de dados local é pequeno o bastante pra não importar).
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                conn.commit()
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_itens_edital_descricao_trgm "
                    "ON itens_edital USING gin (lower(descricao) gin_trgm_ops)"
                ))
                conn.commit()
                log.info("Migração: índice trigram de itens_edital.descricao garantido")
            except Exception as e:
                conn.rollback()
                log.warning("Migração do índice trigram (pg_trgm) falhou: %s", e)


def get_session():
    """Dependency do FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
