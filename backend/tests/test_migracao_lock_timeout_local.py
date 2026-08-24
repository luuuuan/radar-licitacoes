"""
Achado real em produção: _migrar_colunas_novas/_migrar_indices_novos usavam
"SET lock_timeout = '5s'" (sem LOCAL) antes de cada ALTER TABLE/CREATE
INDEX, pra não travar o startup esperando um lock (ver comentário no
código). Só que "SET" sem LOCAL vale pra SESSÃO inteira, não só pra
transação atual -- e como a conexão volta pro pool do SQLAlchemy depois
(sem RESET), o limite de 5s vazava pra qualquer requisição futura,
completamente sem relação, que pegasse essa mesma conexão emprestada.
Horas depois de um deploy, um SELECT trivial em /api/auth/me começou a
morrer com "canceling statement due to lock timeout".

Só dá pra testar a semântica SQL (SET vs SET LOCAL) de verdade contra um
Postgres real -- aqui simulamos a conexão (mock) e conferimos que todo
comando de lock_timeout emitido usa LOCAL, nunca a forma que vaza pra
sessão. Rode com:  cd backend && pytest
"""
from unittest.mock import MagicMock

from app import database as db_module


class _ConexaoFalsa:
    """Registra os comandos executados sem precisar de um Postgres de
    verdade -- execute/commit/rollback nunca falham, então o código sob
    teste passa por TODAS as iterações (nenhuma cai no "except")."""
    def __init__(self):
        self.comandos: list[str] = []

    def execute(self, clausula, *args, **kwargs):
        self.comandos.append(str(clausula))
        return MagicMock()

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _EngineFalsa:
    def __init__(self, conexao):
        self._conexao = conexao
        self.url = MagicMock()
        self.url.get_backend_name.return_value = "postgresql"

    def connect(self):
        return self._conexao


def _comandos_de_lock_timeout(comandos: list[str]) -> list[str]:
    return [c for c in comandos if "lock_timeout" in c]


def test_migrar_colunas_novas_so_usa_set_local(monkeypatch):
    conexao = _ConexaoFalsa()
    monkeypatch.setattr(db_module, "engine", _EngineFalsa(conexao))

    db_module._migrar_colunas_novas()

    comandos_timeout = _comandos_de_lock_timeout(conexao.comandos)
    assert comandos_timeout, "esperava pelo menos 1 SET de lock_timeout (postgres, colunas novas existem)"
    for c in comandos_timeout:
        assert c.strip().upper().startswith("SET LOCAL"), (
            f"vazamento de sessão: {c!r} não usa LOCAL -- fica valendo além desta "
            "transação, contaminando a próxima requisição que reusar a conexão do pool"
        )


def test_migrar_indices_novos_so_usa_set_local(monkeypatch):
    conexao = _ConexaoFalsa()
    monkeypatch.setattr(db_module, "engine", _EngineFalsa(conexao))

    db_module._migrar_indices_novos()

    comandos_timeout = _comandos_de_lock_timeout(conexao.comandos)
    assert comandos_timeout, "esperava pelo menos 1 SET de lock_timeout (postgres, índices)"
    for c in comandos_timeout:
        assert c.strip().upper().startswith("SET LOCAL"), (
            f"vazamento de sessão: {c!r} não usa LOCAL -- fica valendo além desta "
            "transação, contaminando a próxima requisição que reusar a conexão do pool"
        )
