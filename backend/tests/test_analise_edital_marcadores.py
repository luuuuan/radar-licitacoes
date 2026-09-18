"""
Achado real (pedido do usuário): a Análise por IA dizia "modelo no Anexo IV"
sem dizer EM QUAL arquivo nem EM QUE PÁGINA -- quando o edital vem em mais
de um documento (edital + termo de referência/anexo separados), o usuário
não sabia onde procurar. Pra IA conseguir apontar isso sem inventar, o texto
mandado pra ela agora carrega marcadores reais: "=== DOCUMENTO: <nome> ==="
no início de cada arquivo combinado, e "[pág. N]" antes de cada página.
Rode com:  cd backend && pytest
"""
import json
import pypdf
from unittest.mock import patch, MagicMock

from app import analise_edital as ia


def _corpo_de(kw: dict) -> dict:
    """_chamar_modelo manda o corpo via data= (bytes), não json= -- ver
    ensure_ascii=False em _post_com_retry (achado real: prompt grande em
    português inflava até 3x com ensure_ascii=True, estourando o teto de
    tamanho de requisição do Gemini)."""
    return json.loads(kw["data"].decode("utf-8"))


class _PaginaFake:
    def __init__(self, texto):
        self._texto = texto

    def extract_text(self):
        return self._texto


class _LeitorFake:
    def __init__(self, *a, **k):
        self.pages = [_PaginaFake("primeira pagina"), _PaginaFake("segunda pagina")]


# --------- _texto_de_pdf_bytes: marcador de página --------- #

def test_marcar_paginas_insere_marcador_por_pagina(monkeypatch):
    monkeypatch.setattr(pypdf, "PdfReader", _LeitorFake)
    texto = ia._texto_de_pdf_bytes(b"fake", max_paginas=10, max_chars=10000, marcar_paginas=True)
    assert "[pág. 1]" in texto and "[pág. 2]" in texto
    assert texto.index("[pág. 1]") < texto.index("primeira pagina")
    assert texto.index("[pág. 2]") < texto.index("segunda pagina")


def test_sem_marcar_paginas_nao_insere_marcador_nenhum(monkeypatch):
    """Comportamento padrão (marcar_paginas=False) -- usado por
    itens_pdf.py, que pede pra IA copiar a descrição do item como está no
    edital; um marcador solto no meio do texto viraria lixo colado nela."""
    monkeypatch.setattr(pypdf, "PdfReader", _LeitorFake)
    texto = ia._texto_de_pdf_bytes(b"fake", max_paginas=10, max_chars=10000)
    assert "[pág." not in texto


# --------- analisar(): cabeçalho de documento no texto combinado --------- #

def _resposta_gemini(json_texto: str):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": json_texto}]}}]}
    return r


def test_analisar_marca_cada_documento_combinado_com_seu_titulo():
    arquivos = [
        {"titulo": "Edital de Pregão nº 16/2026", "url": "http://x/edital.pdf"},
        {"titulo": "Termo de Referência", "url": "http://x/tr.pdf"},
    ]
    textos = {
        "http://x/edital.pdf": "cláusula de habilitação " * 30,
        "http://x/tr.pdf": "especificação técnica do objeto " * 30,
    }

    def _fake_baixar(url, max_chars=24000, **kw):
        return textos[url][:max_chars], False

    chamadas = []

    def _fake_post(url, **kw):
        chamadas.append(kw)
        return _resposta_gemini('{"objeto": "teste"}')

    with patch("app.analise_edital._baixar_texto_pdf", side_effect=_fake_baixar), \
         patch("app.analise_edital.requests.post", side_effect=_fake_post):
        resultado = ia.analisar("Objeto de teste", arquivos, api_key="fake-key")

    assert resultado["status"] == "ok"
    texto_enviado = _corpo_de(chamadas[0])["contents"][0]["parts"][0]["text"]
    assert "=== DOCUMENTO: Edital de Pregão nº 16/2026 ===" in texto_enviado
    assert "=== DOCUMENTO: Termo de Referência ===" in texto_enviado
    # o cabeçalho do 2º documento vem depois do texto do 1º -- confirma que
    # cada cabeçalho está de fato colado no início do bloco certo, não os
    # dois jogados soltos em qualquer lugar do prompt.
    pos_doc1 = texto_enviado.index("=== DOCUMENTO: Edital de Pregão nº 16/2026 ===")
    pos_texto1 = texto_enviado.index("cláusula de habilitação")
    pos_doc2 = texto_enviado.index("=== DOCUMENTO: Termo de Referência ===")
    pos_texto2 = texto_enviado.index("especificação técnica do objeto")
    assert pos_doc1 < pos_texto1 < pos_doc2 < pos_texto2


def test_analisar_pede_marcacao_de_pagina_ao_baixar_pdf():
    """analisar() precisa pedir marcar_paginas=True em toda chamada de
    download -- sem isso, o prompt promete "[pág. N]" pra IA mas o texto
    de verdade nunca teria esse marcador."""
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    recebido = {}

    def _fake_baixar(url, max_chars=24000, **kw):
        recebido.update(kw)
        return "conteúdo de teste " * 30, False

    with patch("app.analise_edital._baixar_texto_pdf", side_effect=_fake_baixar), \
         patch("app.analise_edital.requests.post", return_value=_resposta_gemini('{"objeto": "teste"}')):
        ia.analisar("Objeto de teste", arquivos, api_key="fake-key")

    assert recebido.get("marcar_paginas") is True
