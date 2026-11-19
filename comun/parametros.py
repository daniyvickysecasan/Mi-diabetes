"""Versiones de parámetros (ratio y sensibilidad por franja, objetivos). Afecta a la dosis.

Reglas (CLAUDE.md):
  * Ningún cambio se activa sin aprobación: la tabla exige `aprobada_en` y `aprobada_por` para el
    estado 'activa' y solo admite una activa.
  * Los valores los da Daniel (o, en la fase 7, el ajuste semanal como propuesta). Nunca un modelo
    de lenguaje.

Formato de las franjas (hora local de Europe/Madrid, la primera empieza a las 00:00):
  ratio_hc:     [{"desde": "00:00", "g_por_u": 10}, {"desde": "12:00", "g_por_u": 12}]
  sensibilidad: [{"desde": "00:00", "mgdl_por_u": 40}]

Uso desde la línea de comandos (en el PC, con la aprobación escrita de Daniel):
  python -m comun.parametros listar
  python -m comun.parametros proponer --ratio 00:00=10,12:00=12 --sensibilidad 00:00=40 --motivo "..."
  python -m comun.parametros aprobar 1 --por Daniel
"""
import argparse
import json
import re
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

ZONA = ZoneInfo("Europe/Madrid")
PATRON_HORA = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
# Límites de cordura de los valores que se pueden guardar (no son límites de dosis: solo evitan
# erratas como 1 g/U o 400 mg/dL/U).
RATIO_MIN, RATIO_MAX = 2.0, 50.0
SENS_MIN, SENS_MAX = 5.0, 300.0
OBJETIVO_MIN, OBJETIVO_MAX = 80, 140


class ParametrosNoValidos(ValueError):
    pass


def _validar_franjas(franjas: list[dict], clave: str, minimo: float, maximo: float) -> list[dict]:
    if not franjas:
        raise ParametrosNoValidos(f"{clave}: hace falta al menos una franja")
    limpias = []
    for f in franjas:
        desde = str(f.get("desde", ""))
        if not PATRON_HORA.match(desde):
            raise ParametrosNoValidos(f"{clave}: hora no válida {desde!r} (formato HH:MM)")
        try:
            valor = float(f[clave])
        except (KeyError, TypeError, ValueError):
            raise ParametrosNoValidos(f"{clave}: falta el valor de la franja {desde}") from None
        if not minimo <= valor <= maximo:
            raise ParametrosNoValidos(f"{clave}: {valor} fuera de {minimo}–{maximo}")
        limpias.append({"desde": desde, clave: valor})
    horas = [f["desde"] for f in limpias]
    if horas[0] != "00:00":
        raise ParametrosNoValidos(f"{clave}: la primera franja debe empezar a las 00:00")
    if horas != sorted(horas) or len(set(horas)) != len(horas):
        raise ParametrosNoValidos(f"{clave}: las franjas deben ir en orden y sin repetir hora")
    return limpias


def valor_en(franjas: list[dict], clave: str, en: datetime) -> tuple[float, str]:
    """Valor de la franja que cubre la hora local de `en`, y la hora de inicio de esa franja."""
    hora = en.astimezone(ZONA).time()
    elegida = franjas[0]
    for f in franjas:
        if time.fromisoformat(f["desde"]) <= hora:
            elegida = f
    return float(elegida[clave]), elegida["desde"]


def proponer(con, ratio_hc: list[dict], sensibilidad: list[dict], motivo: str,
             objetivo_mgdl: int = 90, objetivo_correccion_mgdl: int = 110,
             confianza: str = "baja", origen: str = "manual") -> dict:
    ratio_hc = _validar_franjas(ratio_hc, "g_por_u", RATIO_MIN, RATIO_MAX)
    sensibilidad = _validar_franjas(sensibilidad, "mgdl_por_u", SENS_MIN, SENS_MAX)
    for o in (objetivo_mgdl, objetivo_correccion_mgdl):
        if not OBJETIVO_MIN <= o <= OBJETIVO_MAX:
            raise ParametrosNoValidos(f"objetivo {o} fuera de {OBJETIVO_MIN}–{OBJETIVO_MAX}")
    if objetivo_correccion_mgdl < objetivo_mgdl:
        raise ParametrosNoValidos("el objetivo de corrección no puede ser menor que el objetivo")
    if confianza not in ("baja", "media", "alta"):
        raise ParametrosNoValidos("confianza: baja, media o alta")
    if not (motivo or "").strip():
        raise ParametrosNoValidos("explica el motivo de la propuesta")
    return con.execute(
        """INSERT INTO versiones_parametros
             (version, estado, origen, ratio_hc, sensibilidad, objetivo_mgdl, objetivo_correccion_mgdl,
              confianza, motivo)
           VALUES ((SELECT coalesce(max(version), 0) + 1 FROM versiones_parametros), 'propuesta', %s,
                   %s, %s, %s, %s, %s, %s) RETURNING *""",
        (origen, json.dumps(ratio_hc), json.dumps(sensibilidad), objetivo_mgdl, objetivo_correccion_mgdl,
         confianza, motivo.strip())).fetchone()


def aprobar(con, id_: int, por: str, ahora: datetime | None = None) -> dict:
    """Activa una propuesta y retira la activa anterior, en una sola transacción."""
    ahora = ahora or datetime.now(timezone.utc)
    if not (por or "").strip():
        raise ParametrosNoValidos("indica quién aprueba")
    with con.transaction():
        v = con.execute("SELECT * FROM versiones_parametros WHERE id = %s FOR UPDATE", (id_,)).fetchone()
        if v is None:
            raise ParametrosNoValidos(f"no existe la versión {id_}")
        if v["estado"] != "propuesta":
            raise ParametrosNoValidos(f"la versión {v['version']} está {v['estado']}: solo se aprueban propuestas")
        con.execute("UPDATE versiones_parametros SET estado = 'retirada' WHERE estado = 'activa'")
        return con.execute(
            """UPDATE versiones_parametros SET estado = 'activa', aprobada_por = %s, aprobada_en = %s,
                      vigente_desde = %s WHERE id = %s RETURNING *""",
            (por.strip(), ahora, ahora, id_)).fetchone()


def activa(con) -> dict | None:
    return con.execute("SELECT * FROM versiones_parametros WHERE estado = 'activa'").fetchone()


def para_la_app(v: dict | None) -> dict | None:
    if v is None:
        return None
    return {"id": v["id"], "version": v["version"], "estado": v["estado"], "origen": v["origen"],
            "ratio_hc": v["ratio_hc"], "sensibilidad": v["sensibilidad"],
            "objetivo_mgdl": v["objetivo_mgdl"], "objetivo_correccion_mgdl": v["objetivo_correccion_mgdl"],
            "confianza": v["confianza"], "motivo": v["motivo"], "aprobada_por": v["aprobada_por"],
            "aprobada_en": v["aprobada_en"].isoformat() if v["aprobada_en"] else None}


# ------------------------------------------------------------------ línea de comandos
def _franjas_cli(texto: str, clave: str) -> list[dict]:
    franjas = []
    for parte in texto.split(","):
        desde, _, valor = parte.strip().partition("=")
        try:
            franjas.append({"desde": desde.strip(), clave: float(valor.replace(" ", ""))})
        except ValueError:
            raise ParametrosNoValidos(f"no entiendo {parte!r}: usa HH:MM=valor") from None
    return franjas


def main(argv=None):
    from comun.ajustes import Ajustes
    from comun.bd import conectar

    p = argparse.ArgumentParser(prog="python -m comun.parametros", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="orden", required=True)
    sub.add_parser("listar")
    pr = sub.add_parser("proponer")
    pr.add_argument("--ratio", required=True, help="gramos por unidad: 00:00=10,12:00=12")
    pr.add_argument("--sensibilidad", required=True, help="mg/dL por unidad: 00:00=40")
    pr.add_argument("--objetivo", type=int, default=90)
    pr.add_argument("--objetivo-correccion", type=int, default=110)
    pr.add_argument("--confianza", default="baja", choices=["baja", "media", "alta"])
    pr.add_argument("--motivo", required=True)
    ap = sub.add_parser("aprobar")
    ap.add_argument("id", type=int)
    ap.add_argument("--por", required=True)
    a = p.parse_args(argv)

    with conectar(Ajustes.desde_entorno().database_url) as con:
        if a.orden == "listar":
            for v in con.execute("SELECT * FROM versiones_parametros ORDER BY version").fetchall():
                print(json.dumps(para_la_app(v) | {"estado": v["estado"]}, ensure_ascii=False))
        elif a.orden == "proponer":
            v = proponer(con, _franjas_cli(a.ratio, "g_por_u"), _franjas_cli(a.sensibilidad, "mgdl_por_u"),
                         a.motivo, a.objetivo, a.objetivo_correccion, a.confianza)
            print(f"Propuesta creada: id {v['id']}, versión {v['version']}. Aún NO está activa.")
        else:
            v = aprobar(con, a.id, a.por)
            print(f"Versión {v['version']} activa desde {v['aprobada_en']:%d-%m-%Y %H:%M} UTC.")


if __name__ == "__main__":
    main()
