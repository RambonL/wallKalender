"""Selbsttest: .venv/bin/python test_server.py [ordner]  (mit Ordner: Vorschau-PNGs dort ablegen)"""
import sys
import tempfile
from datetime import date

import icalendar

import server

# Outlook-artig: Windows-TZID mit eigenem VTIMEZONE-Block
ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VTIMEZONE
TZID:W. Europe Standard Time
BEGIN:STANDARD
DTSTART:16010101T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:1
SUMMARY:Daily Standup
DTSTART;TZID=W. Europe Standard Time:20261005T093000
DTEND;TZID=W. Europe Standard Time:20261005T094500
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Urlaub
DTSTART;VALUE=DATE:20261118
DTEND;VALUE=DATE:20261121
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Abgesagt
STATUS:CANCELLED
DTSTART:20261105T120000Z
DTEND:20261105T130000Z
END:VEVENT
BEGIN:VEVENT
UID:4
SUMMARY:Bis Mitternacht
DTSTART:20261106T220000Z
DTEND:20261106T230000Z
END:VEVENT
BEGIN:VEVENT
UID:5
SUMMARY:Team Weekly – Projektstatus und Planung Q1
DTSTART;TZID=W. Europe Standard Time:20261103T110000
DTEND;TZID=W. Europe Standard Time:20261103T120000
RRULE:FREQ=WEEKLY;BYDAY=TU
END:VEVENT
"""
for i, (h, title) in enumerate([(13, "Zahnarzt"), (14, "Review Release 4.2"), (15, "1:1 Chef"),
                                (16, "Kundencall Müller GmbH"), (17, "Paket abholen"), (19, "Kino")]):
    ICS += f"BEGIN:VEVENT\nUID:x{i}\nSUMMARY:{title}\nDTSTART:20261112T{h - 1:02d}0000Z\nDTEND:20261112T{h:02d}0000Z\nEND:VEVENT\n"
ICS += "BEGIN:VEVENT\nUID:esc\nSUMMARY:Tom & Jerry <Party>\nDTSTART;VALUE=DATE:20261105\nDTEND;VALUE=DATE:20261106\nEND:VEVENT\n"
ICS += "BEGIN:VEVENT\nUID:ruf\nSUMMARY:Rufbereitschaft\nDTSTART:20261113T070000Z\nDTEND:20261116T070000Z\nEND:VEVENT\n"  # ≥24 h mit Uhrzeit
ICS += "END:VCALENDAR\n"

server._cals = {"test": icalendar.Calendar.from_ical(ICS)}
evs = server.events(date(2026, 11, 1), date(2026, 11, 30))

assert (True, "09:30", "Daily Standup", "") in evs[date(2026, 11, 9)], evs[date(2026, 11, 9)]  # Winterzeit
assert not any(t == "Daily Standup" for _, _, t, _ in evs.get(date(2026, 11, 7), []))         # Samstag
for day in (18, 19, 20):
    assert (False, "", "Urlaub", "") in evs[date(2026, 11, day)]
assert not any(t == "Urlaub" for _, _, t, _ in evs.get(date(2026, 11, 21), []))               # DTEND exklusiv
assert not any(t == "Abgesagt" for items in evs.values() for _, _, t, _ in items)
assert (True, "23:00", "Bis Mitternacht", "") in evs[date(2026, 11, 6)]
assert not any(t == "Bis Mitternacht" for _, _, t, _ in evs.get(date(2026, 11, 7), []))
assert evs[date(2026, 11, 18)][0][0] is False                                               # ganztägig zuerst
for day in (13, 14, 15, 16):                                                                # ≥24 h wie ganztägig, ohne Uhrzeit
    assert (False, "", "Rufbereitschaft", "") in evs[date(2026, 11, day)]
assert '<div class="ev allday urlaub">Urlaub</div>' in server.html_items(evs[date(2026, 11, 18)])  # Urlaub pink
assert "feiertag" in server.day_class(date(2026, 11, 1), date(2026, 11, 5), 6, 7, [])              # Allerheiligen pink
assert "feiertag" not in server.day_class(date(2026, 11, 7), date(2026, 11, 5), 5, 7, [])
assert "urlaubstag" in server.day_class(date(2026, 11, 18), date(2026, 11, 5), 2, 7, evs[date(2026, 11, 18)])  # ganze Zelle
assert "urlaubstag" not in server.day_class(date(2026, 11, 17), date(2026, 11, 5), 1, 7, evs[date(2026, 11, 17)])

# Zeitanzeige-Schalter: von–bis bzw. Dauer
for mode, standup, weekly in (("bis", "09:30–09:45", "11:00–12:00"), ("dauer", "09:30 · 15 min", "11:00 · 1 h")):
    server.zeit = mode
    e2 = server.events(date(2026, 11, 9), date(2026, 11, 10))
    assert (True, standup, "Daily Standup", "") in e2[date(2026, 11, 9)], (mode, e2[date(2026, 11, 9)])
    assert (True, weekly, "Team Weekly – Projektstatus und Planung Q1", "") in e2[date(2026, 11, 10)], mode
server.zeit = "start"

f = server.font(26)
assert server.wrap("Team Weekly – Projektstatus und Planung Q1", f, 239, 3) == ["Team Weekly –", "Projektstatus und", "Planung Q1"]
assert server.wrap("Ein Titel der viel zu lang ist für drei Zeilen in einer Spalte", f, 239, 3)[-1].endswith("…")
assert server.wrap("", f, 239, 3) == []

L = server.tag_label
assert L(date(2027, 1, 1)) == "Neujahr" and L(date(2027, 1, 4)) == "AT 1"                  # Fr Feiertag -> Mo ist AT 1
assert L(date(2027, 3, 26)) == "Karfreitag" and L(date(2027, 5, 27)) == "Fronleichnam"     # Ostern 2027: 28.3.
assert L(date(2026, 11, 2)) == "AT 1" and L(date(2026, 11, 30)) == "AT 21"
assert L(date(2026, 11, 7)) == "" and L(date(2026, 11, 1)) == "Allerheiligen"              # Sa leer, Feiertag auch am So
assert L(date(2026, 11, 18)) == "AT 13"                                                    # Urlaub zählt als AT

today = date(2026, 11, 5)  # Do, damit auch ohne Wochenende sichtbar
# Anzahl Tageszellen; Nov 2026 ohne Wochenende: Zeile 26.–30.10. hat keinen Novembertag und fällt weg
cells = {("month", True): 42, ("month", False): 25, ("week", True): 7, ("week", False): 5}
for (name, we), n in cells.items():
    server.view, server.weekend = name, we
    img = server.VIEWS[name][1](server.shown_date(today), today)
    assert img.size == (server.W, server.H) and img.mode == "L"
    html = server.page(today)
    assert html.count('class="day') == n, (name, we)
    assert 'class="day today"' in html and ">AT 4<" in html                                 # Do 5.11. eingekreist, AT 4
    assert "Tom &amp; Jerry &lt;Party&gt;" in html and "<Party>" not in html                 # Titel escaped
    assert f'const state = "{name}|{we:d}|0|start|1|0|2026-11-05|{server.START}"' in html  # Raster ist Standard, hell
    if len(sys.argv) > 1:
        out = f"{sys.argv[1]}/{name}{'' if we else '-mofr'}"
        img.save(f"{out}.png")
        open(f"{out}.html", "w").write(html)

# Pfeile: Monat/Woche blättern, auch über den Jahreswechsel
server.view, server.weekend = "month", True
for off, want in ((0, date(2026, 11, 1)), (2, date(2027, 1, 1)), (-11, date(2025, 12, 1))):
    server.offset = off
    assert server.shown_date(today) == want, (off, server.shown_date(today))
server.offset = 1
html = server.page(today)
assert "<h1>Dezember 2026</h1>" in html and 'class="day today"' not in html                    # heute nicht im Dezember
server.view = "week"
assert server.shown_date(today) == date(2026, 11, 12)
server.offset = 0

# KW im Montagsfeld der Monatsansicht (Nov 2026: erste Zeile ab 26.10. = KW 44, sechs Zeilen)
server.view = "month"
html = server.page(today)
assert "<span class=kwm>KW 44</span>" in html and "<span class=kwm>KW 49</span>" in html and html.count("class=kwm") == 6

# Outlook-Raster: Überschneidungen nebeneinander, Blöcke in Minuten ab Mitternacht
a, b, c = (600, 660, "x"), (840, 930, "y"), (840, 895, "z")
assert server.columns([b, a, c]) == [(a, 0, 1), (b, 0, 2), (c, 1, 2)]
assert server.columns([(540, 600), (570, 630), (600, 660)]) == [((540, 600), 0, 2), ((570, 630), 1, 2), ((600, 660), 0, 2)]
week = [date(2026, 11, 2) + server.timedelta(days=i) for i in range(7)]
bl = server.blocks(week)
assert (1380, 1440, "23:00", "Bis Mitternacht", "") in bl[date(2026, 11, 6)]
assert (570, 585, "09:30", "Daily Standup", "") in bl[date(2026, 11, 2)]
assert not any(t == "Tom & Jerry <Party>" for items in bl.values() for *_, t, _ in items)  # ganztägig: oben, nicht im Raster
server.view, server.raster = "week", True
html = server.page(today)
assert html.count('class="blk') == 7 and 'class="now"' in html and "Tom &amp; Jerry" in html  # 5× Standup, Weekly, Mitternacht
assert html.count('class="col') == 7
img = server.VIEWS["week"][1](server.shown_date(today), today)
assert img.size == (server.W, server.H)
if len(sys.argv) > 1:
    img.save(f"{sys.argv[1]}/week-raster.png")
    open(f"{sys.argv[1]}/week-raster.html", "w").write(html)
server.view = "month"

# THEME_CSS: eigene Farben landen hinter dem eingebauten CSS, fehlende Datei stört nicht
with tempfile.NamedTemporaryFile("w", suffix=".css") as f:
    f.write(".dark{--bg:#123456}")
    f.flush()
    server.THEME_CSS = f.name
    assert ".dark{--bg:#123456}</style>" in server.page(today)
assert "#123456" not in server.page(today)  # Datei weg
server.THEME_CSS = ""

# Teams: trotz 10er-Grenze pro Antwort kommen alle Termine an, und die ICS-Datei landet richtig im Kalender
import teams_ics  # noqa: E402


def g(uid, start, end, allday=False, cancelled=False):
    return {"id": uid, "subject": uid, "isAllDay": allday, "isCancelled": cancelled,
            "start": {"dateTime": start + ".0000000", "timeZone": "UTC"}, "end": {"dateTime": end + ".0000000", "timeZone": "UTC"}}


G = [g(f"T{d}", f"2026-11-{d:02d}T09:00:00", f"2026-11-{d:02d}T09:30:00") for d in range(2, 28)]
G += [g("Messe", "2026-11-23T00:00:00", "2026-11-26T00:00:00", allday=True),
      g("Abgesagt", "2026-11-04T12:00:00", "2026-11-04T13:00:00", cancelled=True)]
utc = teams_ics.utc


def fake_ask(start, end):  # verhält sich wie teams-for-linux: höchstens 10 Termine, sonst nextLink
    hit = [x for x in G if utc(x["start"]) < end and utc(x["end"]) > start]
    return {"value": hit[:10], **({"@odata.nextLink": "…"} if len(hit) > 10 else {})}


start, end = teams_ics.month_range(date(2026, 11, 9))
assert (start.isoformat(), end.isoformat()) == ("2026-10-25T23:00:00+00:00", "2027-01-03T23:00:00+00:00")  # 26.10.–3.1. Berlin
got = teams_ics.fetch(fake_ask, start, end)
assert {x["id"] for x in got} == {x["id"] for x in G}
ws, wend = teams_ics.wide_range(date(2026, 11, 9))
assert (ws.isoformat(), wend.isoformat()) == ("2026-08-31T22:00:00+00:00", "2027-05-31T22:00:00+00:00")  # 1.9.–31.5. Berlin
gone = g("Gelöscht", "2026-11-10T08:00:00", "2026-11-10T09:00:00")  # im stündlichen Abruf, inzwischen in Teams gelöscht
far = g("Weit", "2027-03-01T08:00:00", "2027-03-01T09:00:00")
assert {x["id"] for x in teams_ics.merge(got, [gone, far], start, end)} == {x["id"] for x in G} | {"Weit"}
server._cals = {"teams": icalendar.Calendar.from_ical(teams_ics.to_ics(got))}
evs = server.events(date(2026, 11, 1), date(2026, 11, 30))
assert (True, "10:00", "T9", "Teams") in evs[date(2026, 11, 9)]                            # 09:00 UTC = 10:00 Winterzeit
assert all((False, "", "Messe", "Teams") in evs[date(2026, 11, d)] for d in (23, 24, 25))
assert '<div class="ev teams">' in server.html_items(evs[date(2026, 11, 9)])                # Teams blau
assert '<div class="ev allday teams">' in server.html_items(evs[date(2026, 11, 23)])
assert not any(t == "Messe" for _, _, t, _ in evs.get(date(2026, 11, 26), []))
assert not any(t == "Abgesagt" for items in evs.values() for _, _, t, _ in items)
print("ok")
