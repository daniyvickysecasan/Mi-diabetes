"""Error de los modelos de comidas frente a las comidas de referencia (pesadas o con etiqueta).

  python -m herramientas.evaluar_comidas          # usa DATABASE_URL

Criterio de la fase 3: con 20 comidas de referencia, error medio de hidratos ≤ 5 g en platos
simples (un alimento) y ≤ 15 g en compuestos. Solo lee la base; no escribe nada.
"""
import os
import sys

import psycopg
from psycopg.rows import dict_row

LIMITES = {"simple": 5.0, "compuesto": 15.0}
MINIMO = 20


def evaluar(filas: list[dict]) -> dict:
    grupos = {"simple": [], "compuesto": []}
    for f in filas:
        if f["hidratos_modelo_g"] is None or f["hidratos_bolo_g"] is None:
            continue
        clase = "simple" if len(f["items"] or []) <= 1 else "compuesto"
        grupos[clase].append(abs(float(f["hidratos_modelo_g"]) - float(f["hidratos_bolo_g"])))
    res = {k: {"n": len(v), "error_medio_g": round(sum(v) / len(v), 1) if v else None,
               "ok": bool(v) and sum(v) / len(v) <= LIMITES[k]} for k, v in grupos.items()}
    n = sum(r["n"] for r in res.values())
    res["total"] = {"n": n, "suficientes": n >= MINIMO,
                    "aceptada": n >= MINIMO and all(r["ok"] or r["n"] == 0 for k, r in res.items() if k in LIMITES)}
    return res


def main() -> int:
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as con:
        filas = con.execute("""SELECT hidratos_modelo_g, hidratos_bolo_g, items FROM comidas
                               WHERE referencia AND revisada""").fetchall()
    r = evaluar(filas)
    for k in LIMITES:
        g = r[k]
        print(f"{k:10} {g['n']:3} comidas · error medio {g['error_medio_g']} g (límite {LIMITES[k]} g)")
    t = r["total"]
    print(f"\n{t['n']} de {MINIMO} comidas de referencia · fase 3 {'ACEPTADA' if t['aceptada'] else 'aún no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
