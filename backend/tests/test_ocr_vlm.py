"""
OCR de PDF escaneado via modelo de visão (VLM) na DeepInfra, em vez do
Tesseract -- lê tabela de verdade (preserva colunas/linhas), enquanto o
Tesseract embaralha e mistura a descrição de um item com a do vizinho
(achado real, validado contra a API de verdade: edital 56106, item 24 --
ver analise_ocr_vlm.txt na raiz do repo). Tentado ANTES do Tesseract;
falha de QUALQUER tipo (sem saldo/402, erro de rede, indisponível) tem
que cair pro Tesseract sem quebrar nada -- é exatamente isso que estes
testes cobrem. Rode com: cd backend && pytest
"""
import io
from unittest.mock import patch, MagicMock

import pypdf
import requests

from app import analise_edital as ia


def _pdf_paginas_em_branco(n: int) -> bytes:
    """Container PDF válido sem texto -- simula um PDF escaneado
    (extract_text() não acha nada, força o caminho de OCR)."""
    w = pypdf.PdfWriter()
    for _ in range(n):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _resposta_ok(texto="texto transcrito da pagina"):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"choices": [{"message": {"content": texto}}]}
    return r


class _ImagemFake:
    """Suficiente pra passar por img.convert('RGB').save(buf, format='JPEG')
    sem precisar de uma imagem PIL de verdade."""
    def convert(self, modo):
        return self
    def save(self, buf, format=None, quality=None):
        buf.write(b"fake-jpeg-bytes")


# --------------------------- _chamar_vlm_pagina --------------------------- #

def test_chamar_vlm_pagina_feliz_devolve_o_texto():
    with patch("app.analise_edital.requests.post", return_value=_resposta_ok("ola")):
        assert ia._chamar_vlm_pagina("b64fake", api_key="fake-key") == "ola"


def test_chamar_vlm_pagina_402_sem_saldo_nao_retenta(monkeypatch):
    """Achado real: a chave da DeepInfra ficou sem saldo em produção e a
    completar-descricao passou a falhar com "erro_ia" -- HTTP 402. Isso
    NÃO é passageiro (retentar não muda nada), então tem que falhar na
    hora, sem gastar tentativa/tempo à toa, e devolver None pra quem
    chama tratar como "VLM indisponível" e cair pro Tesseract."""
    monkeypatch.setattr(ia.time, "sleep", lambda s: None)
    r402 = MagicMock(status_code=402, text="You need positive balance to do inference")
    with patch("app.analise_edital.requests.post", return_value=r402) as mock_post:
        resultado = ia._chamar_vlm_pagina("b64fake", api_key="fake-key")
    assert resultado is None
    assert mock_post.call_count == 1   # não retentou


def test_chamar_vlm_pagina_429_nao_retenta(monkeypatch):
    monkeypatch.setattr(ia.time, "sleep", lambda s: None)
    r429 = MagicMock(status_code=429, text="rate limit")
    with patch("app.analise_edital.requests.post", return_value=r429) as mock_post:
        resultado = ia._chamar_vlm_pagina("b64fake", api_key="fake-key")
    assert resultado is None
    assert mock_post.call_count == 1


def test_chamar_vlm_pagina_retenta_em_5xx_e_da_certo_na_segunda(monkeypatch):
    monkeypatch.setattr(ia.time, "sleep", lambda s: None)
    respostas = [MagicMock(status_code=503, text="sobrecarregado"), _resposta_ok("recuperou")]
    with patch("app.analise_edital.requests.post", side_effect=respostas) as mock_post:
        resultado = ia._chamar_vlm_pagina("b64fake", api_key="fake-key")
    assert resultado == "recuperou"
    assert mock_post.call_count == 2


def test_chamar_vlm_pagina_erro_de_rede_esgota_tentativas():
    with patch("app.analise_edital.requests.post",
              side_effect=requests.exceptions.ReadTimeout("sem resposta")) as mock_post, \
         patch("app.analise_edital.time.sleep"):
        resultado = ia._chamar_vlm_pagina("b64fake", api_key="fake-key", tentativas=2)
    assert resultado is None
    assert mock_post.call_count == 2


# ------------------------------ _ocr_pdf_vlm ------------------------------ #

def test_ocr_pdf_vlm_sem_ativado_devolve_vazio(monkeypatch):
    monkeypatch.setattr(ia.settings, "OCR_VLM_ATIVO", False)
    monkeypatch.setattr(ia.settings, "DEEPINFRA_API_KEY", "fake-key")
    assert ia._ocr_pdf_vlm(b"fake") == ""


def test_ocr_pdf_vlm_sem_chave_devolve_vazio(monkeypatch):
    monkeypatch.setattr(ia.settings, "OCR_VLM_ATIVO", True)
    monkeypatch.setattr(ia.settings, "DEEPINFRA_API_KEY", "")
    assert ia._ocr_pdf_vlm(b"fake") == ""


def test_ocr_pdf_vlm_402_devolve_vazio_pro_chamador_cair_no_tesseract(monkeypatch):
    """O teste central do pedido: sem saldo na DeepInfra não pode quebrar
    nada -- _ocr_pdf_vlm tem que devolver "" silenciosamente (sem lançar
    exceção) exatamente como se o VLM estivesse desligado."""
    monkeypatch.setattr(ia.settings, "OCR_VLM_ATIVO", True)
    monkeypatch.setattr(ia.settings, "DEEPINFRA_API_KEY", "fake-key")
    monkeypatch.setattr(ia.settings, "OCR_VLM_MAX_PAGINAS", 3)
    with patch("pdf2image.convert_from_bytes", return_value=[_ImagemFake(), _ImagemFake()]), \
         patch("app.analise_edital._chamar_vlm_pagina", return_value=None) as mock_chamar:
        resultado = ia._ocr_pdf_vlm(b"fake")
    assert resultado == ""
    assert mock_chamar.call_count == 1   # 1ª página já falhou (402) -- não insiste nas demais


def test_ocr_pdf_vlm_usa_seu_proprio_limite_de_paginas_nao_o_do_tesseract(monkeypatch):
    monkeypatch.setattr(ia.settings, "OCR_VLM_ATIVO", True)
    monkeypatch.setattr(ia.settings, "DEEPINFRA_API_KEY", "fake-key")
    monkeypatch.setattr(ia.settings, "OCR_VLM_MAX_PAGINAS", 15)
    monkeypatch.setattr(ia.settings, "OCR_MAX_PAGINAS_ITENS", 40)   # não deve influenciar o VLM
    chamadas = {}
    def _fake_convert(conteudo, dpi, first_page, last_page, timeout=None):
        chamadas["last_page"] = last_page
        return []
    with patch("pdf2image.convert_from_bytes", side_effect=_fake_convert):
        ia._ocr_pdf_vlm(b"fake")
    assert chamadas["last_page"] == 15


def test_ocr_pdf_vlm_junta_texto_das_paginas_e_limpa_caractere_corrompido(monkeypatch):
    """Achado real (teste contra a API de verdade): caracteres especiais
    (², –) às vezes saem como replacement character (U+FFFD) no próprio
    payload da API -- não é bug de decodificação do cliente. A instrução
    do prompt ajuda, mas não garante 100%; a saída final não pode conter
    "�" de jeito nenhum."""
    monkeypatch.setattr(ia.settings, "OCR_VLM_ATIVO", True)
    monkeypatch.setattr(ia.settings, "DEEPINFRA_API_KEY", "fake-key")
    with patch("pdf2image.convert_from_bytes", return_value=[_ImagemFake(), _ImagemFake()]), \
         patch("app.analise_edital._chamar_vlm_pagina",
              side_effect=["pagina 1: 75 g/m� sem problema", "pagina 2: ok"]):
        resultado = ia._ocr_pdf_vlm(b"fake")
    assert "�" not in resultado
    assert "pagina 1: 75 g/m sem problema" in resultado
    assert "pagina 2: ok" in resultado


# ----------------------- fallback em _texto_de_pdf_bytes ------------------ #

def test_texto_de_pdf_bytes_vlm_falha_cai_pro_tesseract_sem_quebrar(monkeypatch):
    """Ponta a ponta: VLM indisponível/falhando (ex.: 402) não pode impedir
    o Tesseract de continuar funcionando como sempre funcionou -- a
    completar-descricao/coleta não pode quebrar por causa disso."""
    monkeypatch.setattr(ia.settings, "OCR_ATIVO", True)
    conteudo = _pdf_paginas_em_branco(1)
    with patch("app.analise_edital._ocr_pdf_vlm", return_value="") as mock_vlm, \
         patch("app.analise_edital._ocr_pdf", return_value="texto do tesseract, como sempre") as mock_tess:
        texto = ia._texto_de_pdf_bytes(conteudo, max_paginas=40, max_chars=10000)
    assert mock_vlm.called
    assert mock_tess.called
    assert texto == "texto do tesseract, como sempre"


def test_texto_de_pdf_bytes_vlm_funciona_nao_chama_tesseract(monkeypatch):
    monkeypatch.setattr(ia.settings, "OCR_ATIVO", True)
    conteudo = _pdf_paginas_em_branco(1)
    with patch("app.analise_edital._ocr_pdf_vlm", return_value="texto do vlm, com tabela certinha") as mock_vlm, \
         patch("app.analise_edital._ocr_pdf") as mock_tess:
        texto = ia._texto_de_pdf_bytes(conteudo, max_paginas=40, max_chars=10000)
    assert mock_vlm.called
    assert not mock_tess.called   # VLM deu certo -- nem precisou tentar o Tesseract
    assert texto == "texto do vlm, com tabela certinha"
