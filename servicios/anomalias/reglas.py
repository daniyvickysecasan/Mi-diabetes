"""Reglas de detección de anomalías (fase 4). Funciones PURAS: sin base de datos ni red.

Cada regla recibe series ya leídas y devuelve episodios {"tipo", "inicio", "fin", "datos"}. El
servicio (detector.py) los guarda en `anomalias`, y la evaluación (evaluar.py) los compara con los
episodios que Daniel etiqueta a mano.

NADA DE ESTO ENTRA EN UN CÁLCULO DE DOSIS. Las anomalías solo preguntan o avisan; la sugerencia
(comun/sugerencia.py) no lee la tabla `anomalias` (hay un test que lo comprueba). La alerta por
acumulación de aquí es un AVISO por ntfy; el freno de la sugerencia sigue siendo el suyo.

Todos los umbrales están en `Umbrales` y se cambian con variables de entorno ANOMALIA_<NOMBRE>
(p. ej. ANOMALIA_HUECO_MIN=40). Los valores por defecto los aprobó Daniel el 20-11-2026, también
los que tocan la seguridad (acumulación de insulina y posible olvido de Tresiba).
"""
import os
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

VERSION = "anomalias-v1"
ZONA = ZoneInfo("Europe/Madrid")
TIPOS = ("hueco", "presion", "subida_sin_comida", "dosis_sin_comida", "acumulacion", "olvido_tresiba")


@dataclass(frozen=True)
class Umbrales:
    # Hueco de datos: sin lecturas durante más de esto.
    hueco_min: int = 30
    # Caída por presión sobre el sensor: bajada rápida y rebote sin hidratos.
    presion_caida_mgdl: int = 30            # bajada mínima desde el máximo previo
    presion_ventana_caida_min: int = 30     # el máximo previo se busca en esta ventana
    presion_pendiente: float = 2.0          # mg/dL/min de media en la bajada (o más deprisa)
    presion_recuperacion: float = 0.7       # recupera al menos esta fracción de lo que bajó…
    presion_recuperacion_min: int = 60      # …en este tiempo desde el mínimo
    # Subida sin comida.
    subida_mgdl: int = 60                   # subida mínima…
    subida_ventana_min: int = 120           # …en este tiempo…
    subida_final_mgdl: int = 160            # …hasta al menos este valor
    subida_sin_comida_antes_min: int = 180  # sin comida ni hidratos desde 3 h antes del inicio
    # Fiasp sin comida.
    dosis_espera_min: int = 60              # se pregunta pasado este tiempo (para dar tiempo a apuntar)
    dosis_comida_antes_min: int = 180       # una comida hasta 3 h antes la explica (segunda inyección)
    dosis_comida_despues_min: int = 60      # o hasta 1 h después
    dosis_ventana_h: int = 6                # el servicio solo mira las dosis de las últimas 6 h
    # Acumulación de insulina (AVISO URGENTE; aprobado por Daniel el 20-11-2026).
    acumulacion_suelo_mgdl: int = 70        # la insulina sin hidratos que la cubran llevaría por debajo
    acumulacion_min_u: float = 0.5          # exceso mínimo para avisar (un paso de la pluma)
    absorcion_comida_min: int = 180         # hidratos de una comida: absorción lineal en 3 h
    absorcion_hc_min: int = 60              # hidratos de corrección: en 1 h
    acumulacion_repetir_min: int = 60       # si sigue, se repite como mucho cada 60 min
    glucosa_max_antiguedad_min: int = 15
    # Posible olvido de Tresiba (aprobado por Daniel el 20-11-2026).
    tresiba_horas: float = 4.0              # tantas horas seguidas…
    tresiba_mgdl: int = 180                 # …por encima de esto…
    tresiba_fraccion: float = 0.9           # …con al menos esta fracción de lecturas…
    tresiba_sin_nada_antes_h: float = 2.0   # …y sin comida, hidratos ni Fiasp desde 2 h antes
    tresiba_cobertura: float = 0.7          # hacen falta lecturas en el 70 % de la ventana
    # Notificaciones (avisos.py)
    silencio_desde: str = "23:00"           # las no urgentes esperan a la mañana
    silencio_hasta: str = "08:00"
    max_avisos_dia: int = 6                 # no urgentes; el resto se queda solo en la app
    margen_solape_min: int = 30             # dos episodios del mismo tipo así de cerca son el mismo
    desactivadas: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def desde_entorno(cls, entorno: dict | None = None) -> "Umbrales":
        e = os.environ if entorno is None else entorno
        valores = {}
        for f in fields(cls):
            v = e.get(f"ANOMALIA_{f.name.upper()}")
            if v is None or v.strip() == "":
                continue
            if f.name == "desactivadas":
                valores[f.name] = tuple(t.strip() for t in v.split(",") if t.strip())
            elif f.type in (int, "int"):
                valores[f.name] = int(v)
            elif f.type in (float, "float"):
                valores[f.name] = float(v.replace(",", "."))
            else:
                valores[f.name] = v.strip()
        u = cls(**valores)
        desconocidas = set(u.desactivadas) - set(TIPOS)
        if desconocidas:
            raise ValueError(f"ANOMALIA_DESACTIVADAS con tipos desconocidos: {sorted(desconocidas)}")
        return u

    def como_dict(self) -> dict:
        d = asdict(self)
        d["desactivadas"] = list(self.desactivadas)
        return d


UMBRALES = Umbrales()


def _min(td: timedelta) -> float:
    return td.total_seconds() / 60


def _hl(ts: datetime) -> str:
    return f"{ts.astimezone(ZONA):%H:%M}"


def _num(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def _hay_en(eventos: list[datetime], desde: datetime, hasta: datetime) -> bool:
    """`eventos` ORDENADOS (las reglas los ordenan al entrar)."""
    i = bisect_left(eventos, desde)
    return i < len(eventos) and eventos[i] <= hasta


def _episodio(tipo, inicio, fin, datos) -> dict:
    return {"tipo": tipo, "inicio": inicio, "fin": fin, "datos": datos}


# ------------------------------------------------------------------ huecos
def huecos(lecturas: list[tuple[datetime, int]], ahora: datetime, u: Umbrales = UMBRALES,
           ultima_previa: datetime | None = None, solo_abierto: bool = False) -> list[dict]:
    """Tramos sin lecturas de más de `hueco_min`. `fin` None = sigue abierto.
    `ultima_previa`: última lectura anterior a la serie (para un hueco que empezó antes)."""
    tiempos = sorted(t for t, _ in lecturas)
    if ultima_previa is not None:
        tiempos = [ultima_previa] + [t for t in tiempos if t > ultima_previa]
    out = []
    if not solo_abierto:
        for a, b in zip(tiempos, tiempos[1:]):
            if _min(b - a) > u.hueco_min:
                out.append(_episodio("hueco", a, b, {"minutos": round(_min(b - a)), "abierto": False}))
    if tiempos and _min(ahora - tiempos[-1]) > u.hueco_min:
        out.append(_episodio("hueco", tiempos[-1], None, {"minutos": round(_min(ahora - tiempos[-1])), "abierto": True}))
    return out


# ------------------------------------------------------------------ presión sobre el sensor
def presion(lecturas: list[tuple[datetime, int]], hidratos: list[datetime], u: Umbrales = UMBRALES) -> list[dict]:
    """Bajada rápida seguida de un rebote rápido sin hidratos (comida o de corrección) entre medias.
    Solo se ve DESPUÉS del rebote: es una pregunta a posteriori, nunca quita importancia a una bajada."""
    s, hidratos = sorted(lecturas), sorted(hidratos)
    out, i, n = [], 1, len(s)
    while i < n - 1:
        t, v = s[i]
        # mínimo local
        if not (v <= s[i - 1][1] and v <= s[i + 1][1]):
            i += 1
            continue
        previas = [(tp, vp) for tp, vp in s[max(0, i - 60):i] if timedelta(0) < t - tp <= timedelta(minutes=u.presion_ventana_caida_min)]
        if not previas:
            i += 1
            continue
        tp, vp = max(previas, key=lambda x: (x[1], x[0]))
        caida = vp - v
        dur = _min(t - tp)
        if caida < u.presion_caida_mgdl or dur <= 0 or caida / dur < u.presion_pendiente:
            i += 1
            continue
        objetivo = v + u.presion_recuperacion * caida
        rec = next(((tr, vr) for tr, vr in s[i + 1:i + 90]
                    if t < tr <= t + timedelta(minutes=u.presion_recuperacion_min) and vr >= objetivo), None)
        if rec is None or _hay_en(hidratos, tp, rec[0]):
            i += 1
            continue
        out.append(_episodio("presion", tp, rec[0], {
            "desde_mgdl": vp, "minimo_mgdl": v, "minimo_ts": t.isoformat(), "vuelta_mgdl": rec[1],
            "caida_mgdl": caida, "minutos_caida": round(dur), "minutos_rebote": round(_min(rec[0] - t))}))
        while i < n and s[i][0] <= rec[0]:
            i += 1
    return out


# ------------------------------------------------------------------ subida sin comida
def subida_sin_comida(lecturas: list[tuple[datetime, int]], comidas: list[datetime], u: Umbrales = UMBRALES,
                      excluir: list[tuple[datetime, datetime]] = ()) -> list[dict]:
    """Subida de al menos `subida_mgdl` en `subida_ventana_min` hasta `subida_final_mgdl` o más, sin
    comida, hidratos ni evento de comida del chat desde `subida_sin_comida_antes_min` antes del inicio.
    `excluir`: tramos que no cuentan como inicio (p. ej. el rebote tras una caída por presión)."""
    s, comidas = sorted(lecturas), sorted(comidas)
    out, i, n = [], 0, len(s)
    ventana = timedelta(minutes=u.subida_ventana_min)
    inicio_j = 0
    while i < n:
        t, v = s[i]
        while s[inicio_j][0] < t - ventana:
            inicio_j += 1
        tramo = s[inicio_j:i + 1]
        if v < u.subida_final_mgdl or len(tramo) < 2:
            i += 1
            continue
        tm, vm = min(tramo, key=lambda x: (x[1], -x[0].timestamp()))
        if v - vm < u.subida_mgdl:
            i += 1
            continue
        desde_min = [x for x in tramo if x[0] >= tm]
        continuo = all(_min(b[0] - a[0]) <= 20 for a, b in zip(desde_min, desde_min[1:]))
        excluido = any(a <= tm <= b for a, b in excluir)
        if not continuo or excluido or _hay_en(comidas, tm - timedelta(minutes=u.subida_sin_comida_antes_min), t):
            i += 1
            continue
        # sigue hasta el máximo (mientras no baje más de 20 mg/dL desde el máximo, como mucho 3 h)
        j, tmax, vmax = i, t, v
        while j + 1 < n and s[j + 1][0] <= t + timedelta(hours=3) and _min(s[j + 1][0] - s[j][0]) <= 20 \
                and s[j + 1][1] > vmax - 20:
            j += 1
            if s[j][1] > vmax:
                tmax, vmax = s[j]
        if _hay_en(comidas, t, tmax):   # comió durante la subida: el tramo explicado no cuenta
            tmax, vmax = t, v
        out.append(_episodio("subida_sin_comida", tm, tmax, {
            "desde_mgdl": vm, "hasta_mgdl": vmax, "subida_mgdl": vmax - vm, "detectada_ts": t.isoformat()}))
        while i < n and s[i][0] <= tmax:
            i += 1
        while i < n and s[i][1] > vm + u.subida_mgdl / 2 and s[i][0] <= tmax + timedelta(hours=3):
            i += 1      # mientras siga alta, es la misma subida
    return out


# ------------------------------------------------------------------ Fiasp sin comida
def dosis_sin_comida(dosis: list[dict], comidas: list[datetime], ahora: datetime, u: Umbrales = UMBRALES) -> list[dict]:
    """Dosis de Fiasp sin comida cerca ni vínculo con una comida o sugerencia.
    `dosis`: {"id", "ts", "unidades", "comida_id", "sugerencia_tipo"}."""
    out, comidas = [], sorted(comidas)
    for d in dosis:
        if _min(ahora - d["ts"]) < u.dosis_espera_min:
            continue
        if d.get("comida_id") is not None or d.get("sugerencia_tipo") in ("comida", "segunda", "correccion"):
            continue
        if _hay_en(comidas, d["ts"] - timedelta(minutes=u.dosis_comida_antes_min),
                   d["ts"] + timedelta(minutes=u.dosis_comida_despues_min)):
            continue
        out.append(_episodio("dosis_sin_comida", d["ts"], d["ts"],
                             {"dosis_id": d["id"], "unidades": float(d["unidades"])}))
    return out


# ------------------------------------------------------------------ acumulación de insulina
def hidratos_sin_absorber(comidas: list[dict], ahora: datetime, u: Umbrales = UMBRALES) -> list[dict]:
    """Absorción lineal. `comidas`: {"ts", "gramos", "tipo": 'comida'|'correccion', "ratio_g_u"}.
    Devuelve las que aún tienen hidratos por absorber, con las unidades que los cubren."""
    out = []
    for c in comidas:
        dur = u.absorcion_comida_min if c["tipo"] == "comida" else u.absorcion_hc_min
        m = _min(ahora - c["ts"])
        if m < 0 or m >= dur or not c["gramos"]:
            continue
        resto = float(c["gramos"]) * (1 - m / dur)
        out.append({**c, "resto_g": round(resto, 1),
                    "cubre_u": round(resto / c["ratio_g_u"], 2) if c.get("ratio_g_u") else 0.0})
    return out


def acumulacion(ahora: datetime, glucosa_mgdl: int | None, glucosa_ts: datetime | None, insulina_activa_u: float,
                sensibilidad: float | None, comidas: list[dict], dosis_3h: int, u: Umbrales = UMBRALES) -> dict | None:
    """Aviso si la insulina activa que no cubren los hidratos aún sin absorber, por la sensibilidad,
    llevaría la glucosa por debajo de `acumulacion_suelo_mgdl`. Sin parámetros o sin glucosa
    reciente no se evalúa (None). No cambia ninguna dosis."""
    if not sensibilidad or glucosa_mgdl is None or glucosa_ts is None:
        return None
    if _min(ahora - glucosa_ts) > u.glucosa_max_antiguedad_min:
        return None
    pendientes = hidratos_sin_absorber(comidas, ahora, u)
    cubre = sum(c["cubre_u"] for c in pendientes)
    exceso = insulina_activa_u - cubre
    bajada = exceso * sensibilidad
    if exceso < u.acumulacion_min_u or bajada <= glucosa_mgdl - u.acumulacion_suelo_mgdl:
        return None
    return _episodio("acumulacion", ahora, ahora, {
        "glucosa_mgdl": glucosa_mgdl, "insulina_activa_u": round(insulina_activa_u, 2),
        "cubierta_u": round(cubre, 2), "exceso_u": round(exceso, 2), "sensibilidad": sensibilidad,
        "bajada_mgdl": round(bajada), "dosis_3h": dosis_3h,
        "hidratos_sin_absorber_g": round(sum(c["resto_g"] for c in pendientes), 1)})


# ------------------------------------------------------------------ posible olvido de Tresiba
def olvido_tresiba(lecturas: list[tuple[datetime, int]], eventos: list[datetime], en: datetime,
                   hay_tresiba: bool, u: Umbrales = UMBRALES) -> dict | None:
    """Subida sostenida sin comida ni dosis: `tresiba_horas` seguidas por encima de `tresiba_mgdl`
    (al menos `tresiba_fraccion` de las lecturas), sin bajar, sin comida, hidratos ni Fiasp
    (`eventos`) desde `tresiba_sin_nada_antes_h` antes. La Tresiba se sigue dando por puesta:
    solo se pregunta."""
    if not hay_tresiba:
        return None
    s = sorted(lecturas)
    tiempos = [t for t, _ in s]
    return _olvido(s, tiempos, sorted(eventos), en, u)


def _olvido(s, tiempos, eventos, en, u) -> dict | None:
    desde = en - timedelta(hours=u.tresiba_horas)
    tramo = s[bisect_left(tiempos, desde):bisect_right(tiempos, en)]
    if not tramo:
        return None
    esperadas = u.tresiba_horas * 60 / 5
    if len({int(t.timestamp() // 300) for t, _ in tramo}) < u.tresiba_cobertura * esperadas:
        return None
    altas = sum(1 for _, v in tramo if v > u.tresiba_mgdl)
    if altas < u.tresiba_fraccion * len(tramo):
        return None
    pend = pendiente_mgdl_h(tramo)
    if pend is None or pend < 0:
        return None
    if _hay_en(eventos, desde - timedelta(hours=u.tresiba_sin_nada_antes_h), en):
        return None
    return _episodio("olvido_tresiba", desde, en, {
        "media_mgdl": round(sum(v for _, v in tramo) / len(tramo)), "ultima_mgdl": tramo[-1][1],
        "pct_por_encima": round(100 * altas / len(tramo)), "pendiente_mgdl_h": round(pend, 1),
        "horas": u.tresiba_horas})


def pendiente_mgdl_h(tramo: list[tuple[datetime, int]]) -> float | None:
    """Pendiente por mínimos cuadrados (mg/dL por hora)."""
    if len(tramo) < 3:
        return None
    t0 = tramo[0][0]
    xs = [_min(t - t0) / 60 for t, _ in tramo]
    ys = [float(v) for _, v in tramo]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def olvidos_tresiba_historico(lecturas: list[tuple[datetime, int]], eventos: list[datetime],
                              dias_con_tresiba: set[date], u: Umbrales = UMBRALES) -> list[dict]:
    """Recorre el histórico cada 5 min y se queda con el primer aviso de cada día local."""
    s, eventos = sorted(lecturas), sorted(eventos)
    tiempos = [t for t, _ in s]
    out, vistos = [], set()
    for t in tiempos:
        dia = t.astimezone(ZONA).date()
        if dia in vistos or dia not in dias_con_tresiba:
            continue
        e = _olvido(s, tiempos, eventos, t, u)
        if e:
            out.append(e)
            vistos.add(dia)
    return out


# ------------------------------------------------------------------ textos y preguntas
def opcion(clave: str, texto: str, falsa_alarma: bool = False) -> dict:
    return {"clave": clave, "texto": texto, "falsa_alarma": falsa_alarma}


NO_SE = opcion("no_se", "No lo sé")

PREGUNTAS = {
    "hueco": ("¿Qué ha pasado con el sensor?", [
        opcion("sensor_nuevo", "Sensor nuevo o calentando"),
        opcion("movil", "Móvil lejos o sin conexión"),
        opcion("sensor_fallo", "El sensor ha fallado"),
        opcion("sistema", "Fallo de LibreLinkUp, Nightscout o el PC"), NO_SE]),
    "presion": ("¿Estabas apoyado sobre el sensor?", [
        opcion("presion", "Sí, fue por presión"),
        opcion("bajada_real", "No, fue una bajada real", falsa_alarma=True), NO_SE]),
    "subida_sin_comida": ("¿Qué fue esta subida?", [
        opcion("comida_sin_apuntar", "Comí algo sin apuntarlo"),
        opcion("estres_enfermedad", "Estrés o enfermedad"),
        opcion("alba", "Al despertar (fenómeno del alba)"),
        opcion("ejercicio", "Ejercicio intenso"),
        opcion("rebote", "Rebote tras una bajada"), NO_SE]),
    "dosis_sin_comida": ("¿Para qué fue esa Fiasp?", [
        opcion("correccion", "Corrección de una subida"),
        opcion("comida_sin_apuntar", "Comí y no lo apunté"),
        opcion("segunda", "Segunda inyección de una comida"),
        opcion("error", "No me la puse: es un error", falsa_alarma=True), NO_SE]),
    "acumulacion": ("¿Qué has hecho?", [
        opcion("vigilo", "Visto, lo vigilo"),
        opcion("hidratos", "He tomado hidratos"),
        opcion("no_era", "No era para tanto", falsa_alarma=True)]),
    "olvido_tresiba": ("¿Te has puesto hoy la Tresiba?", [
        opcion("puesta", "Sí, me la puse", falsa_alarma=True),
        opcion("olvidada", "No, se me olvidó"),
        opcion("no_recuerdo", "No me acuerdo")]),
}

URGENTES = {"acumulacion"}


def redactar(e: dict) -> dict:
    """Título, texto, pregunta y opciones de un episodio. Las cifras son las medidas, nada estimado
    por un modelo de lenguaje, y ningún texto da unidades de insulina que ponerse."""
    d, tipo = e["datos"], e["tipo"]
    if tipo == "hueco":
        titulo = "No llegan lecturas"
        texto = (f"No hay lecturas de glucosa desde las {_hl(e['inicio'])} ({d['minutos']} min)." if d.get("abierto")
                 else f"No hubo lecturas de glucosa entre las {_hl(e['inicio'])} y las {_hl(e['fin'])} ({d['minutos']} min).")
    elif tipo == "presion":
        titulo = "¿Bajada por presión?"
        texto = (f"A las {_hl(datetime.fromisoformat(d['minimo_ts']))} la glucosa bajó de {d['desde_mgdl']} a {d['minimo_mgdl']} "
                 f"en {d['minutos_caida']} min y volvió a {d['vuelta_mgdl']} en {d['minutos_rebote']} min sin tomar hidratos.")
    elif tipo == "subida_sin_comida":
        titulo = "Subida sin comida apuntada"
        texto = (f"Desde las {_hl(e['inicio'])} la glucosa subió de {d['desde_mgdl']} a {d['hasta_mgdl']} "
                 "sin comida ni hidratos apuntados.")
    elif tipo == "dosis_sin_comida":
        titulo = "Fiasp sin comida"
        texto = f"A las {_hl(e['inicio'])} te pusiste {_num(d['unidades'])} U de Fiasp y no hay ninguna comida apuntada cerca."
    elif tipo == "acumulacion":
        titulo = "Acumulación de insulina"
        texto = (f"Tienes {_num(d['insulina_activa_u'])} U de Fiasp activas y {_num(d['exceso_u'])} U no las cubren "
                 f"hidratos sin absorber. Con tu sensibilidad podrían bajarte unos {d['bajada_mgdl']} mg/dL "
                 f"desde {d['glucosa_mgdl']}. Vigila la glucosa.")
        if d.get("dosis_3h", 0) >= 2:
            texto += f" Llevas {d['dosis_3h']} dosis de Fiasp en 3 h."
    elif tipo == "olvido_tresiba":
        titulo = "¿Te has puesto la Tresiba?"
        texto = (f"Llevas {_num(d['horas'])} h por encima de 180 (ahora {d['ultima_mgdl']}) sin comida ni Fiasp apuntadas. "
                 "A veces es un olvido de la Tresiba. Solo es una pregunta: no cambia ningún cálculo.")
    else:
        raise ValueError(tipo)
    pregunta, opciones = PREGUNTAS[tipo]
    return {"titulo": titulo, "texto": texto, "pregunta": pregunta, "opciones": opciones,
            "urgente": tipo in URGENTES}


def avisar_desde(detectada: datetime, urgente: bool, u: Umbrales = UMBRALES) -> datetime:
    """Las no urgentes que se detectan en las horas de silencio esperan a que acaben."""
    if urgente:
        return detectada
    local = detectada.astimezone(ZONA)
    ini, fin = time.fromisoformat(u.silencio_desde), time.fromisoformat(u.silencio_hasta)
    h = local.time()
    dentro = (ini <= h or h < fin) if ini > fin else (ini <= h < fin)
    if not dentro:
        return detectada
    dia = local.date() if h < fin else local.date() + timedelta(days=1)
    return datetime.combine(dia, fin, ZONA).astimezone(detectada.tzinfo)
