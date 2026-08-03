"""
Testes da verificação por IA dos documentos que o usuário já tem cadastrados
contra o que um edital exige (sem rede — a chamada à IA é mockada). Rode
com:  cd backend && pytest
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
