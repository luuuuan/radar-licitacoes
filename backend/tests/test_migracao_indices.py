"""
Achado real: itens_edital.edital_id nunca teve índice, apesar de ser a
coluna mais lida da tabela (detalhe do edital, cotação, comparação por IA,
busca por item...) — cada consulta fazia uma varredura completa. Como
create_all() não altera tabela já existente, um banco que já tinha
itens_edital criado antes desta mudança só ganha o índice através da
migração manual (_migrar_indices_novos). Rode com:  cd backend && pytest

Achado real #2 (auditoria do agente debugger): o mesmo problema vale pra
todo `usuario_id`/`fornecedor_id`/`telegram_codigo*` que virou "index=True"
no modelo DEPOIS que a tabela já existia (ver _COLUNAS_NOVAS em
database.py) -- um banco que já tinha produtos/matches/documentos/
regras_exclusao/propostas/usuarios antes dessas colunas existirem nunca
ganhou o índice sozinho.
"""
from sqlalchemy import create_engine, text

from app import database as db_module
from app.models import Base

_INDICES_NOVOS = [
    ("itens_edital", "ix_itens_edital_edital_id"),
    ("produtos", "ix_produtos_usuario_id"),
    ("produtos", "ix_produtos_fornecedor_id"),
    ("matches", "ix_matches_usuario_id"),
    ("documentos", "ix_documentos_usuario_id"),
    ("regras_exclusao", "ix_regras_exclusao_usuario_id"),
    ("propostas", "ix_propostas_usuario_id"),
    ("usuarios", "ix_usuarios_telegram_codigo"),
    ("usuarios", "ix_usuarios_telegram_codigo_2"),
]


def _engine_sem_indice_novo():
    """Simula um banco criado ANTES destas mudanças: as tabelas já existem
    (via create_all na versão atual do modelo, que já tem index=True), mas
    sem os índices -- pra isolar o teste do estado real do modelo, dropamos
    os índices logo em seguida."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        for _tabela, nome in _INDICES_NOVOS:
            conn.execute(text(f"DROP INDEX IF EXISTS {nome}"))
        conn.commit()
    return engine


def _tem_indice(engine, nome, tabela="itens_edital"):
    with engine.connect() as conn:
        linhas = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:tabela"
        ), {"tabela": tabela}).fetchall()
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


def test_migrar_indices_cria_todos_os_usuario_id_e_telegram_codigo(monkeypatch):
    """Achado do agente debugger: produtos/matches/documentos/regras_exclusao/
    propostas.usuario_id, produtos.fornecedor_id e usuarios.telegram_codigo(_2)
    ganharam index=True no modelo depois que as tabelas já existiam -- num
    banco que já tinha essas tabelas, o índice nunca aparecia sozinho."""
    engine = _engine_sem_indice_novo()
    monkeypatch.setattr(db_module, "engine", engine)
    for tabela, nome in _INDICES_NOVOS:
        assert not _tem_indice(engine, nome, tabela), f"{nome} já existia antes da migração"

    db_module._migrar_indices_novos()

    for tabela, nome in _INDICES_NOVOS:
        assert _tem_indice(engine, nome, tabela), f"{nome} não foi criado pela migração"
