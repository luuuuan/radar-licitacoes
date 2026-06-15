"""
Executa a primeira coleta manualmente (sem esperar o agendador).

Uso (dentro do container):
    python -m scripts.primeira_coleta
"""
from app.database import SessionLocal, init_db
from app.service import processar_coleta


def main():
    init_db()
    db = SessionLocal()
    try:
        resumo = processar_coleta(db)
        print("✓ Coleta concluída:", resumo)
    finally:
        db.close()


if __name__ == "__main__":
    main()
