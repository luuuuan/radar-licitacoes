"""
Achado real: editais grandes têm o texto do PDF cortado em MAX_TOTAL chars
antes de chegar na IA (limite do prompt) -- se o corte cai no meio da seção
de habilitação, o usuário via uma lista de documentos incompleta sem
nenhum aviso. "analise_incompleta" deixa a IA sinalizar esse caso
explicitamente. Rode com:  cd backend && pytest
"""
import json
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
    with patch("app.analise_edital._baixar_texto_pdf", return_value=(texto_pdf, False)), \
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


# ---------------------------------------------------------------------------
# HTTP 413 do Gemini -- achado real (edital 125821, PNCP
# 88830609000139/2026/371): mesmo já sem a inflação de ensure_ascii (ver
# _chamar_modelo), um edital com catálogo de 90+ itens produzia texto grande
# o bastante pra estourar o teto de tamanho de requisição do próprio Gemini
# -- limite exato não documentado publicamente, então em vez de mirar um
# número exato de novo (arriscando reproduzir o mesmo erro), analisar()
# reage ao 413 truncando pro último valor que já rodou de verdade em
# produção sem erro (_MAX_TOTAL_SEGURO_413) e tenta mais uma vez --
# graceful degradation em vez de falhar a análise inteira.
# ---------------------------------------------------------------------------

def test_413_do_gemini_retenta_truncando_pro_teto_seguro(monkeypatch):
    from app.analise_edital import _MAX_TOTAL_SEGURO_413

    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    texto_grande = "x" * (_MAX_TOTAL_SEGURO_413 + 50000)
    monkeypatch.setattr("app.analise_edital._baixar_texto_pdf",
                        lambda *a, **k: (texto_grande, False))

    chamadas = []

    def _fake_post(url, data=None, **kw):
        corpo = json.loads(data.decode("utf-8"))
        texto_prompt = corpo["contents"][0]["parts"][0]["text"]
        chamadas.append(texto_prompt)
        if len(chamadas) == 1:
            r = MagicMock()
            r.status_code = 413
            r.text = "Request Entity Too Large"
            return r
        return _resposta_gemini('{"objeto": "teste"}')

    with patch("app.analise_edital.requests.post", side_effect=_fake_post):
        resultado = analisar("Objeto de teste", arquivos, api_key="fake-key")

    assert resultado["status"] == "ok"
    assert len(chamadas) == 2   # 1ª tentativa (413) + retentativa truncada
    # a 2ª tentativa manda MENOS texto que a 1ª -- truncou de verdade
    assert len(chamadas[1]) < len(chamadas[0])


# ---------------------------------------------------------------------------
# Edital SEM lotes (só por item) + texto truncado -- achado real (edital
# 145959, 209 itens, PDF de 212000 caracteres): a checagem de cobertura por
# lotes só roda quando a IA devolve "lotes"; um edital só-por-item nunca tem
# esse campo preenchido, então o corte em MAX_TOTAL passava batido e
# "analise_incompleta" ficava sempre false (autorrelato da IA, que não
# enxerga o próprio corte -- ver docstring de `total_itens` em analisar()).
# ---------------------------------------------------------------------------

def test_sem_lotes_e_texto_truncado_marca_incompleta(monkeypatch):
    from app.analise_edital import MAX_TOTAL

    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    # texto MAIOR que MAX_TOTAL -> analisar() vai cortar de verdade
    texto_grande = "x" * (MAX_TOTAL + 20000)
    monkeypatch.setattr("app.analise_edital._baixar_texto_pdf",
                        lambda *a, **k: (texto_grande, False))
    monkeypatch.setattr("app.analise_edital.requests.post",
                        lambda *a, **k: _resposta_gemini('{"objeto": "teste"}'))  # sem "lotes"

    resultado = analisar("Objeto de teste", arquivos, api_key="fake-key", total_itens=209)

    assert resultado["status"] == "ok"
    assert resultado["analise_incompleta"] is True
    assert any("cortado" in p for p in resultado["pontos_atencao"])


def test_sem_lotes_mas_texto_nao_truncado_nao_marca_incompleta(monkeypatch):
    """Edital pequeno, sem lotes, texto cabe inteiro em MAX_TOTAL -- não há
    corte nenhum, então não tem porquê marcar incompleta (distingue "não
    mede cobertura" de "cortou de verdade")."""
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    monkeypatch.setattr("app.analise_edital._baixar_texto_pdf",
                        lambda *a, **k: ("texto de edital " * 30, False))
    monkeypatch.setattr("app.analise_edital.requests.post",
                        lambda *a, **k: _resposta_gemini('{"objeto": "teste"}'))

    resultado = analisar("Objeto de teste", arquivos, api_key="fake-key", total_itens=5)

    assert resultado["status"] == "ok"
    assert resultado["analise_incompleta"] is False


def test_413_do_gemini_sem_sucesso_na_retentativa_ainda_reporta_erro(monkeypatch):
    """A retentativa não é garantia de sucesso (o edital pode continuar
    grande demais, ou o erro pode ser outro) -- se a 2ª tentativa também
    falhar, o erro final continua sendo reportado normalmente."""
    arquivos = [{"titulo": "Edital", "url": "http://x/edital.pdf"}]
    monkeypatch.setattr("app.analise_edital._baixar_texto_pdf",
                        lambda *a, **k: ("x" * 200000, False))

    r413 = MagicMock()
    r413.status_code = 413
    r413.text = "Request Entity Too Large"
    monkeypatch.setattr("app.analise_edital.requests.post", lambda *a, **k: r413)

    resultado = analisar("Objeto de teste", arquivos, api_key="fake-key")

    assert resultado["status"] == "erro_ia"
    assert resultado["detalhe"] == "http_413:Request Entity Too Large"
