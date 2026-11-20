"""Lee la base, aplica las reglas (reglas.py) y guarda los episodios en `anomalias`.

Solo se comunica con el resto por la base (regla 3). No escribe en ninguna tabla de dosis,
comidas ni sugerencias, y la sugerencia no lee `anomalias`.
"""
import json
from datetime import date, datetime, timedelta

from comun import glucosa as G
from comun import parametros as P
from comun.insulina import insulina_activa_bd
from servicios.anomalias import reglas as R

VENTANA_LECTURAS = timedelta(hours=12)     # lo que mira cada ciclo
EPISODIO_RECIENTE = timedelta(hours=6)     # un episodio más viejo ya no se pregunta (salvo huecos)
TIPOS_CHAT_COMIDA = ("comida", "bebida")


def cargar(con, desde: datetime, hasta: datetime) -> dict:
    """Series de [desde, hasta] para las reglas. Las comidas incluyen las del chat (sin descartar)
    y los hidratos de corrección: cualquier ingesta explica una subida."""
    lecturas = [(f["ts"], f["valor_mgdl"]) for f in con.execute(
        """SELECT ts, valor_mgdl FROM glucosa WHERE fuente = 'nightscout' AND tipo = 'sgv'
           AND ts >= %s AND ts <= %s ORDER BY ts""", (desde, hasta)).fetchall()]
    comidas = [f["ts"] for f in con.execute(
        "SELECT ts FROM comidas WHERE ts >= %s AND ts <= %s", (desde, hasta)).fetchall()]
    chat = [f["ts"] for f in con.execute(
        """SELECT coalesce(ts_evento_inicio, ts_mensaje) AS ts FROM eventos_chat
           WHERE tipo = ANY(%s) AND estado <> 'descartado'
             AND coalesce(ts_evento_inicio, ts_mensaje) BETWEEN %s AND %s""",
        (list(TIPOS_CHAT_COMIDA), desde, hasta)).fetchall()]
    hc = [f["ts"] for f in con.execute(
        "SELECT ts FROM hidratos_correccion WHERE ts >= %s AND ts <= %s", (desde, hasta)).fetchall()]
    fiasp = con.execute(
        """SELECT e.id, e.ts, e.unidades, e.comida_id, s.tipo AS sugerencia_tipo
           FROM dosis_efectivas e
           LEFT JOIN dosis d ON d.id = coalesce(e.dosis_app_id, e.id)
           LEFT JOIN sugerencias s ON s.id = d.sugerencia_id
           WHERE e.insulina = 'fiasp' AND e.ts >= %s AND e.ts <= %s ORDER BY e.ts""", (desde, hasta)).fetchall()
    return {"lecturas": lecturas, "ingestas": sorted(comidas + chat + hc), "hidratos": sorted(comidas + hc),
            "fiasp": fiasp}


def _comidas_con_ratio(con, ahora: datetime, u: R.Umbrales) -> list[dict]:
    """Hidratos que aún se pueden estar absorbiendo, con el ratio de su franja (versión activa).
    Comida revisada: sus hidratos del bolo. Sin revisar: el mínimo de su rango (lo prudente)."""
    v = P.activa(con)
    if v is None:
        return []
    out = []
    for f in con.execute(
            """SELECT ts, revisada, hidratos_bolo_g, hidratos_g, rango_hidratos_min_g FROM comidas
               WHERE ts > %s AND ts <= %s""", (ahora - timedelta(minutes=u.absorcion_comida_min), ahora)).fetchall():
        if f["revisada"]:
            g = f["hidratos_bolo_g"] if f["hidratos_bolo_g"] is not None else f["hidratos_g"]
        else:
            g = f["rango_hidratos_min_g"] if f["rango_hidratos_min_g"] is not None else f["hidratos_g"]
        if g:
            out.append({"ts": f["ts"], "gramos": float(g), "tipo": "comida",
                        "ratio_g_u": P.valor_en(v["ratio_hc"], "g_por_u", f["ts"])[0]})
    for f in con.execute("SELECT ts, gramos FROM hidratos_correccion WHERE ts > %s AND ts <= %s",
                         (ahora - timedelta(minutes=u.absorcion_hc_min), ahora)).fetchall():
        out.append({"ts": f["ts"], "gramos": float(f["gramos"]), "tipo": "correccion",
                    "ratio_g_u": P.valor_en(v["ratio_hc"], "g_por_u", f["ts"])[0]})
    return out


def evaluar_acumulacion(con, ahora: datetime, u: R.Umbrales) -> dict | None:
    v = P.activa(con)
    if v is None:
        return None
    g = G.actual(con, ahora=ahora)
    if g is None:
        return None
    iob = insulina_activa_bd(con, ahora)
    n3h = con.execute("""SELECT count(*) AS n FROM dosis_efectivas
                         WHERE insulina = 'fiasp' AND ts > %s AND ts <= %s""",
                      (ahora - timedelta(hours=3), ahora)).fetchone()["n"]
    sens = P.valor_en(v["sensibilidad"], "mgdl_por_u", ahora)[0]
    return R.acumulacion(ahora, g["valor_mgdl"], datetime.fromisoformat(g["ts"]), iob["unidades"], sens,
                         _comidas_con_ratio(con, ahora, u), n3h, u)


def detectar(con, ahora: datetime, u: R.Umbrales = R.UMBRALES) -> list[dict]:
    """Episodios que se ven ahora (sin guardar)."""
    d = cargar(con, ahora - VENTANA_LECTURAS, ahora)
    episodios = []
    ultima = con.execute("""SELECT max(ts) AS ts FROM glucosa WHERE fuente = 'nightscout' AND tipo = 'sgv'
                            AND ts <= %s""", (ahora,)).fetchone()["ts"]
    if ultima is not None:
        episodios += R.huecos([], ahora, u, ultima_previa=ultima, solo_abierto=True)
    pres = R.presion(d["lecturas"], d["hidratos"], u)
    episodios += pres
    episodios += R.subida_sin_comida(d["lecturas"], d["ingestas"], u, excluir=[(p["inicio"], p["fin"]) for p in pres])
    recientes = [x for x in d["fiasp"] if x["ts"] >= ahora - timedelta(hours=u.dosis_ventana_h)]
    episodios += R.dosis_sin_comida(recientes, d["ingestas"], ahora, u)
    acu = evaluar_acumulacion(con, ahora, u)
    if acu:
        episodios.append(acu)
    hay_tresiba = con.execute("SELECT 1 FROM config_tresiba WHERE vigente_desde <= %s LIMIT 1", (ahora,)).fetchone()
    eventos = sorted(d["ingestas"] + [x["ts"] for x in d["fiasp"]])
    olv = R.olvido_tresiba(d["lecturas"], eventos, ahora, bool(hay_tresiba), u)
    if olv:
        episodios.append(olv)
    corte = ahora - EPISODIO_RECIENTE
    return [e for e in episodios if e["tipo"] not in u.desactivadas
            and (e["tipo"] == "hueco" or (e["fin"] or e["inicio"]) >= corte)]


def _json(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


def _actualizar(con, fila: dict, e: dict) -> None:
    fin = e["fin"] if fila["fin"] is None or (e["fin"] is not None and e["fin"] > fila["fin"]) else fila["fin"]
    if fila["tipo"] == "hueco":
        fin = fila["fin"] if fila["fin"] is not None else e["fin"]
    datos = e["datos"] if fila["estado"] == "abierta" else fila["datos"]
    t = R.redactar({**e, "inicio": fila["inicio"], "fin": fin, "datos": datos})
    con.execute("""UPDATE anomalias SET fin = %s, datos = %s, texto = %s, actualizada_en = now() WHERE id = %s""",
                (fin, _json(datos), t["texto"], fila["id"]))


def guardar(con, episodios: list[dict], ahora: datetime, u: R.Umbrales = R.UMBRALES) -> list[int]:
    """Guarda los episodios nuevos y actualiza los que ya estaban. Devuelve los id nuevos."""
    nuevos = []
    margen = timedelta(minutes=u.margen_solape_min)
    for e in episodios:
        tipo = e["tipo"]
        if tipo == "acumulacion":
            previa = con.execute("""SELECT * FROM anomalias WHERE tipo = 'acumulacion' AND detectada_en > %s
                                    ORDER BY detectada_en DESC LIMIT 1""",
                                 (ahora - timedelta(minutes=u.acumulacion_repetir_min),)).fetchone()
        elif tipo == "olvido_tresiba":
            dia: date = ahora.astimezone(R.ZONA).date()
            inicio_dia = datetime.combine(dia, datetime.min.time(), R.ZONA)
            previa = con.execute("""SELECT * FROM anomalias WHERE tipo = 'olvido_tresiba' AND fin >= %s
                                    ORDER BY id DESC LIMIT 1""", (inicio_dia,)).fetchone()
        elif tipo == "hueco":     # un hueco es "desde la última lectura": la misma, el mismo hueco
            previa = con.execute("SELECT * FROM anomalias WHERE tipo = 'hueco' AND inicio = %s",
                                 (e["inicio"],)).fetchone()
        elif tipo == "dosis_sin_comida":
            previa = con.execute("SELECT * FROM anomalias WHERE dosis_id = %s", (e["datos"]["dosis_id"],)).fetchone()
        else:
            fin = e["fin"] or ahora
            previa = con.execute(
                """SELECT * FROM anomalias WHERE tipo = %s AND inicio <= %s AND coalesce(fin, %s) >= %s
                   ORDER BY id DESC LIMIT 1""", (tipo, fin + margen, ahora, e["inicio"] - margen)).fetchone()
        if previa is not None:
            if tipo != "olvido_tresiba":
                _actualizar(con, previa, e)
            continue
        t = R.redactar(e)
        f = con.execute(
            """INSERT INTO anomalias (tipo, inicio, fin, urgente, titulo, texto, pregunta, opciones, datos,
                                      detector_version, umbrales, dosis_id, avisar_desde, detectada_en)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (tipo, e["inicio"], e["fin"], t["urgente"], t["titulo"], t["texto"], t["pregunta"], _json(t["opciones"]),
             _json(e["datos"]), R.VERSION, _json(u.como_dict()), e["datos"].get("dosis_id"),
             R.avisar_desde(ahora, t["urgente"], u), ahora)).fetchone()
        nuevos.append(f["id"])
    return nuevos


def cerrar_huecos(con, ahora: datetime) -> int:
    """Un hueco abierto se cierra con la primera lectura que llega después."""
    n = 0
    for a in con.execute("SELECT * FROM anomalias WHERE tipo = 'hueco' AND fin IS NULL").fetchall():
        f = con.execute("""SELECT min(ts) AS ts FROM glucosa WHERE fuente = 'nightscout' AND tipo = 'sgv'
                           AND ts > %s AND ts <= %s""", (a["inicio"], ahora)).fetchone()["ts"]
        if f is None:
            minutos = round((ahora - a["inicio"]).total_seconds() / 60)
            con.execute("UPDATE anomalias SET datos = datos || %s::jsonb WHERE id = %s",
                        (_json({"minutos": minutos}), a["id"]))
            continue
        datos = {**a["datos"], "minutos": round((f - a["inicio"]).total_seconds() / 60), "abierto": False}
        t = R.redactar({"tipo": "hueco", "inicio": a["inicio"], "fin": f, "datos": datos})
        con.execute("""UPDATE anomalias SET fin = %s, datos = %s, texto = %s, actualizada_en = now()
                       WHERE id = %s""", (f, _json(datos), t["texto"], a["id"]))
        n += 1
    return n


def ciclo(con, ahora: datetime, u: R.Umbrales = R.UMBRALES) -> dict:
    cerrados = cerrar_huecos(con, ahora)
    episodios = detectar(con, ahora, u)
    nuevos = guardar(con, episodios, ahora, u)
    return {"detectados": len(episodios), "nuevos": nuevos, "huecos_cerrados": cerrados}
