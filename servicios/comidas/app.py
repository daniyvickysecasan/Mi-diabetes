"""Interfaz web local (abre http://localhost:7860, o desde el móvil en la misma wifi:
http://IP-DEL-ORDENADOR:7860). Ejecuta:  python app.py"""
import pandas as pd
import gradio as gr

import config as C
from nutricion import (NUTS, TablaAlimentos, analizar_plato, calcular_bolo, guardar_registro, hc_bolo)

TABLA = TablaAlimentos()
COLS = ["plato", "nombre", "nombre_en", "gramos"] + [f"{k}_100g" for k in NUTS] + ["fuente"]


def items_a_df(platos):
    filas = []
    for n, p in enumerate(platos, 1):
        for i in p["items"]:
            filas.append([n, i["nombre"], i["nombre_en"], round(i["gramos"])]
                         + [round(i["por100"][k], 1) for k in NUTS] + [i["fuente"]])
    return pd.DataFrame(filas, columns=COLS)


def resumen_platos(platos):
    md = []
    for n, p in enumerate(platos, 1):
        c = p["consenso"]
        md.append(f"### Plato {n} ({p['origen']}): **{p['hc_bolo']:.0f} g de hidratos** "
                  f"(rango {p['rango_hc'][0]:.0f}–{p['rango_hc'][1]:.0f})")
        md.append(f"Fibra {c['fibra']:.0f} g · azúcares {c['azucares']:.0f} g · grasa {c['grasa']:.0f} g · "
                  f"proteína {c['proteina']:.0f} g · {c['kcal']:.0f} kcal")
        md.append("| Estimación | Hidratos (g) |\n|---|---|" + "".join(
            f"\n| {e} | {hc_bolo(t):.0f} |" for e, t in p["estimaciones"]))
        md += [f"- ⚠️ {a}" for a in p["alertas"]] + [f"- ✗ {e}" for e in p["errores"]]
    return "\n\n".join(md)


def analizar(archivos, texto, progress=gr.Progress()):
    platos = []
    archivos = archivos or []
    if not archivos and not (texto or "").strip():
        raise gr.Error("Sube al menos una foto o escribe una descripción.")
    for ruta in archivos:
        progress(0, desc=f"Analizando foto {len(platos) + 1}")
        with open(ruta if isinstance(ruta, str) else ruta.name, "rb") as f:
            platos.append(analizar_plato(TABLA, imagen=f.read(), nota=texto or "", progreso=lambda m: None))
    if not archivos:
        progress(0, desc="Analizando descripción")
        platos.append(analizar_plato(TABLA, texto=texto, progreso=lambda m: None))
    return platos, resumen_platos(platos), items_a_df(platos)


def df_a_items(df):
    items = []
    for _, r in df.iterrows():
        if not str(r["nombre"]).strip():
            continue
        por100 = {k: float(r[f"{k}_100g"] or 0) for k in NUTS}
        fuente = str(r["fuente"] or "").strip()
        if not fuente:  # fuente vacía = volver a buscar en la base con el nombre editado
            fila, punt = TABLA.buscar(str(r["nombre"]), str(r["nombre_en"]))
            if fila:
                por100 = {k: fila[k] for k in NUTS}
                fuente = f"{fila['fuente']}: {fila['nombre']} ({punt:.0f})"
            else:
                fuente = "manual"
        items.append({"plato": r["plato"], "nombre": r["nombre"], "nombre_en": r["nombre_en"],
                      "gramos": float(r["gramos"] or 0), "por100": por100, "fuente": fuente})
    return items


def recalcular(platos, df, ratio, sens, objetivo, glucosa, activa):
    if not platos:
        raise gr.Error("Primero analiza la comida.")
    items = df_a_items(df)
    tot = {k: sum(i["por100"][k] * i["gramos"] / 100 for i in items) for k in NUTS}
    b = calcular_bolo(tot, ratio, sens, objetivo, glucosa or None, activa or 0)
    consenso = sum(p["hc_bolo"] for p in platos)
    md = [f"## Comida: **{b['hc_bolo']:.0f} g de hidratos** para el bolo",
          f"Hidratos totales {tot['hidratos']:.0f} g · fibra {tot['fibra']:.0f} g · azúcares {tot['azucares']:.0f} g · "
          f"grasa {tot['grasa']:.0f} g · proteína {tot['proteina']:.0f} g · {tot['kcal']:.0f} kcal",
          f"Consenso de los modelos: {consenso:.0f} g"]
    if abs(b["hc_bolo"] - consenso) > max(C.ALERTA_ABS_G, C.ALERTA_REL * consenso):
        md.append("⚠️ La tabla revisada se aleja del consenso de los modelos: comprueba alimentos y gramos.")
    md.append(f"### Bolo de referencia: {b['bolo_inmediato']} U inmediatas "
              f"({b['u_hidratos']:.1f} por hidratos, {b['u_correccion']:+.1f} de corrección)")
    if b["bolo_extendido"]:
        md.append(f"Grasa y proteína: {b['ugp']:.1f} UGP → {b['bolo_extendido']} U extendidas en {b['horas_extendido']} h")
    md.append("_Solo para pruebas: compáralo con tu cálculo habitual._")
    return "\n\n".join(md), {"items": items, "bolo": b}


def guardar(platos, ultimo):
    if not platos or not ultimo:
        raise gr.Error("Analiza y recalcula antes de guardar.")
    guardar_registro(platos, ultimo["items"], ultimo["bolo"])
    return "Guardado en " + C.RUTA_REGISTRO


with gr.Blocks(title="Hidratos por foto") as demo:
    platos_st, ultimo_st = gr.State([]), gr.State(None)
    gr.Markdown("# Hidratos por foto o descripción\nUna foto por plato. Sin foto, describe la comida.")
    fotos = gr.File(label="Fotos (una por plato)", file_count="multiple", file_types=["image"])
    texto = gr.Textbox(label="Nota para las fotos, o descripción si no hay foto", lines=2)
    btn = gr.Button("Analizar", variant="primary")
    resumen = gr.Markdown()
    gr.Markdown("**Revisa la tabla.** Corrige gramos o valores por 100 g. "
                "Si cambias el alimento, borra su celda *fuente* para que se vuelva a buscar en la base.")
    tabla = gr.Dataframe(headers=COLS, interactive=True, wrap=True)
    with gr.Row():
        ratio = gr.Number(C.RATIO_HC, label="g de hidratos por U")
        sens = gr.Number(C.SENSIBILIDAD, label="Sensibilidad (mg/dL por U)")
        obj = gr.Number(C.OBJETIVO, label="Objetivo (mg/dL)")
        glu = gr.Number(None, label="Glucosa actual (opcional)")
        act = gr.Number(0, label="Insulina activa (U)")
    btn2 = gr.Button("Calcular comida y bolo")
    final = gr.Markdown()
    btn3 = gr.Button("Guardar en registro")
    estado = gr.Markdown()

    btn.click(analizar, [fotos, texto], [platos_st, resumen, tabla])
    btn2.click(recalcular, [platos_st, tabla, ratio, sens, obj, glu, act], [final, ultimo_st])
    btn3.click(guardar, [platos_st, ultimo_st], estado)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
