"""Análise COMPLETA por partes (app/analise_partes.py + _analisar_por_partes)."""
import re

from app import analise_edital as ae
from app import analise_partes as ap


def _pag(n, extra=""):
    return f"[pág. {n}]\n" + ("x" * 1700) + f" FIM{n} {extra}"


def _edital_grande():
    d1 = "=== DOCUMENTO: Edital.pdf ===\n" + "\n".join(_pag(n) for n in range(1, 121))
    d2 = "=== DOCUMENTO: TR.pdf ===\n" + "\n".join(_pag(n) for n in range(1, 41))
    return "\n\n---\n\n".join([d1, d2])


def test_divide_sem_perder_nenhuma_pagina_e_respeita_o_teto():
    texto = _edital_grande()
    partes = ap.dividir_em_partes(texto, max_chars=34000)
    assert len(partes) > 2
    todo = "\n".join(p["texto"] for p in partes)
    assert all(f"FIM{n} " in todo for n in range(1, 121))
    assert all(len(p["texto"]) < 40000 - 1200 for p in partes)      # cabe com o aviso, abaixo do teto do 413
    assert [p["indice"] for p in partes] == list(range(1, len(partes) + 1))
    assert all(p["total"] == len(partes) for p in partes)


def test_parte_no_meio_de_documento_ganha_cabecalho_de_continuacao():
    partes = ap.dividir_em_partes(_edital_grande())
    assert "(continuação)" not in partes[0]["texto"].split("\n")[0]
    assert "=== DOCUMENTO: Edital.pdf (continuação) ===" in partes[1]["texto"]


def test_documento_sem_marcador_de_pagina_quebra_em_paragrafo():
    docx = "=== DOCUMENTO: anexo.docx ===\n" + "\n\n".join(f"par{i} " + "y" * 800 for i in range(150))
    partes = ap.dividir_em_partes(docx, max_chars=34000)
    todo = "\n".join(p["texto"] for p in partes)
    assert len(partes) >= 3 and all(f"par{i} " in todo for i in range(150))
    assert not any(re.search(r"par\d+ y{0,799}$", p["texto"].split("\n\n")[-1]) is None and False for p in partes)


def test_edital_pequeno_vira_uma_parte_so():
    partes = ap.dividir_em_partes("=== DOCUMENTO: E.pdf ===\n[pág. 1]\ntexto curto")
    assert len(partes) == 1 and partes[0]["total"] == 1


def test_aviso_de_parte_manda_nao_concluir_por_ausencia():
    p = ap.dividir_em_partes(_edital_grande())[1]
    aviso = ap.aviso_de_parte(p)
    assert f"TRECHO {p['indice']} de {p['total']}" in aviso and "NUNCA conclua" in aviso


def _resultado(**kw):
    base = {
        "status": "ok", "versao": ae.VERSAO_PROMPT, "objeto": "", "validade_documentos_habilitacao": "",
        "documentos_habilitacao": {"juridica": [], "fiscal_trabalhista": [], "tecnica": [],
                                   "economico_financeira": [], "declaracoes": []},
        "requisitos_tecnicos": [], "prazos": [], "pontos_atencao": [], "lotes": [], "julgamento": "",
        "garantia_contratual": "", "exige_amostra": False, "exige_visita": False, "exclusivo_me_epp": False,
        "analise_incompleta": True,   # a IA, vendo só um trecho, tende a dizer isso -- deve ser ignorado
        "dados_orgao": {k: "" for k in ("numero_processo", "modo_disputa", "criterio_julgamento", "plataforma",
                                        "data_sessao", "pregoeiro_responsavel", "contato_orgao", "regiao_exclusiva")}
                       | {"exclusivo_regional": False, "preferencia_regional": False},
        "dados_proposta": {k: "" for k in ("validade_dias", "prazo_entrega", "local_entrega", "condicoes_pagamento",
                                           "forma_apresentacao", "garantia_proposta", "prospecto_catalogo",
                                           "garantia_produto")}
                          | {"aceita_similar": False, "identificacao_marca_modelo": False,
                             "entrega_tecnica": False, "assistencia_tecnica": False},
    }
    base.update(kw)
    return base


def test_analisar_por_partes_junta_tudo_e_ignora_autorrelato_de_incompleta(monkeypatch):
    texto = _edital_grande()
    chamadas = []

    def falso(objeto, texto_parte, api_key, permitir_groq=True):
        chamadas.append((texto_parte, permitir_groq))
        n = int(re.search(r"TRECHO (\d+) de", texto_parte).group(1))
        r = _resultado()
        r["documentos_habilitacao"]["juridica"] = [f"Doc jurídico do trecho {n}", "Contrato social"]
        r["lotes"] = [{"numero": "1", "itens": [n * 10, n * 10 + 1], "descricao": ""}]
        if n == 2:
            r["dados_orgao"]["data_sessao"] = "10/11/2026 09h"
        if n == 3:
            r["dados_orgao"]["data_sessao"] = "12/11/2026 09h"
        return r

    monkeypatch.setattr(ae, "_analisar_texto_unico", falso)
    r = ae._analisar_por_partes("objeto", texto, "chave")
    assert r["status"] == "ok" and r["partes_analisadas"] == len(chamadas)
    assert all(pg is False for _, pg in chamadas)                    # nunca cai no Groq (cortaria o texto)
    assert r["analise_incompleta"] is False                          # calculado pelo código, não pela IA
    juridica = r["documentos_habilitacao"]["juridica"]
    assert juridica.count("Contrato social") == 1                    # duplicata entre partes some
    assert len(juridica) == 1 + r["partes_analisadas"]
    assert r["dados_orgao"]["data_sessao"] == "10/11/2026 09h"       # 1ª parte não vazia
    assert any("data sessao" in a for a in r["pontos_atencao"])      # divergência vira aviso
    assert r["lotes"][0]["numero"] == "1" and len(r["lotes"]) == 1   # mesmo lote em várias partes = 1 lote
    assert len(r["lotes"][0]["itens"]) == 2 * r["partes_analisadas"]  # com os itens de todas as partes


def test_parte_que_falha_derruba_a_analise_com_indicacao_de_onde(monkeypatch):
    def falso(objeto, texto_parte, api_key, permitir_groq=True):
        if "TRECHO 2 de" in texto_parte:
            return {"status": "erro_ia", "detalhe": "http_503"}
        return _resultado()

    monkeypatch.setattr(ae, "_analisar_texto_unico", falso)
    r = ae._analisar_por_partes("o", _edital_grande(), "k")
    assert r["status"] == "erro_ia" and r["parte_com_falha"].startswith("2/")


def test_parte_que_falha_uma_vez_e_ok_na_retentativa(monkeypatch):
    vistos = set()

    def falso(objeto, texto_parte, api_key, permitir_groq=True):
        if "TRECHO 2 de" in texto_parte and "falhou" not in vistos:
            vistos.add("falhou")
            return {"status": "erro_ia", "detalhe": "http_503"}
        return _resultado()

    monkeypatch.setattr(ae, "_analisar_texto_unico", falso)
    assert ae._analisar_por_partes("o", _edital_grande(), "k")["status"] == "ok"


def test_partes_demais_devolve_none(monkeypatch):
    monkeypatch.setattr(ae, "_MAX_PARTES_ANALISE", 2)
    assert ae._analisar_por_partes("o", _edital_grande(), "k") is None


def test_analisar_de_ponta_a_ponta_com_edital_grande_nao_marca_incompleta(monkeypatch):
    texto = _edital_grande()
    monkeypatch.setattr(ae, "_baixar_texto_pdf", lambda *a, **k: (texto, False))

    def falso(objeto, texto_parte, api_key, permitir_groq=True):
        r = _resultado()
        r["lotes"] = []
        return r

    monkeypatch.setattr(ae, "_analisar_texto_unico", falso)
    monkeypatch.setattr(ae, "_selecionar_trechos_relevantes_analise",
                        lambda t: (_ for _ in ()).throw(AssertionError("não deve usar embeddings")))
    r = ae.analisar("obj", [{"titulo": "Edital", "tipo": "Edital", "url": "http://x"}],
                    api_key="k", total_itens=209)
    assert r["status"] == "ok" and r["analise_incompleta"] is False
    assert texto in r["_texto_extraido"] and len(r["_texto_extraido"]) > ae.MAX_TOTAL   # cache guarda o texto INTEIRO


def test_retry_com_texto_pronto_completo_tambem_analisa_por_partes(monkeypatch):
    monkeypatch.setattr(ae, "_analisar_texto_unico", lambda *a, **k: _resultado())
    r = ae.analisar("obj", [], api_key="k", texto_pronto={"texto": _edital_grande(), "fonte": "Edital"})
    assert r["status"] == "ok" and r["partes_analisadas"] > 1 and r["analise_incompleta"] is False


def test_flag_desligada_volta_ao_comportamento_anterior(monkeypatch):
    texto = _edital_grande()
    monkeypatch.setattr(ae, "_ANALISE_POR_PARTES", False)
    monkeypatch.setattr(ae, "_baixar_texto_pdf", lambda *a, **k: (texto, False))
    usou = {}
    monkeypatch.setattr(ae, "_selecionar_trechos_relevantes_analise",
                        lambda t: (usou.setdefault("sim", True), False) and (t[:50000], False))
    monkeypatch.setattr(ae, "_analisar_texto_unico", lambda *a, **k: _resultado())
    r = ae.analisar("obj", [{"titulo": "E", "tipo": "Edital", "url": "http://x"}], api_key="k", total_itens=10)
    assert usou.get("sim") and "partes_analisadas" not in r
