"""
Achado da auditoria do agente debugger em app/connectors/transparencia.py:
`.get(chave, {})` só usa o "{}" de fallback quando a CHAVE não existe -- se a
API mandar a chave presente com valor `null` (comum em JSON real), o .get
devolve None mesmo, e o próximo .get() na cadeia quebra com AttributeError.
O try/except do método evitava o crash da coleta inteira, mas descartava o
registro em silêncio, mesmo tendo um fallback bom (reg.get("orgao")) que
nunca chegava a rodar. Rode com:  cd backend && pytest
"""
from app.connectors.transparencia import TransparenciaConnector


def _conector():
    return TransparenciaConnector.__new__(TransparenciaConnector)


def test_mapear_com_orgaovinculado_null_usa_o_fallback_reg_orgao():
    c = _conector()
    reg = {"id": "1", "objeto": "Compra de teste",
          "unidadeGestora": {"orgaoVinculado": None},
          "orgao": {"nome": "Ministério de Teste"}}
    ed = c._mapear(reg)
    assert ed is not None
    assert ed.orgao == "Ministério de Teste"


def test_mapear_com_licitacao_null_nao_quebra():
    c = _conector()
    reg = {"id": "1", "objeto": "Compra de teste", "licitacao": None}
    ed = c._mapear(reg)
    assert ed is not None
    assert ed.id_externo == "transp-1"


def test_mapear_com_unidadegestora_ausente_usa_fallback():
    c = _conector()
    reg = {"id": "2", "objeto": "Outra compra", "nomeOrgao": "Órgão Direto"}
    ed = c._mapear(reg)
    assert ed is not None
    assert ed.orgao == "Órgão Direto"


def test_mapear_registro_normal_continua_funcionando():
    c = _conector()
    reg = {"id": "42", "objeto": "Compra normal",
          "unidadeGestora": {"orgaoVinculado": {"nome": "Ministério X"}}}
    ed = c._mapear(reg)
    assert ed is not None
    assert ed.orgao == "Ministério X"
    assert ed.id_externo == "transp-42"


def test_mapear_sem_identificador_nenhum_retorna_none():
    c = _conector()
    assert c._mapear({"objeto": "sem id nenhum"}) is None


def test_mapear_pega_numero_de_dentro_de_licitacao_quando_reg_nao_tem_id():
    c = _conector()
    reg = {"objeto": "Compra via licitacao aninhada", "licitacao": {"numero": "999"}}
    ed = c._mapear(reg)
    assert ed is not None
    assert ed.id_externo == "transp-999"
