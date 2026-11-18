"""Recolector de Nightscout contra el simulador."""
from datetime import timedelta

from comun.origen import es_origen_propio
from herramientas import sinteticos
from servicios.recoleccion.recolector import ciclo_incremental, detectar_huecos, importar, verificar


def _historico(con, ns, ahora, dias=16):
    return importar(con, ns, ahora - timedelta(days=dias), ahora + timedelta(minutes=1), "historico")


def _sgv_unicos(entradas):
    return {e["date"]: e["sgv"] for e in entradas if e.get("type") == "sgv"}


def test_historico_coincide_con_nightscout(con, ns, datos_ns, ahora):
    _historico(con, ns, ahora)
    esperadas = _sgv_unicos(datos_ns[0])
    n = con.execute("SELECT count(*) AS n FROM glucosa WHERE tipo='sgv'").fetchone()["n"]
    assert n == len(esperadas)
    r = verificar(con, ns, ahora - timedelta(days=16), ahora + timedelta(minutes=1))
    assert r["ok"], r
    assert r["valores_distintos"] == 0


def test_guarda_la_glucemia_capilar_aparte(con, ns, ahora):
    _historico(con, ns, ahora)
    assert con.execute("SELECT count(*) AS n FROM glucosa WHERE tipo='mbg'").fetchone()["n"] == 1


def test_reimportar_no_duplica(con, ns, ahora):
    _historico(con, ns, ahora)
    antes = con.execute("SELECT count(*) AS n FROM glucosa").fetchone()["n"]
    r = _historico(con, ns, ahora)
    assert r["glucosa"]["insertadas"] == 0
    assert r["glucosa"]["conflictos"] == 0
    assert con.execute("SELECT count(*) AS n FROM glucosa").fetchone()["n"] == antes
    assert con.execute("SELECT count(*) AS n FROM tratamientos_nightscout").fetchone()["n"] == 2


def test_ignora_tratamientos_de_origen_propio(con, ns, ahora, datos_ns):
    r = _historico(con, ns, ahora)
    assert r["tratamientos"]["propios_ignorados"] == 1
    ids = {f["id_externo"] for f in con.execute("SELECT id_externo FROM tratamientos_nightscout")}
    assert ids == {"t1", "t2"}
    # Ninguno de los guardados es nuestro, y ningún tratamiento se convierte en dosis.
    assert not any(es_origen_propio(t) for t in datos_ns[1] if t["_id"] in ids)
    assert con.execute("SELECT count(*) AS n FROM dosis").fetchone()["n"] == 0


def test_origen_propio_variantes():
    assert es_origen_propio({"enteredBy": "mi-diabetes"})
    assert es_origen_propio({"enteredBy": "Mi-Diabetes/libreview"})
    assert es_origen_propio({"enteredBy": "otro", "origen_mi_diabetes": True})
    assert not es_origen_propio({"enteredBy": "careportal"})
    assert not es_origen_propio({})


def test_ciclo_incremental_recoge_lectura_nueva(con, ns, ns_app, ajustes, ahora):
    _historico(con, ns, ahora)
    nueva = sinteticos.nueva_lectura(ahora + timedelta(minutes=5))
    ns_app.state.datos["entradas"].append(nueva)
    r = ciclo_incremental(con, ns, ajustes, ahora=ahora + timedelta(minutes=6))
    assert r["glucosa"]["insertadas"] == 1
    fila = con.execute("SELECT valor_mgdl FROM glucosa ORDER BY ts DESC LIMIT 1").fetchone()
    assert fila["valor_mgdl"] == nueva["sgv"]


def test_ciclo_incremental_lee_con_solape(con, ns, ns_app, ajustes, ahora):
    """LibreLinkUp puede subir lecturas antiguas tarde: el solape de 3 h las recoge."""
    _historico(con, ns, ahora)
    tardia = sinteticos.nueva_lectura(ahora - timedelta(minutes=62, seconds=30))
    ns_app.state.datos["entradas"].append(tardia)
    r = ciclo_incremental(con, ns, ajustes, ahora=ahora + timedelta(minutes=1))
    assert r["glucosa"]["insertadas"] == 1


def test_detecta_el_hueco_de_dos_horas(con, ns, ahora):
    _historico(con, ns, ahora)
    huecos = con.execute("SELECT * FROM huecos WHERE estado='cerrado'").fetchall()
    assert len(huecos) == 1
    assert 120 <= huecos[0]["minutos"] <= 130


def test_hueco_abierto_y_luego_rellenado(con, ns, ns_app, ajustes, ahora):
    _historico(con, ns, ahora)
    mas_tarde = ahora + timedelta(minutes=45)
    detectar_huecos(con, ahora - timedelta(hours=6), mas_tarde, ajustes.hueco_min, ahora=mas_tarde)
    abierto = con.execute("SELECT * FROM huecos WHERE estado='abierto'").fetchone()
    assert abierto is not None
    # Llegan lecturas que cierran el hueco
    for m in range(5, 50, 5):
        ns_app.state.datos["entradas"].append(sinteticos.nueva_lectura(ahora + timedelta(minutes=m)))
    ciclo_incremental(con, ns, ajustes, ahora=mas_tarde)
    estado = con.execute("SELECT estado FROM huecos WHERE id=%s", (abierto["id"],)).fetchone()["estado"]
    assert estado == "rellenado"
    assert con.execute("SELECT count(*) AS n FROM huecos WHERE estado='abierto'").fetchone()["n"] == 0


def test_ventana_llena_se_parte(con, ns_app, ahora, monkeypatch):
    from servicios.recoleccion import nightscout
    monkeypatch.setattr(nightscout, "MAX_POR_PETICION", 100)
    from fastapi.testclient import TestClient
    cliente = nightscout.ClienteNightscout("http://ns.test", "token-lectura-prueba",
                                           cliente=TestClient(ns_app, base_url="http://ns.test"))
    entradas = cliente.entradas(ahora - timedelta(days=2), ahora)
    esperadas = [e for e in ns_app.state.datos["entradas"]
                 if (ahora - timedelta(days=2)).timestamp() * 1000 <= e["date"] < ahora.timestamp() * 1000]
    assert len(entradas) == len(esperadas)


def test_token_incorrecto_no_filtra_el_token(con, ns_app, ahora):
    from fastapi.testclient import TestClient
    from servicios.recoleccion.nightscout import ClienteNightscout, ErrorNightscout
    secreto = "token-equivocado-secreto"
    c = ClienteNightscout("http://ns.test", secreto, cliente=TestClient(ns_app, base_url="http://ns.test"))
    try:
        c.entradas(ahora - timedelta(hours=1), ahora)
        raise AssertionError("debía fallar")
    except ErrorNightscout as e:
        assert secreto not in str(e)
        assert "401" in str(e)


def test_lecturas_fuera_de_rango_se_ignoran(con):
    from servicios.recoleccion.recolector import guardar_glucosa
    r = guardar_glucosa(con, [{"type": "sgv", "sgv": 5, "date": 1700000000000},
                              {"type": "sgv", "sgv": "abc", "date": 1700000060000},
                              {"type": "cal", "date": 1700000120000},
                              {"type": "sgv", "sgv": 110, "date": 1700000180000}])
    assert r["insertadas"] == 1 and r["ignoradas"] == 3
