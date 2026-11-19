"""Aviso "Lee la pluma": las dosis de la NovoPen solo llegan cuando Daniel la acerca al iPhone con
LibreLink (NFC) y LibreLink las sube a LibreView. Si pasa tiempo sin leerla, la base no las tiene.

Solo informa: no cambia ningún cálculo ni ninguna dosis. Las dosis apuntadas en la app ya cuentan
en la insulina activa mientras la pluma no las confirma (regla conservadora de la conciliación).

Dos motivos, en este orden:
1. `dosis_sin_leer`: hay dosis apuntadas en la app que siguen `pendiente` (la pluma no ha enviado
   nada posterior) desde hace más del margen. Es la señal más clara de que falta leerla.
   Si el recolector de LibreView está activo (lo mira cada pocas horas), el margen se cuenta hasta
   su última lectura buena, no hasta ahora: así no avisa de dosis que LibreView aún no ha mirado.
2. `sin_lecturas`: la última dosis que llegó de la pluma es más antigua que el umbral (o nunca ha
   llegado ninguna). Puede ser que no te hayas inyectado, por eso el texto dice "si te has inyectado".
   Con LibreView activo también se cuenta hasta su última lectura buena.
"""
from datetime import datetime, timedelta

from comun.conciliacion import ORIGENES_PLUMA

UMBRAL_HORAS = 12.0
MARGEN_MIN = 45          # lo que tarda en llegar una dosis desde que lees la pluma (LibreView + sondeo)
VENTANA_PENDIENTES = timedelta(hours=48)


def _horas(td: timedelta) -> str:
    h = td.total_seconds() / 3600
    if h < 1:
        return f"{max(1, round(h * 60))} min"
    if h < 48:
        return f"{round(h)} h"
    return f"{round(h / 24)} días"


def evaluar(ahora: datetime, ultima_pluma: datetime | None, pendientes_app: list[datetime],
            umbral_horas: float = UMBRAL_HORAS, margen_min: int = MARGEN_MIN,
            ultima_lectura: datetime | None = None) -> dict | None:
    """Función pura. Devuelve el aviso o None si no hace falta leer la pluma.
    `ultima_lectura`: última lectura buena de LibreView (None = recolector de LibreView sin usar)."""
    base = {"ultima_pluma": ultima_pluma.isoformat() if ultima_pluma else None}
    corte = min(ahora, ultima_lectura) if ultima_lectura else ahora
    viejas = [t for t in pendientes_app if corte - t >= timedelta(minutes=margen_min)]
    if viejas:
        n = len(viejas)
        desde = ahora - min(viejas)
        texto = (f"{'Tienes 1 dosis apuntada' if n == 1 else f'Tienes {n} dosis apuntadas'} hace {_horas(desde)} "
                 "que la pluma aún no ha enviado. Lee la pluma con LibreLink.")
        return {**base, "motivo": "dosis_sin_leer", "pendientes": n, "texto": texto}
    if ultima_pluma is None:
        return {**base, "motivo": "sin_lecturas", "pendientes": 0,
                "texto": "Aún no ha llegado ninguna dosis de la pluma. Léela con LibreLink."}
    if corte - ultima_pluma >= timedelta(hours=umbral_horas):
        return {**base, "motivo": "sin_lecturas", "pendientes": 0,
                "texto": f"Hace {_horas(ahora - ultima_pluma)} que no llega ninguna dosis de la pluma. "
                         "Si te has inyectado, léela con LibreLink."}
    return None


def evaluar_bd(con, ahora: datetime, umbral_horas: float = UMBRAL_HORAS, margen_min: int = MARGEN_MIN) -> dict | None:
    """Última dosis de cualquier vía de la pluma (también las descartadas: prueban que se leyó)."""
    ultima = con.execute("SELECT max(ts) AS ts FROM dosis WHERE origen = ANY(%s) AND ts <= %s",
                         (list(ORIGENES_PLUMA), ahora)).fetchone()["ts"]
    pend = [f["ts"] for f in con.execute(
        """SELECT ts FROM dosis WHERE origen = 'app' AND insulina = 'fiasp' AND estado_conciliacion = 'pendiente'
           AND ts BETWEEN %s AND %s""", (ahora - VENTANA_PENDIENTES, ahora)).fetchall()]
    lectura = con.execute("SELECT ultimo_ok FROM estado_libreview").fetchone()
    return evaluar(ahora, ultima, pend, umbral_horas, margen_min, lectura["ultimo_ok"] if lectura else None)
