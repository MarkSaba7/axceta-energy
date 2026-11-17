"""Cloud : ingère la télémétrie MQTT, la stocke, calcule les métriques et déleste pendant les pointes."""
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from datetime import time as dtime
from typing import Annotated
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
from fastapi import FastAPI, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Montreal"))
# Plages de pointe hivernales d'Hydro-Québec (jours de semaine), configurables.
PEAK_WINDOWS = [
    tuple(dtime.fromisoformat(h) for h in w.split("-"))
    for w in os.getenv("PEAK_WINDOWS", "06:00-09:00,16:00-20:00").split(",")
]
SHEDDABLE = os.getenv("SHEDDABLE_TYPES", "heater,ev_charger").split(",")
# Tarifs approximatifs, à ajuster à la grille tarifaire en vigueur.
PRICE_KWH = float(os.getenv("PRICE_KWH", "0.07"))
PEAK_PRICE_KWH = float(os.getenv("PEAK_PRICE_KWH", "0.50"))
API_TOKEN = os.getenv("API_TOKEN", "")
DeviceId = Annotated[str, Path(pattern=r"^[\w.-]+$")]  # pas de / + # : l'id finit dans un topic MQTT

# Simplification : SQLite + un seul verrou global ; passer à TimescaleDB/Postgres si le nombre d'appareils grossit.
db = sqlite3.connect(os.getenv("DB_PATH", ":memory:"), check_same_thread=False)
lock = threading.Lock()
db.executescript("""
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY, type TEXT, version TEXT, online INTEGER DEFAULT 0, is_on INTEGER DEFAULT 1,
  shed INTEGER DEFAULT 0, power_w REAL DEFAULT 0, current_a REAL DEFAULT 0, last_seen REAL);
CREATE TABLE IF NOT EXISTS readings (device_id TEXT, ts REAL, power_w REAL, energy_wh REAL, peak INTEGER);
CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts);
""")
peak_override = None  # None = horaire normal, True/False = pointe forcée (démo)


def is_peak(ts):
    if peak_override is not None:
        return peak_override
    dt = datetime.fromtimestamp(ts, TZ)
    return dt.weekday() < 5 and any(start <= dt.time() < end for start, end in PEAK_WINDOWS)


def handle(topic, payload):
    """Traite un message MQTT entrant. Séparé du client MQTT pour être testable sans broker."""
    _, dev, kind = topic.split("/")
    with lock, db:
        if kind == "meta":
            meta = json.loads(payload)
            db.execute(
                "INSERT INTO devices(id, type, version) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET type=excluded.type, version=excluded.version",
                (dev, str(meta["type"]), str(meta.get("version", ""))),
            )
        elif kind == "status" and payload == b"online":
            db.execute(
                "INSERT INTO devices(id, online, last_seen) VALUES (?, 1, ?) "
                "ON CONFLICT(id) DO UPDATE SET online=1, last_seen=excluded.last_seen",
                (dev, time.time()),
            )
        elif kind == "status":  # "offline" (LWT) : UPDATE seulement, un appareil oublié ne doit pas revenir
            db.execute("UPDATE devices SET online=0, power_w=0, current_a=0 WHERE id=?", (dev,))
        elif kind == "telemetry":
            r = json.loads(payload)
            ts, power, energy = float(r["ts"]), float(r["power_w"]), float(r["energy_wh"])
            db.execute("INSERT INTO readings VALUES (?, ?, ?, ?, ?)", (dev, ts, power, energy, is_peak(ts)))
            db.execute(
                "UPDATE devices SET is_on=?, power_w=?, current_a=?, last_seen=? WHERE id=?",
                (bool(r["on"]), power, float(r["current_a"]), time.time(), dev),
            )


def peak_transition(active):
    """Début de pointe : marque les gros appareils à délester. Fin : ceux à rallumer. Retourne leurs ids."""
    with lock, db:
        if active:
            marks = ",".join("?" * len(SHEDDABLE))
            sql = f"SELECT id FROM devices WHERE online AND is_on AND type IN ({marks}) ORDER BY id"
            ids = [row[0] for row in db.execute(sql, SHEDDABLE)]
            db.executemany("UPDATE devices SET shed=1 WHERE id=?", [(i,) for i in ids])
        else:
            ids = [row[0] for row in db.execute("SELECT id FROM devices WHERE shed ORDER BY id")]
            db.execute("UPDATE devices SET shed=0")
    return ids


def state():
    now = time.time()
    day_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with lock:
        cur = db.execute("SELECT * FROM devices ORDER BY id")
        cols = [c[0] for c in cur.description]
        devices = [dict(zip(cols, row)) for row in cur]
        today = dict(db.execute(
            "SELECT device_id, SUM(energy_wh) FROM readings WHERE ts >= ? GROUP BY device_id", (day_start,)))
        total_wh, peak_wh = db.execute(
            "SELECT COALESCE(SUM(energy_wh), 0), COALESCE(SUM(CASE WHEN peak THEN energy_wh END), 0) "
            "FROM readings WHERE ts >= ?", (day_start,)).fetchone()
        # Puissance moyenne par minute sur la dernière heure (minute en cours exclue car incomplète).
        history = db.execute(
            "SELECT CAST(ts / 60 AS INT) * 60 AS m, SUM(energy_wh) * 60, MAX(peak) FROM readings "
            "WHERE ts >= ? AND ts < ? GROUP BY m ORDER BY m", (now - 3600, now // 60 * 60)).fetchall()
    for d in devices:
        d["today_wh"] = round(today.get(d["id"], 0), 2)
    return {
        "peak_active": is_peak(now),
        "peak_override": peak_override,
        "current_w": round(sum(d["power_w"] for d in devices if d["online"]), 1),
        "today_kwh": round(total_wh / 1000, 4),
        "today_peak_kwh": round(peak_wh / 1000, 4),
        "today_cost": round((total_wh - peak_wh) / 1000 * PRICE_KWH + peak_wh / 1000 * PEAK_PRICE_KWH, 2),
        "devices": devices,
        "history": [{"t": t, "w": round(w, 1), "peak": bool(p)} for t, w, p in history],
    }


# --- MQTT ---------------------------------------------------------------------------------------
mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cloud-api")


def on_connect(client, userdata, flags, reason_code, properties):
    client.subscribe([(f"home/+/{kind}", 1) for kind in ("meta", "status", "telemetry")])


def on_message(client, userdata, msg):
    try:
        handle(msg.topic, msg.payload)
    except (ValueError, KeyError, TypeError) as e:  # message d'appareil mal formé : on l'ignore
        print(f"message ignoré sur {msg.topic} : {e}", flush=True)


def send_power(dev, on):
    mqttc.publish(f"home/{dev}/cmd", json.dumps({"on": on}), qos=1)


def peak_watcher():
    was = False
    while True:
        now = is_peak(time.time())
        if now != was:
            for dev in peak_transition(now):
                send_power(dev, not now)
            was = now
        time.sleep(1)


@asynccontextmanager
async def lifespan(app):
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    if os.getenv("MQTT_USERNAME"):
        mqttc.username_pw_set(os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD"))
    if os.getenv("MQTT_TLS") == "1":
        mqttc.tls_set()
    mqttc.connect_async(os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883")))
    mqttc.loop_start()
    threading.Thread(target=peak_watcher, daemon=True).start()
    yield
    mqttc.loop_stop()


# --- API HTTP -----------------------------------------------------------------------------------
app = FastAPI(title="Axceta Energy Cloud", lifespan=lifespan)


@app.middleware("http")
async def require_token(request: Request, call_next):
    expected = f"Bearer {API_TOKEN}"
    if API_TOKEN and not secrets.compare_digest(request.headers.get("authorization", ""), expected):
        return JSONResponse({"detail": "jeton invalide"}, status_code=401)
    return await call_next(request)


class Power(BaseModel):
    on: bool


class Peak(BaseModel):
    active: bool | None


@app.get("/api/state")
def get_state():
    return state()


@app.post("/api/devices/{dev}/power")
def set_power(dev: DeviceId, body: Power):
    with lock, db:
        db.execute("UPDATE devices SET shed=0 WHERE id=?", (dev,))  # choix manuel > délestage
    send_power(dev, body.on)
    return {"ok": True}


@app.delete("/api/devices/{dev}")
def forget_device(dev: DeviceId):
    """Oublie un appareil retiré. Ses mesures passées restent dans les totaux : l'énergie a été consommée."""
    for kind in ("status", "meta"):
        mqttc.publish(f"home/{dev}/{kind}", b"", qos=1, retain=True)  # efface les messages retenus
    with lock, db:
        db.execute("DELETE FROM devices WHERE id=?", (dev,))
    return {"ok": True}


@app.post("/api/peak")
def set_peak(body: Peak):
    global peak_override
    peak_override = body.active
    return {"ok": True}
