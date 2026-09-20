"""
Achado real (edital 127082, reportado pelo usuário): quando o PNCP tem uma
retificação alterando data/condições da sessão, o edital original sozinho
já enche o limite de caracteres do prompt (MAX_TOTAL) -- a IA lia só o
texto desatualizado e devolvia a "data da sessão" errada. _prioridade_arquivo
agora prioriza retificação/errata/aditamento antes do edital original, pra
garantir que o texto mais atualizado sempre entre no que é mandado pra IA.
Rode com:  cd backend && pytest
"""
import json
from unittest.mock import patch, MagicMock

from app.analise_edital import analisar, _prioridade_arquivo


def _texto_enviado_a_ia(chamada_post_kwargs) -> str:
    # _chamar_modelo manda o corpo via data= (bytes), não json= -- ver
    # ensure_ascii=False em _post_com_retry.
    corpo = json.loads(chamada_post_kwargs["data"].decode("utf-8"))
    return corpo["contents"][0]["parts"][0]["text"]


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


def test_prioridade_arquivo_reconhece_termo_de_referencia_sem_acento(monkeypatch):
    """Achado real (edital 141844, PNCP 46384111000140/2026/1036): título
    veio exatamente "TERMO DE REFERENCIA COM APROVACAO - SEI.pdf" (sem
    acento no "ê") -- a comparação direta com "termo de referência" nunca
    batia, então esse documento (onde ficam item/habilitação) caía na
    mesma prioridade genérica (3) que um aviso administrativo qualquer, e
    podia perder a vaga pra ele."""
    assert _prioridade_arquivo({"titulo": "TERMO DE REFERENCIA COM APROVACAO - SEI.pdf"}) == 2
    assert _prioridade_arquivo({"titulo": "Retificacao do Edital"}) == 0   # sem cedilha também


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
        return textos[url][:max_chars], False

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


def test_analisar_nao_deixa_1o_documento_gigante_engolir_o_2o():
    """Achado real (edital 141844): um documento sozinho (aviso
    administrativo) batia o teto inteiro de MAX_TOTAL, e o loop parava aí --
    o 2º candidato (Termo de Referência, bem menor, onde fica a
    habilitação) nunca chegava a ser baixado nem entrava no texto mandado
    pra IA, mesmo sobrando espaço de sobra pra ele. Os dois empatam em
    prioridade (nenhum bate palavra-chave de retificação/edital/termo de
    referência), então a ordem original é preservada -- o gigante processa
    primeiro."""
    arquivos = [
        {"titulo": "Aviso de Contratação Direta", "url": "http://x/aviso.pdf"},
        {"titulo": "Especificações Técnicas", "url": "http://x/especificacoes.pdf"},
    ]
    textos = {
        "http://x/aviso.pdf": "A" * 90000,   # sozinho já passa de MAX_TOTAL (80000)
        "http://x/especificacoes.pdf": "MARCADOR-ESPECIFICACOES habilitação exigida. " * 20,
    }

    def _fake_baixar(url, max_chars=80000, **kw):
        return textos[url][:max_chars], False

    chamadas = []

    def _fake_post(url, **kw):
        chamadas.append(kw)
        return _resposta_gemini('{"objeto": "teste"}')

    with patch("app.analise_edital._baixar_texto_pdf", side_effect=_fake_baixar), \
         patch("app.analise_edital.requests.post", side_effect=_fake_post):
        resultado = analisar("Objeto de teste", arquivos, api_key="fake-key")

    assert resultado["status"] == "ok"
    assert len(chamadas) == 1
    assert "MARCADOR-ESPECIFICACOES" in _texto_enviado_a_ia(chamadas[0])


def test_analisar_documento_unico_gigante_continua_usando_o_orcamento_inteiro():
    """Contraste com o teste acima: quando NÃO há um 2º candidato esperando
    a vez, o documento único continua aproveitando o orçamento inteiro --
    a reserva de metade só se aplica quando reservar faz sentido."""
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    texto_grande = "MARCADOR-INICIO " + "A" * 90000

    def _fake_baixar(url, max_chars=80000, **kw):
        return texto_grande[:max_chars], False

    chamadas = []

    def _fake_post(url, **kw):
        chamadas.append(kw)
        return _resposta_gemini('{"objeto": "teste"}')

    with patch("app.analise_edital._baixar_texto_pdf", side_effect=_fake_baixar), \
         patch("app.analise_edital.requests.post", side_effect=_fake_post):
        resultado = analisar("Objeto de teste", arquivos, api_key="fake-key")

    assert resultado["status"] == "ok"
    texto_enviado = _texto_enviado_a_ia(chamadas[0])
    # não foi cortado pela metade (40000) -- usou perto do teto cheio (80000)
    assert texto_enviado.count("A") > 70000
