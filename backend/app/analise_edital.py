"""
Análise de edital com IA (Gemini texto, free tier).

Baixa o PDF do edital publicado no PNCP, extrai o texto e pede ao Gemini um
resumo estruturado: objeto, documentos exigidos para habilitação, requisitos
técnicos do objeto, prazos, se exige amostra/visita, e pontos de atenção.

É OPCIONAL e tolerante a falhas:
- sem chave Gemini do usuário -> status "sem_ia"
- PDF não disponível no PNCP -> status "sem_arquivo"
- PDF escaneado/sem texto extraível -> status "sem_texto"
- erro/timeout da IA -> status "erro_ia"

Nada disso quebra o resto do sistema. A análise é informativa; decisões de
habilitação continuam sendo do usuário (a IA pode errar/omitir).
"""
from __future__ import annotations
import base64
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from datetime import date

import requests

from .config import settings

log = logging.getLogger("ia.edital")

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Versão do prompt/análise. Ao melhorar o prompt, incremente este número:
# análises em cache com versão antiga serão refeitas automaticamente.
# v13 = achado real (edital 141995): prompt de "lotes" reforçado contra
# numeração reiniciada por lote + validação descarta a lista inteira
# quando um item aparece em mais de um lote (ver função lotes() abaixo).
# v14 = achado real (edital 143879): "analise_incompleta" agora também é
# calculado comparando a cobertura real dos lotes contra o Nº de itens
# verdadeiro do edital (total_itens, dado do PNCP), não só o autorrelato
# da IA -- sem o bump, quem já tinha uma análise em cache com essa mesma
# falha (texto truncado sem a IA perceber) continuaria vendo
# "analise_incompleta: false" pra sempre, até clicar "Realizar nova
# análise" manualmente.
# v15 = achado real (edital 145959, 209 itens, PDF de 212000 caracteres, SEM
# agrupamento em lotes): a checagem v14 só roda quando existem "lotes" --
# um edital só-por-item (sem lotes) passava pela checagem sem cobertura
# nenhuma sendo medida, mesmo com o texto claramente cortado em MAX_TOTAL
# antes de chegar à IA (confirmado baixando o PDF: o corte caía no meio da
# tabela, entre o item 194 e o 209 real). Novo ramo: sem lotes pra medir
# cobertura item a item, usa "o texto foi truncado" como sinal (mesmo
# raciocínio de _MAX_TOTAL_SEGURO_413) -- sem ele, nunca dava pra provar
# cobertura completa em edital sem lote, e "analise_incompleta" ficava
# sempre false por autorrelato mesmo com texto cortado.
VERSAO_PROMPT = 15

# Versão da LÓGICA de verificar_documentos_usuario() (não do prompt em si,
# embora um ajuste no prompt também conte). Achado real (agente
# error-detective): a cache dessa verificação (AnaliseIAExtras) era
# versionada só por Usuario.versao_documentos -- muda quando o usuário edita
# um Documento, nunca quando ESTA FUNÇÃO muda de comportamento (ex.: parar
# de cobrar declaração, ver _formatar_requisitos). Sem isso, quem já tinha
# rodado a verificação antes de uma correção continuava vendo o resultado
# ANTIGO pra sempre, até editar um documento ou clicar "Realizar nova
# análise" -- ao contrário de Edital.analise_ia, que já era corretamente
# versionado por VERSAO_PROMPT. Incremente ao mudar _formatar_requisitos,
# _PROMPT_VERIFICACAO_DOCUMENTOS ou verificar_documentos_usuario (ex.: v2 =
# corrige corte de documentos_usuario[:8] pra orçamento de caracteres,
# achado real do edital 126768 -- sem incrementar aqui, quem já tinha
# rodado a verificação incompleta continuaria vendo o resultado velho
# mesmo depois do código corrigido).
# v3 = pedido do usuário (edital 136161: "habilitação jurídica" mostrava 18
# itens no checklist por nome, mas só 11 apareciam aqui): itens com
# aplicavel=false não somem mais da lista, viram status="nao_aplicavel";
# declarações (antes de fora por completo, ver _formatar_requisitos) agora
# entram como itens informativos status="declaracao" -- a lista passa a
# mostrar os MESMOS itens do checklist por nome, só que cada um explicando
# por que não vira ✓/✗ quando for o caso, em vez de esconder.
VERSAO_VERIFICACAO_DOCUMENTOS = 3

_PROMPT = """Você é um especialista em licitações públicas brasileiras (Lei 14.133/2021 e LC 123/2006).
Analise o EDITAL abaixo e responda APENAS com um JSON válido (sem texto fora do JSON, sem ```), com exatamente esta estrutura:

- "objeto": string. Resumo claro do que está sendo contratado, em 1 a 2 frases.

- "documentos_habilitacao": objeto com estas 5 chaves, cada uma um array. Liste CADA documento/certidão INDIVIDUALMENTE e por extenso, como aparece no edital — não resuma vários documentos numa frase só nem agrupe categorias diferentes no mesmo item. Cada exigência entra em UMA ÚNICA categoria — mesmo quando o edital diz que um cadastro (ex.: Sicaf) substitui documentação de VÁRIAS categorias ao mesmo tempo (jurídica, fiscal, econômico-financeira), liste-o UMA VEZ SÓ, na categoria mais natural pra ele; nunca repita a mesma exigência em duas categorias diferentes. Cada chave recebe uma lista vazia [] (nunca false, nunca a chave ausente) se essa categoria não constar:
  - "juridica": array de strings. Habilitação jurídica (ex.: ato constitutivo/contrato social e alterações, procuração do representante legal, registro comercial).
  - "fiscal_trabalhista": array de strings. Regularidade fiscal e trabalhista (ex.: CND Receita Federal/PGFN, CRF do FGTS, CNDT, certidão negativa estadual, certidão negativa municipal, alvará de funcionamento).
  - "tecnica": array de strings. Qualificação técnica (ex.: atestado de capacidade técnica, registro em conselho de classe, comprovação de quantitativo mínimo já fornecido).
  - "economico_financeira": array de strings. Qualificação econômico-financeira (ex.: balanço patrimonial, certidão negativa de falência/recuperação judicial, capital social mínimo, índices contábeis exigidos).
  - "declaracoes": array de OBJETOS (não strings), um para CADA declaração exigida (ex.: declaração de ME/EPP, de não emprego de menor, de idoneidade/inexistência de fato impeditivo, de elaboração independente de proposta). Se não houver nenhuma declaração, use lista vazia []. Cada objeto:
    - "nome": string. A declaração, como aparece no edital.
    - "modelo_orgao": boolean ou null. true se o EDITAL/ÓRGÃO fornece um modelo/anexo PRONTO pra essa declaração (a empresa só preenche e assina — geralmente citado como "conforme Anexo X", "modelo constante do Anexo"). false SOMENTE se o edital afirmar explicitamente que a declaração segue texto livre/próprio da empresa. Os anexos com os modelos costumam vir no final do edital ou em arquivo separado, fora do texto disponível — se o texto não menciona anexo pra essa declaração (em vez de negar explicitamente que exista um), use null, não false: ausência de menção não é o mesmo que confirmação de que não há modelo.
    - "detalhe": string curta. Sempre que conseguir identificar com segurança, informe EM QUAL DOCUMENTO (pelo nome no cabeçalho "=== DOCUMENTO: ... ===") e EM QUAL PÁGINA (pelo marcador "[pág. N]" mais próximo) essa declaração/exigência aparece — o texto às vezes vem de mais de um arquivo (edital + termo de referência/anexo em arquivo separado), e sem isso o usuário não sabe onde procurar. Ex.: "modelo pronto no Anexo IV — documento 'Edital.pdf', pág. 12", "exigida no item 8.2, documento 'Termo de Referência.pdf', pág. 3 — sem modelo, redigir texto próprio". Se não der pra identificar documento/página com segurança (ex.: texto vindo de OCR, sem marcador de página por perto), descreva só o que for certo, sem inventar número de página ou nome de documento. "" apenas se não houver nada relevante a acrescentar.

- "requisitos_tecnicos": array de strings. Especificações TÉCNICAS do produto/serviço contratado (o objeto em si) que NÃO tenham campo próprio neste JSON: normas/certificações do produto, embalagem, nível de serviço (SLA), condições de conservação. NÃO coloque aqui garantia do produto, assistência técnica nem entrega/instalação técnica — essas têm campos dedicados em "dados_proposta" (garantia_produto, assistencia_tecnica, entrega_tecnica) e devem ir SÓ lá. Não repita aqui os documentos de habilitação da empresa. Vazio se não encontrar.

- "dados_orgao": objeto com (string vazia "" em qualquer chave que não constar):
  - "numero_processo": número do PROCESSO administrativo (ex.: "Processo nº 1234/2025"). É comum o edital ter um número de processo e um número de pregão/edital DIFERENTES — se ambos aparecerem, inclua os dois de forma clara (ex.: "Processo 1234/2025 — Pregão Eletrônico 07/2025"), nunca só um dos dois torcendo pra ser o certo.
  - "modo_disputa": "aberto", "fechado", "aberto e fechado" ou "".
  - "criterio_julgamento": o CRITÉRIO de escolha do vencedor. Ex.: "menor preço", "maior desconto", "técnica e preço". (Diferente de "julgamento", que é a unidade de disputa — ver abaixo.)
  - "plataforma": sistema/portal onde ocorre a sessão/disputa (ex.: Compras.gov.br, BLL, Portal de Compras Públicas).
  - "data_sessao": APENAS a data/hora rotulada explicitamente como sessão pública/abertura/disputa. NÃO use outra data do documento (assinatura do edital, publicação, prazo de validade de certidão) só porque é a mais parecida com uma data de sessão. "" se não houver data de sessão explícita.
  - "pregoeiro_responsavel": APENAS o nome identificado explicitamente como pregoeiro/agente de contratação/leiloeiro responsável pela sessão. NÃO use o nome de quem assina o edital (ordenador de despesa, secretário) nem de outra autoridade — são papéis diferentes. "" se não constar.
  - "contato_orgao": telefone/e-mail de contato do órgão para dúvidas sobre o edital.
  - "exclusivo_regional": boolean. true se a participação for restrita a empresas de uma região/estado/município específico.
  - "regiao_exclusiva": string. Qual região/UF/município, se exclusivo_regional for true. "" caso contrário.

- "dados_proposta": objeto com (string vazia "" em qualquer chave que não constar):
  - "validade_dias": prazo de validade da proposta, como texto (ex.: "60 dias").
  - "prazo_entrega": prazo de entrega/execução do objeto, como aparece no edital.
  - "local_entrega": ENDEREÇO COMPLETO de entrega/execução (rua, número, bairro, cidade/UF), quando o edital informar um endereço literal — não só o nome do órgão. Se não houver endereço completo, use o local/unidade como aparecer (ex.: "almoxarifado central da Secretaria"). Se o edital for Registro de Preços e disser que o local será definido depois (ex.: "conforme ordem de fornecimento"), registre isso literalmente. "" se não constar.
  - "condicoes_pagamento": forma e prazo de pagamento (ex.: "30 dias após atesto da nota fiscal").
  - "aceita_similar": boolean. true se o edital permite marca/modelo similar ou equivalente ao especificado.
  - "forma_apresentacao": como a proposta/documentos devem ser enviados (ex.: "anexar planilha de preços no sistema"). NÃO inclua aqui exigência de catálogo/prospecto nem de identificar marca/modelo — isso vai SÓ em "prospecto_catalogo"/"identificacao_marca_modelo", não repita aqui.
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
- "julgamento": string. A UNIDADE de adjudicação (não confundir com criterio_julgamento, que é o critério de preço): "lote" se a disputa/adjudicação é por lote, grupo ou item agrupado/global (não dá pra disputar 1 item isolado), "item" se é por item individual, "" se não identificar.
- "lotes": array de objetos. APENAS quando "julgamento" for "lote" — a composição de cada lote/grupo, pra saber quais itens precisam ser todos fornecidos juntos. Lista vazia [] se "julgamento" não for "lote", ou se não conseguir identificar a composição dos lotes com segurança (não invente agrupamento nenhum). Cada objeto:
  - "numero": string. O identificador do lote como aparece no edital (ex.: "1", "Lote 02", "Grupo A").
  - "itens": array de inteiros. Os números dos itens (mesma numeração GLOBAL usada no restante do edital, ex.: em "requisitos_tecnicos"/na tabela de itens) que pertencem a este lote -- NUNCA reinicie a contagem em cada lote (é ERRADO numerar como "1, 2, 3" o 1º/2º/3º item de um lote se esses itens são, por exemplo, os itens 14, 15 e 16 na numeração do edital). Cada item pertence a UM SÓ lote -- o mesmo número nunca pode aparecer em dois lotes diferentes.
  - "descricao": string curta resumindo o conteúdo do lote (ex.: "Material de escritório — papelaria"). "" se não conseguir resumir.
- "garantia_contratual": string. Percentual/forma de garantia CONTRATUAL exigida do vencedor após assinar o contrato (diferente da garantia de proposta e da garantia do produto). Vazio se não exigir.
- "analise_incompleta": boolean. true se o texto do edital termina no meio de uma seção relevante (sobretudo a de habilitação) ou não contém seção de habilitação alguma — sinal de que pode ter sido truncado e a análise talvez não capture todos os documentos. false se o texto parece completo.
- "pontos_atencao": array de strings (máx. 6). Riscos ou exigências INCOMUNS que NÃO tenham campo próprio neste JSON (ex.: multa/penalidade severa, prazo de entrega atipicamente curto, exigência técnica atípica, cláusula restritiva de concorrência). NÃO repita aqui informação que já esteja em outro campo estruturado (garantia_contratual, garantia_produto, validade_dias, exige_amostra, exige_visita etc.) — a tela já mostra esses campos separadamente, duplicar não ajuda. Única exceção: se "analise_incompleta" for true, inclua aqui um aviso de que a análise pode estar incompleta por truncamento do texto.

O texto abaixo pode conter mais de um documento, cada um começando com um cabeçalho "=== DOCUMENTO: <nome do arquivo> ===" e separado do seguinte por "---" (por exemplo, o edital principal e um termo de referência/anexo publicados como arquivos separados, ou uma RETIFICAÇÃO/ERRATA seguida do edital original). Dentro de cada documento, use os marcadores "[pág. N]" que aparecem antes de trechos do texto pra saber em que página do ARQUIVO ATUAL (não do PDF combinado) uma informação está — são reiniciados a cada novo documento. Quando houver informação conflitante entre eles (datas, prazos, exigências), o valor da RETIFICAÇÃO/ERRATA prevalece sobre o do edital original — a retificação é sempre mais recente, mesmo quando aparece antes no texto.

O texto foi extraído automaticamente de PDF e pode conter artefatos: cabeçalhos/rodapés repetidos em cada página, trechos de colunas fora de ordem, palavras quebradas por hífen no fim de linha. Ignore esses artefatos e reconstitua o sentido do conteúdo.

Regras gerais: não invente nada que não esteja no texto. Responda em português. Seja específico e completo em "documentos_habilitacao": o usuário vai separar cada certidão a partir dessa lista antes de enviar a proposta, então esquecer um documento é pior do que listar um a mais.

OBJETO (resumo do PNCP): {objeto}

TEXTO DO EDITAL (pode estar truncado):
\"\"\"{texto}\"\"\""""

_S = {"type": "STRING"}
_L_S = {"type": "ARRAY", "items": _S}
_B = {"type": "BOOLEAN"}

# Schema (formato Gemini, subset de OpenAPI) espelhando _PROMPT -- força tipo,
# obrigatoriedade de chave e os enums no decoder do próprio Gemini, em vez de
# só pedir por prosa ("nunca troque lista por false"). Reduz a resposta
# trocar tipo (isso já existiu: ver normalização em analisar()) a zero.
_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "objeto": _S,
        "documentos_habilitacao": {
            "type": "OBJECT",
            "properties": {
                "juridica": _L_S, "fiscal_trabalhista": _L_S,
                "tecnica": _L_S, "economico_financeira": _L_S,
                "declaracoes": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "nome": _S,
                            "modelo_orgao": {"type": "BOOLEAN", "nullable": True},
                            "detalhe": _S,
                        },
                        "required": ["nome", "modelo_orgao", "detalhe"],
                    },
                },
            },
            "required": ["juridica", "fiscal_trabalhista", "tecnica",
                        "economico_financeira", "declaracoes"],
        },
        "requisitos_tecnicos": _L_S,
        "dados_orgao": {
            "type": "OBJECT",
            "properties": {
                "numero_processo": _S,
                # sem "enum" aqui de propósito: a API do Gemini rejeita
                # enum com valor vazio ("cannot be empty") -- e "" é o
                # sentinela pra "não identificado" que o prompt já pede.
                # Fica STRING livre, a lista de valores esperados continua
                # só na prosa do _PROMPT.
                "modo_disputa": _S,
                "criterio_julgamento": _S, "plataforma": _S,
                "data_sessao": _S, "pregoeiro_responsavel": _S, "contato_orgao": _S,
                "exclusivo_regional": _B, "regiao_exclusiva": _S,
            },
            "required": ["numero_processo", "modo_disputa", "criterio_julgamento",
                        "plataforma", "data_sessao", "pregoeiro_responsavel",
                        "contato_orgao", "exclusivo_regional", "regiao_exclusiva"],
        },
        "dados_proposta": {
            "type": "OBJECT",
            "properties": {
                "validade_dias": _S, "prazo_entrega": _S, "local_entrega": _S,
                "condicoes_pagamento": _S, "aceita_similar": _B,
                "forma_apresentacao": _S, "garantia_proposta": _S,
                "identificacao_marca_modelo": _B, "prospecto_catalogo": _S,
                "entrega_tecnica": _B, "assistencia_tecnica": _B, "garantia_produto": _S,
            },
            "required": ["validade_dias", "prazo_entrega", "local_entrega",
                        "condicoes_pagamento", "aceita_similar", "forma_apresentacao",
                        "garantia_proposta", "identificacao_marca_modelo",
                        "prospecto_catalogo", "entrega_tecnica", "assistencia_tecnica",
                        "garantia_produto"],
        },
        "validade_documentos_habilitacao": _S,
        "prazos": _L_S,
        "exige_amostra": _B, "exige_visita": _B, "exclusivo_me_epp": _B,
        # mesmo motivo do modo_disputa acima: sem "enum" (Gemini rejeita
        # valor vazio no enum, e "" é o sentinela de "não identificado").
        "julgamento": _S,
        "lotes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "numero": _S,
                    "itens": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "descricao": _S,
                },
                "required": ["numero", "itens", "descricao"],
            },
        },
        "garantia_contratual": _S,
        "analise_incompleta": _B,
        "pontos_atencao": _L_S,
    },
    "required": ["objeto", "documentos_habilitacao", "requisitos_tecnicos", "dados_orgao",
                "dados_proposta", "validade_documentos_habilitacao", "prazos",
                "exige_amostra", "exige_visita", "exclusivo_me_epp", "julgamento", "lotes",
                "garantia_contratual", "analise_incompleta", "pontos_atencao"],
}


def ia_texto_disponivel(api_key: str | None = None) -> bool:
    return bool(api_key)   # só a chave do próprio usuário (sem fallback global)


def _e_zip(conteudo: bytes) -> bool:
    return conteudo[:4] == b"PK\x03\x04"


# Assinatura do OLE2/Compound File Binary Format — formato dos arquivos
# antigos do Office (.doc, .xls, .ppt de 97-2003). Achado real: o PNCP
# permite o órgão publicar o edital nesse formato em vez de PDF, e o
# sistema não tinha NENHUM suporte a isso — dava "sem_texto" (mensagem de
# "parece imagem escaneada"), quando o problema de verdade era um formato
# de arquivo que nunca foi lido, nem tentado.
_MAGIC_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# RTF é texto puro (não binário) começando com "{\rtf" — achado real
# (edital PNCP 24772188000154/2026/130): o órgão publicou o "Edital" nesse
# formato, com Content-Type application/octet-stream (não avisa o formato
# real) e nome de arquivo terminando em .rtf. pypdf tentava ler como PDF e
# falhava ("invalid pdf header") — sem suporte nenhum, cada NCM zerado
# nesse arquivo. LibreOffice já é usado pra .doc/.docx (_texto_de_word_bytes,
# abaixo) e lê RTF nativamente, sem precisar de conversor novo nenhum.
_MAGIC_RTF = b"{\\rtf"


def _e_docx(conteudo: bytes) -> bool:
    """.docx é um .zip por dentro (padrão OOXML) — diferencia do caso já
    tratado em _texto_de_zip (um .zip "burro" com PDFs soltos dentro)."""
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(conteudo))
        return "word/document.xml" in zf.namelist()
    except Exception:
        return False


def _e_odt(conteudo: bytes) -> bool:
    """.odt (OpenDocument Text, formato nativo do LibreOffice) também é um
    .zip por dentro, como o .docx (_e_docx) -- só que com um arquivo
    "mimetype" próprio em vez de word/document.xml. Achado real (edital
    PNCP 88830609000139/2026/371, usuário reportou "análise incompleta"):
    o órgão publicou o edital E o termo de referência em .odt dentro de um
    .zip -- sem esta checagem, os dois caíam no mesmo balde do .zip "burro"
    de PDFs soltos (_texto_de_zip), que só olhava extensão .pdf, e eram
    simplesmente ignorados. A IA acabava analisando só os anexos PDF
    secundários (mapa de riscos, ETP) e nunca o edital de verdade -- o
    aviso "parece cortado" estava certo (faltava a seção de habilitação),
    só que o motivo real não era o teto de 80000 caracteres, era o edital
    nunca ter sido lido."""
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(conteudo))
        return zf.read("mimetype").strip() == b"application/vnd.oasis.opendocument.text"
    except Exception:
        return False


def _texto_de_word_bytes(conteudo: bytes, extensao: str, max_chars: int) -> str:
    """Converte .doc/.docx/.rtf pra texto via LibreOffice headless (binário
    'soffice', instalado no Dockerfile — pacote libreoffice-writer). Não dá
    pra ler o .doc antigo (nem confiar em decodificar RTF na mão — tem
    controle de formatação misturado no meio do texto) com um parser
    caseiro: são formatos complexos o bastante pra existirem ferramentas
    dedicadas só pra isso; é por isso que se usa o LibreOffice em vez de
    tentar decodificar os bytes na mão. Se o binário não estiver instalado
    (ex.: dev local sem o Dockerfile), volta "" silenciosamente — mesmo
    espírito do OCR opcional (ver settings.OCR_ATIVO)."""
    if not shutil.which("soffice"):
        return ""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            entrada = os.path.join(tmp, f"documento{extensao}")
            with open(entrada, "wb") as f:
                f.write(conteudo)
            # -env:UserInstallation isolado por chamada: sem isso, duas
            # conversões concorrentes (2 usuários analisando editais em
            # Word ao mesmo tempo) brigam pelo mesmo perfil padrão do
            # LibreOffice e uma delas trava/falha.
            subprocess.run(
                ["soffice", "--headless", "--norestore",
                 f"-env:UserInstallation=file://{tmp}/perfil",
                 "--convert-to", "txt:Text", "--outdir", tmp, entrada],
                timeout=60, capture_output=True, check=False,
            )
            saida = os.path.join(tmp, "documento.txt")
            if not os.path.exists(saida):
                return ""
            with open(saida, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()[:max_chars]
    except Exception as e:
        log.warning("Falha ao converter Word (%s) via LibreOffice: %s", extensao, e)
        return ""


def _texto_de_pdf_bytes(conteudo: bytes, max_paginas: int, max_chars: int,
                        max_paginas_ocr: int | None = None, marcar_paginas: bool = False) -> str:
    """Extrai texto de um PDF (bytes), com fallback de OCR se vier quase
    vazio (PDF escaneado). `max_paginas_ocr` (opcional) sobrescreve
    settings.OCR_MAX_PAGINAS só pra esta chamada — usado por itens_pdf.py,
    que roda em segundo plano e pode pagar um OCR mais largo.

    `marcar_paginas`: prefixa cada página com "[pág. N]" -- só usado pela
    análise de edital (analisar()), pra IA conseguir dizer em que página
    achou uma exigência/declaração. Fica False por padrão porque
    itens_pdf.py pede pra IA copiar a descrição do item "como está no
    edital" -- se o marcador entrasse no meio do texto, viraria lixo
    colado na descrição extraída."""
    try:
        import pypdf
        leitor = pypdf.PdfReader(io.BytesIO(conteudo))
    except Exception:
        return ""
    partes, total = [], 0
    paginas_processadas = paginas_vazias = 0
    for i, pag in enumerate(leitor.pages):
        if i >= max_paginas:
            break
        try:
            t = pag.extract_text() or ""
        except Exception:
            t = ""
        paginas_processadas += 1
        if not t.strip():
            paginas_vazias += 1
        if marcar_paginas and t.strip():
            t = f"[pág. {i + 1}]\n{t}"
        partes.append(t)
        total += len(t)
        if total > max_chars:
            break
    texto = "\n".join(partes)[:max_chars]

    # PDF escaneado (pypdf extraiu pouco ou nada): tenta OCR como último
    # recurso. O limiar por SOMA é alto de propósito — uma página com texto
    # real de edital tem bem mais que isso; um PDF com só a capa "de texto"
    # e o resto escaneado ficava abaixo do limiar final (300 chars
    # combinados em analisar()) sem nunca acionar o OCR.
    #
    # achado real (edital 142070, PNCP 20765627000140/2026/45): PDF de 40
    # páginas, 38 delas escaneadas (0 chars) -- só 2 no meio (um formulário
    # digitado) tinham texto de verdade, e a SOMA delas já passava do
    # limiar de 500 chars, então o OCR nunca era acionado. O texto de
    # verdade do edital (provavelmente nas páginas escaneadas -- objeto,
    # habilitação) ficava perdido, mesmo com a maior parte do documento
    # nunca lida. Também aciona OCR quando a MAIORIA das páginas processadas
    # veio vazia, não só quando a soma total é pequena -- pega o caso de um
    # documento grande e misto (a maioria escaneada) que a soma sozinha não
    # capturava.
    #
    # Camadas: 1º tenta o modelo de visão (lê tabela de verdade, sem
    # embaralhar colunas -- ver _ocr_pdf_vlm); se ele não estiver
    # disponível ou falhar por qualquer motivo (sem saldo, erro de rede),
    # cai pro Tesseract, exatamente como antes de o VLM existir.
    maioria_vazia = paginas_processadas > 0 and (paginas_vazias / paginas_processadas) > 0.5
    if len(texto.strip()) < 500 or maioria_vazia:
        vlm = _ocr_pdf_vlm(conteudo)
        if vlm:
            return vlm[:max_chars]
        if settings.OCR_ATIVO:
            ocr = _ocr_pdf(conteudo, max_paginas=max_paginas_ocr)
            if ocr:
                return ocr[:max_chars]
    return texto


_EXTENSOES_ZIP_SUPORTADAS = (".pdf", ".odt", ".docx", ".doc", ".rtf")


def _prioridade_arquivo_zip(nome: str) -> int:
    """Prioriza qual arquivo processar primeiro dentro de um zip "burro"
    (_texto_de_zip), quando há mais itens do que cabem no limite. Não dá
    pra reaproveitar _prioridade_arquivo (usada pra ranquear a lista de
    "documentos" do PNCP, cada um já com um título/tipo estruturado) --
    aqui só existe o NOME do arquivo dentro do zip, e o achado real (edital
    PNCP 88830609000139/2026/371) é que o próprio edital principal costuma
    vir nomeado só com um código de processo (ex.: "PESRP170-26.odt"), sem
    a palavra "edital" em lugar nenhum -- caía no mesmo catch-all (pior
    prioridade) que um anexo claramente secundário, e só não causava
    problema aqui porque o zip tinha poucos itens. Sinal positivo
    (retificação/edital/termo de referência) vem primeiro; sinal negativo
    (claramente um anexo de apoio: minuta, ata, mapa de risco, planilha)
    vai pro fim; nome genérico sem nenhum dos dois sinais -- o caso mais
    comum pro próprio edital -- fica no meio, acima do que foi reconhecido
    como secundário."""
    n = nome.lower()
    if any(p in n for p in ("retificaç", "retificac", "errata", "aditamento", "adendo")):
        return 0
    if "edital" in n:
        return 1
    if "termo de refer" in n or "termoreferencia" in n:
        return 2
    if any(p in n for p in (
        "minuta", "ata de registro", "ata_de_registro", "mapa de risco", "mapa_de_risco",
        "estimativa", "cotaç", "cotac", "pesquisa de preç", "pesquisa_de_preç",
        "planilha", "declaraç", "declarac", "procuraç", "procurac", "contrato social",
    )):
        return 4
    return 3


def _texto_de_zip(conteudo: bytes, max_paginas: int, max_chars: int,
                  max_paginas_ocr: int | None = None, marcar_paginas: bool = False) -> str:
    """O PNCP às vezes publica um único 'documento' como um .zip contendo
    vários arquivos (edital + anexos) em vez de um PDF direto. Sem isso,
    esses editais caíam sempre em "sem_texto" (pypdf/pdf2image não leem
    .zip).

    Processa .pdf E .odt/.docx/.doc/.rtf dentro do zip -- achado real
    (edital PNCP 88830609000139/2026/371, ver _e_odt): antes só pegava
    .pdf, então um zip com o edital/termo de referência em .odt e só os
    anexos secundários em .pdf (mapa de riscos, ETP) analisava só os
    anexos e nunca o edital de verdade, mesmo com espaço de sobra no teto
    de caracteres. Ordena por _prioridade_arquivo_zip -- um zip pode ter
    mais itens do que cabem no limite de arquivos processados, e o
    edital/termo de referência (onde fica a habilitação) não pode perder
    a vaga pra uma minuta de contrato ou ata de registro de preços."""
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(conteudo))
    except Exception:
        return ""
    nomes = [n for n in zf.namelist() if n.lower().endswith(_EXTENSOES_ZIP_SUPORTADAS)]
    nomes.sort(key=_prioridade_arquivo_zip)
    partes = []
    total = 0
    for nome in nomes[:8]:
        if total >= max_chars:
            break
        try:
            dados = zf.read(nome)
        except Exception:
            continue
        if nome.lower().endswith(".pdf"):
            t = _texto_de_pdf_bytes(dados, max_paginas, max_chars - total,
                                    max_paginas_ocr=max_paginas_ocr, marcar_paginas=marcar_paginas)
        else:
            extensao = "." + nome.rsplit(".", 1)[-1].lower()
            t = _texto_de_word_bytes(dados, extensao, max_chars - total)
        if t:
            if marcar_paginas:
                t = f"=== DOCUMENTO: {nome} ===\n{t}"
            partes.append(t)
            total += len(t)
    return "\n\n---\n\n".join(partes)[:max_chars]


def _baixar_texto_pdf(url: str, timeout: int = 45, max_paginas: int = 40, max_chars: int = 24000,
                      max_paginas_ocr: int | None = None, marcar_paginas: bool = False) -> tuple[str, bool]:
    """Retorna (texto, falhou_busca). Achado real: um PDF genuinamente
    escaneado/sem texto extraível e uma falha passageira ao BAIXAR o
    arquivo (rede, PNCP fora do ar) geravam o mesmo texto vazio -- a
    Análise por IA (e o completar-descrição de itens) diziam "publicado
    como imagem/escaneado" quando na real só não conseguiu buscar (mesmo
    raciocínio de erro_arquivos_pncp em main.py, um nível mais fundo: ali
    é a LISTA de arquivos, aqui é o CONTEÚDO de um arquivo). falhou_busca
    só fica True quando nem chegou a baixar os bytes (rede/HTTP não-200) —
    depois de 1 retentativa curta, já que o PNCP tem se mostrado instável
    (503 passageiro observado em produção). Um download com sucesso mas
    sem texto extraível (scan ruim, OCR indisponível) devolve texto=""
    com falhou_busca=False -- isso SIM é "sem texto legível" de verdade."""
    r = None
    for tentativa in range(2):
        try:
            r = requests.get(url, timeout=timeout,
                            headers={"User-Agent": "RadarLicitacoes/1.0"})
        except requests.RequestException:
            r = None
        if r is not None and r.status_code not in (500, 502, 503, 504):
            break
        if tentativa == 0:
            time.sleep(1.5)
    if r is None or r.status_code != 200 or not r.content:
        return "", True
    if r.content[:8] == _MAGIC_OLE2:
        return _texto_de_word_bytes(r.content, ".doc", max_chars), False
    if r.content[:5] == _MAGIC_RTF:
        return _texto_de_word_bytes(r.content, ".rtf", max_chars), False
    if _e_zip(r.content):
        if _e_docx(r.content):
            return _texto_de_word_bytes(r.content, ".docx", max_chars), False
        if _e_odt(r.content):
            return _texto_de_word_bytes(r.content, ".odt", max_chars), False
        return _texto_de_zip(r.content, max_paginas, max_chars,
                             max_paginas_ocr=max_paginas_ocr, marcar_paginas=marcar_paginas), False
    return _texto_de_pdf_bytes(r.content, max_paginas, max_chars,
                               max_paginas_ocr=max_paginas_ocr, marcar_paginas=marcar_paginas), False


def _ocr_imagem(conteudo: bytes) -> str:
    """OCR de uma imagem solta (jpg/png/webp/...), mesmo motor do OCR de PDF
    escaneado (_ocr_pdf), só que sem o passo de rasterizar páginas — a
    imagem já É a página."""
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        log.warning("OCR indisponível (pytesseract/Pillow não instalados).")
        return ""
    try:
        img = Image.open(io.BytesIO(conteudo))
        return pytesseract.image_to_string(img, lang=settings.OCR_IDIOMA).strip()
    except Exception as e:
        log.warning("Falha no OCR de imagem: %s", e)
        return ""


def extrair_texto_upload(nome_arquivo: str, conteudo: bytes, content_type: str | None,
                         max_chars: int = 16000) -> str:
    """Extrai texto de um arquivo que o usuário enviou (PDF ou imagem) —
    mesmo pipeline de PDF/OCR usado pra ler o edital do PNCP, só que a
    origem é um upload em vez de uma URL. Não persiste nada em disco: o
    conteúdo já vem em memória (bytes), e este texto é só o que vai pra IA
    (o arquivo original, esse sim, é guardado -- ver Documento.arquivo_cifrado)."""
    nome = (nome_arquivo or "").lower()
    eh_pdf = nome.endswith(".pdf") or (content_type or "") == "application/pdf"
    if eh_pdf:
        return _texto_de_pdf_bytes(conteudo, max_paginas=20, max_chars=max_chars)
    if not settings.OCR_ATIVO:
        return ""
    return _ocr_imagem(conteudo)[:max_chars]


_PROMPT_EXTRAIR_VALIDADE = """Abaixo está o texto de um documento de habilitação (certidão, CND, FGTS, alvará, etc.) de uma empresa brasileira que participa de licitações públicas.

Encontre a DATA DE VALIDADE do documento -- até quando ele vale, não a data de emissão. Se o texto só tiver "data de emissão" e o tipo de certidão tiver um prazo de validade padrão conhecido e óbvio (ex.: certidões federais costumam valer 180 dias / 6 meses da emissão), pode calcular a partir da emissão. Se não conseguir determinar com segurança, responda null -- não chute.

Responda APENAS com um JSON válido (sem texto fora do JSON, sem ```), com exatamente esta estrutura:
{{"data_validade": "AAAA-MM-DD" ou null}}

TEXTO DO DOCUMENTO:
{texto}"""


def extrair_validade_documento(texto: str, api_key: str | None = None) -> date | None:
    """Lê o texto de um documento de habilitação já extraído (upload do
    usuário) e pede à IA só a data de validade -- nada de julgar se o
    documento atende alguma exigência de edital ou emitir veredito de
    apto/inapto, isso é outra funcionalidade (verificar_documentos_usuario,
    mais abaixo) e não deve ser tocado por esta. Retorna None sempre que não
    tiver certeza (sem chave, sem data no texto, resposta malformada) -- o
    chamador cai pra pedir a data manualmente, nunca inventa um valor."""
    texto = (texto or "").strip()
    if not texto or not ia_texto_disponivel(api_key):
        return None
    prompt = _PROMPT_EXTRAIR_VALIDADE.format(texto=texto[:8000])
    txt, status = _gerar(prompt, api_key=api_key, timeout=30)
    if status != "ok" or not txt:
        return None
    dados = _parse_json(txt)
    if not isinstance(dados, dict) or not dados.get("data_validade"):
        return None
    try:
        return date.fromisoformat(str(dados["data_validade"]))
    except (ValueError, TypeError):
        return None


_DEEPINFRA_CHAT_URL_VLM = "https://api.deepinfra.com/v1/openai/chat/completions"

_PROMPT_OCR = """Transcreva TODO o texto desta página de um edital de licitação pública brasileiro, em Markdown, em português, sem resumir, reescrever ou traduzir.

Se houver uma ou mais TABELAS na página: preserve a estrutura. Cada linha da tabela vira uma linha do Markdown, colunas separadas por "|". Não junte colunas nem mescle linhas. Célula com texto longo (especificação técnica) fica inteira na célula da SUA linha, associada ao número do item correto dessa MESMA linha -- nunca misture a especificação de um item com a de outro. Se houver mais de uma tabela, transcreva todas, na ordem em que aparecem.

Evite símbolos que costumam sair corrompidos na resposta: escreva "m2" em vez de "m²", "graus" por extenso em vez de "°", hífen simples "-" em vez de travessão "–" ou "—". Use só caracteres ASCII comuns e letras acentuadas do português (á é í ó ú ã õ â ê ô ç)."""


def _chamar_vlm_pagina(imagem_b64: str, api_key: str, timeout: int = 60, tentativas: int = 2) -> str | None:
    """Manda uma página (imagem em base64) pro modelo de visão da DeepInfra
    e devolve o texto transcrito, ou None se a chamada falhar (sem saldo,
    erro de rede, timeout, resposta inesperada etc.) -- quem chama trata
    None como "essa página não deu" e para (best-effort, mesmo espírito do
    resto do OCR). Mesmo padrão de retentativa de itens_pdf.py._gerar:
    só reage a instabilidade passageira (5xx/rede); HTTP 402 (sem saldo)
    ou 429 (limite) não são passageiros -- falha na hora, sem retentar."""
    body = {
        "model": settings.DEEPINFRA_MODELO_OCR,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{imagem_b64}"}},
            {"type": "text", "text": _PROMPT_OCR},
        ]}],
        "temperature": 0.1,
    }
    for tentativa in range(1, max(1, tentativas) + 1):
        try:
            r = requests.post(_DEEPINFRA_CHAT_URL_VLM, json=body, timeout=timeout,
                              headers={"Authorization": f"Bearer {api_key}",
                                       "Content-Type": "application/json"})
        except requests.RequestException as e:
            if tentativa < tentativas:
                time.sleep(1.5 * tentativa)
                continue
            log.warning("VLM de OCR falha de rede: %s", e)
            return None
        if r.status_code in (500, 502, 503, 504):
            if tentativa < tentativas:
                time.sleep(1.5 * tentativa)
                continue
            log.warning("VLM de OCR HTTP %s (esgotou tentativas)", r.status_code)
            return None
        if r.status_code != 200:
            # inclui 402 (sem saldo na DeepInfra) e 429 (limite de taxa) --
            # falha "estrutural" da chamada, não passageira, não adianta retentar.
            log.warning("VLM de OCR HTTP %s: %s", r.status_code, r.text[:200])
            return None
        try:
            return r.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError):
            return None
    return None


def _ocr_pdf_vlm(conteudo: bytes) -> str:
    """OCR de PDF escaneado via modelo de visão (VLM) na DeepInfra, em vez
    do Tesseract -- lê tabela de verdade (preserva estrutura de colunas/
    linhas), enquanto o Tesseract embaralha e mistura a descrição de um
    item com a do vizinho (achado real: edital 56106, item 24 -- validado
    contra a API real, o VLM leu a linha certa sem misturar com os itens
    vizinhos). Pago por página (chave global do operador,
    DEEPINFRA_API_KEY) -- por isso um limite de páginas PRÓPRIO e menor
    que o do Tesseract (settings.OCR_VLM_MAX_PAGINAS, não
    OCR_MAX_PAGINAS). Best-effort: QUALQUER falha (sem VLM ativo/sem
    chave, sem saldo, erro de rede, indisponível) devolve "" -- quem
    chama (_texto_de_pdf_bytes) cai pro Tesseract normalmente, exatamente
    como se o VLM não existisse."""
    api_key = settings.DEEPINFRA_API_KEY
    if not (settings.OCR_VLM_ATIVO and api_key):
        return ""
    try:
        from pdf2image import convert_from_bytes
    except Exception:
        log.warning("VLM de OCR indisponível (pdf2image não instalado).")
        return ""
    try:
        imagens = convert_from_bytes(
            conteudo, dpi=settings.OCR_DPI, first_page=1,
            last_page=settings.OCR_VLM_MAX_PAGINAS,
            timeout=settings.OCR_ORCAMENTO_SEGUNDOS)
    except Exception as e:
        log.warning("Falha ao rasterizar PDF pro VLM de OCR: %s", e)
        return ""

    partes = []
    for img in imagens:
        buf = io.BytesIO()
        # JPEG, não PNG: achado real (validado contra a API de verdade) --
        # reencodar a página como PNG (sem perdas) saiu ~3.5x maior que o
        # JPEG equivalente pra uma imagem de scan/foto, e isso já bastou
        # pra causar timeout de rede na chamada. JPEG com qualidade alta é
        # prática padrão pra OCR via VLM (a imagem já é fundamentalmente
        # fotográfica, não teve perda relevante de legibilidade no teste).
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        imagem_b64 = base64.b64encode(buf.getvalue()).decode()
        texto = _chamar_vlm_pagina(imagem_b64, api_key)
        if texto is None:
            # achado real (edital 141791): abortar o documento inteiro na
            # 1ª página que falhar (ex.: um 429 passageiro de rate limit,
            # bem comum numa API paga sob carga) descartava TODAS as
            # páginas seguintes -- mesmo sem nenhum motivo pra achar que
            # elas também falhariam. A IA sinalizou "pula do item 11.4 pro
            # 12.4.1" -- exatamente a cara de uma página no meio do
            # documento sumindo sozinha. Pula só a página que falhou (best-
            # effort por página, não por documento) e continua com as
            # seguintes -- perder 1 página é bem menos grave que perder
            # o resto do documento por causa dela.
            log.warning("VLM de OCR: uma página falhou -- pula ela e continua com as seguintes.")
            continue
        partes.append(texto)

    texto_final = "\n\n".join(partes).strip()
    # achado real: caracteres especiais (², –) às vezes saem corrompidos
    # como replacement character (U+FFFD) DIRETO no payload da própria
    # API -- confirmado que não é bug de decodificação do lado cliente.
    # A instrução no prompt reduz, mas não garante 100%; limpa na saída
    # também, pra nunca deixar "�" literal ir parar em ItemEdital.descricao.
    texto_final = texto_final.replace("�", "")
    if texto_final:
        log.info("VLM de OCR extraiu %d caracteres de PDF escaneado (%d página(s)).",
                 len(texto_final), len(partes))
    return texto_final


def _ocr_pdf(conteudo: bytes, max_paginas: int | None = None) -> str:
    """OCR de PDF escaneado com Tesseract (grátis, local, sem GPU).
    Pesado: limita o nº de páginas para não sobrecarregar o servidor
    (settings.OCR_MAX_PAGINAS, salvo `max_paginas` explícito) E o tempo
    total (settings.OCR_ORCAMENTO_SEGUNDOS) -- rasterizar/reconhecer texto
    numa CPU fraca não tem limite natural nenhum, então isso evita ficar
    rodando indefinidamente e prendendo a trava do chamador pra sempre.
    Requer os binários do sistema 'tesseract-ocr' e 'poppler-utils'."""
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
    except Exception:
        log.warning("OCR indisponível (pytesseract/pdf2image não instalados).")
        return ""
    inicio = time.monotonic()
    orcamento = settings.OCR_ORCAMENTO_SEGUNDOS
    try:
        # converte só as primeiras páginas em imagem (DPI moderado p/ velocidade)
        imagens = convert_from_bytes(
            conteudo, dpi=settings.OCR_DPI, first_page=1,
            last_page=max_paginas or settings.OCR_MAX_PAGINAS,
            timeout=orcamento)
    except Exception as e:
        log.warning("Falha ao rasterizar PDF para OCR: %s", e)
        return ""
    partes = []
    for img in imagens:
        restante = orcamento - (time.monotonic() - inicio)
        if restante <= 0:
            log.warning("OCR interrompido por orçamento de tempo (%ds) -- aproveitando o que já foi lido.",
                       orcamento)
            break
        try:
            partes.append(pytesseract.image_to_string(img, lang=settings.OCR_IDIOMA, timeout=restante))
        except Exception as e:
            # achado real (edital 141791, mesmo raciocínio do VLM acima):
            # uma falha do Tesseract numa página específica (imagem
            # corrompida, timeout daquela página) não é motivo pra
            # descartar as páginas seguintes -- pula só esta e continua.
            log.warning("Falha no OCR de uma página (pulando, continuando com as seguintes): %s", e)
            continue
    texto = "\n".join(partes).strip()
    if texto:
        log.info("OCR extraiu %d caracteres de PDF escaneado.", len(texto))
    return texto


_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


# Teto de quanto vale a pena ESPERAR um 429 de rate limit (tokens/requisições
# por minuto) passar antes de desistir -- um pouco acima da janela de 60s que
# a Groq usa (achado real, ver _GROQ_LIMITE_PROMPT_BYTES). Só espera quando o
# PRÓPRIO provedor informa quanto falta (Retry-After ou "please try again in
# Xs" no corpo, formato usado pela Groq) -- nunca um valor chutado: sem essa
# informação não dá pra saber se esperar vai adiantar alguma coisa (pode ser
# cota diária esgotada, tipo a do Gemini free tier, que não volta em 1 min).
_RETRY_APOS_429_TETO_S = 61.0


def _retry_after_segundos(r) -> float | None:
    """Tempo sugerido pelo provedor pra esperar antes de tentar de novo um
    429, quando confiável (positivo e dentro do teto) -- None caso
    contrário (não veio, veio inválido, ou é longo demais pra valer a pena
    segurar o trabalho em segundo plano esperando)."""
    bruto = None
    try:
        bruto = r.headers.get("Retry-After") or r.headers.get("retry-after")
    except Exception:
        bruto = None
    # só str/int/float -- em teste, um MagicMock sem headers configurado
    # devolve outro MagicMock aqui (não None), e float(MagicMock()) não
    # levanta erro (retorna 1.0, valor padrão do dunder __float__ mockado).
    if isinstance(bruto, (str, int, float)):
        try:
            segundos = float(bruto)
            if 0 < segundos <= _RETRY_APOS_429_TETO_S:
                return segundos
        except (TypeError, ValueError):
            pass
    texto = getattr(r, "text", None)
    m = re.search(r"try again in ([\d.]+)\s*s", texto, re.IGNORECASE) if isinstance(texto, str) else None
    if m:
        try:
            segundos = float(m.group(1))
            if 0 < segundos <= _RETRY_APOS_429_TETO_S:
                return segundos
        except ValueError:
            pass
    return None


def _post_com_retry(url: str, headers: dict, body: dict, timeout: int, tentativas: int,
                    extrair_texto, rotulo: str, ensure_ascii: bool = True):
    """POST com a retentativa curta (backoff simples) comum a qualquer
    provedor de IA usado aqui — mesmo espírito do
    PNCPConnector._get_com_retry — pra falha TRANSIENTE (timeout/rede,
    5xx). Em 429 (rate limit) só tenta de novo quando o provedor informa
    quanto esperar E esse tempo é confiável (ver _retry_after_segundos) —
    achado real: numa comparação de catálogo em lotes, cada lote batia no
    limite de tokens/minuto da Groq em sequência rápida (sem essa espera,
    todos os lotes seguintes falhavam igual, mesmo o limite sendo por
    JANELA DE TEMPO, não cota fixa). Sem essa informação, retentar na hora
    só reforça o limite -- devolve o erro direto, mesmo comportamento de
    antes. Consome o mesmo orçamento de `tentativas` do backoff de 5xx (não
    é um mecanismo à parte). extrair_texto(dados_json) -> texto da
    resposta, específico do formato de cada provedor (Gemini x Groq).

    ensure_ascii=False (achado real do agente error-detective): `requests`,
    com `json=body`, serializa via `json.dumps` com ensure_ascii=TRUE por
    padrão -- cada caractere acentuado (ç, ã, é...) vira um escape \\uXXXX
    de 6 bytes, em vez dos 2 bytes reais em UTF-8. Isso destruía o corte
    por bytes de _chamar_groq: o corpo de verdade enviado podia ficar
    3x maior do que o que foi medido antes de truncar, reproduzindo o
    mesmo 413 que o corte deveria ter evitado. Serializa manualmente com
    ensure_ascii=False (corpo de verdade menor) quando o chamador pede --
    achado real #2 (edital 125821): "Gemini nunca foi capado por bytes,
    não tem esse risco" só era verdade enquanto o teto de caracteres do
    prompt (MAX_TOTAL) era pequeno o bastante pra nunca aproximar do
    limite de tamanho de requisição do próprio Gemini, mesmo com a
    inflação de 3x -- assim que MAX_TOTAL subiu, _chamar_modelo passou a
    pedir ensure_ascii=False também."""
    ultimo_erro = "sem_resposta"
    for tentativa in range(1, max(1, tentativas) + 1):
        try:
            if ensure_ascii:
                r = requests.post(url, json=body, timeout=timeout, headers=headers)
            else:
                corpo = json.dumps(body, ensure_ascii=False).encode("utf-8")
                cabecalhos = {**headers, "Content-Type": "application/json; charset=utf-8"}
                r = requests.post(url, data=corpo, timeout=timeout, headers=cabecalhos)
        except requests.RequestException as e:
            ultimo_erro = f"rede:{e}"
            if tentativa < tentativas:
                time.sleep(1.5 * tentativa)
                continue
            return None, ultimo_erro
        if r.status_code in (500, 502, 503, 504):
            ultimo_erro = f"http_{r.status_code}"
            log.warning("%s HTTP %s (tentativa %d/%d): %s",
                       rotulo, r.status_code, tentativa, tentativas, r.text[:200])
            if tentativa < tentativas:
                time.sleep(1.5 * tentativa)
                continue
            return None, ultimo_erro
        if r.status_code == 429:
            ultimo_erro = "http_429"
            espera = _retry_after_segundos(r)
            log.warning("%s HTTP 429 (tentativa %d/%d, espera=%s): %s",
                       rotulo, tentativa, tentativas, espera, r.text[:200])
            if espera is not None and tentativa < tentativas:
                time.sleep(espera)
                continue
            return None, ultimo_erro
        if r.status_code != 200:
            log.warning("%s HTTP %s: %s", rotulo, r.status_code, r.text[:200])
            # achado real (edital 125821): 413 do Gemini persistindo mesmo
            # trocando de chave/tamanho de texto -- sem acesso a log de
            # produção pra ver o corpo real da resposta, o "http_413" sozinho
            # não dizia se o erro vinha mesmo do Gemini (limite real deles) ou
            # de algo no caminho de rede (proxy/egress) antes de chegar lá.
            # Anexa um trecho curto do corpo da resposta ao erro (só nesse
            # branch genérico -- 429/5xx já têm log dedicado e continuam
            # exatos "http_429"/"http_5xx" pros comparadores que dependem
            # disso) pra aparecer no "detalhe" que o app expõe.
            detalhe = r.text[:200].strip()
            erro = f"http_{r.status_code}"
            if detalhe:
                erro = f"{erro}:{detalhe}"
            return None, erro
        try:
            return extrair_texto(r.json()), "ok"
        except (ValueError, KeyError, IndexError):
            return None, "sem_resposta"
    return None, ultimo_erro


def _chamar_modelo(modelo: str, body: dict, chave: str, timeout: int, tentativas: int):
    """1 modelo Gemini.

    ensure_ascii=False (achado real, edital 125821: HTTP 413 do Gemini
    depois de subir MAX_TOTAL de 80000 pra 200000 chars): "Gemini nunca
    foi capado por bytes, não tem esse risco" deixou de ser verdade assim
    que o teto de caracteres subiu o bastante -- o mesmo problema já
    corrigido pra Groq (ver docstring de _post_com_retry) também vale
    aqui: sem isso, `requests` com `json=body` serializa com
    ensure_ascii=True por padrão, e um prompt de 200000 caracteres em
    português (cheio de ç/ã/é/õ) pode virar bem mais que 400KB de corpo
    de verdade -- estourando o limite de tamanho de requisição do próprio
    Gemini."""
    url = f"{_BASE}/{modelo}:generateContent"
    headers = {"x-goog-api-key": chave, "Content-Type": "application/json"}
    return _post_com_retry(
        url, headers, body, timeout, tentativas,
        extrair_texto=lambda d: d["candidates"][0]["content"]["parts"][0]["text"],
        rotulo=f"Gemini texto ({modelo})", ensure_ascii=False)


# Achado real em produção (edital de 350 itens, modelo antigo
# openai/gpt-oss-120b): a Groq devolveu HTTP 413 ("Request too large...
# TPM: Limit 8000, Requested 11382") -- confirmado na doc deles que os
# modelos grandes do tier gratuito (120b, 20b) compartilham só 8000
# tokens/minuto, POR REQUISIÇÃO, não uma cota que enche e esvazia. Trocado
# pro groq/compound-mini (ver comentário em settings.GROQ_MODELO_TEXTO em
# config.py): 70000 tokens/minuto, confirmado ao vivo via header
# x-ratelimit-limit-tokens -- ~8.75x mais espaço.
#
# 2º achado real (edital 134686, já no compound-mini): 413 de novo, mas
# com mensagem GENÉRICA ("Request Entity Too Large" / code
# "request_too_large"), sem os números de TPM que o erro acima tinha --
# na doc da Groq (console.groq.com/docs/errors), 413 é um código PRÓPRIO,
# separado do 429 (esse sim é o de tokens/minuto, com número). Ou seja,
# não é mais o teto de tokens/minuto -- é o corpo bruto da requisição
# (bytes) estourando um limite à parte, não documentado publicamente. O
# corte antigo (`_GROQ_LIMITE_PROMPT_CHARS`) media CARACTERES, não bytes
# -- texto em português (ç, ã, é, õ...) vira 2 bytes por caractere em
# UTF-8, então um prompt de 70000 caracteres podia virar ~140KB de corpo
# de verdade sem o corte perceber. Agora mede bytes UTF-8 reais, com um
# teto bem mais conservador (sem número oficial da Groq pra mirar).
# Corta só a ponta do TEXTO DO EDITAL (fica sempre no fim do _PROMPT) --
# as instruções completas continuam intactas, e o próprio prompt já pede
# pra IA sinalizar "analise_incompleta" quando o texto parece cortado no
# meio, então truncar aqui é seguro (mesmo raciocínio de MAX_TOTAL em
# analisar(), só que com um teto ainda menor, específico da Groq).
# max_tokens explícito reserva espaço pra resposta dentro do orçamento de
# tokens/minuto (entrada + saída contam juntas) -- achado real: o valor
# antigo (3000) podia cortar a resposta em editais com muita exigência de
# habilitação, bem menor que o maxOutputTokens usado pro Gemini (16384).
#
# 3º achado real (usuário reportou, edital 135627, 2026-09-22): groq/
# compound-mini parou de existir -- a Groq anunciou a descontinuação em
# 24/08/2026 e desligou de vez em 21/09/2026 (console.groq.com/docs/
# deprecations); toda chamada passou a devolver HTTP 404 "model_not_found".
# Voltou pro openai/gpt-oss-120b (settings.GROQ_MODELO_TEXTO em config.py)
# -- e com ele volta também o teto de 8000 tokens/minuto POR REQUISIÇÃO que
# motivou a troca pro compound-mini em primeiro lugar (1º achado acima).
# Sem reduzir o orçamento junto, o mesmo 413 "TPM: Limit 8000" do 1º achado
# ia se repetir na hora, com o cap de bytes antigo (dimensionado pros 70000
# tokens/min do compound-mini, não pros 8000 do gpt-oss-120b). Preferido
# reduzir o orçamento AGORA (mesmo sem um jeito preciso de converter bytes
# UTF-8 em tokens -- só uma estimativa conservadora, ~4-5 bytes/token em
# português) a esperar o próximo 413/429 pra descobrir nesse tamanho -- o
# 1º achado já provou, com número real, que esse modelo especificamente
# não aguenta o teto antigo. Groq continua sendo só o ÚLTIMO recurso da
# cadeia (depois dos 2 modelos Gemini falharem) -- um fallback mais restrito
# ainda é melhor que um que sempre falha com 404.
_GROQ_LIMITE_PROMPT_BYTES = 20_000
# reduzido de 6000 (era dimensionado pro orçamento do compound-mini) --
# ver comentário grande acima sobre a volta pro gpt-oss-120b e o teto de
# 8000 tokens/min por requisição (prompt + resposta somados).
_GROQ_MAX_TOKENS_RESPOSTA = 2500


def _truncar_utf8(texto: str, max_bytes: int) -> str:
    """Corta `texto` pra no máximo `max_bytes` bytes UTF-8, sem quebrar um
    caractere multibyte no meio (isso geraria um byte inválido no meio do
    corpo da requisição -- pior que só truncar cedo demais)."""
    bruto = texto.encode("utf-8")
    if len(bruto) <= max_bytes:
        return texto
    bruto = bruto[:max_bytes]
    while bruto:
        try:
            return bruto.decode("utf-8")
        except UnicodeDecodeError:
            bruto = bruto[:-1]
    return ""


def _chamar_groq(prompt: str, timeout: int, tentativas: int):
    """Último recurso, provedor DIFERENTE do Gemini (settings.GROQ_API_KEY,
    chave global do operador) -- API compatível com formato OpenAI. Sem
    response_schema aqui (Groq não tem o mesmo mecanismo de schema do
    Gemini): "response_format: json_object" garante JSON válido, mas quem
    segura a estrutura esperada continua sendo só a prosa do _PROMPT --
    igual era pro Gemini antes do schema existir."""
    if not settings.GROQ_API_KEY:
        return None, "sem_chave_groq"
    tamanho_original = len(prompt.encode("utf-8"))
    prompt = _truncar_utf8(prompt, _GROQ_LIMITE_PROMPT_BYTES)
    if tamanho_original > _GROQ_LIMITE_PROMPT_BYTES:
        # achado real (2x já): se o corte de 90_000 bytes um dia se mostrar
        # insuficiente de novo, isso aqui é o que vai diferenciar "o corte
        # não bastou" de "é outra causa" no log de produção -- sem isso, um
        # 413 futuro fica tão sem pista quanto os dois anteriores.
        log.warning("Groq: prompt cortado de %d para %d bytes (limite %d)",
                   tamanho_original, len(prompt.encode("utf-8")), _GROQ_LIMITE_PROMPT_BYTES)
    body = {
        "model": settings.GROQ_MODELO_TEXTO,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": _GROQ_MAX_TOKENS_RESPOSTA,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.GROQ_API_KEY}", "Content-Type": "application/json"}
    return _post_com_retry(
        _GROQ_URL, headers, body, timeout, tentativas,
        extrair_texto=lambda d: d["choices"][0]["message"]["content"],
        rotulo="Groq texto", ensure_ascii=False)


_MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
# folga bem maior que a do Groq (_GROQ_MAX_TOKENS_RESPOSTA) -- o orçamento
# de tokens/min da Mistral pra este modelo é ~78x o do Groq (ver achado
# real em settings.MISTRAL_MODELO_TEXTO), sobra espaço de sobra sem
# precisar cortar o texto do edital como precisa fazer pro Groq.
_MISTRAL_MAX_TOKENS_RESPOSTA = 8000


def _chamar_mistral(prompt: str, timeout: int, tentativas: int):
    """2º provedor diferente do Gemini na cadeia (settings.MISTRAL_API_KEY,
    chave global do operador), tentado ANTES do Groq -- ver achado real em
    settings.MISTRAL_MODELO_TEXTO sobre por que esse modelo específico e
    por que o orçamento de tokens é tão mais folgado que o do Groq. API
    compatível com formato OpenAI, mesmo padrão de _chamar_groq -- sem
    response_schema aqui também: "response_format: json_object" garante
    JSON válido, a estrutura em si continua vindo da prosa do _PROMPT
    (Mistral tem um mecanismo de JSON Schema próprio, mas exigiria
    converter o dialeto do Gemini -- "OBJECT"/"STRING" maiúsculo -- pro
    JSON Schema padrão; não vale o risco pra um fallback de fallback)."""
    if not settings.MISTRAL_API_KEY:
        return None, "sem_chave_mistral"
    body = {
        "model": settings.MISTRAL_MODELO_TEXTO,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": _MISTRAL_MAX_TOKENS_RESPOSTA,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.MISTRAL_API_KEY}", "Content-Type": "application/json"}
    return _post_com_retry(
        _MISTRAL_URL, headers, body, timeout, tentativas,
        extrair_texto=lambda d: d["choices"][0]["message"]["content"],
        rotulo="Mistral texto", ensure_ascii=False)


def _gerar(prompt: str, api_key: str | None = None, timeout: int = 70, tentativas: int = 2,
          response_schema: dict | None = None, max_output_tokens: int = 16384,
          permitir_groq: bool = True):
    """Chama o Gemini (settings.IA_MODELO_TEXTO). Achado real: 503 ("modelo
    sobrecarregado") acontecendo com frequência mesmo depois de esgotar as
    retentativas -- ao falhar por completo num modelo com um erro que
    parece sobrecarga/limite/modelo indisponível (5xx, 429, 404 ou rede),
    tenta o próximo antes de desistir de vez: 1º IA_MODELO_TEXTO_FALLBACK
    (mesma chave do usuário, ainda Gemini), depois Mistral
    (settings.MISTRAL_API_KEY), depois, se ainda assim falhar, Groq
    (settings.GROQ_API_KEY) -- Mistral e Groq são provedores DIFERENTES do
    Gemini (protege contra uma instabilidade do Google inteiro, não só de
    1 modelo), tentados nessa ordem porque a Mistral tem muito mais
    orçamento de tokens/minuto no tier gratuito desta conta (ver achado
    real em settings.MISTRAL_MODELO_TEXTO) -- Groq fica como o ÚLTIMO
    recurso de todos. 429 troca de modelo/provedor (pedido do usuário:
    mesmo cota costumando ser por projeto — não necessariamente por
    modelo dentro do mesmo projeto —, e cada provedor de fallback tem cota
    totalmente independente da do Gemini) mas NÃO retenta 429 dentro do
    MESMO modelo (isso continua sem efeito — ver _post_com_retry). 404
    também troca -- achado real: o Gemini desativa modelo antigo pra
    contas novas sem cumprir a data de desligamento anunciada
    (gemini-2.5-flash sumiu antes do previsto); sem tratar 404 como "tenta
    o próximo", um modelo desatualizado em IA_MODELO_TEXTO_FALLBACK
    travava a cadeia ali mesmo, sem nunca chegar nos outros provedores.
    Outros 4xx (400 etc.) do Gemini não trocam de modelo/provedor: não é
    erro de limite/disponibilidade, retentar (mesmo modelo diferente) não
    costuma ajudar -- mas uma vez que a cadeia SAI do Gemini, qualquer
    falha da Mistral (mesmo não-transiente) ainda tenta o Groq depois:
    são provedores sem relação nenhuma entre si, um erro específico de um
    não prediz nada sobre o outro.

    response_schema: opcional, Schema (formato Gemini) pra forçar tipos/
    chaves obrigatórias/enums no decoder — em vez de só pedir por prosa
    ("nunca troque lista por false"). Usado hoje só por analisar(); os
    demais chamadores continuam sem schema (JSON livre). Só vale pros
    modelos Gemini -- nem Mistral nem Groq usam esse mecanismo aqui (ver
    _chamar_mistral/_chamar_groq).

    permitir_groq: achado real (usuário reportou, edital 139008, catálogo
    de 3690 produtos): _chamar_groq trunca QUALQUER prompt em
    _GROQ_LIMITE_PROMPT_BYTES (20000 bytes) sem saber o que está cortando.
    Pra analisar() isso é seguro (corta só o FIM do texto do edital, e o
    próprio prompt já pede pra IA sinalizar "analise_incompleta" quando
    percebe o corte). Mas comparar_catalogo_usuario() manda o CATÁLOGO
    inteiro dentro do prompt -- com um catálogo grande (na casa de milhares
    de produtos, ~100000+ chars só de catálogo), truncar em 20000 bytes
    sobra menos de 20% dos produtos, cortados no meio de qualquer jeito.
    A IA continuava respondendo "com sucesso" pra todos os itens pedidos,
    mesmo sem ver a maioria do catálogo -- não é o mesmo tipo de corte
    "seguro" de analisar(): aqui o corte tira candidatos de vista, não só
    encurta contexto de apoio, e o resultado (candidatos genuinamente
    errados/desalinhados, mas parecendo um "sucesso" normal) é pior que
    simplesmente reportar esse lote como falha (comparar_catalogo_usuario
    já lida bem com lote com falha -- ver lotes_com_falha). Só o chamador
    de comparação passa False aqui; analisar() continua com Groq normal."""
    chave = api_key   # só a chave do próprio usuário (sem fallback global)
    if not chave:
        return None, "sem_chave"
    generation_config = {
        "temperature": 0.2, "responseMimeType": "application/json",
        # maxOutputTokens explícito: achado real num edital com 57 itens —
        # comparar_catalogo_usuario() pede um JSON com até 2 candidatos por
        # item, e sem esse limite a resposta usa o padrão implícito do
        # modelo, que pode não ser suficiente pra um JSON desse tamanho —
        # a IA responde 200 (não é erro_ia), mas o texto vem cortado no
        # meio e falha ao parsear (status "resposta_invalida"). Parametrizado
        # (não fixo em 16384) porque analisar() agora também pede "lotes"
        # (composição de cada lote, um array de itens que cresce com o
        # tamanho do edital) dentro da MESMA resposta única que já cobre
        # habilitação/prazos/declarações/etc -- um edital grande e
        # multi-lote correria o mesmo risco de corte que já quebrou
        # comparar_catalogo_usuario() uma vez, só que aqui um corte derruba
        # a análise INTEIRA (não só a lista de lotes), então o teto pro
        # chamador mais arriscado (analisar()) precisa de mais folga que o
        # padrão -- ver call site em analisar().
        "maxOutputTokens": max_output_tokens,
    }
    if response_schema is not None:
        generation_config["responseSchema"] = response_schema
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }

    modelos = [settings.IA_MODELO_TEXTO]
    if settings.IA_MODELO_TEXTO_FALLBACK and settings.IA_MODELO_TEXTO_FALLBACK != settings.IA_MODELO_TEXTO:
        modelos.append(settings.IA_MODELO_TEXTO_FALLBACK)

    ultimo_erro = "sem_resposta"
    eh_transiente = False
    for i, modelo in enumerate(modelos):
        txt, erro = _chamar_modelo(modelo, body, chave, timeout, tentativas)
        if txt is not None:
            return txt, erro
        ultimo_erro = erro
        # startswith, não == -- erro pode vir com um trecho do corpo da
        # resposta colado depois de ":" (ver _post_com_retry).
        eh_transiente = (erro.startswith("http_5") or erro.startswith("http_429")
                        or erro.startswith("http_404") or erro.startswith("rede:"))
        if not eh_transiente:
            return None, ultimo_erro

    # os 2 modelos Gemini esgotaram com erro de sobrecarga/limite/modelo
    # indisponível/rede -- tenta Mistral antes do Groq (mais orçamento de
    # tokens/minuto, ver docstring desta função). Sem custo se
    # MISTRAL_API_KEY não estiver configurada: _chamar_mistral devolve
    # "sem_chave_mistral" sem chamar rede.
    if eh_transiente:
        txt, erro = _chamar_mistral(prompt, timeout, tentativas)
        if txt is not None:
            return txt, erro
        if erro != "sem_chave_mistral":
            # prefixo "mistral_": mesmo raciocínio do "groq_" logo abaixo
            # -- sem isso o front não tem como saber que esse erro veio de
            # um provedor de fallback (cota do operador), não da chave
            # Gemini pessoal do usuário.
            ultimo_erro = f"mistral_{erro}"

    # Mistral falhou (ou nem estava configurada) -- última tentativa,
    # outro provedor diferente ainda (sem custo se GROQ_API_KEY não
    # estiver configurada: _chamar_groq devolve "sem_chave_groq" sem
    # chamar rede). Tenta mesmo se o erro da Mistral não parecia
    # transiente -- são provedores sem relação nenhuma entre si, uma
    # falha específica de um não prediz nada sobre o outro.
    if eh_transiente and not permitir_groq:
        # ver docstring de `permitir_groq` -- pra este chamador, um "sucesso"
        # do Groq com o prompt truncado é pior que reportar falha aqui.
        return None, ultimo_erro
    if eh_transiente:
        txt, erro = _chamar_groq(prompt, timeout, tentativas)
        if txt is not None:
            return txt, erro
        if erro != "sem_chave_groq":
            # prefixo "groq_": o erro final veio do Groq (recurso
            # compartilhado/de operador), não da chave Gemini do próprio
            # usuário -- achado real: usuário via um 503 do Gemini no log
            # do Railway mas a mensagem em tela dizia "plano gratuito"
            # (linguagem de limite pessoal), porque quem realmente
            # encerrou a cadeia foi um 429 do Groq. Sem esse prefixo o
            # front não tem como saber de qual provedor veio o erro que
            # efetivamente chegou até aqui -- ver _msgErroIA no index.html.
            ultimo_erro = f"groq_{erro}"
    return None, ultimo_erro


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


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")


def _prioridade_arquivo(a: dict) -> int:
    """Prioriza retificação/errata/aditamento -- achado real (edital 127082):
    quando existe uma retificação alterando data/condições, o edital
    original sozinho já enche o limite de caracteres do prompt (MAX_TOTAL)
    antes de `analisar()` sequer chegar a baixar a retificação -- a IA lia
    só o texto desatualizado e devolvia a data errada. Colocando a
    retificação primeiro, ela sempre entra no texto combinado (ver
    `analisar()`: a lista final ainda é truncada em MAX_TOTAL, mas agora o
    que sobra de fora é o final do edital original, não a correção).

    _sem_acento: achado real (edital 141844): o título vinha exatamente
    "TERMO DE REFERENCIA COM APROVACAO" (sem acento no "ê") -- a comparação
    direta com "termo de referência" (acentuado) nunca batia, então esse
    documento (onde fica a habilitação/itens) caía no mesmo grupo genérico
    (prioridade 3) que um aviso administrativo qualquer, perdendo a vaga.

    Checa "tipo" (tipoDocumentoNome, classificação estruturada do próprio
    PNCP -- mais confiável que adivinhar pelo nome do arquivo) além do
    título -- achado real (edital 138442, processo de Contratação Direta
    sem nenhum "Termo de Referência"/"Edital" entre os documentos): Mapa de
    Riscos, DFD (Documento de Formalização da Demanda) e o despacho que
    autoriza a contratação são puramente administrativos/processuais --
    nunca trazem item ou exigência de habilitação, mas empatavam em
    prioridade (3) com o próprio Aviso de Contratação Direta (que, nesse
    tipo de processo sem edital separado, é onde a habilitação de fato
    aparece) só por serem mais curtos e vierem antes na lista do PNCP.
    Prioridade 4 -- pior que o genérico (3) -- pra sempre perder a vaga
    pra qualquer coisa que não seja claramente descartável."""
    t = _sem_acento((a.get("titulo") or "").lower())
    tipo = _sem_acento((a.get("tipo") or "").lower())
    if "retificac" in t or "errata" in t or "aditamento" in t or "adendo" in t:
        return 0
    if "edital" in t:
        return 1
    if "termo de referencia" in t or "anexo" in t:
        return 2
    if ("mapa de risco" in tipo or "formalizacao da demanda" in tipo
            or "autoriza a contratacao direta" in tipo or "despacho" in tipo):
        return 4
    return 3


# Achado real (edital 125821): quando o texto do edital estoura o teto de
# tamanho de requisição do Gemini (HTTP 413 -- limite exato não é
# documentado publicamente, e nem sempre é sobre tamanho -- ver comentário
# de MAX_TOTAL sobre o 413 persistindo mesmo numa retentativa deste mesmo
# valor), a retentativa dentro de analisar() trunca pra bem menos que
# MAX_TOTAL (80000) e tenta mais uma vez, em vez de desistir de vez --
# defesa extra pro caso raro de um documento único, sem zip/múltiplos
# arquivos, sozinho já passar de 40000 chars. Módulo-level (não dentro de
# analisar()) porque o 413 pode acontecer também no caminho de
# texto_pronto (cache), que pula o bloco onde MAX_TOTAL é calculado.
_MAX_TOTAL_SEGURO_413 = 40000
# Módulo-level (mesmo motivo do comentário acima): a checagem de cobertura
# perto do fim de analisar() precisa comparar len(texto) contra este valor
# pra detectar truncamento mesmo quando texto veio de texto_pronto (cache),
# que nunca passa pelo bloco onde essa constante era definida localmente.
MAX_TOTAL = 80000


def analisar(objeto: str, arquivos: list[dict], api_key: str | None = None,
            texto_pronto: dict | None = None, total_itens: int | None = None) -> dict:
    """arquivos: lista de {titulo, tipo, url} (do endpoint de documentos).
    api_key: chave Gemini do próprio usuário (obrigatória, cai para a global).

    texto_pronto: opcional, {"texto": str, "fonte": str|None} já extraído
    numa chamada anterior (ver _texto_pronto_cache em main.py) -- pula o
    download/extração de PDF inteiramente quando fornecido. Achado real:
    reabrir a aba ou tentar de novo (depois de uma falha na chamada de IA,
    não na busca) baixava o PDF de novo à toa. Quando None (padrão), extrai
    normalmente e devolve o texto usado em resultado["_texto_extraido"]/
    ["_fonte_extraida"] -- só quando a EXTRAÇÃO deu certo, mesmo que a
    chamada de IA em si falhe depois -- pra quem chama poder cachear.

    total_itens: opcional, Nº real de itens do edital (ItemEdital, dado
    estruturado do PNCP -- independe de quanto texto a IA conseguiu ler).
    Achado real (edital 143879, 223 itens, Termo de Referência de ~98000
    caracteres sozinho já estourando MAX_TOTAL=80000): a análise voltou
    "analise_incompleta": false, mas os "lotes" só cobriam os itens 1-141
    -- a IA não sinalizou o corte porque o texto que ELA recebeu já tinha
    sido truncado ANTES de chegar até ela (ver MAX_TOTAL logo abaixo); pra
    ela, o texto simplesmente "acabava" num ponto que parecia normal, sem
    nada de óbvio indicando que faltava mais. Autorrelato de truncamento
    não é confiável pra esse caso -- comparar a cobertura real dos lotes
    contra o Nº de itens que a gente JÁ SABE que o edital tem (não depende
    de IA nenhuma) é."""
    if not ia_texto_disponivel(api_key):
        return {"status": "sem_ia"}

    if texto_pronto is not None:
        texto, fonte = texto_pronto["texto"], texto_pronto.get("fonte")
        # texto_pronto vem de uma extração anterior já cortada em MAX_TOTAL
        # (ver `_texto_extraido` no retorno desta função) -- não temos aqui o
        # tamanho ANTES do corte, então usamos len(texto)>=MAX_TOTAL como
        # aproximação de "foi cortado" (mesmo raciocínio já usado na
        # retentativa de 413 logo abaixo).
        texto_truncado = len(texto) >= MAX_TOTAL
    else:
        if not arquivos:
            return {"status": "sem_arquivo"}

        # prioriza retificação/errata, depois o edital principal, depois termo
        # de referência/anexos (onde costumam estar as exigências de
        # habilitação e a garantia contratual) -- ver _prioridade_arquivo
        candidatos = sorted(arquivos, key=_prioridade_arquivo)

        # baixa e combina até 2 documentos (ex.: edital + termo de
        # referência), respeitando o limite total de caracteres do prompt.
        # Achado real: editais grandes (ex.: 350 itens) enchiam o limite
        # antigo (24000, ~6 mil tokens) ainda no meio da tabela de itens,
        # cortando fora a seção de habilitação/garantia que costuma vir
        # depois -- a IA sinalizava "analise_incompleta" com frequência. O
        # Gemini (gemini-3.6/3.5-flash) tem contexto de sobra pra um texto
        # bem maior; o teto pequeno era mais conservador do que precisava.
        # A Groq (fallback, teto de corpo bem menor) já trunca de novo por
        # conta própria em _GROQ_LIMITE_PROMPT_BYTES -- não depende deste
        # valor. max_paginas também sobe (senão o PDF para de ser lido bem
        # antes de bater esse teto de caracteres).
        #
        # Chegou a subir pra 200000 (achado real, edital 125821, PNCP
        # 88830609000139/2026/371, catálogo de 90+ itens estourando o teto
        # de 80000 no meio da tabela do Anexo I) -- revertido de volta
        # depois que 200000 reproduziu HTTP 413 do Gemini DE VERDADE em
        # produção, de forma persistente (não só na 1ª tentativa: refiz a
        # análise várias vezes, com cooldown real entre elas, e continuou
        # 413 mesmo na retentativa truncada pro próprio valor antigo de
        # 80000 -- ver _MAX_TOTAL_SEGURO_413 logo abaixo, que cobre esse
        # caso residual). Não achei o limite exato/documentado que o
        # Gemini aceita nesse campo -- 80000 é o último valor confirmado
        # estável em produção; mirar um número maior de novo arriscaria
        # reproduzir o mesmo problema pra qualquer edital grande, não só
        # este. Fica como está até haver um jeito confiável de confirmar
        # um teto maior (ex.: erro mais específico do Gemini, ou suporte
        # oficial confirmando o limite real).
        partes, fontes = [], []
        falhou_download = False
        for a in candidatos[:5]:
            # achado real (edital 138442, Contratação Direta com 6
            # documentos administrativos curtos empatados em prioridade --
            # ver _prioridade_arquivo): o teto de "só 2 documentos" (não de
            # orçamento) parava o loop cedo demais, depois de só juntar os 2
            # primeiros por ORDEM da lista do PNCP (raramente por
            # relevância) -- mesmo sobrando dezenas de milhares de
            # caracteres de orçamento, o documento de verdade com a
            # habilitação (4º ou 5º na lista) nunca chegava a ser baixado.
            # Só para quando o ORÇAMENTO acaba, não por contar documentos.
            if sum(len(p) for p in partes) >= MAX_TOTAL:
                break
            if not a.get("url"):
                continue
            t, falhou = _baixar_texto_pdf(a["url"], max_paginas=150, max_chars=MAX_TOTAL, marcar_paginas=True)
            if falhou:
                falhou_download = True
                continue
            if len(t) > 300:
                # achado real (edital 141844, PNCP 46384111000140/2026/1036):
                # um "Aviso de Contratação Direta" sozinho já batia (e
                # estourava) MAX_TOTAL -- o loop parava aqui, sem sobrar
                # ESPAÇO NENHUM pro Termo de Referência (2º candidato, onde
                # ficam item/habilitação), mesmo esse sendo bem menor que o
                # orçamento total. Se o 1º documento sozinho já consome
                # (quase) tudo e existe outro candidato esperando a vez,
                # reserva metade do orçamento pra ele -- só se aplica ao 1º
                # documento (fontes ainda vazio), então um edital de
                # arquivo único continua usando o orçamento inteiro, sem
                # perder texto à toa.
                if not fontes and len(t) >= MAX_TOTAL - 5000 and len(candidatos) > 1:
                    t = t[:MAX_TOTAL // 2]
                titulo = a.get("titulo") or "documento"
                partes.append(f"=== DOCUMENTO: {titulo} ===\n{t}")
                fontes.append(titulo)
        texto_bruto = "\n\n---\n\n".join(partes)
        texto_truncado = len(texto_bruto) > MAX_TOTAL
        texto = texto_bruto[:MAX_TOTAL]
        fonte = ", ".join(fontes) if fontes else None
        if len(texto) < 300:
            # não confunde "não consegui baixar o arquivo" (rede/PNCP
            # instável) com "baixei e realmente não tem texto legível"
            # (scan) -- ver docstring de _baixar_texto_pdf.
            if falhou_download:
                return {"status": "erro_download_pdf"}
            return {"status": "sem_texto"}  # PDF escaneado/imagem ou não extraível

    # max_output_tokens dobrado em relação ao padrão de _gerar (16384): esta
    # é a única chamada cujo JSON de resposta cresce com o Nº DE ITENS do
    # edital (o "lotes" novo, ver _gerar) além de tudo mais que o prompt já
    # pede -- e, diferente de comparar_catalogo_usuario() (que já lida com
    # isso dividindo em lotes de 25 itens por chamada), analisar() é uma
    # chamada única: um corte aqui derruba a análise inteira, não só uma
    # parte dela.
    txt, st = _gerar(_PROMPT.format(objeto=(objeto or "")[:1000], texto=texto), api_key=api_key,
                     response_schema=_RESPONSE_SCHEMA, max_output_tokens=32768)
    if st.startswith("http_413"):   # startswith: st pode vir com o corpo da resposta colado
        # Achado real (edital 125821): MAX_TOTAL=200000 reproduziu HTTP 413
        # de verdade no Gemini mesmo já sem a inflação de ensure_ascii (ver
        # _chamar_modelo) -- o teto exato que o Gemini aceita pra esse
        # campo não é documentado publicamente, então em vez de mirar um
        # número exato (arriscando cair de novo), reagimos ao 413 truncando
        # pro último valor que já rodou de verdade em produção sem erro
        # (_MAX_TOTAL_SEGURO_413 = o antigo MAX_TOTAL de 80000) e tentando
        # uma vez mais -- graceful degradation (edital gigante ainda gera
        # uma análise parcial, sinalizada por analise_incompleta, em vez de
        # falhar por completo) em vez de travar a análise inteira.
        texto = texto[:_MAX_TOTAL_SEGURO_413]
        txt, st = _gerar(_PROMPT.format(objeto=(objeto or "")[:1000], texto=texto), api_key=api_key,
                         response_schema=_RESPONSE_SCHEMA, max_output_tokens=32768)
    if st != "ok" or not txt:
        return {"status": "erro_ia", "detalhe": st, "_texto_extraido": texto, "_fonte_extraida": fonte}
    data = _parse_json(txt)
    if not isinstance(data, dict):
        log.warning("analisar(): resposta da IA não é um JSON válido (%d chars). Início: %r Fim: %r",
                   len(txt), txt[:300], txt[-300:])
        return {"status": "resposta_invalida", "_texto_extraido": texto, "_fonte_extraida": fonte}

    # normaliza saída
    def lista(x):
        return [str(i) for i in x] if isinstance(x, list) else ([str(x)] if x else [])

    # nome curto pra não sombrear a variável `txt` (resposta crua do Gemini) do
    # escopo de fora — ela já foi consumida acima, mas mantém o código claro.
    def s(x):
        return str(x or "")

    # achado real (auditoria do prompt-engineer): bool(x) em Python trata
    # QUALQUER string não-vazia como True -- se a IA (ou uma resposta sem o
    # response_schema aplicado, ex.: mock de teste) mandar a STRING "false"
    # em vez do boolean, bool("false") vira True e inverte o sentido do
    # campo silenciosamente. Trata string explicitamente antes de cair no
    # bool() padrão.
    def b(x):
        if isinstance(x, str):
            return x.strip().lower() not in ("", "false", "não", "nao", "0")
        return bool(x)

    # declarações não são "certidão com validade" — o edital ou fornece um
    # modelo pronto (a empresa só preenche/assina) ou exige que a empresa
    # redija o próprio texto; nenhum dos dois casos tem o que "cadastrar"
    # como documento reutilizável (ver checklist_habilitacao.py). Por isso,
    # ao contrário das outras 4 categorias, cada declaração vem com esse
    # veredito da IA em vez de ser só um texto solto.
    def declaracoes(x):
        if not isinstance(x, list):
            return []
        out = []
        for item in x:
            if isinstance(item, dict) and item.get("nome"):
                mo = item.get("modelo_orgao")
                out.append({
                    "nome": str(item["nome"]),
                    "modelo_orgao": mo if isinstance(mo, bool) else None,
                    "detalhe": s(item.get("detalhe")),
                })
            elif isinstance(item, str) and item.strip():
                # tolerância: se a IA ainda mandar string simples (formato
                # antigo), entra sem veredito em vez de descartar a declaração.
                out.append({"nome": item, "modelo_orgao": None, "detalhe": ""})
        return out

    # cada lote traz os números de item que o compõem — sem isso não dá pra
    # cruzar depois com o catálogo do usuário e saber quais lotes ele cobre
    # por inteiro (ver _anexar_cobertura_lotes em main.py). "itens" que a IA
    # mandar não numérico é descartado em vez de quebrar o lote inteiro --
    # achado real (auditoria do agente debugger): int(n) sozinho ACEITA e
    # TRUNCA silenciosamente um float (int(4.5) == 4, colidindo com um item
    # 4 legítimo do mesmo lote) e aceita bool como inteiro (int(True) == 1,
    # já que bool é subclasse de int em Python) -- os dois casos corrompem
    # o lote em vez de descartar o valor ruim. Só aceita int de verdade (e
    # bool explicitamente NÃO conta, apesar de ser subclasse de int). O
    # response_schema força tipo no Gemini, mas o fallback Groq (ver
    # _chamar_groq) NÃO usa response_schema -- é JSON livre por prosa, onde
    # esse tipo de desvio pode acontecer de verdade. Deduplica preservando a
    # ordem pra uma eventual colisão não aparecer repetida na tela.
    def lotes(x):
        if not isinstance(x, list):
            return []
        out = []
        numeros_vistos = set()
        for item in x:
            if not isinstance(item, dict) or not item.get("numero"):
                continue
            numero = s(item.get("numero"))
            if numero in numeros_vistos:   # lote duplicado -- mantém só a 1ª ocorrência
                continue
            numeros_vistos.add(numero)
            itens_norm = []
            itens_vistos = set()
            for n in (item.get("itens") or []):
                # numeração de item de edital começa em 1 -- 0/negativo é
                # sempre lixo (nunca vai bater com nenhum item real, e
                # deixaria o lote "sem cobertura" pra sempre por um motivo
                # que não tem a ver com o catálogo do usuário).
                if isinstance(n, bool) or not isinstance(n, int) or n <= 0 or n in itens_vistos:
                    continue
                itens_vistos.add(n)
                itens_norm.append(n)
            out.append({
                "numero": numero,
                "itens": itens_norm,
                "descricao": s(item.get("descricao")),
            })
        # achado real (edital 141995, PNCP 01641472000196/2026/17): a IA às
        # vezes reinicia a contagem de item EM CADA lote (1, 2, 3...) em vez
        # de usar a numeração GLOBAL do edital que o prompt pede -- o mesmo
        # número aparecia em lotes DIFERENTES ao mesmo tempo (ex.: item 1
        # listado em 3 lotes), embora lotes por definição particionem os
        # itens (cada item pertence a exatamente um lote, nunca a dois).
        # Detecta essa inconsistência -- mais confiável que tentar adivinhar
        # qual lote está certo -- e descarta TODOS os lotes, mesma semântica
        # de "não conseguiu identificar com segurança" que o campo já
        # documenta, em vez de mostrar uma composição sabidamente errada
        # (o usuário via "Papel Sulfite" agrupado no lote errado, com um
        # aviso falso de que o item 1 -- na real "Bloco Autoadesivo" --
        # estava faltando nesse lote).
        contagem_por_item: dict[int, int] = {}
        for lote in out:
            for n in lote["itens"]:
                contagem_por_item[n] = contagem_por_item.get(n, 0) + 1
        if any(c > 1 for c in contagem_por_item.values()):
            return []
        return out

    def documentos_habilitacao(x):
        x = x if isinstance(x, dict) else {}
        return {
            "juridica": lista(x.get("juridica")),
            "fiscal_trabalhista": lista(x.get("fiscal_trabalhista")),
            "tecnica": lista(x.get("tecnica")),
            "economico_financeira": lista(x.get("economico_financeira")),
            "declaracoes": declaracoes(x.get("declaracoes")),
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
            "exclusivo_regional": b(x.get("exclusivo_regional")),
            "regiao_exclusiva": s(x.get("regiao_exclusiva")),
        }

    def dados_proposta(x):
        x = x if isinstance(x, dict) else {}
        return {
            "validade_dias": s(x.get("validade_dias")),
            "prazo_entrega": s(x.get("prazo_entrega")),
            "local_entrega": s(x.get("local_entrega")),
            "condicoes_pagamento": s(x.get("condicoes_pagamento")),
            "aceita_similar": b(x.get("aceita_similar")),
            "forma_apresentacao": s(x.get("forma_apresentacao")),
            "garantia_proposta": s(x.get("garantia_proposta")),
            "identificacao_marca_modelo": b(x.get("identificacao_marca_modelo")),
            "prospecto_catalogo": s(x.get("prospecto_catalogo")),
            "entrega_tecnica": b(x.get("entrega_tecnica")),
            "assistencia_tecnica": b(x.get("assistencia_tecnica")),
            "garantia_produto": s(x.get("garantia_produto")),
        }

    lotes_normalizados = lotes(data.get("lotes"))
    incompleta = b(data.get("analise_incompleta"))
    aviso_cobertura = None
    # cobertura real dos lotes x Nº de itens que o edital de fato tem (ver
    # docstring de `total_itens` acima) -- só aplica quando há lotes pra
    # medir (julgamento "item" não tem esse sinal disponível) e o dado
    # estruturado do PNCP está disponível.
    if lotes_normalizados and total_itens:
        itens_cobertos = {n for l in lotes_normalizados for n in l["itens"]}
        # folga pequena (2 itens): alguns editais têm item(ns) fora da
        # numeração normal (ex.: um "item 0"/anexo administrativo) sem que
        # isso signifique truncamento de verdade.
        if len(itens_cobertos) < total_itens - 2:
            incompleta = True
            aviso_cobertura = ("Esta análise pode estar incompleta: nem todos os itens do "
                              "edital foram cobertos pelos lotes identificados.")
    elif texto_truncado and total_itens:
        # achado real (edital 145959, 209 itens, PDF de 212000 caracteres):
        # editais só-por-item (sem agrupamento em lotes) não têm "lotes" pra
        # medir cobertura -- o ramo acima simplesmente não roda, e o corte
        # em MAX_TOTAL passava batido (a IA nunca escreveu
        # "analise_incompleta": o texto que ELA recebeu já tinha sido
        # cortado ANTES, então pra ela parecia terminar num ponto normal --
        # mesmo raciocínio da docstring de `total_itens`). Sem "lotes" pra
        # confirmar cobertura item a item, a única informação que temos é
        # "o texto foi cortado" -- não dá pra provar que os itens depois do
        # corte foram vistos, então assume incompleto em vez de confiar no
        # autorrelato da IA.
        incompleta = True
        aviso_cobertura = ("Esta análise pode estar incompleta: o texto do edital foi cortado "
                          "antes do fim (documento grande) e este edital não usa agrupamento "
                          "em lotes pra confirmar se todos os itens foram cobertos.")
    pontos_atencao = lista(data.get("pontos_atencao"))
    if incompleta and not b(data.get("analise_incompleta")):
        # achado real (edital 143879): quando é ESTA checagem (não a IA)
        # que detecta o corte, a IA nunca escreveu o aviso que o prompt
        # pede pra esse caso (ver linha 125 do _PROMPT) -- sem isso,
        # "pontos_atencao" ficava sem nenhum sinal do problema, mesmo com
        # "analise_incompleta" virando true.
        pontos_atencao = pontos_atencao + [aviso_cobertura]
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
        "exige_amostra": b(data.get("exige_amostra")),
        "exige_visita": b(data.get("exige_visita")),
        "exclusivo_me_epp": b(data.get("exclusivo_me_epp")),
        "julgamento": s(data.get("julgamento")),
        "lotes": lotes_normalizados,
        "garantia_contratual": s(data.get("garantia_contratual")),
        "analise_incompleta": incompleta,
        "pontos_atencao": pontos_atencao,
        "_texto_extraido": texto,
        "_fonte_extraida": fonte,
    }


# Prompt pra cruzar os DOCUMENTOS QUE O USUÁRIO JÁ TEM CADASTRADOS (aba
# Documentos, cada um com o texto extraído do PDF/imagem anexado no cadastro
# — ver Documento.texto_extraido) contra o que ESTE edital exige. Reaproveita
# os campos que `analisar()` acima já extraiu (requisitos_tecnicos e
# documentos_habilitacao), em vez de reanalisar o edital do zero. Ao
# contrário do cruzamento por NOME (checklist_habilitacao.montar, sempre
# ativo, rápido/grátis), este lê o CONTEÚDO de cada documento.
_PROMPT_VERIFICACAO_DOCUMENTOS = """Você é um especialista em licitações públicas brasileiras. Abaixo estão os DOCUMENTOS DE HABILITAÇÃO exigidos por um edital e os DOCUMENTOS QUE O FORNECEDOR JÁ TEM CADASTRADOS (nome + texto extraído de cada um).

Para CADA exigência listada, verifique se algum dos documentos cadastrados comprovadamente a atende. Responda APENAS com um JSON válido (sem texto fora do JSON, sem ```), com exatamente esta estrutura:
- "itens": array, um item pra CADA exigência da lista abaixo (não pule nenhuma), cada um com:
  - "exigido": a exigência, copiada exatamente como está na lista.
  - "aplicavel": boolean. Editais costumam listar exigências ALTERNATIVAS pra tipos de empresa diferentes (ex.: um item pra "sociedade empresária ou EIRELI", outro pra "empresário individual", outro pra "sociedade simples" — só UM se aplica a cada fornecedor). false quando os documentos cadastrados já deixam claro que esta exigência é de um tipo de empresa/situação DIFERENTE da do fornecedor (ex.: exigência é de sociedade empresária, mas os documentos mostram que o fornecedor é MEI) — não é uma pendência, é uma alternativa que não se aplica. true em qualquer outro caso, inclusive quando não há informação suficiente pra saber se aplica ou não (não deduza inaplicabilidade sem uma base clara nos documentos).
  - "atendido": true se algum documento cadastrado comprova isso, false caso contrário. Ignorado (pode ser false) quando "aplicavel" for false.
  - "documento": nome do documento cadastrado que atende (o mais relevante), ou "" se nenhum atende.
  - "observacao": string curta (só quando relevante) — ex. "documento encontrado mas sem data de emissão visível", "atestado cobre item diferente do exigido". "" se não houver nada a observar.

Regras: não invente nada que não esteja nos textos. Se a lista de documentos cadastrados estiver vazia, todo item vem com atendido=false, aplicavel=true e documento="". Responda em português.

OBJETO DO EDITAL: {objeto}

DOCUMENTOS DE HABILITAÇÃO EXIGIDOS:
{requisitos}

DOCUMENTOS CADASTRADOS PELO FORNECEDOR:
{documentos}"""


def _formatar_requisitos(documentos_habilitacao: dict) -> str:
    """Só entram aqui os 4 tipos de DOCUMENTO de habilitação (jurídica,
    fiscal/trabalhista, técnica, econômico-financeira) -- pedido do
    usuário: nem "declarações" nem "requisitos_tecnicos" fazem sentido
    aqui, os dois por motivos parecidos:
    - declarações NÃO são "certidão com validade" -- mesmo raciocínio já
      aplicado no checklist por nome (checklist_habilitacao._item_declaracao):
      é texto redigido/preenchido especificamente pra ESTE edital, não um
      arquivo fixo reaproveitável. Perguntar "algum documento cadastrado já
      atende essa declaração" não faz sentido.
    - requisitos_tecnicos são especificação do PRODUTO/SERVIÇO ofertado
      (ex.: "alimento para peixes com tal composição"), não documento
      nenhum que o fornecedor precise ter cadastrado -- cruzar contra
      "documentos cadastrados" também não faz sentido aqui (isso já é
      coberto, de outro jeito, pela comparação de catálogo/item).
    Sem essa exclusão, a verificação por IA marcava os dois como "não
    atendido" (✗) mesmo quando não havia nada de errado, só porque nenhum
    arquivo cadastrado é, por definição, aquilo que a exigência pedia."""
    linhas = []
    docs = documentos_habilitacao or {}
    for categoria in ("juridica", "fiscal_trabalhista", "tecnica", "economico_financeira"):
        for d in (docs.get(categoria) or []):
            linhas.append(f"- {d}")
    return "\n".join(linhas) if linhas else "(nenhum requisito específico identificado na análise do edital)"


def _itens_declaracao(declaracoes: list | None) -> list[dict]:
    """Declarações não são documento fixo reaproveitável (ver
    _formatar_requisitos) -- não tem como cruzar por CONTEÚDO contra nada
    cadastrado, então nunca são mandadas pra IA verificar. Pedido do
    usuário (edital 136161): antes disso elas simplesmente não apareciam
    na Verificação por IA, o que fazia a lista parecer menor do que o
    checklist por nome (que lista tudo) sem explicar por quê. Entram aqui
    como itens informativos (status="declaracao"), não como pendência."""
    itens = []
    for d in (declaracoes or []):
        if not isinstance(d, dict) or not d.get("nome"):
            continue
        modelo = d.get("modelo_orgao")
        if modelo is True:
            nota = "Modelo pronto fornecido pelo órgão — só preencher e assinar."
        elif modelo is False:
            nota = "Sem modelo do órgão — redigir texto próprio."
        else:
            nota = "Declaração a preencher especificamente para este edital."
        detalhe = (d.get("detalhe") or "").strip()
        itens.append({
            "exigido": str(d.get("nome")),
            "status": "declaracao",
            "atendido": False,
            "documento": "",
            "observacao": f"{nota} {detalhe}".strip(),
        })
    return itens


def verificar_documentos_usuario(objeto: str, documentos_habilitacao: dict,
                                 documentos_usuario: list[dict], api_key: str | None = None) -> dict:
    """Cruza os documentos que o usuário já tem cadastrados (cada um com
    `nome` e `texto`, vindo de Documento.texto_extraido) contra o que este
    edital exige — verificação de CONTEÚDO, complementar ao cruzamento por
    nome que já existe (checklist_habilitacao). NÃO fica em cache: é
    específico do usuário, e ed.analise_ia é um cache compartilhado entre
    todos que veem este edital.

    Não recebe mais requisitos_tecnicos (ver _formatar_requisitos) -- só
    documentos_habilitacao é usado agora pra decidir se há o que checar."""
    if not ia_texto_disponivel(api_key):
        return {"status": "sem_ia"}
    if not documentos_usuario:
        return {"status": "sem_documentos"}

    docs = documentos_habilitacao or {}
    tem_requisito = any(docs.get(c) for c in ("juridica", "fiscal_trabalhista", "tecnica", "economico_financeira"))
    itens_declaracao = _itens_declaracao(docs.get("declaracoes"))
    if not tem_requisito:
        # nada pra verificar por conteúdo, mas pode haver declaração(ões)
        # pra mostrar mesmo assim -- não precisa de IA pra isso.
        if itens_declaracao:
            return {"status": "ok", "itens": itens_declaracao}
        return {"status": "sem_requisitos"}
    requisitos = _formatar_requisitos(docs)

    # Achado real (usuário reportou: CNDT cadastrada e válida, IA disse
    # "não atendido" -- edital 126768): o corte antigo era um limite FIXO
    # de 8 documentos (documentos_usuario[:8]), não um orçamento de
    # caracteres -- uma conta com mais de 8 documentos cadastrados
    # (comum: um catálogo de habilitação típico já tem CND federal/
    # estadual/municipal/FGTS/CNDT + Sicaf + contrato social + certidão
    # simplificada, passa de 8 fácil) simplesmente PERDIA os documentos
    # além do 8º -- a IA nunca via o texto deles, então "não atendido" pra
    # esse documento estava certo dado o que foi mandado, só que o que foi
    # mandado já tinha descartado o documento certo antes de perguntar.
    # Texto extraído real costuma ser bem menor que 3000 chars (uma
    # certidão de página única fica na casa de 1-2 mil), então o teto de
    # verdade (24000 chars, mesmo orçamento do analisar() principal) quase
    # sempre cabe bem mais que 8 documentos -- só para de incluir quando o
    # ORÇAMENTO de caracteres estoura, não numa contagem arbitrária.
    _MAX_DOCUMENTOS_CARACTERES = 24000
    partes = []
    total_chars = 0
    for d in documentos_usuario:
        nome = (d.get("nome") or "documento").strip()
        texto = (d.get("texto") or "").strip()[:3000]
        if not texto:
            continue
        bloco = f'### "{nome}"\n{texto}'
        if partes and total_chars + len(bloco) > _MAX_DOCUMENTOS_CARACTERES:
            break
        partes.append(bloco)
        total_chars += len(bloco)
    if not partes:
        return {"status": "sem_documentos"}
    documentos_txt = "\n\n".join(partes)

    prompt = _PROMPT_VERIFICACAO_DOCUMENTOS.format(
        objeto=(objeto or "")[:1000], requisitos=requisitos[:6000], documentos=documentos_txt)
    txt, st = _gerar(prompt, api_key=api_key)
    if st != "ok" or not txt:
        return {"status": "erro_ia", "detalhe": st}
    data = _parse_json(txt)
    if not isinstance(data, dict) or not isinstance(data.get("itens"), list):
        log.warning("verificar_documentos_usuario(): resposta da IA não é um JSON válido "
                   "(%d chars). Início: %r Fim: %r", len(txt), txt[:300], txt[-300:])
        return {"status": "resposta_invalida"}

    itens = []
    for it in data["itens"]:
        if not isinstance(it, dict) or not it.get("exigido"):
            continue
        # pedido do usuário: exigência alternativa que não se aplica a este
        # fornecedor (ex.: exigência de sociedade empresária quando os
        # documentos cadastrados mostram que o fornecedor é MEI) não é uma
        # pendência real -- vira status="nao_aplicavel" em vez de ✓/✗, mas
        # continua na lista (achado real, edital 136161: sumir deixava a
        # Verificação por IA com menos itens que o checklist por nome, sem
        # explicar por quê). Só marca assim quando a IA disse EXPLICITAMENTE
        # false; sem essa informação (chave ausente, formato antigo de
        # cache) trata como aplicável, igual o prompt pede.
        aplicavel = it.get("aplicavel")
        atendido = bool(it.get("atendido"))
        if aplicavel is False:
            status, atendido = "nao_aplicavel", False
        else:
            status = "atendido" if atendido else "nao_atendido"
        itens.append({
            "exigido": str(it.get("exigido")),
            "status": status,
            "atendido": atendido,
            "documento": str(it.get("documento") or ""),
            "observacao": str(it.get("observacao") or ""),
        })
    itens.extend(itens_declaracao)
    return {"status": "ok", "itens": itens}


# Prompt pra comparar o CATÁLOGO COMPLETO do usuário contra os itens deste
# edital — segunda opinião da IA, independente do motor de matching por
# texto (matching/engine.py, TF-IDF + palavra-chave). Só inclui um item
# quando a IA está confiante que achou um produto de verdade compatível,
# não uma categoria parecida.
_PROMPT_COMPARAR_CATALOGO = """Você é um especialista em compras públicas. Abaixo estão os ITENS que um edital de licitação está pedindo, e o CATÁLOGO de produtos que um fornecedor tem disponível (cada um com um ID).

Para cada item do edital, verifique se ALGUM produto do catálogo é realmente compatível (mesmo tipo de produto/serviço, atende as características principais pedidas — não é só uma categoria parecida). Responda APENAS com um JSON válido (sem texto fora do JSON, sem ```), com exatamente esta estrutura:
- "itens": array — só inclua um item aqui quando encontrar pelo menos 1 produto do catálogo genuinamente compatível. Cada entrada:
  - "numero_item": o número do item do edital (copiado exatamente, é um número).
  - "candidatos": array com 1 ou 2 produtos do catálogo compatíveis com este item, do melhor pro segundo melhor. SÓ inclua um segundo candidato quando ele TAMBÉM for genuinamente compatível (mesmo critério do primeiro) — nunca complete artificialmente pra ter 2. Cada candidato:
    - "produto_id": o ID do produto do catálogo (é um número).
    - "justificativa": string curta (1 frase) explicando por que esse produto atende.

Regras: não invente produto_id que não esteja na lista do catálogo abaixo. Não force compatibilidade só porque a categoria é parecida (ex.: "papel sulfite" não é o mesmo produto que "papel fotográfico"; "álcool em gel" não é o mesmo que "álcool líquido") — se nenhum produto realmente atender, simplesmente não inclua esse item no array. Responda em português.

OBJETO DO EDITAL: {objeto}

ITENS DO EDITAL:
{itens}

CATÁLOGO DO FORNECEDOR (ID: descrição):
{catalogo}"""


def _formatar_itens_edital(itens: list[dict]) -> str:
    linhas = [f"- item {it.get('numero')}: {it.get('descricao') or ''}" for it in itens]
    return "\n".join(linhas) if linhas else "(nenhum item)"


def _formatar_catalogo(catalogo: list[dict], max_produtos: int = 2000) -> str:
    linhas = [f"- ID {p.get('id')}: {p.get('descricao') or ''}" for p in catalogo[:max_produtos]]
    return "\n".join(linhas) if linhas else "(catálogo vazio)"


# Achado real: edital com 57 itens já cortava a resposta da IA (maxOutputTokens
# ajudou, mas não escala — tem edital com mais de 150 itens). Em vez de mandar
# TODOS os itens numa chamada só (uma resposta gigante, tudo ou nada), quebra
# em lotes pequenos — cada chamada fica curta e previsível, e se UM lote
# falhar (resposta_invalida/erro_ia), os outros continuam valendo em vez de
# perder a comparação inteira.
_TAMANHO_LOTE_COMPARACAO = 25


def _comparar_lote_catalogo(objeto: str, itens_lote: list[dict], catalogo: list[dict],
                            catalogo_txt: str, api_key: str | None) -> dict:
    itens_txt = _formatar_itens_edital(itens_lote)[:30000]
    prompt = _PROMPT_COMPARAR_CATALOGO.format(
        objeto=(objeto or "")[:1000], itens=itens_txt, catalogo=catalogo_txt)
    # permitir_groq=False: ver docstring de _gerar() -- o catálogo inteiro vai
    # dentro deste prompt, então o corte cego de 20000 bytes do Groq tira
    # candidatos de vista (não só contexto de apoio) e ainda assim volta
    # "sucesso" pra todos os itens, com pares errados -- pior que reportar
    # este lote como falha (já tratado normalmente por quem chama).
    txt, st = _gerar(prompt, api_key=api_key, timeout=90, permitir_groq=False)
    if st != "ok" or not txt:
        return {"status": "erro_ia", "detalhe": st}
    data = _parse_json(txt)
    if not isinstance(data, dict) or not isinstance(data.get("itens"), list):
        log.warning("comparar_catalogo_usuario(): resposta da IA não é um JSON válido "
                   "(lote com %d itens do edital, %d produtos, %d chars de resposta). Início: %r Fim: %r",
                   len(itens_lote), len(catalogo), len(txt), txt[:300], txt[-300:])
        return {"status": "resposta_invalida"}

    ids_validos = {p.get("id") for p in catalogo}
    itens = []
    for it in data["itens"]:
        if not isinstance(it, dict):
            continue
        try:
            numero = int(it.get("numero_item"))
        except (TypeError, ValueError):
            continue
        candidatos_brutos = it.get("candidatos")
        if not isinstance(candidatos_brutos, list):
            continue
        candidatos = []
        vistos: set[int] = set()
        for c in candidatos_brutos:
            if not isinstance(c, dict):
                continue
            try:
                produto_id = int(c.get("produto_id"))
            except (TypeError, ValueError):
                continue
            if produto_id not in ids_validos or produto_id in vistos:
                continue
            vistos.add(produto_id)
            candidatos.append({"produto_id": produto_id,
                              "justificativa": str(c.get("justificativa") or "")})
            if len(candidatos) == 2:
                break
        if not candidatos:
            continue
        itens.append({"numero": numero, "candidatos": candidatos})
    return {"status": "ok", "itens": itens}


def comparar_catalogo_usuario(objeto: str, itens_edital: list[dict], catalogo: list[dict],
                              api_key: str | None = None, deve_cancelar=None) -> dict:
    """Manda o CATÁLOGO COMPLETO do usuário (id + descrição) e os itens deste
    edital pra IA comparar diretamente — segunda opinião independente do
    motor de matching por texto. NÃO fica em cache: é específico do
    catálogo do usuário no momento da chamada, não do edital em si.

    Itens processados em lotes de _TAMANHO_LOTE_COMPARACAO (ver comentário
    acima) — pra edital pequeno (≤ 1 lote) o comportamento é idêntico a uma
    chamada só, igual antes. `deve_cancelar` (opcional) é checado ENTRE
    lotes, nunca no meio de uma chamada já em voo — mesmo espírito do
    cancelamento cooperativo já usado entre as etapas da análise."""
    if not ia_texto_disponivel(api_key):
        return {"status": "sem_ia"}
    if not itens_edital:
        return {"status": "sem_itens"}
    if not catalogo:
        return {"status": "sem_catalogo"}

    # Teto generoso de propósito: o contexto de entrada do Gemini aguenta
    # muito mais que isso (na casa do milhão de tokens) — calculado 1x fora
    # do loop, o catálogo é o mesmo pra todos os lotes. 300000 chars cobre
    # os 2000 produtos de _formatar_catalogo mesmo com descrições bem acima
    # da média (~47 chars/produto num catálogo real de 852 itens) — sem
    # esse teto acompanhar o de lá, um catálogo grande cortava no meio da
    # lista de produtos de qualquer forma, só que em texto em vez de contagem.
    catalogo_txt = _formatar_catalogo(catalogo)[:300000]
    lotes = [itens_edital[i:i + _TAMANHO_LOTE_COMPARACAO]
             for i in range(0, len(itens_edital), _TAMANHO_LOTE_COMPARACAO)]

    itens_ok: list[dict] = []
    lotes_com_falha = 0
    ultimo_erro: dict | None = None
    for lote in lotes:
        if deve_cancelar and deve_cancelar():
            break
        r = _comparar_lote_catalogo(objeto, lote, catalogo, catalogo_txt, api_key)
        if r["status"] == "ok":
            itens_ok.extend(r["itens"])
        else:
            lotes_com_falha += 1
            ultimo_erro = r

    # nenhum lote deu certo: propaga o erro do último (mesmo comportamento
    # de hoje pra edital pequeno, que só tem 1 lote — tudo ou nada continua
    # valendo só quando literalmente TUDO falhou).
    if not itens_ok and lotes_com_falha:
        return ultimo_erro

    resultado = {"status": "ok", "itens": itens_ok}
    if lotes_com_falha:
        resultado["lotes_com_falha"] = lotes_com_falha
    return resultado


_PROMPT_RERANK = """Você está avaliando se produtos de um catálogo são o MESMO item físico pedido num edital de licitação.

Item do edital: "{item}"

Catálogo (um produto por linha, numerado):
{catalogo}

Para CADA produto do catálogo, na mesma ordem, dê um score de 0.0 a 1.0 indicando o quanto ele é o MESMO produto físico do item acima — mesmo tipo de objeto e uso prático, não apenas categoria parecida. Responda APENAS com um JSON no formato {{"scores": [n1, n2, ...]}}, com exatamente {n} números, um por produto, na mesma ordem da lista."""


def rerank_gemini(texto_item: str, documentos: list[str], api_key: str | None = None,
                  timeout: int = 60, tentativas: int = 2) -> list[float] | None:
    """Alternativa ao reranker da DeepInfra (ver matching/embeddings.rerank):
    usa o Gemini (chave própria do usuário) pra pontuar cada produto do
    catálogo contra um item de edital. Mesma assinatura/contrato de retorno
    (lista de scores na mesma ordem de `documentos`, ou None em qualquer
    falha) — quem chama trata igual, sem saber qual provedor respondeu.

    EXPERIMENTAL: é 1 chamada ao Gemini POR ITEM. Num recálculo completo
    (milhares de editais) isso estoura de longe os limites de taxa do
    Gemini — viável só pra testar em poucos editais por vez, não pra rodar
    a base inteira (ver seletor em main.py, restrito a uma conta)."""
    if not ia_texto_disponivel(api_key) or not documentos:
        return None
    catalogo_txt = "\n".join(f"{i+1}. {d}" for i, d in enumerate(documentos))
    prompt = _PROMPT_RERANK.format(item=(texto_item or "")[:500],
                                   catalogo=catalogo_txt[:20000], n=len(documentos))
    txt, st = _gerar(prompt, api_key=api_key, timeout=timeout, tentativas=tentativas)
    if st != "ok" or not txt:
        return None
    data = _parse_json(txt)
    if not isinstance(data, dict) or not isinstance(data.get("scores"), list):
        return None
    scores = data["scores"]
    if len(scores) != len(documentos):
        return None
    try:
        return [max(0.0, min(1.0, float(s))) for s in scores]
    except (TypeError, ValueError):
        return None
