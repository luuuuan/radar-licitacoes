"""
Teste comparativo: validação técnica por REGEX (matching/validacao.py) vs. por
IA (Gemini, pedindo julgamento estruturado em JSON) — mesmos casos, lado a
lado, pra medir se o Gemini melhora a qualidade do julgamento em casos que o
regex acerta, erra ou nem tenta (categoria fora do vocabulário hardcoded).

100% um experimento, não mexe em nada do app principal. Nada aqui é chamado
por main.py/service.py.

Requer GEMINI_API_KEY (chave grátis do próprio usuário, mesma usada pelo
resto do app). Rodar (a partir de backend/):

    GEMINI_API_KEY=sua_chave python -m scripts.teste_validacao_ia

Sem a chave, o script ainda roda e mostra só o lado regex (indica que a IA
foi pulada), pra não travar quem só quer conferir o lado local.
"""
from __future__ import annotations
import json
import os

import requests

from app.matching.validacao import validar, classificar

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_MODELO = os.environ.get("IA_MODELO_TESTE", "gemini-3.5-flash")

_PROMPT = """Você está comparando um ITEM de edital de licitação pública brasileira com um
PRODUTO candidato do catálogo de um fornecedor, para decidir se o produto
atende tecnicamente ao que o item exige. Isso é só a checagem TÉCNICA (specs,
medidas, formato, características) — não é sobre preço nem sobre se é "o
mesmo tipo de coisa" (isso já foi decidido antes; assuma que sim).

Regras:
- Pendência CRÍTICA: o produto claramente NÃO cumpre uma exigência técnica
  objetiva do item (número insuficiente, formato/modelo incompatível,
  característica exigida contradita explicitamente no texto do produto).
  Isso reprova o produto (classificação "Não atende").
- Pendência de AVISO: a exigência existe no item, mas a descrição do produto
  simplesmente NÃO MENCIONA aquele atributo — isso não é prova de que o
  produto não tem, só falta confirmar manualmente. Não reprova sozinho
  (classificação "Atende parcialmente" se só houver avisos).
- "Atende": todas as exigências do item foram confirmadas no texto do
  produto, sem nenhuma pendência.
- NÃO invente exigência que não está no texto do item, nem suponha que o
  produto tem um atributo que a descrição não menciona.

Responda APENAS com um JSON válido (sem texto fora do JSON, sem ```), com
exatamente esta estrutura:
{{
  "classificacao": "Atende" | "Atende parcialmente" | "Não atende",
  "criticas": [lista de strings, uma frase curta por pendência crítica],
  "avisos": [lista de strings, uma frase curta por pendência de aviso],
  "raciocinio": "1-2 frases explicando a decisão"
}}

ITEM DO EDITAL:
\"\"\"{item}\"\"\"

PRODUTO CANDIDATO:
\"\"\"{produto}\"\"\""""


def _gemini_validar(item_texto: str, produto_texto: str, api_key: str) -> dict | None:
    url = f"{_BASE}/{_MODELO}:generateContent"
    body = {
        "contents": [{"parts": [{"text": _PROMPT.format(item=item_texto, produto=produto_texto)}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    try:
        r = requests.post(url, json=body, timeout=60,
                          headers={"x-goog-api-key": api_key, "Content-Type": "application/json"})
    except requests.RequestException as e:
        print(f"    [erro de rede: {e}]")
        return None
    if r.status_code != 200:
        print(f"    [Gemini HTTP {r.status_code}: {r.text[:200]}]")
        return None
    try:
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(txt)
    except (ValueError, KeyError, IndexError):
        print(f"    [resposta da IA não é JSON válido: {r.text[:200]}]")
        return None


# nome do caso, descrição do ITEM, descrição do PRODUTO candidato
CASOS = [
    ("specs batem (regex já acerta)",
     "Impressora bivolt, no mínimo 30 ppm",
     "Impressora bivolt (110/220v), 32 ppm"),

    ("capacidade insuficiente (regex já acerta)",
     "Bandeja com capacidade para no mínimo 250 folhas",
     "Bandeja com capacidade para 150 folhas"),

    ("grampo incompatível (regex já acerta)",
     "Grampeador de mesa, capacidade mínima 20 folhas, grampos 26/6",
     "Grampeador tipo alicate, capacidade 100 folhas, grampos 23/13"),

    ("separador de milhar (bug que a gente corrigiu no regex)",
     "Bandeja com capacidade para no mínimo 2.500 folhas",
     "Bandeja com capacidade para 300 folhas"),

    ("dual-mode com fio E sem fio (bug de ordem que a gente corrigiu)",
     "Impressora sem fio",
     "Impressora com conectividade sem fio (wi-fi) e também com fio (ethernet), para maior flexibilidade"),

    ("conversão de unidade que o regex NÃO entende",
     "Monitor com no mínimo 24 polegadas de tela",
     "Monitor com tela de 61 cm (medida diagonal)"),

    ("categoria fora do vocabulário do regex: móveis",
     "Mesa de escritório em MDF, medindo 1,20m x 0,60m, com 2 gavetas com chave",
     "Mesa em MDF 1,20 x 0,60m, com apenas 1 gaveta, sem chave"),

    ("categoria fora do vocabulário do regex: veículo",
     "Veículo tipo van, motor a diesel, capacidade mínima para 15 passageiros, ar-condicionado",
     "Van modelo XYZ, motor 2.8 diesel, 12 lugares, com ar-condicionado"),
]


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("Aviso: GEMINI_API_KEY não definida — mostrando só o resultado do regex "
              "(rode com GEMINI_API_KEY=sua_chave pra comparar com a IA).\n")

    for nome, item_texto, produto_texto in CASOS:
        print("=" * 100)
        print(f"CASO: {nome}")
        print(f"  ITEM:    {item_texto}")
        print(f"  PRODUTO: {produto_texto}")

        r = validar(item_texto, produto_texto)
        classificacao_regex = classificar(0.8, r)  # score fixo: isolando a qualidade da validação, não do matching
        print(f"\n  [REGEX] verificavel={r.verificavel} -> {classificacao_regex}")
        for p in r.criticas:
            print(f"    CRÍTICA: {p.descricao}")
        for p in r.avisos:
            print(f"    AVISO:   {p.descricao}")

        if api_key:
            resp = _gemini_validar(item_texto, produto_texto, api_key)
            if resp:
                print(f"\n  [GEMINI] -> {resp.get('classificacao')}")
                for c in resp.get("criticas") or []:
                    print(f"    CRÍTICA: {c}")
                for a in resp.get("avisos") or []:
                    print(f"    AVISO:   {a}")
                print(f"    raciocínio: {resp.get('raciocinio')}")
        print()


if __name__ == "__main__":
    main()
