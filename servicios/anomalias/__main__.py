"""Línea de comandos del servicio de anomalías (fase 4).

  python -m servicios.anomalias ciclo                 # una pasada: detecta, guarda y avisa
  python -m servicios.anomalias servicio              # bucle cada INTERVALO_ANOMALIAS_S (5 min)
  python -m servicios.anomalias probar-aviso          # manda un aviso de prueba a ntfy
  python -m servicios.anomalias evaluar --desde 2026-05-01 [--hasta ...]
  python -m servicios.anomalias umbrales              # umbrales en uso (ANOMALIA_* del .env)
"""
import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from comun.ajustes import Ajustes
from comun.bd import conectar
from servicios.anomalias import avisos as A
from servicios.anomalias import detector as D
from servicios.anomalias import evaluar as E
from servicios.anomalias import reglas as R

log = logging.getLogger("anomalias")


def _fecha(texto: str) -> datetime:
    dt = datetime.fromisoformat(texto)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _imprimir(r) -> None:
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))


def cliente_ntfy(aj: Ajustes) -> A.ClienteNtfy | None:
    if not aj.ntfy_url or not aj.ntfy_tema:
        return None
    return A.ClienteNtfy(aj.ntfy_url, aj.ntfy_tema, aj.ntfy_token)


def ciclo(con, aj: Ajustes, u: R.Umbrales, ntfy: A.ClienteNtfy | None, ahora: datetime | None = None) -> dict:
    ahora = ahora or datetime.now(timezone.utc)
    try:
        r = D.ciclo(con, ahora, u)
        r["avisos"] = A.enviar_pendientes(con, ntfy, ahora, u, aj.url_app)
        con.execute("UPDATE estado_anomalias SET ultimo_ciclo = %s, ultimo_error = NULL", (ahora,))
        return r
    except Exception as e:
        con.execute("UPDATE estado_anomalias SET ultimo_ciclo = %s, ultimo_error = %s", (ahora, str(e)[:300]))
        raise


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="anomalias")
    sub = p.add_subparsers(dest="orden", required=True)
    sub.add_parser("ciclo")
    sub.add_parser("servicio")
    sub.add_parser("probar-aviso")
    sub.add_parser("umbrales")
    ev = sub.add_parser("evaluar")
    ev.add_argument("--desde", type=_fecha, required=True)
    ev.add_argument("--hasta", type=_fecha)
    a = p.parse_args(argv)

    aj = Ajustes.desde_entorno()
    u = R.Umbrales.desde_entorno()
    if a.orden == "umbrales":
        _imprimir(u.como_dict())
        return 0
    ntfy = cliente_ntfy(aj)
    if a.orden == "probar-aviso":
        if ntfy is None:
            print("Faltan NTFY_URL y NTFY_TEMA en el .env")
            return 1
        ntfy.publicar("Mi Diabetes", "Aviso de prueba: si lo ves en el iPhone, ntfy funciona.", False,
                      f"{aj.url_app}/#avisos" if aj.url_app else None)
        print("Enviado")
        return 0
    if a.orden == "evaluar":
        with conectar(aj.database_url, autocommit=True) as con:
            _imprimir(E.evaluar(con, a.desde, a.hasta or datetime.now(timezone.utc), u))
        return 0
    if a.orden == "ciclo":
        with conectar(aj.database_url, autocommit=True) as con:
            _imprimir(ciclo(con, aj, u, ntfy))
        return 0
    parar = {"si": False}
    signal.signal(signal.SIGTERM, lambda *_: parar.update(si=True))
    log.info("Anomalías cada %s s; ntfy %s", aj.intervalo_anomalias_s, "configurado" if ntfy else "SIN CONFIGURAR")
    while not parar["si"]:
        t0 = time.monotonic()
        try:
            with conectar(aj.database_url, autocommit=True) as con:
                r = ciclo(con, aj, u, ntfy)
            if r["nuevos"] or r["avisos"]["enviados"] or r["avisos"]["errores"]:
                log.info("ciclo: %s nuevas, avisos %s", len(r["nuevos"]), r["avisos"])
        except Exception as e:  # un fallo no para el servicio
            log.error("ciclo fallido: %s", e)
        espera = max(5.0, aj.intervalo_anomalias_s - (time.monotonic() - t0))
        while espera > 0 and not parar["si"]:
            time.sleep(min(1.0, espera))
            espera -= 1.0
    return 0


if __name__ == "__main__":
    sys.exit(main())
