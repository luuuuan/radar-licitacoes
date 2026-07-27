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


def test_operador_nao_vaza_de_uma_clausula_para_a_proxima():
    a = extrair_atributos("no mínimo 30 ppm, 250 folhas")
    achado_folhas = next(n for n in a.numericos if n.unidade == "folhas")
    assert achado_folhas.valor == 250
    assert achado_folhas.operador == "=="


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
    assert classificar(0.8, r) == "Atende"
