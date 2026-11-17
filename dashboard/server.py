"""Application locale : sert le dashboard, relaie l'API cloud (le jeton reste côté serveur)
et pilote les conteneurs Docker qui simulent les appareils."""
import asyncio
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import docker
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

CLOUD_API_URL = os.getenv("CLOUD_API_URL", "http://localhost:8000").rstrip("/")
API_TOKEN = os.getenv("API_TOKEN", "")
DEVICE_IMAGE = os.getenv("DEVICE_IMAGE", "local/axceta-device:latest")
DEVICE_NETWORK = os.getenv("DEVICE_NETWORK") or None
DEVICE_ENV = {k: v for k, v in os.environ.items() if k.startswith("MQTT_")}  # broker transmis aux nouveaux appareils
TYPES = ["lamp", "powerbar", "fridge", "heater", "ev_charger"]
LABEL = "axceta.device_id"
INDEX = Path(__file__).with_name("index.html")

try:
    dock = docker.from_env()
except docker.errors.DockerException:
    dock = None  # sans Docker, le dashboard affiche quand même les données du cloud

app = FastAPI(title="Axceta Energy - dashboard local")


@app.get("/")
def index():
    return FileResponse(INDEX)


# --- Relais vers le cloud -----------------------------------------------------------------------
def forward(method, path, body):
    req = urllib.request.Request(f"{CLOUD_API_URL}/api/{urllib.parse.quote(path)}", data=body or None, method=method, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {API_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return Response(r.read(), r.status, media_type="application/json")
    except urllib.error.HTTPError as e:
        return Response(e.read(), e.code, media_type="application/json")
    except OSError:
        return Response('{"detail": "cloud injoignable"}', 502, media_type="application/json")


@app.api_route("/api/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy(path: str, request: Request):
    return await asyncio.to_thread(forward, request.method, path, await request.body())


# --- Contrôle des appareils simulés (conteneurs Docker) ----------------------------------------
class NewDevice(BaseModel):
    type: str


def docker_client():
    if dock is None:
        raise HTTPException(503, "Docker indisponible : monter /var/run/docker.sock dans le conteneur")
    return dock


def find(dev):
    found = docker_client().containers.list(all=True, filters={"label": f"{LABEL}={dev}"})
    if not found:
        raise HTTPException(404, "aucun conteneur pour cet appareil")
    return found[0]


@app.get("/sim/devices")
def sim_list():
    containers = docker_client().containers.list(all=True, filters={"label": LABEL})
    return {"types": TYPES, "containers": {c.labels[LABEL]: c.status for c in containers}}


@app.post("/sim/devices")
def sim_add(body: NewDevice):
    if body.type not in TYPES:
        raise HTTPException(400, f"type inconnu, choix : {TYPES}")
    dev = f"{body.type.replace('_', '-')}-{secrets.token_hex(2)}"
    docker_client().containers.run(
        DEVICE_IMAGE, detach=True, name=f"axceta-{dev}", network=DEVICE_NETWORK, labels={LABEL: dev},
        environment={**DEVICE_ENV, "DEVICE_ID": dev, "DEVICE_TYPE": body.type})
    return {"id": dev}  # l'appareil s'enregistre tout seul auprès du cloud à sa connexion


@app.post("/sim/devices/{dev}/network/{action}")
def sim_network(dev: str, action: str):
    """Simule une panne réseau : un conteneur en pause ne répond plus, le broker publie son LWT."""
    if action not in ("cut", "restore"):
        raise HTTPException(404)
    c = find(dev)
    if action == "cut" and c.status == "running":
        c.pause()
    elif action == "restore" and c.status == "paused":
        c.unpause()
    return {"ok": True}


@app.delete("/sim/devices/{dev}")
def sim_remove(dev: str):
    c = find(dev)
    if c.status == "paused":
        c.unpause()
    c.stop(timeout=5)  # SIGTERM : l'appareil publie "offline" proprement
    c.remove()
    return {"ok": True}
