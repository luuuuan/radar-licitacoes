"""
Testes do seletor experimental de modelo do reranker em POST /api/recalcular
— restrito a uma lista fechada de contas (_USUARIOS_SELETOR_RERANKER) e de
modelos válidos (_MODELOS_RERANKER_PERMITIDOS), pra não virar um jeito de
qualquer usuário mandar string arbitrária pra URL da DeepInfra. Chama a
função da rota direto, sem HTTP (mesmo padrão dos outros testes de main.py).
Rode com:  cd backend && pytest
"""
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from app.main import recalcular, _USUARIOS_SELETOR_RERANKER, _recalculo_locks, _recalculo_status


def _usuario(id):
    return SimpleNamespace(id=id)


@pytest.fixture(autouse=True)
def _limpa_estado_global():
    """Os dicts de trava/status de recálculo são globais por usuário — limpa
    antes/depois de cada teste pra um teste não vazar estado pro outro."""
    _recalculo_locks.clear()
    _recalculo_status.clear()
    yield
    _recalculo_locks.clear()
    _recalculo_status.clear()


def test_usuario_na_lista_com_modelo_valido_e_repassado():
    assert 5 in _USUARIOS_SELETOR_RERANKER   # premissa do teste
    bt = BackgroundTasks()
    recalcular(bt, com_ia=True, modelo_reranker="Qwen/Qwen3-Reranker-8B", user=_usuario(5))
    args = bt.tasks[0].args
    assert args == (5, None, "Qwen/Qwen3-Reranker-8B")


def test_usuario_fora_da_lista_modelo_e_ignorado():
    outro_id = max(_USUARIOS_SELETOR_RERANKER) + 1000
    bt = BackgroundTasks()
    recalcular(bt, com_ia=True, modelo_reranker="Qwen/Qwen3-Reranker-8B", user=_usuario(outro_id))
    args = bt.tasks[0].args
    assert args == (outro_id, None, None)


def test_modelo_fora_da_lista_permitida_e_ignorado_mesmo_pra_usuario_liberado():
    bt = BackgroundTasks()
    recalcular(bt, com_ia=True, modelo_reranker="algum-modelo-arbitrario", user=_usuario(5))
    args = bt.tasks[0].args
    assert args == (5, None, None)


def test_sem_modelo_reranker_passa_none():
    bt = BackgroundTasks()
    recalcular(bt, com_ia=True, modelo_reranker=None, user=_usuario(5))
    args = bt.tasks[0].args
    assert args == (5, None, None)


def test_gemini_e_um_provedor_permitido_pra_usuario_liberado():
    bt = BackgroundTasks()
    recalcular(bt, com_ia=True, modelo_reranker="gemini", user=_usuario(5))
    args = bt.tasks[0].args
    assert args == (5, None, "gemini")


def test_gemini_ignorado_pra_usuario_fora_da_lista():
    outro_id = max(_USUARIOS_SELETOR_RERANKER) + 1000
    bt = BackgroundTasks()
    recalcular(bt, com_ia=True, modelo_reranker="gemini", user=_usuario(outro_id))
    args = bt.tasks[0].args
    assert args == (outro_id, None, None)


def test_erro_inesperado_no_recalculo_nao_quebra_com_nameerror(monkeypatch):
    """Achado real: o bloco `except` de _rodar_recalculo_bg chamava um `log`
    que nunca existiu nesse módulo (main.py sempre usou
    logging.getLogger(nome) local) — uma exceção inesperada durante o
    recálculo derrubava o PRÓPRIO except com NameError, mascarando o erro
    original e (mais grave) pulando o finally que libera a trava."""
    from app.main import _rodar_recalculo_bg, _lock_recalculo
    import app.service as service_module

    def _explode(*a, **k):
        raise RuntimeError("falha simulada no recálculo")
    monkeypatch.setattr(service_module, "recalcular_matches", _explode)

    usuario_id = 987654
    _rodar_recalculo_bg(usuario_id)   # não pode levantar NameError

    assert "falha simulada no recálculo" in _recalculo_status[usuario_id]["erro"]
    assert not _lock_recalculo(usuario_id).locked()   # trava foi liberada mesmo com erro
