"""
Achado real: itens_edital.edital_id nunca teve índice, apesar de ser a
coluna mais lida da tabela (detalhe do edital, cotação, comparação por IA,
busca por item...) — cada consulta fazia uma varredura completa. Como
create_all() não altera tabela já existente, um banco que já tinha
itens_edital criado antes desta mudança só ganha o índice através da
migração manual (_migrar_indices_novos). Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine, text

from app import database as db_module
from app.models import Base


def _engine_sem_indice_novo():
    """Simula um banco criado ANTES desta mudança: tabela itens_edital já
    existe (via create_all na versão atual do modelo, que já tem
    index=True), mas sem o índice — pra isolar o teste do estado real do
    modelo, dropamos o índice logo em seguida."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_itens_edital_edital_id"))
        conn.commit()
    return engine


def _tem_indice(engine, nome):
    with engine.connect() as conn:
        linhas = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='itens_edital'"
        )).fetchall()
    return nome in {r[0] for r in linhas}


def test_migrar_indices_cria_indice_de_edital_id_quando_faltando(monkeypatch):
    engine = _engine_sem_indice_novo()
    monkeypatch.setattr(db_module, "engine", engine)
    assert not _tem_indice(engine, "ix_itens_edital_edital_id")

    db_module._migrar_indices_novos()

    assert _tem_indice(engine, "ix_itens_edital_edital_id")


def test_migrar_indices_e_idempotente(monkeypatch):
    """Rodar de novo (ex.: próximo deploy) não pode falhar nem duplicar."""
    engine = _engine_sem_indice_novo()
    monkeypatch.setattr(db_module, "engine", engine)

    db_module._migrar_indices_novos()
    db_module._migrar_indices_novos()   # não deve levantar exceção

    assert _tem_indice(engine, "ix_itens_edital_edital_id")


def test_migrar_indices_pula_trigram_no_sqlite(monkeypatch):
    """pg_trgm só existe no Postgres — no sqlite (dev/testes) a migração
    não deve nem tentar, só o índice comum de edital_id."""
    engine = _engine_sem_indice_novo()
    monkeypatch.setattr(db_module, "engine", engine)

    db_module._migrar_indices_novos()   # não deve levantar exceção

    with engine.connect() as conn:
        linhas = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='itens_edital'"
        )).fetchall()
    assert not any("trgm" in r[0] for r in linhas)
