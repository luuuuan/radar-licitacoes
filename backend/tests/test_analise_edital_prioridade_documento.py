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


def test_prioridade_arquivo_deprioriza_documentos_administrativos_por_tipo():
    """Achado real (edital 138442, Contratação Direta sem nenhum "Termo de
    Referência"/"Edital" entre os documentos): Mapa de Riscos, DFD e o
    despacho que autoriza a contratação são puramente processuais -- nunca
    trazem habilitação, mas empatavam (prioridade 3, genérica) com o Aviso
    de Contratação Direta, que costuma ser onde a habilitação de fato
    aparece nesse tipo de processo. Usa "tipo" (tipoDocumentoNome, vindo do
    próprio PNCP), não o nome do arquivo (que varia muito)."""
    assert _prioridade_arquivo({"titulo": "6. MR_180380.pdf", "tipo": "Mapa de Riscos"}) == 4
    assert _prioridade_arquivo({"titulo": "5. DFD180380.pdf",
                                "tipo": "Documento de Formalização da Demanda - DFD"}) == 4
    assert _prioridade_arquivo({"titulo": "1.1. SEI_Despacho.pdf",
                                "tipo": "Ato que autoriza a Contratação Direta"}) == 4
    # o Aviso continua na prioridade genérica (3) -- mais alta que os administrativos
    assert _prioridade_arquivo({"titulo": "2. AC180380.pdf", "tipo": "Aviso de Contratação Direta"}) == 3


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


def test_analisar_combina_mais_de_2_documentos_quando_sobra_orcamento():
    """Achado real (edital 138442, Contratação Direta com 6 documentos
    curtos empatados em prioridade -- 4 deles administrativos, deprioriza-
    dos por tipo, ver test_prioridade_arquivo_deprioriza_...): o teto
    antigo de "só 2 documentos" (independente de orçamento) parava o loop
    bem antes de estourar os 80000 chars disponíveis, deixando de fora
    documentos de verdade relevantes só porque vieram 3º/4º/5º na lista.
    Com 5 documentos pequenos (bem abaixo do orçamento somados), todos
    devem entrar -- só o 6º fica de fora por causa do teto candidatos[:5]."""
    arquivos = [
        {"titulo": f"Documento {i}", "url": f"http://x/doc{i}.pdf", "tipo": "Outros Documentos"}
        for i in range(1, 7)
    ]
    textos = {f"http://x/doc{i}.pdf": f"MARCADOR-{i} " + ("texto " * 50) for i in range(1, 7)}

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
    texto_enviado = _texto_enviado_a_ia(chamadas[0])
    for i in range(1, 6):   # os 5 primeiros (candidatos[:5]) devem estar presentes
        assert f"MARCADOR-{i}" in texto_enviado
    assert "MARCADOR-6" not in texto_enviado   # 6º fica de fora por causa do candidatos[:5]
