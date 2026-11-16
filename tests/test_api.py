from fastapi.testclient import TestClient

from gasolina_gt.app import app

cliente = TestClient(app)


def test_health_lee_las_cinco_tablas():
    r = cliente.get("/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok" and j["fuente_oro"] == "instantanea"
    assert all(t["filas"] > 0 for t in j["tablas"].values())


def test_precio_actual_por_combustible():
    j = cliente.get("/precios/actual").json()
    assert {p["combustible"] for p in j["precios"]} == {"super", "regular", "diesel"}


def test_prediccion_con_probabilidad_numerica():
    j = cliente.get("/prediccion", params={"combustible": "super"}).json()
    assert j["prediccion"] in {"sube", "baja", "se_mantiene"}
    assert isinstance(j["probabilidad"], float)


def test_historico_filtra_y_pagina():
    j = cliente.get("/precios", params={"combustible": "diesel", "desde": "2026-10-01",
                                         "limit": 3}).json()
    assert len(j["datos"]) <= 3 and all(d["combustible"] == "diesel" for d in j["datos"])
    invertido = {"desde": "2026-12-01", "hasta": "2026-01-01"}
    assert cliente.get("/precios", params=invertido).status_code == 400


def test_metricas_y_panel():
    j = cliente.get("/modelo/metricas").json()
    assert j["accuracy_modelo"] >= j["accuracy_baseline"]
    assert "Gasolina GT" in cliente.get("/").text
