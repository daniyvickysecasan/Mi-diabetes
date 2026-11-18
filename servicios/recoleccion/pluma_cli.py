"""Línea de comandos del recolector de la pluma (LibreView → base → Nightscout).

  python -m servicios.recoleccion.pluma_cli sondear            # forma de las respuestas, sin datos
  python -m servicios.recoleccion.pluma_cli ciclo              # una pasada (últimas SOLAPE_PLUMA_H horas)
  python -m servicios.recoleccion.pluma_cli servicio           # bucle cada INTERVALO_PLUMA_S
  python -m servicios.recoleccion.pluma_cli historico --dias 90
  python -m servicios.recoleccion.pluma_cli retraso [--dias 7] # retraso inyección → visto, y frente a la app
"""
import argparse
import json
import logging
import signal
import sys
import time
from datetime import timedelta

from comun.ajustes import Ajustes
from comun.bd import conectar
from servicios.recoleccion import pluma
from servicios.recoleccion.libreview import ClienteLibreView, sondear
from servicios.recoleccion.nightscout import EscritorNightscout

log = logging.getLogger("pluma")


def _imprimir(r) -> None:
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))


def _cliente(aj: Ajustes) -> ClienteLibreView:
    return ClienteLibreView(aj.libreview_email, aj.libreview_password, aj.libreview_region,
                            aj.libreview_version, zona=aj.zona_horaria)


def _escritor(aj: Ajustes):
    if not aj.nightscout_token_escritura:
        log.warning("Sin NIGHTSCOUT_TOKEN_ESCRITURA: las dosis se guardan en la base pero no se escriben en Nightscout")
        return None
    return EscritorNightscout(aj.nightscout_url, aj.nightscout_token_escritura)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)     # las URL llevan tokens
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="pluma")
    sub = p.add_subparsers(dest="orden", required=True)
    sub.add_parser("sondear", help="muestra la forma de las respuestas de LibreView (sin valores)")
    sub.add_parser("ciclo", help="una pasada")
    sub.add_parser("servicio", help="bucle cada INTERVALO_PLUMA_S")
    h = sub.add_parser("historico", help="importa los últimos días (sin medir retraso)")
    h.add_argument("--dias", type=int, default=90)
    r = sub.add_parser("retraso", help="informe de retraso")
    r.add_argument("--dias", type=int, default=7)
    a = p.parse_args(argv)
    aj = Ajustes.desde_entorno()

    if a.orden == "retraso":
        with conectar(aj.database_url, autocommit=True) as con:
            _imprimir(pluma.informe_retraso(con, a.dias))
        return 0
    lv = _cliente(aj)
    if a.orden == "sondear":
        _imprimir(sondear(lv))
        return 0
    escritor = _escritor(aj)
    if a.orden == "historico":
        ahora = pluma.ahora_utc()
        with conectar(aj.database_url, autocommit=True) as con:
            _imprimir(pluma.importar(con, lv, ahora - timedelta(days=a.dias), ahora, "historico", escritor))
    elif a.orden == "ciclo":
        with conectar(aj.database_url, autocommit=True) as con:
            _imprimir(pluma.ciclo(con, lv, aj.solape_pluma_h, escritor))
    elif a.orden == "servicio":
        parar = {"si": False}
        signal.signal(signal.SIGTERM, lambda *_: parar.update(si=True))
        log.info("Recolector de la pluma cada %s s", aj.intervalo_pluma_s)
        while not parar["si"]:
            t0 = time.monotonic()
            try:
                with conectar(aj.database_url, autocommit=True) as con:
                    res = pluma.ciclo(con, lv, aj.solape_pluma_h, escritor)
                log.info("ciclo: %s dosis nuevas, %s enviadas a Nightscout, %s errores de envío",
                         res["dosis"]["insertadas"], res["nightscout"].get("enviadas", 0),
                         res["nightscout"].get("errores", 0))
            except Exception as e:  # LibreView caído o cambiado: se registra y se sigue
                log.error("ciclo fallido: %s", e)
            espera = max(30.0, aj.intervalo_pluma_s - (time.monotonic() - t0))
            while espera > 0 and not parar["si"]:
                time.sleep(min(1.0, espera))
                espera -= 1.0
    return 0


if __name__ == "__main__":
    sys.exit(main())
