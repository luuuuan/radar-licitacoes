"""
Testes de extrair_itens_completos() (completa descrição de item lendo o PDF
do edital via DeepInfra/DeepSeek — chave global do operador, não a Gemini
pessoal do usuário). Sem rede — a chamada à IA e o download do PDF são
mockados. Rode com:  cd backend && pytest
"""
import json
from unittest.mock import patch

from app import itens_pdf as ip


def test_extrair_sem_chave_retorna_sem_ia(monkeypatch):
    # força "sem chave" independente do .env local (dev pode ter uma chave
    # real configurada pra testar manualmente contra a API de verdade)
    monkeypatch.setattr(ip.settings, "DEEPINFRA_API_KEY", "")
    r = ip.extrair_itens_completos("Objeto", [{"titulo": "Edital", "url": "http://x"}],
                                   [{"numero": 1, "descricao": "curta"}], api_key=None)
    assert r == {"status": "sem_ia"}


def test_extrair_sem_arquivos_retorna_sem_arquivo():
    r = ip.extrair_itens_completos("Objeto", [], [{"numero": 1, "descricao": "curta"}], api_key="fake-key")
    assert r == {"status": "sem_arquivo"}


def test_extrair_sem_itens_retorna_sem_itens():
    r = ip.extrair_itens_completos("Objeto", [{"titulo": "Edital", "url": "http://x"}], [], api_key="fake-key")
    assert r == {"status": "sem_itens"}


def test_extrair_pdf_sem_texto_suficiente_retorna_sem_texto(monkeypatch):
    monkeypatch.setattr(ip, "_baixar_texto_pdf", lambda url, max_paginas, max_chars, max_paginas_ocr=None: "")
    r = ip.extrair_itens_completos(
        "Objeto", [{"titulo": "Edital", "url": "http://x"}],
        [{"numero": 1, "descricao": "curta"}], api_key="fake-key")
    assert r == {"status": "sem_texto"}


def test_extrair_feliz_normaliza_e_filtra_numeros_invalidos(monkeypatch):
    monkeypatch.setattr(ip, "_baixar_texto_pdf",
                        lambda url, max_paginas, max_chars, max_paginas_ocr=None: "texto do edital " * 50)
    resposta_ia = json.dumps({"itens": [
        {"numero_item": 15, "descricao_completa": "Caneta esferográfica com tinta azul, fluxo uniforme..."},
        {"numero_item": 999, "descricao_completa": "não deveria aparecer — número não existe nos itens de referência"},
        {"numero_item": "abc", "descricao_completa": "número inválido, ignorado"},
        {"numero_item": 16, "descricao_completa": ""},
    ]})
    with patch("app.itens_pdf._gerar", return_value=(resposta_ia, "ok")) as mock_gerar:
        r = ip.extrair_itens_completos(
            "Aquisição de material de escritório",
            [{"titulo": "Edital", "url": "http://x"}],
            [{"numero": 15, "descricao": "Caneta Esferográfica ... venti"},
             {"numero": 16, "descricao": "Caneta marca texto"}],
            api_key="fake-key")
    assert mock_gerar.called
    assert r["status"] == "ok"
    assert r["itens"] == [{"numero": 15, "descricao_completa": "Caneta esferográfica com tinta azul, fluxo uniforme..."}]


def test_extrair_erro_ia_propaga_status(monkeypatch):
    monkeypatch.setattr(ip, "_baixar_texto_pdf",
                        lambda url, max_paginas, max_chars, max_paginas_ocr=None: "texto do edital " * 50)
    with patch("app.itens_pdf._gerar", return_value=(None, "http_500")):
        r = ip.extrair_itens_completos(
            "Objeto", [{"titulo": "Edital", "url": "http://x"}],
            [{"numero": 1, "descricao": "curta"}], api_key="fake-key")
    assert r == {"status": "erro_ia", "detalhe": "http_500"}


def test_extrair_resposta_sem_json_valido_retorna_resposta_invalida(monkeypatch):
    monkeypatch.setattr(ip, "_baixar_texto_pdf",
                        lambda url, max_paginas, max_chars, max_paginas_ocr=None: "texto do edital " * 50)
    with patch("app.itens_pdf._gerar", return_value=("isso não é json", "ok")):
        r = ip.extrair_itens_completos(
            "Objeto", [{"titulo": "Edital", "url": "http://x"}],
            [{"numero": 1, "descricao": "curta"}], api_key="fake-key")
    assert r == {"status": "resposta_invalida"}


def test_extrair_usa_limite_de_ocr_maior_que_o_da_analise_sincrona(monkeypatch):
    """Achado real: um edital escaneado com a tabela de itens além da
    página 12 nunca completava nada — o OCR usado aqui reaproveitava
    settings.OCR_MAX_PAGINAS (12), pensado pra Análise por IA, que roda
    dentro da request HTTP e não pode demorar. extrair_itens_completos()
    roda em segundo plano (ver _rodar_completar_descricao_bg em main.py),
    então pode pagar um OCR mais largo — settings.OCR_MAX_PAGINAS_ITENS."""
    chamadas = []
    def _fake_baixar(url, max_paginas, max_chars, max_paginas_ocr=None):
        chamadas.append(max_paginas_ocr)
        return "texto do edital " * 50
    monkeypatch.setattr(ip, "_baixar_texto_pdf", _fake_baixar)
    with patch("app.itens_pdf._gerar", return_value=('{"itens": []}', "ok")):
        ip.extrair_itens_completos(
            "Objeto", [{"titulo": "Edital", "url": "http://x"}],
            [{"numero": 1, "descricao": "curta"}], api_key="fake-key")
    assert chamadas == [ip.settings.OCR_MAX_PAGINAS_ITENS]
    assert ip.settings.OCR_MAX_PAGINAS_ITENS > ip.settings.OCR_MAX_PAGINAS
