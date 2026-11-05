#!/usr/bin/env python3
"""
Automower-Wetterwaechter (Version fuer GitHub Actions)
======================================================
Parkt einen Husqvarna Automower bei Regen, nassem Rasen oder Kaelte -
basierend auf den Messwerten der naechstgelegenen DWD-Wetterstation
(abgerufen ueber die kostenlose Bright-Sky-API, kein Schluessel noetig).

Das Wetter wird nur geprueft, wenn es eine Rolle spielt: im Maehfenster laut
Zeitplan, VORLAUF_MIN Minuten vor Maehbeginn, wenn der Maeher (auch manuell
gestartet) unterwegs ist, oder solange das Skript ihn geparkt hat.

Im Wachdienst (WACHE=1) bleibt ein Lauf bis knapp 6 Stunden aktiv und prueft
nach eigener Uhr alle INTERVALL_MIN Minuten. Steht in den naechsten
RUHE_AB_STUNDEN Stunden kein Maehbeginn an (z. B. Winterpause), endet er frueher.

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
import subprocess
import sys
import time
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

# Optional (als GitHub-Variables anlegen, sonst gelten diese Standardwerte):
MOWER_NAME = env_str("MOWER_NAME")                      # leer = erster Maeher im Konto
REGEN_AKTUELL_MM = env_float("REGEN_AKTUELL_MM", 0.1)    # Niederschlag in 30 min, ab dem "es regnet"
REGEN_24H_MM = env_float("REGEN_24H_MM", 5.0)            # Regensumme 24 h, ab der nicht gemaeht wird
TEMP_MIN_C = env_float("TEMP_MIN_C", 5.0)                # darunter wird nicht gemaeht
TEMP_HYSTERESE_C = env_float("TEMP_HYSTERESE_C", 1.0)    # Freigabe erst bei TEMP_MIN_C + Hysterese
VORLAUF_MIN = env_float("VORLAUF_MIN", 30)              # so viele Minuten vor Maehbeginn wird schon geprueft
# Wachdienst: ein Lauf bleibt aktiv und prueft nach eigener Uhr (GitHub erlaubt max. 6 h pro Lauf)
WACHE = env_str("WACHE").lower() in ("1", "true", "ja")
INTERVALL_MIN = env_float("INTERVALL_MIN", 10)          # Pruefabstand im Wachdienst
WACHDAUER_MIN = env_float("WACHDAUER_MIN", 340)         # Laufdauer eines Wachdienstes (unter 6 h)
RUHE_AB_STUNDEN = env_float("RUHE_AB_STUNDEN", 12)      # kein Start in dieser Zeit -> Wachdienst pausiert
# ===========================================================

AUTH_URL = "https://api.authentication.husqvarnagroup.dev/v1/oauth2/token"
API_URL = "https://api.amc.husqvarna.dev/v1"
BRIGHTSKY_URL = "https://api.brightsky.dev"
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


def gruende_gegen_maehen(wx, state):
    gruende = []
    if wx["regen_30min"] >= REGEN_AKTUELL_MM or wx["zustand"] in REGEN_CONDITIONS:
        gruende.append(f"Regen aktuell ({wx['regen_30min']} mm/30 min, {wx['zustand']})")
    if wx["regen_24h"] >= REGEN_24H_MM:
        gruende.append(f"{wx['regen_24h']} mm Regen in 24 h")
    grenze = TEMP_MIN_C + (TEMP_HYSTERESE_C if state.get("geparkt_vom_skript") else 0)
    if wx["temp"] is not None and wx["temp"] < grenze:
        gruende.append(f"{wx['temp']} °C")
    return gruende


def pruefen_noetig(attr, state):
    """Wetter nur pruefen, wenn es fuer den Maeher gerade eine Rolle spielt.
    Gibt einen Grund (Text) zurueck oder None."""
    if state.get("geparkt_vom_skript"):
        return "vom Skript geparkt - warte auf besseres Wetter"
    aktivitaet = attr.get("mower", {}).get("activity", "")
    if aktivitaet in ("MOWING", "LEAVING"):
        return "Maeher ist unterwegs"
    if im_maehfenster({"attributes": attr}):
        return "Maehfenster laut Zeitplan"
    naechster = attr.get("planner", {}).get("nextStartTimestamp") or 0
    if naechster:
        # Husqvarna liefert die Ortszeit als Millisekunden-Zahl
        start = datetime(1970, 1, 1) + timedelta(milliseconds=naechster)
        minuten = (start - datetime.now()).total_seconds() / 60
        if minuten <= VORLAUF_MIN:
            return f"Maehbeginn um {start:%H:%M}"
    return None


# ---------------------- Hauptlogik ----------------------
def naechster_start(attr):
    ms = attr.get("planner", {}).get("nextStartTimestamp") or 0
    return datetime(1970, 1, 1) + timedelta(milliseconds=ms) if ms else None


def durchlauf(state):
    """Ein Pruefdurchgang. Gibt (naechster Start oder None, Wetter war relevant) zurueck.
    Wirft eine Ausnahme bei Fehlern."""
    token = get_token()
    mower = get_mower(token)
    attr = mower.get("attributes", {})
    anlass = pruefen_noetig(attr, state)
    if not anlass:
        ns = naechster_start(attr)
        txt = f"naechster Start {ns:%d.%m. %H:%M}" if ns else "kein Start geplant"
        log(f"Kein Maehfenster ({txt}) - Wetter wird nicht geprueft.")
        return ns, False
    log(f"Pruefe Wetter: {anlass}")
    wx = get_wetter()
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
    return naechster_start(attr), True


def status_sichern(state, vorher):
    """Status speichern und - in GitHub Actions - sofort ins Repository schreiben,
    damit er auch bei einem abgebrochenen Lauf erhalten bleibt."""
    save_state(state)
    jetzt = json.dumps(state, sort_keys=True)
    if jetzt == vorher or TESTMODUS or os.environ.get("GITHUB_ACTIONS") != "true":
        return jetzt
    ordner = os.path.dirname(STATE_FILE)
    befehle = [
        ["git", "config", "user.name", "automower-bot"],
        ["git", "config", "user.email", "automower-bot@users.noreply.github.com"],
        ["git", "add", os.path.basename(STATE_FILE)],
        ["git", "commit", "-q", "-m", "Status aktualisiert"],
        ["git", "pull", "-q", "--rebase"],
        ["git", "push", "-q"],
    ]
    for b in befehle:
        r = subprocess.run(b, cwd=ordner, capture_output=True, text=True)
        if r.returncode != 0 and b[1] != "commit":
            log(f"Hinweis: Status konnte nicht gesichert werden ({' '.join(b[:2])}: {r.stderr.strip()[:150]})")
            break
    return jetzt


def main():
    fehlend = [n for n, v in (("HUSQ_APP_KEY", HUSQ_APP_KEY), ("HUSQ_APP_SECRET", HUSQ_APP_SECRET),
                              ("LAT", LAT), ("LON", LON)) if not v]
    if fehlend:
        log("FEHLER: Es fehlen die Secrets " + ", ".join(fehlend))
        sys.exit(1)

    beginn = time.monotonic()
    ende = beginn + WACHDAUER_MIN * 60
    if WACHE:
        log(f"Wachdienst: pruefe alle {INTERVALL_MIN:g} Minuten, "
            f"bis {datetime.now() + timedelta(minutes=WACHDAUER_MIN):%H:%M}.")
    state = load_state()
    vorher = json.dumps(state, sort_keys=True)
    letzter_fehler = None
    while True:
        try:
            ns, relevant = durchlauf(state)
            letzter_fehler = None
        except Exception as e:  # noqa: BLE001
            log(f"FEHLER: {e}")
            letzter_fehler, ns, relevant = e, None, True
        vorher = status_sichern(state, vorher)

        if not WACHE:
            break
        rest = ende - time.monotonic()
        if rest < INTERVALL_MIN * 60:
            log("Wachdienst endet - der naechste Lauf uebernimmt.")
            break
        # Nichts zu tun in absehbarer Zeit (z. B. Winterpause): Lauf beenden, um GitHub nicht unnoetig zu belasten
        if (not relevant and not state.get("geparkt_vom_skript")
                and (ns is None or ns - datetime.now() > timedelta(hours=RUHE_AB_STUNDEN))):
            log(f"Kein Maehbeginn in den naechsten {RUHE_AB_STUNDEN:g} Stunden - Wachdienst pausiert.")
            break
        time.sleep(INTERVALL_MIN * 60)

    if letzter_fehler is not None:
        sys.exit(1)


if __name__ == "__main__":
    main()
