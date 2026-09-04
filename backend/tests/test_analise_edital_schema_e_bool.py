"""
Achado real (auditoria do prompt-engineer): bool("false") é True em Python
-- se a IA (ou uma resposta sem o response_schema aplicado) mandasse a
STRING "false" num campo booleano, o valor virava true silenciosamente,
invertendo o sentido do campo (ex.: "aceita_similar" viraria true quando o
edital diz que não aceita). Também confirma que analisar() manda o
response_schema pro Gemini (força tipo/obrigatoriedade no decoder, reduz
ainda mais o espaço pra esse tipo de erro). Rode com:  cd backend && pytest
"""
from unittest.mock import patch, MagicMock

from app.analise_edital import analisar, _RESPONSE_SCHEMA


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


def test_string_false_nao_vira_true_num_campo_booleano():
    resultado = _analisar_com_resposta('{"dados_proposta": {"aceita_similar": "false"}}')
    assert resultado["status"] == "ok"
    assert resultado["dados_proposta"]["aceita_similar"] is False


def test_string_nao_tambem_conta_como_false():
    resultado = _analisar_com_resposta('{"exige_amostra": "não"}')
    assert resultado["exige_amostra"] is False


def test_string_true_continua_true():
    resultado = _analisar_com_resposta('{"exige_visita": "true"}')
    assert resultado["exige_visita"] is True


def test_boolean_de_verdade_continua_funcionando():
    resultado = _analisar_com_resposta('{"exclusivo_me_epp": true}')
    assert resultado["exclusivo_me_epp"] is True


def test_analisar_manda_response_schema_pro_gemini():
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    chamadas = []

    def _fake_post(url, **kw):
        chamadas.append(kw)
        return _resposta_gemini('{"objeto": "teste"}')

    with patch("app.analise_edital._baixar_texto_pdf", return_value="x" * 400), \
         patch("app.analise_edital.requests.post", side_effect=_fake_post):
        analisar("Objeto de teste", arquivos, api_key="fake-key")

    assert len(chamadas) == 1
    assert chamadas[0]["json"]["generationConfig"]["responseSchema"] == _RESPONSE_SCHEMA


def test_schema_cobre_todas_as_chaves_top_level_obrigatorias():
    esperadas = {
        "objeto", "documentos_habilitacao", "requisitos_tecnicos", "dados_orgao",
        "dados_proposta", "validade_documentos_habilitacao", "prazos",
        "exige_amostra", "exige_visita", "exclusivo_me_epp", "julgamento",
        "garantia_contratual", "analise_incompleta", "pontos_atencao",
    }
    assert set(_RESPONSE_SCHEMA["properties"].keys()) == esperadas
    assert set(_RESPONSE_SCHEMA["required"]) == esperadas
