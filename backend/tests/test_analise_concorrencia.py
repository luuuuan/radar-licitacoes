"""
Achado real nº 1: GET /api/editais/{id}/analise não tinha nenhuma trava — o
cache (Edital.analise_ia) é GLOBAL por edital (não por usuário, ao
contrário de Match/Proposta/etc), mas duas requisições concorrentes pro
MESMO edital ainda sem análise disparavam a chamada de IA em dobro (a 2ª
sobrescrevia o resultado da 1ª sem corromper nada, só desperdiçava a
chamada). Trava por edital_id (_analise_locks, mesmo padrão de
_recalculo_locks por usuário) fecha essa janela.

Achado real nº 2 (revelado testando o nº 1 com o MESMO usuário nas duas
threads, por engano): a cache POR USUÁRIO (AnaliseIAExtras — verificação de
documentos e comparação de catálogo, únicas por usuario_id+edital_id) não
tinha trava nenhuma. O MESMO usuário disparando 2 requests concorrentes pro
MESMO edital (duplo clique, 2 abas) batia num IntegrityError de verdade:
as duas liam "não existe" e as duas tentavam inserir a mesma linha. Trava
por (usuario_id, edital_id) (_extras_locks) resolve.

Rode com:  cd backend && pytest
"""
import threading
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as app_main
from app import analise_edital as ia_module
from app.models import Base, Usuario, Edital


def _engine_arquivo(tmp_path, nome):
    # precisa ser um arquivo (não sqlite:///:memory:): duas "sessões" em
    # threads diferentes têm que enxergar o MESMO banco, como duas requests
    # reais enxergariam o mesmo Postgres em produção. tmp_path (fixture do
    # pytest) limpa sozinho depois do teste.
    return create_engine(f"sqlite:///{tmp_path / nome}")


def test_duas_requisicoes_concorrentes_pro_mesmo_edital_chamam_a_ia_uma_vez_so(monkeypatch, tmp_path):
    engine = _engine_arquivo(tmp_path, "concorrencia.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    db_setup = Session()
    # dois usuários DIFERENTES — é exatamente esse o cenário do achado real
    # (dois usuários abrindo o MESMO edital ainda sem análise ao mesmo
    # tempo). Usar o mesmo usuário nos dois lados bateria numa 2ª corrida,
    # separada: AnaliseIAExtras (cache POR usuário) não tem trava nenhuma,
    # e um insert duplicado pra (usuario_id, edital_id) colide na constraint
    # de unicidade — mas isso é outro problema (usuário disparando 2x a
    # própria request), fora do escopo desta trava.
    u1 = Usuario(nome="Teste 1", email="t1@t.com", senha_hash="x")
    u2 = Usuario(nome="Teste 2", email="t2@t.com", senha_hash="x")
    db_setup.add_all([u1, u2])
    db_setup.commit()
    ed = Edital(fonte="PNCP", id_externo="sem-ref-valida", orgao="Orgao",
                objeto="Aquisicao de material", uf="SP")
    db_setup.add(ed)
    db_setup.commit()
    edital_id, uid1, uid2 = ed.id, u1.id, u2.id
    db_setup.close()

    chamadas = []

    def _analisar_fake(objeto, arquivos, api_key=None):
        chamadas.append(1)
        time.sleep(0.3)   # dá tempo da 2ª requisição bater na trava enquanto a 1ª "processa"
        return {"status": "ok", "versao": ia_module.VERSAO_PROMPT, "resumo": "ok",
                "objeto": objeto, "requisitos_tecnicos": [], "documentos_habilitacao": []}

    monkeypatch.setattr(ia_module, "analisar", _analisar_fake)
    monkeypatch.setattr(ia_module, "ia_texto_disponivel", lambda chave: True)
    # busca de arquivos no PNCP encurtada -- sem isso, cairia numa chamada de
    # rede de verdade (ou, com id_externo inválido, no status "sem_ref", que
    # agora é tratado como falha de busca, não "sem arquivo" -- ver achado
    # real no próprio analise_edital()/main.py). "vazio" = busca funcionou,
    # só não achou nenhum arquivo -- deixa a análise seguir até chamar a IA,
    # que é o que este teste quer observar.
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed: {"status": "vazio", "arquivos": [], "portal": None})

    resultados = [None, None]

    def _chamar(i, uid):
        db = Session()
        u_local = db.get(Usuario, uid)
        resultados[i] = app_main.analise_edital(edital_id, forcar=False, user=u_local, db=db)
        db.close()

    t1 = threading.Thread(target=_chamar, args=(0, uid1))
    t2 = threading.Thread(target=_chamar, args=(1, uid2))
    t1.start()
    time.sleep(0.05)   # garante que a 1ª já pegou a trava antes da 2ª tentar
    t2.start()
    t1.join()
    t2.join()

    assert len(chamadas) == 1, f"IA foi chamada {len(chamadas)} vez(es), esperado 1"
    assert resultados[0]["status"] == "ok"
    assert resultados[1]["status"] == "ok"


def test_analise_ja_cacheada_nao_dispara_a_ia_nem_precisa_da_trava(monkeypatch, tmp_path):
    """Requisição normal (edital já com análise válida em cache) não deve
    nem chegar perto da trava — é só leitura."""
    engine = _engine_arquivo(tmp_path, "cache.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    db = Session()
    u = Usuario(nome="Teste", email="t2@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    import json
    ed = Edital(fonte="PNCP", id_externo="sem-ref-valida-2", orgao="Orgao",
                objeto="Aquisicao", uf="SP",
                analise_ia=json.dumps({"status": "ok", "versao": ia_module.VERSAO_PROMPT,
                                       "resumo": "já calculado", "objeto": "Aquisicao",
                                       "requisitos_tecnicos": [], "documentos_habilitacao": []}))
    db.add(ed)
    db.commit()

    chamadas = []
    monkeypatch.setattr(ia_module, "analisar", lambda *a, **k: chamadas.append(1))

    r = app_main.analise_edital(ed.id, forcar=False, user=u, db=db)

    assert chamadas == []
    assert r["cache"] is True
    assert r["resumo"] == "já calculado"


def test_mesmo_usuario_duas_requisicoes_concorrentes_pro_mesmo_edital_nao_quebra(monkeypatch, tmp_path):
    """Achado nº 2 do docstring do módulo: mesmo usuário, mesmo edital, 2
    requests ao mesmo tempo — sem trava, isso derrubava com IntegrityError
    ao tentar inserir 2x a mesma linha em AnaliseIAExtras."""
    engine = _engine_arquivo(tmp_path, "mesmo_usuario.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    db_setup = Session()
    u = Usuario(nome="Teste", email="mesmo@t.com", senha_hash="x")
    db_setup.add(u)
    db_setup.commit()
    import json
    # já com a análise-base cacheada -> as 2 threads pulam direto pra
    # _rodar_extras_ia (onde está a trava nova), sem depender de timing da
    # trava de _analise_locks (essa já testada à parte, acima). Catálogo e
    # documentos ficam vazios de propósito -> cada thread cai no caminho
    # "sem dado -> upsert com None", que é onde a corrida acontecia.
    ed = Edital(fonte="PNCP", id_externo="sem-ref-mesmo-usuario", orgao="Orgao",
                objeto="Aquisicao", uf="SP",
                analise_ia=json.dumps({"status": "ok", "versao": ia_module.VERSAO_PROMPT,
                                       "resumo": "ok", "objeto": "Aquisicao",
                                       "requisitos_tecnicos": [], "documentos_habilitacao": []}))
    db_setup.add(ed)
    db_setup.commit()
    edital_id, uid = ed.id, u.id
    db_setup.close()

    _obter_original = app_main._obter_cache_extras

    def _obter_lento(db, user, edital_id_):
        r = _obter_original(db, user, edital_id_)
        time.sleep(0.15)   # alarga de propósito a janela entre "ler" e "inserir"
        return r
    monkeypatch.setattr(app_main, "_obter_cache_extras", _obter_lento)

    erros = []
    resultados = [None, None]

    def _chamar(i):
        db = Session()
        u_local = db.get(Usuario, uid)
        try:
            resultados[i] = app_main.analise_edital(edital_id, forcar=False, user=u_local, db=db)
        except Exception as e:
            erros.append(e)
        finally:
            db.close()

    t1 = threading.Thread(target=_chamar, args=(0,))
    t2 = threading.Thread(target=_chamar, args=(1,))
    t1.start()
    time.sleep(0.03)
    t2.start()
    t1.join()
    t2.join()

    assert erros == [], f"não deveria dar erro: {erros}"
    assert resultados[0]["status"] == "ok"
    assert resultados[1]["status"] == "ok"
