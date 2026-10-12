#!/usr/bin/env python3
"""
Automower-Wetterwaechter (Version fuer GitHub Actions)
======================================================
Parkt einen Husqvarna Automower bei Regen, nassem Rasen oder Kaelte -
basierend auf den Messwerten der naechstgelegenen DWD-Wetterstation
(abgerufen ueber die kostenlose Bright-Sky-API, kein Schluessel noetig).

Optional wird zusaetzlich der eigene Hunter-Regensensor (z. B. Rain-Clik)
ueber das Hydrawise-Konto abgefragt. Ist er eingerichtet, entscheidet er
ueber "Regen aktuell"; Temperatur und 24-h-Regenmenge kommen weiter vom DWD.

Regeln:
  * Regen aktuell                        -> Maeher faehrt in die Station
  * Regen in den letzten 24 h >= X mm    -> Maeher startet nicht / faehrt heim
  * Temperatur unter X °C                -> Maeher startet nicht / faehrt heim
Sobald das Wetter wieder passt, wird der Maeher fuer den NAECHSTEN
Starttermin freigegeben (ein laufendes Zeitfenster wird uebersprungen).

Alle Einstellungen kommen aus Umgebungsvariablen, die GitHub aus den
"Secrets" bzw. "Variables" des Repositorys setzt (siehe ANLEITUNG.md).

Lokaler Testlauf ohne Befehle an den Maeher:
  HUSQ_APP_KEY=... HUSQ_APP_SECRET=... LAT=... LON=... python3 automower_wetter.py --test
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests


def env_str(name, default=""):
    wert = os.environ.get(name, "").strip()
    return wert if wert else default


def env_float(name, default):
    wert = os.environ.get(name, "").strip().replace(",", ".")
    try:
        return float(wert) if wert else default
    except ValueError:
        print(f"Warnung: {name}='{wert}' ist keine Zahl, nehme {default}")
        return default


# ====================== EINSTELLUNGEN ======================
# Pflicht (als GitHub-Secrets anlegen):
HUSQ_APP_KEY = env_str("HUSQ_APP_KEY")
HUSQ_APP_SECRET = env_str("HUSQ_APP_SECRET")
LAT = env_float("LAT", None)
LON = env_float("LON", None)

# Optional: Hunter-Regensensor ueber Hydrawise (als GitHub-Secrets anlegen):
HYDRAWISE_USER = env_str("HYDRAWISE_USER")              # E-Mail des Hydrawise-Kontos
HYDRAWISE_PASSWORD = env_str("HYDRAWISE_PASSWORD")      # Passwort des Hydrawise-Kontos
HYDRAWISE_SENSOR = env_str("HYDRAWISE_SENSOR")          # optional: Name des Sensors, leer = erster Regensensor

# Optional (als GitHub-Variables anlegen, sonst gelten diese Standardwerte):
MOWER_NAME = env_str("MOWER_NAME")                      # leer = erster Maeher im Konto
REGEN_AKTUELL_MM = env_float("REGEN_AKTUELL_MM", 0.1)    # Niederschlag in 30 min, ab dem "es regnet"
REGEN_24H_MM = env_float("REGEN_24H_MM", 5.0)            # Regensumme 24 h, ab der nicht gemaeht wird
TEMP_MIN_C = env_float("TEMP_MIN_C", 5.0)                # darunter wird nicht gemaeht
TEMP_HYSTERESE_C = env_float("TEMP_HYSTERESE_C", 1.0)    # Freigabe erst bei TEMP_MIN_C + Hysterese
# ===========================================================

AUTH_URL = "https://api.authentication.husqvarnagroup.dev/v1/oauth2/token"
API_URL = "https://api.amc.husqvarna.dev/v1"
BRIGHTSKY_URL = "https://api.brightsky.dev"
# Hydrawise-Cloud (gleiche Zugangswege wie die Hydrawise-App bzw. Home Assistant)
HYDRAWISE_TOKEN_URL = "https://app.hydrawise.com/api/v2/oauth/access-token"
HYDRAWISE_GRAPH_URL = "https://app.hydrawise.com/api/v2/graph"
HYDRAWISE_CLIENT_ID = "hydrawise_app"
HYDRAWISE_CLIENT_SECRET = "zn3CrjglwNV1"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "automower_wetter_state.json")
REGEN_CONDITIONS = {"rain", "sleet", "snow", "hail", "thunderstorm"}
WOCHENTAGE = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

TESTMODUS = "--test" in sys.argv or env_str("TESTMODUS").lower() in ("1", "true", "ja")


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    # Kein Zugangstoken speichern - die Datei liegt im Repository.
    state.pop("token", None)
    state.pop("token_exp", None)
    # Datum einmal pro Tag aktualisieren: so gibt es regelmaessig einen Commit
    # und GitHub schaltet den Zeitplan nicht nach 60 Tagen ab.
    state["stand"] = date.today().isoformat()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


# ---------------------- Husqvarna API ----------------------
def get_token():
    r = requests.post(
        AUTH_URL,
        data={"grant_type": "client_credentials",
              "client_id": HUSQ_APP_KEY,
              "client_secret": HUSQ_APP_SECRET},
        timeout=20,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Anmeldung bei Husqvarna fehlgeschlagen ({r.status_code}). "
                           "Application Key/Secret pruefen.")
    return r.json()["access_token"]


def api_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "X-Api-Key": HUSQ_APP_KEY,
        "Authorization-Provider": "husqvarna",
        "Content-Type": "application/vnd.api+json",
    }


def get_mower(token):
    r = requests.get(f"{API_URL}/mowers", headers=api_headers(token), timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"Maeherdaten nicht abrufbar ({r.status_code}): {r.text[:200]}")
    mowers = r.json().get("data", [])
    if not mowers:
        raise RuntimeError("Kein Maeher im Husqvarna-Konto gefunden.")
    if MOWER_NAME:
        for m in mowers:
            if m.get("attributes", {}).get("system", {}).get("name") == MOWER_NAME:
                return m
        raise RuntimeError(f"Maeher '{MOWER_NAME}' nicht gefunden.")
    return mowers[0]


def send_action(token, mower_id, action):
    if TESTMODUS:
        log(f"[TEST] wuerde senden: {action}")
        return
    body = {"data": {"type": action}}
    r = requests.post(f"{API_URL}/mowers/{mower_id}/actions",
                      headers=api_headers(token), data=json.dumps(body), timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"Befehl {action} fehlgeschlagen: {r.status_code} {r.text[:200]}")
    log(f"Befehl gesendet: {action}")


def im_maehfenster(mower):
    """True, wenn laut Maehplan gerade ein Zeitfenster aktiv ist (Ortszeit ueber TZ)."""
    now = datetime.now()
    minute = now.hour * 60 + now.minute
    tag = WOCHENTAGE[now.weekday()]
    tasks = mower.get("attributes", {}).get("calendar", {}).get("tasks", [])
    for t in tasks:
        start = t.get("start", 0)
        if t.get(tag) and start <= minute < start + t.get("duration", 0):
            return True
    return False


# ---------------------- Wetter (DWD via Bright Sky) ----------------------
def get_wetter():
    r = requests.get(f"{BRIGHTSKY_URL}/current_weather",
                     params={"lat": LAT, "lon": LON}, timeout=20)
    r.raise_for_status()
    cj = r.json()
    w = cj.get("weather", {})
    quellen = cj.get("sources") or [{}]
    station = quellen[0].get("station_name", "?")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    r = requests.get(f"{BRIGHTSKY_URL}/weather",
                     params={"lat": LAT, "lon": LON,
                             "date": (now - timedelta(hours=24)).isoformat(),
                             "last_date": now.isoformat()},
                     timeout=20)
    r.raise_for_status()
    regen_24h = 0.0
    for h in r.json().get("weather", []):
        ts = datetime.fromisoformat(h["timestamp"])
        if ts <= now:
            regen_24h += h.get("precipitation") or 0.0

    return {
        "station": station,
        "temp": w.get("temperature"),
        "regen_30min": w.get("precipitation_30") or 0.0,
        "zustand": w.get("condition"),
        "regen_24h": round(regen_24h, 1),
    }


# ---------------------- Hunter-Regensensor (Hydrawise) ----------------------
def get_regensensor():
    """Gibt (name, nass) zurueck, oder None wenn Hydrawise nicht eingerichtet ist.
    Wirft eine Ausnahme bei Verbindungs-/Anmeldefehlern."""
    if not (HYDRAWISE_USER and HYDRAWISE_PASSWORD):
        return None
    r = requests.post(
        HYDRAWISE_TOKEN_URL,
        data={"client_id": HYDRAWISE_CLIENT_ID, "client_secret": HYDRAWISE_CLIENT_SECRET,
              "grant_type": "password", "scope": "all",
              "username": HYDRAWISE_USER, "password": HYDRAWISE_PASSWORD},
        timeout=20,
    )
    j = r.json() if r.content else {}
    if r.status_code >= 300 or "access_token" not in j:
        raise RuntimeError("Anmeldung bei Hydrawise fehlgeschlagen - E-Mail/Passwort pruefen.")
    global _HYDRAWISE_AUTH
    _HYDRAWISE_AUTH = f"{j.get('token_type', 'Bearer')} {j['access_token']}"
    query = ("{ me { controllers { name sensors { name "
             "model { sensorType } status { active } } } } }")
    r = requests.post(HYDRAWISE_GRAPH_URL, json={"query": query},
                      headers={"Authorization": _HYDRAWISE_AUTH}, timeout=20)
    r.raise_for_status()
    controllers = (((r.json().get("data") or {}).get("me") or {}).get("controllers")) or []
    alle = [s for c in controllers for s in (c.get("sensors") or [])]
    if not alle:
        raise RuntimeError(f"Hydrawise meldet {len(controllers)} Steuergeraet(e), aber keine Sensoren. "
                           "Regensensor in der Hydrawise-App unter Steuergeraet > Sensoren einrichten.")
    kandidaten = []
    if HYDRAWISE_SENSOR:
        kandidaten = [s for s in alle if (s.get("name") or "").strip().lower() == HYDRAWISE_SENSOR.lower()]
        if not kandidaten:
            log(f"Hinweis: Sensorname '{HYDRAWISE_SENSOR}' nicht gefunden - waehle automatisch.")
    if not kandidaten:
        # bevorzugt Kontakt-Regensensoren, sonst jeder Sensor ausser Durchflussmessern
        kandidaten = ([s for s in alle if (s.get("model") or {}).get("sensorType") in ("LEVEL_OPEN", "LEVEL_CLOSED")]
                      or [s for s in alle if (s.get("model") or {}).get("sensorType") != "FLOW"])
    if not kandidaten:
        gefunden = ", ".join(f"'{s.get('name')}' ({(s.get('model') or {}).get('sensorType')})" for s in alle)
        raise RuntimeError(f"Kein passender Regensensor. Gefunden: {gefunden}")
    s = kandidaten[0]
    return s.get("name") or "Regensensor", bool((s.get("status") or {}).get("active"))


_HYDRAWISE_AUTH = None


def _wert(v, art):
    """LocalizedValueType {value, unit} in °C bzw. mm umrechnen."""
    if not v or v.get("value") is None:
        return None
    x, einheit = float(v["value"]), (v.get("unit") or "").lower()
    if art == "temp" and "f" in einheit:
        x = (x - 32) * 5 / 9
    if art == "regen" and einheit in ("in", "inch", "inches", '"'):
        x = x * 25.4
    return round(x, 1)


def get_hydrawise_wetter():
    """Temperatur und 24-h-Regen, die Hydrawise fuer das Steuergeraet verwendet.
    Gibt dict oder None zurueck (nur nach erfolgreichem get_regensensor)."""
    if not _HYDRAWISE_AUTH:
        return None
    lv = "{ value unit }"
    query = ("{ me { controllers { name "
             f"observationsSummary {{ currentTemperature {lv} rainfallLast24Hours {lv} }} "
             f"weatherStations {{ location distance {lv} currentObservation {{ temperature {lv} }} }} "
             "} } }")
    r = requests.post(HYDRAWISE_GRAPH_URL, json={"query": query},
                      headers={"Authorization": _HYDRAWISE_AUTH}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("errors") and not j.get("data"):
        raise RuntimeError(str(j["errors"])[:200])
    for c in (((j.get("data") or {}).get("me") or {}).get("controllers")) or []:
        obs = c.get("observationsSummary") or {}
        temp = _wert(obs.get("currentTemperature"), "temp")
        regen = _wert(obs.get("rainfallLast24Hours"), "regen")
        stationen = c.get("weatherStations") or []
        name = (stationen[0].get("location") if stationen else None) or "Hydrawise"
        if temp is None and stationen:
            temp = _wert((stationen[0].get("currentObservation") or {}).get("temperature"), "temp")
        if temp is not None or regen is not None:
            return {"station": name, "temp": temp, "regen_24h": regen}
    return None


def gruende_gegen_maehen(wx, state):
    gruende = []
    if wx.get("sensor_nass") is not None:
        # Eigener Regensensor vorhanden: er entscheidet ueber "Regen aktuell"
        if wx["sensor_nass"]:
            gruende.append(f"Regensensor '{wx['sensor_name']}' meldet Regen")
    elif wx["regen_30min"] >= REGEN_AKTUELL_MM or wx["zustand"] in REGEN_CONDITIONS:
        gruende.append(f"Regen aktuell ({wx['regen_30min']} mm/30 min, {wx['zustand']})")
    if wx["regen_24h"] >= REGEN_24H_MM:
        gruende.append(f"{wx['regen_24h']} mm Regen in 24 h")
    grenze = TEMP_MIN_C + (TEMP_HYSTERESE_C if state.get("geparkt_vom_skript") else 0)
    if wx["temp"] is not None and wx["temp"] < grenze:
        gruende.append(f"{wx['temp']} °C")
    return gruende


# ---------------------- Hauptlogik ----------------------
def main():
    fehlend = [n for n, v in (("HUSQ_APP_KEY", HUSQ_APP_KEY), ("HUSQ_APP_SECRET", HUSQ_APP_SECRET),
                              ("LAT", LAT), ("LON", LON)) if not v]
    if fehlend:
        log("FEHLER: Es fehlen die Secrets " + ", ".join(fehlend))
        sys.exit(1)

    state = load_state()
    try:
        wx = get_wetter()
        wx["sensor_nass"] = None
        try:
            sensor = get_regensensor()
            if sensor:
                wx["sensor_name"], wx["sensor_nass"] = sensor
                log(f"Hunter-Regensensor '{sensor[0]}': {'NASS' if sensor[1] else 'trocken'}")
        except Exception as e:  # noqa: BLE001
            log(f"Warnung: Regensensor nicht abrufbar ({e}) - nehme DWD-Regendaten.")
        try:
            hw = get_hydrawise_wetter()
            if hw:
                log(f"Hydrawise-Wetter ({hw['station']}): {hw['temp']} °C, 24 h {hw['regen_24h']} mm "
                    f"(DWD {wx['station']}: {wx['temp']} °C, 24 h {wx['regen_24h']} mm)")
                if hw["temp"] is not None:
                    wx["temp"] = hw["temp"]
                if hw["regen_24h"] is not None:
                    wx["regen_24h"] = hw["regen_24h"]
                wx["station"] = f"{hw['station']} (Hydrawise)"
        except Exception as e:  # noqa: BLE001
            log(f"Hinweis: Hydrawise-Wetterdaten nicht abrufbar ({e}) - nehme DWD.")
        token = get_token()
        mower = get_mower(token)
        attr = mower.get("attributes", {})
        mower_id = mower["id"]
        aktivitaet = attr.get("mower", {}).get("activity", "?")
        sperrgrund = attr.get("planner", {}).get("restrictedReason", "")
        verbunden = attr.get("metadata", {}).get("connected", True)

        gruende = gruende_gegen_maehen(wx, state)
        log(f"Station {wx['station']}: {wx['temp']} °C, Regen 30 min {wx['regen_30min']} mm, "
            f"24 h {wx['regen_24h']} mm | Maeher: {aktivitaet} | "
            f"{'SPERRE: ' + ', '.join(gruende) if gruende else 'Wetter ok'}"
            f"{' | TESTMODUS' if TESTMODUS else ''}")

        if not verbunden:
            log("Maeher ist offline - kein Befehl moeglich.")
        elif gruende:
            if not state.get("geparkt_vom_skript"):
                if aktivitaet == "PARKED_IN_CS" and sperrgrund == "PARK_OVERRIDE":
                    log("Maeher wurde bereits manuell geparkt - Skript greift nicht ein.")
                else:
                    send_action(token, mower_id, "ParkUntilFurtherNotice")
                    if not TESTMODUS:
                        state["geparkt_vom_skript"] = True
                        state["grund"] = ", ".join(gruende)
        elif state.get("geparkt_vom_skript"):
            # Wetter wieder gut: fuer den naechsten Starttermin freigeben
            if im_maehfenster(mower):
                send_action(token, mower_id, "ParkUntilNextSchedule")
            else:
                send_action(token, mower_id, "ResumeSchedule")
            if not TESTMODUS:
                state["geparkt_vom_skript"] = False
                state.pop("grund", None)
    except Exception as e:  # noqa: BLE001
        log(f"FEHLER: {e}")
        save_state(state)
        sys.exit(1)
    save_state(state)


if __name__ == "__main__":
    main()

