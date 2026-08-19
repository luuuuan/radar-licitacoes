"""
Testes da verificação por IA dos documentos que o usuário já tem cadastrados
contra o que um edital exige (sem rede — a chamada à IA é mockada). Rode
com:  cd backend && pytest
"""
import json
import re
from datetime import date

from app import analise_edital as ia


def test_formatar_requisitos_junta_tecnicos_e_habilitacao():
    texto = ia._formatar_requisitos(
        ["Garantia mínima de 12 meses"],
        {"juridica": ["Contrato social"], "fiscal_trabalhista": [], "tecnica": ["Atestado de capacidade técnica"],
         "economico_financeira": [], "declaracoes": []},
    )
    assert "Garantia mínima de 12 meses" in texto
    assert "Contrato social" in texto
    assert "Atestado de capacidade técnica" in texto


def test_formatar_requisitos_vazio_retorna_mensagem_padrao():
    texto = ia._formatar_requisitos([], {})
    assert "nenhum requisito" in texto.lower()


def test_verificar_documentos_sem_chave_retorna_sem_ia():
    r = ia.verificar_documentos_usuario("Objeto", ["Garantia mínima 12 meses"], {},
                                        [{"nome": "x.pdf", "texto": "texto qualquer"}], api_key=None)
    assert r == {"status": "sem_ia"}


def test_verificar_documentos_sem_documentos_cadastrados():
    r = ia.verificar_documentos_usuario("Objeto", ["Garantia mínima 12 meses"], {}, [], api_key="fake-key")
    assert r == {"status": "sem_documentos"}


def test_verificar_documentos_sem_requisitos_do_edital():
    r = ia.verificar_documentos_usuario("Objeto", [], {}, [{"nome": "x.pdf", "texto": "algo"}], api_key="fake-key")
    assert r == {"status": "sem_requisitos"}


def test_verificar_documentos_feliz_normaliza_resposta(monkeypatch):
    resposta_ia = json.dumps({"itens": [
        {"exigido": "Garantia mínima de 12 meses", "atendido": True,
         "documento": "ficha_tecnica.pdf", "observacao": ""},
        {"exigido": "CND Receita Federal", "atendido": False, "documento": "", "observacao": ""},
    ]})
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (resposta_ia, "ok"))

    r = ia.verificar_documentos_usuario(
        "Aquisição de equipamento", ["Garantia mínima de 12 meses"], {"fiscal_trabalhista": ["CND Receita Federal"]},
        [{"nome": "ficha_tecnica.pdf", "texto": "texto extraído do documento " * 5}],
        api_key="fake-key")

    assert r["status"] == "ok"
    assert len(r["itens"]) == 2
    assert r["itens"][0]["atendido"] is True
    assert r["itens"][0]["documento"] == "ficha_tecnica.pdf"
    assert r["itens"][1]["atendido"] is False


def test_verificar_documentos_ignora_itens_sem_exigido(monkeypatch):
    resposta_ia = json.dumps({"itens": [
        {"exigido": "", "atendido": True, "documento": "x", "observacao": ""},
        {"exigido": "Garantia mínima", "atendido": True, "documento": "x.pdf", "observacao": ""},
    ]})
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (resposta_ia, "ok"))
    r = ia.verificar_documentos_usuario("Objeto", ["Garantia mínima"], {},
                                        [{"nome": "x.pdf", "texto": "texto extraído " * 5}], api_key="fake-key")
    assert len(r["itens"]) == 1
    assert r["itens"][0]["exigido"] == "Garantia mínima"


def test_verificar_documentos_erro_ia_propaga_status(monkeypatch):
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (None, "http_500"))
    r = ia.verificar_documentos_usuario("Objeto", ["Garantia mínima"], {},
                                        [{"nome": "x.pdf", "texto": "texto extraído " * 5}], api_key="fake-key")
    assert r == {"status": "erro_ia", "detalhe": "http_500"}


def test_extrair_texto_upload_pdf_usa_extracao_de_pdf(monkeypatch):
    chamadas = []
    monkeypatch.setattr(ia, "_texto_de_pdf_bytes", lambda conteudo, max_paginas, max_chars: chamadas.append(1) or "texto do pdf")
    texto = ia.extrair_texto_upload("catalogo.pdf", b"bytes-fake", "application/pdf")
    assert texto == "texto do pdf"
    assert chamadas == [1]


def test_extrair_texto_upload_imagem_sem_ocr_ativo_retorna_vazio(monkeypatch):
    monkeypatch.setattr(ia.settings, "OCR_ATIVO", False)
    texto = ia.extrair_texto_upload("foto.jpg", b"bytes-fake", "image/jpeg")
    assert texto == ""


# --------- extrair_validade_documento (cofre de documentos, v1) --------- #
# Único trabalho da IA aqui: achar a data de validade no texto. Nenhum
# destes testes cobre (nem deveria) julgamento de apto/inapto -- isso
# continua sendo só verificar_documentos_usuario, acima, intocada.

def test_extrair_validade_sem_texto_nao_chama_ia():
    assert ia.extrair_validade_documento("", api_key="fake-key") is None


def test_extrair_validade_sem_chave_retorna_none():
    assert ia.extrair_validade_documento("CND válida até 10/03/2026", api_key=None) is None


def test_extrair_validade_feliz(monkeypatch):
    monkeypatch.setattr(ia, "_gerar",
                        lambda prompt, api_key=None, timeout=30: (json.dumps({"data_validade": "2026-03-10"}), "ok"))
    assert ia.extrair_validade_documento("CND válida até 10/03/2026", api_key="fake-key") == date(2026, 3, 10)


def test_extrair_validade_ia_nao_encontra_retorna_none(monkeypatch):
    monkeypatch.setattr(ia, "_gerar",
                        lambda prompt, api_key=None, timeout=30: (json.dumps({"data_validade": None}), "ok"))
    assert ia.extrair_validade_documento("texto sem nenhuma data", api_key="fake-key") is None


def test_extrair_validade_resposta_invalida_retorna_none(monkeypatch):
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=30: ("isto não é um JSON", "ok"))
    assert ia.extrair_validade_documento("texto qualquer", api_key="fake-key") is None


def test_extrair_validade_data_mal_formada_retorna_none(monkeypatch):
    monkeypatch.setattr(ia, "_gerar",
                        lambda prompt, api_key=None, timeout=30: (json.dumps({"data_validade": "não é uma data"}), "ok"))
    assert ia.extrair_validade_documento("texto qualquer", api_key="fake-key") is None


def test_extrair_validade_erro_ia_retorna_none(monkeypatch):
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=30: (None, "http_500"))
    assert ia.extrair_validade_documento("texto qualquer", api_key="fake-key") is None


# --------- comparar_catalogo_usuario (segunda opinião da IA sobre itens) --------- #

def test_comparar_catalogo_sem_chave_retorna_sem_ia():
    r = ia.comparar_catalogo_usuario("Objeto", [{"numero": 1, "descricao": "x"}],
                                     [{"id": 1, "descricao": "y"}], api_key=None)
    assert r == {"status": "sem_ia"}


def test_comparar_catalogo_sem_itens_do_edital():
    r = ia.comparar_catalogo_usuario("Objeto", [], [{"id": 1, "descricao": "y"}], api_key="fake-key")
    assert r == {"status": "sem_itens"}


def test_comparar_catalogo_sem_catalogo():
    r = ia.comparar_catalogo_usuario("Objeto", [{"numero": 1, "descricao": "x"}], [], api_key="fake-key")
    assert r == {"status": "sem_catalogo"}


def test_comparar_catalogo_feliz_normaliza_resposta(monkeypatch):
    resposta_ia = json.dumps({"itens": [
        {"numero_item": 1, "candidatos": [
            {"produto_id": 7, "justificativa": "mesmo tipo de produto"},
        ]},
    ]})
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (resposta_ia, "ok"))

    r = ia.comparar_catalogo_usuario(
        "Aquisição de material de escritório",
        [{"numero": 1, "descricao": "Caneta esferográfica azul"}, {"numero": 2, "descricao": "Grampeador"}],
        [{"id": 7, "descricao": "Caneta esferográfica azul BIC"}],
        api_key="fake-key")

    assert r["status"] == "ok"
    assert r["itens"] == [{"numero": 1, "candidatos": [
        {"produto_id": 7, "justificativa": "mesmo tipo de produto"},
    ]}]


def test_comparar_catalogo_aceita_ate_2_candidatos_por_item(monkeypatch):
    """Pedido do usuário: quando existe mais de 1 produto genuinamente
    compatível, a IA pode sugerir até 2 — o front mostra um modal pra
    escolher qual vai pra cotação."""
    resposta_ia = json.dumps({"itens": [
        {"numero_item": 1, "candidatos": [
            {"produto_id": 7, "justificativa": "melhor opção"},
            {"produto_id": 8, "justificativa": "também compatível"},
        ]},
    ]})
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (resposta_ia, "ok"))

    r = ia.comparar_catalogo_usuario(
        "Objeto", [{"numero": 1, "descricao": "x"}],
        [{"id": 7, "descricao": "y"}, {"id": 8, "descricao": "z"}], api_key="fake-key")

    assert len(r["itens"][0]["candidatos"]) == 2
    assert [c["produto_id"] for c in r["itens"][0]["candidatos"]] == [7, 8]


def test_comparar_catalogo_corta_em_2_e_ignora_produto_id_inventado(monkeypatch):
    """A IA não pode inventar um produto_id que não existe no catálogo
    mandado — isso quebraria o botão "Adicionar na cotação" no front. Um
    candidato inválido no meio não derruba os outros válidos do mesmo item,
    e nunca sobra mais de 2."""
    resposta_ia = json.dumps({"itens": [
        {"numero_item": 1, "candidatos": [
            {"produto_id": 999, "justificativa": "inventado"},
            {"produto_id": 7, "justificativa": "válido"},
            {"produto_id": 8, "justificativa": "também válido, mas já tem 2"},
        ]},
    ]})
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (resposta_ia, "ok"))

    r = ia.comparar_catalogo_usuario(
        "Objeto", [{"numero": 1, "descricao": "x"}],
        [{"id": 7, "descricao": "y"}, {"id": 8, "descricao": "z"}], api_key="fake-key")
    assert r["itens"] == [{"numero": 1, "candidatos": [
        {"produto_id": 7, "justificativa": "válido"},
        {"produto_id": 8, "justificativa": "também válido, mas já tem 2"},
    ]}]


def test_comparar_catalogo_item_sem_candidato_valido_nenhum_fica_de_fora(monkeypatch):
    resposta_ia = json.dumps({"itens": [
        {"numero_item": 1, "candidatos": [{"produto_id": 999, "justificativa": "x"}]},
    ]})
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (resposta_ia, "ok"))

    r = ia.comparar_catalogo_usuario(
        "Objeto", [{"numero": 1, "descricao": "x"}], [{"id": 1, "descricao": "y"}], api_key="fake-key")
    assert r["itens"] == []


def test_comparar_catalogo_ignora_item_com_numero_nao_numerico(monkeypatch):
    resposta_ia = json.dumps({"itens": [
        {"numero_item": "abc", "candidatos": [{"produto_id": 1, "justificativa": "x"}]},
        {"numero_item": 2, "candidatos": [{"produto_id": 1, "justificativa": "ok"}]},
    ]})
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (resposta_ia, "ok"))

    r = ia.comparar_catalogo_usuario(
        "Objeto", [{"numero": 2, "descricao": "x"}], [{"id": 1, "descricao": "y"}], api_key="fake-key")
    assert r["itens"] == [{"numero": 2, "candidatos": [{"produto_id": 1, "justificativa": "ok"}]}]


def test_comparar_catalogo_erro_ia_propaga_status(monkeypatch):
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (None, "http_500"))
    r = ia.comparar_catalogo_usuario(
        "Objeto", [{"numero": 1, "descricao": "x"}], [{"id": 1, "descricao": "y"}], api_key="fake-key")
    assert r == {"status": "erro_ia", "detalhe": "http_500"}


def test_comparar_catalogo_resposta_truncada_vira_resposta_invalida_e_loga(monkeypatch, caplog):
    """Achado real: edital com 57 itens — a IA respondia HTTP 200 (não é
    erro_ia), mas o JSON vinha cortado no meio (provavelmente por estourar
    o teto de tokens de saída) e não dava pra parsear. Antes disso não
    ficava nenhum rastro nos logs pra diagnosticar depois."""
    resposta_cortada = '{"itens": [{"numero_item": 1, "candidatos": [{"produto_id": 7, "just'
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (resposta_cortada, "ok"))

    import logging
    with caplog.at_level(logging.WARNING, logger="ia.edital"):
        r = ia.comparar_catalogo_usuario(
            "Objeto", [{"numero": 1, "descricao": "x"}], [{"id": 7, "descricao": "y"}], api_key="fake-key")

    assert r == {"status": "resposta_invalida"}
    assert any("resposta da IA não é um JSON válido" in msg for msg in caplog.messages)


def test_gerar_manda_max_output_tokens_no_body(monkeypatch):
    """maxOutputTokens explícito evita que o Gemini corte a resposta no
    meio quando o JSON esperado é grande (muitos itens/candidatos)."""
    capturado = {}

    class _RespostaFake:
        status_code = 200
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}

    def _post_fake(url, json=None, timeout=None, headers=None):
        capturado["body"] = json
        return _RespostaFake()

    monkeypatch.setattr(ia.requests, "post", _post_fake)

    ia._gerar("prompt qualquer", api_key="fake-key")

    assert capturado["body"]["generationConfig"]["maxOutputTokens"] > 8192


# ---- Lotes: edital grande (achado real: mais de 150 itens em alguns) não
# pode mais depender de UMA chamada de IA responder tudo de uma vez só. ----

def _itens_edital(n):
    return [{"numero": i, "descricao": f"item {i}"} for i in range(1, n + 1)]


def test_comparar_catalogo_poucos_itens_continua_1_chamada_so(monkeypatch):
    """Edital pequeno (≤ tamanho de 1 lote) não muda de comportamento."""
    chamadas = []

    def _gerar_fake(prompt, api_key=None, timeout=70):
        chamadas.append(prompt)
        return json.dumps({"itens": [{"numero_item": 1, "candidatos": [
            {"produto_id": 1, "justificativa": "ok"}]}]}), "ok"

    monkeypatch.setattr(ia, "_gerar", _gerar_fake)
    r = ia.comparar_catalogo_usuario("Objeto", _itens_edital(5), [{"id": 1, "descricao": "y"}], api_key="fake-key")

    assert len(chamadas) == 1
    assert r["status"] == "ok"
    assert "lotes_com_falha" not in r


def test_comparar_catalogo_divide_em_lotes_e_junta_o_resultado(monkeypatch):
    """60 itens, lote de 25 -> 3 chamadas; cada uma devolve o item numero_item
    igual ao número do 1º item do próprio lote, só pra confirmar que o
    conteúdo de cada lote chega separado (não tudo numa prompt só)."""
    chamadas = []

    def _gerar_fake(prompt, api_key=None, timeout=70):
        chamadas.append(prompt)
        # extrai o número do primeiro item citado no prompt (tosco, mas
        # suficiente pra provar que cada chamada recebeu um pedaço diferente)
        primeiro_numero = int(re.search(r"- item (\d+):", prompt).group(1))
        return json.dumps({"itens": [{"numero_item": primeiro_numero, "candidatos": [
            {"produto_id": 1, "justificativa": "ok"}]}]}), "ok"

    monkeypatch.setattr(ia, "_gerar", _gerar_fake)
    r = ia.comparar_catalogo_usuario("Objeto", _itens_edital(60), [{"id": 1, "descricao": "y"}], api_key="fake-key")

    assert len(chamadas) == 3   # 25 + 25 + 10
    assert r["status"] == "ok"
    assert [it["numero"] for it in r["itens"]] == [1, 26, 51]
    assert "lotes_com_falha" not in r


def test_comparar_catalogo_um_lote_falha_outros_continuam_valendo(monkeypatch):
    """Achado real, pedido explícito do usuário: se ALGUNS lotes falharem,
    os itens dos lotes que deram certo continuam aparecendo — não é
    tudo-ou-nada como antes."""
    chamadas = {"n": 0}

    def _gerar_fake(prompt, api_key=None, timeout=70):
        chamadas["n"] += 1
        if chamadas["n"] == 2:
            return "isso não é um JSON", "ok"   # 2º lote falha
        primeiro_numero = int(re.search(r"- item (\d+):", prompt).group(1))
        return json.dumps({"itens": [{"numero_item": primeiro_numero, "candidatos": [
            {"produto_id": 1, "justificativa": "ok"}]}]}), "ok"

    monkeypatch.setattr(ia, "_gerar", _gerar_fake)
    r = ia.comparar_catalogo_usuario("Objeto", _itens_edital(60), [{"id": 1, "descricao": "y"}], api_key="fake-key")

    assert chamadas["n"] == 3
    assert r["status"] == "ok"
    assert [it["numero"] for it in r["itens"]] == [1, 51]   # o do meio (lote 2) faltou
    assert r["lotes_com_falha"] == 1


def test_comparar_catalogo_todos_os_lotes_falham_propaga_erro(monkeypatch):
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (None, "http_500"))
    r = ia.comparar_catalogo_usuario("Objeto", _itens_edital(60), [{"id": 1, "descricao": "y"}], api_key="fake-key")
    assert r == {"status": "erro_ia", "detalhe": "http_500"}


def test_comparar_catalogo_para_de_processar_lotes_se_cancelar(monkeypatch):
    """Cancelamento cooperativo entre lotes — mesmo espírito do cancelamento
    já usado entre as etapas da análise (nunca no meio de uma chamada em voo)."""
    chamadas = []
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (
        chamadas.append(1),
        (json.dumps({"itens": []}), "ok"))[1])

    cancelar_apos = {"n": 0}
    def deve_cancelar():
        return len(chamadas) >= 1   # cancela assim que o 1º lote termina

    r = ia.comparar_catalogo_usuario("Objeto", _itens_edital(60), [{"id": 1, "descricao": "y"}],
                                     api_key="fake-key", deve_cancelar=deve_cancelar)

    assert len(chamadas) == 1   # só o 1º lote rodou; os outros 2 foram pulados
    assert r["status"] == "ok"
