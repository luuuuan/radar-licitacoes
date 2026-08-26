"""
Conector do PNCP — Portal Nacional de Contratações Públicas (Lei 14.133/2021).

A API de CONSULTAS é pública (sem autenticação). Sob a nova lei, órgãos
federais, estaduais e municipais são obrigados a publicar suas contratações
no PNCP, então este único conector já cobre a MAIOR PARTE das fontes pedidas.

Endpoint usado (editais com recebimento de propostas EM ABERTO):
  GET {BASE}/v1/contratacoes/proposta
  Parâmetros: dataFinal (yyyyMMdd), codigoModalidadeContratacao, pagina,
              tamanhoPagina, uf (opcional)

Documentação oficial (Swagger):
  https://pncp.gov.br/api/consulta/swagger-ui/index.html

Tabelas de domínio (modalidade): 4=Concorrência Eletrônica, 6=Pregão
Eletrônico, 8=Dispensa, 9=Inexigibilidade, etc.
"""
from __future__ import annotations
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta, datetime

import requests

from .base import BaseConnector, EditalColetado, ItemColetado
from ..config import settings, parse_csv_ints, parse_csv_str

log = logging.getLogger("conector.pncp")

MODALIDADE_NOME = {
    1: "Leilão Eletrônico", 2: "Diálogo Competitivo", 3: "Concurso",
    4: "Concorrência Eletrônica", 5: "Concorrência Presencial",
    6: "Pregão Eletrônico", 7: "Pregão Presencial", 8: "Dispensa",
    9: "Inexigibilidade", 10: "Manifestação de Interesse",
    11: "Pré-qualificação", 12: "Credenciamento", 13: "Leilão Presencial",
}


def _parse_data(valor: str | None) -> date | None:
    if not valor:
        return None
    # Achado real (auditoria do agente debugger): o `if/else` dentro do
    # `[:...]` tinha um bug de precedência de operador -- `A if C else B`
    # é mais "solto" que `+`, então o slice virava
    # `valor[:(len(fmt)+6) if "%f" in fmt else len(valor)]`. Pros formatos
    # SEM %f, o "else" sempre disparava e fatiava pro tamanho do próprio
    # valor (ou seja, sem cortar nada) -- só funcionava quando o valor já
    # vinha com o tamanho exato do formato, sem sufixo de fuso ou frações
    # de segundo. Na prática mascarado pelo fallback (fromisoformat) logo
    # abaixo, que pega a maioria dos formatos reais do PNCP -- mas a
    # intenção do loop (cada formato com seu corte certo) nunca rodava de
    # verdade. Comprimentos explícitos, sem fórmula, pra não repetir o erro.
    for fmt, comprimento in (
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d", 10),
        ("%Y-%m-%dT%H:%M:%S.%f", 26),
    ):
        try:
            return datetime.strptime(valor[:comprimento], fmt).date()
        except (ValueError, TypeError):
            continue
    try:
        return datetime.fromisoformat(valor.replace("Z", "")).date()
    except (ValueError, TypeError):
        return None


class PNCPConnector(BaseConnector):
    nome = "PNCP"

    def __init__(self, session: requests.Session | None = None,
                 ufs: str | None = None, modalidades: str | None = None,
                 horizonte: int | None = None):
        self.base = settings.PNCP_BASE_URL.rstrip("/")
        self.itens_base = settings.PNCP_ITENS_BASE_URL.rstrip("/")
        self.modalidades = parse_csv_ints(modalidades if modalidades is not None else settings.PNCP_MODALIDADES)
        self.ufs = parse_csv_str(ufs if ufs is not None else settings.PNCP_UFS)  # [] = todas
        self.horizonte = horizonte if horizonte is not None else settings.PNCP_HORIZONTE_DIAS
        self.tam_pagina = settings.PNCP_TAMANHO_PAGINA
        self.delay = settings.PNCP_DELAY
        self.tentativas = max(1, settings.PNCP_TENTATIVAS)
        self.http = session or requests.Session()
        self.http.headers.update({"Accept": "application/json",
                                  "User-Agent": "RadarLicitacoes/1.0"})
        self._falhas_seguidas = 0
        self._abortado = False

    def _get_com_retry(self, url: str, params: dict, timeout: int):
        """GET com re-tentativas em falhas transitórias (timeout, 5xx, 429).
        Para 429, respeita o tempo pedido pelo servidor (Retry-After) e espera
        mais entre tentativas. Retorna a resposta ou None."""
        ultimo_erro = None
        for tentativa in range(1, self.tentativas + 1):
            try:
                resp = self.http.get(url, params=params, timeout=timeout)
                if resp.status_code in (500, 502, 503, 504, 429):
                    ultimo_erro = f"HTTP {resp.status_code}"
                    if resp.status_code == 429 and tentativa < self.tentativas:
                        # respeita o tempo pedido pelo PNCP, ou espera progressiva (mais longa)
                        espera = self._retry_after(resp) or min(30, 2 * (2 ** tentativa))
                        time.sleep(espera)
                        continue
                    raise requests.RequestException(ultimo_erro)
                return resp
            except requests.RequestException as e:
                ultimo_erro = str(e)
                if tentativa < self.tentativas:
                    time.sleep(self.delay * (2 ** tentativa))  # backoff: 0.6s, 1.2s...
        log.warning("Desisti após %d tentativa(s): %s", self.tentativas, ultimo_erro)
        return None

    @staticmethod
    def _retry_after(resp) -> float | None:
        """Lê o header Retry-After (segundos) que o servidor manda no 429."""
        v = resp.headers.get("Retry-After")
        if not v:
            return None
        try:
            return min(60.0, float(v))   # nunca espera mais que 60s por tentativa
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    def coletar(self) -> list[EditalColetado]:
        data_final = (date.today() + timedelta(days=self.horizonte)).strftime("%Y%m%d")
        alvos_uf = self.ufs or [None]  # None => não filtra por UF
        editais: dict[str, EditalColetado] = {}
        self._falhas_seguidas = 0
        self._abortado = False

        for modalidade in self.modalidades:
            if self._abortado:
                break
            for uf in alvos_uf:
                if self._abortado:
                    break
                self._coletar_modalidade_uf(modalidade, uf, data_final, editais)

        if self._abortado:
            log.warning("PNCP recusou muitas vezes seguidas (429/erro). Coleta "
                        "interrompida — os editais já obtidos foram mantidos. "
                        "Tente novamente em alguns minutos.")
        lista = list(editais.values())
        self._coletar_itens_paralelo(lista)
        log.info("PNCP: %d editais coletados%s", len(lista),
                 " (parcial)" if self._abortado else "")
        return lista

    def _coletar_itens_paralelo(self, editais: list[EditalColetado]) -> None:
        """Busca os itens de todos os editais em paralelo (muito mais rápido
        que serial). max_workers moderado para não sobrecarregar o portal."""
        alvos = [e for e in editais if e.raw and e.raw.get("_ref_itens")]
        if alvos:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(self._preencher_itens, alvos))
        # não persistimos nada do raw (evita inflar o banco)
        for e in editais:
            e.raw = None

    def _preencher_itens(self, ed: EditalColetado) -> None:
        ref = ed.raw.get("_ref_itens") if ed.raw else None
        if ref:
            ed.itens = self._coletar_itens(*ref)

    def _coletar_modalidade_uf(self, modalidade: int, uf: str | None,
                               data_final: str, acc: dict) -> None:
        pagina = 1
        while True:
            params = {
                "dataFinal": data_final,
                "codigoModalidadeContratacao": modalidade,
                "pagina": pagina,
                "tamanhoPagina": self.tam_pagina,
            }
            if uf:
                params["uf"] = uf
            resp = self._get_com_retry(f"{self.base}/v1/contratacoes/proposta",
                                       params, timeout=40)
            if resp is None:
                # falhou mesmo após as re-tentativas: conta para o disjuntor
                self._falhas_seguidas += 1
                if self._falhas_seguidas >= 5:
                    self._abortado = True   # PNCP está recusando demais; para tudo
                break
            self._falhas_seguidas = 0  # sucesso reseta o contador

            if resp.status_code == 204:  # sem conteúdo
                break
            if resp.status_code != 200:
                log.warning("HTTP %s (mod=%s uf=%s): %s",
                            resp.status_code, modalidade, uf, resp.text[:200])
                break

            payload = resp.json()
            registros = payload.get("data") or []
            for reg in registros:
                ed = self._mapear_edital(reg, modalidade)
                if ed and ed.id_externo not in acc:
                    acc[ed.id_externo] = ed

            if not registros:
                break
            # Achado real (auditoria do agente debugger): confiar cego em
            # "totalPaginas" -- se a API mandar esse campo ausente/zero num
            # dia ruim, `or 1` tratava como "só tem 1 página" mesmo com um
            # lote CHEIO de volta, descartando o resto em silêncio (sem
            # erro, indistinguível de "não tem edital novo"). Só para de
            # paginar por "totalPaginas" quando ele realmente veio; sem
            # isso (ou com ele mentindo), usa um sinal que não depende da
            # API contar certo: a página não veio cheia, não tem próxima.
            total_paginas = payload.get("totalPaginas") or 0
            pagina_cheia = len(registros) >= self.tam_pagina
            ultima_pagina = (total_paginas and pagina >= total_paginas) or (not total_paginas and not pagina_cheia)
            if ultima_pagina:
                break
            pagina += 1
            time.sleep(self.delay)

    # ------------------------------------------------------------------ #
    def _mapear_edital(self, reg: dict, modalidade: int) -> EditalColetado | None:
        id_externo = reg.get("numeroControlePNCP")
        if not id_externo:
            return None

        orgao_ent = reg.get("orgaoEntidade") or {}
        unidade = reg.get("unidadeOrgao") or {}

        ed = EditalColetado(
            fonte=self.nome,
            id_externo=id_externo,
            orgao=orgao_ent.get("razaoSocial") or unidade.get("nomeUnidade"),
            cnpj_orgao=orgao_ent.get("cnpj"),
            objeto=reg.get("objetoCompra"),
            modalidade=reg.get("modalidadeNome") or MODALIDADE_NOME.get(modalidade),
            uf=unidade.get("ufSigla"),
            municipio=unidade.get("municipioNome"),
            valor_estimado=reg.get("valorTotalEstimado"),
            data_publicacao=_parse_data(reg.get("dataPublicacaoPncp")),
            data_abertura=_parse_data(reg.get("dataAberturaProposta")),
            data_encerramento=_parse_data(reg.get("dataEncerramentoProposta")),
            link=self._montar_link(reg),
            categoria_pncp=str(reg.get("codigoCategoriaProcesso") or reg.get("categoriaProcesso") or ""),
            # NÃO guardamos o JSON inteiro do PNCP (inflaria o banco com milhares
            # de editais). Só uma referência temporária para buscar os itens.
            raw={"_ref_itens": (orgao_ent.get("cnpj"), reg.get("anoCompra"),
                                reg.get("sequencialCompra"))},
        )
        return ed

    def _montar_link(self, reg: dict) -> str | None:
        # Link direto para a página da contratação no PNCP
        cnpj = (reg.get("orgaoEntidade") or {}).get("cnpj")
        ano = reg.get("anoCompra")
        seq = reg.get("sequencialCompra")
        if cnpj and ano and seq:
            return f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}"
        return reg.get("linkSistemaOrigem")

    # limite de segurança contra um loop sem fim (a API sempre devolvendo uma
    # página cheia) -- bem acima de qualquer edital real, só uma rede de segurança.
    _MAX_PAGINAS_ITENS = 20

    def _coletar_itens(self, cnpj: str | None, ano, sequencial) -> list[ItemColetado]:
        """Busca os itens detalhados da contratação (best-effort), paginando
        até acabar. Achado real (auditoria do agente debugger): buscava só a
        1ª página (100 itens) e nunca ia atrás do resto -- editais com mais
        de 100 itens (comuns em compra de material/uniforme/TI) perdiam os
        itens 101+ em silêncio, sem erro nenhum. Como esse é o dado usado
        pra comparar com o catálogo do usuário, isso escondia matches reais."""
        if not (cnpj and ano and sequencial):
            return []
        url = f"{self.itens_base}/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/itens"
        tam_pagina = 100
        itens: list[ItemColetado] = []
        for pagina in range(1, self._MAX_PAGINAS_ITENS + 1):
            try:
                resp = self._get_com_retry(url, {"pagina": pagina, "tamanhoPagina": tam_pagina}, timeout=30)
                if resp is None or resp.status_code != 200:
                    break
                dados = resp.json()
                # o endpoint pode devolver uma lista direta ou {"data": [...]}
                registros = dados.get("data") if isinstance(dados, dict) else dados
                registros = registros or []
            except (requests.RequestException, ValueError):
                break

            for it in registros:
                itens.append(ItemColetado(
                    numero=it.get("numeroItem"),
                    descricao=it.get("descricao") or "",
                    material_ou_servico=it.get("materialOuServicoNome") or it.get("materialOuServico"),
                    ncm=it.get("ncmNbsCodigo"),
                    catalogo_codigo=str(it.get("codigoItemCatalogo") or it.get("catalogoCodigoItem") or ""),
                    quantidade=it.get("quantidade"),
                    valor_unitario=it.get("valorUnitarioEstimado"),
                    unidade_medida=it.get("unidadeMedida"),
                ))
            # não confia em nenhum campo de "total de páginas" do lado dos
            # itens (nem toda resposta observada até hoje traz um) -- página
            # que não veio cheia é o sinal de que acabou.
            if len(registros) < tam_pagina:
                break
            time.sleep(self.delay)
        time.sleep(self.delay)
        return itens
