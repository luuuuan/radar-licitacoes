"""
Pedido do usuário: quando a disputa é por lote (não dá pra disputar 1 item
isolado), a análise por IA passa a identificar também a composição de cada
lote (quais números de item pertencem a ele), pra depois cruzar com o
catálogo do usuário e mostrar quais lotes ele consegue cobrir por inteiro
(ver test_lotes_cobertura.py pro cruzamento em si). Rode com:
cd backend && pytest
"""
import json
from unittest.mock import patch, MagicMock

from app import analise_edital as ia


def _resposta_gemini(dados: dict):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps(dados)}]}}]}
    return r


def _analisar_com_resposta(dados: dict):
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]

    def _fake_baixar(url, max_chars=24000, **kw):
        return "texto de edital " * 30, False

    with patch("app.analise_edital._baixar_texto_pdf", side_effect=_fake_baixar), \
         patch("app.analise_edital.requests.post", return_value=_resposta_gemini(dados)):
        return ia.analisar("Objeto de teste", arquivos, api_key="fake-key")


def test_analisar_normaliza_lotes_com_itens_inteiros():
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [
            {"numero": "1", "itens": [1, 2, 3], "descricao": "Papelaria"},
            {"numero": "2", "itens": [4, 5], "descricao": "Material de limpeza"},
        ],
    })
    assert resultado["status"] == "ok"
    assert resultado["lotes"] == [
        {"numero": "1", "itens": [1, 2, 3], "descricao": "Papelaria"},
        {"numero": "2", "itens": [4, 5], "descricao": "Material de limpeza"},
    ]


def test_analisar_sem_lotes_no_campo_retorna_lista_vazia():
    resultado = _analisar_com_resposta({"julgamento": "item"})
    assert resultado["status"] == "ok"
    assert resultado["lotes"] == []


def test_analisar_lote_sem_numero_e_descartado():
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [{"numero": "", "itens": [1], "descricao": "x"},
                  {"numero": "2", "itens": [4], "descricao": "válido"}],
    })
    assert resultado["lotes"] == [{"numero": "2", "itens": [4], "descricao": "válido"}]


def test_analisar_item_de_lote_nao_numerico_e_descartado_sem_quebrar_o_lote():
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [{"numero": "1", "itens": [1, "abc", 3], "descricao": "x"}],
    })
    assert resultado["lotes"] == [{"numero": "1", "itens": [1, 3], "descricao": "x"}]


def test_analisar_lotes_nao_e_lista_retorna_vazio():
    resultado = _analisar_com_resposta({"julgamento": "item", "lotes": "não é uma lista"})
    assert resultado["lotes"] == []


def test_analisar_item_float_nao_e_truncado_e_sim_descartado():
    """Achado real (agente debugger): int(4.5) == 4 truncava silenciosamente
    em vez de rejeitar -- um float coincidindo com um item inteiro
    legítimo do mesmo lote colidia com ele sem nenhum sinal de que algo
    tinha sido corrompido."""
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [{"numero": "1", "itens": [1, 4.5, 2], "descricao": "x"}],
    })
    assert resultado["lotes"] == [{"numero": "1", "itens": [1, 2], "descricao": "x"}]


def test_analisar_item_bool_e_descartado_nao_vira_1_ou_0():
    """Achado real (agente debugger): bool é subclasse de int em Python --
    int(True) == 1 aceitava um valor que nunca deveria ter sido um número
    de item."""
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [{"numero": "1", "itens": [1, True, 2], "descricao": "x"}],
    })
    assert resultado["lotes"] == [{"numero": "1", "itens": [1, 2], "descricao": "x"}]


def test_analisar_item_zero_ou_negativo_e_descartado():
    """Numeração de item de edital começa em 1 -- 0/negativo nunca é um
    item real e deixaria o lote marcado como "não coberto" por um motivo
    que não tem nada a ver com o catálogo do usuário."""
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [{"numero": "1", "itens": [0, -1, 3], "descricao": "x"}],
    })
    assert resultado["lotes"] == [{"numero": "1", "itens": [3], "descricao": "x"}]


def test_analisar_item_duplicado_no_mesmo_lote_e_deduplicado():
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [{"numero": "1", "itens": [1, 1, 2], "descricao": "x"}],
    })
    assert resultado["lotes"] == [{"numero": "1", "itens": [1, 2], "descricao": "x"}]


def test_analisar_lote_com_numero_duplicado_mantem_so_a_primeira_ocorrencia():
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [{"numero": "1", "itens": [1], "descricao": "primeira"},
                  {"numero": "1", "itens": [2], "descricao": "duplicada"}],
    })
    assert resultado["lotes"] == [{"numero": "1", "itens": [1], "descricao": "primeira"}]


def test_analisar_usa_max_output_tokens_maior_que_o_padrao(monkeypatch):
    """Achado real (agente backend-architect): a resposta de analisar()
    cresce com o Nº de itens do edital (por causa de "lotes"), e diferente
    de comparar_catalogo_usuario() (que já divide em lotes de chamada),
    analisar() é uma chamada única -- um corte aqui derruba a análise
    inteira, não só a lista de lotes. Por isso usa um teto de tokens de
    saída maior que o padrão de _gerar()."""
    capturado = {}

    def _fake_gerar(prompt, api_key=None, timeout=70, tentativas=2,
                    response_schema=None, max_output_tokens=16384):
        capturado["max_output_tokens"] = max_output_tokens
        return '{"julgamento": "item"}', "ok"

    monkeypatch.setattr(ia, "_gerar", _fake_gerar)
    ia.analisar("Objeto de teste", [], api_key="fake-key",
               texto_pronto={"texto": "texto de edital " * 30, "fonte": None})

    assert capturado["max_output_tokens"] > 16384
