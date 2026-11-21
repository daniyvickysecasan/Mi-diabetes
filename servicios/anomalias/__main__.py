"""Línea de comandos del servicio de anomalías (fase 4).

  python -m servicios.anomalias ciclo                 # una pasada: detecta, guarda y avisa
  python -m servicios.anomalias servicio              # bucle cada INTERVALO_ANOMALIAS_S (5 min)
  python -m servicios.anomalias probar-aviso          # manda un aviso de prueba a ntfy
  python -m servicios.anomalias evaluar --desde 2026-11-21 --en-vivo   # criterio de la fase 4
  python -m servicios.anomalias evaluar --desde 2026-05-01 [--hasta ...] # reglas sobre el histórico
  python -m servicios.anomalias umbrales              # umbrales en uso (ANOMALIA_* del .env)
  python -m servicios.anomalias claves-push           # genera VAPID_PUBLICA y VAPID_PRIVADA para el .env
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


def cliente_webpush(aj: Ajustes) -> A.ClienteWebPush | None:
    if not aj.vapid_privada or not aj.vapid_publica or not aj.url_app:
        return None
    return A.ClienteWebPush(aj.vapid_privada, aj.url_app)


def claves_push() -> dict:
    """Par de claves VAPID (P-256). La pública va a la PWA; la privada es un secreto del .env."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01, b64urlencode
    v = Vapid01()
    v.generate_keys()
    publica = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    privada = v.private_key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption())
    return {"VAPID_PUBLICA": b64urlencode(publica), "VAPID_PRIVADA": b64urlencode(privada)}


def ciclo(con, aj: Ajustes, u: R.Umbrales, ntfy: A.ClienteNtfy | None, ahora: datetime | None = None,
          webpush: A.ClienteWebPush | None = None) -> dict:
    ahora = ahora or datetime.now(timezone.utc)
    try:
        r = D.ciclo(con, ahora, u)
        r["avisos"] = A.enviar_pendientes(con, ntfy, ahora, u, aj.url_app, webpush, aj.avisos_ntfy)
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
    sub.add_parser("claves-push")
    ev = sub.add_parser("evaluar")
    ev.add_argument("--desde", type=_fecha, required=True)
    ev.add_argument("--hasta", type=_fecha)
    ev.add_argument("--en-vivo", action="store_true", help="avisos respondidos + episodios que se escaparon")
    a = p.parse_args(argv)

    aj = Ajustes.desde_entorno()
    u = R.Umbrales.desde_entorno()
    if a.orden == "umbrales":
        _imprimir(u.como_dict())
        return 0
    if a.orden == "claves-push":
        for k, v in claves_push().items():
            print(f"{k}={v}")
        return 0
    ntfy = cliente_ntfy(aj)
    webpush = cliente_webpush(aj)
    if a.orden == "probar-aviso":
        if ntfy is None and webpush is None:
            print("No hay ningún canal: faltan NTFY_* o VAPID_* en el .env")
            return 1
        if ntfy is not None:
            ntfy.publicar("Mi Diabetes", "Aviso de prueba por ntfy.", False,
                          f"{aj.url_app}/#avisos" if aj.url_app else None)
            print("ntfy: enviado")
        if webpush is not None:
            with conectar(aj.database_url, autocommit=True) as con:
                ok, err = A._por_webpush(con, webpush, {"id": 0, "titulo": "Mi Diabetes", "texto": "Aviso de prueba en la app.",
                                                        "pregunta": "Si lo ves, Web Push funciona.", "urgente": False},
                                         datetime.now(timezone.utc))
            print(f"Web Push: {ok} dispositivo(s)" + (f"; errores: {err}" if err else ""))
        return 0
    if a.orden == "evaluar":
        with conectar(aj.database_url, autocommit=True) as con:
            f = E.evaluar_en_vivo if a.en_vivo else E.evaluar
            _imprimir(f(con, a.desde, a.hasta or datetime.now(timezone.utc), u))
        return 0
    if a.orden == "ciclo":
        with conectar(aj.database_url, autocommit=True) as con:
            _imprimir(ciclo(con, aj, u, ntfy, webpush=webpush))
        return 0
    parar = {"si": False}
    signal.signal(signal.SIGTERM, lambda *_: parar.update(si=True))
    log.info("Anomalías cada %s s; Web Push %s; ntfy %s (%s)", aj.intervalo_anomalias_s,
             "configurado" if webpush else "SIN CONFIGURAR", "configurado" if ntfy else "SIN CONFIGURAR", aj.avisos_ntfy)
    while not parar["si"]:
        t0 = time.monotonic()
        try:
            with conectar(aj.database_url, autocommit=True) as con:
                r = ciclo(con, aj, u, ntfy, webpush=webpush)
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
