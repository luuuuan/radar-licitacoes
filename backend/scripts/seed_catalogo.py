"""
Cadastro inicial do catálogo a partir de um CSV.

Uso (dentro do container):
    python -m scripts.seed_catalogo scripts/catalogo_exemplo.csv

Formato do CSV (separador ;):
    descricao;ncm;cest;ean;catmat;catser;palavras_chave
"""
import csv
import sys

from app.database import SessionLocal, init_db
from app.models import Produto


def main(caminho: str):
    init_db()
    db = SessionLocal()
    n = 0
    try:
        with open(caminho, encoding="utf-8-sig") as f:
            leitor = csv.DictReader(f, delimiter=";")
            for linha in leitor:
                if not (linha.get("descricao") or "").strip():
                    continue
                db.add(Produto(
                    descricao=linha["descricao"].strip(),
                    ncm=(linha.get("ncm") or "").strip() or None,
                    cest=(linha.get("cest") or "").strip() or None,
                    ean=(linha.get("ean") or "").strip() or None,
                    catmat=(linha.get("catmat") or "").strip() or None,
                    catser=(linha.get("catser") or "").strip() or None,
                    palavras_chave=(linha.get("palavras_chave") or "").strip() or None,
                ))
                n += 1
        db.commit()
        print(f"✓ {n} produto(s) importado(s).")
    finally:
        db.close()


if __name__ == "__main__":
    caminho = sys.argv[1] if len(sys.argv) > 1 else "scripts/catalogo_exemplo.csv"
    main(caminho)
