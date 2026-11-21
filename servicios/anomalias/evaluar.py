"""Criterio de aceptación de la fase 4: detectar ≥ 80 % con ≤ 1 falsa alarma diaria de media.

Dos formas de medirlo:
- `evaluar_en_vivo` (la que vale desde el 21-11-2026, decisión de Daniel): durante unas dos semanas
  Daniel responde a los avisos (aciertos y falsas alarmas) y etiqueta a mano solo lo que se
  escapa (episodios_etiquetados que no casan con ningún aviso). Hacen falta ≥ 20 casos reales.
- `evaluar` (abajo): pasa las reglas por el histórico y lo compara con episodios etiquetados.

Pasa las reglas por el histórico SIN guardar ni avisar, y compara con `episodios_etiquetados`:
- Detectado: hay una detección del mismo tipo que se solapa con el episodio (± margen).
- Posible falsa alarma: detección de un tipo etiquetado que no casa con ninguna etiqueta. Es una
  cota superior: si un episodio real no está etiquetado, cuenta como falsa.
También resume las respuestas dadas en la app ("No era para tanto", "fue una bajada real"…).
La acumulación de insulina no se evalúa aquí (es un aviso del momento).
"""
from datetime import datetime, timedelta

from servicios.anomalias import detector as D
from servicios.anomalias import reglas as R

TIPOS_EVALUADOS = ("hueco", "presion", "subida_sin_comida", "dosis_sin_comida", "olvido_tresiba")


MINIMO_CASOS = 20


def evaluar_en_vivo(con, desde: datetime, hasta: datetime, u: R.Umbrales = R.UMBRALES) -> dict:
    """Aciertos = avisos respondidos con una opción que no es falsa alarma (incluido «No lo sé»).
    Falsas = respondidos con una opción de falsa alarma. Escapados = episodios etiquetados a mano
    que no casan con ningún aviso del mismo tipo. Detección = aciertos / (aciertos + escapados)."""
    margen = timedelta(minutes=u.margen_solape_min)
    avisos = con.execute(
        """SELECT a.*, (SELECT o->>'falsa_alarma' = 'true' FROM jsonb_array_elements(a.opciones) o
                        WHERE o->>'clave' = a.respuesta) AS falsa
           FROM anomalias a WHERE a.detectada_en >= %s AND a.detectada_en <= %s""", (desde, hasta)).fetchall()
    etiquetas = con.execute("SELECT * FROM episodios_etiquetados WHERE fin >= %s AND inicio <= %s",
                            (desde, hasta)).fetchall()
    dias = max(1.0, (hasta - desde).total_seconds() / 86400)
    por_tipo, tot = {}, {"aciertos": 0, "falsas": 0, "sin_responder": 0, "escapados": 0}
    for tipo in R.TIPOS:
        av = [a for a in avisos if a["tipo"] == tipo]
        et = [e for e in etiquetas if e["tipo"] == tipo]
        resp = [a for a in av if a["estado"] == "respondida"]
        falsas = sum(1 for a in resp if a["falsa"])
        aciertos = len(resp) - falsas
        escapados = sum(1 for e in et if not any(
            _solapan(e["inicio"], e["fin"], a["inicio"], a["fin"] or a["inicio"], margen) for a in av))
        sin = sum(1 for a in av if a["estado"] == "abierta")
        por_tipo[tipo] = {"avisos": len(av), "aciertos": aciertos, "falsas": falsas, "sin_responder": sin,
                          "escapados": escapados,
                          "deteccion_pct": round(100 * aciertos / (aciertos + escapados), 1) if aciertos + escapados else None}
        for k, v in (("aciertos", aciertos), ("falsas", falsas), ("sin_responder", sin), ("escapados", escapados)):
            tot[k] += v
    casos = tot["aciertos"] + tot["escapados"]
    pct = round(100 * tot["aciertos"] / casos, 1) if casos else None
    falsas_dia = round(tot["falsas"] / dias, 2)
    return {"modo": "en_vivo", "desde": desde.isoformat(), "hasta": hasta.isoformat(), "dias": round(dias, 1),
            "version": R.VERSION, "por_tipo": por_tipo,
            "total": tot | {"casos_reales": casos, "deteccion_pct": pct, "falsas_por_dia": falsas_dia,
                            "casos_suficientes": casos >= MINIMO_CASOS,
                            "cumple": casos >= MINIMO_CASOS and pct >= 80 and falsas_dia <= 1}}


def detectar_historico(con, desde: datetime, hasta: datetime, u: R.Umbrales = R.UMBRALES) -> list[dict]:
    d = D.cargar(con, desde - D.VENTANA_LECTURAS, hasta)
    eps = R.huecos(d["lecturas"], hasta, u)
    pres = R.presion(d["lecturas"], d["hidratos"], u)
    eps += pres
    eps += R.subida_sin_comida(d["lecturas"], d["ingestas"], u, excluir=[(p["inicio"], p["fin"]) for p in pres])
    eps += R.dosis_sin_comida(d["fiasp"], d["ingestas"], hasta, u)
    primera = con.execute("SELECT min(vigente_desde) AS t FROM config_tresiba").fetchone()["t"]
    if primera is not None:
        dias = set()
        dia = max(primera, desde).astimezone(R.ZONA).date()
        while dia <= hasta.astimezone(R.ZONA).date():
            dias.add(dia)
            dia += timedelta(days=1)
        eventos = sorted(d["ingestas"] + [x["ts"] for x in d["fiasp"]])
        eps += R.olvidos_tresiba_historico(d["lecturas"], eventos, dias, u)
    return [e for e in eps if e["tipo"] not in u.desactivadas and (e["fin"] or hasta) >= desde and e["inicio"] <= hasta]


def _solapan(a_ini, a_fin, b_ini, b_fin, margen) -> bool:
    return a_ini - margen <= b_fin and b_ini - margen <= a_fin


def evaluar(con, desde: datetime, hasta: datetime, u: R.Umbrales = R.UMBRALES) -> dict:
    margen = timedelta(minutes=u.margen_solape_min)
    eps = detectar_historico(con, desde, hasta, u)
    etiquetas = con.execute("""SELECT * FROM episodios_etiquetados WHERE fin >= %s AND inicio <= %s ORDER BY inicio""",
                            (desde, hasta)).fetchall()
    dias = max(1.0, (hasta - desde).total_seconds() / 86400)
    por_tipo, total_et, total_det, total_falsas = {}, 0, 0, 0
    tipos_etiquetados = {e["tipo"] for e in etiquetas}
    for tipo in TIPOS_EVALUADOS:
        et = [e for e in etiquetas if e["tipo"] == tipo]
        de = [e for e in eps if e["tipo"] == tipo]
        detectados = [e for e in et if any(_solapan(e["inicio"], e["fin"], x["inicio"], x["fin"] or hasta, margen) for x in de)]
        sueltas = [x for x in de if not any(_solapan(e["inicio"], e["fin"], x["inicio"], x["fin"] or hasta, margen) for e in et)]
        por_tipo[tipo] = {
            "etiquetados": len(et), "detectados": len(detectados),
            "deteccion_pct": round(100 * len(detectados) / len(et), 1) if et else None,
            "detecciones": len(de), "sin_etiqueta": len(sueltas),
            "sin_etiqueta_por_dia": round(len(sueltas) / dias, 2),
            "no_detectados": [{"id": e["id"], "inicio": e["inicio"].isoformat(), "fin": e["fin"].isoformat()}
                              for e in et if e not in detectados],
        }
        total_et += len(et)
        total_det += len(detectados)
        if tipo in tipos_etiquetados:
            total_falsas += len(sueltas)
    respuestas = con.execute(
        """SELECT a.tipo, a.respuesta, o->>'falsa_alarma' = 'true' AS falsa
           FROM anomalias a CROSS JOIN LATERAL jsonb_array_elements(a.opciones) o
           WHERE a.estado = 'respondida' AND o->>'clave' = a.respuesta AND a.detectada_en BETWEEN %s AND %s""",
        (desde, hasta)).fetchall()
    falsas_resp = sum(1 for r in respuestas if r["falsa"])
    pct = round(100 * total_det / total_et, 1) if total_et else None
    falsas_dia = round(total_falsas / dias, 2)
    return {
        "desde": desde.isoformat(), "hasta": hasta.isoformat(), "dias": round(dias, 1), "version": R.VERSION,
        "umbrales": u.como_dict(), "por_tipo": por_tipo,
        "total": {"etiquetados": total_et, "detectados": total_det, "deteccion_pct": pct,
                  "posibles_falsas_por_dia": falsas_dia,
                  "cumple": bool(total_et) and pct >= 80 and falsas_dia <= 1},
        "respuestas_app": {"respondidas": len(respuestas), "falsa_alarma": falsas_resp,
                           "falsas_por_dia": round(falsas_resp / dias, 2)},
    }
