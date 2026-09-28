"""
Achado real: abrir a aba "Análise por IA" de um edital JÁ analisado disparava
2 chamadas de IA de novo (verificação de documentos + comparação de
catálogo) a cada abertura, mesmo sem nada ter mudado — lento e sem o usuário
ter pedido. As duas checagens agora ficam cacheadas por (edital, usuário) em
AnaliseIAExtras, versionadas por Usuario.versao_catalogo/versao_documentos
(incrementados nos endpoints de criar/editar/excluir produto e documento).
Sem mudança de versão -> reusa o cache, sem chamar IA. Com mudança -> devolve
o resultado antigo marcado como desatualizado (não gasta IA sozinho); só
forcar=True (botão "Realizar nova análise") recalcula de fato.
Rode com:  cd backend && pytest
"""
import json
from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Usuario, Edital, ItemEdital, Produto, Documento, AnaliseIAExtras
from app.main import (
    _anexar_verificacao_ia_documentos, _anexar_comparacao_catalogo_ia,
    criar_produto, atualizar_produto, remover_produto, ProdutoIn,
    remover_documento,
)


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _semear(db):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    db.add(Documento(usuario_id=u.id, nome="Certidao", data_validade=date(2030, 1, 1),
                     texto_extraido="CND valida", ativo=True))
    p = Produto(usuario_id=u.id, descricao="Caneta azul")
    db.add(p)
    ed = Edital(fonte="PNCP", id_externo="extras1", orgao="Teste", uf="SP", objeto="Material de escritorio")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Caneta azul", valor_unitario=2.0))
    db.commit()
    return u, ed, p


_resposta_docs = json.dumps({"itens": [{"exigido": "CND", "atendido": True, "documento": "Certidao", "observacao": ""}]})


def _resposta_catalogo(produto_id):
    return json.dumps({"itens": [
        {"numero_item": 1, "candidatos": [{"produto_id": produto_id, "justificativa": "ok"}]},
    ]})


def _resultado_base(ed):
    return {"status": "ok", "objeto": ed.objeto, "requisitos_tecnicos": ["Garantia minima"],
           "documentos_habilitacao": {"fiscal_trabalhista": ["CND Receita Federal"]}}


# ---------------------- verificação de documentos ---------------------- #

def test_verificacao_documentos_1a_vez_chama_ia_e_grava_cache():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_docs, "ok")
        out = _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
    assert mock_gerar.call_count == 1
    assert out["verificacao_documentos_ia"]["status"] == "ok"
    cache = db.query(AnaliseIAExtras).filter_by(usuario_id=u.id, edital_id=ed.id).first()
    assert cache is not None
    assert cache.versao_documentos_calc == u.versao_documentos == 0


def test_verificacao_documentos_sem_mudanca_reusa_cache_sem_chamar_ia():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_docs, "ok")
        _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
        out2 = _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
    assert mock_gerar.call_count == 1, "a 2a chamada deveria ter reusado o cache"
    assert out2["verificacao_documentos_ia"]["status"] == "ok"
    assert "verificacao_documentos_desatualizada" not in out2


def test_verificacao_documentos_apos_editar_documento_devolve_antigo_marcado_desatualizado():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_docs, "ok")
        _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
        u.versao_documentos += 1   # simula editar/criar/excluir um documento
        db.commit()
        out2 = _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
    assert mock_gerar.call_count == 1, "não deve gastar IA sozinho só porque a versão mudou"
    assert out2["verificacao_documentos_ia"]["status"] == "ok", "devolve o resultado anterior, não vazio"
    assert out2["verificacao_documentos_desatualizada"] is True


def test_verificacao_documentos_forcar_recalcula_mesmo_com_cache_valido():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_docs, "ok")
        _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
        out2 = _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key", forcar=True)
    assert mock_gerar.call_count == 2, "forcar=True deve gastar uma nova chamada de IA"
    assert "verificacao_documentos_desatualizada" not in out2


def test_verificacao_documentos_cache_de_logica_antiga_e_recalculada_sem_forcar():
    """Achado real (agente error-detective): esta cache era versionada só
    por versao_documentos_calc (muda quando o usuário edita um Documento) --
    uma correção na LÓGICA de verificar_documentos_usuario() (ex.: parar de
    cobrar declaração/requisito técnico, ou passar a marcar itens "não
    aplicáveis") nunca invalidava cache já existente: quem já tinha rodado a
    verificação antes da correção continuava vendo o resultado ANTIGO pra
    sempre, mesmo sem ter mudado nenhum documento. Simula exatamente esse
    cache "antigo" (formato de antes desta correção, sem a chave
    _versao_logica) gravado direto no banco -- sem passar por
    _anexar_verificacao_ia_documentos, que já geraria no formato novo."""
    db = _sessao()
    u, ed, p = _semear(db)
    cache_antigo = json.dumps({"itens": [
        {"exigido": "Declaração de ME/EPP", "atendido": False, "documento": "", "observacao": ""},
    ]})   # sem "_versao_logica" -- exatamente o formato gravado antes desta correção
    db.add(AnaliseIAExtras(usuario_id=u.id, edital_id=ed.id,
                           verificacao_documentos_ia=cache_antigo, versao_documentos_calc=u.versao_documentos))
    db.commit()

    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_docs, "ok")
        out = _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")

    assert mock_gerar.call_count == 1, "cache sem _versao_logica tem que ser tratado como inválido, não servido como está"
    assert out["verificacao_documentos_ia"]["itens"][0]["exigido"] == "CND"   # veio do mock, não do cache antigo
    assert "verificacao_documentos_desatualizada" not in out   # recalculou de verdade, não só marcou como desatualizado


# ---------------------- comparação de catálogo ---------------------- #

def test_comparacao_catalogo_sem_mudanca_reusa_cache_sem_chamar_ia():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_catalogo(p.id), "ok")
        _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key")
        out2 = _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key")
    assert mock_gerar.call_count == 1
    assert len(out2["comparacao_catalogo_ia"]["itens"]) == 1
    assert "comparacao_catalogo_desatualizada" not in out2


def test_comparacao_catalogo_apos_mudar_catalogo_devolve_antigo_marcado_desatualizado():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_catalogo(p.id), "ok")
        _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key")
        u.versao_catalogo += 1   # simula criar/editar/excluir um produto
        db.commit()
        out2 = _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key")
    assert mock_gerar.call_count == 1, "não deve gastar IA sozinho só porque a versão mudou"
    assert len(out2["comparacao_catalogo_ia"]["itens"]) == 1, "devolve o resultado anterior, não vazio"
    assert out2["comparacao_catalogo_desatualizada"] is True


def test_comparacao_catalogo_forcar_recalcula_mesmo_com_cache_valido():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_catalogo(p.id), "ok")
        _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key")
        out2 = _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key", forcar=True)
    assert mock_gerar.call_count == 2
    assert "comparacao_catalogo_desatualizada" not in out2


# ---------------------- versões bumped pelos endpoints de CRUD ---------------------- #

def test_criar_atualizar_excluir_produto_incrementa_versao_catalogo():
    db = _sessao()
    u, ed, p = _semear(db)
    versao_inicial = u.versao_catalogo
    dados = ProdutoIn(descricao="Lapis HB")
    r = criar_produto(dados, user=u, db=db)
    assert u.versao_catalogo == versao_inicial + 1

    atualizar_produto(r["id"], ProdutoIn(descricao="Lapis HB 2"), user=u, db=db)
    assert u.versao_catalogo == versao_inicial + 2

    remover_produto(r["id"], user=u, db=db)
    assert u.versao_catalogo == versao_inicial + 3


def test_excluir_documento_incrementa_versao_documentos():
    db = _sessao()
    u, ed, p = _semear(db)
    doc = db.query(Documento).filter_by(usuario_id=u.id).first()
    versao_inicial = u.versao_documentos
    remover_documento(doc.id, user=u, db=db)
    assert u.versao_documentos == versao_inicial + 1


# ------------------- fuso de AnaliseIAExtras.atualizado_em ------------------- #
# Achado real (usuário reportou 2x, edital 139008): o sino de notificações
# passou a exigir AnaliseIAExtras.atualizado_em >= Edital.analise_em pra
# avisar "análise concluída" (ver _query_analise_pendente, main.py) --
# analise_em é gravado em hora de Brasília naive (BR_TZ). Se atualizado_em
# continuasse em UTC naive (era o default original da coluna, utcnow), a
# comparação ficaria sistematicamente ~3h ENGANOSAMENTE a favor (UTC "lê"
# maior que BRT pro mesmo instante), mascarando o problema em vez de
# corrigi-lo -- exatamente a mesma classe de bug já corrigida em
# Match.analise_vista_em (commit 00c4dc7). Confere o valor gravado de
# verdade, não um valor forjado no teste.
def test_atualizado_em_usa_fuso_de_brasilia_nao_utc():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_docs, "ok")
        _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
    cache = db.query(AnaliseIAExtras).filter_by(usuario_id=u.id, edital_id=ed.id).first()

    agora_brt = datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
    agora_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((cache.atualizado_em - agora_brt).total_seconds()) < 10
    # se estivesse em UTC (bug), a diferença pro relógio BRT seria de ~3h --
    # bem acima de qualquer tolerância de execução do teste.
    assert abs((cache.atualizado_em - agora_utc).total_seconds()) > 3000


def test_pacote_completo_de_extras_habilita_notificacao_de_analise_concluida():
    """Prova de ponta a ponta (não um AnaliseIAExtras forjado no teste, como
    em test_notificacoes.py): roda a análise inteira via analise_edital()
    (parte rápida + _rodar_extras_ia) e confere que Edital.analise_em e
    AnaliseIAExtras.pacote_concluido_em, escritos por dois módulos diferentes
    (main.py e models.py) a poucos milissegundos de distância, ficam no
    MESMO fuso -- é a checagem real que _query_analise_pendente faz."""
    from app.main import analise_edital
    from app import analise_edital as ia_module
    db = _sessao()
    u, ed, p = _semear(db)
    chave_gemini = "fake-gemini-key"
    with patch("app.main._auth.decifrar", return_value=chave_gemini), \
         patch.object(ia_module, "ia_texto_disponivel", return_value=True), \
         patch("app.main._texto_pronto_cache", return_value="texto do edital"), \
         patch.object(ia_module, "analisar", return_value=_resultado_base(ed)), \
         patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.side_effect = [(_resposta_docs, "ok"), (_resposta_catalogo(p.id), "ok")]
        analise_edital(ed.id, forcar=False, user=u, db=db)

    db.refresh(ed)
    cache = db.query(AnaliseIAExtras).filter_by(usuario_id=u.id, edital_id=ed.id).first()
    assert ed.analise_em is not None
    assert cache is not None and cache.pacote_concluido_em is not None
    assert cache.pacote_concluido_em >= ed.analise_em


def test_atualizado_em_bate_antes_do_pacote_completo_pacote_concluido_em_so_no_fim():
    """Prova de ponta a ponta do bug relatado (usuário, edital 145353):
    atualizado_em (onupdate automático em AnaliseIAExtras) já bate assim que
    a PRIMEIRA das duas chamadas de IA (verificação de documentos) termina e
    commita -- ANTES da segunda (comparação de catálogo, a mais lenta,
    lotes de até 90s pra catálogo grande) sequer começar. pacote_concluido_em
    só é gravado depois que as DUAS terminam -- é por isso que
    _query_analise_pendente usa esse campo, não atualizado_em, pra decidir
    quando notificar "análise concluída"."""
    from app.main import analise_edital
    from app import analise_edital as ia_module
    db = _sessao()
    u, ed, p = _semear(db)
    chave_gemini = "fake-gemini-key"
    estado_apos_1a_chamada = {}

    def _gerar_e_capturar(*a, **kw):
        # 1ª chamada (verificação de documentos): retorna e deixa o commit
        # dela acontecer normalmente (dentro de _upsert_cache_extras).
        if not estado_apos_1a_chamada:
            estado_apos_1a_chamada["chamada"] = 1
            return (_resposta_docs, "ok")
        # 2ª chamada (comparação de catálogo): a 1ª já commitou antes de
        # chegarmos aqui -- captura o estado ANTES desta 2ª rodar.
        if estado_apos_1a_chamada["chamada"] == 1:
            cache = db.query(AnaliseIAExtras).filter_by(usuario_id=u.id, edital_id=ed.id).first()
            estado_apos_1a_chamada["atualizado_em"] = cache.atualizado_em if cache else None
            estado_apos_1a_chamada["pacote_concluido_em"] = cache.pacote_concluido_em if cache else None
            estado_apos_1a_chamada["chamada"] = 2
        return (_resposta_catalogo(p.id), "ok")

    with patch("app.main._auth.decifrar", return_value=chave_gemini), \
         patch.object(ia_module, "ia_texto_disponivel", return_value=True), \
         patch("app.main._texto_pronto_cache", return_value="texto do edital"), \
         patch.object(ia_module, "analisar", return_value=_resultado_base(ed)), \
         patch("app.analise_edital._gerar", side_effect=_gerar_e_capturar):
        analise_edital(ed.id, forcar=False, user=u, db=db)

    assert estado_apos_1a_chamada["atualizado_em"] is not None      # já bateu só com a 1ª checagem
    assert estado_apos_1a_chamada["pacote_concluido_em"] is None    # mas o pacote inteiro ainda não

    db.refresh(ed)
    cache = db.query(AnaliseIAExtras).filter_by(usuario_id=u.id, edital_id=ed.id).first()
    assert cache.pacote_concluido_em is not None
    assert cache.pacote_concluido_em >= ed.analise_em
