"""Datos sintéticos de glucosa y tratamientos con el formato de Nightscout.

Solo para tests y demostraciones: no representan a ninguna persona.
Incluyen a propósito: un hueco de 2 h, una lectura duplicada, una glucemia capilar (mbg),
un tramo con lecturas cada minuto y un tratamiento con nuestro origen propio.
"""
import math
import random
from datetime import datetime, timedelta, timezone

DIRECCIONES = [(3, "DoubleUp"), (2, "SingleUp"), (1, "FortyFiveUp"), (-1, "Flat"),
               (-2, "FortyFiveDown"), (-3, "SingleDown")]


def _direccion(pendiente: float) -> str:
    for limite, nombre in DIRECCIONES:
        if pendiente > limite or (limite == -1 and pendiente >= -1):
            return nombre
    return "DoubleDown"


def valor(t: datetime, rnd: random.Random) -> float:
    h = t.hour + t.minute / 60
    v = 125 + 25 * math.sin(2 * math.pi * (h - 4) / 24)
    for comida in (8.5, 14.5, 21.5):           # subidas tras las comidas
        dt = h - comida
        if 0 < dt < 4:
            v += 85 * dt * math.exp(1 - dt) / 1.0 * math.exp(-0.2 * dt)
    if 3 <= h < 4 and t.day % 4 == 0:          # alguna hipo nocturna
        v -= 70
    return max(40, min(400, v + rnd.gauss(0, 6)))


def generar(desde: datetime, hasta: datetime, semilla: int = 7, paso_min: int = 5,
            con_incidencias: bool = True) -> tuple[list[dict], list[dict]]:
    rnd = random.Random(semilla)
    entradas, t, anterior = [], desde, None
    hueco = (desde + (hasta - desde) * 0.4, desde + (hasta - desde) * 0.4 + timedelta(hours=2))
    minuto_a_minuto = (desde + (hasta - desde) * 0.7, desde + (hasta - desde) * 0.7 + timedelta(hours=1))
    while t < hasta:
        if con_incidencias and hueco[0] <= t < hueco[1]:
            t += timedelta(minutes=paso_min)
            continue
        v = round(valor(t, rnd))
        pendiente = 0 if anterior is None else (v - anterior[1]) / max(1, (t - anterior[0]).total_seconds() / 60)
        ms = int(t.timestamp() * 1000)
        entradas.append({"_id": f"e{ms}", "type": "sgv", "sgv": v, "date": ms,
                         "dateString": t.isoformat().replace("+00:00", "Z"),
                         "direction": _direccion(pendiente), "device": "nightscout-librelink-up"})
        anterior = (t, v)
        paso = 1 if con_incidencias and minuto_a_minuto[0] <= t < minuto_a_minuto[1] else paso_min
        t += timedelta(minutes=paso)
    tratamientos = []
    if con_incidencias and entradas:
        d = entradas[len(entradas) // 3]
        entradas.append(dict(d, _id=d["_id"] + "dup"))                       # duplicado exacto
        m = entradas[len(entradas) // 2]
        entradas.append({"_id": "mbg1", "type": "mbg", "mbg": m["sgv"] + 8, "date": m["date"] + 1000,
                         "dateString": m["dateString"], "device": "glucometro"})
        base = desde + timedelta(hours=8)
        tratamientos = [
            {"_id": "t1", "eventType": "Note", "notes": "nota manual", "enteredBy": "careportal",
             "created_at": base.isoformat().replace("+00:00", "Z")},
            {"_id": "t2", "eventType": "Carb Correction", "carbs": 15, "enteredBy": "careportal",
             "created_at": (base + timedelta(hours=19)).isoformat().replace("+00:00", "Z")},
            {"_id": "t3", "eventType": "Correction Bolus", "insulin": 4, "enteredBy": "mi-diabetes/libreview",
             "created_at": (base + timedelta(hours=26)).isoformat().replace("+00:00", "Z")},
        ]
    return entradas, tratamientos


def nueva_lectura(t: datetime, semilla: int = 99) -> dict:
    v = round(valor(t, random.Random(semilla + int(t.timestamp()))))
    ms = int(t.timestamp() * 1000)
    return {"_id": f"e{ms}", "type": "sgv", "sgv": v, "date": ms,
            "dateString": t.isoformat().replace("+00:00", "Z"), "direction": "Flat",
            "device": "nightscout-librelink-up"}


def ahora_redondeado() -> datetime:
    n = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return n - timedelta(minutes=n.minute % 5)


# ------------------------------------------------------------------ CSV de LibreView (sintético)
# Cabecera real de la exportación de LibreView en español (vista el 19-11-2026, sin datos).
CABECERA_LIBREVIEW_ES_REAL = [
    "Dispositivo", "Número de serie", "Sello de tiempo del dispositivo", "Tipo de registro",
    "Historial de glucosa mg/dL", "Escaneo de glucosa mg/dL", "Insulina de acción rápida no numérica",
    "Insulina de acción rápida (unidades)", "Alimento no numérico", "Carbohidratos (gramos)",
    "Carbohidratos (porciones)", "Insulina de acción larga no numérica", "Insulina de acción larga (unidades)",
    "Notas", "Tira reactiva para glucosa mg/dL", "Cuerpos cetónicos mmol/L", "Comida e insulina (unidades)",
    "Insulina de corrección (unidades)", "Insulina del cambio de usuario (unidades)",
    "Históricos de cuerpos cetónicos mmol/L", "Escanear cuerpos cetónicos mmol/L"]

CABECERA_LIBREVIEW_ES = [
    "Dispositivo", "Número de serie", "Sello de tiempo del dispositivo", "Tipo de registro",
    "Historial de glucosa mg/dL", "Escaneo de glucosa mg/dL", "Insulina de acción rápida sin valor numérico",
    "Insulina de acción rápida (unidades)", "Alimentos sin valor numérico", "Carbohidratos (gramos)",
    "Carbohidratos (porciones)", "Insulina de acción prolongada sin valor numérico",
    "Insulina de acción prolongada (unidades)", "Notas", "Glucosa de la tira mg/dL", "Cetona mmol/L",
    "Insulina para comidas (unidades)", "Insulina de corrección (unidades)",
    "Insulina de cambio del usuario (unidades)"]


def csv_libreview(dosis: list[tuple], glucosa: list[tuple] = (), lentas: list[tuple] = (),
                  serie: str = "SINTETICA-0001", sep: str = ",") -> bytes:
    """CSV con el formato de exportación de LibreView en español (formato supuesto: se ajusta al real).

    dosis/lentas: (hora local naive, unidades, notas opcionales); glucosa: (hora local naive, mg/dL).
    """
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    buf.write(f"Datos de glucosa{sep}Generado el{sep}18-11-2026 10:00 UTC{sep}Generado por{sep}Persona sintética\n")
    w = _csv.writer(buf, delimiter=sep, lineterminator="\n")
    w.writerow(CABECERA_LIBREVIEW_ES)
    vacia = [""] * len(CABECERA_LIBREVIEW_ES)
    for t, v in glucosa:
        f = list(vacia); f[0] = "FreeStyle Libre 2"; f[1] = "SENSOR-1"; f[2] = t.strftime("%d-%m-%Y %H:%M")
        f[3] = "0"; f[4] = str(v); w.writerow(f)
    for d in dosis:
        t, u, notas = (d + (None,))[:3]
        f = list(vacia); f[0] = "NovoPen Echo Plus"; f[1] = serie; f[2] = t.strftime("%d-%m-%Y %H:%M")
        f[3] = "4"; f[7] = f"{u:g}".replace(".", ",") if sep == ";" else f"{u:g}"; f[13] = notas or ""
        w.writerow(f)
    for t, u, *_ in lentas:
        f = list(vacia); f[0] = "Manual"; f[1] = ""; f[2] = t.strftime("%d-%m-%Y %H:%M")
        f[3] = "4"; f[12] = f"{u:g}"; w.writerow(f)
    return buf.getvalue().encode("utf-8-sig")
