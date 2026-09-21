"""
Achado real (edital 142070, PNCP 20765627000140/2026/45): PDF de 40 páginas,
38 delas escaneadas (0 chars extraídos por página) -- só 2 no meio (um
formulário digitado, não escaneado) tinham texto de verdade. A SOMA dessas 2
páginas (quase 4 mil caracteres) já passava do limiar antigo de OCR (500
chars totais), então o OCR nunca era acionado -- o texto de verdade do
edital (bem provavelmente nas páginas escaneadas: objeto, habilitação)
ficava perdido pra sempre, mesmo com a maior parte do documento nunca lida,
e a análise saía incompleta sem nenhum aviso além de "analise_incompleta".
_texto_de_pdf_bytes agora também aciona OCR quando a MAIORIA das páginas
processadas veio vazia, não só quando a soma total é pequena. Rode com:
cd backend && pytest
"""
import io
from unittest.mock import patch

import pypdf
from fpdf import FPDF

from app import analise_edital as ia


def _pagina_com_texto(texto: str) -> pypdf.PageObject:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 10, texto)
    leitor = pypdf.PdfReader(io.BytesIO(bytes(pdf.output())))
    return leitor.pages[0]


def _pdf_misto(paginas_em_branco: int, textos_paginas_com_texto: list[str]) -> bytes:
    """Simula um PDF majoritariamente escaneado (páginas em branco = sem
    texto extraível, como um scan) com algumas páginas digitadas no meio --
    mesmo formato do edital 142070 real (8 escaneadas, 2 digitadas)."""
    w = pypdf.PdfWriter()
    for _ in range(paginas_em_branco):
        w.add_blank_page(width=595, height=842)
    for texto in textos_paginas_com_texto:
        w.add_page(_pagina_com_texto(texto))
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_pdf_maioria_paginas_vazias_aciona_ocr_mesmo_com_soma_acima_do_limiar(monkeypatch):
    """8 páginas em branco + 2 com bastante texto (soma > 500 chars, o
    limiar antigo) -- antes da correção, o OCR nunca era chamado aqui."""
    monkeypatch.setattr(ia.settings, "OCR_ATIVO", True)
    texto_pagina = "Texto digitado real, bem extenso, repetido bastante. " * 10   # > 500 chars
    conteudo = _pdf_misto(paginas_em_branco=8, textos_paginas_com_texto=[texto_pagina, texto_pagina])
    with patch("app.analise_edital._ocr_pdf_vlm", return_value="") as mock_vlm, \
         patch("app.analise_edital._ocr_pdf", return_value="texto recuperado via OCR das paginas escaneadas") as mock_tess:
        texto = ia._texto_de_pdf_bytes(conteudo, max_paginas=40, max_chars=80000)
    assert mock_vlm.called
    assert mock_tess.called
    assert texto == "texto recuperado via OCR das paginas escaneadas"


def test_pdf_com_poucas_paginas_vazias_nao_aciona_ocr(monkeypatch):
    """Contraste: a maioria das páginas TEM texto real (só 1 de 5 em
    branco, ex.: uma folha de rosto/separador) -- não é um PDF escaneado,
    não deve gastar OCR à toa."""
    monkeypatch.setattr(ia.settings, "OCR_ATIVO", True)
    texto_pagina = "Texto digitado real de uma pagina qualquer do edital. " * 10
    conteudo = _pdf_misto(paginas_em_branco=1,
                          textos_paginas_com_texto=[texto_pagina] * 4)
    with patch("app.analise_edital._ocr_pdf_vlm") as mock_vlm, \
         patch("app.analise_edital._ocr_pdf") as mock_tess:
        texto = ia._texto_de_pdf_bytes(conteudo, max_paginas=40, max_chars=80000)
    assert not mock_vlm.called
    assert not mock_tess.called
    assert "Texto digitado real" in texto


def test_pdf_totalmente_vazio_continua_acionando_ocr_pelo_limiar_de_soma(monkeypatch):
    """Não regride o comportamento já existente: PDF 100% em branco (soma
    de texto = 0, bem abaixo do limiar de 500) continua acionando OCR,
    igual antes desta correção -- ver test_ocr_vlm.py."""
    monkeypatch.setattr(ia.settings, "OCR_ATIVO", True)
    conteudo = _pdf_misto(paginas_em_branco=3, textos_paginas_com_texto=[])
    with patch("app.analise_edital._ocr_pdf_vlm", return_value="") as mock_vlm, \
         patch("app.analise_edital._ocr_pdf", return_value="texto do tesseract") as mock_tess:
        texto = ia._texto_de_pdf_bytes(conteudo, max_paginas=40, max_chars=80000)
    assert mock_vlm.called
    assert mock_tess.called
    assert texto == "texto do tesseract"
