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


def _analisar_com_resposta(dados: dict, total_itens=None):
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]

    def _fake_baixar(url, max_chars=24000, **kw):
        return "texto de edital " * 30, False

    with patch("app.analise_edital._baixar_texto_pdf", side_effect=_fake_baixar), \
         patch("app.analise_edital.requests.post", return_value=_resposta_gemini(dados)):
        return ia.analisar("Objeto de teste", arquivos, api_key="fake-key", total_itens=total_itens)


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


def test_analisar_item_repetido_em_lotes_diferentes_descarta_todos_os_lotes():
    """Achado real (edital 141995, PNCP 01641472000196/2026/17): a IA
    reiniciou a contagem de item EM CADA lote (1, 2, 3...) em vez de usar
    a numeração global do edital -- item 1 aparecia em 3 lotes ao mesmo
    tempo (Lote 1: itens 1-20, Lote 2: item 1, Lote 3: itens 1-3), embora
    lotes por definição particionem os itens (cada item pertence a um só
    lote). "Papel Sulfite" (o item de verdade do Lote 2) acabava agrupado
    visualmente no Lote 1 só porque a faixa 1-20 dele por coincidência
    cobria o número errado. Mais confiável descartar TODA a lista de
    lotes (mesma semântica de "não identificado com segurança" que o
    campo já documenta) do que arriscar mostrar uma composição errada."""
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [
            {"numero": "1", "itens": list(range(1, 21)), "descricao": "MATERIAL DE EXPEDIENTE - DIVERSOS"},
            {"numero": "2", "itens": [1], "descricao": "PAPEL SULFITE"},
            {"numero": "3", "itens": [1, 2, 3], "descricao": "EQUIPAMENTOS"},
        ],
    })
    assert resultado["lotes"] == []


def test_analisar_lotes_com_itens_realmente_exclusivos_nao_e_descartado():
    """Contraste com o teste acima: quando os itens de fato não se repetem
    entre lotes (o caso normal, correto), a lista continua valendo."""
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [
            {"numero": "1", "itens": [1, 2, 3], "descricao": "Papelaria"},
            {"numero": "2", "itens": [4, 5], "descricao": "Material de limpeza"},
        ],
    })
    assert resultado["lotes"] == [
        {"numero": "1", "itens": [1, 2, 3], "descricao": "Papelaria"},
        {"numero": "2", "itens": [4, 5], "descricao": "Material de limpeza"},
    ]


# --------- cobertura dos lotes x Nº real de itens do edital --------- #
# Achado real (edital 143879, PNCP 83891283000136/2026/1054, 223 itens):
# o Termo de Referência sozinho já tinha ~98000 caracteres, estourando
# MAX_TOTAL (80000) -- o texto que chegou até a IA já vinha cortado ANTES
# dela ver, então "analise_incompleta" saiu false (pra ela, o texto só
# "acabava" num ponto que parecia normal) mesmo os lotes só cobrindo os
# itens 1-141 de 223. Comparar a cobertura real contra o total de itens
# (dado estruturado do PNCP, não depende de IA) pega esse caso que o
# autorrelato do modelo não pega.

def test_analisar_cobertura_de_lotes_menor_que_total_marca_incompleta():
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "analise_incompleta": False,   # a própria IA não percebeu o corte
        "lotes": [
            {"numero": "1", "itens": list(range(1, 51)), "descricao": "Lote grande 1"},
            {"numero": "2", "itens": list(range(51, 100)), "descricao": "Lote grande 2"},
        ],
    }, total_itens=223)
    assert resultado["analise_incompleta"] is True
    assert any("incompleta" in p.lower() for p in resultado["pontos_atencao"])


def test_analisar_cobertura_completa_dos_lotes_nao_marca_incompleta():
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [
            {"numero": "1", "itens": [1, 2, 3], "descricao": "Papelaria"},
            {"numero": "2", "itens": [4, 5], "descricao": "Material de limpeza"},
        ],
    }, total_itens=5)
    assert resultado["analise_incompleta"] is False


def test_analisar_pequena_folga_de_cobertura_nao_marca_incompleta():
    """2 itens de folga (ex.: um item fora da numeração normal) não conta
    como truncamento de verdade -- só uma lacuna GRANDE conta."""
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [
            {"numero": "1", "itens": [1, 2, 3], "descricao": "Papelaria"},
        ],
    }, total_itens=5)   # cobre 3 de 5 -- 2 de folga, dentro da tolerância
    assert resultado["analise_incompleta"] is False


def test_analisar_sem_total_itens_nao_faz_a_checagem():
    """total_itens=None (chamador não passou, ou dado indisponível) --
    comportamento de antes, sem essa checagem nova."""
    resultado = _analisar_com_resposta({
        "julgamento": "lote",
        "lotes": [{"numero": "1", "itens": [1, 2], "descricao": "Papelaria"}],
    }, total_itens=None)
    assert resultado["analise_incompleta"] is False


def test_analisar_sem_lotes_nao_faz_a_checagem_de_cobertura():
    """julgamento "item" (sem lotes) não tem esse sinal disponível -- não
    pode marcar incompleta por um motivo que não dá pra medir aqui."""
    resultado = _analisar_com_resposta({"julgamento": "item"}, total_itens=223)
    assert resultado["lotes"] == []
    assert resultado["analise_incompleta"] is False


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
