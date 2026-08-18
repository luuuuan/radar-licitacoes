"""
Achado real: um edital publicado como PDF escaneado nunca conseguia
completar a descrição de itens (itens_pdf.py) quando a tabela de itens
ficava além da página 12 do documento — o OCR usado ali reaproveitava
settings.OCR_MAX_PAGINAS (12), um limite pensado pra Análise por IA, que
roda dentro da request HTTP e não pode demorar. Como itens_pdf.py roda em
segundo plano (ver _rodar_completar_descricao_bg em main.py), pode pagar
um OCR mais largo (settings.OCR_MAX_PAGINAS_ITENS) sem risco de timeout —
esses testes cobrem o encanamento de _ocr_pdf até _baixar_texto_pdf que
permite isso, mantendo o caminho síncrono (Análise por IA) sem mudança.
Rode com:  cd backend && pytest
"""
import io
from unittest.mock import patch

import pypdf

from app import analise_edital as ia


def _pdf_paginas_em_branco(n: int) -> bytes:
    """Página sem texto nenhum -- simula um PDF escaneado (container PDF
    válido, mas extract_text() não acha nada, mesmo caso de um PDF de
    imagem sem camada de texto)."""
    w = pypdf.PdfWriter()
    for _ in range(n):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_ocr_pdf_usa_max_paginas_explicito_em_vez_do_padrao(monkeypatch):
    monkeypatch.setattr(ia.settings, "OCR_ATIVO", True)
    ultimo_last_page = {}

    def _fake_convert(conteudo, dpi, first_page, last_page):
        ultimo_last_page["valor"] = last_page
        return []   # lista vazia -- não precisa OCRar nenhuma imagem de verdade

    with patch("pdf2image.convert_from_bytes", side_effect=_fake_convert):
        ia._ocr_pdf(b"fake", max_paginas=40)

    assert ultimo_last_page["valor"] == 40


def test_ocr_pdf_sem_max_paginas_explicito_cai_no_padrao_da_config(monkeypatch):
    monkeypatch.setattr(ia.settings, "OCR_ATIVO", True)
    ultimo_last_page = {}

    def _fake_convert(conteudo, dpi, first_page, last_page):
        ultimo_last_page["valor"] = last_page
        return []

    with patch("pdf2image.convert_from_bytes", side_effect=_fake_convert):
        ia._ocr_pdf(b"fake")

    assert ultimo_last_page["valor"] == ia.settings.OCR_MAX_PAGINAS


def test_texto_de_pdf_bytes_passa_max_paginas_ocr_pro_fallback(monkeypatch):
    """PDF "escaneado" (páginas em branco -> extract_text() vazio) força o
    fallback de OCR -- confirma que o max_paginas_ocr pedido por quem
    chamou (itens_pdf.py) chega até lá, e não o padrão de 12 páginas."""
    monkeypatch.setattr(ia.settings, "OCR_ATIVO", True)
    conteudo = _pdf_paginas_em_branco(1)
    chamadas = []

    def _fake_ocr(conteudo, max_paginas=None):
        chamadas.append(max_paginas)
        return "texto vindo do ocr"

    monkeypatch.setattr(ia, "_ocr_pdf", _fake_ocr)

    texto = ia._texto_de_pdf_bytes(conteudo, max_paginas=200, max_chars=10000,
                                   max_paginas_ocr=ia.settings.OCR_MAX_PAGINAS_ITENS)

    assert chamadas == [ia.settings.OCR_MAX_PAGINAS_ITENS]
    assert texto == "texto vindo do ocr"


def test_texto_de_pdf_bytes_sem_max_paginas_ocr_mantem_comportamento_antigo(monkeypatch):
    """A Análise por IA (analise_edital.analisar) não pede max_paginas_ocr
    -- continua limitada a settings.OCR_MAX_PAGINAS, sem mudança."""
    monkeypatch.setattr(ia.settings, "OCR_ATIVO", True)
    conteudo = _pdf_paginas_em_branco(1)
    chamadas = []

    def _fake_ocr(conteudo, max_paginas=None):
        chamadas.append(max_paginas)
        return "texto vindo do ocr"

    monkeypatch.setattr(ia, "_ocr_pdf", _fake_ocr)

    ia._texto_de_pdf_bytes(conteudo, max_paginas=40, max_chars=10000)

    assert chamadas == [None]   # _ocr_pdf cai no padrão (settings.OCR_MAX_PAGINAS) sozinho


def test_baixar_texto_pdf_repassa_max_paginas_ocr_pro_pdf_comum(monkeypatch):
    conteudo = b"%PDF-1.4 conteudo fake de pdf"

    class _RespostaFake:
        content = conteudo
        status_code = 200
    monkeypatch.setattr(ia.requests, "get", lambda *a, **k: _RespostaFake())

    chamadas = []
    monkeypatch.setattr(ia, "_texto_de_pdf_bytes",
                        lambda conteudo, max_paginas, max_chars, max_paginas_ocr=None:
                            chamadas.append(max_paginas_ocr) or "texto do pdf")

    ia._baixar_texto_pdf("http://exemplo/arquivo", max_paginas_ocr=99)

    assert chamadas == [99]


def test_baixar_texto_pdf_repassa_max_paginas_ocr_pro_zip_de_pdfs(monkeypatch):
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("edital.pdf", "conteudo fake de pdf")
    conteudo = buf.getvalue()

    class _RespostaFake:
        content = conteudo
        status_code = 200
    monkeypatch.setattr(ia.requests, "get", lambda *a, **k: _RespostaFake())

    chamadas = []
    monkeypatch.setattr(ia, "_texto_de_zip",
                        lambda conteudo, max_paginas, max_chars, max_paginas_ocr=None:
                            chamadas.append(max_paginas_ocr) or "texto do zip")

    ia._baixar_texto_pdf("http://exemplo/arquivo", max_paginas_ocr=77)

    assert chamadas == [77]
