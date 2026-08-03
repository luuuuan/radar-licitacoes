"""
Testes da verificação de documento do usuário contra o edital (sem rede —
a chamada à IA é mockada). Rode com:  cd backend && pytest
"""
import json

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


def test_analisar_documento_sem_chave_retorna_sem_ia():
    r = ia.analisar_documento_usuario("Objeto", [], {}, "arquivo.pdf", "texto qualquer bem longo o suficiente",
                                      api_key=None)
    assert r == {"status": "sem_ia"}


def test_analisar_documento_texto_curto_demais_retorna_sem_texto():
    r = ia.analisar_documento_usuario("Objeto", [], {}, "arquivo.pdf", "abc", api_key="fake-key")
    assert r == {"status": "sem_texto"}


def test_analisar_documento_feliz_normaliza_resposta(monkeypatch):
    resposta_ia = json.dumps({
        "classificacao": "Atende parcialmente",
        "resumo": "Cobre a maior parte, falta a garantia mínima.",
        "pontos_atendidos": ["Certificação X"],
        "pontos_nao_atendidos": ["Garantia mínima de 12 meses"],
    })
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (resposta_ia, "ok"))

    r = ia.analisar_documento_usuario(
        "Aquisição de equipamento", ["Garantia mínima de 12 meses"], {},
        "ficha_tecnica.pdf", "texto extraído do documento do fornecedor " * 3,
        api_key="fake-key")

    assert r["status"] == "ok"
    assert r["classificacao"] == "Atende parcialmente"
    assert r["pontos_atendidos"] == ["Certificação X"]
    assert r["pontos_nao_atendidos"] == ["Garantia mínima de 12 meses"]


def test_analisar_documento_classificacao_invalida_vira_nao_verificavel(monkeypatch):
    """A IA às vezes foge do enum pedido no prompt — não pode quebrar o
    front (que só sabe colorir os 3 rótulos + Nao_verificavel)."""
    resposta_ia = json.dumps({"classificacao": "talvez", "resumo": "", "pontos_atendidos": [], "pontos_nao_atendidos": []})
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (resposta_ia, "ok"))

    r = ia.analisar_documento_usuario("Objeto", [], {}, "x.pdf", "texto qualquer bem longo o suficiente " * 3,
                                      api_key="fake-key")
    assert r["classificacao"] == "Nao_verificavel"


def test_analisar_documento_erro_ia_propaga_status(monkeypatch):
    monkeypatch.setattr(ia, "_gerar", lambda prompt, api_key=None, timeout=70: (None, "http_500"))
    r = ia.analisar_documento_usuario("Objeto", [], {}, "x.pdf", "texto qualquer bem longo o suficiente " * 3,
                                      api_key="fake-key")
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
