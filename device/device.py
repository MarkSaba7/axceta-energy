"""Appareil simulé : mesure le courant sur le fil d'un appareil et le publie au cloud en MQTT.

Topics (DEVICE_ID = id de l'appareil) :
  home/<id>/meta       retenu   {"type", "version"}         -> auto-enregistrement
  home/<id>/status     retenu   "online" | "offline"        -> "offline" publié par le broker (LWT) si on perd l'appareil
  home/<id>/telemetry           {"ts", "on", "voltage_v", "current_a", "power_w", "energy_wh"}
  home/<id>/cmd        (reçu)   {"on": true|false}
"""
import contextlib
import json
import math
import os
import random
import signal
import socket
import sys
import time

import paho.mqtt.client as mqtt

DEVICE_ID = os.getenv("DEVICE_ID") or socket.gethostname()
DEVICE_TYPE = os.getenv("DEVICE_TYPE", "lamp")
INTERVAL = float(os.getenv("INTERVAL_S", "2"))
VERSION = os.getenv("VERSION", "dev")

# type -> (tension en V, puissance en W quand allumé, en fonction du temps t en secondes)
PROFILES = {
    "lamp": (120, lambda t: 60),
    "powerbar": (120, lambda t: 150 + 40 * math.sin(t / 30)),  # télé + console + chargeurs
    "fridge": (120, lambda t: 150 if t % 120 < 40 else 3),  # cycles du compresseur
    "heater": (240, lambda t: 1500 if t % 60 < 30 else 0),  # plinthe + thermostat
    "ev_charger": (240, lambda t: 7200),  # borne niveau 2, 30 A
}
if DEVICE_TYPE not in PROFILES:
    sys.exit(f"DEVICE_TYPE inconnu : {DEVICE_TYPE} (choix : {', '.join(PROFILES)})")
VOLTS, PROFILE = PROFILES[DEVICE_TYPE]
TOPIC = f"home/{DEVICE_ID}"
state = {"on": True}


def on_connect(client, userdata, flags, reason_code, properties):
    client.publish(f"{TOPIC}/meta", json.dumps({"type": DEVICE_TYPE, "version": VERSION}), qos=1, retain=True)
    client.publish(f"{TOPIC}/status", "online", qos=1, retain=True)
    client.subscribe(f"{TOPIC}/cmd", qos=1)
    print(f"{DEVICE_ID} ({DEVICE_TYPE}) connecté", flush=True)


def on_message(client, userdata, msg):
    try:
        state["on"] = bool(json.loads(msg.payload)["on"])
        print(f"commande reçue : on={state['on']}", flush=True)
    except (ValueError, KeyError, TypeError):
        print(f"commande ignorée : {msg.payload!r}", flush=True)


def stop(*_):
    raise SystemExit


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=DEVICE_ID)
client.on_connect = on_connect
client.on_message = on_message
client.will_set(f"{TOPIC}/status", "offline", qos=1, retain=True)
# Hors ligne, paho garde les messages QoS 1 en file et les renvoie à la reconnexion (store-and-forward).
client.max_queued_messages_set(1800)
if os.getenv("MQTT_USERNAME"):
    client.username_pw_set(os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD"))
if os.getenv("MQTT_TLS") == "1":
    client.tls_set()
# keepalive court : le broker déclare l'appareil hors ligne ~7 s après la perte de connexion.
client.connect_async(os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883")), keepalive=5)
signal.signal(signal.SIGTERM, stop)
client.loop_start()

start = time.time()
try:
    while True:
        watts = PROFILE(time.time() - start) * random.uniform(0.97, 1.03) if state["on"] else 0.0
        reading = {
            "ts": time.time(),
            "on": state["on"],
            "voltage_v": VOLTS,
            "current_a": round(watts / VOLTS, 3),
            "power_w": round(watts, 1),
            "energy_wh": watts * INTERVAL / 3600,
        }
        client.publish(f"{TOPIC}/telemetry", json.dumps(reading), qos=1)
        time.sleep(INTERVAL)
finally:
    # Arrêt propre (docker stop) : on annonce "offline" nous-mêmes, le LWT ne sert qu'aux pertes brutales.
    with contextlib.suppress(Exception):
        client.publish(f"{TOPIC}/status", "offline", qos=1, retain=True).wait_for_publish(2)
    client.disconnect()
    client.loop_stop()
