"""
Achado real: editais grandes têm o texto do PDF cortado em MAX_TOTAL chars
antes de chegar na IA (limite do prompt) -- se o corte cai no meio da seção
de habilitação, o usuário via uma lista de documentos incompleta sem
nenhum aviso. "analise_incompleta" deixa a IA sinalizar esse caso
explicitamente. Rode com:  cd backend && pytest
"""
from unittest.mock import patch, MagicMock

from app.analise_edital import analisar


def _resposta_gemini(json_texto: str):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": json_texto}]}}]}
    return r


def _analisar_com_resposta(json_texto: str) -> dict:
    arquivos = [{"titulo": "Edital", "tipo": "pdf", "url": "http://x/edital.pdf"}]
    texto_pdf = "x" * 400
    with patch("app.analise_edital._baixar_texto_pdf", return_value=texto_pdf), \
         patch("app.analise_edital.requests.post", return_value=_resposta_gemini(json_texto)):
        return analisar("Objeto de teste", arquivos, api_key="fake-key")


def test_analise_incompleta_true_quando_ia_sinaliza():
    resultado = _analisar_com_resposta('{"analise_incompleta": true}')
    assert resultado["status"] == "ok"
    assert resultado["analise_incompleta"] is True


def test_analise_incompleta_false_por_padrao():
    resultado = _analisar_com_resposta('{"objeto": "teste"}')
    assert resultado["status"] == "ok"
    assert resultado["analise_incompleta"] is False
