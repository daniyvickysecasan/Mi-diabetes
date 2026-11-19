"""Reúne las entradas de la base, calcula la sugerencia (comun/sugerencia.py) y la guarda.

Flujo en la app (acordado el 19-11-2026):
  1. Al pedir una dosis, la app lanza una lectura de la pluma en LibreView (solicitudes_lectura_pluma).
  2. Cuando el lector la cierra, la app pide la sugerencia con el id de esa solicitud.
  3. Si la lectura falla o LibreView pide el SMS, el cálculo NO espera ni inventa: queda bloqueado
     y pide leer la pluma o apuntar las dosis. Si Daniel confirma que todas sus dosis están
     apuntadas en la app (`dosis_apuntadas=True`), se calcula con aviso y más incertidumbre.

AFECTA A LA DOSIS: cambios con tests y aprobación de Daniel.
"""
import json
from datetime import datetime, timedelta, timezone

from comun import estado_libreview as EL
from comun import glucosa as G
from comun import parametros as P
from comun import sugerencia as S
from comun.insulina import FIASP, insulina_activa_bd

LECTURA_VALIDA = timedelta(minutes=10)    # una lectura de la pluma más vieja ya no cuenta


class LecturaEnCurso(Exception):
    """La lectura de la pluma aún no ha terminado: la app debe esperar y volver a pedir."""


class PeticionNoValida(ValueError):
    pass


def estado_pluma(con, solicitud_id: int | None, dosis_apuntadas: bool, ahora: datetime) -> str:
    if solicitud_id is not None:
        s = EL.solicitud(con, solicitud_id)
        if s is None:
            raise PeticionNoValida("No existe esa lectura de la pluma")
        if s["atendida_en"] is None:
            if s["pedida_en"] >= ahora - EL.SIN_RESPUESTA:
                raise LecturaEnCurso("Leyendo la pluma…")
        elif s["resultado"] == "ok" and s["atendida_en"] >= ahora - LECTURA_VALIDA:
            return "leida"
    return "confirmada_a_mano" if dosis_apuntadas else "no_leida"


def _entrada_comun(con, ahora: datetime, tipo: str, solicitud_id: int | None, dosis_apuntadas: bool,
                   en: datetime) -> dict:
    v = P.activa(con)
    ratio = sens = None
    franjas = {}
    if v is not None:
        ratio, franjas["ratio_desde"] = P.valor_en(v["ratio_hc"], "g_por_u", en)
        sens, franjas["sensibilidad_desde"] = P.valor_en(v["sensibilidad"], "mgdl_por_u", en)
    g = G.actual(con, ahora=ahora)
    iob = insulina_activa_bd(con, ahora)
    n3h = con.execute("""SELECT count(*) AS n FROM dosis_efectivas
                         WHERE insulina = 'fiasp' AND ts > %s AND ts <= %s""",
                      (ahora - timedelta(hours=3), ahora)).fetchone()["n"]
    hc = con.execute("""SELECT coalesce(sum(gramos), 0) AS g FROM hidratos_correccion
                        WHERE ts > %s AND ts <= %s""",
                     (ahora - timedelta(minutes=S.LIMITES.hidratos_correccion_aviso_min), ahora)).fetchone()["g"]
    delta = g["delta_5min_mgdl"] if g else None
    return {
        "ahora": ahora, "tipo": tipo,
        "version_parametros_id": v["id"] if v else None, "ratio_g_u": ratio, "sensibilidad_mgdl_u": sens,
        "objetivo_correccion_mgdl": v["objetivo_correccion_mgdl"] if v else None,
        "confianza": v["confianza"] if v else None,
        "glucosa_mgdl": g["valor_mgdl"] if g else None,
        "glucosa_ts": datetime.fromisoformat(g["ts"]) if g else None,
        "pendiente_mgdl_min": delta / 5 if delta is not None else None,
        "tendencia": g["tendencia"] if g else None,
        "insulina_activa_u": iob["unidades"], "dosis_fiasp_3h": n3h, "dosis_con_dudas": iob["con_dudas"],
        "estado_pluma": estado_pluma(con, solicitud_id, dosis_apuntadas, ahora),
        "hidratos_correccion_recientes_g": float(hc),
    }, {"franjas": franjas, "insulina_activa": iob}


def _guardar(con, r: dict, extra: dict, comida_id: int | None, solicitud_id: int | None,
             origen_id: int | None = None) -> dict:
    traza = r["traza"] | extra
    return con.execute(
        """INSERT INTO sugerencias
             (ts, comida_id, version_parametros_id, calculo_version, glucosa_mgdl, tendencia, insulina_activa_u,
              hidratos_g, u_hidratos, u_correccion, u_inmediata, u_segunda, ts_segunda, incertidumbre,
              bloqueada, motivo_bloqueo, avisos, traza, tipo, sugerencia_origen_id, solicitud_lectura_id,
              estado_pluma, ts_comida, grasa_g, proteina_g, ugp)
           VALUES (%(ts)s, %(comida_id)s, %(version_parametros_id)s, %(calculo_version)s, %(glucosa_mgdl)s,
                   %(tendencia)s, %(insulina_activa_u)s, %(hidratos_g)s, %(u_hidratos)s, %(u_correccion)s,
                   %(u_inmediata)s, %(u_segunda)s, %(ts_segunda)s, %(incertidumbre)s, %(bloqueada)s,
                   %(motivo_bloqueo)s, %(avisos)s, %(traza)s, %(tipo)s, %(origen)s, %(solicitud)s,
                   %(estado_pluma)s, %(ts_comida)s, %(grasa_g)s, %(proteina_g)s, %(ugp)s)
           RETURNING *""",
        r | {"ts": datetime.fromisoformat(traza["entrada"]["ahora"]), "comida_id": comida_id, "origen": origen_id, "solicitud": solicitud_id,
             "incertidumbre": json.dumps(r["incertidumbre"]), "avisos": json.dumps(r["avisos"], ensure_ascii=False),
             "traza": json.dumps(traza, default=str, ensure_ascii=False)}).fetchone()


def sugerir(con, *, ahora: datetime | None = None, tipo: str = "comida", comida_id: int | None = None,
            hidratos_g: float = 0.0, hidratos_min_g: float | None = None, hidratos_max_g: float | None = None,
            grasa_g: float = 0.0, proteina_g: float = 0.0, ts_comida: datetime | None = None,
            solicitud_lectura_id: int | None = None, dosis_apuntadas: bool = False) -> dict:
    """Calcula y guarda una sugerencia. Con `comida_id`, los gramos salen de esa comida, que debe
    estar revisada por Daniel."""
    ahora = ahora or datetime.now(timezone.utc)
    if tipo not in ("comida", "correccion"):
        raise PeticionNoValida("tipo: comida o correccion")
    if comida_id is not None:
        c = con.execute("SELECT * FROM comidas WHERE id = %s", (comida_id,)).fetchone()
        if c is None:
            raise PeticionNoValida("No existe esa comida")
        if not c["revisada"]:
            raise PeticionNoValida("Revisa la comida antes de calcular la dosis")
        hidratos_g = float(c["hidratos_bolo_g"] if c["hidratos_bolo_g"] is not None else c["hidratos_g"] or 0)
        hidratos_min_g = float(c["rango_hidratos_min_g"]) if c["rango_hidratos_min_g"] is not None else None
        hidratos_max_g = float(c["rango_hidratos_max_g"]) if c["rango_hidratos_max_g"] is not None else None
        grasa_g, proteina_g = float(c["grasa_g"] or 0), float(c["proteina_g"] or 0)
        ts_comida = ts_comida or c["ts"]
    if tipo == "correccion":
        hidratos_g, hidratos_min_g, hidratos_max_g, grasa_g, proteina_g = 0.0, None, None, 0.0, 0.0
    for x in (hidratos_g, grasa_g, proteina_g):
        if x < 0:
            raise PeticionNoValida("Los gramos no pueden ser negativos")
    if hidratos_min_g is not None and hidratos_max_g is not None and not hidratos_min_g <= hidratos_g <= hidratos_max_g:
        raise PeticionNoValida("Los hidratos deben estar dentro de su rango")
    ts_comida = ts_comida or ahora
    base, extra = _entrada_comun(con, ahora, tipo, solicitud_lectura_id, dosis_apuntadas, ts_comida)
    e = S.Entrada(**base, ts_comida=ts_comida, hidratos_g=hidratos_g, hidratos_min_g=hidratos_min_g,
                  hidratos_max_g=hidratos_max_g, grasa_g=grasa_g, proteina_g=proteina_g)
    return _guardar(con, S.calcular(e), extra, comida_id, solicitud_lectura_id)


def segunda(con, sugerencia_id: int, *, ahora: datetime | None = None, solicitud_lectura_id: int | None = None,
            dosis_apuntadas: bool = False) -> dict:
    """Vuelve a comprobar a su hora la segunda inyección de una sugerencia de comida."""
    ahora = ahora or datetime.now(timezone.utc)
    o = con.execute("SELECT * FROM sugerencias WHERE id = %s", (sugerencia_id,)).fetchone()
    if o is None:
        raise PeticionNoValida("No existe esa sugerencia")
    if o["tipo"] != "comida" or o["u_segunda"] is None:
        raise PeticionNoValida("Esa sugerencia no tiene segunda inyección")
    base, extra = _entrada_comun(con, ahora, "segunda", solicitud_lectura_id, dosis_apuntadas, ahora)
    e = S.Entrada(**base, ts_comida=o["ts_comida"])
    r = S.segunda(e, float(o["u_segunda"]), o["ts_segunda"])
    return _guardar(con, r, extra, o["comida_id"], solicitud_lectura_id, origen_id=o["id"])


def para_la_app(f: dict) -> dict:
    num = (lambda x: float(x) if x is not None else None)
    return {
        "id": f["id"], "ts": f["ts"].isoformat(), "tipo": f["tipo"], "comida_id": f["comida_id"],
        "sugerencia_origen_id": f["sugerencia_origen_id"], "version_parametros_id": f["version_parametros_id"],
        "calculo_version": f["calculo_version"], "glucosa_mgdl": f["glucosa_mgdl"], "tendencia": f["tendencia"],
        "insulina_activa_u": num(f["insulina_activa_u"]), "hidratos_g": num(f["hidratos_g"]),
        "grasa_g": num(f["grasa_g"]), "proteina_g": num(f["proteina_g"]), "ugp": num(f["ugp"]),
        "u_hidratos": num(f["u_hidratos"]), "u_correccion": num(f["u_correccion"]),
        "u_inmediata": num(f["u_inmediata"]), "u_segunda": num(f["u_segunda"]),
        "ts_segunda": f["ts_segunda"].isoformat() if f["ts_segunda"] else None,
        "estado_pluma": f["estado_pluma"], "bloqueada": f["bloqueada"], "motivo_bloqueo": f["motivo_bloqueo"],
        "incertidumbre": f["incertidumbre"], "avisos": f["avisos"], "freno": f["traza"].get("freno", []),
    }
