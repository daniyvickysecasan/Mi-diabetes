"""Línea de comandos del recolector.

  python -m servicios.recoleccion historico [--desde 2020-01-01] [--hasta ...]
  python -m servicios.recoleccion ciclo          # una pasada incremental
  python -m servicios.recoleccion servicio       # bucle cada INTERVALO_RECOLECCION_S (5 min)
  python -m servicios.recoleccion verificar --desde 2025-01-01 [--hasta ...]
  python -m servicios.recoleccion huecos [--dias 30]
"""
import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

from comun.ajustes import Ajustes
from comun.bd import conectar
from servicios.recoleccion.nightscout import ClienteNightscout
from servicios.recoleccion.recolector import (FUENTE, ahora_utc, ciclo_incremental, detectar_huecos,
                                             importar, verificar)

log = logging.getLogger("recoleccion")


def _fecha(texto: str) -> datetime:
    dt = datetime.fromisoformat(texto)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _imprimir(r) -> None:
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx registra cada URL completa, y la URL lleva el token: nunca a INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="recoleccion")
    sub = p.add_subparsers(dest="orden", required=True)
    h = sub.add_parser("historico", help="importa todo el histórico (reanudable e idempotente)")
    h.add_argument("--desde", type=_fecha, default=_fecha("2015-01-01"))
    h.add_argument("--hasta", type=_fecha)
    sub.add_parser("ciclo", help="una pasada incremental")
    sub.add_parser("servicio", help="bucle incremental cada INTERVALO_RECOLECCION_S")
    v = sub.add_parser("verificar", help="compara Nightscout con la base")
    v.add_argument("--desde", type=_fecha, required=True)
    v.add_argument("--hasta", type=_fecha)
    g = sub.add_parser("huecos", help="recalcula huecos de los últimos días")
    g.add_argument("--dias", type=int, default=30)
    a = p.parse_args(argv)

    aj = Ajustes.desde_entorno()
    if a.orden == "huecos":
        with conectar(aj.database_url, autocommit=True) as con:
            _imprimir(detectar_huecos(con, ahora_utc() - timedelta(days=a.dias), ahora_utc(), aj.hueco_min))
            return 0

    ns = ClienteNightscout(aj.nightscout_url, aj.nightscout_token)
    if a.orden == "historico":
        with conectar(aj.database_url, autocommit=True) as con:
            _imprimir(importar(con, ns, a.desde, a.hasta or ahora_utc(), "historico", hueco_min=aj.hueco_min))
    elif a.orden == "ciclo":
        with conectar(aj.database_url, autocommit=True) as con:
            _imprimir(ciclo_incremental(con, ns, aj))
    elif a.orden == "verificar":
        with conectar(aj.database_url, autocommit=True) as con:
            r = verificar(con, ns, a.desde, a.hasta or ahora_utc())
            _imprimir(r)
            return 0 if r["ok"] else 1
    elif a.orden == "servicio":
        parar = {"si": False}
        signal.signal(signal.SIGTERM, lambda *_: parar.update(si=True))
        log.info("Recolector de %s cada %s s", FUENTE, aj.intervalo_recoleccion_s)
        while not parar["si"]:
            t0 = time.monotonic()
            try:
                with conectar(aj.database_url, autocommit=True) as con:
                    r = ciclo_incremental(con, ns, aj)
                log.info("ciclo: %s lecturas nuevas, %s tratamientos nuevos, %s huecos abiertos",
                         r["glucosa"].get("insertadas", 0), r["tratamientos"].get("insertados", 0),
                         r["huecos"]["abiertos"])
            except Exception as e:  # un fallo de red no para el servicio
                log.error("ciclo fallido: %s", e)
            espera = max(5.0, aj.intervalo_recoleccion_s - (time.monotonic() - t0))
            while espera > 0 and not parar["si"]:
                time.sleep(min(1.0, espera))
                espera -= 1.0
    return 0


if __name__ == "__main__":
    sys.exit(main())
