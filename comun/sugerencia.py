"""Sugerencia de dosis de Fiasp. Cálculo DETERMINISTA, trazable y con tests.

AFECTA A LA DOSIS: cualquier cambio aquí (fórmula, umbrales, redondeo) exige tests y la aprobación
explícita de Daniel, y cambiar VERSION_CALCULO.

Ninguna cifra de este módulo procede de un modelo de lenguaje. Los hidratos entran solo desde una
comida revisada por Daniel o escritos por él; los eventos del chat no entran nunca.

Fórmula (plan, fase 3, tarea 5):
    u_hidratos   = hidratos / ratio
    u_correccion = (glucosa − objetivo_corrección) / sensibilidad     (negativa por debajo del objetivo)
    total        = u_hidratos + u_correccion − insulina_activa
    sugerencia   = max(0, total) redondeada HACIA ABAJO al paso de la pluma (0,5 U)

Con alerta por acumulación de insulina no se suma corrección positiva (la negativa sí resta).

Freno por hipoglucemia (sin predicción hasta la fase 5): no se sugiere NADA si la glucosa es baja,
baja deprisa, baja con glucosa intermedia, no hay lectura reciente o no se sabe la tendencia.

Grasa y proteína (método de Varsovia, plumas sin bolo extendido): 1 UGP = 100 kcal de grasa y
proteína ≈ 10 g de hidratos. Se sugiere como SEGUNDA INYECCIÓN con hora, solo una fracción al
principio, y se vuelve a calcular a su hora con el freno por hipoglucemia (`segunda`).
"""
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

VERSION_CALCULO = "sugerencia-v1"
ZONA = ZoneInfo("Europe/Madrid")


@dataclass(frozen=True)
class Limites:
    """Umbrales de seguridad. Aceptados por Daniel en el hilo el 19-11-2026 (preguntas 2–6)."""
    paso_pluma_u: float = 0.5               # NovoPen Echo Plus
    glucosa_max_antiguedad_min: float = 15
    freno_glucosa_baja: int = 80            # por debajo: nada
    freno_bajada_rapida: float = -2.0       # mg/dL/min o más deprisa (↓ ⇊): nada
    freno_bajada: float = -1.0              # bajando más deprisa que esto (↘)…
    freno_bajada_glucosa: int = 120         # …con glucosa por debajo de esto: nada
    acumulacion_suelo_mgdl: int = 70        # la insulina activa sola llevaría por debajo de esto
    acumulacion_dosis_3h: int = 2           # o tantas dosis de Fiasp en 3 h
    hidratos_correccion_aviso_min: int = 60
    ugp_g_hidratos: float = 10.0            # 1 UGP ≈ 10 g de hidratos
    ugp_minimo: float = 1.0                 # por debajo no hay segunda inyección
    fraccion_segunda: float = 0.5           # parte de la insulina de UGP que se sugiere al principio
    segunda_min: int = 90                   # minutos tras la comida
    segunda_min_ugp_alto: int = 120
    ugp_alto: float = 3.0
    segunda_ventana_antes_min: int = 15     # se puede recalcular desde 15 min antes…
    segunda_ventana_despues_min: int = 120  # …hasta 2 h después de su hora
    rango_ancho_media_u: float = 0.5        # ancho del rango de unidades que sube la incertidumbre
    rango_ancho_alta_u: float = 1.0


LIMITES = Limites()

# Pendiente representativa (mg/dL/min) cuando solo se conoce la flecha (convención de Nightscout).
PENDIENTE_FLECHA = {"DoubleUp": 3.5, "SingleUp": 2.5, "FortyFiveUp": 1.5, "Flat": 0.0,
                    "FortyFiveDown": -1.5, "SingleDown": -2.5, "DoubleDown": -3.5}


@dataclass(frozen=True)
class Entrada:
    ahora: datetime
    tipo: str                               # 'comida' o 'correccion'
    # Parámetros de la versión activa (None si no hay ninguna aprobada)
    version_parametros_id: int | None
    ratio_g_u: float | None
    sensibilidad_mgdl_u: float | None
    objetivo_correccion_mgdl: int | None
    confianza: str | None
    # Glucosa
    glucosa_mgdl: int | None
    glucosa_ts: datetime | None
    pendiente_mgdl_min: float | None
    tendencia: str | None
    # Insulina
    insulina_activa_u: float
    dosis_fiasp_3h: int
    dosis_con_dudas: bool
    estado_pluma: str                       # 'leida', 'no_leida', 'confirmada_a_mano'
    # Comida (gramos revisados por Daniel; nunca de un modelo sin revisar)
    ts_comida: datetime | None = None
    hidratos_g: float = 0.0
    hidratos_min_g: float | None = None
    hidratos_max_g: float | None = None
    grasa_g: float = 0.0
    proteina_g: float = 0.0
    hidratos_correccion_recientes_g: float = 0.0
    avisos_externos: tuple[str, ...] = field(default_factory=tuple)


def abajo(u: float, paso: float) -> float:
    """Redondea hacia abajo al paso de la pluma (con tolerancia de coma flotante)."""
    return math.floor(u / paso + 1e-9) * paso


def pendiente_efectiva(e: Entrada) -> float | None:
    if e.pendiente_mgdl_min is not None:
        return e.pendiente_mgdl_min
    return PENDIENTE_FLECHA.get(e.tendencia or "")


def motivos_freno(e: Entrada, lim: Limites = LIMITES) -> list[str]:
    """Motivos por los que NO se sugiere ninguna dosis. Lista vacía = se puede calcular."""
    m = []
    if e.version_parametros_id is None or not e.ratio_g_u or not e.sensibilidad_mgdl_u:
        m.append("sin_parametros")
    if e.estado_pluma == "no_leida":
        m.append("pluma_sin_leer")
    if e.glucosa_mgdl is None or e.glucosa_ts is None:
        m.append("sin_glucosa")
        return m
    if (e.ahora - e.glucosa_ts).total_seconds() / 60 > lim.glucosa_max_antiguedad_min:
        m.append("glucosa_antigua")
    p = pendiente_efectiva(e)
    if e.glucosa_mgdl < lim.freno_glucosa_baja:
        m.append("glucosa_baja")
    if p is None:
        m.append("sin_tendencia")
    elif p <= lim.freno_bajada_rapida:
        m.append("bajando_rapido")
    elif p < lim.freno_bajada and e.glucosa_mgdl < lim.freno_bajada_glucosa:
        m.append("bajando")
    return m


TEXTO_FRENO = {
    "sin_parametros": "No hay parámetros aprobados (ratio y sensibilidad).",
    "pluma_sin_leer": "No se han podido leer las dosis de la pluma: léela o apunta las dosis que falten.",
    "sin_glucosa": "No hay lectura de glucosa.",
    "glucosa_antigua": "La última lectura de glucosa tiene más de 15 min.",
    "glucosa_baja": "Glucosa por debajo de 80 mg/dL: trata la hipo primero.",
    "sin_tendencia": "No se conoce la tendencia de la glucosa.",
    "bajando_rapido": "La glucosa baja deprisa (2 mg/dL/min o más).",
    "bajando": "La glucosa baja y está por debajo de 120 mg/dL.",
}


def acumulacion(e: Entrada, lim: Limites = LIMITES) -> list[str]:
    m = []
    if e.sensibilidad_mgdl_u and e.glucosa_mgdl is not None and \
            e.insulina_activa_u * e.sensibilidad_mgdl_u > e.glucosa_mgdl - lim.acumulacion_suelo_mgdl:
        m.append("insulina_activa_alta")
    if e.dosis_fiasp_3h >= lim.acumulacion_dosis_3h:
        m.append("varias_dosis_3h")
    return m


def _total(hidratos: float, e: Entrada, acumula: bool, lim: Limites) -> dict:
    u_hc = hidratos / e.ratio_g_u
    u_corr = (e.glucosa_mgdl - e.objetivo_correccion_mgdl) / e.sensibilidad_mgdl_u
    u_corr_usada = min(u_corr, 0.0) if acumula else u_corr
    bruto = u_hc + u_corr_usada - e.insulina_activa_u
    return {"hidratos_g": hidratos, "u_hidratos": u_hc, "u_correccion": u_corr,
            "u_correccion_usada": u_corr_usada, "bruto": bruto,
            "redondeada": abajo(max(0.0, bruto), lim.paso_pluma_u)}


def ugp(grasa_g: float, proteina_g: float) -> float:
    return (grasa_g * 9 + proteina_g * 4) / 100


def calcular(e: Entrada, lim: Limites = LIMITES) -> dict:
    """Sugerencia completa. Devuelve los campos de `sugerencias` más la traza."""
    traza = {"calculo_version": VERSION_CALCULO, "limites": asdict(lim),
             "entrada": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in asdict(e).items()}}
    avisos = list(e.avisos_externos)
    r = {"tipo": e.tipo, "calculo_version": VERSION_CALCULO, "version_parametros_id": e.version_parametros_id,
         "glucosa_mgdl": e.glucosa_mgdl, "tendencia": e.tendencia,
         "insulina_activa_u": round(e.insulina_activa_u, 2), "hidratos_g": e.hidratos_g,
         "ts_comida": e.ts_comida, "grasa_g": e.grasa_g, "proteina_g": e.proteina_g,
         "estado_pluma": e.estado_pluma,
         "u_hidratos": None, "u_correccion": None, "u_inmediata": None, "u_segunda": None,
         "ts_segunda": None, "ugp": None, "bloqueada": False, "motivo_bloqueo": None}

    if e.dosis_con_dudas:
        avisos.append("Hay dosis recientes sin conciliar con la pluma: la insulina activa puede no ser exacta.")
    if e.estado_pluma == "confirmada_a_mano":
        avisos.append("La pluma no se ha leído: cuentan solo las dosis apuntadas.")
    if e.hidratos_correccion_recientes_g > 0:
        avisos.append(f"Tomaste {e.hidratos_correccion_recientes_g:g} g de hidratos de corrección hace menos de "
                      f"{lim.hidratos_correccion_aviso_min} min.")

    freno = motivos_freno(e, lim)
    if freno:
        r |= {"bloqueada": True, "motivo_bloqueo": " ".join(TEXTO_FRENO[m] for m in freno)}
        traza["freno"] = freno
        r |= {"avisos": avisos, "incertidumbre": {"nivel": "no_aplica", "rango_u": None, "motivos": []},
              "traza": traza}
        return r

    acu = acumulacion(e, lim)
    if acu:
        avisos.append("Alerta por acumulación de insulina: no se suma corrección.")
    principal = _total(e.hidratos_g, e, bool(acu), lim)
    if principal["bruto"] < 0:
        avisos.append("Posible exceso de insulina: la insulina activa supera lo necesario.")
    lo = e.hidratos_min_g if e.hidratos_min_g is not None else e.hidratos_g
    hi = e.hidratos_max_g if e.hidratos_max_g is not None else e.hidratos_g
    rango = [_total(lo, e, bool(acu), lim)["redondeada"], _total(hi, e, bool(acu), lim)["redondeada"]]

    motivos = []
    if e.confianza != "alta":
        motivos.append(f"Parámetros con confianza {e.confianza}.")
    if rango[1] - rango[0] >= lim.rango_ancho_media_u:
        motivos.append(f"Los hidratos están entre {lo:g} y {hi:g} g.")
    if e.dosis_con_dudas or e.estado_pluma != "leida":
        motivos.append("Insulina activa con dudas.")
    ancho = rango[1] - rango[0]
    if e.confianza == "baja" or ancho >= lim.rango_ancho_alta_u or e.dosis_con_dudas:
        nivel = "alta"
    elif e.confianza == "media" or ancho >= lim.rango_ancho_media_u or e.estado_pluma != "leida":
        nivel = "media"
    else:
        nivel = "baja"

    r |= {"u_hidratos": round(principal["u_hidratos"], 2), "u_correccion": round(principal["u_correccion"], 2),
          "u_inmediata": principal["redondeada"]}
    traza |= {"acumulacion": acu, "principal": principal, "rango": {"hidratos_g": [lo, hi], "u": rango}}

    if e.tipo == "comida":
        u_gp = ugp(e.grasa_g, e.proteina_g)
        r["ugp"] = round(u_gp, 2)
        if u_gp >= lim.ugp_minimo:
            completa = u_gp * lim.ugp_g_hidratos / e.ratio_g_u
            u2 = abajo(completa * lim.fraccion_segunda, lim.paso_pluma_u)
            minutos = lim.segunda_min_ugp_alto if u_gp >= lim.ugp_alto else lim.segunda_min
            traza["segunda"] = {"ugp": u_gp, "u_completa": completa, "fraccion": lim.fraccion_segunda,
                                "redondeada": u2, "minutos": minutos}
            if u2 > 0:
                r |= {"u_segunda": u2, "ts_segunda": (e.ts_comida or e.ahora) + timedelta(minutes=minutos)}
                avisos.append("Segunda inyección por grasa y proteína: a su hora se vuelve a comprobar la glucosa.")

    r |= {"avisos": avisos, "incertidumbre": {"nivel": nivel, "rango_u": rango, "motivos": motivos},
          "traza": traza}
    return r


def segunda(e: Entrada, u_prevista: float, ts_prevista: datetime, lim: Limites = LIMITES) -> dict:
    """Vuelve a comprobar la segunda inyección a su hora. Las unidades son las ya calculadas
    (no se resta la insulina activa: incluye la primera inyección, que cubre los hidratos); solo
    se aplica el freno por hipoglucemia, la ventana horaria y la alerta por acumulación."""
    traza = {"calculo_version": VERSION_CALCULO, "limites": asdict(lim), "u_prevista": u_prevista,
             "ts_prevista": ts_prevista.isoformat(),
             "entrada": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in asdict(e).items()}}
    avisos = list(e.avisos_externos)
    r = {"tipo": "segunda", "calculo_version": VERSION_CALCULO, "version_parametros_id": e.version_parametros_id,
         "glucosa_mgdl": e.glucosa_mgdl, "tendencia": e.tendencia,
         "insulina_activa_u": round(e.insulina_activa_u, 2), "hidratos_g": None, "ts_comida": e.ts_comida,
         "grasa_g": None, "proteina_g": None, "ugp": None, "estado_pluma": e.estado_pluma,
         "u_hidratos": None, "u_correccion": None, "u_inmediata": None, "u_segunda": None, "ts_segunda": None,
         "bloqueada": False, "motivo_bloqueo": None}
    freno = motivos_freno(e, lim)
    minutos = (e.ahora - ts_prevista).total_seconds() / 60
    if minutos < -lim.segunda_ventana_antes_min:
        freno.append("segunda_pronto")
    elif minutos > lim.segunda_ventana_despues_min:
        freno.append("segunda_tarde")
    textos = TEXTO_FRENO | {
        "segunda_pronto": f"Aún es pronto: la segunda inyección es a las {ts_prevista.astimezone(ZONA):%H:%M}.",
        "segunda_tarde": "Han pasado más de 2 h de la hora prevista: ya no se sugiere."}
    if freno:
        traza["freno"] = freno
        return r | {"bloqueada": True, "motivo_bloqueo": " ".join(textos[m] for m in freno), "avisos": avisos,
                    "incertidumbre": {"nivel": "no_aplica", "rango_u": None, "motivos": []}, "traza": traza}
    acu = acumulacion(e, lim)
    traza["acumulacion"] = acu
    if acu:
        avisos.append("Alerta por acumulación de insulina: valora no ponerla.")
    if e.dosis_con_dudas:
        avisos.append("Hay dosis recientes sin conciliar con la pluma.")
    return r | {"u_inmediata": u_prevista, "avisos": avisos,
                "incertidumbre": {"nivel": "alta", "rango_u": [0.0, u_prevista],
                                  "motivos": ["La absorción de grasa y proteína es muy variable."]},
                "traza": traza}
