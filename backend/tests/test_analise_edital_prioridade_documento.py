"""
Achado real (edital 127082, reportado pelo usuário): quando o PNCP tem uma
retificação alterando data/condições da sessão, o edital original sozinho
já enche o limite de caracteres do prompt (MAX_TOTAL) -- a IA lia só o
texto desatualizado e devolvia a "data da sessão" errada. _prioridade_arquivo
agora prioriza retificação/errata/aditamento antes do edital original, pra
garantir que o texto mais atualizado sempre entre no que é mandado pra IA.
Rode com:  cd backend && pytest
"""
from unittest.mock import patch, MagicMock

from app.analise_edital import analisar, _prioridade_arquivo


def _texto_enviado_a_ia(chamada_post_kwargs) -> str:
    return chamada_post_kwargs["json"]["contents"][0]["parts"][0]["text"]


def test_prioridade_arquivo_prioriza_retificacao_antes_do_edital():
    arquivos = [
        {"titulo": "Edital de Pregão nº 16/2026", "url": "http://x/edital.pdf"},
        {"titulo": "Retificação do Edital nº 16/2026", "url": "http://x/retificacao.pdf"},
        {"titulo": "Termo de Referência", "url": "http://x/tr.pdf"},
    ]
    ordenados = sorted(arquivos, key=_prioridade_arquivo)
    assert [a["titulo"] for a in ordenados] == [
        "Retificação do Edital nº 16/2026",
        "Edital de Pregão nº 16/2026",
        "Termo de Referência",
    ]


def test_prioridade_arquivo_reconhece_errata_e_aditamento():
    assert _prioridade_arquivo({"titulo": "Errata nº 1"}) == 0
    assert _prioridade_arquivo({"titulo": "1º Aditamento ao Edital"}) == 0
    assert _prioridade_arquivo({"titulo": "Edital"}) == 1
    assert _prioridade_arquivo({"titulo": "Anexo I - Termo de Referência"}) == 2
    assert _prioridade_arquivo({"titulo": "Modelo de Declaração"}) == 3


def _resposta_gemini(json_texto: str):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": json_texto}]}}]}
    return r


def test_analisar_inclui_texto_da_retificacao_mesmo_com_edital_grande():
    """O edital original sozinho já bate o limite de MAX_TOTAL (24000 chars)
    -- sem a prioridade certa, o texto da retificação nunca chegaria a ser
    baixado nem entraria no prompt mandado pra IA."""
    arquivos = [
        {"titulo": "Edital", "url": "http://x/edital.pdf"},
        {"titulo": "Retificação", "url": "http://x/retificacao.pdf"},
    ]
    textos = {
        "http://x/edital.pdf": "A" * 30000,
        # >300 chars, senão analisar() descarta como "documento vazio demais"
        "http://x/retificacao.pdf": "MARCADOR-RETIFICACAO nova data de sessao 15/09/2026. " * 10,
    }

    def _fake_baixar(url, max_chars=24000, **kw):
        return textos[url][:max_chars]

    chamadas = []

    def _fake_post(url, **kw):
        chamadas.append(kw)
        return _resposta_gemini('{"objeto": "teste"}')

    with patch("app.analise_edital._baixar_texto_pdf", side_effect=_fake_baixar), \
         patch("app.analise_edital.requests.post", side_effect=_fake_post):
        resultado = analisar("Objeto de teste", arquivos, api_key="fake-key")

    assert resultado["status"] == "ok"
    assert len(chamadas) == 1
    assert "MARCADOR-RETIFICACAO" in _texto_enviado_a_ia(chamadas[0])
