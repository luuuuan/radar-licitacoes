"""
Testes de POST /api/editais/{id}/analise/iniciar + GET .../analise/status.
Achado real: a Análise por IA rodava síncrona dentro da request HTTP e podia
passar de 100s+ em editais grandes (resumo + lotes de comparação de
catálogo, até 90s cada) -- o proxy da Railway derrubava a conexão antes de
terminar, o navegador mostrava "Erro ao analisar edital", mas o trabalho
continuava no servidor e salvava o resultado (por isso reabrir a página sem
clicar em nada já mostrava a análise pronta). Agora roda em BackgroundTasks,
mesmo padrão do completar-descrição, só que por (usuário, edital) -- não só
por edital -- porque a comparação de catálogo é por usuário.
Rode com: cd backend && pytest
"""
import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as app_main
from app import analise_edital as ia_module
from app.models import Base, Usuario, Edital


@pytest.fixture(autouse=True)
def _limpa_estado_global():
    """_analise_status/_analise_cancelar são dicts em memória, no MÓDULO
    app.main -- persistem entre testes (cada teste usa um banco novo, mas
    os IDs de usuário/edital reiniciam do 1, então uma chave antiga podia
    vazar pro teste seguinte sem isso)."""
    app_main._analise_status.clear()
    app_main._analise_cancelar.clear()
    yield
    app_main._analise_status.clear()
    app_main._analise_cancelar.clear()


def _sessao_e_fabrica(tmp_path, nome):
    """Devolve (sessão pronta pra usar, sessionmaker) -- a fábrica serve pra
    monkeypatch.setattr(app_main, "SessionLocal", fabrica): _rodar_analise_bg
    abre sua PRÓPRIA sessão (SessionLocal() puro, sem receber `db` de
    parâmetro -- é assim que roda de verdade em produção, fora do ciclo de
    vida da request), então sem isso ele bateria no banco de dev local de
    verdade em vez do banco isolado deste teste (mesmo padrão já usado em
    test_completar_descricao_itens.py)."""
    engine = create_engine(f"sqlite:///{tmp_path / nome}")
    Base.metadata.create_all(engine)
    Fabrica = sessionmaker(bind=engine)
    return Fabrica(), Fabrica


def _usuario_e_edital(db, email="t@t.com"):
    u = Usuario(nome="Teste", email=email, senha_hash="x")
    db.add(u)
    db.commit()
    ed = Edital(fonte="PNCP", id_externo="sem-ref-valida", orgao="Orgao",
               objeto="Aquisicao de material", uf="SP")
    db.add(ed)
    db.commit()
    return u, ed


def test_iniciar_dispara_e_status_reflete_conclusao(monkeypatch, tmp_path):
    db, fabrica = _sessao_e_fabrica(tmp_path, "a.db")
    monkeypatch.setattr(app_main, "SessionLocal", fabrica)
    u, ed = _usuario_e_edital(db)

    monkeypatch.setattr(ia_module, "ia_texto_disponivel", lambda chave: True)
    monkeypatch.setattr(ia_module, "analisar", lambda objeto, arquivos, api_key=None, **kw: {
        "status": "ok", "versao": ia_module.VERSAO_PROMPT, "resumo": "ok",
        "objeto": objeto, "requisitos_tecnicos": [], "documentos_habilitacao": []})
    # id_externo "sem-ref-valida" não parseia em (cnpj, ano, seq) -> sem
    # isso, _listar_arquivos_pncp cairia em "sem_ref" (falha de busca, não
    # "sem arquivo" -- ver test_analise_edital_erro_arquivos_pncp.py) e
    # nunca chegaria a chamar o analisar() acima.
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed_: {"status": "vazio", "arquivos": [], "portal": None})

    r = app_main.analise_edital_iniciar(ed.id, BackgroundTasks(), forcar=False, user=u, db=db)
    assert r == {"ok": True, "em_andamento": True}

    st = app_main.analise_edital_status(ed.id, user=u, db=db)
    assert st["rodando"] is True

    # simula o que bg.add_task teria disparado (sessão isolada de teste, sem
    # depender do event loop do FastAPI rodando de verdade)
    app_main._rodar_analise_bg(ed.id, u.id, False)

    st = app_main.analise_edital_status(ed.id, user=u, db=db)
    assert st["rodando"] is False
    assert st["erro"] is None
    assert st["resultado"]["status"] == "ok"


def test_iniciar_com_analise_ja_em_andamento_nao_duplica(tmp_path):
    db, _ = _sessao_e_fabrica(tmp_path, "b.db")
    u, ed = _usuario_e_edital(db)
    app_main._analise_status[(u.id, ed.id)] = {"rodando": True, "erro": None}

    r = app_main.analise_edital_iniciar(ed.id, BackgroundTasks(), forcar=False, user=u, db=db)

    assert r["ok"] is False
    assert r["em_andamento"] is True


def test_iniciar_edital_inexistente_da_404(tmp_path):
    db, _ = _sessao_e_fabrica(tmp_path, "c.db")
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()

    with pytest.raises(Exception) as exc:
        app_main.analise_edital_iniciar(99999, BackgroundTasks(), forcar=False, user=u, db=db)
    assert "404" in str(exc.value) or "não encontrado" in str(exc.value).lower()


def test_status_sem_nada_iniciado_retorna_parado(tmp_path):
    db, _ = _sessao_e_fabrica(tmp_path, "d.db")
    u, ed = _usuario_e_edital(db)
    st = app_main.analise_edital_status(ed.id, user=u, db=db)
    assert st == {"rodando": False, "erro": None}


def test_bg_com_erro_fica_registrado_no_status_sem_derrubar(monkeypatch, tmp_path):
    db, fabrica = _sessao_e_fabrica(tmp_path, "e.db")
    monkeypatch.setattr(app_main, "SessionLocal", fabrica)
    u, ed = _usuario_e_edital(db)

    monkeypatch.setattr(ia_module, "ia_texto_disponivel", lambda chave: True)
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed_: {"status": "vazio", "arquivos": [], "portal": None})
    def _explode(*a, **k):
        raise RuntimeError("falha simulada da IA")
    monkeypatch.setattr(ia_module, "analisar", _explode)

    app_main._analise_status[(u.id, ed.id)] = {"rodando": True, "erro": None}
    app_main._rodar_analise_bg(ed.id, u.id, False)   # não deve levantar

    st = app_main.analise_edital_status(ed.id, user=u, db=db)
    assert st["rodando"] is False
    assert "falha simulada" in st["erro"]


def test_status_e_isolado_por_usuario_nao_vaza_catalogo_entre_contas(monkeypatch, tmp_path):
    """A comparação de catálogo é por usuário -- o status de um usuário não
    pode devolver o resultado (com o catálogo) de outro usuário no MESMO
    edital."""
    db, fabrica = _sessao_e_fabrica(tmp_path, "f.db")
    monkeypatch.setattr(app_main, "SessionLocal", fabrica)
    u1, ed = _usuario_e_edital(db, email="u1@t.com")
    u2 = Usuario(nome="Outro", email="u2@t.com", senha_hash="x")
    db.add(u2)
    db.commit()

    monkeypatch.setattr(ia_module, "ia_texto_disponivel", lambda chave: True)
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed_: {"status": "vazio", "arquivos": [], "portal": None})
    monkeypatch.setattr(ia_module, "analisar", lambda objeto, arquivos, api_key=None, **kw: {
        "status": "ok", "versao": ia_module.VERSAO_PROMPT, "resumo": "ok",
        "objeto": objeto, "requisitos_tecnicos": [], "documentos_habilitacao": []})

    app_main.analise_edital_iniciar(ed.id, BackgroundTasks(), forcar=False, user=u1, db=db)
    app_main._rodar_analise_bg(ed.id, u1.id, False)

    st_u1 = app_main.analise_edital_status(ed.id, user=u1, db=db)
    st_u2 = app_main.analise_edital_status(ed.id, user=u2, db=db)
    assert st_u1["rodando"] is False and st_u1.get("resultado")
    assert st_u2 == {"rodando": False, "erro": None}   # u2 nunca pediu nada pra este edital


# ---------------------------------------------------------------------------
# Achado real (usuário reportou, edital 139008): _analise_status é um dict em
# memória, POR PROCESSO -- se o servidor reinicia (deploy) enquanto uma
# análise roda em segundo plano, a entrada some mesmo que a análise já tenha
# sido salva no banco ANTES do processo cair. A central de notificações não
# tem esse problema (lê Edital.analise_em direto do banco); esta rota, sem
# fallback, devolvia {"rodando": False, "erro": None} sem resultado nenhum --
# a página ficava presa no loader pra sempre (o polling do front só sai do
# loop quando recebe um "resultado").
# ---------------------------------------------------------------------------

def test_status_sem_entrada_em_memoria_usa_cache_do_banco_se_ja_completou(monkeypatch, tmp_path):
    db, fabrica = _sessao_e_fabrica(tmp_path, "g.db")
    monkeypatch.setattr(app_main, "SessionLocal", fabrica)
    u, ed = _usuario_e_edital(db)

    monkeypatch.setattr(ia_module, "ia_texto_disponivel", lambda chave: True)
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed_: {"status": "vazio", "arquivos": [], "portal": None})
    monkeypatch.setattr(ia_module, "analisar", lambda objeto, arquivos, api_key=None, **kw: {
        "status": "ok", "versao": ia_module.VERSAO_PROMPT, "resumo": "ok",
        "objeto": objeto, "requisitos_tecnicos": [], "documentos_habilitacao": []})

    app_main.analise_edital_iniciar(ed.id, BackgroundTasks(), forcar=False, user=u, db=db)
    app_main._rodar_analise_bg(ed.id, u.id, False)   # análise termina e salva no banco

    # simula o servidor reiniciando (deploy): a entrada em memória some, mas
    # o banco continua com a análise salva
    app_main._analise_status.clear()

    # sessão NOVA (mesmo padrão de uma request HTTP de verdade, que nunca
    # reaproveita a sessão de outra request) -- com a sessão `db` original,
    # db.get() devolveria o objeto Edital já carregado no identity map dela
    # (analise_ia ainda None ali), sem refletir o commit feito por
    # _rodar_analise_bg numa sessão separada -- o teste passaria "por
    # acidente" sem realmente exercitar o fallback.
    db_nova = fabrica()

    st = app_main.analise_edital_status(ed.id, user=u, db=db_nova)

    assert st["rodando"] is False
    assert st["erro"] is None
    assert st["resultado"]["status"] == "ok"


def test_status_sem_entrada_em_memoria_e_sem_analise_no_banco_continua_parado(tmp_path):
    """O fallback só se aplica quando o banco realmente tem uma análise
    válida -- edital nunca analisado continua devolvendo o "parado" de
    sempre, não inventa um resultado."""
    db, _ = _sessao_e_fabrica(tmp_path, "h.db")
    u, ed = _usuario_e_edital(db)

    st = app_main.analise_edital_status(ed.id, user=u, db=db)

    assert st == {"rodando": False, "erro": None}


# ---------------------------------------------------------------------------
# Achado real (auditoria de arquitetura pedida pelo usuário): _analise_status
# guarda o RESULTADO INTEIRO da análise por (usuário, edital), sem teto nem
# expurgo -- cresce com toda combinação já analisada neste processo, nunca
# encolhe. _definir_status_analise agora descarta a entrada mais antiga JÁ
# TERMINADA ao passar do teto, nunca uma em andamento.
# ---------------------------------------------------------------------------

def test_definir_status_analise_descarta_a_mais_antiga_terminada_ao_passar_do_teto(monkeypatch):
    app_main._analise_status.clear()
    monkeypatch.setattr(app_main, "_MAX_ANALISE_STATUS", 3)

    app_main._definir_status_analise((1, 100), {"rodando": False, "erro": None})
    app_main._definir_status_analise((1, 101), {"rodando": False, "erro": None})
    app_main._definir_status_analise((1, 102), {"rodando": False, "erro": None})
    assert len(app_main._analise_status) == 3

    app_main._definir_status_analise((1, 103), {"rodando": False, "erro": None})

    assert len(app_main._analise_status) == 3
    assert (1, 100) not in app_main._analise_status   # a mais antiga saiu
    assert (1, 103) in app_main._analise_status


def test_definir_status_analise_nunca_descarta_entrada_em_andamento(monkeypatch):
    app_main._analise_status.clear()
    monkeypatch.setattr(app_main, "_MAX_ANALISE_STATUS", 2)

    app_main._definir_status_analise((1, 100), {"rodando": True, "erro": None})   # ainda rodando -- protegida
    app_main._definir_status_analise((1, 101), {"rodando": False, "erro": None})
    app_main._definir_status_analise((1, 102), {"rodando": False, "erro": None})

    # passou do teto (3 > 2), mas a única candidata a sair é a "rodando",
    # que nunca pode ser removida -- fica temporariamente acima do teto
    # em vez de apagar um job ainda em andamento.
    assert (1, 100) in app_main._analise_status
    assert app_main._analise_status[(1, 100)]["rodando"] is True


# ---------------------------------------------------------------------------
# Achado real (auditoria de arquitetura pedida pelo usuário): até a versão
# anterior, _analise_cancelar era keyed só por usuário (não por (usuário,
# edital)). analise_edital_status() cai no fallback de cache (acima) e chama
# analise_edital() de volta quando não há entrada em _analise_status para
# aquele edital -- e analise_edital() reseta _analise_cancelar no próprio
# início ("defensivo"). Com a chave por usuário só, um poll de status de um
# edital B (cache-hit, sem entrada em _analise_status) resetava o MESMO dict
# usado pelo cancelamento de um edital A, ainda rodando em segundo plano --
# o botão "Cancelar" da análise de A parecia não fazer nada.
# ---------------------------------------------------------------------------

def test_cancelar_de_um_edital_nao_e_apagado_por_poll_de_status_de_outro_edital(monkeypatch, tmp_path):
    db, fabrica = _sessao_e_fabrica(tmp_path, "i.db")
    monkeypatch.setattr(app_main, "SessionLocal", fabrica)
    u, edital_a = _usuario_e_edital(db, email="t@t.com")
    edital_b = Edital(fonte="PNCP", id_externo="sem-ref-valida-b", orgao="Orgao",
                      objeto="Outro objeto", uf="SP")
    db.add(edital_b)
    db.commit()

    # edital B já tem uma análise válida cacheada (de uma rodada anterior) e
    # NENHUMA entrada em _analise_status -- é exatamente a condição que
    # dispara o fallback de analise_edital_status chamando analise_edital().
    edital_b.analise_ia = __import__("json").dumps({
        "status": "ok", "versao": ia_module.VERSAO_PROMPT, "resumo": "ok",
        "objeto": edital_b.objeto, "requisitos_tecnicos": [], "documentos_habilitacao": []})
    db.commit()

    # edital A está com uma análise "em andamento" nesta engine (simulado:
    # não precisa de fato rodar em background pra este teste, só precisa
    # que o usuário tenha acabado de pedir cancelamento pra ele).
    app_main.analise_cancelar(edital_a.id, user=u)
    assert app_main._analise_cancelar[(u.id, edital_a.id)] is True

    # usuário abre OUTRA aba com o edital B -- o front consulta /status, cai
    # no fallback de cache (sem entrada em _analise_status pra B) e chama
    # analise_edital(B, ...) internamente.
    st_b = app_main.analise_edital_status(edital_b.id, user=u, db=db)
    assert st_b["resultado"]["status"] == "ok"   # confirma que passou pelo fallback de fato

    # o cancelamento do edital A tem que continuar valendo
    assert app_main._analise_cancelar.get((u.id, edital_a.id)) is True
