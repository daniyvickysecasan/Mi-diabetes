"""Insulina activa (IOB) de Fiasp. Cálculo determinista, sin base de datos.

Modelo exponencial de OpenAPS (oref0, `lib/iob/calculate.js`, Dragan Maksimovic):
la actividad de una dosis de U unidades a los t minutos es

    actividad(t) = U · S/τ² · t · (1 − t/D) · e^(−t/τ)          0 ≤ t ≤ D

con pico en tp y duración D, y

    τ = tp·(1 − tp/D) / (1 − 2·tp/D)
    a = 2·τ/D
    S = 1 / (1 − a + (1 + a)·e^(−D/τ))

La insulina que queda es U menos la integral de la actividad:

    iob(t) = U · (1 − S·(1 − a)·((t²/(τ·D·(1 − a)) − t/τ − 1)·e^(−t/τ) + 1))

Parámetros de Fiasp: los de oref0 para insulinas ultrarrápidas ("ultra-rapid": pico 55 min) con
duración de 5 h (el mínimo que oref0 admite para la curva exponencial).

CAMBIAR PICO O DURACIÓN AFECTA A LA DOSIS: exige tests y la aprobación explícita de Daniel.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

VERSION_CURVA = "fiasp-oref0-exp-v1"


@dataclass(frozen=True)
class Curva:
    pico_min: float = 55.0
    duracion_min: float = 300.0

    def __post_init__(self):
        if not 0 < self.pico_min < self.duracion_min / 2:
            raise ValueError("el pico debe estar entre 0 y la mitad de la duración")

    @property
    def _tau(self) -> float:
        tp, td = self.pico_min, self.duracion_min
        return tp * (1 - tp / td) / (1 - 2 * tp / td)

    @property
    def _a(self) -> float:
        return 2 * self._tau / self.duracion_min

    @property
    def _s(self) -> float:
        tau, a = self._tau, self._a
        return 1 / (1 - a + (1 + a) * math.exp(-self.duracion_min / tau))

    def fraccion_activa(self, minutos: float) -> float:
        """Fracción de la dosis que queda por actuar (1 al inyectar, 0 al acabar)."""
        if minutos <= 0:
            return 1.0
        if minutos >= self.duracion_min:
            return 0.0
        t, td, tau, a, s = minutos, self.duracion_min, self._tau, self._a, self._s
        f = 1 - s * (1 - a) * ((t * t / (tau * td * (1 - a)) - t / tau - 1) * math.exp(-t / tau) + 1)
        return min(1.0, max(0.0, f))

    def actividad(self, minutos: float) -> float:
        """Fracción de la dosis que actúa por minuto en ese instante (U/min por unidad)."""
        if minutos <= 0 or minutos >= self.duracion_min:
            return 0.0
        t, td, tau, s = minutos, self.duracion_min, self._tau, self._s
        return s / (tau * tau) * t * (1 - t / td) * math.exp(-t / tau)


FIASP = Curva()


def insulina_activa(dosis: list[dict], ahora: datetime, curva: Curva = FIASP) -> dict:
    """Suma de la insulina activa de una lista de dosis {"ts": datetime, "unidades": número}.

    Las dosis posteriores a `ahora` no cuentan (todavía no se han puesto).
    """
    total = actividad = 0.0
    consideradas = []
    for d in dosis:
        minutos = (ahora - d["ts"]).total_seconds() / 60
        if minutos < 0 or minutos >= curva.duracion_min:
            continue
        u = float(d["unidades"])
        resto = u * curva.fraccion_activa(minutos)
        total += resto
        actividad += u * curva.actividad(minutos)
        consideradas.append({**d, "minutos": round(minutos, 1), "activa_u": round(resto, 3)})
    return {
        "unidades": round(total, 2),
        "actividad_u_por_h": round(actividad * 60, 3),
        "dosis": consideradas,
        "curva": {"version": VERSION_CURVA, "pico_min": curva.pico_min, "duracion_min": curva.duracion_min},
    }


def proyeccion(dosis: list[dict], ahora: datetime, horas: float = 5, paso_min: int = 10,
               curva: Curva = FIASP) -> list[dict]:
    """Insulina activa prevista desde ahora, suponiendo que no se pone ninguna dosis más."""
    pasos = int(horas * 60 // paso_min)
    return [{"ts": ahora + timedelta(minutes=i * paso_min),
             "unidades": insulina_activa(dosis, ahora + timedelta(minutes=i * paso_min), curva)["unidades"]}
            for i in range(pasos + 1)]


def insulina_activa_bd(con, ahora: datetime, curva: Curva = FIASP) -> dict:
    """Insulina activa de Fiasp leyendo `dosis_efectivas` (cada inyección real una sola vez)."""
    filas = con.execute(
        """SELECT id, ts, unidades, origen, estado_conciliacion, requiere_revision
           FROM dosis_efectivas
           WHERE insulina = 'fiasp' AND ts <= %s AND ts > %s ORDER BY ts""",
        (ahora, ahora - timedelta(minutes=curva.duracion_min))).fetchall()
    res = insulina_activa(filas, ahora, curva)
    res["dosis"] = [{"id": d["id"], "ts": d["ts"].isoformat(), "unidades": float(d["unidades"]),
                     "origen": d["origen"], "estado": d["estado_conciliacion"],
                     "requiere_revision": d["requiere_revision"], "minutos": d["minutos"],
                     "activa_u": d["activa_u"]} for d in res["dosis"]]
    res["con_dudas"] = any(d["requiere_revision"] or d["estado"] == "pendiente" for d in res["dosis"])
    res["ts"] = ahora.isoformat()
    return res
