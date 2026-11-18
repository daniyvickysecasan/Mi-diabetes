"""Conciliación de dosis de Fiasp: app ↔ pluma (API de LibreView y CSV de LibreView).

Reglas (CLAUDE.md, regla 2, y plan de la fase 2):
- La pluma manda en hora y unidades; la app manda en el vínculo con la comida.
- Una inyección real cuenta UNA vez en la insulina activa (vista `dosis_efectivas`).
- Nada se resuelve en silencio: toda duda queda con `requiere_revision` y un motivo legible.
- Conservadora: ante la duda, una dosis CUENTA (una dosis olvidada haría sugerir de más).

Pasos, en este orden, deterministas e idempotentes:
0. Las dosis que LibreView marca como cebado (notas 'cebado…') quedan descartadas: no cuentan.
1. Duplicados entre vías de la pluma: la misma dosis por la API y por el CSV (mismas unidades,
   ±2 min) es una sola. Manda la fila ya revisada, si la hay; si no, la de la API.
   La otra queda `descartada` apuntando a la canónica.
2. Posible cebado: una dosis pequeña de la pluma seguida de otra en pocos minutos se marca para
   revisar (sigue contando hasta que Daniel la descarte).
3. Parejas app ↔ pluma a ±15 min, uno a uno. Se prefieren las de unidades iguales y, entre ellas,
   la más cercana en hora. Unidades iguales → `emparejada`; distintas → `discrepancia` (cuenta la
   pluma y se pide revisión).
4. App sin pareja: `pendiente` mientras la pluma no haya sincronizado más allá; si la pluma ya tiene
   dosis posteriores y no esta, `solo_app` con revisión (¿otra pluma? ¿registro erróneo?).
5. Pluma sin pareja: `solo_pluma` (cuenta; no se registró en la app).

Las filas revisadas por Daniel (`revisada_en`) no se tocan nunca. Las demás se recalculan desde
cero en cada ejecución: si llega tarde una dosis de la pluma, la pareja se forma entonces.

CAMBIAR ESTAS REGLAS AFECTA A LA DOSIS: exige tests y la aprobación explícita de Daniel.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

VERSION = "conciliacion-v1"
ORIGENES_PLUMA = ("pluma_libreview", "csv_libreview")
VENTANA_PAREJA = timedelta(minutes=15)
VENTANA_DUPLICADO = timedelta(minutes=2)
TOLERANCIA_U = 0.01
CEBADO_MAX_U = 2.0
CEBADO_VENTANA = timedelta(minutes=5)
ZONA = ZoneInfo("Europe/Madrid")
_BLOQUEO = 727275           # pg_advisory_xact_lock: una conciliación a la vez


@dataclass
class Resultado:
    estado: str
    par: int | None = None
    revisar: bool = False
    motivo: str | None = None


def _hm(ts: datetime) -> str:
    return ts.astimezone(ZONA).strftime("%H:%M")


def _u(x) -> str:
    v = float(x)
    return f"{v:g} U"


def _es_pluma(f: dict) -> bool:
    return f["origen"] in ORIGENES_PLUMA


def _iguales(a, b) -> bool:
    return abs(float(a) - float(b)) < TOLERANCIA_U


def es_cebado_marcado(f: dict) -> bool:
    """Las vías de la pluma guardan las dosis marcadas como cebado con notas que empiezan por 'cebado'."""
    return str(f.get("notas") or "").strip().lower().startswith("cebado")


def calcular(filas: list[dict]) -> dict[int, Resultado]:
    """Función pura: dado el conjunto de dosis de Fiasp, devuelve el estado de cada fila libre.

    Cada fila: id, ts, unidades, origen, revisada_en, estado_conciliacion, dosis_par_id, notas.
    Las filas revisadas no aparecen en la salida (no se tocan).
    """
    libres = [f for f in filas if f["revisada_en"] is None]
    fijas = [f for f in filas if f["revisada_en"] is not None]
    res: dict[int, Resultado] = {}

    # 0. Cebados marcados en LibreView: no son inyecciones.
    for f in libres:
        if _es_pluma(f) and es_cebado_marcado(f):
            res[f["id"]] = Resultado("descartada", None, False, "Marcada como cebado en LibreView: no cuenta")
    libres = [f for f in libres if f["id"] not in res]

    # 1. Duplicados entre la API y el CSV.
    canonicas = [f for f in fijas if _es_pluma(f) and f["estado_conciliacion"] != "descartada"]
    absorbe: set[int] = set()
    orden = sorted((f for f in libres if _es_pluma(f)),
                   key=lambda f: (f["origen"] != "pluma_libreview", f["ts"], f["id"]))
    for f in orden:
        cand = [c for c in canonicas if c["origen"] != f["origen"] and c["id"] not in absorbe
                and _iguales(c["unidades"], f["unidades"]) and abs(c["ts"] - f["ts"]) <= VENTANA_DUPLICADO]
        if cand:
            c = min(cand, key=lambda c: (abs(c["ts"] - f["ts"]), c["id"]))
            absorbe.add(c["id"])
            via = "la API de LibreView" if c["origen"] == "pluma_libreview" else "el CSV de LibreView"
            res[f["id"]] = Resultado("descartada", c["id"], False, f"Duplicado: la misma dosis ya llegó por {via}")
        else:
            canonicas.append(f)
    pluma_libres = [f for f in canonicas if f["revisada_en"] is None]

    # 2. Posible cebado (inyección al aire antes de la dosis).
    cebado: dict[int, str] = {}
    for f in pluma_libres:
        if float(f["unidades"]) > CEBADO_MAX_U:
            continue
        sig = [c for c in canonicas if c["id"] != f["id"] and timedelta(0) < c["ts"] - f["ts"] <= CEBADO_VENTANA]
        if sig:
            s = min(sig, key=lambda c: c["ts"])
            cebado[f["id"]] = (f"¿Cebado? {_u(f['unidades'])} a las {_hm(f['ts'])} y {_u(s['unidades'])} "
                               f"a las {_hm(s['ts'])}. Cuenta como dosis hasta que la descartes")

    # 3. Parejas app ↔ pluma.
    app_libres = [f for f in libres if not _es_pluma(f)]
    pares = []
    for a in app_libres:
        for p in pluma_libres:
            dt = abs(a["ts"] - p["ts"])
            if dt <= VENTANA_PAREJA:
                du = abs(float(a["unidades"]) - float(p["unidades"]))
                pares.append((du >= TOLERANCIA_U, du, dt, a["id"], p["id"], a, p))
    pares.sort(key=lambda x: x[:5])
    usadas: set[int] = set()
    for distintas, _du, _dt, ia, ip, a, p in pares:
        if ia in usadas or ip in usadas:
            continue
        usadas |= {ia, ip}
        if not distintas:
            motivo = cebado.get(ip)
            res[ia] = Resultado("emparejada", ip, False, None)
            res[ip] = Resultado("emparejada", ia, ip in cebado, motivo)
        else:
            m = (f"La app dice {_u(a['unidades'])} a las {_hm(a['ts'])} y la pluma {_u(p['unidades'])} "
                 f"a las {_hm(p['ts'])}. Cuenta la pluma")
            res[ia] = Resultado("discrepancia", ip, True, m)
            res[ip] = Resultado("discrepancia", ia, True, m + (f". {cebado[ip]}" if ip in cebado else ""))

    # 4. App sin pareja.
    for a in app_libres:
        if a["id"] in usadas:
            continue
        posteriores = [c for c in canonicas if c["ts"] > a["ts"] + VENTANA_PAREJA]
        if posteriores:
            res[a["id"]] = Resultado("solo_app", None, True,
                                     "La pluma ya ha enviado dosis posteriores y no tiene esta. "
                                     "¿Otra pluma o registro erróneo? Cuenta hasta que la descartes")
        else:
            res[a["id"]] = Resultado("pendiente", None, False, "Esperando los datos de la pluma")

    # 5. Pluma sin pareja.
    for p in pluma_libres:
        if p["id"] in usadas:
            continue
        res[p["id"]] = Resultado("solo_pluma", None, p["id"] in cebado,
                                 cebado.get(p["id"], "Dosis de la pluma sin registrar en la app"))

    # 6. Horas dudosas del CSV (cambio de hora): siempre a revisar.
    for f in filas:
        nota = str(f.get("notas") or "")
        r = res.get(f["id"])
        if r and r.estado != "descartada" and ("hora ambigua" in nota or "hora inexistente" in nota):
            r.revisar = True
            r.motivo = f"{r.motivo}. Ojo: {nota}" if r.motivo else f"Ojo: {nota}"
    return res


def conciliar(con, ahora: datetime | None = None) -> dict:
    """Recalcula la conciliación de todas las dosis de Fiasp no revisadas. Idempotente.

    Llamarla tras insertar dosis de cualquier vía (app, API de LibreView, CSV).
    """
    ahora = ahora or datetime.now(timezone.utc)
    with con.transaction():
        con.execute("SELECT pg_advisory_xact_lock(%s)", (_BLOQUEO,))
        filas = con.execute(
            """SELECT id, ts, unidades, origen, revisada_en, estado_conciliacion, dosis_par_id,
                      requiere_revision, motivo_conciliacion, notas
               FROM dosis WHERE insulina = 'fiasp' ORDER BY ts, id""").fetchall()
        res = calcular(filas)
        actual = {f["id"]: f for f in filas}
        cambios = {i: r for i, r in res.items()
                   if (actual[i]["estado_conciliacion"], actual[i]["dosis_par_id"], actual[i]["requiere_revision"],
                       actual[i]["motivo_conciliacion"]) != (r.estado, r.par, r.revisar, r.motivo)}
        if cambios:
            # Primero se sueltan las parejas (el índice único y los CHECK no admiten estados a medias).
            con.execute("""UPDATE dosis SET estado_conciliacion = 'pendiente', dosis_par_id = NULL
                           WHERE id = ANY(%s)""", (list(cambios),))
            for i, r in cambios.items():
                con.execute(
                    """UPDATE dosis SET estado_conciliacion = %s, dosis_par_id = %s, requiere_revision = %s,
                              motivo_conciliacion = %s, conciliado_en = %s WHERE id = %s""",
                    (r.estado, r.par, r.revisar, r.motivo, ahora, i))
    cuenta: dict[str, int] = {}
    for r in res.values():
        cuenta[r.estado] = cuenta.get(r.estado, 0) + 1
    return {"version": VERSION, "filas": len(filas), "cambiadas": len(cambios), "estados": cuenta,
            "por_revisar": sum(1 for r in res.values() if r.revisar)}


# ------------------------------------------------------------------ revisión por Daniel
ACCIONES = ("aceptar", "separar", "descartar", "reabrir")


class RevisionInvalida(ValueError):
    pass


def revisar(con, dosis_id: int, accion: str, ahora: datetime | None = None) -> dict:
    """Decisión de Daniel sobre una dosis. Congela la fila (y su pareja) salvo `reabrir`.

    - aceptar:   da por buena la conciliación automática tal cual está.
    - separar:   la pareja eran dos inyecciones distintas; cuentan las dos.
    - descartar: esta fila no es una inyección (cebado, registro erróneo). Deja de contar.
    - reabrir:   deshace la decisión; la conciliación automática vuelve a decidir.
    """
    if accion not in ACCIONES:
        raise RevisionInvalida(f"Acción desconocida: {accion}")
    ahora = ahora or datetime.now(timezone.utc)
    with con.transaction():
        con.execute("SELECT pg_advisory_xact_lock(%s)", (_BLOQUEO,))
        d = con.execute("SELECT * FROM dosis WHERE id = %s FOR UPDATE", (dosis_id,)).fetchone()
        if d is None:
            raise RevisionInvalida("No existe esa dosis")
        if d["insulina"] != "fiasp":
            raise RevisionInvalida("Solo se concilian dosis de Fiasp")
        par = d["dosis_par_id"] if d["estado_conciliacion"] in ("emparejada", "discrepancia") else None
        if accion == "aceptar":
            if d["estado_conciliacion"] == "pendiente":
                raise RevisionInvalida("Esta dosis aún espera los datos de la pluma; no hay nada que aceptar")
            ids = [dosis_id] + ([par] if par else [])
            con.execute("""UPDATE dosis SET revisada_en = %s, revision = 'aceptada', requiere_revision = false
                           WHERE id = ANY(%s)""", (ahora, ids))
        elif accion == "separar":
            if not par:
                raise RevisionInvalida("Esta dosis no está emparejada con otra")
            for i, otra in ((dosis_id, par), (par, dosis_id)):
                fila = con.execute("SELECT origen FROM dosis WHERE id = %s", (i,)).fetchone()
                estado = "solo_pluma" if fila["origen"] in ORIGENES_PLUMA else "solo_app"
                con.execute(
                    """UPDATE dosis SET estado_conciliacion = %s, dosis_par_id = NULL, requiere_revision = false,
                              revisada_en = %s, revision = 'separadas', separada_de = %s,
                              motivo_conciliacion = 'Separadas por Daniel: son dos inyecciones distintas'
                       WHERE id = %s""", (estado, ahora, otra, i))
        elif accion == "descartar":
            if par:   # la otra mitad queda libre y la conciliación la vuelve a situar
                con.execute("""UPDATE dosis SET estado_conciliacion = 'pendiente', dosis_par_id = NULL,
                                      revisada_en = NULL, revision = NULL WHERE id = %s""", (par,))
            con.execute(
                """UPDATE dosis SET estado_conciliacion = 'descartada', dosis_par_id = NULL,
                          requiere_revision = false, revisada_en = %s, revision = 'descartada',
                          motivo_conciliacion = 'Descartada por Daniel: no es una inyección' WHERE id = %s""",
                (ahora, dosis_id))
        else:  # reabrir
            ids = [dosis_id] + ([par] if par else []) + ([d["separada_de"]] if d["separada_de"] else [])
            con.execute("""UPDATE dosis SET revisada_en = NULL, revision = NULL, separada_de = NULL
                           WHERE id = ANY(%s)""", (ids,))
    return conciliar(con, ahora)
