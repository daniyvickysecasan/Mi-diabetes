"""Aplica las migraciones SQL pendientes, en orden, cada una en su transacción.

Uso:  python -m comun.migrar            (usa DATABASE_URL)
"""
import sys
from pathlib import Path

import psycopg

from comun.ajustes import Ajustes

CARPETA = Path(__file__).parent / "migraciones"


def pendientes(con) -> list[Path]:
    con.execute("""CREATE TABLE IF NOT EXISTS migraciones_aplicadas (
                       nombre text PRIMARY KEY, aplicada_en timestamptz NOT NULL DEFAULT now())""")
    hechas = {r[0] for r in con.execute("SELECT nombre FROM migraciones_aplicadas")}
    return [p for p in sorted(CARPETA.glob("*.sql")) if p.name not in hechas]


def migrar(database_url: str) -> list[str]:
    aplicadas = []
    with psycopg.connect(database_url, autocommit=True) as con:
        con.execute("SELECT pg_advisory_lock(727274)")   # dos procesos a la vez no migran dos veces
        try:
            for ruta in pendientes(con):
                with con.transaction():
                    con.execute(ruta.read_text(encoding="utf-8"))
                    con.execute("INSERT INTO migraciones_aplicadas (nombre) VALUES (%s)", (ruta.name,))
                aplicadas.append(ruta.name)
        finally:
            con.execute("SELECT pg_advisory_unlock(727274)")
    return aplicadas


if __name__ == "__main__":
    hechas = migrar(Ajustes.desde_entorno().database_url)
    print("Migraciones aplicadas: " + (", ".join(hechas) if hechas else "ninguna (al día)"))
    sys.exit(0)
