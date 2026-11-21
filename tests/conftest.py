"""Fixtures: TimescaleDB real y Nightscout simulado con datos sintéticos.

Necesita TEST_DATABASE_URL apuntando a un servidor con TimescaleDB, p. ej.:
  docker run -d --name tsdb -e POSTGRES_PASSWORD=prueba -p 5433:5432 timescale/timescaledb:2.17.2-pg16
  export TEST_DATABASE_URL=postgresql://postgres:prueba@localhost:5433/postgres
Cada sesión de tests crea y borra su propia base `midiabetes_test`.
"""
import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from comun.ajustes import Ajustes
from comun.migrar import migrar
from herramientas import sinteticos
from herramientas.nightscout_simulado import crear_app as crear_ns
from servicios.recoleccion.nightscout import ClienteNightscout

URL_ADMIN = os.environ.get("TEST_DATABASE_URL")
TOKEN = "token-lectura-prueba"
TABLAS = ["platos", "suscripciones_push", "anomalias", "episodios_etiquetados", "decisiones", "sugerencias", "dosis", "eventos_chat", "comidas", "hidratos_correccion",
          "config_tresiba", "versiones_parametros", "glucosa", "tratamientos_nightscout",
          "ejecuciones_recoleccion", "huecos", "llegadas_pluma", "envios_nightscout", "horas_pluma_libreview", "solicitudes_lectura_pluma", "cambios_pluma_libreview", "sesiones", "retos_webauthn", "credenciales_passkey"]


@pytest.fixture(scope="session")
def url_bd():
    if not URL_ADMIN:
        pytest.skip("Define TEST_DATABASE_URL (servidor con TimescaleDB) para los tests de base de datos")
    with psycopg.connect(URL_ADMIN, autocommit=True) as con:
        con.execute("DROP DATABASE IF EXISTS midiabetes_test WITH (FORCE)")
        con.execute("CREATE DATABASE midiabetes_test")
    url = psycopg.conninfo.make_conninfo(URL_ADMIN, dbname="midiabetes_test")
    migrar(url)
    yield url
    with psycopg.connect(URL_ADMIN, autocommit=True) as con:
        con.execute("DROP DATABASE IF EXISTS midiabetes_test WITH (FORCE)")


@pytest.fixture
def con(url_bd):
    from psycopg.rows import dict_row
    with psycopg.connect(url_bd, autocommit=True, row_factory=dict_row) as c:
        c.execute("TRUNCATE " + ", ".join(TABLAS) + " RESTART IDENTITY CASCADE")
        # Fila única: se reinicia en vez de borrarse.
        c.execute("""UPDATE estado_libreview SET estado='sin_iniciar', codigo=NULL, destino_sms=NULL,
                     cambiado_en=now(), ultimo_ok=NULL, ultima_sincronizacion=NULL, error=NULL""")
        c.execute("UPDATE estado_anomalias SET ultimo_ciclo=NULL, ultimo_error=NULL, ntfy_ok=NULL, ntfy_error=NULL")
        yield c


@pytest.fixture
def ahora():
    return sinteticos.ahora_redondeado()


@pytest.fixture
def datos_ns(ahora):
    return sinteticos.generar(ahora - timedelta(days=16), ahora)


@pytest.fixture
def ns_app(datos_ns):
    entradas, tratamientos = datos_ns
    return crear_ns(entradas, tratamientos, token=TOKEN)


@pytest.fixture
def ns(ns_app):
    http = TestClient(ns_app, base_url="http://nightscout.test")
    return ClienteNightscout("http://nightscout.test", TOKEN, cliente=http)


@pytest.fixture
def ajustes(url_bd):
    return Ajustes(database_url=url_bd, nightscout_url="http://nightscout.test", nightscout_token=TOKEN,
                   rp_id="localhost", origenes=("http://localhost:8000",), codigo_alta="codigo-de-prueba",
                   cookie_segura=False)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)
