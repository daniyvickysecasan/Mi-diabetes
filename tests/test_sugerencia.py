"""Cálculo puro de la sugerencia de dosis (sin base de datos). AFECTA A LA DOSIS."""
import ast
import dataclasses
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from comun import sugerencia as S

AHORA = datetime(2026, 11, 19, 13, 0, tzinfo=timezone.utc)   # 14:00 en Madrid


def entrada(**cambios) -> S.Entrada:
    base = dict(ahora=AHORA, tipo="comida", version_parametros_id=1, ratio_g_u=10.0, sensibilidad_mgdl_u=40.0,
                objetivo_correccion_mgdl=110, confianza="media", glucosa_mgdl=150,
                glucosa_ts=AHORA - timedelta(minutes=3), pendiente_mgdl_min=0.0, tendencia="Flat",
                insulina_activa_u=0.0, dosis_fiasp_3h=0, dosis_con_dudas=False, estado_pluma="leida",
                ts_comida=AHORA, hidratos_g=60.0)
    return S.Entrada(**(base | cambios))


# ------------------------------------------------------------------ fórmula
def test_hidratos_mas_correccion():
    r = S.calcular(entrada())
    assert r["u_hidratos"] == 6.0 and r["u_correccion"] == 1.0
    assert r["u_inmediata"] == 7.0 and not r["bloqueada"]


def test_redondea_siempre_hacia_abajo_a_media_unidad():
    assert S.calcular(entrada(hidratos_g=69))["u_inmediata"] == 7.5      # 6,9 + 1 = 7,9 → 7,5
    assert S.calcular(entrada(hidratos_g=64))["u_inmediata"] == 7.0      # 7,4 → 7,0
    assert S.abajo(1.5, 0.5) == 1.5 and S.abajo(1.4999999999, 0.5) == 1.5 and S.abajo(0.49, 0.5) == 0


def test_resta_la_insulina_activa():
    assert S.calcular(entrada(insulina_activa_u=1.2))["u_inmediata"] == 5.5     # 7 − 1,2 = 5,8


def test_correccion_negativa_por_debajo_de_110():
    r = S.calcular(entrada(glucosa_mgdl=90))
    assert r["u_correccion"] == -0.5 and r["u_inmediata"] == 5.5


def test_total_negativo_da_cero_con_aviso():
    r = S.calcular(entrada(hidratos_g=0, glucosa_mgdl=100, insulina_activa_u=1.0, dosis_fiasp_3h=1))
    assert r["u_inmediata"] == 0 and not r["bloqueada"]
    assert any("exceso" in a for a in r["avisos"])


def test_correccion_sola():
    r = S.calcular(entrada(tipo="correccion", hidratos_g=0, glucosa_mgdl=250))
    assert r["u_inmediata"] == 3.5 and r["u_segunda"] is None                  # 140 / 40 = 3,5


def test_usa_el_objetivo_de_correccion_no_el_de_90():
    r = S.calcular(entrada(hidratos_g=0, glucosa_mgdl=110))
    assert r["u_correccion"] == 0


# ------------------------------------------------------------------ freno por hipoglucemia
@pytest.mark.parametrize("cambios, motivo", [
    ({"glucosa_mgdl": 79}, "glucosa_baja"),
    ({"pendiente_mgdl_min": -2.0, "glucosa_mgdl": 250}, "bajando_rapido"),
    ({"pendiente_mgdl_min": None, "tendencia": "DoubleDown", "glucosa_mgdl": 300}, "bajando_rapido"),
    ({"pendiente_mgdl_min": None, "tendencia": "SingleDown"}, "bajando_rapido"),
    ({"pendiente_mgdl_min": -1.2, "glucosa_mgdl": 119}, "bajando"),
    ({"pendiente_mgdl_min": None, "tendencia": "FortyFiveDown", "glucosa_mgdl": 100}, "bajando"),
    ({"pendiente_mgdl_min": None, "tendencia": None}, "sin_tendencia"),
    ({"glucosa_ts": AHORA - timedelta(minutes=16)}, "glucosa_antigua"),
    ({"glucosa_mgdl": None, "glucosa_ts": None}, "sin_glucosa"),
    ({"version_parametros_id": None, "ratio_g_u": None, "sensibilidad_mgdl_u": None}, "sin_parametros"),
    ({"estado_pluma": "no_leida"}, "pluma_sin_leer"),
    ({"estado_pluma": "no_leida", "lectura_ok": True}, "pluma_sin_confirmar"),
])
def test_freno_bloquea_sin_ninguna_cifra(cambios, motivo):
    r = S.calcular(entrada(grasa_g=30, proteina_g=30, **cambios))
    assert r["bloqueada"] is True and motivo in r["traza"]["freno"]
    assert r["u_inmediata"] is None and r["u_segunda"] is None and r["motivo_bloqueo"]


@pytest.mark.parametrize("cambios", [
    {"glucosa_mgdl": 80},
    {"pendiente_mgdl_min": -1.9, "glucosa_mgdl": 120},
    {"pendiente_mgdl_min": -1.0, "glucosa_mgdl": 90},
    {"pendiente_mgdl_min": 3.0, "glucosa_mgdl": 85},
    {"glucosa_ts": AHORA - timedelta(minutes=15)},
    {"estado_pluma": "confirmada_a_mano"},
])
def test_freno_no_salta_en_el_limite(cambios):
    assert S.calcular(entrada(**cambios))["bloqueada"] is False


# ------------------------------------------------------------------ acumulación
def test_acumulacion_por_insulina_activa_quita_la_correccion_positiva():
    # 1,75 U activas × 40 = 70 mg/dL, no supera 150 − 70 = 80: sin alerta.
    assert S.calcular(entrada(insulina_activa_u=1.75))["traza"]["acumulacion"] == []
    r = S.calcular(entrada(insulina_activa_u=2.25))           # 90 > 80: alerta
    assert r["traza"]["acumulacion"] == ["insulina_activa_alta"]
    assert r["u_inmediata"] == 3.5                            # 6 + 0 − 2,25 = 3,75 → 3,5
    assert any("acumulación" in a for a in r["avisos"])


def test_acumulacion_por_varias_dosis_mantiene_la_correccion_negativa():
    r = S.calcular(entrada(glucosa_mgdl=90, dosis_fiasp_3h=2))
    assert r["traza"]["acumulacion"] == ["varias_dosis_3h"]
    assert r["u_inmediata"] == 5.5                            # 6 − 0,5


# ------------------------------------------------------------------ incertidumbre
def test_incertidumbre_con_rango_de_hidratos():
    r = S.calcular(entrada(hidratos_min_g=45, hidratos_max_g=80, confianza="alta"))
    assert r["incertidumbre"]["rango_u"] == [5.5, 9.0]
    assert r["incertidumbre"]["nivel"] == "alta"


def test_incertidumbre_baja_solo_con_todo_seguro():
    assert S.calcular(entrada(confianza="alta"))["incertidumbre"]["nivel"] == "baja"
    assert S.calcular(entrada(confianza="baja"))["incertidumbre"]["nivel"] == "alta"
    assert S.calcular(entrada(confianza="alta", dosis_con_dudas=True))["incertidumbre"]["nivel"] == "alta"
    assert S.calcular(entrada(confianza="alta", estado_pluma="confirmada_a_mano"))["incertidumbre"]["nivel"] == "media"


# ------------------------------------------------------------------ segunda inyección
def test_segunda_inyeccion_varsovia():
    r = S.calcular(entrada(grasa_g=20, proteina_g=25))      # (180 + 100) / 100 = 2,8 UGP
    assert r["ugp"] == 2.8
    assert r["u_segunda"] == 1.0                            # 2,8 × 10 / 10 × 0,5 = 1,4 → 1,0
    assert r["ts_segunda"] == AHORA + timedelta(minutes=90)
    assert r["u_inmediata"] == 7.0                          # la primera no cambia


def test_segunda_mas_tarde_con_mucha_grasa():
    r = S.calcular(entrada(grasa_g=40, proteina_g=30))      # 4,8 UGP
    assert r["ts_segunda"] == AHORA + timedelta(minutes=120) and r["u_segunda"] == 2.0


def test_sin_segunda_con_poca_grasa():
    r = S.calcular(entrada(grasa_g=5, proteina_g=10))       # 0,85 UGP
    assert r["u_segunda"] is None and r["ts_segunda"] is None


def test_recalculo_de_la_segunda():
    prevista = AHORA
    ok = S.segunda(entrada(tipo="segunda", insulina_activa_u=4.0), 1.5, prevista)
    assert ok["u_inmediata"] == 1.5 and not ok["bloqueada"]           # no resta la insulina activa
    baja = S.segunda(entrada(tipo="segunda", glucosa_mgdl=75), 1.5, prevista)
    assert baja["bloqueada"] and baja["u_inmediata"] is None
    pronto = S.segunda(entrada(tipo="segunda"), 1.5, prevista + timedelta(minutes=16))
    assert pronto["bloqueada"] and "segunda_pronto" in pronto["traza"]["freno"]
    tarde = S.segunda(entrada(tipo="segunda"), 1.5, prevista - timedelta(minutes=121))
    assert tarde["bloqueada"] and "segunda_tarde" in tarde["traza"]["freno"]


# ------------------------------------------------------------------ trazabilidad
def test_la_traza_permite_rehacer_el_calculo():
    r = S.calcular(entrada(hidratos_min_g=50, hidratos_max_g=70, grasa_g=20, proteina_g=25, insulina_activa_u=0.8))
    datos = dict(r["traza"]["entrada"])
    for k in ("ahora", "glucosa_ts", "ts_comida"):
        datos[k] = datetime.fromisoformat(datos[k])
    datos["avisos_externos"] = tuple(datos["avisos_externos"])
    otra = S.calcular(S.Entrada(**datos), S.Limites(**r["traza"]["limites"]))
    assert otra == r


def test_los_limites_estan_en_la_traza():
    r = S.calcular(entrada())
    assert r["traza"]["limites"] == dataclasses.asdict(S.LIMITES)
    assert r["calculo_version"] == S.VERSION_CALCULO


# ------------------------------------------------------------------ regla 1: nada de modelos de lenguaje
RAIZ = pathlib.Path(__file__).resolve().parent.parent
PROHIBIDO = ("servicios", "ollama", "httpx", "requests", "openai", "anthropic")


@pytest.mark.parametrize("modulo", ["comun/sugerencia.py", "comun/sugerir.py", "comun/parametros.py",
                                    "comun/decisiones.py", "api/sugerencias.py"])
def test_el_calculo_no_importa_modelos_ni_red(modulo):
    arbol = ast.parse((RAIZ / modulo).read_text())
    importados = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Import):
            importados |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            importados.add(n.module.split(".")[0])
    assert not importados & set(PROHIBIDO), importados


def test_el_calculo_no_lee_eventos_del_chat():
    for modulo in ("comun/sugerencia.py", "comun/sugerir.py"):
        assert "eventos_chat" not in (RAIZ / modulo).read_text().replace("los eventos del chat", "")
