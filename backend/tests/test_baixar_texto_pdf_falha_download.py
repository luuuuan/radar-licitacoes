"""
Achado real (edital 122399, reportado pelo usuário): a Análise por IA
mostrou "está publicado como imagem/escaneado, sem texto que a IA
consiga ler" -- mas esse mesmo PDF tinha texto extraível de verdade (o
PNCP só estava instável no momento, 503 passageiro). _baixar_texto_pdf
tratava "não consegui nem baixar o arquivo" (rede/PNCP fora do ar) igual
a "baixei e realmente não tem texto legível" (scan ruim) -- mesmo
raciocínio da correção de erro_arquivos_pncp, um nível mais fundo: ali é
a LISTA de arquivos, aqui é o CONTEÚDO de um arquivo específico. Rode
com:  cd backend && pytest
"""
from unittest.mock import patch, MagicMock

import requests

from app.analise_edital import _baixar_texto_pdf, analisar
from app import itens_pdf


def _resposta(status_code=200, content=b"", exception=None):
    if exception:
        raise exception
    r = MagicMock()
    r.status_code = status_code
    r.content = content
    return r


# --------- _baixar_texto_pdf: falhou_busca distingue "não baixou" --------- #
# de "baixou e não tinha texto" --------- #

def test_falha_de_rede_marca_falhou_busca_true(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    with patch("app.analise_edital.requests.get",
              side_effect=requests.exceptions.ConnectionError("falhou")):
        texto, falhou = _baixar_texto_pdf("http://x/edital.pdf")
    assert texto == ""
    assert falhou is True


def test_http_503_persistente_marca_falhou_busca_true(monkeypatch):
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    with patch("app.analise_edital.requests.get",
              return_value=MagicMock(status_code=503, content=b"")):
        texto, falhou = _baixar_texto_pdf("http://x/edital.pdf")
    assert texto == ""
    assert falhou is True


def test_http_503_passageiro_retenta_e_da_certo(monkeypatch):
    """PNCP instável (503 passageiro, visto em produção) -- retenta 1x
    antes de desistir; se a 2ª tentativa funciona, não marca falha."""
    monkeypatch.setattr("app.analise_edital.time.sleep", lambda s: None)
    respostas = [
        MagicMock(status_code=503, content=b""),
        MagicMock(status_code=200, content=b"%PDF-1.4 conteudo fake de pdf"),
    ]
    with patch("app.analise_edital.requests.get", side_effect=respostas), \
         patch("app.analise_edital._texto_de_pdf_bytes", return_value="texto extraido"):
        texto, falhou = _baixar_texto_pdf("http://x/edital.pdf")
    assert texto == "texto extraido"
    assert falhou is False


def test_pdf_baixado_com_sucesso_mas_sem_texto_nao_marca_falha(monkeypatch):
    """Scan ruim/OCR indisponível: baixou os bytes, só não extraiu texto
    -- isso É "sem texto legível" de verdade, não falha de busca."""
    with patch("app.analise_edital.requests.get",
              return_value=MagicMock(status_code=200, content=b"%PDF-1.4 fake")), \
         patch("app.analise_edital._texto_de_pdf_bytes", return_value=""):
        texto, falhou = _baixar_texto_pdf("http://x/edital.pdf")
    assert texto == ""
    assert falhou is False


# --------- analisar(): status erro_download_pdf x sem_texto --------- #

def _resposta_gemini_ok(json_texto='{"objeto": "teste"}'):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": json_texto}]}}]}
    return r


def test_analisar_falha_de_download_vira_erro_download_pdf_nao_sem_texto(monkeypatch):
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    with patch("app.analise_edital._baixar_texto_pdf", return_value=("", True)):
        resultado = analisar("Objeto de teste", arquivos, api_key="fake-key")
    assert resultado["status"] == "erro_download_pdf"
    assert resultado["status"] != "sem_texto"


def test_analisar_pdf_genuinamente_sem_texto_continua_sem_texto(monkeypatch):
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    with patch("app.analise_edital._baixar_texto_pdf", return_value=("", False)):
        resultado = analisar("Objeto de teste", arquivos, api_key="fake-key")
    assert resultado["status"] == "sem_texto"


def test_analisar_sucesso_quando_download_funciona(monkeypatch):
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    with patch("app.analise_edital._baixar_texto_pdf", return_value=("x" * 400, False)), \
         patch("app.analise_edital.requests.post", return_value=_resposta_gemini_ok()):
        resultado = analisar("Objeto de teste", arquivos, api_key="fake-key")
    assert resultado["status"] == "ok"


# --------- extrair_itens_completos() (itens_pdf.py): mesmo raciocínio --------- #

def test_extrair_itens_falha_de_download_vira_erro_download_pdf():
    with patch("app.itens_pdf._baixar_texto_pdf", return_value=("", True)):
        r = itens_pdf.extrair_itens_completos(
            "Objeto", [{"titulo": "Edital", "url": "http://x"}],
            [{"numero": 1, "descricao": "curta"}], api_key="fake-key")
    assert r == {"status": "erro_download_pdf"}


def test_extrair_itens_genuinamente_sem_texto_continua_sem_texto():
    with patch("app.itens_pdf._baixar_texto_pdf", return_value=("", False)):
        r = itens_pdf.extrair_itens_completos(
            "Objeto", [{"titulo": "Edital", "url": "http://x"}],
            [{"numero": 1, "descricao": "curta"}], api_key="fake-key")
    assert r == {"status": "sem_texto"}
