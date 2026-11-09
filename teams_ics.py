#!/usr/bin/env python3
"""Teams-Arbeitskalender als teams.ics, geholt über teams-for-linux (MQTT-Befehl get-calendar, Microsoft Graph).

Läuft dauerhaft und schreibt alle 5 min die Termine nach TEAMS_ICS: aktueller und nächster Monat frisch,
dazu 2 Monate zurück bis 6 Monate voraus, die nur einmal pro Stunde neu geholt werden (ändern sich selten).
Der Kalender-Server liest die Datei wie jeden anderen Kalender: ICS_URLS="file:///<pfad>/teams.ics ..."
Antwortet Teams nicht (PC aus, Teams zu), bleibt die alte Datei stehen.
In die Datei kommen nur Titel und Zeiten, keine Beschreibungen oder Teilnehmer.

Voraussetzung in der config.json von teams-for-linux: "graphApi": {"enabled": true} und mqtt.commandTopic "command".
Env: TEAMS_MQTT=mqtt://benutzer:passwort@host:1883 (Sonderzeichen im Passwort %-kodiert), TEAMS_ICS (Standard teams.ics),
     TEAMS_TOPIC (topicPrefix aus der teams-for-linux-Config, Standard teams), TZ
"""
import calendar
import json
import os
import queue
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import icalendar
import paho.mqtt.client as mqtt

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))
UTC = timezone.utc
PREFIX = os.environ.get("TEAMS_TOPIC", "teams")
COMMAND, ANSWER = f"{PREFIX}/command", f"{PREFIX}/calendar"
WIDE_SECONDS = 3600  # so oft wird der weite Bereich (2 Monate zurück bis 6 voraus) neu geholt


def utc(t):
    """Graph-Zeit {"dateTime": "2026-11-09T09:00:00.0000000", "timeZone": "UTC"} als datetime."""
    assert t["timeZone"] == "UTC", t["timeZone"]  # Graph liefert UTC, solange niemand anderes verlangt
    return datetime.fromisoformat(t["dateTime"][:19]).replace(tzinfo=UTC)


def month_start(d, k):
    """1. des Monats, k Monate von d entfernt, als Mitternacht Ortszeit in UTC."""
    m = d.month - 1 + k
    return datetime(d.year + m // 12, m % 12 + 1, 1, tzinfo=TZ).astimezone(UTC)


def wide_range(today):
    return month_start(today, -2), month_start(today, 7)  # bis Ende des 6. Monats nach diesem


def merge(near, wide, start, end):
    """Im nahen Bereich start..end gilt nur die frische Abfrage, damit gelöschte Termine dort sofort verschwinden."""
    return near + [e for e in wide if not (utc(e["start"]) < end and utc(e["end"]) > start)]


def month_range(today):
    """Montag vor dem 1. dieses Monats bis Sonntag nach dem Ende des nächsten Monats (wie im Monatsraster), in UTC."""
    nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    first = calendar.Calendar().monthdatescalendar(today.year, today.month)[0][0]
    last = calendar.Calendar().monthdatescalendar(nxt.year, nxt.month)[-1][-1] + timedelta(days=1)
    return [datetime.combine(d, datetime.min.time(), TZ).astimezone(UTC) for d in (first, last)]


def fetch(ask, start, end):
    """Alle Termine von start bis end.

    Workaround für https://github.com/IsmaelMartinez/teams-for-linux/issues/3202: get-calendar liefert höchstens
    10 Termine. Steht @odata.nextLink in der Antwort, fehlen welche, dann den Zeitraum halbieren und beide Hälften
    einzeln fragen. Ist das Issue behoben, reicht eine Anfrage mit "top".
    """
    data = ask(start, end)
    if "@odata.nextLink" not in data or end - start <= timedelta(hours=1):  # ponytail: >10 Termine in 1 h, Rest fehlt
        return data["value"]
    mid = start + (end - start) / 2
    return fetch(ask, start, mid) + fetch(ask, mid, end)


def to_ics(events):
    """Graph-Termine (Zeiten in UTC) als ICS-Datei."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//wallKalender//teams_ics//DE")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Teams")  # daran erkennt der Server Teams-Termine und färbt sie blau
    for e in {e["id"]: e for e in events}.values():  # Termine über eine Hälftengrenze kommen doppelt
        ev = icalendar.Event()
        ev.add("uid", e["id"])
        ev.add("summary", e.get("subject") or "")
        for key in ("start", "end"):
            t = utc(e[key])
            ev.add(f"dt{key}", t.date() if e["isAllDay"] else t)
        if e.get("isCancelled"):
            ev.add("status", "CANCELLED")
        cal.add_component(ev)
    return cal.to_ical()


def main():
    url = urlsplit(os.environ["TEAMS_MQTT"])
    path = os.environ.get("TEAMS_ICS", "teams.ics")
    answers = queue.Queue()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="wallKalender")  # nie "teams-for-linux", sonst fliegt Teams vom Broker
    client.username_pw_set(unquote(url.username), unquote(url.password))
    client.on_connect = lambda c, *_: c.subscribe(ANSWER)  # nach einem Reconnect neu abonnieren
    client.on_message = lambda c, userdata, msg: answers.put(msg.payload)
    client.connect(url.hostname, url.port or 1883)
    client.subscribe(ANSWER)  # sofort, damit das Abo vor der ersten Anfrage steht
    client.loop_start()

    def ask(start, end):
        while not answers.empty():  # verspätete Antworten wegwerfen; Antworten tragen keine Anfrage-ID
            answers.get_nowait()
        client.publish(COMMAND, json.dumps({"action": "get-calendar", "startDate": f"{start:%Y-%m-%dT%H:%M:%SZ}",
                                            "endDate": f"{end:%Y-%m-%dT%H:%M:%SZ}"}))
        return json.loads(answers.get(timeout=20))["data"]

    wide, wide_at = [], 0.0
    while True:
        try:
            today = datetime.now(TZ).date()
            if time.time() - wide_at > WIDE_SECONDS:
                wide, wide_at = fetch(ask, *wide_range(today)), time.time()
            start, end = month_range(today)
            events = merge(fetch(ask, start, end), wide, start, end)
            with open(path + ".tmp", "wb") as f:
                f.write(to_ics(events))
            os.replace(path + ".tmp", path)  # der Server liest nie eine halb geschriebene Datei
            print(f"{datetime.now(TZ):%H:%M} {len(events)} Termine nach {path}", flush=True)
        except Exception as e:
            print(f"{datetime.now(TZ):%H:%M} Teams antwortet nicht, alte Datei bleibt: {e!r}", flush=True)
        time.sleep(300)


if __name__ == "__main__":
    main()
