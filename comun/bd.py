"""Conexión a PostgreSQL (psycopg 3, SQL explícito)."""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


@contextmanager
def conectar(database_url: str, autocommit: bool = False):
    """Conexión con filas como diccionarios.

    Sin autocommit: confirma al salir sin error y deshace si falla.
    Con autocommit: cada `with con.transaction():` es una transacción real (lo usa el recolector).
    """
    con = psycopg.connect(database_url, row_factory=dict_row, autocommit=autocommit)
    try:
        yield con
        if not autocommit:
            con.commit()
    except Exception:
        if not autocommit:
            con.rollback()
        raise
    finally:
        con.close()
