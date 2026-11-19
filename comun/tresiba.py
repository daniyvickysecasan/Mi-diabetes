"""Tresiba: configuración con historial y dosis programadas.

La pluma inteligente solo es de Fiasp: la Tresiba se configura en la app (unidades y hora local) y
la dosis diaria se da por puesta. Cada cambio es una fila nueva de `config_tresiba` con su
`vigente_desde`; nunca se reescribe el pasado.

La Tresiba no entra en la insulina activa (es basal, casi constante). Las dosis programadas sirven
para el panel, la fase 4 (aviso de posible olvido) y los modelos posteriores.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _fila(f: dict) -> dict:
    return {"id": f["id"], "vigente_desde": f["vigente_desde"].isoformat(), "unidades": float(f["unidades"]),
            "hora_local": f["hora_local"].strftime("%H:%M"), "zona_horaria": f["zona_horaria"], "notas": f["notas"]}


def vigente(con, en: datetime | None = None) -> dict | None:
    en = en or datetime.now(timezone.utc)
    f = con.execute("SELECT * FROM config_tresiba WHERE vigente_desde <= %s ORDER BY vigente_desde DESC LIMIT 1",
                    (en,)).fetchone()
    return _fila(f) if f else None


def historial(con) -> list[dict]:
    return [_fila(f) for f in con.execute("SELECT * FROM config_tresiba ORDER BY vigente_desde DESC").fetchall()]


def guardar(con, unidades: float, hora_local: time, vigente_desde: datetime, notas: str | None = None,
            zona: str = "Europe/Madrid") -> dict:
    f = con.execute(
        """INSERT INTO config_tresiba (vigente_desde, unidades, hora_local, zona_horaria, notas)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (vigente_desde, unidades, hora_local, zona, notas)).fetchone()
    return _fila(f)


def programadas(configs: list[dict], desde: datetime, hasta: datetime) -> list[dict]:
    """Dosis diarias que se dan por puestas en [desde, hasta). Función pura.

    `configs`: filas con vigente_desde (datetime), unidades, hora_local (time), zona_horaria.
    Cada día local se aplica la configuración vigente a la hora de la dosis.
    """
    configs = sorted(configs, key=lambda c: c["vigente_desde"])
    if not configs:
        return []
    salida = []
    zona0 = ZoneInfo(configs[0]["zona_horaria"])
    dia: date = desde.astimezone(zona0).date() - timedelta(days=1)
    ultimo: date = hasta.astimezone(zona0).date() + timedelta(days=1)
    while dia <= ultimo:
        for_dia = None
        for c in configs:      # la última configuración vigente a la hora de la dosis de ese día
            ts = datetime.combine(dia, c["hora_local"], ZoneInfo(c["zona_horaria"])).astimezone(timezone.utc)
            if c["vigente_desde"] <= ts:
                for_dia = (ts, c)
        if for_dia and desde <= for_dia[0] < hasta:
            ts, c = for_dia
            salida.append({"ts": ts, "unidades": float(c["unidades"]), "config_id": c.get("id")})
        dia += timedelta(days=1)
    return salida


def programadas_bd(con, desde: datetime, hasta: datetime) -> list[dict]:
    return programadas(con.execute("SELECT * FROM config_tresiba").fetchall(), desde, hasta)
