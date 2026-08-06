"""
Testes do motor de correspondência (sem banco, sem HTTP).
Rode com:  cd backend && pytest
"""
import pytest

from app.config import settings
from app.matching import engine as engine_mod
from app.matching.engine import (
    MatchingEngine, ProdutoCat, ItemEdt, aplicar_regras_exclusao, normalizar, so_digitos,
)


def _catalogo():
    return [
        ProdutoCat(id=1, descricao="Papel sulfite A4 75g resma branca",
                   ncm="48025590", palavras_chave="papel a4, sulfite, resma"),
        ProdutoCat(id=2, descricao="Caneta esferográfica azul",
                   catmat="279317", palavras_chave="caneta, esferografica"),
        ProdutoCat(id=3, descricao="Álcool em gel 70% antisséptico 500ml",
                   palavras_chave="alcool gel, antisseptico"),
    ]


def test_codigo_ncm_exato_bate():
    eng = MatchingEngine(_catalogo())
    r = eng.avaliar("Aquisição de papel", [ItemEdt(1, "Papel branco", ncm="4802.55.90")])
    assert r.nivel == "forte"
    assert r.detalhe[0]["motivo"].startswith("código NCM")


def test_codigo_catmat_exato_bate():
    eng = MatchingEngine(_catalogo())
    r = eng.avaliar("Material", [ItemEdt(1, "Caneta qualquer", catalogo_codigo="279317")])
    assert r.detalhe[0]["produto_id"] == 2
    assert r.detalhe[0]["score_item"] == 1.0


def test_similaridade_textual_bate():
    eng = MatchingEngine(_catalogo())
    r = eng.avaliar("Higiene", [ItemEdt(1, "Álcool em gel 70%, frasco 500ml com válvula pump")])
    assert r.itens_compativeis == 1
    assert r.nivel in ("medio", "forte")


def test_palavra_chave_bate():
    eng = MatchingEngine(_catalogo())
    r = eng.avaliar("Expediente", [ItemEdt(1, "Resma de papel tamanho A4 sulfite")])
    assert r.itens_compativeis == 1


def test_item_irrelevante_nao_bate():
    eng = MatchingEngine(_catalogo())
    r = eng.avaliar("Obra", [ItemEdt(1, "Serviço de pavimentação asfáltica e drenagem")])
    assert r.itens_compativeis == 0
    assert r.nivel == "fraco"


def test_edital_grande_um_item_fraco_nao_e_forte():
    eng = MatchingEngine(_catalogo())
    itens = [ItemEdt(i, f"Item irrelevante numero {i} sobre engenharia civil") for i in range(40)]
    itens.append(ItemEdt(99, "papel"))
    r = eng.avaliar("Edital grande", itens)
    assert r.nivel != "forte"


def test_numero_solto_e_unidade_nao_contam_como_termo_em_comum():
    """Regressão: "Régua 30 cm" batia como 'forte' com um item de edital pedindo
    "Quadro/Moldura ... com 30 cm de largura e 40 cm de altura" só porque os dois
    textos compartilham "30" e "cm" — sinal nenhum de que são o mesmo produto."""
    eng = MatchingEngine([ProdutoCat(id=1, descricao="Régua 30 cm")])
    r = eng.avaliar("Material de expediente", [ItemEdt(
        1, "Quadro/Moldura, em madeira, cor dourado, moldura com 2 cm (frente) "
           "com 30 cm de largura e 40 cm de altura, vidro incolor antirreflexo "
           "e fundo em MDF, com e pendurador")])
    assert r.itens_compativeis == 0


def test_anti_coincidencia_atua_mesmo_com_score_alto():
    """Regressão do bug real: "Pasta L" (pasta de arquivo de escritório) é um
    produto com descrição curta e genérica o suficiente pra o TF-IDF dar score
    ~1.0 contra um item de edital de material odontológico só por compartilhar
    a palavra "pasta" — mesmo sem nenhuma outra palavra distintiva em comum.
    Antes da correção, a proteção anti-coincidência só rodava com melhor < 0.9
    e esse caso escapava, virando "forte" indevidamente."""
    eng = MatchingEngine([ProdutoCat(id=1, descricao="Pasta L")])
    r = eng.avaliar("Material odontológico", [ItemEdt(
        1, "CIMENTO de hidroxido de calcio, sem eugenol, pasta/pasta")])
    assert r.itens_compativeis == 0
    assert r.nivel == "fraco"


def test_papel_nao_bate_com_fragmentadora_de_papel():
    """Já funcionava antes da correção — garante que não regrediu."""
    eng = MatchingEngine([ProdutoCat(id=1, descricao="Papel Sulfite A4 75g")])
    r = eng.avaliar("Equipamentos de escritório",
                     [ItemEdt(1, "Fragmentadora de papel 12 folhas")])
    assert r.itens_compativeis == 0


def test_papel_sulfite_bate_com_varias_palavras_em_comum():
    """Vários termos distintivos em comum (papel, sulfite, a4, branco, resma)
    — a correção não pode derrubar um match legítimo como esse."""
    eng = MatchingEngine([ProdutoCat(id=1, descricao="Papel Sulfite A4 75g branco resma")])
    r = eng.avaliar("Material de expediente",
                     [ItemEdt(1, "Resma de papel sulfite A4 branco 75 gramas")])
    assert r.itens_compativeis == 1
    assert r.nivel == "forte"


def test_papel_generico_nao_desempata_arbitrariamente_pra_produto_errado():
    """Caso real (auditoria em produção): item "Papel Não Clorado" (papel
    sulfite comum) casava com "Papel Photo 135g Glossy Adesivo" — produto
    sem nenhuma relação — só porque a única palavra-chave em comum era
    "papel", empatando com vários "Papel A4 75g Sulfite" corretos também
    cadastrados; o desempate acabava sendo por ordem no catálogo, não por
    relevância. Com catálogo tendo o produto errado ANTES do certo (a ordem
    que expôs o bug), a similaridade textual agora precisa vencer e escolher
    o certo."""
    catalogo = [
        ProdutoCat(id=1, descricao="Papel Photo 135g A4 Glossy Adesivo",
                   palavras_chave="papel fotografico, papel brilhante, papel glossy, papel photo, papel"),
        ProdutoCat(id=2, descricao="Papel A4 75 g/m2 500 Folhas Branco Chamex",
                   palavras_chave="papel sulfite a4, papel branco a4, papel sulfite, papel 75g, folha a4, papel a4, sulfite, papel, resma"),
    ]
    eng = MatchingEngine(catalogo)
    r = eng.avaliar("Material de expediente", [ItemEdt(
        1, "Papel Nao Clorado formato: a4, comprimento: 297, largura: 210, "
           "gramatura: 75, aplicacao: impressora laser")])
    if r.detalhe and r.detalhe[0]["produto_id"] is not None:
        assert r.detalhe[0]["produto_id"] == 2


def _catalogo_diverso():
    """Catálogo com uma boa variedade de categorias — necessário pra
    reproduzir de forma realista o peso IDF do TF-IDF (um catálogo com só 1-2
    produtos deixa a ponderação de termo raro/comum degenerada e não
    reproduz o comportamento real; a auditoria que achou esse bug usou o
    catálogo de produção, com 63 produtos de categorias variadas)."""
    return [
        ProdutoCat(id=1, descricao="Bobina Termica 80mm x 40m Branca 48g 0118 Rio branco - 30UN",
                   palavras_chave="bobina para impressora, bobina termica, papel termico, bobina 80mm, termica, bobina"),
        ProdutoCat(id=2, descricao="Caneta Esferografica Cristal Dura 1,0mm Azul 0819 Bic - 50UN",
                   palavras_chave="caneta esferografica, caneta cristal, esferografica"),
        ProdutoCat(id=3, descricao="Grampeador Metal 20 Folhas MX-G20C 714465 Maxprint - UN",
                   palavras_chave="grampeador de mesa, grampeador manual, grampeador metal, grampeador, metal"),
        ProdutoCat(id=4, descricao="Papel A4 75 g/m2 500 Folhas Branco 3502 Chamex",
                   palavras_chave="papel sulfite a4, papel branco a4, papel sulfite, papel a4, sulfite, papel, resma"),
        ProdutoCat(id=5, descricao="Clips Galvanizado N4/0 420 Unidades Linha Leve Bacchi",
                   palavras_chave="clipe para papel, clips galvanizado, clipe metalico, clips, clipe"),
        ProdutoCat(id=6, descricao="Cola Branca 40g 0065 Iris - UN",
                   palavras_chave="cola escolar, cola liquida, cola branca, cola pva, branca, cola"),
        ProdutoCat(id=7, descricao="Lapis Grafite N2 com Borracha EcoMax Sextavo FaberCastell",
                   palavras_chave="lapis grafite, lapis preto, lapis n2, lapis"),
        ProdutoCat(id=8, descricao="Pasta Suspensa Delloplus Pacote com 6 Unidades Azul Dello",
                   palavras_chave="pasta suspensa, pasta arquivo, pasta azul, pasta"),
        ProdutoCat(id=9, descricao="Fita Crepe Branca Phenix Tape 15mm x 50m",
                   palavras_chave="fita crepe branca 15mm"),
        ProdutoCat(id=10, descricao="Envelope Kraft Natural 240mm 340mm 80G 0233 Scrity - 100UN",
                   palavras_chave="envelope kraft, envelope pardo, envelope saco, envelope, natural, kraft"),
        ProdutoCat(id=11, descricao="Calculadora de Mesa 8 Digitos VX1385",
                   palavras_chave="calculadora de mesa, calculadora 8 digitos, calculadora"),
        ProdutoCat(id=12, descricao="Perfurador de Papel 2 Furos para 12 Folhas Jocar Office",
                   palavras_chave="perfurador de papel, furador de papel, perfurador"),
        ProdutoCat(id=13, descricao="Compasso Escolar Colorido Metal Sortido Tris",
                   palavras_chave="compasso escolar, compasso metal, compasso"),
        ProdutoCat(id=14, descricao="Marcador Quadro Branco Slim 12 Unidades Preto BRW",
                   palavras_chave="marcador quadro branco, caneta quadro branco, marcador"),
        ProdutoCat(id=15, descricao="Corretivo Liquido a Base de Agua 18ml Radex",
                   palavras_chave="corretivo liquido, corretivo, radex"),
    ]


def test_palavra_isolada_precisa_de_similaridade_textual_minima():
    """Caso real (auditoria em produção): item "Ribbon ... impressora
    térmica" casava com "Bobina Térmica" (produto de PAPEL, não fita de
    impressão) só por compartilharem a palavra "térmica" — um MODIFICADOR,
    não o substantivo do produto. A contagem de palavras-chave sozinha não
    enxerga isso (bate 1 termo e pronto); o cosseno TF-IDF do texto INTEIRO
    sim (fica bem baixo, porque o resto das duas descrições não se parece em
    nada). Regra geral, não específica desse produto: 0-1 palavra-chave
    específica só vale se a similaridade textual junto não for irrisória —
    2+ específicas continuam valendo incondicionalmente."""
    eng = MatchingEngine(_catalogo_diverso(), usar_ia=False)
    r = eng.avaliar("Material de expediente", [ItemEdt(
        1, "Ribbon material: cera, largura: 110, comprimento: 74, cor: preta, "
           "aplicacao: impressora termica")])
    assert r.itens_compativeis == 0


def test_palavra_isolada_com_similaridade_textual_boa_continua_valendo():
    """A correção não pode virar falso negativo generalizado — uma palavra
    isolada mas GENUÍNA ("clipe"/"clips") com o resto do texto também
    parecido (similaridade textual acima do piso) continua batendo."""
    eng = MatchingEngine(_catalogo_diverso(), usar_ia=False)
    r = eng.avaliar("Material de expediente", [ItemEdt(
        1, "Clipe tratamento superficial: galvanizado, tamanho: 8/0, "
           "material: arame de aco, formato: paralelo")])
    assert r.itens_compativeis == 1


def test_piso_de_palavra_isolada_compara_o_candidato_certo_nao_qualquer_um():
    """Caso real (auditoria em produção): item "Perfurador Papel...
    quantidade furos: 2" tinha o candidato CERTO ("Perfurador de Papel")
    disponível via similaridade textual, só que com score abaixo do score
    fixo da palavra-chave isolada "metal" (que aponta pro candidato ERRADO,
    "Grampeador Metal"). A primeira versão do piso comparava contra
    `melhor` — a similaridade do MELHOR candidato qualquer, nesse caso o
    próprio Perfurador — e como esse valor já passava do piso, "liberava"
    a troca pelo candidato ERRADO da palavra-chave, mesmo esse candidato
    não tendo nenhuma similaridade real com o item. Tem que comparar a
    similaridade do candidato ESPECÍFICO que a palavra-chave escolheu."""
    eng = MatchingEngine(_catalogo_diverso(), usar_ia=False)
    it = ItemEdt(1, "Perfurador Papel material: metal, tipo: mesa, tratamento "
                    "superficial: pintado, capacidade perfuracao: 30, funcionamento: "
                    "manual, caracteristicas adicionais: furo redondo, quantidade furos: 2")
    sc, prod, motivo = eng._score_item(it)
    assert prod is not None
    assert "Grampeador" not in prod.descricao
    assert "Perfurador" in prod.descricao


def test_codigo_catmat_exato_sem_corroboracao_textual_nao_vira_candidato():
    """Comportamento mudou de propósito: código exato (CATMAT/CATSER/NCM)
    batendo SEM nenhuma palavra distintiva em comum com o item não vira mais
    vencedor automático — mesma correção do caso real de NCM (Álcool
    Etílico x Desinfetante), aplicada de forma geral a qualquer tipo de
    código. "Pasta L" (pasta de arquivo) x "CIMENTO de hidroxido de calcio"
    (material odontológico) compartilhando um CATMAT é exatamente esse tipo
    de falso positivo — cai pro fluxo normal, que aqui não acha nada."""
    eng = MatchingEngine([ProdutoCat(id=1, descricao="Pasta L", catmat="150123")])
    r = eng.avaliar("Material odontológico",
                     [ItemEdt(1, "CIMENTO de hidroxido de calcio", catalogo_codigo="150123")])
    assert r.nivel == "fraco"
    assert r.detalhe == []


def test_regra_exclusao_por_termo():
    ignora = aplicar_regras_exclusao(
        "Contratação de empresa de engenharia para reforma", [],
        termos=["engenharia"], categoria_pncp=None, categorias_excluidas=[])
    assert ignora is True


def test_regra_exclusao_por_categoria():
    ignora = aplicar_regras_exclusao(
        "Qualquer objeto", [], termos=[], categoria_pncp="9",
        categorias_excluidas=["9"])
    assert ignora is True


def test_normalizar_remove_acentos_e_pontuacao():
    assert normalizar("Álcool 70%, em GEL!") == "alcool 70 em gel"


def test_so_digitos():
    assert so_digitos("4802.55.90") == "48025590"


# ---------------------------------------------------------------------------
# Combinação do score textual com a IA semântica (embeddings mockados —
# sem chamada de rede real).
# ---------------------------------------------------------------------------
def _ligar_ia_falsa(monkeypatch, ia_score, ia_prod):
    """Liga o modo IA e substitui a geração de embeddings e o cálculo de
    similaridade semântica por valores controlados, sem tocar na rede."""
    monkeypatch.setattr(engine_mod, "ia_disponivel", lambda key: True)
    monkeypatch.setattr(
        engine_mod, "_ia_embeddings_gemini",
        lambda textos, timeout=30, api_key=None: [[1.0]] * len(textos))
    monkeypatch.setattr(
        MatchingEngine, "_ia_score_item",
        lambda self, item_emb: (ia_score, ia_prod))


def test_ia_sem_sinal_nao_penaliza_score_textual(monkeypatch):
    """Quando a IA não confirma nada (cosseno abaixo do IA_FLOOR -> ia_sc=0),
    isso é "sem opinião", não "sinal negativo" — não deve derrubar um score
    textual que já era bom."""
    catalogo = _catalogo()
    monkeypatch.setattr(MatchingEngine, "_score_item",
                         lambda self, item, **kwargs: (0.5, catalogo[0], "similaridade textual"))
    _ligar_ia_falsa(monkeypatch, ia_score=0.0, ia_prod=None)

    eng = MatchingEngine(catalogo, usar_ia=True, gemini_key="fake-key")
    r = eng.avaliar("Objeto", [ItemEdt(1, "qualquer coisa")])

    assert r.detalhe[0]["score_item"] == pytest.approx(0.5, abs=1e-3)
    assert r.detalhe[0]["motivo"] == "similaridade textual"


def test_ia_com_sinal_combina_score(monkeypatch):
    """Quando a IA tem sinal (ia_sc > 0), ela deve continuar sendo combinada
    com o score textual pela média ponderada de IA_PESO, podendo inclusive
    trocar o produto sugerido."""
    catalogo = _catalogo()
    produto_ia = catalogo[1]
    monkeypatch.setattr(MatchingEngine, "_score_item",
                         lambda self, item, **kwargs: (0.5, catalogo[0], "similaridade textual"))
    _ligar_ia_falsa(monkeypatch, ia_score=0.8, ia_prod=produto_ia)

    eng = MatchingEngine(catalogo, usar_ia=True, gemini_key="fake-key")
    r = eng.avaliar("Objeto", [ItemEdt(1, "qualquer coisa")])

    esperado = 0.5 * (1 - settings.IA_PESO) + 0.8 * settings.IA_PESO
    assert r.detalhe[0]["score_item"] == pytest.approx(esperado, abs=1e-3)
    assert r.detalhe[0]["produto_id"] == produto_ia.id
    assert "IA" in r.detalhe[0]["motivo"]


# ---------------------------------------------------------------------------
# Escolha do provedor de embeddings: Gemini (chave pessoal) quando existir,
# senão BGE-M3/DeepInfra (chave GLOBAL do operador) — assim quem não tem
# chave Gemini própria também ganha a camada semântica extra.
# ---------------------------------------------------------------------------
def test_sem_gemini_e_sem_deepinfra_usar_ia_fica_desligado(monkeypatch):
    monkeypatch.setattr(settings, "DEEPINFRA_API_KEY", "")
    eng = MatchingEngine(_catalogo(), usar_ia=True, gemini_key=None)
    assert eng.usar_ia is False
    assert eng._ia_embeddings is None


def test_sem_gemini_mas_com_deepinfra_usa_bge_m3(monkeypatch):
    monkeypatch.setattr(settings, "DEEPINFRA_API_KEY", "chave-global-fake")
    chamadas = {}
    def _fake_deepinfra(textos, timeout=30, api_key=None):
        chamadas["api_key"] = api_key
        return [[1.0]] * len(textos)
    monkeypatch.setattr(engine_mod, "_ia_embeddings_deepinfra", _fake_deepinfra)

    eng = MatchingEngine(_catalogo(), usar_ia=True, gemini_key=None)
    assert eng.usar_ia is True
    eng._ia_embeddings(["texto qualquer"])
    assert chamadas["api_key"] == "chave-global-fake"


def test_com_gemini_ignora_deepinfra_mesmo_configurado(monkeypatch):
    monkeypatch.setattr(settings, "DEEPINFRA_API_KEY", "chave-global-fake")
    monkeypatch.setattr(engine_mod, "ia_disponivel", lambda key: bool(key))
    chamadas = {"gemini": False, "deepinfra": False}
    monkeypatch.setattr(engine_mod, "_ia_embeddings_gemini",
                        lambda textos, timeout=30, api_key=None: chamadas.__setitem__("gemini", True) or [[1.0]] * len(textos))
    monkeypatch.setattr(engine_mod, "_ia_embeddings_deepinfra",
                        lambda textos, timeout=30, api_key=None: chamadas.__setitem__("deepinfra", True) or [[1.0]] * len(textos))

    eng = MatchingEngine(_catalogo(), usar_ia=True, gemini_key="chave-gemini-fake")
    eng._ia_embeddings(["texto qualquer"])
    assert chamadas == {"gemini": True, "deepinfra": False}


def test_codigo_exato_com_texto_relacionado_e_confianca_alta():
    """Código bate E o texto corrobora (mesmo que fracamente) — confia
    automático, como sempre foi."""
    eng = MatchingEngine(_catalogo())
    r = eng.avaliar("Aquisição de papel", [ItemEdt(1, "Papel branco", ncm="4802.55.90")])
    assert r.detalhe[0]["confianca"] == "alta"
    assert r.detalhe[0]["produto_id"] == 1


def test_codigo_exato_sem_relacao_textual_nao_vira_candidato_nenhum():
    """Caso real de produção — RECORRENTE: item "Álcool Etílico ... gel ...
    70% v/v" (e depois também a variante "hidratado") batia por NCM idêntico
    com "Desinfetante 5 litros Lavanda" — mesmo código fiscal (categoria
    ampla de produto de limpeza/higiene), mas os textos não têm nada em
    comum. Isso ainda aparecia como sugestão "média" com score 1.0 (posição
    nº1), confundindo o usuário mesmo pedindo confirmação. Agora, sem
    NENHUMA corroboração textual, o código exato nem vira candidato — cai
    pro fluxo normal de pontuação, que aqui não acha nada (catálogo
    genuinamente não tem produto de álcool) — resultado honesto: sem
    sugestão nenhuma, em vez de uma errada com aparência de confiável."""
    catalogo = [ProdutoCat(id=1, descricao="Desinfetante 5 litros Lavanda 9007 Urca",
                           ncm="34029090", palavras_chave="desinfetante, lavanda, limpeza")]
    eng = MatchingEngine(catalogo)
    r = eng.avaliar("Material de limpeza e higiene", [ItemEdt(
        1, "Álcool Etílico composição básica: com emoliente, forma farmacêutica: gel, "
           "teor alcoólico: 70% v/v", ncm="3402.90.90")])
    assert r.detalhe == []
    assert r.itens_compativeis == 0


def test_codigo_exato_sem_corroboracao_nao_ofusca_match_textual_real():
    """Quando o código exato bate errado (sem corroboração) mas o catálogo
    TEM um produto de verdade compatível em outro lugar, esse produto real
    tem que vencer — o código exato sem suporte nenhum não pode nem
    aparecer como candidato, muito menos ofuscar o match certo."""
    catalogo = [
        ProdutoCat(id=1, descricao="Desinfetante 5 litros Lavanda 9007 Urca", ncm="34029090",
                   palavras_chave="desinfetante lavanda, desinfetante piso"),
        ProdutoCat(id=2, descricao="Álcool Etílico Gel 70% Antisséptico 500ml",
                   palavras_chave="alcool gel, antisseptico, alcool 70"),
    ]
    eng = MatchingEngine(catalogo)
    r = eng.avaliar("Aquisição de álcool", [ItemEdt(
        1, "Álcool Etílico tipo: hidratado, teor alcoólico: 70% ( 70°gl), "
           "apresentação: glicerinado, líquido", ncm="3402.90.90")])
    item = r.detalhe[0]
    assert item["produto_id"] == 2
    assert item["confianca"] == "alta"
    assert all(c["produto_id"] != 1 for c in item["candidatos"])
