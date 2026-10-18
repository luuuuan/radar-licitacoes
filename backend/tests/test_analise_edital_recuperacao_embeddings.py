"""
Achado real (edital 155685, UFFS 90013/2026, pedido do usuário): o corte
bruto nos primeiros MAX_TOTAL caracteres cobria o Edital inteiro (78645
chars) + só ~1300 chars do Termo de Referência (354635 chars, 181 páginas)
-- a IA nunca viu a seção real de "Qualificação técnica da Contratada" (pág.
101), que dizia explicitamente NÃO exigir atestado de capacidade técnica, e
inventou essa exigência pra preencher a lacuna.

v16 troca o corte cego por recuperação via embeddings (BGE-M3/DeepInfra) nas
9 categorias padrão de habilitação da Lei 14.133 quando o texto combinado
excede MAX_TOTAL, com fallback de múltiplos pedaços pro caso raríssimo de
nem os trechos recuperados caberem numa chamada só, e uma checagem
determinística de confiança (independente do autorrelato da IA) baseada na
pior similaridade encontrada entre as 9 consultas. Rode com:
cd backend && pytest
"""
import json
from unittest.mock import patch, MagicMock

from app.analise_edital import (
    analisar,
    _truncar_justo_entre_documentos,
    _selecionar_trechos_relevantes_analise,
    _mesclar_resultados_analise,
    _analisar_texto_unico,
    _CONSULTAS_HABILITACAO_ANALISE,
    _LIMIAR_CONFIANCA_ANALISE,
    MAX_TOTAL,
)


def _resposta_gemini(json_texto: str):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": json_texto}]}}]}
    return r


# --------------------------------------------------------------------- #
# _truncar_justo_entre_documentos
# --------------------------------------------------------------------- #

def test_truncar_justo_reparte_igualmente_entre_documentos():
    bruto = ("=== DOCUMENTO: A ===\n" + "a" * 1000) + "\n\n---\n\n" + ("=== DOCUMENTO: B ===\n" + "b" * 1000)
    resultado = _truncar_justo_entre_documentos(bruto, 400)
    # 2 documentos, 400 de orçamento -- 200 pra cada, não 400 pro 1º só
    partes = resultado.split("\n\n---\n\n")
    assert len(partes) == 2
    assert len(partes[0]) <= 200
    assert len(partes[1]) <= 200
    assert len(resultado) <= 400


def test_truncar_justo_documento_unico_usa_orcamento_inteiro():
    bruto = "=== DOCUMENTO: A ===\n" + "a" * 1000
    resultado = _truncar_justo_entre_documentos(bruto, 400)
    assert len(resultado) == 400


def test_truncar_justo_documento_pequeno_nao_desperdica_sobra_do_outro():
    # achado real (edital 141844): doc curto não precisa do teto inteiro --
    # mas o excedente não sobra automaticamente pro outro (reparte IGUAL,
    # não tenta redistribuir) -- ver docstring da função.
    bruto = ("=== DOCUMENTO: A ===\n" + "a" * 50) + "\n\n---\n\n" + ("=== DOCUMENTO: B ===\n" + "b" * 1000)
    resultado = _truncar_justo_entre_documentos(bruto, 400)
    partes = resultado.split("\n\n---\n\n")
    assert "a" * 50 in partes[0]   # o pequeno inteiro cabe na fatia de 200
    assert len(partes[1]) <= 200


# --------------------------------------------------------------------- #
# _selecionar_trechos_relevantes_analise
# --------------------------------------------------------------------- #

def test_selecionar_trechos_documento_pequeno_nao_chama_embeddings():
    texto = "x" * 4000   # <= 2 pedaços de 2500 -- early return, sem IA nenhuma
    with patch("app.analise_edital.embeddings_deepinfra") as mock_emb:
        trecho, confianca_baixa = _selecionar_trechos_relevantes_analise(texto)
    mock_emb.assert_not_called()
    assert trecho == texto[:MAX_TOTAL]
    assert confianca_baixa is False


def test_selecionar_trechos_embeddings_indisponivel_marca_confianca_baixa():
    texto = "x" * 10000   # > 2 pedaços, aciona o caminho de embeddings
    with patch("app.analise_edital.embeddings_deepinfra", return_value=[None] * 20):
        trecho, confianca_baixa = _selecionar_trechos_relevantes_analise(texto)
    assert confianca_baixa is True
    assert len(trecho) <= MAX_TOTAL


def test_selecionar_trechos_acha_pedaco_mais_parecido():
    """Simula embeddings de verdade com vetores 2-D de direção (não
    magnitude -- cosseno de dois vetores 1-D positivos é SEMPRE 1.0,
    independente do valor, já que 1-D não tem "ângulo" de verdade; direção
    em 2-D é o jeito mínimo de simular similaridade alta/baixa de verdade).
    O pedaço cuja direção bate com uma consulta é o que deveria ser
    escolhido pra ela -- confirma que o pedaço-alvo entra no texto final."""
    chunk_tam = 2500
    n_pedacos = 5
    # cada bloco com EXATAMENTE chunk_tam chars, pra "PEDACO-N-" cair bem no
    # início do pedaço que _selecionar_trechos_relevantes_analise corta
    # (achado real deste teste: "PEDACO-{i}-" tem tamanho variável -- 9
    # chars pra i de 1 dígito -- um padding fixo por fora desalinhava os
    # blocos manuais com o corte de tamanho fixo da função, de propósito
    # testada aqui).
    blocos = [f"PEDACO-{i}-" for i in range(n_pedacos)]
    texto = "".join(b + ("x" * (chunk_tam - len(b))) for b in blocos)
    assert len(texto) // chunk_tam >= n_pedacos - 1

    ALINHADO, ORTOGONAL = [1.0, 0.0], [0.0, 1.0]

    def _vetores_fake(textos, api_key=None):
        out = []
        for t in textos:
            if t.startswith("PEDACO-2-"):
                out.append(ALINHADO)
            elif t.startswith("PEDACO-"):
                out.append(ORTOGONAL)
            else:
                # é uma das consultas -- a 1ª pede o que o pedaço 2 tem
                out.append(ALINHADO if t == _CONSULTAS_HABILITACAO_ANALISE[0] else ORTOGONAL)
        return out

    with patch("app.analise_edital.embeddings_deepinfra", side_effect=_vetores_fake):
        trecho, confianca_baixa = _selecionar_trechos_relevantes_analise(texto)
    assert "PEDACO-2-" in trecho


def test_selecionar_trechos_pior_score_abaixo_do_limiar_marca_confianca_baixa():
    texto = "x" * 10000

    def _vetores_fake(textos, api_key=None):
        # TODOS os pedaços numa direção, TODAS as consultas na direção
        # ORTOGONAL -- cosseno 0.0 garantido pra qualquer par, abaixo do
        # piso de propósito (ver nota de vetor 2-D no teste acima).
        return [[0.0, 1.0] if t in _CONSULTAS_HABILITACAO_ANALISE else [1.0, 0.0] for t in textos]

    with patch("app.analise_edital.embeddings_deepinfra", side_effect=_vetores_fake):
        trecho, confianca_baixa = _selecionar_trechos_relevantes_analise(texto)
    assert confianca_baixa is True


def test_selecionar_trechos_pior_score_acima_do_limiar_confianca_alta():
    import math
    texto = "x" * 10000
    score_bom = _LIMIAR_CONFIANCA_ANALISE + 0.3
    # vetor 2-D com cosseno EXATO = score_bom contra [1,0] -- ver nota de
    # vetor 2-D no teste "acha_pedaco_mais_parecido" acima (1-D não serve
    # pra simular um cosseno específico).
    vetor_alvo = [score_bom, math.sqrt(1 - score_bom ** 2)]

    def _vetores_fake(textos, api_key=None):
        return [[1.0, 0.0] if t in _CONSULTAS_HABILITACAO_ANALISE else vetor_alvo for t in textos]

    with patch("app.analise_edital.embeddings_deepinfra", side_effect=_vetores_fake):
        trecho, confianca_baixa = _selecionar_trechos_relevantes_analise(texto)
    assert confianca_baixa is False


# --------------------------------------------------------------------- #
# analisar() de ponta a ponta -- confirma que a seção "perdida" pelo corte
# bruto agora chega na IA quando os embeddings encontram ela.
# --------------------------------------------------------------------- #

def test_analisar_recupera_secao_que_o_corte_bruto_perderia():
    # edital pequeno + termo de referência gigante -- juntos passam de
    # MAX_TOTAL, e a seção-alvo só existe bem no fim do 2º documento (fora
    # do que o corte cego dos primeiros MAX_TOTAL chars alcançaria).
    edital = "x" * 70000
    tr_antes = "y" * 60000
    marcador = "MARCADOR-QUALIFICACAO-TECNICA nao exige atestado"
    tr_depois = "z" * 60000
    tr = tr_antes + marcador + tr_depois
    assert len(edital) + len(tr) > MAX_TOTAL
    # confirma a premissa do teste: o corte cego (só os primeiros MAX_TOTAL
    # chars do texto combinado) NÃO alcançaria o marcador.
    bruto_combinado = f"=== DOCUMENTO: Edital ===\n{edital}\n\n---\n\n=== DOCUMENTO: Termo de Referência ===\n{tr}"
    assert marcador not in bruto_combinado[:MAX_TOTAL]

    arquivos = [
        {"titulo": "Edital", "url": "http://x/edital.pdf"},
        {"titulo": "Termo de Referência", "url": "http://x/tr.pdf"},
    ]
    textos = {"http://x/edital.pdf": edital, "http://x/tr.pdf": tr}

    def _fake_baixar(url, max_chars=400000, **kw):
        return textos[url][:max_chars], False

    ALINHADO, ORTOGONAL = [1.0, 0.0], [0.0, 1.0]

    def _vetores_fake(textos_in, api_key=None):
        # só o pedaço que contém o marcador "bate" com a consulta de
        # qualificação técnica (índice 2 em _CONSULTAS_HABILITACAO_ANALISE)
        # -- vetores de DIREÇÃO (2-D), não magnitude (ver nota de vetor 2-D
        # em test_selecionar_trechos_acha_pedaco_mais_parecido acima).
        out = []
        for t in textos_in:
            if t in _CONSULTAS_HABILITACAO_ANALISE:
                out.append(ALINHADO if t == _CONSULTAS_HABILITACAO_ANALISE[2] else ORTOGONAL)
            else:
                out.append(ALINHADO if marcador in t else ORTOGONAL)
        return out

    chamadas_gemini = []

    def _fake_post(url, **kw):
        if "data" in kw:   # chamada de verdade pro Gemini
            chamadas_gemini.append(json.loads(kw["data"].decode("utf-8"))["contents"][0]["parts"][0]["text"])
        return _resposta_gemini('{"objeto": "teste"}')

    with patch("app.analise_edital._baixar_texto_pdf", side_effect=_fake_baixar), \
         patch("app.analise_edital.embeddings_deepinfra", side_effect=_vetores_fake), \
         patch("app.analise_edital.requests.post", side_effect=_fake_post):
        resultado = analisar("Objeto de teste", arquivos, api_key="fake-key")

    assert resultado["status"] == "ok"
    assert len(chamadas_gemini) == 1   # coube numa chamada só (sem fallback de múltiplos pedaços)
    assert marcador in chamadas_gemini[0]


def test_analisar_sem_corte_nao_aciona_embeddings():
    """Texto pequeno (não passa de MAX_TOTAL) -- caminho rápido, sem chamar
    embeddings nenhuma: não muda nada pra essa fatia de editais (achado
    real: a maioria dos editais já cabia certinho antes do v16)."""
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    texto_pequeno = "x" * 1000

    def _fake_baixar(url, max_chars=400000, **kw):
        return texto_pequeno, False

    with patch("app.analise_edital._baixar_texto_pdf", side_effect=_fake_baixar), \
         patch("app.analise_edital.embeddings_deepinfra") as mock_emb, \
         patch("app.analise_edital.requests.post", return_value=_resposta_gemini('{"objeto": "teste"}')):
        resultado = analisar("Objeto de teste", arquivos, api_key="fake-key")

    mock_emb.assert_not_called()
    assert resultado["status"] == "ok"
    assert resultado["analise_incompleta"] is False


def test_analisar_marca_incompleta_quando_confianca_baixa_mesmo_ia_nao_percebendo():
    """Pedido do usuário: prefere analise_incompleta de sobra a deixar
    passar batido -- mesmo quando a IA responde sem sinalizar nada de
    errado, a checagem determinística (pior score abaixo do piso) marca
    incompleta sozinha."""
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    texto_grande = "x" * 90000

    def _fake_baixar(url, max_chars=400000, **kw):
        return texto_grande, False

    def _vetores_sempre_baixo(textos_in, api_key=None):
        # pedaços numa direção, consultas na ORTOGONAL -- cosseno 0.0
        # garantido (ver nota de vetor 2-D em
        # test_selecionar_trechos_acha_pedaco_mais_parecido acima).
        return [[0.0, 1.0] if t in _CONSULTAS_HABILITACAO_ANALISE else [1.0, 0.0] for t in textos_in]

    with patch("app.analise_edital._baixar_texto_pdf", side_effect=_fake_baixar), \
         patch("app.analise_edital.embeddings_deepinfra", side_effect=_vetores_sempre_baixo), \
         patch("app.analise_edital.requests.post", return_value=_resposta_gemini('{"analise_incompleta": false}')):
        resultado = analisar("Objeto de teste", arquivos, api_key="fake-key")

    assert resultado["status"] == "ok"
    assert resultado["analise_incompleta"] is True
    assert any("confiança" in p for p in resultado["pontos_atencao"])


# --------------------------------------------------------------------- #
# fallback de múltiplos pedaços (raríssimo: nem o texto recuperado cabe
# numa chamada só) -- _mesclar_resultados_analise
# --------------------------------------------------------------------- #

def test_mesclar_uniao_de_listas_remove_duplicata():
    base = {"status": "ok", "versao": 16, "objeto": "", "documentos_habilitacao":
            {"juridica": [], "fiscal_trabalhista": [], "tecnica": [], "economico_financeira": [], "declaracoes": []},
            "validade_documentos_habilitacao": "", "requisitos_tecnicos": [], "dados_orgao": {}, "dados_proposta": {},
            "prazos": [], "exige_amostra": False, "exige_visita": False, "exclusivo_me_epp": False,
            "julgamento": "", "lotes": [], "garantia_contratual": "", "analise_incompleta": False, "pontos_atencao": []}
    r1 = {**base, "documentos_habilitacao": {**base["documentos_habilitacao"],
          "tecnica": ["Atestado de capacidade técnica"]}, "prazos": ["Sessão: 10/10/2026"]}
    r2 = {**base, "documentos_habilitacao": {**base["documentos_habilitacao"],
          "tecnica": ["atestado de capacidade técnica", "Registro em conselho de classe"]}, "prazos": ["Sessão: 10/10/2026"]}

    mesclado = _mesclar_resultados_analise([r1, r2])

    # "Atestado..." e "atestado..." (maiúscula/minúscula) contam como o
    # mesmo item -- dedupe por texto normalizado, mantém a 1ª grafia
    assert mesclado["documentos_habilitacao"]["tecnica"] == [
        "Atestado de capacidade técnica", "Registro em conselho de classe"]
    assert mesclado["prazos"] == ["Sessão: 10/10/2026"]   # duplicado entre os 2 pedaços, só 1 vez


def test_mesclar_booleano_vira_true_se_qualquer_pedaco_disser_sim():
    base = {"status": "ok", "versao": 16, "objeto": "", "documentos_habilitacao":
            {"juridica": [], "fiscal_trabalhista": [], "tecnica": [], "economico_financeira": [], "declaracoes": []},
            "validade_documentos_habilitacao": "", "requisitos_tecnicos": [], "dados_orgao": {}, "dados_proposta": {},
            "prazos": [], "exige_amostra": False, "exige_visita": False, "exclusivo_me_epp": False,
            "julgamento": "", "lotes": [], "garantia_contratual": "", "analise_incompleta": False, "pontos_atencao": []}
    r1 = {**base, "exige_amostra": False}
    r2 = {**base, "exige_amostra": True}   # a exigência de amostra só aparecia neste pedaço

    mesclado = _mesclar_resultados_analise([r1, r2])

    assert mesclado["exige_amostra"] is True


def test_mesclar_texto_unico_usa_primeiro_nao_vazio():
    base = {"status": "ok", "versao": 16, "objeto": "", "documentos_habilitacao":
            {"juridica": [], "fiscal_trabalhista": [], "tecnica": [], "economico_financeira": [], "declaracoes": []},
            "validade_documentos_habilitacao": "", "requisitos_tecnicos": [], "dados_orgao": {}, "dados_proposta": {},
            "prazos": [], "exige_amostra": False, "exige_visita": False, "exclusivo_me_epp": False,
            "julgamento": "", "lotes": [], "garantia_contratual": "", "analise_incompleta": False, "pontos_atencao": []}
    r1 = {**base, "garantia_contratual": ""}   # este pedaço não viu a cláusula de garantia
    r2 = {**base, "garantia_contratual": "5% do valor do contrato"}

    mesclado = _mesclar_resultados_analise([r1, r2])

    assert mesclado["garantia_contratual"] == "5% do valor do contrato"


def test_mesclar_declaracoes_dedup_por_nome():
    base = {"status": "ok", "versao": 16, "objeto": "", "documentos_habilitacao":
            {"juridica": [], "fiscal_trabalhista": [], "tecnica": [], "economico_financeira": [], "declaracoes": []},
            "validade_documentos_habilitacao": "", "requisitos_tecnicos": [], "dados_orgao": {}, "dados_proposta": {},
            "prazos": [], "exige_amostra": False, "exige_visita": False, "exclusivo_me_epp": False,
            "julgamento": "", "lotes": [], "garantia_contratual": "", "analise_incompleta": False, "pontos_atencao": []}
    decl = {"nome": "Declaração de ME/EPP", "modelo_orgao": True, "detalhe": "Anexo III"}
    r1 = {**base, "documentos_habilitacao": {**base["documentos_habilitacao"], "declaracoes": [decl]}}
    r2 = {**base, "documentos_habilitacao": {**base["documentos_habilitacao"], "declaracoes": [decl]}}

    mesclado = _mesclar_resultados_analise([r1, r2])

    assert len(mesclado["documentos_habilitacao"]["declaracoes"]) == 1


def test_mesclar_lotes_descarta_tudo_se_item_colide_entre_pedacos():
    """Mesma invariante de lotes() em _analisar_texto_unico (um item só
    pode pertencer a UM lote) -- aqui verificada depois de JUNTAR pedaços,
    já que cada um só validou a si mesmo."""
    base = {"status": "ok", "versao": 16, "objeto": "", "documentos_habilitacao":
            {"juridica": [], "fiscal_trabalhista": [], "tecnica": [], "economico_financeira": [], "declaracoes": []},
            "validade_documentos_habilitacao": "", "requisitos_tecnicos": [], "dados_orgao": {}, "dados_proposta": {},
            "prazos": [], "exige_amostra": False, "exige_visita": False, "exclusivo_me_epp": False,
            "julgamento": "item", "garantia_contratual": "", "analise_incompleta": False, "pontos_atencao": []}
    r1 = {**base, "lotes": [{"numero": "1", "itens": [1, 2], "descricao": "A"}]}
    r2 = {**base, "lotes": [{"numero": "2", "itens": [2, 3], "descricao": "B"}]}   # item 2 também aqui -- colide

    mesclado = _mesclar_resultados_analise([r1, r2])

    assert mesclado["lotes"] == []
