"""Reglas de anomalías (fase 4), puras: sin base ni red."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from servicios.anomalias import reglas as R

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)      # 11:00 en Madrid
U = R.Umbrales()


def serie(valores, t0=T0, paso=5):
    return [(t0 + timedelta(minutes=paso * i), v) for i, v in enumerate(valores)]


def m(n):
    return T0 + timedelta(minutes=n)


# ------------------------------------------------------------------ huecos
def test_hueco_cerrado_y_abierto():
    lect = serie([100] * 4) + [(m(60), 110), (m(65), 112)]
    eps = R.huecos(lect, m(120), U)
    assert [(e["inicio"], e["fin"]) for e in eps] == [(m(15), m(60)), (m(65), None)]
    assert eps[0]["datos"] == {"minutos": 45, "abierto": False}
    assert eps[1]["datos"]["abierto"] and eps[1]["datos"]["minutos"] == 55


def test_hueco_corto_no_cuenta_y_solo_abierto():
    lect = serie([100] * 4) + [(m(40), 110)]
    assert R.huecos(lect, m(45), U) == []                  # 25 min < 30
    assert R.huecos([], m(100), U, ultima_previa=m(40), solo_abierto=True)[0]["inicio"] == m(40)


# ------------------------------------------------------------------ presión
CAIDA = [140, 140, 138, 115, 90, 78, 95, 120, 132, 136, 138]   # baja 62 en 15 min y rebota en 20


def test_presion_detectada():
    eps = R.presion(serie(CAIDA), [], U)
    assert len(eps) == 1
    d = eps[0]["datos"]
    assert d["minimo_mgdl"] == 78 and d["desde_mgdl"] == 140 and d["vuelta_mgdl"] >= 78 + 0.7 * 62
    assert eps[0]["inicio"] == m(5) and eps[0]["fin"] == m(40)


def test_presion_con_hidratos_entre_medias_no():
    assert R.presion(serie(CAIDA), [m(28)], U) == []


def test_bajada_lenta_o_sin_rebote_no_es_presion():
    lenta = [140 - 4 * i for i in range(16)] + [80 + 10 * i for i in range(6)]   # 0,8 mg/dL/min
    assert R.presion(serie(lenta), [], U) == []
    sin_rebote = CAIDA[:6] + [80, 82, 84, 85, 86, 88, 90, 91, 92, 92, 93, 94, 95]
    assert R.presion(serie(sin_rebote), [], U) == []


# ------------------------------------------------------------------ subida sin comida
SUBIDA = [100] * 12 + [100 + 6 * i for i in range(1, 19)] + [208, 205, 200]   # +108 en 90 min


def test_subida_sin_comida():
    eps = R.subida_sin_comida(serie(SUBIDA), [], U)
    assert len(eps) == 1
    e = eps[0]
    assert e["datos"]["desde_mgdl"] == 100 and e["datos"]["hasta_mgdl"] == 208
    assert e["inicio"] == m(55) and e["fin"] == m(145)


def test_subida_con_comida_antes_no():
    assert R.subida_sin_comida(serie(SUBIDA), [m(-60)], U) == []      # comió 2 h antes del inicio
    assert R.subida_sin_comida(serie(SUBIDA), [m(-130)], U) != []     # más de 3 h antes: no la explica


def test_subida_pequena_o_que_no_llega_no():
    assert R.subida_sin_comida(serie([100] * 12 + [100 + 3 * i for i in range(1, 19)]), [], U) == []   # +54
    assert R.subida_sin_comida(serie([80] * 12 + [80 + 4 * i for i in range(1, 19)]), [], U) == []     # hasta 152


def test_rebote_de_presion_no_es_subida():
    lect = serie([200, 200, 198, 160, 130, 120, 150, 185, 195, 198])
    pres = R.presion(lect, [], U)
    assert pres
    assert R.subida_sin_comida(lect, [], U, excluir=[(p["inicio"], p["fin"]) for p in pres]) == []


# ------------------------------------------------------------------ Fiasp sin comida
def _d(n, **k):
    return {"id": n, "ts": m(n), "unidades": 4, "comida_id": None, "sugerencia_tipo": None} | k


def test_dosis_sin_comida():
    ahora = m(200)
    dosis = [_d(0), _d(10, comida_id=7), _d(20, sugerencia_tipo="correccion"), _d(30, sugerencia_tipo="segunda"),
             _d(170)]                                     # demasiado reciente: aún puede apuntar la comida
    eps = R.dosis_sin_comida(dosis, [], ahora, U)
    assert [e["datos"]["dosis_id"] for e in eps] == [0]
    assert R.dosis_sin_comida([_d(0)], [m(-150)], ahora, U) == []     # segunda inyección de una comida
    assert R.dosis_sin_comida([_d(0)], [m(45)], ahora, U) == []       # comió después
    assert len(R.dosis_sin_comida([_d(0)], [m(-200), m(70)], ahora, U)) == 1


# ------------------------------------------------------------------ acumulación
def test_hidratos_sin_absorber_lineal():
    c = [{"ts": m(0), "gramos": 60, "tipo": "comida", "ratio_g_u": 8},
         {"ts": m(30), "gramos": 15, "tipo": "correccion", "ratio_g_u": 8},
         {"ts": m(-200), "gramos": 50, "tipo": "comida", "ratio_g_u": 8}]
    r = R.hidratos_sin_absorber(c, m(60), U)
    assert [x["resto_g"] for x in r] == [40.0, 7.5]
    assert r[0]["cubre_u"] == 5.0


def test_acumulacion_avisa_sin_hidratos():
    a = R.acumulacion(m(0), 150, m(-3), 3.0, 45, [], 2, U)
    assert a["tipo"] == "acumulacion" and a["datos"]["exceso_u"] == 3.0 and a["datos"]["bajada_mgdl"] == 135
    assert "2 dosis de Fiasp en 3 h" in R.redactar(a)["texto"] and R.redactar(a)["urgente"]


def test_acumulacion_cubierta_por_la_comida_no_avisa():
    comida = [{"ts": m(-30), "gramos": 60, "tipo": "comida", "ratio_g_u": 8}]   # 50 g pendientes = 6,25 U
    assert R.acumulacion(m(0), 150, m(-3), 6.0, 45, comida, 1, U) is None


@pytest.mark.parametrize("args", [
    (150, m(-20), 3.0, 45),        # lectura de hace 20 min
    (None, None, 3.0, 45),         # sin glucosa
    (150, m(-3), 3.0, None),       # sin parámetros
    (150, m(-3), 1.5, 45),         # 67 mg/dL < 150 − 70
    (72, m(-3), 0.3, 45),          # exceso por debajo de un paso de pluma
])
def test_acumulacion_no_avisa(args):
    assert R.acumulacion(m(0), *args, [], 0, U) is None


# ------------------------------------------------------------------ olvido de Tresiba
ALTA = [200 + i for i in range(49)]        # 4 h por encima de 180 y subiendo


def test_olvido_tresiba():
    lect = serie(ALTA)
    e = R.olvido_tresiba(lect, [], m(240), True, U)
    assert e and e["datos"]["pct_por_encima"] == 100 and e["datos"]["pendiente_mgdl_h"] == 12.0
    assert "Solo es una pregunta" in R.redactar(e)["texto"]


@pytest.mark.parametrize("lect, eventos, hay", [
    (serie(ALTA), [], False),                            # sin Tresiba configurada
    (serie(ALTA), [m(-60)], True),                       # Fiasp o comida 1 h antes de la ventana
    (serie([170] * 49), [], True),                       # por debajo de 180
    (serie([260 - i for i in range(49)]), [], True),     # bajando
    (serie(ALTA[::3], paso=15), [], True),               # pocas lecturas
])
def test_olvido_tresiba_no(lect, eventos, hay):
    assert R.olvido_tresiba(lect, eventos, m(240), hay, U) is None


def test_olvido_tresiba_historico_una_vez_al_dia():
    lect = serie([200 + (i % 40) // 4 for i in range(12 * 10)])        # 10 h altas
    eps = R.olvidos_tresiba_historico(lect, [], {m(0).astimezone(R.ZONA).date()}, U)
    assert len(eps) == 1 and eps[0]["fin"] == m(165)        # con el 70 % de la ventana ya cubierto
    assert R.olvidos_tresiba_historico(lect, [], {date(2026, 1, 1)}, U) == []


# ------------------------------------------------------------------ silencio y umbrales
@pytest.mark.parametrize("local, esperado", [
    ("2026-11-20T23:30", "2026-11-21T08:00"), ("2026-11-21T07:00", "2026-11-21T08:00"),
    ("2026-11-21T12:00", "2026-11-21T12:00"), ("2026-11-21T08:00", "2026-11-21T08:00"),
])
def test_silencio_nocturno(local, esperado):
    t = datetime.fromisoformat(local).replace(tzinfo=R.ZONA).astimezone(timezone.utc)
    assert R.avisar_desde(t, False, U).astimezone(R.ZONA).replace(tzinfo=None) == datetime.fromisoformat(esperado)
    assert R.avisar_desde(t, True, U) == t                # lo urgente no espera


def test_umbrales_del_entorno():
    u = R.Umbrales.desde_entorno({"ANOMALIA_HUECO_MIN": "40", "ANOMALIA_PRESION_PENDIENTE": "2,5",
                                  "ANOMALIA_DESACTIVADAS": "presion, hueco", "ANOMALIA_SILENCIO_DESDE": "22:30"})
    assert (u.hueco_min, u.presion_pendiente, u.desactivadas, u.silencio_desde) == (40, 2.5, ("presion", "hueco"), "22:30")
    with pytest.raises(ValueError):
        R.Umbrales.desde_entorno({"ANOMALIA_DESACTIVADAS": "alcohol"})


def test_ningun_texto_da_unidades_que_ponerse():
    """Los avisos informan; nunca dicen cuánta insulina ponerse (regla 1)."""
    for tipo, (pregunta, opciones) in R.PREGUNTAS.items():
        for o in opciones:
            assert "ponte" not in o["texto"].lower()
    assert "alcohol" not in R.TIPOS                       # Daniel rechazó el aviso por alcohol


def test_la_sugerencia_no_lee_las_anomalias():
    """Regla 1 y 3: el cálculo de dosis no depende de las anomalías."""
    raiz = Path(__file__).resolve().parent.parent
    for f in ("comun/sugerencia.py", "comun/sugerir.py", "comun/insulina.py", "comun/conciliacion.py"):
        texto = (raiz / f).read_text(encoding="utf-8")
        assert "anomalia" not in texto.lower() and "servicios" not in texto
