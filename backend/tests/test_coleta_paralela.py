"""
Achado real: numa coleta com mais de um usuário, a etapa de calcular
compatibilidade pro catálogo de cada um rodava em sequência, um usuário de
cada vez — depois que a busca no PNCP (que roda uma vez só, compartilhada)
já tinha terminado. Essa etapa agora roda em paralelo (pool de threads, cada
uma com sua própria sessão de banco) quando há mais de 1 usuário na rodada
de cron; a coleta manual (sempre 1 usuário) continua sequencial, sem
necessidade de paralelizar. Rode com:  cd backend && pytest
"""
import os
import tempfile
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import database as db_module
from app.connectors.base import BaseConnector
from app.models import Base, Usuario, Edital, Produto, Match, LogColeta
from app import service


class _ConectorVazio(BaseConnector):
    nome = "PNCP"

    def coletar(self):
        return []


def _engine_arquivo():
    """Precisa ser um arquivo (não :memory:) — cada worker da
    ThreadPoolExecutor abre a SUA PRÓPRIA conexão via SessionLocal()
    (monkeypatchado abaixo), e :memory: não compartilha dados entre
    conexões diferentes."""
    fd, caminho = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{caminho}")
    Base.metadata.create_all(engine)
    return engine, caminho


def _limpar(engine, caminho, *sessoes):
    for s in sessoes:
        try:
            s.close()
        except Exception:
            pass
    engine.dispose()
    try:
        os.remove(caminho)
    except PermissionError:
        pass   # Windows às vezes ainda segura o handle por um instante — não é crítico limpar


def _semear_usuario(db, email, n_editais_compat):
    u = Usuario(nome="Teste", email=email, senha_hash="x")
    db.add(u)
    db.commit()
    db.add(Produto(usuario_id=u.id, descricao="Caneta esferografica azul",
                   palavras_chave="caneta, esferografica, azul"))
    for i in range(n_editais_compat):
        db.add(Edital(fonte="PNCP", id_externo=f"{email}-ed{i}",
                      objeto="Aquisicao de caneta esferografica",
                      orgao="Orgao Teste", uf="SP"))
    db.commit()
    return u


def test_gerar_matches_varios_usuarios_processa_todos_e_soma_fortes(monkeypatch):
    """Mocka _gerar_matches_usuario (a lógica de pontuação em si já tem
    cobertura própria em outros testes, e depende do reranker de IA pra
    produzir qualquer score — não é o que este teste quer verificar). O que
    importa aqui é a ORQUESTRAÇÃO: cada worker roda com sua própria sessão,
    grava seu próprio LogColeta, e os resultados de todos são agregados
    corretamente de volta pra thread principal."""
    engine, caminho = _engine_arquivo()
    db = db2 = None
    try:
        SessionFactory = sessionmaker(bind=engine)
        monkeypatch.setattr(db_module, "SessionLocal", SessionFactory)
        db = SessionFactory()
        u1 = _semear_usuario(db, "u1@t.com", 1)
        u2 = _semear_usuario(db, "u2@t.com", 1)

        def _forte_falso(db, usuario, **kw):
            return {"editais": 1, "atualizados": 1, "fortes": 1}

        progresso = []
        with patch("app.service._gerar_matches_usuario", side_effect=_forte_falso):
            total_fortes = service._gerar_matches_varios_usuarios(
                [u1, u2], "PNCP", service.utcnow(), deve_cancelar=lambda: False,
                progresso_fase=lambda fase, feitos, total: progresso.append((fase, feitos, total)))

        assert total_fortes == 2   # 1 "forte" simulado por usuário, somados

        db2 = SessionFactory()
        logs = db2.execute(select(LogColeta).where(LogColeta.origem == "cron")).scalars().all()
        assert len(logs) == 2
        assert {l.usuario_id for l in logs} == {u1.id, u2.id}
        assert all(l.matches_fortes == 1 for l in logs)

        # progresso reportado pra cada usuário concluído, terminando em 2/2
        assert len(progresso) == 2
        assert all(fase == "compatibilidade" and total == 2 for fase, _, total in progresso)
        assert {feitos for _, feitos, _ in progresso} == {1, 2}
    finally:
        _limpar(engine, caminho, db, db2)


def test_gerar_matches_varios_usuarios_erro_em_um_nao_derruba_os_outros(monkeypatch):
    engine, caminho = _engine_arquivo()
    db = db2 = None
    try:
        SessionFactory = sessionmaker(bind=engine)
        monkeypatch.setattr(db_module, "SessionLocal", SessionFactory)
        db = SessionFactory()
        u1 = _semear_usuario(db, "u1@t.com", 1)
        u2 = _semear_usuario(db, "u2@t.com", 1)

        def _falha_pro_primeiro(db, usuario, **kw):
            if usuario.email == "u1@t.com":
                raise RuntimeError("falha simulada")
            return {"editais": 1, "atualizados": 1, "fortes": 1}

        with patch("app.service._gerar_matches_usuario", side_effect=_falha_pro_primeiro):
            total_fortes = service._gerar_matches_varios_usuarios(
                [u1, u2], "PNCP", service.utcnow(), deve_cancelar=lambda: False)

        assert total_fortes == 1   # só o u2 processou com sucesso

        db2 = SessionFactory()
        logs = {l.usuario_id: l for l in db2.execute(
            select(LogColeta).where(LogColeta.origem == "cron")).scalars().all()}
        assert logs[u1.id].erro == "falha simulada"
        assert logs[u2.id].erro is None
        assert logs[u2.id].matches_fortes == 1
    finally:
        _limpar(engine, caminho, db, db2)


def test_processar_coleta_usa_caminho_paralelo_so_para_cron_com_mais_de_1_usuario(monkeypatch):
    """Coleta manual (usuario_id definido) sempre tem 1 usuário em alvos —
    não tem o que paralelizar, e log_usuario (registro "em andamento"
    específico da coleta manual) só se aplica a esse caminho. Cron só
    considera usuários que já têm ao menos 1 match (ver processar_coleta) —
    por isso os dois usuários precisam de um Match seedado de antemão pra
    entrarem em "alvos"."""
    engine, caminho = _engine_arquivo()
    db = None
    try:
        SessionFactory = sessionmaker(bind=engine)
        monkeypatch.setattr(db_module, "SessionLocal", SessionFactory)
        db = SessionFactory()
        u1 = _semear_usuario(db, "u1@t.com", 0)
        u2 = _semear_usuario(db, "u2@t.com", 0)
        ed = Edital(fonte="PNCP", id_externo="ed-generico", objeto="Objeto qualquer",
                   orgao="Orgao Teste", uf="SP")
        db.add(ed)
        db.commit()
        db.add(Match(usuario_id=u1.id, edital_id=ed.id, score=0.9, nivel="forte"))
        db.add(Match(usuario_id=u2.id, edital_id=ed.id, score=0.9, nivel="forte"))
        db.commit()

        chamadas_paralelo = {"n": 0}
        monkeypatch.setattr(service, "_gerar_matches_varios_usuarios",
                            lambda *a, **kw: chamadas_paralelo.__setitem__("n", chamadas_paralelo["n"] + 1) or 0)

        # coleta manual: usuario_id definido -> 1 usuário só -> sequencial
        service.processar_coleta(db, conectores=[_ConectorVazio()], usuario_id=u1.id)
        assert chamadas_paralelo["n"] == 0

        # cron com 2 usuários (ambos já com match) -> paralelo
        service.processar_coleta(db, conectores=[_ConectorVazio()], usuario_id=None)
        assert chamadas_paralelo["n"] == 1
    finally:
        _limpar(engine, caminho, db)


def test_processar_coleta_reporta_fase_buscando_antes_da_compatibilidade(monkeypatch):
    engine, caminho = _engine_arquivo()
    db = None
    try:
        SessionFactory = sessionmaker(bind=engine)
        monkeypatch.setattr(db_module, "SessionLocal", SessionFactory)
        db = SessionFactory()
        _semear_usuario(db, "u1@t.com", 0)

        fases = []
        service.processar_coleta(db, conectores=[_ConectorVazio()], usuario_id=None,
                                 progresso_fase=lambda fase, feitos, total: fases.append(fase))

        assert fases[0] == "buscando"
        assert "compatibilidade" in fases
    finally:
        _limpar(engine, caminho, db)
