"""
Testes da autoliberação da trava de coleta quando fica presa (achado real:
uma coleta manual travou sem nunca liberar, e bloqueou TODAS as coletas
automáticas do cron silenciosamente por horas — o disparo do GitHub Actions
continuava recebendo 200, sem nenhum jeito de saber que nada rodou de
verdade). Rode com:  cd backend && pytest
"""
from datetime import timedelta
from unittest.mock import MagicMock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main as m
from app.models import Base, LogColeta, Usuario


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


def test_forcar_liberacao_nao_faz_nada_quando_nao_esta_travada():
    _limpar_estado_trava()
    assert m._forcar_liberacao_coleta_travada() is False


def test_forcar_liberacao_libera_e_retorna_true_quando_travada():
    _limpar_estado_trava()
    m._coleta_lock.acquire()
    m._coleta_iniciada_em = m._utcnow_main() - m._LIMITE_COLETA_TRAVADA - timedelta(minutes=1)
    assert m._forcar_liberacao_coleta_travada() is True
    assert m._coleta_lock.locked() is False
    assert m._coleta_iniciada_em is None


def test_forcar_liberacao_fecha_logs_orfaos_quando_db_informado():
    _limpar_estado_trava()
    db = _sessao()
    db.add(LogColeta(usuario_id=1, fonte="coleta", origem="manual",
                     iniciado_em=m._utcnow_main() - timedelta(hours=4)))
    db.commit()
    m._coleta_lock.acquire()
    m._coleta_iniciada_em = m._utcnow_main() - m._LIMITE_COLETA_TRAVADA - timedelta(minutes=1)

    assert m._forcar_liberacao_coleta_travada(db) is True

    orfao = db.execute(select(LogColeta)).scalar_one()
    assert orfao.finalizado_em is not None
    assert orfao.erro == "interrompida (processo reiniciado antes de terminar)"


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
    mock_db.execute.return_value.scalars.return_value.all.return_value = []   # sem órfãos pra limpar
    chamou_processar = {"n": 0}

    def _processar_falso(db, usuario_id=None, deve_cancelar=None, progresso_fase=None):
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


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_limpar_logs_coleta_orfaos_fecha_registro_nunca_finalizado():
    """Achado real: 4 rodadas de coleta desde jul/2026 ficaram com
    finalizado_em nulo pra sempre (processo morto no meio) — sem fechar
    essas linhas, o indicador do dono daquele registro fica preso mostrando
    "travado" indefinidamente, mesmo após a trava em memória já ter sumido
    num redeploy."""
    db = _sessao()
    orfao = LogColeta(usuario_id=1, fonte="coleta", origem="manual",
                      iniciado_em=m._utcnow_main() - timedelta(hours=5))
    db.add(orfao)
    db.commit()

    m._limpar_logs_coleta_orfaos(db)

    db.refresh(orfao)
    assert orfao.finalizado_em is not None
    assert orfao.erro == "interrompida (processo reiniciado antes de terminar)"


def test_limpar_logs_coleta_orfaos_nao_mexe_em_registro_ja_finalizado():
    db = _sessao()
    fim = m._utcnow_main()
    ok = LogColeta(usuario_id=1, fonte="PNCP", origem="cron",
                   iniciado_em=fim - timedelta(minutes=30), finalizado_em=fim,
                   editais_novos=5, erro=None)
    db.add(ok)
    db.commit()

    m._limpar_logs_coleta_orfaos(db)

    db.refresh(ok)
    assert ok.finalizado_em == fim
    assert ok.erro is None


def test_rodar_coleta_bg_limpa_orfaos_de_rodada_anterior_antes_de_comecar(monkeypatch):
    """A limpeza de órfãos roda logo após conseguir a trava, no início de
    uma coleta nova de verdade — não só no caminho de trava presa."""
    _limpar_estado_trava()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    db = SessionFactory()
    orfao_id = None
    orfao = LogColeta(usuario_id=1, fonte="coleta", origem="manual",
                      iniciado_em=m._utcnow_main() - timedelta(hours=1))
    db.add(orfao)
    db.commit()
    orfao_id = orfao.id

    # _rodar_coleta_bg fecha a sessão que recebe (finally: db.close()) — usa
    # uma sessão nova pra cada chamada de SessionLocal(), do mesmo jeito que
    # o SessionLocal() de verdade faria, em vez de reusar um objeto fechado.
    monkeypatch.setattr(m, "SessionLocal", SessionFactory)
    monkeypatch.setattr(m, "processar_coleta", lambda *a, **kw: None)
    monkeypatch.setattr("app.lembretes.verificar_todos", lambda db: None)

    m._rodar_coleta_bg(usuario_id=99)

    db2 = SessionFactory()
    orfao_depois = db2.execute(select(LogColeta).where(LogColeta.id == orfao_id)).scalar_one()
    assert orfao_depois.finalizado_em is not None
    _limpar_estado_trava()


def _usuario(db, id_=1):
    u = Usuario(id=id_, nome="Teste", email=f"t{id_}@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    return u


def test_coleta_status_em_andamento_mesmo_sem_log_proprio_do_usuario():
    """Achado real: numa coleta de cron (processa usuário por usuário em
    sequência), o LogColeta "terminou" de UM usuário só é criado quando
    chega a vez dele — enquanto isso, esse usuário via o botão manual
    recusar com "já existe uma coleta em andamento" (trava global, correta)
    ao mesmo tempo que o indicador do dashboard mostrava "ocioso" (nada no
    histórico dele ainda nesta rodada). O estado em_andamento/travado precisa
    vir da trava global, não do histórico deste usuário especificamente."""
    _limpar_estado_trava()
    db = _sessao()
    u = _usuario(db)   # usuário SEM nenhum LogColeta seu ainda
    m._coleta_lock.acquire()
    m._coleta_iniciada_em = m._utcnow_main() - timedelta(minutes=5)
    try:
        r = m.coleta_status(user=u, db=db)
        assert r["estado"] == "em_andamento"
        assert r["iniciada_ha_seg"] is not None and r["iniciada_ha_seg"] >= 300
    finally:
        _limpar_estado_trava()


def test_coleta_status_libera_trava_presa_sozinho_em_vez_de_reportar_travado():
    """Achado real: antes disso, 'travado' ficava preso indefinidamente até
    alguém tentar rodar uma coleta nova (o único outro lugar que verificava
    o limite) — agora o próprio /api/coleta/status (consultado a cada ~30s
    pelo dashboard) solta a trava sozinho assim que detecta que passou do
    limite, sem depender de um novo disparo. 'travado' não é mais um estado
    observável a partir daqui."""
    _limpar_estado_trava()
    db = _sessao()
    u = _usuario(db)
    m._coleta_lock.acquire()
    m._coleta_iniciada_em = m._utcnow_main() - m._LIMITE_COLETA_TRAVADA - timedelta(minutes=1)
    try:
        r = m.coleta_status(user=u, db=db)
        assert r["estado"] != "travado"
        assert m._coleta_lock.locked() is False
        assert m._coleta_iniciada_em is None
    finally:
        _limpar_estado_trava()


def test_coleta_status_travado_fecha_orfao_e_mostra_erro_na_ultima_coleta():
    """Quando existe um LogColeta órfão (rodada que morreu no meio) desta
    conta, liberar a trava presa deixa esse registro visível como "erro na
    última coleta" -- mais preciso do que sumir silenciosamente ou fingir
    "nunca coletou"."""
    _limpar_estado_trava()
    db = _sessao()
    u = _usuario(db)
    db.add(LogColeta(usuario_id=u.id, fonte="coleta", origem="manual",
                     iniciado_em=m._utcnow_main() - timedelta(hours=4)))
    db.commit()
    m._coleta_lock.acquire()
    m._coleta_iniciada_em = m._utcnow_main() - m._LIMITE_COLETA_TRAVADA - timedelta(minutes=1)
    try:
        r = m.coleta_status(user=u, db=db)
        assert r["estado"] == "ocioso"
        assert r["erro"] == "interrompida (processo reiniciado antes de terminar)"
        assert m._coleta_lock.locked() is False
    finally:
        _limpar_estado_trava()


def test_coleta_status_ocioso_usa_ultima_coleta_concluida_do_usuario():
    _limpar_estado_trava()
    db = _sessao()
    u = _usuario(db)
    fim = m._utcnow_main() - timedelta(hours=2)
    db.add(LogColeta(usuario_id=u.id, fonte="PNCP", origem="cron",
                     iniciado_em=fim - timedelta(minutes=40), finalizado_em=fim,
                     editais_novos=12, editais_vistos=100, matches_fortes=3))
    db.commit()

    r = m.coleta_status(user=u, db=db)
    assert r["estado"] == "ocioso"
    assert r["novos"] == 12
    assert r["fortes"] == 3


def test_coleta_status_nunca_quando_sem_trava_e_sem_historico():
    _limpar_estado_trava()
    db = _sessao()
    u = _usuario(db)
    r = m.coleta_status(user=u, db=db)
    assert r["estado"] == "nunca"


def _limpar_estado_fase():
    m._coleta_fase, m._coleta_fase_feitos, m._coleta_fase_total = None, 0, None


def test_coleta_status_reporta_fase_quando_em_andamento():
    """Achado real: o indicador do dashboard só dizia "coleta em andamento"
    do início ao fim, mesmo já tendo terminado de buscar no PNCP fazia tempo
    e só faltando calcular compatibilidade usuário por usuário — parecia uma
    trava sem explicação nenhuma."""
    _limpar_estado_trava()
    _limpar_estado_fase()
    db = _sessao()
    u = _usuario(db)
    m._coleta_lock.acquire()
    m._coleta_iniciada_em = m._utcnow_main()
    m._coleta_fase, m._coleta_fase_feitos, m._coleta_fase_total = "compatibilidade", 2, 5
    try:
        r = m.coleta_status(user=u, db=db)
        assert r["fase"] == "compatibilidade"
        assert r["fase_feitos"] == 2
        assert r["fase_total"] == 5
    finally:
        _limpar_estado_trava()
        _limpar_estado_fase()


def test_coleta_status_fase_nula_quando_ociosa():
    _limpar_estado_trava()
    _limpar_estado_fase()
    db = _sessao()
    u = _usuario(db)
    fim = m._utcnow_main() - timedelta(hours=1)
    db.add(LogColeta(usuario_id=u.id, fonte="PNCP", origem="cron",
                     iniciado_em=fim - timedelta(minutes=30), finalizado_em=fim))
    db.commit()
    r = m.coleta_status(user=u, db=db)
    assert r["estado"] == "ocioso"
    assert r["fase"] is None
    assert r["fase_feitos"] is None
    assert r["fase_total"] is None
