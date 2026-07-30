"""
Análise de edital com IA (Gemini texto, free tier).

Baixa o PDF do edital publicado no PNCP, extrai o texto e pede ao Gemini um
resumo estruturado: objeto, documentos exigidos para habilitação, requisitos
técnicos do objeto, prazos, se exige amostra/visita, e pontos de atenção.

É OPCIONAL e tolerante a falhas:
- sem GEMINI_API_KEY -> status "sem_ia"
- PDF não disponível no PNCP -> status "sem_arquivo"
- PDF escaneado/sem texto extraível -> status "sem_texto"
- erro/timeout da IA -> status "erro_ia"

Nada disso quebra o resto do sistema. A análise é informativa; decisões de
habilitação continuam sendo do usuário (a IA pode errar/omitir).
"""
from __future__ import annotations
import io
import json
import logging

import requests

from .config import settings

log = logging.getLogger("ia.edital")

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Versão do prompt/análise. Ao melhorar o prompt, incremente este número:
# análises em cache com versão antiga serão refeitas automaticamente.
VERSAO_PROMPT = 6

_PROMPT = """Você é um especialista em licitações públicas brasileiras (Lei 14.133/2021 e LC 123/2006).
Analise o EDITAL abaixo e responda APENAS com um JSON válido (sem texto fora do JSON, sem ```), com exatamente esta estrutura:

- "objeto": string. Resumo claro do que está sendo contratado, em 1 a 2 frases.

- "documentos_habilitacao": objeto com estas 5 chaves, cada uma um array de strings. Liste CADA documento/certidão INDIVIDUALMENTE e por extenso, como aparece no edital — não resuma vários documentos numa frase só nem agrupe categorias diferentes no mesmo item. Cada exigência entra em UMA ÚNICA categoria — mesmo quando o edital diz que um cadastro (ex.: Sicaf) substitui documentação de VÁRIAS categorias ao mesmo tempo (jurídica, fiscal, econômico-financeira), liste-o UMA VEZ SÓ, na categoria mais natural pra ele; nunca repita a mesma exigência em duas categorias diferentes. Vazio (não a chave, a lista) se essa categoria não constar:
  - "juridica": habilitação jurídica (ex.: ato constitutivo/contrato social e alterações, procuração do representante legal, registro comercial).
  - "fiscal_trabalhista": regularidade fiscal e trabalhista (ex.: CND Receita Federal/PGFN, CRF do FGTS, CNDT, certidão negativa estadual, certidão negativa municipal, alvará de funcionamento).
  - "tecnica": qualificação técnica (ex.: atestado de capacidade técnica, registro em conselho de classe, comprovação de quantitativo mínimo já fornecido).
  - "economico_financeira": qualificação econômico-financeira (ex.: balanço patrimonial, certidão negativa de falência/recuperação judicial, capital social mínimo, índices contábeis exigidos).
  - "declaracoes": declarações exigidas (ex.: declaração de ME/EPP, de não emprego de menor, de idoneidade/inexistência de fato impeditivo, de elaboração independente de proposta).

- "requisitos_tecnicos": array de strings. Especificações TÉCNICAS que o produto/serviço contratado (o objeto em si) precisa atender: normas/certificações do produto, garantia mínima do produto, assistência técnica, nível de serviço (SLA), embalagem. Não repita aqui os documentos de habilitação da empresa. Vazio se não encontrar.

- "dados_orgao": objeto com (string vazia "" em qualquer chave que não constar):
  - "numero_processo": número do processo administrativo/edital.
  - "modo_disputa": "aberto", "fechado", "aberto e fechado" ou "".
  - "criterio_julgamento": ex.: "menor preço", "maior desconto", "técnica e preço".
  - "plataforma": sistema/portal onde ocorre a sessão/disputa (ex.: Compras.gov.br, BLL, Portal de Compras Públicas).
  - "data_sessao": data e horário da sessão pública de abertura/disputa, como aparece no edital.
  - "pregoeiro_responsavel": nome do pregoeiro/agente de contratação responsável.
  - "contato_orgao": telefone/e-mail de contato do órgão para dúvidas sobre o edital.
  - "exclusivo_regional": boolean. true se a participação for restrita a empresas de uma região/estado/município específico.
  - "regiao_exclusiva": string. Qual região/UF/município, se exclusivo_regional for true. "" caso contrário.

- "dados_proposta": objeto com (string vazia "" em qualquer chave que não constar):
  - "validade_dias": prazo de validade da proposta, como texto (ex.: "60 dias").
  - "prazo_entrega": prazo de entrega/execução do objeto, como aparece no edital.
  - "local_entrega": local de entrega ou execução, se especificado.
  - "condicoes_pagamento": forma e prazo de pagamento (ex.: "30 dias após atesto da nota fiscal").
  - "aceita_similar": boolean. true se o edital permite marca/modelo similar ou equivalente ao especificado.
  - "forma_apresentacao": como a proposta/documentos devem ser enviados (ex.: "anexar planilha de preços e catálogo do produto no sistema").
  - "garantia_proposta": se exige caução/garantia de manutenção da proposta, e o valor/percentual. "" se não exigir.
  - "identificacao_marca_modelo": boolean. true se a proposta precisa identificar marca/modelo/fabricante do produto ofertado.
  - "prospecto_catalogo": string. Se exige anexar prospecto/catálogo/folder técnico do produto junto com a proposta, e em que condição (ex.: "obrigatório", "se solicitado pelo pregoeiro"). "" se não exigir.
  - "entrega_tecnica": boolean. true se exigir instalação/entrega técnica especializada do produto (não é só deixar na doca).
  - "assistencia_tecnica": boolean. true se exigir assistência técnica pós-venda (rede autorizada, SLA de atendimento, etc.).
  - "garantia_produto": string. Prazo/tipo de garantia do PRODUTO em si (ex.: "garantia de fábrica, mínimo 12 meses") — diferente de garantia_contratual (caução) e garantia_proposta (caução da proposta). "" se não constar.

- "validade_documentos_habilitacao": string. Prazo máximo de emissão aceito para as certidões/documentos de habilitação, como aparece no edital (ex.: "documentos emitidos há no máximo 60 dias da data da sessão"). "" se não constar.

- "prazos": array de strings. Datas/prazos relevantes como aparecem (abertura, envio de propostas, sessão, entrega) — pode repetir o que já está em dados_orgao/dados_proposta, é só uma lista cronológica resumida.
- "exige_amostra": boolean. true se exigir amostra ou prova de conceito.
- "exige_visita": boolean. true se exigir visita técnica/vistoria.
- "exclusivo_me_epp": boolean. true se o edital (ou algum lote/item) for exclusivo ou tiver cota reservada para microempresa/EPP (LC 123/2006, art. 47/48).
- "julgamento": string. "lote" se a disputa/adjudicação é por lote fechado (não dá pra disputar 1 item isolado), "item" se é por item individual, "" se não identificar.
- "garantia_contratual": string. Percentual/forma de garantia CONTRATUAL exigida do vencedor após assinar o contrato (diferente da garantia de proposta e da garantia do produto). Vazio se não exigir.
- "pontos_atencao": array de strings (máx. 6). Cláusulas que merecem atenção: garantia exigida, prazo de entrega curto, exigências específicas, penalidades relevantes.

Regras: não invente nada que não esteja no texto. Se algo não constar, use lista vazia, string vazia ou false — nunca omita uma chave. Responda em português. Seja específico e completo em "documentos_habilitacao": o usuário vai separar cada certidão a partir dessa lista antes de enviar a proposta, então esquecer um documento é pior do que listar um a mais.

OBJETO (resumo do PNCP): {objeto}

TEXTO DO EDITAL (pode estar truncado):
\"\"\"{texto}\"\"\""""


def ia_texto_disponivel(api_key: str | None = None) -> bool:
    return bool(api_key)   # só a chave do próprio usuário (sem fallback global)


def _e_zip(conteudo: bytes) -> bool:
    return conteudo[:4] == b"PK\x03\x04"


def _texto_de_pdf_bytes(conteudo: bytes, max_paginas: int, max_chars: int) -> str:
    """Extrai texto de um PDF (bytes), com fallback de OCR se vier quase
    vazio (PDF escaneado)."""
    try:
        import pypdf
        leitor = pypdf.PdfReader(io.BytesIO(conteudo))
    except Exception:
        return ""
    partes, total = [], 0
    for i, pag in enumerate(leitor.pages):
        if i >= max_paginas:
            break
        try:
            t = pag.extract_text() or ""
        except Exception:
            t = ""
        partes.append(t)
        total += len(t)
        if total > max_chars:
            break
    texto = "\n".join(partes)[:max_chars]

    # PDF escaneado (pypdf extraiu pouco ou nada): tenta OCR como último
    # recurso. O limiar é alto de propósito — uma página com texto real de
    # edital tem bem mais que isso; um PDF com só a capa "de texto" e o
    # resto escaneado ficava abaixo do limiar final (300 chars combinados
    # em analisar()) sem nunca acionar o OCR.
    if len(texto.strip()) < 500 and settings.OCR_ATIVO:
        ocr = _ocr_pdf(conteudo)
        if ocr:
            return ocr[:max_chars]
    return texto


def _texto_de_zip(conteudo: bytes, max_paginas: int, max_chars: int) -> str:
    """O PNCP às vezes publica um único 'documento' como um .zip contendo
    vários PDFs (edital + anexos) em vez de um PDF direto. Sem isso, esses
    editais caíam sempre em "sem_texto" (pypdf/pdf2image não leem .zip)."""
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(conteudo))
    except Exception:
        return ""
    nomes_pdf = sorted(n for n in zf.namelist() if n.lower().endswith(".pdf"))
    partes = []
    total = 0
    for nome in nomes_pdf[:5]:
        if total >= max_chars:
            break
        try:
            dados = zf.read(nome)
        except Exception:
            continue
        t = _texto_de_pdf_bytes(dados, max_paginas, max_chars - total)
        if t:
            partes.append(t)
            total += len(t)
    return "\n\n---\n\n".join(partes)[:max_chars]


def _baixar_texto_pdf(url: str, timeout: int = 45,
                      max_paginas: int = 40, max_chars: int = 24000) -> str:
    try:
        r = requests.get(url, timeout=timeout,
                        headers={"User-Agent": "RadarLicitacoes/1.0"})
    except requests.RequestException:
        return ""
    if r.status_code != 200 or not r.content:
        return ""
    if _e_zip(r.content):
        return _texto_de_zip(r.content, max_paginas, max_chars)
    return _texto_de_pdf_bytes(r.content, max_paginas, max_chars)


def _ocr_pdf(conteudo: bytes) -> str:
    """OCR de PDF escaneado com Tesseract (grátis, local, sem GPU).
    Pesado: limita o nº de páginas para não sobrecarregar o servidor.
    Requer os binários do sistema 'tesseract-ocr' e 'poppler-utils'."""
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
    except Exception:
        log.warning("OCR indisponível (pytesseract/pdf2image não instalados).")
        return ""
    try:
        # converte só as primeiras páginas em imagem (DPI moderado p/ velocidade)
        imagens = convert_from_bytes(
            conteudo, dpi=settings.OCR_DPI, first_page=1,
            last_page=settings.OCR_MAX_PAGINAS)
    except Exception as e:
        log.warning("Falha ao rasterizar PDF para OCR: %s", e)
        return ""
    partes = []
    for img in imagens:
        try:
            partes.append(pytesseract.image_to_string(img, lang=settings.OCR_IDIOMA))
        except Exception as e:
            log.warning("Falha no OCR de uma página: %s", e)
            break
    texto = "\n".join(partes).strip()
    if texto:
        log.info("OCR extraiu %d caracteres de PDF escaneado.", len(texto))
    return texto


def _gerar(prompt: str, api_key: str | None = None, timeout: int = 70):
    chave = api_key   # só a chave do próprio usuário (sem fallback global)
    if not chave:
        return None, "sem_chave"
    url = f"{_BASE}/{settings.IA_MODELO_TEXTO}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    try:
        r = requests.post(url, json=body, timeout=timeout,
                         headers={"x-goog-api-key": chave,
                                  "Content-Type": "application/json"})
    except requests.RequestException as e:
        return None, f"rede:{e}"
    if r.status_code != 200:
        log.warning("Gemini texto HTTP %s: %s", r.status_code, r.text[:200])
        return None, f"http_{r.status_code}"
    try:
        dados = r.json()
        return dados["candidates"][0]["content"]["parts"][0]["text"], "ok"
    except (ValueError, KeyError, IndexError):
        return None, "sem_resposta"


def _parse_json(txt: str):
    try:
        return json.loads(txt)
    except Exception:
        t = txt.strip().strip("`")
        ini, fim = t.find("{"), t.rfind("}")
        if ini >= 0 and fim > ini:
            try:
                return json.loads(t[ini:fim + 1])
            except Exception:
                return None
    return None


def analisar(objeto: str, arquivos: list[dict], api_key: str | None = None) -> dict:
    """arquivos: lista de {titulo, tipo, url} (do endpoint de documentos).
    api_key: chave Gemini do próprio usuário (obrigatória, cai para a global)."""
    if not ia_texto_disponivel(api_key):
        return {"status": "sem_ia"}
    if not arquivos:
        return {"status": "sem_arquivo"}

    # prioriza o edital principal, depois termo de referência/anexos (onde
    # costumam estar as exigências de habilitação e a garantia contratual)
    def _prioridade(a):
        t = (a.get("titulo") or "").lower()
        if "edital" in t:
            return 0
        if "termo de referência" in t or "termo referencia" in t or "anexo" in t:
            return 1
        return 2
    candidatos = sorted(arquivos, key=_prioridade)

    # baixa e combina até 2 documentos (ex.: edital + termo de referência),
    # respeitando o limite total de caracteres do prompt
    MAX_TOTAL = 24000
    partes, fontes = [], []
    for a in candidatos[:5]:
        if len(fontes) >= 2 or sum(len(p) for p in partes) >= MAX_TOTAL:
            break
        if not a.get("url"):
            continue
        t = _baixar_texto_pdf(a["url"], max_chars=MAX_TOTAL)
        if len(t) > 300:
            partes.append(t)
            fontes.append(a.get("titulo") or "documento")
    texto = "\n\n---\n\n".join(partes)[:MAX_TOTAL]
    fonte = ", ".join(fontes) if fontes else None
    if len(texto) < 300:
        return {"status": "sem_texto"}  # PDF escaneado/imagem ou não extraível

    txt, st = _gerar(_PROMPT.format(objeto=(objeto or "")[:1000], texto=texto), api_key=api_key)
    if st != "ok" or not txt:
        return {"status": "erro_ia", "detalhe": st}
    data = _parse_json(txt)
    if not isinstance(data, dict):
        return {"status": "resposta_invalida"}

    # normaliza saída
    def lista(x):
        return [str(i) for i in x] if isinstance(x, list) else ([str(x)] if x else [])

    # nome curto pra não sombrear a variável `txt` (resposta crua do Gemini) do
    # escopo de fora — ela já foi consumida acima, mas mantém o código claro.
    def s(x):
        return str(x or "")

    def documentos_habilitacao(x):
        x = x if isinstance(x, dict) else {}
        return {
            "juridica": lista(x.get("juridica")),
            "fiscal_trabalhista": lista(x.get("fiscal_trabalhista")),
            "tecnica": lista(x.get("tecnica")),
            "economico_financeira": lista(x.get("economico_financeira")),
            "declaracoes": lista(x.get("declaracoes")),
        }

    def dados_orgao(x):
        x = x if isinstance(x, dict) else {}
        return {
            "numero_processo": s(x.get("numero_processo")),
            "modo_disputa": s(x.get("modo_disputa")),
            "criterio_julgamento": s(x.get("criterio_julgamento")),
            "plataforma": s(x.get("plataforma")),
            "data_sessao": s(x.get("data_sessao")),
            "pregoeiro_responsavel": s(x.get("pregoeiro_responsavel")),
            "contato_orgao": s(x.get("contato_orgao")),
            "exclusivo_regional": bool(x.get("exclusivo_regional")),
            "regiao_exclusiva": s(x.get("regiao_exclusiva")),
        }

    def dados_proposta(x):
        x = x if isinstance(x, dict) else {}
        return {
            "validade_dias": s(x.get("validade_dias")),
            "prazo_entrega": s(x.get("prazo_entrega")),
            "local_entrega": s(x.get("local_entrega")),
            "condicoes_pagamento": s(x.get("condicoes_pagamento")),
            "aceita_similar": bool(x.get("aceita_similar")),
            "forma_apresentacao": s(x.get("forma_apresentacao")),
            "garantia_proposta": s(x.get("garantia_proposta")),
            "identificacao_marca_modelo": bool(x.get("identificacao_marca_modelo")),
            "prospecto_catalogo": s(x.get("prospecto_catalogo")),
            "entrega_tecnica": bool(x.get("entrega_tecnica")),
            "assistencia_tecnica": bool(x.get("assistencia_tecnica")),
            "garantia_produto": s(x.get("garantia_produto")),
        }

    return {
        "status": "ok",
        "versao": VERSAO_PROMPT,
        "fonte": fonte,
        "objeto": s(data.get("objeto")),
        "documentos_habilitacao": documentos_habilitacao(data.get("documentos_habilitacao")),
        "validade_documentos_habilitacao": s(data.get("validade_documentos_habilitacao")),
        "requisitos_tecnicos": lista(data.get("requisitos_tecnicos")),
        "dados_orgao": dados_orgao(data.get("dados_orgao")),
        "dados_proposta": dados_proposta(data.get("dados_proposta")),
        "prazos": lista(data.get("prazos")),
        "exige_amostra": bool(data.get("exige_amostra")),
        "exige_visita": bool(data.get("exige_visita")),
        "exclusivo_me_epp": bool(data.get("exclusivo_me_epp")),
        "julgamento": s(data.get("julgamento")),
        "garantia_contratual": s(data.get("garantia_contratual")),
        "pontos_atencao": lista(data.get("pontos_atencao")),
    }
