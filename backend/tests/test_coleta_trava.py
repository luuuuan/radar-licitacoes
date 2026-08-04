"""
Testes da autoliberação da trava de coleta quando fica presa (achado real:
uma coleta manual travou sem nunca liberar, e bloqueou TODAS as coletas
automáticas do cron silenciosamente por horas — o disparo do GitHub Actions
continuava recebendo 200, sem nenhum jeito de saber que nada rodou de
verdade). Rode com:  cd backend && pytest
"""
from datetime import timedelta
from unittest.mock import MagicMock

from app import main as m


def _limpar_estado_trava():
    m._coleta_iniciada_em = None
    if m._coleta_lock.locked():
        m._coleta_lock.release()


def test_coleta_travada_falso_quando_nao_ha_coleta_rodando():
    _limpar_estado_trava()
    assert m._coleta_travada() is False


def test_coleta_travada_falso_quando_recente():
    _limpar_estado_trava()
    m._coleta_lock.acquire()
    try:
        m._coleta_iniciada_em = m._utcnow_main()
        assert m._coleta_travada() is False
    finally:
        _limpar_estado_trava()


def test_coleta_travada_true_quando_passou_do_limiar():
    _limpar_estado_trava()
    m._coleta_lock.acquire()
    try:
        m._coleta_iniciada_em = m._utcnow_main() - m._LIMITE_COLETA_TRAVADA - timedelta(minutes=1)
        assert m._coleta_travada() is True
    finally:
        _limpar_estado_trava()


def test_rodar_coleta_bg_forca_liberacao_de_trava_presa_e_roda_normalmente(monkeypatch):
    """Simula uma coleta travada há 4h segurando a trava (processo anterior
    morreu sem passar pelo finally) — um novo disparo tem que detectar isso,
    forçar a liberação, e rodar normalmente em seguida (não ficar bloqueado
    pra sempre)."""
    _limpar_estado_trava()
    m._coleta_lock.acquire()
    m._coleta_iniciada_em = m._utcnow_main() - timedelta(hours=4)

    mock_db = MagicMock()
    mock_db.get.return_value = None   # usuário "não encontrado" -> alvos=[] (sem telegram)
    chamou_processar = {"n": 0}

    def _processar_falso(db, usuario_id=None, deve_cancelar=None):
        chamou_processar["n"] += 1

    monkeypatch.setattr(m, "SessionLocal", lambda: mock_db)
    monkeypatch.setattr(m, "processar_coleta", _processar_falso)
    monkeypatch.setattr("app.lembretes.verificar_todos", lambda db: None)

    m._rodar_coleta_bg(usuario_id=99)

    assert chamou_processar["n"] == 1, "deveria ter rodado a coleta nova, não ficado bloqueado"
    assert m._coleta_lock.locked() is False, "trava tem que ficar livre no final"
    assert m._coleta_iniciada_em is None


def test_rodar_coleta_bg_nao_roda_se_trava_esta_ocupada_de_verdade(monkeypatch):
    """Trava ocupada há pouco tempo (coleta legítima em andamento) — um novo
    disparo tem que ser ignorado, não forçar liberação."""
    _limpar_estado_trava()
    m._coleta_lock.acquire()
    m._coleta_iniciada_em = m._utcnow_main()   # começou agora mesmo

    chamou_processar = {"n": 0}
    monkeypatch.setattr(m, "processar_coleta", lambda *a, **kw: chamou_processar.__setitem__("n", chamou_processar["n"] + 1))

    m._rodar_coleta_bg(usuario_id=99)

    assert chamou_processar["n"] == 0, "não deveria ter rodado — a trava legítima ainda vale"
    assert m._coleta_lock.locked() is True, "não pode ter mexido na trava da coleta legítima"
    _limpar_estado_trava()
