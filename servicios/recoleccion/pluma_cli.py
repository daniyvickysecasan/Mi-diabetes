"""Línea de comandos del recolector de la pluma (web de LibreView → base → Nightscout).

  python -m servicios.recoleccion.pluma_cli servicio           # bucle: SMS desde la app + informe cada INTERVALO_INFORME_S (4 h)
  python -m servicios.recoleccion.pluma_cli ciclo              # una vuelta (últimos 14 días, como la web)
  python -m servicios.recoleccion.pluma_cli historico [--dias 14]
  python -m servicios.recoleccion.pluma_cli retraso [--dias 7] # retraso inyección → visto, y frente a la app

La primera vez LibreView pide el código del SMS: la app muestra el aviso con el botón "Enviarme el SMS".
"""
import argparse
import json
import logging
import signal
import sys
import time

from comun.ajustes import Ajustes
from comun.bd import conectar
from servicios.recoleccion import pluma, sondeo_libreview
from servicios.recoleccion.libreview import ClienteLibreView
from servicios.recoleccion.nightscout import EscritorNightscout

log = logging.getLogger("pluma")


def _imprimir(r) -> None:
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))


def _cliente(aj: Ajustes) -> ClienteLibreView:
    return ClienteLibreView(aj.libreview_email, aj.libreview_password, aj.libreview_region,
                            ruta_sesion=aj.libreview_sesion, zona=aj.zona_horaria,
                            version_web=aj.libreview_version)


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
    sub.add_parser("ciclo", help="una vuelta")
    sub.add_parser("servicio", help="bucle continuo")
    h = sub.add_parser("historico", help="importa los últimos días (sin medir retraso)")
    h.add_argument("--dias", type=int, default=14)
    r = sub.add_parser("retraso", help="informe de retraso")
    r.add_argument("--dias", type=int, default=7)
    a = p.parse_args(argv)
    aj = Ajustes.desde_entorno()

    if a.orden == "retraso":
        with conectar(aj.database_url, autocommit=True) as con:
            _imprimir(pluma.informe_retraso(con, a.dias))
        return 0
    lv, escritor = _cliente(aj), _escritor(aj)
    if a.orden in ("ciclo", "historico"):
        dias, modo = (a.dias, "historico") if a.orden == "historico" else (14, "incremental")
        with conectar(aj.database_url, autocommit=True) as con:
            _imprimir(sondeo_libreview.vuelta(con, lv, escritor, dias=dias, modo=modo))
        return 0

    parar = {"si": False}
    signal.signal(signal.SIGTERM, lambda *_: parar.update(si=True))
    log.info("Recolector de la pluma: mira la base cada %s s y pide el informe cada %s s",
             aj.intervalo_pluma_s, aj.intervalo_informe_s)
    ultimo_informe = float("-inf")
    while not parar["si"]:
        toca = time.monotonic() - ultimo_informe >= aj.intervalo_informe_s
        try:
            with conectar(aj.database_url, autocommit=True) as con:
                res = sondeo_libreview.vuelta(con, lv, escritor, sincronizar=toca)
            if res["accion"] in ("sincronizado", "error"):
                ultimo_informe = time.monotonic()    # tras un error también se espera un intervalo
            if res["accion"] == "sincronizado":
                log.info("informe: %s dosis nuevas, %s horas con varias inyecciones, %s enviadas a Nightscout",
                         res["dosis"]["insertadas"], res["dosis"].get("horas_ambiguas", 0),
                         res["nightscout"].get("enviadas", 0))
        except Exception as e:   # base caída o fallo inesperado: se registra y se sigue
            log.error("vuelta fallida: %s", e)
            ultimo_informe = time.monotonic()
        espera = float(aj.intervalo_pluma_s)
        while espera > 0 and not parar["si"]:
            time.sleep(min(1.0, espera))
            espera -= 1.0
    return 0


if __name__ == "__main__":
    sys.exit(main())
