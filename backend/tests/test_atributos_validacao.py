"""
Testes de extração de atributos técnicos e validação de regra de negócio
(sem banco, sem HTTP). Rode com:  cd backend && pytest
"""
from app.matching.atributos import estado_caracteristica, extrair_atributos
from app.matching.validacao import classificar, validar


def test_extrai_numerico_com_operador_minimo():
    a = extrair_atributos("bandeja com capacidade para no mínimo 250 folhas")
    assert any(n.unidade == "folhas" and n.valor == 250 and n.operador == ">=" for n in a.numericos)


def test_extrai_numerico_sem_operador_e_exato():
    a = extrair_atributos("bandeja com capacidade para 150 folhas")
    assert any(n.unidade == "folhas" and n.valor == 150 and n.operador == "==" for n in a.numericos)


def test_extrai_numerico_com_separador_de_milhar():
    a = extrair_atributos("bandeja com capacidade para no mínimo 2.500 folhas")
    assert any(n.unidade == "folhas" and n.valor == 2500 and n.operador == ">=" for n in a.numericos)


def test_extrai_numerico_polegadas_com_aspas_e_espaco():
    a = extrair_atributos('Monitor de 21" LED Full HD')
    assert any(n.unidade == "polegadas" and n.valor == 21 for n in a.numericos)


def test_operador_maximo_nao_casa_dentro_de_outra_palavra():
    """'ate' sem \\b não pode casar dentro de 'bateria'/'material' e virar
    um operador '<=' que não existe no texto."""
    a = extrair_atributos("Impressora com bateria interna, 30 ppm de velocidade")
    assert any(n.unidade == "ppm" and n.valor == 30 and n.operador == "==" for n in a.numericos)


def test_operador_minimo_reconhece_concordancia_feminina():
    """'capacidade MÍNIMA' (concordando com 'capacidade') é tão '>=' quanto
    'no MÍNIMO' — só a forma adverbial invariável era reconhecida antes."""
    a = extrair_atributos("grampeador de mesa, capacidade mínima 20 folhas, grampos 26/6")
    achado = next(n for n in a.numericos if n.unidade == "folhas")
    assert achado.valor == 20
    assert achado.operador == ">="


def test_validacao_nao_reprova_quando_produto_supera_minimo_feminino():
    item = "grampeador de mesa, capacidade mínima 20 folhas"
    produto = "grampeador de mesa, capacidade 100 folhas"
    r = validar(item, produto)
    assert not any(p.tipo == "numerico" and p.critico for p in r.pendencias)


def test_operador_nao_vaza_de_uma_clausula_para_a_proxima():
    a = extrair_atributos("no mínimo 30 ppm, 250 folhas")
    achado_folhas = next(n for n in a.numericos if n.unidade == "folhas")
    assert achado_folhas.valor == 250
    assert achado_folhas.operador == "=="


def test_operador_nao_vaza_de_uma_clausula_para_a_proxima_sem_pontuacao():
    """Mesmo bug do teste acima, mas separado por conjunção ('e') em vez de
    vírgula — edital real raramente usa vírgula entre cada especificação."""
    a = extrair_atributos("no mínimo 30 ppm e bandeja para 250 folhas")
    achado_folhas = next(n for n in a.numericos if n.unidade == "folhas")
    assert achado_folhas.valor == 250
    assert achado_folhas.operador == "=="


def test_extrai_ppm_abreviado_pag_min():
    a = extrair_atributos("impressora 30 pag/min")
    assert any(n.unidade == "ppm" and n.valor == 30 for n in a.numericos)


def test_extrai_numerico_decimal_com_ponto_fora_do_padrao_br():
    """'1.0mm' (ponto como decimal, comum em ficha técnica copiada de fora)
    tem que virar 1.0, não 0 (o fragmento depois do ponto sozinho)."""
    a = extrair_atributos("Caneta Esferográfica Economic 1.0mm Azul 1001 Compactor")
    achado = next(n for n in a.numericos if n.unidade == "mm")
    assert achado.valor == 1.0


def test_extrai_numerico_milhar_com_decimal_junto():
    a = extrair_atributos("capacidade de 2.500,50 litros")
    achado = next(n for n in a.numericos if n.unidade == "litros")
    assert achado.valor == 2500.5


def test_extrai_numerico_deduplica_especificacao_repetida_no_texto():
    """Edital real costuma repetir a mesma especificação em mais de uma
    frase da descrição do item — não pode virar pendência duplicada."""
    a = extrair_atributos(
        "Lapiseira mecânica 0.7 mm, corpo hexagonal. "
        "Lapiseira mecânica com ponta de 0.7 mm, formato ergonômico."
    )
    achados_mm = [n for n in a.numericos if n.unidade == "mm"]
    assert len(achados_mm) == 1
    assert achados_mm[0].valor == 0.7


def test_extrai_campo_estruturado_do_pncp_sem_unidade_colada():
    """PNCP manda especificação em campos tipo 'comprimento: 145' (sem
    unidade no texto, unidade é implícita no schema do catálogo = mm)."""
    a = extrair_atributos(
        "Extrator Grampo material: metal, tipo: espátula, "
        "tratamento superficial: zincado, comprimento: 145, largura: 15"
    )
    unidades_valores = {(n.unidade, n.valor) for n in a.numericos}
    assert ("mm", 145.0) in unidades_valores
    assert ("mm", 15.0) in unidades_valores


def test_campo_estruturado_nao_duplica_quando_ja_tem_unidade():
    """'comprimento: 145mm' (já com unidade colada) não pode virar '145mmmm'
    nem extrair só o último dígito antes do 'mm'."""
    a = extrair_atributos("comprimento: 145mm, largura: 15")
    achado_145 = next(n for n in a.numericos if n.bruto.startswith("145"))
    assert achado_145.valor == 145.0


def test_campo_estruturado_sem_unidade_fica_marcado_como_inferido():
    """A suposição de mm pra 'comprimento: 21' (sem unidade no texto) é
    exatamente isso — uma suposição, não um fato lido do texto. Precisa
    ficar marcada como tal pra validacao.py saber que não pode reprovar
    um produto sozinha (ex.: '21' pode ser 21cm de uma tesoura, não 21mm)."""
    a = extrair_atributos("Tesoura material: aço inoxidável, comprimento: 21, cabo plástico")
    achado = next(n for n in a.numericos if n.unidade == "mm")
    assert achado.valor == 21.0
    assert achado.inferido is True


def test_unidade_explicita_no_texto_nao_fica_marcada_como_inferida():
    """Quando a unidade está escrita no texto (não veio de suposição de
    campo estruturado), é fato — não pode ficar marcada como inferida,
    mesmo sendo uma das unidades cobertas por _CAMPOS_DIMENSAO_MM."""
    a = extrair_atributos("Papel especial com espessura de 0,10mm, alta gramatura")
    achado = next(n for n in a.numericos if n.unidade == "mm")
    assert achado.valor == 0.10
    assert achado.inferido is False


def test_extrai_categorico_grampo():
    a = extrair_atributos("grampeador de mesa para grampos 26/6")
    assert a.categoricos.get("grampo") == "26/6"


def test_caracteristica_presente_oposto_ausente():
    assert estado_caracteristica("bivolt", "aparelho bivolt 110/220v") == "presente"
    assert estado_caracteristica("bivolt", "aparelho 220v apenas") == "oposto"
    assert estado_caracteristica("bivolt", "aparelho eletrico qualquer") == "ausente"


def test_caracteristica_presente_vence_quando_texto_menciona_os_dois_modos():
    """Produto dual-mode (com fio E sem fio) não pode virar 'oposto' só
    porque 'com fio' também aparece no texto."""
    texto = "impressora com conectividade sem fio (wi-fi) e também com fio (ethernet)"
    assert estado_caracteristica("sem_fio", texto) == "presente"


def test_caracteristica_oposto_por_substring_de_presente_continua_oposto():
    """'duplex manual' contém 'duplex' (termo 'presente'), mas é a frase
    'oposto' completa que deve vencer — não é um caso de dupla menção real."""
    assert estado_caracteristica("duplex", "impressora com duplex manual") == "oposto"


def test_validacao_reprova_capacidade_insuficiente():
    item = "impressora com bandeja para no mínimo 250 folhas"
    produto = "impressora com bandeja para 150 folhas"
    r = validar(item, produto)
    assert not r.atende
    assert any(p.tipo == "numerico" and p.critico for p in r.criticas)


def test_validacao_reprova_grampo_incompativel():
    item = "grampeador de mesa, capacidade mínima 20 folhas, grampos 26/6"
    produto = "grampeador tipo alicate, capacidade 100 folhas, grampos 23/13"
    r = validar(item, produto)
    assert not r.atende
    assert any(p.tipo == "categorico" and p.critico for p in r.criticas)


def test_validacao_passa_quando_specs_batem():
    item = "impressora bivolt, no mínimo 30 ppm"
    produto = "impressora bivolt (110/220v), 32 ppm"
    r = validar(item, produto)
    assert r.atende
    assert not r.criticas


def test_validacao_rebaixa_para_aviso_quando_unidade_do_item_e_inferida():
    """Caso real achado analisando editais do PNCP: item 'comprimento: 21'
    (sem unidade — o código assume mm) contra um produto real de 21cm.
    Antes desse fix, virava CRÍTICA ('exige 21mm, produto oferece 21cm') e
    reprovava um produto que na verdade bate (os dois são 21cm — a
    suposição de mm que estava errada, não o produto). Uma suposição não
    pode reprovar sozinha; agora vira aviso pra confirmar manualmente."""
    item = "Tesoura material: aço inoxidável, material cabo: plástico, comprimento: 21"
    produto = "Tesoura Inox com Plástico 21cm 61 Nox - UN"
    r = validar(item, produto)
    assert r.atende  # não reprova mais
    assert not r.criticas
    assert any(p.tipo == "numerico" and "unidade assumida" in p.descricao for p in r.avisos)
    assert classificar(0.6, r) == "Atende parcialmente"  # ainda pede conferência, não vira "Atende" pleno


def test_validacao_mantem_critica_quando_unidade_e_explicita_dos_dois_lados():
    """O rebaixamento pra aviso é só pra unidade INFERIDA do item. Quando as
    duas unidades vêm explícitas no texto (nenhuma suposição envolvida) e
    os valores realmente não batem, continua sendo crítica — não pode virar
    aviso genérico pra qualquer conflito de unidade mm/cm."""
    item = "Régua com no mínimo 30 cm de comprimento, material acrílico"
    produto = "Régua Acrílica Transparente 150mm Waleu"
    r = validar(item, produto)
    assert not r.atende
    assert any(p.tipo == "numerico" and p.critico and "unidade assumida" not in p.descricao
              for p in r.criticas)


def test_validacao_reconhece_mesma_familia_de_unidade_com_escala_diferente():
    """'2 litros' (item) e '2000 ml' (produto) são o MESMO valor — não pode
    virar aviso de 'produto não informa' só por rótulos de unidade diferentes."""
    r = validar("galão com capacidade mínima de 2 litros", "galão com capacidade para 2000 ml")
    assert r.atende
    assert not r.pendencias
    assert r.verificavel


def test_validacao_reprova_por_familia_mesmo_com_unidades_diferentes():
    item = "HD externo com no mínimo 1 tb"
    produto = "HD externo com 500 gb"
    r = validar(item, produto)
    assert not r.atende
    assert any(p.tipo == "numerico" and p.critico for p in r.criticas)


def test_validacao_avisa_quando_ha_multiplos_valores_para_a_mesma_unidade():
    item = "Bandeja de papel com capacidade para no mínimo 500 folhas"
    produto = (
        "Impressora com bandeja principal para 100 folhas e compartimento de "
        "acessórios com capacidade para 1000 folhas de material de embalagem"
    )
    r = validar(item, produto)
    assert any(
        p.tipo == "numerico" and not p.critico and "múltiplos valores" in p.descricao
        for p in r.avisos
    )


def test_multiplos_valores_ambiguos_nao_vira_critica_com_pick_arbitrario():
    """Caso real (auditoria em produção): "Papel Sulfite A4 180 g/m² 60 kg
    Branco ..." tem "180 g/m²" (a gramatura, o que interessa) e "60 kg"
    (peso de fardo/caixa, uma coisa completamente diferente que só cai na
    mesma família "massa" por coincidência de unidade) — comparar contra o
    MAIOR valor (60kg) reprovava o produto com "produto oferece 60 kg" numa
    comparação de gramatura de papel, o que não faz sentido físico nenhum.
    Já que o próprio aviso de ambiguidade admite não saber qual valor vale,
    também não dá pra afirmar com confiança que o produto NÃO atende."""
    item = "Papel Sulfite gramatura 75g/m2, tipo A4"
    produto = "Papel Sulfite A4 180 g/m2 60 kg Branco 5074 Chamex - 50FL"
    r = validar(item, produto)
    assert not r.criticas
    assert any("múltiplos valores" in p.descricao for p in r.avisos)


def test_valor_unico_sem_ambiguidade_continua_reprovando():
    """A correção acima não pode virar falso negativo generalizado — um
    único valor claro que não bate continua reprovando normalmente."""
    item = "Papel Sulfite gramatura 90g/m2, tipo A4"
    produto = "Caixa Papel A4 75g 500 Folhas Branco Chamex - 10 Resmas"
    r = validar(item, produto)
    assert any(p.critico for p in r.criticas)


def test_classificar_score_baixo_e_sempre_nao_atende():
    from app.matching.validacao import ResultadoValidacao
    assert classificar(0.1, ResultadoValidacao()) == "Não atende"


def test_classificar_sem_nada_verificavel_nao_e_atende_pleno():
    """Score alto sem NENHUM atributo reconhecível no item (categoria fora
    do vocabulário de atributos.py) não pode virar 'Atende' só por falta de
    pendência — não houve validação técnica real, então cai em parcial."""
    from app.matching.validacao import ResultadoValidacao
    r = ResultadoValidacao()
    assert not r.verificavel
    assert classificar(0.9, r) == "Atende parcialmente"


def test_classificar_pendencia_critica_vence_score_alto():
    item = "impressora com bandeja para no mínimo 250 folhas"
    produto = "impressora com bandeja para 150 folhas"
    r = validar(item, produto)
    assert classificar(0.95, r) == "Não atende"


def test_classificar_atende_parcialmente_quando_ha_apenas_avisos():
    item = "impressora bivolt, duplex automático, no mínimo 30 ppm"
    produto = "impressora bivolt (110/220v). Demais especificações sob consulta."
    r = validar(item, produto)
    assert r.atende  # sem críticas
    assert r.avisos  # mas com lacunas de informação
    assert classificar(0.6, r) == "Atende parcialmente"


def test_classificar_atende_pleno_sem_pendencias():
    item = "impressora bivolt, no mínimo 30 ppm"
    produto = "impressora bivolt (110/220v), 32 ppm"
    r = validar(item, produto)
    assert r.verificavel
    assert not r.pendencias


def test_extrai_par_de_dimensao_com_unidade_so_no_final():
    a = extrair_atributos("envelope de segurança 30x40cm")
    achados = {(n.unidade, n.valor) for n in a.numericos}
    assert ("cm", 30.0) in achados and ("cm", 40.0) in achados
    assert all(not n.inferido for n in a.numericos)


def test_extrai_par_de_dimensao_com_unidade_propria_em_cada_lado():
    a = extrair_atributos("placa de aço 12cm x 50mm")
    achados = {(n.unidade, n.valor) for n in a.numericos}
    assert ("cm", 12.0) in achados and ("mm", 50.0) in achados


def test_extrai_par_de_dimensao_sem_unidade_assume_mm_e_marca_inferido():
    a = extrair_atributos("etiqueta adesiva 12x50")
    achados = {(n.unidade, n.valor): n.inferido for n in a.numericos}
    assert achados.get(("mm", 12.0)) is True
    assert achados.get(("mm", 50.0)) is True


def test_par_de_dimensao_nao_confunde_multiplicador_de_quantidade():
    """'3x500ml' é quantidade (3 embalagens de 500ml), não uma dimensão —
    'ml' não é unidade de comprimento, então só '500ml' deve ser extraído,
    e não um par de dimensão com '3'."""
    a = extrair_atributos("caixa com 3x500ml")
    assert not any(n.valor == 3.0 for n in a.numericos)
    assert any(n.unidade == "ml" and n.valor == 500.0 for n in a.numericos)


def test_validacao_de_par_de_dimensao_gera_uma_so_pendencia_nao_quatro():
    """Caso real: item 'BLOCO POST-IT 38 X 51' (par sem unidade) vs produto
    'Post-it 38mm 50mm' (duas medidas soltas, sem 'x' entre elas). Antes da
    comparação em grupo, cada exigência (38 e 51) procurava seu 'melhor'
    candidato entre OS DOIS valores do produto (38mm, 50mm) isoladamente,
    gerando 2x2=4 pendências repetidas. Agora deve virar só 1."""
    item = "BLOCO POST-IT 38 X 51 COM 04 UNIDADES"
    produto = "Bloco Adesivo Post-it 38mm 50mm tropical 50 Folhas"
    r = validar(item, produto)
    pendencias_dimensao = [p for p in r.pendencias if "38" in p.descricao or "51" in p.descricao]
    assert len(pendencias_dimensao) == 1
    assert not pendencias_dimensao[0].critico  # unidade assumida, não pode reprovar sozinha
    assert r.atende  # aviso, não crítica


def test_validacao_de_par_de_dimensao_bate_exato_sem_pendencia():
    item = "BLOCO POST-IT 38 X 51 COM 04 UNIDADES"
    produto = "Bloco Adesivo Post-it 38mm 51mm tropical 50 Folhas"
    r = validar(item, produto)
    assert not r.pendencias
    assert r.atende


def test_extrai_digitos_de_calculadora():
    a = extrair_atributos("Calculadora de Mesa 12 Dígitos")
    assert any(n.unidade == "digitos" and n.valor == 12 for n in a.numericos)


def test_validacao_reprova_calculadora_com_menos_digitos_que_o_exigido():
    """Caso real: item exige 12 dígitos e o produto casado só tem 8 — antes
    de 'digitos' virar unidade reconhecida, esse mismatch não gerava
    NENHUMA pendência (o número não batia em nenhuma unidade da lista)."""
    item = "CALCULADORA DE MESA 12 DIGITOS"
    produto = "Calculadora de Mesa 8 Dígitos VX1385"
    r = validar(item, produto)
    assert not r.atende
    assert any(p.tipo == "numerico" and p.critico for p in r.criticas)
    assert classificar(0.9, r) == "Não atende"


def test_extrai_metros_de_campo_estruturado_sem_assumir_mm():
    """Caso real (edital): 'COMPRIMENTO: 96 METROS' estava virando 96mm por
    engano — o campo estruturado do PNCP assume mm só quando o texto NÃO diz
    nada melhor; aqui a unidade já vem explícita (só que como palavra
    separada, não colada no número), e usar mm seria um erro de escala de
    1000x."""
    a = extrair_atributos("material: papel - comprimento: 96 metros - diametro: 11cm")
    achado = next(n for n in a.numericos if n.unidade == "m")
    assert achado.valor == 96.0
    assert not achado.inferido


def test_extrai_metros_com_texto_solto_sem_dois_pontos():
    a = extrair_atributos("papel contact transparente - rolo c/ 25 metros")
    achado = next(n for n in a.numericos if n.unidade == "m")
    assert achado.valor == 25.0


def test_par_de_dimensao_com_unidades_mistas_mm_e_m():
    """Caso real: 'CORRETIVO EM FITA 4,2MM X 12M' — um lado em mm, outro em
    metros (m bem maior que mm, unidades bem diferentes de escala)."""
    a = extrair_atributos("corretivo em fita 4,2mm x 12m")
    achados = {(n.unidade, n.valor) for n in a.numericos}
    assert ("mm", 4.2) in achados
    assert ("m", 12.0) in achados


def test_3m_marca_nao_vira_3_metros():
    """Caso real (auditoria em produção): 'Bloco Adesivo Post-it 38mm 50mm
    ... 2267 3M - 4BL' estava lendo a MARCA "3M" (fabricante — onipresente em
    fita/adesivo) como um atributo m=3.0, gerando pendência de "medida
    incompatível" sem sentido nenhum. Recorreu em vários produtos/editais
    reais diferentes. "3 metros"/"3m" como medida de verdade não apareceu em
    nenhum caso real visto até agora — a exclusão é só do token exato "3m"
    colado, não de "m" em geral (12m, 20m, "3 metros" por extenso continuam
    batendo, ver testes acima)."""
    a = extrair_atributos("Bloco Adesivo Post-it 38mm 50mm tropical 50 Folhas 2267 3M - 4BL")
    unidades = {n.unidade for n in a.numericos}
    assert "m" not in unidades
    assert ("mm", 38.0) in {(n.unidade, n.valor) for n in a.numericos}
    assert ("mm", 50.0) in {(n.unidade, n.valor) for n in a.numericos}


def test_3m_marca_dentro_de_cadeia_de_dimensao_tambem_nao_conta():
    a = extrair_atributos("fita dupla face 38mm x 50mm x 3m marca 3M")
    achados = {(n.unidade, n.valor) for n in a.numericos}
    assert ("m", 3.0) not in achados


def test_unidade_m_nao_confunde_com_taxa_m_por_min():
    """Caso real: 'VELOCIDADE DE FRAGMENTACAO: 2 M/MIN' é uma TAXA (2 metros
    por minuto), não um comprimento de 2 metros — '/' não é \\w, então a
    proteção geral (?!\\w) sozinha não bloqueava isso."""
    a = extrair_atributos("velocidade de fragmentacao: 2 m/min")
    assert not any(n.unidade == "m" and n.valor == 2.0 for n in a.numericos)


def test_extrai_trio_de_dimensao_comprimento_largura_altura():
    """Caso real: 'DIMENSOES (COMPR. X LARG. X ALT.) 28 X 16 X 7CM' — trio de
    dimensão (não só par), unidade só no último número valendo pros três."""
    a = extrair_atributos("dimensoes (compr. x larg. x alt.) 28 x 16 x 7cm")
    achados = {(n.unidade, n.valor) for n in a.numericos}
    assert {("cm", 28.0), ("cm", 16.0), ("cm", 7.0)} <= achados
    assert all(not n.inferido for n in a.numericos if n.unidade == "cm")


def test_validacao_de_trio_de_dimensao_bate_exato_sem_pendencia():
    item = "grampeador metalico - dimensoes 28 x 16 x 7cm"
    produto = "grampeador de mesa - dimensoes 28cm x 16cm x 7cm"
    r = validar(item, produto)
    assert not r.pendencias
    assert r.atende


def test_extrai_digitos_com_rotulo_antes_do_numero():
    """Caso real: alguns editais descrevem primeiro o rótulo, depois o valor
    ('dígitos: 12'), ao contrário da ordem natural ('12 dígitos'). Sem
    reconhecer essa ordem invertida, o requisito de dígitos não era
    extraído — o mesmo bug da calculadora, só que noutra redação."""
    a = extrair_atributos("calculadora eletronica numero digitos: 12, tipo: mesa")
    achado = next(n for n in a.numericos if n.unidade == "digitos")
    assert achado.valor == 12.0
    assert not achado.inferido


def test_extrai_tensao_com_rotulo_antes_do_numero():
    a = extrair_atributos("fonte alimentacao: bateria, tensao: 12")
    achado = next(n for n in a.numericos if n.unidade == "volts")
    assert achado.valor == 12.0
    assert not achado.inferido


def test_validacao_reprova_calculadora_com_rotulo_invertido():
    """Mesmo caso real do teste de 'dígitos com rótulo antes do número',
    ponta a ponta: item redigido com rótulo antes do número tem que
    reprovar produto de 8 dígitos igual reprovaria na ordem natural."""
    item = ("calculadora eletronica numero digitos: 12, tipo: mesa, "
            "aplicacao: financeira, fonte alimentacao: bateria, tensao: 12, "
            "caracteristicas adicionais: sem impressao")
    produto = "calculadora de mesa 8 digitos vx1385"
    r = validar(item, produto)
    assert not r.atende
    assert any(p.tipo == "numerico" and p.critico and "digitos" in p.descricao for p in r.criticas)


def test_bruto_de_valor_inferido_nao_mostra_unidade_inventada():
    """Caso real: 'comprimento: 13, largura: 4, altura: 2,3' (apagador de
    quadro branco) virava, na mensagem pro usuário, 'item exige 13 mm x 4mm
    x 2,3mm' — o 'mm' foi uma suposição interna (nunca esteve no texto),
    mas aparecia como se fosse. Contraditório com o próprio aviso "(unidade
    assumida, não informada no texto)" logo depois. bruto tem que trazer só
    o número que de fato apareceu no edital."""
    a = extrair_atributos("comprimento: 13, largura: 4, altura: 2,3")
    for n in a.numericos:
        assert n.inferido
        assert n.bruto in ("13", "4", "2,3")   # sem "mm" grudado


def test_validacao_de_grupo_inferido_nao_mostra_unidade_inventada_na_mensagem():
    item = "apagador quadro branco - comprimento: 13, largura: 4, altura: 2,3"
    produto = "apagador para quadro branco base plastica"
    r = validar(item, produto)
    assert len(r.pendencias) == 1
    msg = r.pendencias[0].descricao
    assert "13 x 4 x 2,3" in msg
    assert "mm" not in msg   # não inventa unidade na frente do usuário
    assert "unidade assumida" in msg   # mas deixa claro que precisa confirmar


def test_itens_por_unidade_corrige_folhas_de_embalagem_maior():
    """Caso real (auditoria em produção): 'Caixa Papel A4 75g 500 Folhas ...
    - 10 Resmas' tinha o texto descrevendo a contagem por RESMA (500), não
    da caixa inteira (5000) — um item pedindo 5.000 folhas reprovava esse
    produto por engano, mesmo sendo exatamente o mesmo produto. Com
    itens_por_unidade (campo estruturado do catálogo, sempre o TOTAL de
    folhas soltas na unidade vendida) informado, a crítica falsa some."""
    item = "Papel A4 caixa com 10 resmas contendo 5.000 folhas cada, gramatura: 75"
    produto = "Caixa Papel A4 75g 500 Folhas Branco Chamex - 10 Resmas"

    sem_correcao = validar(item, produto)
    assert any("folhas" in p.descricao for p in sem_correcao.criticas)

    com_correcao = validar(item, produto, itens_por_unidade=5000.0)
    assert not com_correcao.criticas


def test_itens_por_unidade_nao_mexe_quando_e_menor_que_o_valor_do_texto():
    """itens_por_unidade menor que o valor extraído do texto conta uma
    granularidade DIFERENTE (ex.: bloco adesivo vendido em pacote de 4
    BLOCOS, itens_por_unidade=4, cada bloco com 50 folhas) — não dá pra
    saber com segurança o total de folhas só com esse número, então o
    comportamento tem que continuar o mesmo de antes (comparação normal)."""
    item = "Bloco adesivo numero de folhas: 400 folhas"
    produto = "Bloco Adesivo Post-it 38mm 50mm tropical 50 Folhas 2267 3M - 4BL"

    sem = validar(item, produto)
    com = validar(item, produto, itens_por_unidade=4.0)
    assert [p.descricao for p in sem.criticas] == [p.descricao for p in com.criticas]


def test_validacao_nao_pareia_numeros_soltos_nao_relacionados_da_mesma_familia():
    """Caso real (edital 56531, item 19): a descrição menciona 'lâmina reta
    c/ cerca de 7 cm' (medida da lâmina) e, mais adiante, cita outro item de
    referência entre parênteses '(10,8 cm)' — dois números 'cm' soltos em
    frases diferentes, sem relação nenhuma entre si (não é uma dimensão tipo
    "7 x 10,8"). Antes desse fix, os dois caíam na mesma família
    'comprimento' e eram tratados como PAR de dimensão (mesmo mecanismo do
    "38 x 51"), comparando um produto real de 21cm contra um "requisito"
    inventado de 7x10,8cm que nunca existiu no edital — reprovava um produto
    que na real atendia. Achado ao investigar uma inconsistência relatada
    pelo usuário: a IA (Gemini) tinha justificado 'atende' (21cm ~ 20cm),
    mas a validação técnica determinística dizia 'Não atende'."""
    item = ("Tesoura material: aço inoxidável, comprimento: cerca de 20, "
           "características adicionais: lâmina reta c/ cerca de 7 cm, ponta "
           "arredondada Tesoura escolar sem ponta (10,8 cm), aço inoxidável, "
           "cabo de polipropileno, ponta arredondada")
    produto = "Tesoura Inox Multiuso 21cm Brw"
    r = validar(item, produto)
    assert not r.criticas
    assert any("múltiplos valores" in p.descricao for p in r.avisos)


def test_validacao_ainda_reprova_valor_solto_unico_que_nao_bate():
    """A correção acima não pode virar carta-branca: um ÚNICO valor solto
    (sem ambiguidade nenhuma) continua reprovando normalmente quando não
    bate — só o caso de MÚLTIPLOS valores soltos conflitantes vira aviso."""
    item = "Régua com 30 cm de comprimento, material acrílico"
    produto = "Régua Acrílica Transparente 15cm Waleu"
    r = validar(item, produto)
    assert not r.atende
    assert any(p.critico for p in r.criticas)


def test_itens_por_unidade_nao_afeta_unidades_que_nao_sao_contagem_de_pecas():
    """itens_por_unidade não pode "vazar" pra unidades tipo dígitos/volts —
    ali o número só diz quantos produtos vêm na caixa, sem relação nenhuma
    com a especificação técnica (ex.: calculadora vendida em caixa com 10
    unidades não vira "10 dígitos" por causa disso)."""
    item = "Calculadora numero digitos: 12"
    produto = "Calculadora de Mesa 8 Digitos VX1385"

    sem = validar(item, produto)
    com = validar(item, produto, itens_por_unidade=10.0)
    assert [p.descricao for p in sem.criticas] == [p.descricao for p in com.criticas]
    assert any("digitos" in p.descricao for p in com.criticas)
