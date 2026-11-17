import json
import os
import time
from datetime import datetime

os.environ["DB_PATH"] = ":memory:"  # base isolée, même si DB_PATH est défini dans le shell
import app


def telemetry(watts, energy_wh):
    return json.dumps({"ts": time.time(), "on": True, "voltage_v": 120, "current_a": watts / 120,
                       "power_w": watts, "energy_wh": energy_wh}).encode()


def test_metrics_and_load_shedding():
    for dev, kind in (("heater-1", "heater"), ("lamp-1", "lamp")):
        app.handle(f"home/{dev}/meta", json.dumps({"type": kind, "version": "t"}).encode())
        app.handle(f"home/{dev}/status", b"online")
    app.peak_override = True
    app.handle("home/heater-1/telemetry", telemetry(1500, 1.0))
    app.handle("home/lamp-1/telemetry", telemetry(60, 1.0))
    app.peak_override = False
    app.handle("home/lamp-1/telemetry", telemetry(60, 1.0))

    s = app.state()
    assert s["current_w"] == 1560
    assert s["today_kwh"] == 0.003 and s["today_peak_kwh"] == 0.002
    assert s["today_cost"] == round(0.001 * app.PRICE_KWH + 0.002 * app.PEAK_PRICE_KWH, 2)
    assert {d["id"]: d["today_wh"] for d in s["devices"]} == {"heater-1": 1.0, "lamp-1": 2.0}

    assert app.peak_transition(True) == ["heater-1"]  # seul le chauffage est délesté
    assert app.peak_transition(False) == ["heater-1"]  # puis rallumé

    app.handle("home/lamp-1/status", b"offline")  # LWT
    assert app.state()["current_w"] == 1500
    app.handle("home/ghost/status", b"offline")  # un appareil inconnu hors ligne ne crée pas de ligne
    assert "ghost" not in [d["id"] for d in app.state()["devices"]]


def test_peak_windows():
    app.peak_override = None
    at = lambda *a: datetime(*a, tzinfo=app.TZ).timestamp()
    assert app.is_peak(at(2026, 1, 5, 7, 30))  # lundi matin
    assert app.is_peak(at(2026, 1, 5, 17, 0))  # lundi soir
    assert not app.is_peak(at(2026, 1, 5, 12, 0))  # lundi midi
    assert not app.is_peak(at(2026, 1, 10, 7, 30))  # samedi
