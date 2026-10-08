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
    # achado real (edital 136161): o edital costuma escrever a exigência
    # como "Fazenda Nacional (Certidão conjunta RFB/PGFN)", mas a certidão
    # de verdade é emitida com um nome BEM diferente ("Certidão Negativa de
    # Débitos relativos aos Tributos Federais e à Dívida Ativa da União") --
    # sem essa ponte de vocabulário, as duas descrições do MESMO documento
    # não compartilham palavra nenhuma que importe, e o fuzzy ficava sem
    # sinal pra bater com o documento certo (ver _LIMIAR_MATCH).
    "fazenda nacional": "tributos federais divida ativa uniao receita federal fazenda nacional",
    "rfb": "receita federal do brasil",
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
#
# 0.42 era baixo demais: token_set_ratio dá score alto pra textos longos que
# só compartilham palavras de ligação comuns em português (de/que/da/para).
# Caso real observado: 4 "declarações" (não emprego de trabalho degradante,
# reserva de vagas PCD, ME/EPP, elaboração independente de proposta) E um
# "credenciamento no Sicaf" bateram todos com uma CERTIDÃO NEGATIVA DE
# DÉBITOS cadastrada sem ter NADA a ver — e como essa certidão estava
# vencida, isso vazava um "vencido há Xd" falso pros itens errados.
#
# 2º achado real (edital 136161): mesmo com 0.55, "Certificado de
# Regularidade do FGTS - CRF" batia com a exigência "Prova de regularidade
# fiscal perante a Fazenda Nacional (RFB/PGFN)" (score 0.71) -- e pior, uma
# "Certidão Negativa de Processo - TCU" (documento sem NADA a ver) batia com
# "CNDT" (0.75). Causa: token_set_ratio tem um viés — quando o candidato
# cadastrado é um nome CURTO e o exigido é uma frase LONGA, poucas palavras
# de ligação em comum (ex.: só "de"/"regularidade") já inflam o score,
# porque a comparação interna do algoritmo usa a INTERSEÇÃO como uma das
# pontas (curta = parecida com qualquer outra coisa curta). token_sort_ratio
# não tem esse viés (compara as strings inteiras, ordenadas), mas sozinho é
# rígido demais pra frases com ordem de palavras diferente. min(set, sort)
# —  só considera match quando os DOIS concordam — elimina os falsos
# positivos observados sem enfraquecer os pares que deveriam bater.
#
# Testado com pares reais que deveriam bater (CRF/FGTS, CNDT, Fazenda
# Nacional/RFB-PGFN, Fazenda Estadual, Fazenda Municipal, alvará, Sicaf,
# contrato social...) usando min(set, sort) — todos ficam >= 0.49 — contra
# pares que não deveriam (declaração vs. certidão não relacionada, CRF vs.
# Fazenda Nacional, TCU vs. CNDT, Sicaf vs. certidão federal) — todos
# <= 0.46. 0.48 fica no meio dessa lacuna. O preço continua sendo perder
# matches legítimos com frase muito vaga/curta — mas isso vira "não
# cadastrado" (seguro), nunca um match errado com falsa confiança (perigoso).
_LIMIAR_MATCH = 0.48


def _normalizar_doc(nome: str) -> str:
    n = normalizar(nome)
    for abrev, expandido in _SINONIMOS_ORDENADOS:
        n = n.replace(abrev, expandido)
    return n


# achado real (edital 136161, descoberto ao verificar a correção acima em
# produção): "Prova de regularidade junto à Fazenda ESTADUAL" batia com a
# certidão FEDERAL do usuário (0.60) em vez da certidão estadual de verdade
# que ele tinha cadastrada (0.55) -- as duas certidões compartilham tanto
# vocabulário genérico ("certidão negativa de débitos... tributos...") que a
# ÚNICA palavra que realmente distingue uma esfera de governo da outra
# ("estadual" vs. "federal"/"nacional") não pesa o suficiente sozinha pra
# virar o placar. Nunca existe ambiguidade real entre essas 3 esferas (são
# mutuamente exclusivas por definição) -- quando a exigência menciona uma e
# o candidato menciona outra DIFERENTE, é sinal forte de documento errado,
# mesmo com o resto do texto parecido.
_ESFERAS_GOVERNO = {
    "federal": {"federal", "federais", "nacional", "uniao"},
    "estadual": {"estadual", "estaduais"},
    "municipal": {"municipal", "municipais"},
}


def _esferas_mencionadas(texto_norm: str) -> set[str]:
    tokens = set(texto_norm.split())
    return {esfera for esfera, palavras in _ESFERAS_GOVERNO.items() if tokens & palavras}


def _contido(alvo_norm: str, cand_norm: str) -> bool:
    """Candidato cujo nome inteiro aparece literalmente dentro do texto da
    exigência (ex.: edital 153160 — exigido escreve "Certidão simplificada
    expedida pela Junta Comercial (para comprovação de enquadramento como
    ME/EPP, com validade de até 120 dias)" e o usuário tem cadastrado só
    "Certidão Simplificada") é o sinal mais forte de match que existe —
    mais forte que qualquer score de fuzzy, porque não é parecido, é
    IDÊNTICO ao trecho central do texto exigido.

    Esse caso escapava do min(set, sort) de _score por um motivo oposto ao
    que esse mínimo foi desenhado pra evitar: lá o problema era candidato
    CURTO inflando o score contra um alvo genérico; aqui o candidato é
    CURTO mas específico, e o alvo é o MESMO texto só que com uma
    explicação extra, e token_sort_ratio pune a diferença de tamanho como
    se fosse diferença de conteúdo. Exige candidato com conteúdo mínimo
    (não é só uma palavra genérica tipo "certidao") pra não disparar à
    toa em candidatos vagos.
    """
    if len(cand_norm) < 10 and len(cand_norm.split()) < 2:
        return False
    return cand_norm in alvo_norm


def _score(alvo_norm: str, cand_norm: str) -> float:
    """min(set, sort): ver _LIMIAR_MATCH -- token_set_ratio sozinho é
    enviesado a favor de candidatos com nome CURTO. Penaliza quando exigido
    e candidato citam esferas de governo diferentes (ver _ESFERAS_GOVERNO)."""
    if _contido(alvo_norm, cand_norm):
        return 0.9
    base = min(fuzz.token_set_ratio(alvo_norm, cand_norm),
              fuzz.token_sort_ratio(alvo_norm, cand_norm)) / 100.0
    esf_alvo, esf_cand = _esferas_mencionadas(alvo_norm), _esferas_mencionadas(cand_norm)
    if esf_alvo and esf_cand and not (esf_alvo & esf_cand):
        base *= 0.6
    return base


def _status_validade(dias: int) -> str:
    if dias < 0:
        return "vencido"
    if dias <= settings.LEMBRETE_DOC_DIAS:
        return "vence_em_breve"
    return "valido"


def _item_declaracao(d) -> dict:
    """Declaração NÃO é "certidão com validade" — não faz sentido cruzar
    contra os documentos cadastrados nem oferecer "+ cadastrar" (é um texto
    novo a cada edital, não um arquivo reutilizável). O que importa aqui é
    saber se o EDITAL já traz o modelo pronto (só preencher/assinar) ou se
    a empresa precisa redigir o próprio texto — vem da análise por IA."""
    nome = d.get("nome") if isinstance(d, dict) else d
    modelo_orgao = d.get("modelo_orgao") if isinstance(d, dict) else None
    detalhe = (d.get("detalhe") if isinstance(d, dict) else "") or ""
    if modelo_orgao is True:
        status = "modelo_orgao"
    elif modelo_orgao is False:
        status = "modelo_proprio"
    else:
        status = "indefinido"
    return {
        "categoria": _CATEGORIAS["declaracoes"], "exigido": nome, "status": status,
        "documento_id": None, "nome_cadastrado": None,
        "data_validade": None, "dias_para_vencer": None,
        "relevancia": 0.0, "detalhe": detalhe,
    }


def montar(documentos_habilitacao: dict, documentos_usuario: list[dict],
          verificacao_ia: list[dict] | None = None) -> list[dict]:
    """documentos_usuario: lista de dicts com pelo menos id/nome/data_validade
    (mesmo formato de GET /api/documentos, só que sempre com data_validade
    como `date`, não string). Retorna uma lista achatada, pronta pro front:
    [{categoria, exigido, status, documento_id, nome_cadastrado,
      data_validade, dias_para_vencer, relevancia, detalhe}, ...]

    "declaracoes" é tratada à parte (ver _item_declaracao) — as outras 4
    categorias continuam cruzadas por nome (fuzzy) contra documentos_usuario.

    verificacao_ia (opcional): itens de `verificacao_documentos_ia`
    (analise_edital.verificar_documentos_usuario) — verificação por
    CONTEÚDO de UM MESMO exigido, usando o texto extraído do arquivo
    anexado, não só o nome cadastrado. Achado real (edital 155842): o
    fuzzy por nome aqui (min(set,sort) de token ratio, ver _score) casa
    sistematicamente exigências com documentos errados que só
    compartilham vocabulário burocrático genérico ("certidão", "negativa",
    "débitos", "regularidade") — ex.: "Alvará Sanitário de Funcionamento"
    batendo com "Comprovante de inscrição... CNPJ" (score 0.53, nada a
    ver), ou "Regularidade perante a Fazenda estadual" batendo com
    "Certificado de Regularidade do FGTS" em vez da certidão estadual que
    o usuário tinha cadastrada. A verificação por IA lê o CONTEÚDO de
    verdade do arquivo (não só o nome), então quando ela existe pra um
    exigido (mesma string exata, os dois vêm da mesma lista
    documentos_habilitacao) ela tem a palavra final: documento
    corretamente identificado (ou "não atende"/"não aplicável" quando é o
    caso) substitui o palpite só-por-nome em vez de só complementar.
    Quando ela não roda pra este exigido (usuário sem documento com
    arquivo/texto extraído pra este edital) cai de volta no fuzzy de
    sempre, sem mudança de comportamento."""
    hoje = date.today()
    candidatos = [
        {**d, "_norm": _normalizar_doc(d["nome"])}
        for d in documentos_usuario if d.get("ativo", True)
    ]
    por_nome_exato = {c["nome"]: c for c in candidatos}
    verificacao_por_exigido = {
        v["exigido"]: v for v in (verificacao_ia or [])
        if isinstance(v, dict) and v.get("exigido")
    }

    resultado = []
    for chave, rotulo in _CATEGORIAS.items():
        if chave == "declaracoes":
            resultado.extend(_item_declaracao(d) for d in (documentos_habilitacao or {}).get(chave) or [])
            continue
        for exigido in (documentos_habilitacao or {}).get(chave) or []:
            v = verificacao_por_exigido.get(exigido)
            if v is not None and v.get("status") == "nao_aplicavel":
                resultado.append({
                    "categoria": rotulo, "exigido": exigido, "status": "nao_aplicavel",
                    "documento_id": None, "nome_cadastrado": None,
                    "data_validade": None, "dias_para_vencer": None, "relevancia": 0.0,
                    "detalhe": str(v.get("observacao") or ""),
                })
                continue
            if v is not None and v.get("status") == "nao_atendido":
                resultado.append({
                    "categoria": rotulo, "exigido": exigido, "status": "nao_atendido_ia",
                    "documento_id": None, "nome_cadastrado": str(v.get("documento") or "") or None,
                    "data_validade": None, "dias_para_vencer": None, "relevancia": 0.0,
                    "detalhe": str(v.get("observacao") or ""),
                })
                continue
            if v is not None and v.get("status") == "atendido" and v.get("documento"):
                # acha o Documento cadastrado que a IA apontou (mesmo nome
                # exato que foi mandado pra ela) pra herdar id/validade; sem
                # bater, ainda mostra o nome certo (sem link de edição) em
                # vez de voltar pro fuzzy, que é sabidamente o lado errado
                # aqui.
                achado = por_nome_exato.get(str(v["documento"]))
                if achado is not None:
                    sem_validade = achado["data_validade"] is None
                    dias = None if sem_validade else (achado["data_validade"] - hoje).days
                    resultado.append({
                        "categoria": rotulo, "exigido": exigido,
                        "status": "valido" if sem_validade else _status_validade(dias),
                        "documento_id": achado["id"], "nome_cadastrado": achado["nome"],
                        "data_validade": None if sem_validade else achado["data_validade"].isoformat(),
                        "dias_para_vencer": dias, "relevancia": 1.0,
                        "detalhe": str(v.get("observacao") or ""),
                    })
                else:
                    resultado.append({
                        "categoria": rotulo, "exigido": exigido, "status": "valido",
                        "documento_id": None, "nome_cadastrado": str(v["documento"]),
                        "data_validade": None, "dias_para_vencer": None, "relevancia": 1.0,
                        "detalhe": str(v.get("observacao") or ""),
                    })
                continue
            alvo = _normalizar_doc(exigido)
            melhor, melhor_score = None, 0.0
            for c in candidatos:
                score = _score(alvo, c["_norm"])
                if score > melhor_score:
                    melhor, melhor_score = c, score
            if melhor and melhor_score >= _LIMIAR_MATCH:
                # documento sem vencimento (ex.: contrato social) -- sempre
                # válido, não tem contagem de dias pra fazer.
                sem_validade = melhor["data_validade"] is None
                dias = None if sem_validade else (melhor["data_validade"] - hoje).days
                resultado.append({
                    "categoria": rotulo, "exigido": exigido,
                    "status": "valido" if sem_validade else _status_validade(dias),
                    "documento_id": melhor["id"], "nome_cadastrado": melhor["nome"],
                    "data_validade": None if sem_validade else melhor["data_validade"].isoformat(),
                    "dias_para_vencer": dias, "relevancia": round(melhor_score, 2), "detalhe": "",
                })
            else:
                resultado.append({
                    "categoria": rotulo, "exigido": exigido, "status": "nao_cadastrado",
                    "documento_id": None, "nome_cadastrado": None,
                    "data_validade": None, "dias_para_vencer": None, "relevancia": 0.0, "detalhe": "",
                })
    return resultado
