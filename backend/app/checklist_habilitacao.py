"""
Checklist de documentos de habilitação: cruza a lista de documentos exigidos
(extraída do edital pela análise por IA) com os documentos que o usuário já
tem cadastrados (aba Documentos), pra mostrar de cara o que falta e o que
está perto de vencer — sem o usuário ter que comparar as duas listas na mão.

O cruzamento é por TEXTO (fuzzy): o edital escreve "CND Receita Federal/PGFN"
e o usuário pode ter cadastrado "Certidão Negativa Federal", por exemplo —
não são strings iguais, mas são o mesmo documento. Isso nunca é 100%
confiável (é heurística), por isso o resultado sempre mostra o nome exigido
E o nome cadastrado, pra o usuário confirmar visualmente.
"""
from __future__ import annotations
from datetime import date

from rapidfuzz import fuzz

from .config import settings
from .matching.engine import normalizar

# abreviações comuns em documento de habilitação -> forma expandida. Aplicado
# nos dois lados antes do fuzzy, senão "CND" e "certidao negativa de debitos"
# nunca teriam sobreposição de caracteres suficiente pra bater.
_SINONIMOS = {
    "cnd": "certidao negativa de debitos",
    "crf": "certificado de regularidade certidao regularidade fgts",
    "cndt": "certidao negativa de debitos trabalhistas",
    "pgfn": "procuradoria geral da fazenda nacional",
    "sicaf": "sistema de cadastramento unificado de fornecedores",
    "me epp": "microempresa empresa de pequeno porte",
}
# mais longo primeiro: "cnd" é substring de "cndt" — se "cnd" fosse
# substituído antes, corromperia "cndt" e a regra certa nunca mais bateria.
_SINONIMOS_ORDENADOS = sorted(_SINONIMOS.items(), key=lambda kv: len(kv[0]), reverse=True)

_CATEGORIAS = {
    "juridica": "Habilitação jurídica",
    "fiscal_trabalhista": "Regularidade fiscal e trabalhista",
    "tecnica": "Qualificação técnica",
    "economico_financeira": "Qualificação econômico-financeira",
    "declaracoes": "Declarações",
}

# abaixo desse score (0..1), não considera match — melhor mostrar "não
# cadastrado" do que sugerir um documento errado com falsa confiança.
_LIMIAR_MATCH = 0.42


def _normalizar_doc(nome: str) -> str:
    n = normalizar(nome)
    for abrev, expandido in _SINONIMOS_ORDENADOS:
        n = n.replace(abrev, expandido)
    return n


def _status_validade(dias: int) -> str:
    if dias < 0:
        return "vencido"
    if dias <= settings.LEMBRETE_DOC_DIAS:
        return "vence_em_breve"
    return "valido"


def montar(documentos_habilitacao: dict, documentos_usuario: list[dict]) -> list[dict]:
    """documentos_usuario: lista de dicts com pelo menos id/nome/data_validade
    (mesmo formato de GET /api/documentos, só que sempre com data_validade
    como `date`, não string). Retorna uma lista achatada, pronta pro front:
    [{categoria, exigido, status, documento_id, nome_cadastrado,
      data_validade, dias_para_vencer, relevancia}, ...]
    """
    hoje = date.today()
    candidatos = [
        {**d, "_norm": _normalizar_doc(d["nome"])}
        for d in documentos_usuario if d.get("ativo", True)
    ]

    resultado = []
    for chave, rotulo in _CATEGORIAS.items():
        for exigido in (documentos_habilitacao or {}).get(chave) or []:
            alvo = _normalizar_doc(exigido)
            melhor, melhor_score = None, 0.0
            for c in candidatos:
                score = fuzz.token_set_ratio(alvo, c["_norm"]) / 100.0
                if score > melhor_score:
                    melhor, melhor_score = c, score
            if melhor and melhor_score >= _LIMIAR_MATCH:
                dias = (melhor["data_validade"] - hoje).days
                resultado.append({
                    "categoria": rotulo, "exigido": exigido,
                    "status": _status_validade(dias),
                    "documento_id": melhor["id"], "nome_cadastrado": melhor["nome"],
                    "data_validade": melhor["data_validade"].isoformat(),
                    "dias_para_vencer": dias, "relevancia": round(melhor_score, 2),
                })
            else:
                resultado.append({
                    "categoria": rotulo, "exigido": exigido, "status": "nao_cadastrado",
                    "documento_id": None, "nome_cadastrado": None,
                    "data_validade": None, "dias_para_vencer": None, "relevancia": 0.0,
                })
    return resultado
