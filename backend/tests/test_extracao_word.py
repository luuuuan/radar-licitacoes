"""
Achado real: um edital com item https://app.minhalicitacao.com/edital/56743
dava "sem_texto" ("publicado como imagem/escaneado") — mas o arquivo
publicado no PNCP era um Word .doc antigo (assinatura OLE2), não uma imagem
escaneada. Depois, outro edital (24772188000154/2026/130) veio em .rtf —
mesmo sintoma, causa diferente (pypdf tentando ler RTF como PDF). O sistema
só sabia ler PDF (e .zip contendo PDFs); nunca teve suporte a Word/RTF.
Esses testes cobrem a detecção de cada formato e o despacho pro conversor
via LibreOffice — sem depender do binário 'soffice' de verdade estar
instalado (mockado). Rode com:  cd backend && pytest
"""
import io
import os
import zipfile

from app import analise_edital as ia


def _zip_bytes(arquivos: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for nome, conteudo in arquivos.items():
            zf.writestr(nome, conteudo)
    return buf.getvalue()


class _RespostaFake:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code


def test_e_docx_detecta_word_processing_xml():
    assert ia._e_docx(_zip_bytes({"word/document.xml": "<xml/>"})) is True


def test_e_docx_falso_pra_zip_comum_de_pdfs():
    assert ia._e_docx(_zip_bytes({"edital.pdf": "conteudo fake"})) is False


def test_texto_de_word_bytes_sem_libreoffice_instalado_volta_vazio(monkeypatch):
    """Mesmo espírito do OCR opcional: sem o binário (ex.: dev local sem o
    Dockerfile), não quebra — só não extrai nada."""
    monkeypatch.setattr(ia.shutil, "which", lambda nome: None)
    assert ia._texto_de_word_bytes(b"conteudo qualquer", ".doc", 1000) == ""


def test_texto_de_word_bytes_le_a_saida_da_conversao(monkeypatch):
    monkeypatch.setattr(ia.shutil, "which", lambda nome: "/usr/bin/soffice")

    def _run_fake(args, timeout=None, capture_output=None, check=None):
        outdir = args[args.index("--outdir") + 1]
        with open(os.path.join(outdir, "documento.txt"), "w", encoding="utf-8") as f:
            f.write("Texto extraído do Word de teste")
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(ia.subprocess, "run", _run_fake)

    texto = ia._texto_de_word_bytes(b"conteudo qualquer", ".docx", max_chars=1000)
    assert texto == "Texto extraído do Word de teste"


def test_texto_de_word_bytes_conversao_falha_sem_gerar_arquivo_volta_vazio(monkeypatch):
    """LibreOffice pode falhar silenciosamente (arquivo corrompido, timeout
    interno) sem lançar exceção — nesse caso o .txt de saída nunca existe."""
    monkeypatch.setattr(ia.shutil, "which", lambda nome: "/usr/bin/soffice")
    monkeypatch.setattr(ia.subprocess, "run", lambda *a, **k: None)
    assert ia._texto_de_word_bytes(b"lixo", ".doc", 1000) == ""


def test_baixar_texto_detecta_doc_ole2_e_usa_conversor_word(monkeypatch):
    conteudo = ia._MAGIC_OLE2 + b"resto do arquivo doc, bytes binarios aqui"
    monkeypatch.setattr(ia.requests, "get", lambda *a, **k: _RespostaFake(conteudo))
    chamadas = []
    monkeypatch.setattr(ia, "_texto_de_word_bytes",
                        lambda conteudo, ext, max_chars: chamadas.append(ext) or "texto do doc")

    r = ia._baixar_texto_pdf("http://exemplo/arquivo")

    assert chamadas == [".doc"]
    assert r == ("texto do doc", False)


def test_baixar_texto_detecta_docx_via_zip_e_usa_conversor_word(monkeypatch):
    conteudo = _zip_bytes({"word/document.xml": "<xml/>", "[Content_Types].xml": "<xml/>"})
    monkeypatch.setattr(ia.requests, "get", lambda *a, **k: _RespostaFake(conteudo))
    chamadas = []
    monkeypatch.setattr(ia, "_texto_de_word_bytes",
                        lambda conteudo, ext, max_chars: chamadas.append(ext) or "texto do docx")

    r = ia._baixar_texto_pdf("http://exemplo/arquivo")

    assert chamadas == [".docx"]
    assert r == ("texto do docx", False)


def test_baixar_texto_detecta_rtf_e_usa_conversor_word(monkeypatch):
    """Achado real (edital PNCP 24772188000154/2026/130): o "Edital" veio
    publicado em .rtf (Content-Type application/octet-stream, não avisa o
    formato real) — pypdf tentava ler como PDF e falhava ("invalid pdf
    header"). RTF é texto puro começando com "{\\rtf", detectável sem
    precisar abrir como zip/OLE2; reaproveita o mesmo conversor do Word."""
    conteudo = ia._MAGIC_RTF + b"1\\adeflang1025\\ansi resto do arquivo rtf aqui"
    monkeypatch.setattr(ia.requests, "get", lambda *a, **k: _RespostaFake(conteudo))
    chamadas = []
    monkeypatch.setattr(ia, "_texto_de_word_bytes",
                        lambda conteudo, ext, max_chars: chamadas.append(ext) or "texto do rtf")

    r = ia._baixar_texto_pdf("http://exemplo/arquivo")

    assert chamadas == [".rtf"]
    assert r == ("texto do rtf", False)


def test_baixar_texto_zip_de_pdfs_continua_no_caminho_antigo(monkeypatch):
    """Regressão: um .zip "burro" com PDFs soltos dentro (caso já existente,
    editais publicados como .zip com edital+anexos em PDF) não pode passar
    a ser tratado como .docx."""
    conteudo = _zip_bytes({"edital.pdf": "conteudo fake de pdf"})
    monkeypatch.setattr(ia.requests, "get", lambda *a, **k: _RespostaFake(conteudo))
    chamadas_zip, chamadas_word = [], []
    monkeypatch.setattr(ia, "_texto_de_zip", lambda *a, **k: chamadas_zip.append(1) or "texto do zip de pdfs")
    monkeypatch.setattr(ia, "_texto_de_word_bytes", lambda *a, **k: chamadas_word.append(1) or "não deveria chamar")

    r = ia._baixar_texto_pdf("http://exemplo/arquivo")

    assert chamadas_zip == [1]
    assert chamadas_word == []
    assert r == ("texto do zip de pdfs", False)


def test_baixar_texto_pdf_comum_continua_no_caminho_antigo(monkeypatch):
    conteudo = b"%PDF-1.4 conteudo fake de pdf"
    monkeypatch.setattr(ia.requests, "get", lambda *a, **k: _RespostaFake(conteudo))
    chamadas = []
    monkeypatch.setattr(ia, "_texto_de_pdf_bytes", lambda *a, **k: chamadas.append(1) or "texto do pdf")

    r = ia._baixar_texto_pdf("http://exemplo/arquivo")

    assert chamadas == [1]
    assert r == ("texto do pdf", False)


# ---------------------------------------------------------------------------
# .odt (OpenDocument Text) — achado real (edital PNCP 88830609000139/2026/371,
# usuário reportou "análise parece cortada" no edital 125821): o órgão
# publicou o edital E o termo de referência em .odt dentro de um .zip. Sem
# suporte, os dois caíam no mesmo balde do .zip "burro" de PDFs soltos
# (_texto_de_zip), que só olhava extensão .pdf — a IA analisava só os
# anexos PDF secundários (mapa de riscos, ETP) e nunca o edital de
# verdade. O aviso "análise incompleta" estava certo (faltava a seção de
# habilitação), mas o motivo real não era o teto de 80000 caracteres.
# ---------------------------------------------------------------------------

_MIMETYPE_ODT = "application/vnd.oasis.opendocument.text"


def test_e_odt_detecta_mimetype_opendocument():
    assert ia._e_odt(_zip_bytes({"mimetype": _MIMETYPE_ODT, "content.xml": "<xml/>"})) is True


def test_e_odt_falso_pra_zip_comum_de_pdfs():
    assert ia._e_odt(_zip_bytes({"edital.pdf": "conteudo fake"})) is False


def test_e_odt_falso_pra_docx():
    """docx não tem o arquivo "mimetype" do ODF -- discrimina os dois
    formatos, ambos zip por dentro."""
    assert ia._e_odt(_zip_bytes({"word/document.xml": "<xml/>"})) is False


def test_baixar_texto_detecta_odt_via_zip_e_usa_conversor_word(monkeypatch):
    conteudo = _zip_bytes({"mimetype": _MIMETYPE_ODT, "content.xml": "<xml/>"})
    monkeypatch.setattr(ia.requests, "get", lambda *a, **k: _RespostaFake(conteudo))
    chamadas = []
    monkeypatch.setattr(ia, "_texto_de_word_bytes",
                        lambda conteudo, ext, max_chars: chamadas.append(ext) or "texto do odt")

    r = ia._baixar_texto_pdf("http://exemplo/arquivo")

    assert chamadas == [".odt"]
    assert r == ("texto do odt", False)


def test_texto_de_zip_processa_odt_alem_de_pdf(monkeypatch):
    """Regressão do achado real: um zip "burro" com o edital em .odt e um
    anexo em .pdf precisa processar os DOIS, não só o .pdf."""
    conteudo = _zip_bytes({
        "edital.odt": "conteudo fake de odt",
        "anexo.pdf": "conteudo fake de pdf",
    })
    monkeypatch.setattr(ia, "_texto_de_pdf_bytes", lambda *a, **k: "texto do pdf")
    monkeypatch.setattr(ia, "_texto_de_word_bytes", lambda *a, **k: "texto do odt")

    texto = ia._texto_de_zip(conteudo, max_paginas=40, max_chars=24000)

    assert "texto do odt" in texto
    assert "texto do pdf" in texto


def test_texto_de_zip_ignora_extensao_nao_suportada():
    conteudo = _zip_bytes({"planilha.xlsx": "conteudo fake", "edital.pdf": "conteudo fake"})
    assert "planilha" not in ia._texto_de_zip(conteudo, max_paginas=40, max_chars=24000).lower()


class TestPrioridadeArquivoZip:
    def test_retificacao_vem_primeiro(self):
        assert ia._prioridade_arquivo_zip("RETIFICACAO_01.odt") == 0

    def test_edital_vem_antes_de_termo_de_referencia(self):
        assert ia._prioridade_arquivo_zip("Edital.odt") < ia._prioridade_arquivo_zip(
            "Anexo I - Termo de Referencia.odt")

    def test_nome_generico_sem_edital_no_titulo_fica_acima_de_anexo_secundario(self):
        """Achado real: o edital principal costuma vir nomeado só com um
        código de processo (ex.: "PESRP170-26.odt"), sem a palavra
        "edital" -- não pode cair no mesmo catch-all que uma minuta/ata,
        senão perde a vaga pra elas quando o zip tem mais itens do que o
        limite processado."""
        generico = ia._prioridade_arquivo_zip("PESRP170-26.odt")
        minuta = ia._prioridade_arquivo_zip("Anexo III - Minuta de Termo de Contrato.odt")
        ata = ia._prioridade_arquivo_zip("Anexo_II__Minuta_de_Ata_de_Registro_de_Precos_.odt")
        mapa_risco = ia._prioridade_arquivo_zip("Mapa_de_Riscos_ata_registro_de_precos_ETI.pdf")
        assert generico < minuta
        assert generico < ata
        assert generico < mapa_risco

    def test_termo_de_referencia_vem_antes_de_anexo_secundario(self):
        assert ia._prioridade_arquivo_zip("Anexo I - Termo de Referencia.odt") < \
            ia._prioridade_arquivo_zip("Estimativa de precos.pdf")


def test_texto_de_zip_prioriza_edital_sobre_anexo_secundario_quando_zip_grande(monkeypatch):
    """Regressão do achado real (edital 125821): com mais arquivos do que
    o limite processado (8), o edital (nome genérico de processo) e o
    termo de referência não podem perder a vaga pra minutas/atas/mapas."""
    monkeypatch.setattr(ia, "_texto_de_word_bytes", lambda conteudo, ext, max_chars: f"[odt:{max_chars}]")
    monkeypatch.setattr(ia, "_texto_de_pdf_bytes", lambda *a, **k: "[pdf]")

    arquivos = {
        "PESRP170-26.odt": "edital",  # sem a palavra "edital" no nome, de propósito
        "Anexo I - Termo de Referencia.odt": "tr",
        "Anexo III - Minuta de Termo de Contrato.odt": "minuta",
        "Anexo_II__Minuta_de_Ata_de_Registro_de_Precos_.odt": "ata",
        "COLETA_DE_ESTIMATIVAS.pdf": "estimativa",
        "Mapa_de_Riscos.pdf": "mapa",
        "Declaracao_modelo.odt": "declaracao",
        "Procuracao_modelo.odt": "procuracao",
        "Planilha_apoio.pdf": "planilha",
    }
    conteudo = _zip_bytes(arquivos)

    texto = ia._texto_de_zip(conteudo, max_paginas=40, max_chars=24000, marcar_paginas=True)

    # com 9 itens suportados e o teto de 8 arquivos processados, exatamente
    # 1 dos 7 anexos secundários (tier 4) fica de fora -- o importante é
    # que o edital (nome genérico) e o termo de referência SEMPRE entram,
    # não importa quantos anexos secundários existam.
    assert "PESRP170-26.odt" in texto
    assert "Anexo I - Termo de Referencia.odt" in texto
    assert texto.count("=== DOCUMENTO:") == 8
