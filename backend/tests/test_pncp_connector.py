"""
Achados da auditoria do agente debugger em app/connectors/pncp.py:

1. _coletar_itens buscava só a 1ª página (100 itens) de cada contratação e
   nunca ia atrás do resto -- editais com mais de 100 itens (comuns em
   compra de material/uniforme/TI) perdiam os itens 101+ em silêncio.
2. _coletar_modalidade_uf confiava cego em "totalPaginas" -- se a API mandar
   esse campo ausente/zero mas a página vier cheia, parava de paginar
   achando que só tinha 1 página, mesmo tendo mais.
3. _parse_data tinha um bug de precedência de operador (`A if C else B`
   dentro de um slice) que fazia os formatos sem "%f" nunca cortarem o
   valor -- só não dava pra notar porque o fallback (fromisoformat) pegava
   a maioria dos casos reais.

Sem HTTP de verdade -- troca self.http (requests.Session) por uma sessão
fake com uma fila de respostas. Rode com:  cd backend && pytest
"""
from datetime import date

from app.connectors.pncp import PNCPConnector, _parse_data


class _RespostaFake:
    def __init__(self, status_code=200, dados=None):
        self.status_code = status_code
        self._dados = dados
        self.text = ""
        self.headers = {}

    def json(self):
        return self._dados


class _SessaoFake:
    """Fila de respostas fixas, uma por chamada .get() (na ordem)."""
    def __init__(self, respostas):
        self._respostas = list(respostas)
        self.chamadas: list[tuple[str, dict]] = []
        self.headers = {}   # requests.Session tem isso; PNCPConnector.__init__ chama .update()

    def get(self, url, params=None, timeout=None):
        self.chamadas.append((url, dict(params or {})))
        return self._respostas.pop(0)


def _conector(respostas):
    sessao = _SessaoFake(respostas)
    c = PNCPConnector(session=sessao, ufs="SP", modalidades="6", horizonte=10)
    c.delay = 0   # sem espera de verdade no teste
    return c, sessao


# --------- _parse_data --------- #

def test_parse_data_none_ou_vazio():
    assert _parse_data(None) is None
    assert _parse_data("") is None


def test_parse_data_datetime_sem_fracao():
    assert _parse_data("2024-01-15T10:30:00") == date(2024, 1, 15)


def test_parse_data_so_data():
    assert _parse_data("2024-01-15") == date(2024, 1, 15)


def test_parse_data_com_fracao_de_segundo():
    assert _parse_data("2024-01-15T10:30:00.123456") == date(2024, 1, 15)


def test_parse_data_com_timezone_cai_no_fallback_fromisoformat():
    assert _parse_data("2024-01-15T10:30:00-03:00") == date(2024, 1, 15)


def test_parse_data_invalida_devolve_none():
    assert _parse_data("isso nao e uma data") is None


# --------- _coletar_itens: paginação --------- #

def test_coletar_itens_busca_so_1_pagina_quando_vem_menos_que_o_tamanho():
    resp = _RespostaFake(dados={"data": [{"numeroItem": 1, "descricao": "Item 1"}]})
    c, sessao = _conector([resp])
    itens = c._coletar_itens("123", 2024, 1)
    assert len(itens) == 1
    assert len(sessao.chamadas) == 1


def test_coletar_itens_pagina_ate_a_pagina_vir_incompleta():
    """O achado principal: mais de 100 itens não pode ser truncado."""
    cheia = [{"numeroItem": i, "descricao": f"Item {i}"} for i in range(1, 101)]
    pagina1 = _RespostaFake(dados={"data": cheia})
    pagina2 = _RespostaFake(dados={"data": [{"numeroItem": 101, "descricao": "Item 101"}]})
    c, sessao = _conector([pagina1, pagina2])
    itens = c._coletar_itens("123", 2024, 1)
    assert len(itens) == 101
    assert [numero for numero in (chamada[1]["pagina"] for chamada in sessao.chamadas)] == [1, 2]


def test_coletar_itens_lista_direta_sem_envelope_data():
    resp = _RespostaFake(dados=[{"numeroItem": 1, "descricao": "Item solto"}])
    c, sessao = _conector([resp])
    itens = c._coletar_itens("123", 2024, 1)
    assert len(itens) == 1
    assert itens[0].descricao == "Item solto"


def test_coletar_itens_sem_identificadores_nao_faz_requisicao():
    c, sessao = _conector([])
    assert c._coletar_itens(None, 2024, 1) == []
    assert sessao.chamadas == []


def test_coletar_itens_http_erro_para_sem_quebrar():
    # _get_com_retry tenta de novo em 5xx -- preenche respostas suficientes
    # pra cobrir todas as tentativas sem esgotar a fila da sessão fake.
    c, sessao = _conector([_RespostaFake(status_code=500) for _ in range(10)])
    itens = c._coletar_itens("123", 2024, 1)
    assert itens == []


def test_coletar_itens_respeita_o_limite_maximo_de_paginas():
    """Rede de segurança contra loop sem fim -- nunca deveria acontecer de
    verdade (a API sempre devolvendo página cheia), mas o coletor desiste
    num limite alto em vez de rodar pra sempre."""
    respostas = [_RespostaFake(dados={"data": [
        {"numeroItem": i, "descricao": "x"} for i in range(100)]})
        for _ in range(PNCPConnector._MAX_PAGINAS_ITENS)]
    c, sessao = _conector(respostas)
    itens = c._coletar_itens("123", 2024, 1)
    assert len(sessao.chamadas) == PNCPConnector._MAX_PAGINAS_ITENS
    assert len(itens) == 100 * PNCPConnector._MAX_PAGINAS_ITENS


# --------- _coletar_modalidade_uf: não confiar cego em totalPaginas --------- #

def test_coletar_modalidade_uf_continua_paginando_quando_totalpaginas_ausente_mas_pagina_cheia():
    c, sessao = _conector([])
    c.tam_pagina = 2
    cheia = {"data": [{"numeroControlePNCP": "a1", "objetoCompra": "x"},
                      {"numeroControlePNCP": "a2", "objetoCompra": "y"}]}   # sem totalPaginas
    incompleta = {"data": [{"numeroControlePNCP": "a3", "objetoCompra": "z"}]}
    sessao._respostas = [_RespostaFake(dados=cheia), _RespostaFake(dados=incompleta)]
    acc: dict = {}
    c._coletar_modalidade_uf(6, "SP", "20240101", acc)
    assert len(acc) == 3
    assert len(sessao.chamadas) == 2


def test_coletar_modalidade_uf_respeita_totalpaginas_quando_presente():
    c, sessao = _conector([])
    c.tam_pagina = 2
    cheia = {"data": [{"numeroControlePNCP": "a1", "objetoCompra": "x"},
                      {"numeroControlePNCP": "a2", "objetoCompra": "y"}], "totalPaginas": 1}
    sessao._respostas = [_RespostaFake(dados=cheia)]
    acc: dict = {}
    c._coletar_modalidade_uf(6, "SP", "20240101", acc)
    assert len(acc) == 2
    assert len(sessao.chamadas) == 1


def test_coletar_modalidade_uf_para_quando_registros_vazio_mesmo_sem_totalpaginas():
    c, sessao = _conector([])
    vazio = {"data": []}
    sessao._respostas = [_RespostaFake(dados=vazio)]
    acc: dict = {}
    c._coletar_modalidade_uf(6, "SP", "20240101", acc)
    assert acc == {}
    assert len(sessao.chamadas) == 1
