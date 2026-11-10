#!/usr/bin/env python3
"""Kalender-Server: ICS-Kalender rein, Webseite und Graustufen-PNG 1872x1404 raus.

GET /                  Webseite der aktuellen Ansicht (lädt neu bei Ansichts- oder Datumswechsel und alle 5 min)
GET /image.png         aktuelle Ansicht als PNG (für ein E-Ink-Display)
GET|POST /view/<name>  Ansicht wechseln (month|week), z. B. Stream Deck: curl http://host:8080/view/week
GET|POST /weekend      Wochenende ein-/ausblenden (Monat und Woche)
GET|POST /prev /next   einen Monat bzw. eine Woche zurück/weiter; /today zurück zu heute
GET|POST /zeit         Uhrzeit der Termine als Start, von–bis oder Start mit Dauer
GET|POST /raster       Woche als Outlook-Raster (Standard, Stunden senkrecht) oder als Liste
GET|POST /dark         Dunkelmodus der Webseite an/aus (das E-Ink-Bild bleibt hell)
GET /termin[/<n>]      nächster (n-ter) Termin mit Uhrzeit als kurzer Text, z. B. für eine Stream-Deck-Taste
POST /termine          Termin aus dem Formular „+ Termin“ an die Datei TERMINE anhängen; /termine/weg löscht eine Zeile
GET|POST /cal/<n>      Kalender n (1, 2, … wie in ICS_URLS) aus-/einblenden, auf der Webseite im Menü „Kalender“
GET /state             Ansicht, Wochenende, Verschiebung, Zeitanzeige, Raster, Dunkel, aus-Kalender, Datum, Serverstart; fragt die Webseite alle 3 s ab

Env: ICS_URLS (Leerzeichen-getrennt), ICS_NAMES (Namen im Menü, gleiche Reihenfolge), DEVICE_REFRESH_URL, TZ, PORT, DARK=1 (startet im Dunkelmodus),
     THEME_CSS (eigene CSS-Datei, kommt nach dem eingebauten CSS, z. B. eigene Farben für .dark),
     TERMINE (Datei mit monatlichen Terminen, eine Regel pro Zeile, z. B. "4.AT 09:00 Reisekosten abrechnen")
"""
import calendar
import functools
import io
import os
import re
import textwrap
import threading
import time
import urllib.request
from urllib.parse import parse_qs
from datetime import date, datetime, timedelta
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
from dateutil.easter import easter
from PIL import Image, ImageDraw, ImageFont

W, H = 1872, 1404
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))
ICS_URLS = [u.replace("webcal://", "https://", 1) for u in os.environ.get("ICS_URLS", "").split()]
ICS_NAMES = os.environ.get("ICS_NAMES", "").split()  # Namen im Menü „Kalender“, gleiche Reihenfolge wie ICS_URLS
DEVICE_REFRESH_URL = os.environ.get("DEVICE_REFRESH_URL", "")
THEME_CSS = os.environ.get("THEME_CSS", "")
TERMINE = os.environ.get("TERMINE", "")
# Regel: "4." jeden 4., "4.AT" 4. Arbeitstag, "25.12." jedes Jahr, "24.12.2026" (oder .26) einmal;
# dann optional "09:00" oder "09:00-09:30", optional eine Farbe "#e53935", dann der Titel
REGEL = re.compile(r"(\d\d?\.(?:AT|\d\d?(?:\.(?:\d{4}|\d\d)?)?)?)\s+(?:(\d\d?):(\d\d)(?:-(\d\d?):(\d\d))?\s+)?"
                   r"(?:(#[0-9a-f]{6})\s+)?(.+)", re.I)
FARBEN = {"#4a90d9": "Blau", "#43a047": "Grün", "#f9a825": "Gelb", "#ef6c00": "Orange", "#e53935": "Rot", "#8e24aa": "Lila"}
CACHE_SECONDS = 300
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]
TAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# ---------- Arbeitstage und Feiertage ----------
# Arbeitstage (AT) werden pro Monat gezählt: der erste Mo–Fr, der kein Feiertag ist, ist AT 1.
# Urlaub zählt als AT, Feiertage nicht. Steht im Kalender klein neben der Tageszahl, an Feiertagen der Name.
#
# Feiertage NRW. Ändern: Zeile löschen, auskommentieren (#) oder ergänzen, danach Server neu starten.
FEIERTAGE_FEST = {  # (Monat, Tag): Name
    (1, 1): "Neujahr",
    (5, 1): "Tag der Arbeit",
    (10, 3): "Tag der Deutschen Einheit",
    (11, 1): "Allerheiligen",
    (12, 25): "1. Weihnachtstag",
    (12, 26): "2. Weihnachtstag",
    # (12, 24): "Heiligabend",  # gesetzlich ein Arbeitstag; # entfernen, wenn er nicht als AT zählen soll
    # (12, 31): "Silvester",    # ebenso
}
FEIERTAGE_OSTERN = {  # Tage nach Ostersonntag: Name (Ostern rechnet dateutil aus)
    -2: "Karfreitag",
    1: "Ostermontag",
    39: "Christi Himmelfahrt",
    50: "Pfingstmontag",
    60: "Fronleichnam",
}

_lock = threading.Lock()
_cals = {}       # url -> letzter gut geparster Kalender
_failed = set()  # urls, deren letzter Abruf schiefging
_fetched = 0.0


def calendars():
    global _fetched
    with _lock:
        if time.time() - _fetched > CACHE_SECONDS:
            _fetched = time.time()
            for i, url in enumerate(ICS_URLS):
                try:
                    with urllib.request.urlopen(url, timeout=20) as r:
                        _cals[url] = icalendar.Calendar.from_ical(r.read())
                    _failed.discard(url)
                except Exception as e:  # alter Stand bleibt; URL nicht loggen, sie ist ein Secret
                    _failed.add(url)
                    print(f"Kalender {i + 1}: {e}", flush=True)
        off = {ICS_URLS[i] for i in hidden}  # ausgeblendete werden weiter abgerufen, sind beim Einblenden gleich da
        return [c for url, c in _cals.items() if url not in off]


def cal_name(i):
    """Name von Kalender i im Menü: aus ICS_NAMES, sonst X-WR-CALNAME aus der Datei, sonst "Kalender n"."""
    if i < len(ICS_NAMES):
        return ICS_NAMES[i]
    return str(_cals.get(ICS_URLS[i], {}).get("X-WR-CALNAME", "")) or f"Kalender {i + 1}"


def local(x):
    if isinstance(x, datetime):
        return x.astimezone(TZ) if x.tzinfo else x.replace(tzinfo=TZ)
    return x


def zeit_text(s, e):
    """Uhrzeit eines Termins je nach Schalter zeit: "09:30", "09:30–09:45" oder "09:30 · 15 min"."""
    if zeit == "bis":
        return f"{s:%H:%M}–{e:%H:%M}"
    m = int((e - s).total_seconds() // 60)
    if zeit == "dauer" and m:
        return f"{s:%H:%M} · " + (f"{m} min" if m < 60 else f"{m // 60}:{m % 60:02d} h".replace(":00 h", " h"))
    return f"{s:%H:%M}"


def occurrences(first, last):
    """(Start, Ende, ganztägig, Titel, Kalendername) aller nicht abgesagten Termine, die first..last berühren."""
    for c in calendars():
        name = str(c.get("X-WR-CALNAME", ""))  # "Teams" bei teams.ics, färbt die Termine auf der Webseite
        for ev in recurring_ical_events.of(c).between(first, last + timedelta(days=1)):
            if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                continue
            s, e = local(ev.start), local(ev.end)
            allday = not isinstance(s, datetime) or e - s >= timedelta(hours=24)  # mehrtägig mit Uhrzeit (Urlaub aus Outlook) wie ganztägig
            yield s, e, allday, str(ev.get("SUMMARY", "")).strip(), name
    for m in regeln():
        tag, h1, m1, h2, m2, farbe, title = m.groups()
        farbe = (farbe or "").lower()  # steht statt des Kalendernamens; "#…" färbt den Termin auf der Webseite
        for d in regel_tage(tag, first, last):
            if h1 is None:  # ohne Uhrzeit: ganztägig
                yield d, d + timedelta(days=1), True, title, farbe
            else:
                try:
                    s = datetime(d.year, d.month, d.day, int(h1), int(m1), tzinfo=TZ)
                    e = s.replace(hour=int(h2), minute=int(m2)) if h2 else s + timedelta(minutes=30)
                except ValueError:  # z. B. 25:00 von Hand eingetragen
                    continue
                yield s, e, False, title, farbe


def regeln():
    """Passende Zeilen der Datei TERMINE, bei jedem Abruf frisch gelesen; # am Zeilenanfang und Unpassendes fällt weg."""
    try:
        lines = open(TERMINE, encoding="utf-8").read().splitlines() if TERMINE else []
    except OSError:
        return []
    return [m for line in lines if not line.lstrip().startswith("#") and (m := REGEL.fullmatch(line.strip()))]


def termin_zeile(f):
    """Felder aus dem Formular -> Regelzeile wie in der Datei TERMINE, None wenn etwas fehlt oder nicht passt."""
    art, titel, von, bis = f.get("art"), " ".join(f.get("titel", "").split()), f.get("von", ""), f.get("bis", "")
    farbe = f.get("farbe", "") if f.get("farbe") in FARBEN else ""
    try:  # split: Titel ohne Zeilenumbrüche, sonst ließen sich weitere Zeilen einschleusen
        if art in ("einmal", "jahr"):
            d = date.fromisoformat(f.get("datum", ""))
            tag = f"{d.day}.{d.month}." + (str(d.year) if art == "einmal" else "")
        elif art in ("monat", "at") and 0 < int(f.get("nr", "")) <= 31:
            tag = f"{int(f['nr'])}." + "AT" * (art == "at")
        else:
            return None
        for t in (von, bis):
            if t:
                datetime.strptime(t, "%H:%M")  # wirft bei 25:00
    except ValueError:
        return None
    line = " ".join(x for x in (tag, f"{von}-{bis}" if von and bis else von, farbe, titel) if x)
    return line if titel and REGEL.fullmatch(line) else None


def termine_schreiben(neu=None, weg=None):
    """Zeile neu anhängen bzw. die erste Zeile weg löschen; Kommentare bleiben, Datei wird ganz ersetzt (nie halb)."""
    with _lock:
        try:
            lines = open(TERMINE, encoding="utf-8").read().splitlines()
        except FileNotFoundError:
            lines = []
        i = next((j for j, y in enumerate(lines) if y.strip() == weg), None)
        if i is not None:
            del lines[i]
        if neu:
            lines.append(neu)
        with open(TERMINE + ".tmp", "w", encoding="utf-8") as f:
            f.write("".join(x + "\n" for x in lines))
        os.replace(TERMINE + ".tmp", TERMINE)


def termine_stand():
    """Änderungszeit der Datei TERMINE für /state, damit alle Anzeigen nach dem Speichern neu laden."""
    try:
        return os.stat(TERMINE).st_mtime_ns
    except OSError:
        return 0


def regel_tage(tag, first, last):
    """Tage der Regel tag ("4.", "4.AT", "25.12.", "24.12.2026") zwischen first und last; fehlt der Tag, fällt er aus."""
    t = [x for x in tag.upper().split(".") if x]
    n = int(t[0])
    if len(t) > 1 and t[1] != "AT":  # Datum: mit Jahr einmal, sonst jedes Jahr
        out = []
        for y in [int(t[2]) + 2000 * (len(t[2]) == 2)] if len(t) == 3 else range(first.year, last.year + 1):
            try:
                out.append(date(y, int(t[1]), n))
            except ValueError:  # 29.2. außerhalb von Schaltjahren, 31.4.
                pass
        return [d for d in out if first <= d <= last]
    out = []
    y, mo = first.year, first.month
    while date(y, mo, 1) <= last:
        tage = [date(y, mo, i) for i in range(1, calendar.monthrange(y, mo)[1] + 1)]
        tage = [d for d in tage if not frei(d)] if len(t) > 1 else tage  # "AT": nur Arbeitstage zählen
        if 0 < n <= len(tage) and first <= tage[n - 1] <= last:
            out.append(tage[n - 1])
        y, mo = (y, mo + 1) if mo < 12 else (y + 1, 1)
    return out


def events(first, last):
    """{Tag: [(zeitgebunden, "HH:MM", Titel, Kalendername), ...]} für first..last inklusive, ganztägige zuerst."""
    days = {}
    for s, e, allday, title, name in occurrences(first, last):
        timed = isinstance(s, datetime)
        d0 = s.date() if timed else s
        d1 = max(d0, (e - timedelta(microseconds=1)).date() if timed else e - timedelta(days=1))
        d = max(d0, first)
        while d <= min(d1, last):
            t = "" if allday or d != d0 else zeit_text(s, e)
            days.setdefault(d, []).append((not allday, t, title, name))
            d += timedelta(days=1)
    for items in days.values():
        items.sort()
    return days


def next_text(now, n=1):
    """n-ter nächster Termin mit Uhrzeit (läuft gerade oder kommt in 7 Tagen) in drei kurzen Zeilen für eine 72-px-Taste."""
    evs = sorted((s, e, t) for s, e, allday, t, _ in occurrences(now.date(), now.date() + timedelta(days=7))
                 if not allday and e > now)
    if not 0 < n <= len(evs):
        return "keine\nTermine" if n == 1 else ""  # weitere Tasten bleiben leer
    s, e, t = evs[n - 1]
    when = f"bis {e:%H:%M}" if s <= now else f"{s:%H:%M}" if s.date() == now.date() else f"{TAGE[s.weekday()]} {s:%H:%M}"
    return "\n".join([when] + textwrap.wrap(t, 10, max_lines=2, placeholder="…"))


def blocks(days):
    """Für das Raster: {Tag: [(von, bis, Uhrzeit, Titel, Kalendername)]} der Termine mit Uhrzeit, von/bis in Minuten
    ab Mitternacht, am Tagesrand gekappt (22–2 Uhr steht an beiden Tagen). Mindestens 15 min hoch, damit lesbar."""
    out = {}
    for s, e, allday, title, name in occurrences(days[0], days[-1]):
        if allday:
            continue
        for day in days:
            a = datetime.combine(day, datetime.min.time(), TZ)
            lo, hi = max(s, a), min(max(e, s + timedelta(minutes=15)), a + timedelta(days=1))
            if lo < hi:
                out.setdefault(day, []).append(((lo - a) // timedelta(minutes=1), (hi - a) // timedelta(minutes=1),
                                                zeit_text(s, e) if lo == s else "", title, name))
    return out


def columns(items):
    """Wie Outlook: überlappende Termine nebeneinander. [(Termin, Spalte, Spalten der Überlappungsgruppe)]"""
    out, group, ends, group_end = [], [], [], 0
    for b in sorted(items, key=lambda b: (b[0], -b[1])):
        if b[0] >= group_end:  # überlappt nichts davor: vorige Gruppe abschließen
            out += [(g, c, len(ends)) for g, c in group]
            group, ends = [], []
        col = next((i for i, end in enumerate(ends) if end <= b[0]), len(ends))  # erste freie Spalte
        ends[col:col + 1] = [b[1]]
        group.append((b, col))
        group_end = max(group_end, b[1])
    return out + [(g, c, len(ends)) for g, c in group]


def stand():
    return datetime.now(TZ).strftime("Stand %H:%M") + (" · Kalender-Fehler!" if _failed else "")


@functools.cache
def feiertage(year):
    ostern = easter(year)
    return ({date(year, m, d): name for (m, d), name in FEIERTAGE_FEST.items()}
            | {ostern + timedelta(days=k): name for k, name in FEIERTAGE_OSTERN.items()})


def frei(day):
    return day.weekday() >= 5 or day in feiertage(day.year)


def tag_label(day):
    """Feiertagsname, sonst "AT <n>" an Arbeitstagen (n = Arbeitstag im Monat), am Wochenende leer."""
    if day in feiertage(day.year):
        return feiertage(day.year)[day]
    if frei(day):
        return ""
    return f"AT {sum(not frei(day.replace(day=i)) for i in range(1, day.day + 1))}"


def month_data(shown):
    n = 7 if weekend else 5  # ohne Wochenende fallen Zeilen ohne Werktag dieses Monats weg
    weeks = [w[:n] for w in calendar.Calendar().monthdatescalendar(shown.year, shown.month)
             if any(d.month == shown.month for d in w[:n])]
    return f"{MONATE[shown.month - 1]} {shown.year}", weeks, events(weeks[0][0], weeks[-1][-1])


def week_data(shown):
    n = 7 if weekend else 5
    monday = shown - timedelta(days=shown.weekday())
    last = monday + timedelta(days=n - 1)
    title = (f"KW {shown.isocalendar().week} · {monday.day}. {MONATE[monday.month - 1]}"
             f" – {last.day}. {MONATE[last.month - 1]} {last.year}")
    return title, [monday + timedelta(days=i) for i in range(n)], events(monday, last)


# ---------- PNG für E-Ink ----------

@functools.cache
def font(size, bold=False):
    return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)


def fit(text, f, width):
    if f.getlength(text) <= width:
        return text
    while text and f.getlength(text + "…") > width:
        text = text[:-1]
    return text + "…"


def wrap(text, f, width, max_lines):
    lines = []
    for word in text.split():
        if lines and f.getlength(f"{lines[-1]} {word}") <= width:
            lines[-1] += f" {word}"
        else:
            lines.append(word)
    lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return [fit(line, f, width) for line in lines if line]


def draw_items(d, items, x, y, w, bottom, size, split_time, fill=0):
    """Termine untereinander; was nicht mehr passt, wird zu '+N weitere'.
    split_time: Uhrzeit fett in eigener Zeile, Titel umbrochen (Wochenansicht)."""
    lh = round(size * 1.3)
    for i, (timed, t, title, _) in enumerate(items):
        if split_time:
            lines = [(t, True)] * bool(t) + [(line, False) for line in wrap(title, font(size), w, 3)]
        else:
            lines = [(fit(f"{t} {title}".strip(), font(size), w), False)]
        more = 1 if i < len(items) - 1 else 0
        if y + lh * (len(lines) + more) > bottom:
            d.text((x, y), f"+{len(items) - i} weitere", font=font(size), fill=110)
            return
        if not timed:
            d.rectangle((x - 4, y - 2, x + w + 4, y + lh * len(lines) - 4), fill=210)
        for line, bold in lines:
            d.text((x, y), line, font=font(size, bold), fill=fill)
            y += lh
        y += 8 if split_time else 0


def canvas(title):
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    d.text((40, 60), title, font=font(64, True), fill=0, anchor="lm")
    d.text((W - 40, 60), stand(), font=font(28), fill=90, anchor="rm")
    return img, d


def circle(d, cx, cy, r):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=0, width=5)


def render_month(shown, today):
    title, weeks, evs = month_data(shown)
    img, d = canvas(title)
    top, cw = 170, W / len(weeks[0])
    ch = (H - top) / len(weeks)
    for c, name in enumerate(TAGE[:len(weeks[0])]):
        d.text((c * cw + cw / 2, 145), name, font=font(30, True), fill=0, anchor="mm")
    for r, week in enumerate(weeks):
        for c, day in enumerate(week):
            x, y = c * cw, top + r * ch
            d.rectangle((x, y, x + cw, y + ch), fill=235 if frei(day) else 255, outline=0, width=2)
            fill = 0 if day.month == shown.month else 150
            d.text((x + 38, y + 36), str(day.day), font=font(36, True), fill=fill, anchor="mm")
            if c == 0:
                d.text((x + 78, y + 38), f"KW {day.isocalendar().week}", font=font(22), fill=max(fill, 120), anchor="lm")
            d.text((x + cw - 14, y + 36), fit(tag_label(day), font(24), cw - (180 if c == 0 else 90)), font=font(24),
                   fill=max(fill, 90), anchor="rm")
            if day == today:
                circle(d, x + 38, y + 36, 30)
            draw_items(d, evs.get(day, []), x + 12, y + 74, cw - 24, y + ch - 6, 22, False, fill)
    return img


def render_week(shown, today):
    title, days, evs = week_data(shown)
    img, d = canvas(title)
    g = 44 if raster else 0  # Raster: links schmale Spalte mit den Stunden
    top, cw = 130, (W - g) / len(days)
    k = min(3, max([sum(not i[0] for i in evs.get(day, [])) for day in days]))  # Zeilen für Ganztägige über dem Raster
    y0 = top + 90 + (k * 29 + 12 if k else 0)
    if raster:
        d.rectangle((0, top, g, H - 1), outline=0, width=2)
    for c, day in enumerate(days):
        x = g + c * cw
        d.rectangle((x, top, x + cw, H - 1), fill=235 if frei(day) else 255, outline=0, width=2)
        d.line((x, top + 90, x + cw, top + 90), fill=0, width=2)
        d.text((x + 24, top + 45), TAGE[c], font=font(36, True), fill=0, anchor="lm")
        d.text((x + cw - 92, top + 45), fit(tag_label(day), font(26), cw - 184), font=font(26), fill=90, anchor="rm")
        d.text((x + cw - 56, top + 45), str(day.day), font=font(36, True), fill=0, anchor="mm")
        if day == today:
            circle(d, x + cw - 56, top + 45, 32)
        if raster:
            draw_items(d, [i for i in evs.get(day, []) if not i[0]], x + 14, top + 98, cw - 28, y0, 22, False)
        else:
            draw_items(d, evs.get(day, []), x + 14, top + 108, cw - 28, H - 10, 26, True)
    if raster:
        draw_grid(d, days, today, g, cw, y0)
    return img


def draw_grid(d, days, today, g, cw, y0):
    """Outlook-Raster: Stunden senkrecht, Block so hoch wie die Dauer. 7–18 Uhr, erweitert, wenn Termine außerhalb liegen."""
    bl = blocks(days)
    mins = [m for items in bl.values() for b in items for m in b[:2]]
    h0, h1 = min([7 * 60, *mins]) // 60, -(-max([18 * 60, *mins]) // 60)
    ppm = (H - 1 - y0) / ((h1 - h0) * 60)

    def y(m):
        return y0 + (m - h0 * 60) * ppm

    d.line((0, y0, W, y0), fill=0, width=2)
    for h in range(h0 + 1, h1):
        d.line((g, y(h * 60), W, y(h * 60)), fill=190, width=1)
        d.text((g - 6, y(h * 60)), f"{h:02d}", font=font(20), fill=90, anchor="rm")
    for c, day in enumerate(days):
        x = g + c * cw
        for (a, b, t, title, _), col, n in columns(bl.get(day, [])):
            bw = (cw - 8) / n
            bx, top, bottom = x + 4 + col * bw, y(a), y(b)
            d.rectangle((bx, top + 1, bx + bw - 2, bottom - 1), fill=220, outline=0)
            rows = int((bottom - top - 4) // 24)
            if t and rows >= 2:  # Uhrzeit fett, darunter der Titel
                lines = [(t, True)] + [(line, False) for line in wrap(title, font(20), bw - 12, rows - 1)]
            else:
                lines = [(fit(f"{t} {title}".strip(), font(20), bw - 12), False)]
            for i, (line, bold) in enumerate(lines):
                d.text((bx + 6, top + 3 + i * 24), line, font=font(20, bold), fill=0)
        if day == today:  # Jetzt-Linie
            now = datetime.now(TZ)
            ny = y(now.hour * 60 + now.minute)
            if y0 < ny < H:
                d.line((x, ny, x + cw, ny), fill=0, width=4)
                d.ellipse((x - 8, ny - 8, x + 8, ny + 8), fill=0)


# ---------- Webseite ----------

CSS = """
*{box-sizing:border-box;margin:0}
:root{--bg:#fff;--fg:#000;--line:#000;--muted:#555;--faint:#888;--we:#ececec;--pink:#fdebf2;--red:#d32f2f;--allday:#d2d2d2;
  --teams:#e4eefb;--teams2:#bcd4f3;--teams-edge:#5b8fd6;--urlaub:#f9d3e3;--urlaub-edge:#d6699a;--grid:#ccc;--blk:#eee;--blk-edge:#888}
/* Dunkel: neutrales Grau; eigene Farben per THEME_CSS */
.dark{--bg:#1e1e1e;--fg:#e6e6e6;--line:#444;--muted:#b3b3b3;--faint:#888;--we:#161616;--pink:#3b2a31;--red:#f28b82;--allday:#3a3a3a;
  --teams:#263445;--teams2:#34506e;--teams-edge:#7fb0e8;--urlaub:#5c2f44;--urlaub-edge:#e57fae;--grid:#383838;--blk:#2c2c2c;--blk-edge:#888}
html{background:var(--bg)}
body{font:16px/1.25 system-ui,sans-serif;background:var(--bg);color:var(--fg);height:100dvh;display:flex;flex-direction:column}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:.3em 1em;padding:.4em .8em}
h1{font-size:clamp(1.3rem,3.5vw,2.8rem)}
.titel{display:flex;align-items:center;gap:.3em}
.arrow{font-size:clamp(1.3rem,3.5vw,2.8rem);font-weight:700;color:var(--muted);text-decoration:none;padding:0 .2em}
.heute{border:2px solid var(--faint);border-radius:.4em;padding:.1em .5em;color:var(--fg);text-decoration:none;margin-right:.3em}
nav{display:flex;flex-wrap:wrap;gap:.2em 1em;align-items:baseline;color:var(--muted)}
nav>*{white-space:nowrap}
nav a{color:inherit;font-size:1.15em}
nav a.on{color:var(--fg);font-weight:700;text-decoration:none}
nav details{position:relative}
nav summary{cursor:pointer;font-size:1.15em}
nav summary.aus{color:var(--red)}  /* ein Kalender ist ausgeblendet; nur Farbe, damit die Leiste nicht springt */
/* Dialog „+ Termin“ */
dialog{margin:auto;width:min(26em,calc(100vw - 2em));padding:1.2em 1.4em;background:var(--bg);color:var(--fg);
  border:1px solid var(--line);border-radius:.8em;box-shadow:0 1em 3em #0006}
dialog::backdrop{background:#0008;backdrop-filter:blur(2px)}
dialog,dialog ul{scrollbar-width:thin;scrollbar-color:var(--faint) transparent}
dialog h3{font-size:1.25em;margin-bottom:.8em}
dialog form.neu{display:grid;gap:.75em}
dialog label{display:grid;gap:.25em;font-size:.85em;color:var(--muted)}dialog label:has(:disabled){display:none}
dialog input,dialog select{font:inherit;font-size:1rem;color:var(--fg);background:var(--we);border:1px solid var(--line);
  border-radius:.4em;padding:.45em .6em;width:100%}
dialog input:focus,dialog select:focus{outline:2px solid var(--teams-edge);outline-offset:-1px}
dialog .zeit{display:grid;grid-template-columns:1fr 1fr;gap:.75em}
dialog small{color:var(--faint);font-size:.8em;margin-top:-.35em}
dialog .primary{font:inherit;font-weight:700;padding:.55em;border:0;border-radius:.4em;background:var(--teams-edge);color:var(--bg);cursor:pointer}
dialog .x{position:absolute;top:.7em;right:.8em}
dialog .x button,dialog .weg{font:inherit;background:none;border:0;color:var(--faint);cursor:pointer;font-size:1.1em}
dialog .x button:hover,dialog .weg:hover{color:var(--red)}
dialog h4{margin:1.3em 0 .3em;font-size:.75em;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
dialog ul{list-style:none;padding:0;max-height:35vh;overflow-y:auto}
dialog li{display:flex;align-items:baseline;gap:.7em;padding:.4em 0;border-top:1px solid var(--line)}
dialog .farben{border:0;padding:0;display:flex;flex-wrap:wrap;gap:.5em}
dialog .farben legend{font-size:.85em;color:var(--muted);margin-bottom:.3em;width:100%}
dialog .farben label{display:block}dialog .farben input{position:absolute;opacity:0;width:auto}
dialog .farben i{display:block;width:1.7em;height:1.7em;border-radius:50%;background:var(--c,transparent);
  border:2px dashed var(--faint);cursor:pointer}dialog .farben i[style]{border:2px solid transparent}
dialog .farben input:checked+i{outline:2px solid var(--fg);outline-offset:2px}
dialog .farben input:focus-visible+i{outline:2px solid var(--teams-edge);outline-offset:2px}
dialog .punkt{display:inline-block;width:.7em;height:.7em;border-radius:50%;background:var(--c);margin-right:.4em}
dialog .wann{font-size:.8em;color:var(--muted);min-width:7.5em}dialog .was{flex:1}dialog li form{display:contents}
.dark{color-scheme:dark}
nav details>div{position:absolute;left:0;z-index:9;display:flex;flex-direction:column;gap:.3em;margin-top:.3em;padding:.5em .8em;
  background:var(--bg);border:2px solid var(--faint);border-radius:.4em}
main{flex:1;min-height:0;display:grid;grid-template-columns:repeat(var(--n,7),minmax(0,1fr));border-top:2px solid var(--line)}
.wd{text-align:center;font-weight:700;padding:.2em;border-bottom:2px solid var(--line)}
.day{border-right:2px solid var(--line);border-bottom:2px solid var(--line);padding:.25em .35em 0;background:var(--bg);overflow-y:auto;scrollbar-width:none}
.day::-webkit-scrollbar{display:none}
.end{border-right:0}
.we{background:var(--we)}
.feiertag,.urlaubstag{background:var(--pink)}
.other{color:var(--faint)}
.num{display:inline-grid;place-items:center;width:1.9em;height:1.9em;font-weight:700;font-size:clamp(.9rem,1.8vw,1.6rem)}
.today .num{border:.14em solid var(--red);border-radius:50%}
.at{font-size:clamp(.6rem,1vw,1rem);font-weight:400;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kwm{font-size:clamp(.6rem,1vw,.95rem);color:var(--faint);margin-left:.2em}
.month .at{float:right;max-width:65%;margin-top:.5em}
.other .at{color:inherit}
.week .at{margin:0 .6em 0 auto;min-width:0}
.ev{font-size:clamp(.6rem,1vw,1rem);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 .25em;margin:.1em 0}
.allday{background:var(--allday)}
.teams{background:var(--teams)}
.allday.teams{background:var(--teams2)}
.ev.urlaub{background:var(--urlaub)}
.more{font-size:clamp(.6rem,1vw,1rem);color:var(--faint);padding:0 .25em;position:sticky;bottom:0;background:inherit;cursor:pointer}
.week .head{display:flex;justify-content:space-between;align-items:center;font-weight:700;font-size:clamp(.9rem,1.8vw,1.6rem);border-bottom:2px solid var(--line);margin:0 -.35em .4em;padding:0 .35em}
.week .head .num{font-size:inherit}
.week .ev{white-space:normal;margin-bottom:.5em;font-size:clamp(.75rem,1.2vw,1.15rem)}
.week .ev b{display:block}
main.raster{grid-template-columns:3.2em repeat(var(--n),minmax(0,1fr));grid-template-rows:auto minmax(0,1fr)}
.corner{border-right:2px solid var(--line);border-bottom:2px solid var(--line)}
.scroll{grid-column:1/-1;overflow-y:auto;scrollbar-width:none;container-type:size}
.scroll::-webkit-scrollbar{display:none}
.grid{display:grid;grid-template-columns:3.2em repeat(var(--n),minmax(0,1fr));height:calc(2400cqh / 11)}
.hours{border-right:2px solid var(--line)}
.hours div{height:calc(100% / 24);font-size:.75em;color:var(--faint);text-align:right;padding-right:.3em;transform:translateY(-.6em)}
.col{position:relative;border-right:2px solid var(--line);background-image:linear-gradient(var(--grid) 1px,transparent 1px);background-size:100% calc(100% / 24)}
.blk{position:absolute;overflow:hidden;font-size:clamp(.6rem,1vw,.95rem);line-height:1.15;padding:0 .3em;background:var(--blk);border:1px solid var(--bg);border-left:3px solid var(--blk-edge);border-radius:3px}
.blk.teams{background:var(--teams);border-left-color:var(--teams-edge)}
.blk.urlaub{background:var(--urlaub);border-left-color:var(--urlaub-edge)}
/* Termine aus TERMINE mit Farbe (--c): getönt, im Raster mit farbigem Rand */
.ev[data-farbe],.blk[data-farbe]{background:color-mix(in srgb,var(--c) 35%,var(--bg))}.blk[data-farbe]{border-left-color:var(--c)}
.now{position:absolute;left:0;right:0;border-top:2px solid var(--red);z-index:1}
.now::before{content:"";position:absolute;left:-5px;top:-6px;width:10px;height:10px;border-radius:50%;background:var(--red)}
.kurz,.agenda{display:none}
@media (max-width:700px){.week{grid-template-columns:1fr;overflow-y:auto}.week .day{border-right:0}.month .at{float:none;display:block;margin:0}
  /* Handy: kompakter Kopf (kurzer Wochentitel, kleinere Leiste, ohne "Stand"), im Raster-Kopf kein AT */
  .lang,.stand,.raster .head .at{display:none}.kurz{display:inline}
  header{padding:.3em .6em}h1,.arrow{font-size:1.2rem}nav{gap:.1em .8em}nav a,nav summary{font-size:.95em}
  /* Handy-Monat: kompaktes Raster mit Strichen je Termin, darunter die angetippte Woche als Liste (JS baut .agenda) */
  .month{flex:none;grid-template-rows:auto!important}.month .at,.month .kwm{display:none}.month .num{display:grid}.month .day{padding-bottom:.2em;font-size:0}
  .month .ev{display:inline-block;width:.8rem;height:.3rem;margin:.1rem;padding:0;font-size:0;background:var(--muted)}
  .month .ev.teams{background:var(--teams-edge)}.month .ev.urlaub{background:var(--urlaub-edge)}.month .ev[data-farbe]{background:var(--c)}
  .month .day:not(.sel){opacity:.45}
  .agenda{display:block;flex:1;min-height:0;overflow-y:auto;scrollbar-width:none;padding:0 .6em .6em}
  .agenda h3{font-size:1rem;margin:.7em 0 .2em;border-bottom:1px solid var(--line)}.agenda h3.today{color:var(--red)}.agenda h3.kw{border:0;color:var(--muted)}
  .agenda h3 span{font-weight:400;font-size:.85em;color:var(--muted)}.agenda .ev{white-space:normal;font-size:1rem;padding:.15em .4em}}
"""

# Lädt neu, wenn Ansicht oder Datum wechselt (Stream Deck) und alle 5 min für neue Termine.
# Ist der Server weg, bleibt die alte Anzeige stehen statt einer Fehlerseite.
JS = """
for (const s of document.querySelectorAll(".scroll")) s.scrollTop = s.clientHeight * 7 / 11;  // Raster: 7–18 Uhr sichtbar
for (const day of document.querySelectorAll(".day")) {  // Zelle scrollt ohne Leiste; "+N weitere" zählt, was unten verdeckt ist
  if (day.scrollHeight <= day.clientHeight) continue;
  const more = day.appendChild(Object.assign(document.createElement("div"), {className: "more"}));
  const update = () => {
    const bottom = day.getBoundingClientRect().bottom - more.offsetHeight;
    const n = [...day.querySelectorAll(".ev")].filter(e => e.getBoundingClientRect().bottom > bottom + 1).length;
    more.textContent = `+${n} weitere`;
    more.style.visibility = n ? "" : "hidden";  // nicht hidden: Platz bleibt, sonst springt der Inhalt am Ende
  };
  day.addEventListener("scroll", update, {passive: true});
  more.onclick = () => day.scrollBy({top: day.clientHeight * 0.7, behavior: "smooth"});
  update();
}
const m = document.querySelector("main.month");  // Handy: angetippte Woche unter dem Monat als Liste
if (m) {
  const n = +m.style.getPropertyValue("--n"), cells = [...m.querySelectorAll(".day")];
  const ag = Object.assign(document.createElement("div"), {className: "agenda"});
  m.after(ag);
  const show = w => {
    cells.forEach((c, i) => c.classList.toggle("sel", Math.floor(i / n) === w));
    ag.innerHTML = `<h3 class=kw>${cells[w * n].querySelector(".kwm").textContent}</h3>` + cells.slice(w * n, w * n + n).map(c =>
      `<h3${c.classList.contains("today") ? " class=today" : ""}>${c.dataset.d} <span>${c.querySelector(".at").textContent}</span></h3>`
      + [...c.querySelectorAll(".ev")].map(e => e.outerHTML).join("")).join("");
  };
  cells.forEach((c, i) => c.addEventListener("click", () => show(Math.floor(i / n))));
  show(Math.max(0, Math.floor(cells.findIndex(c => c.classList.contains("today")) / n)));
}
const art = document.querySelector("[name=art]");  // Formular „+ Termin“: Datum oder n, das andere aus
if (art) {
  const pick = () => {
    const datum = ["einmal", "jahr"].includes(art.value);
    tdlg.querySelector("[name=datum]").disabled = !datum;
    tdlg.querySelector("[name=nr]").disabled = datum;
    tdlg.querySelector(".nr").firstChild.textContent = art.value === "at" ? "Arbeitstag im Monat (1 = erster)" : "Tag im Monat";
  };
  art.onchange = pick;
  pick();
  if (location.search === "?termine") tdlg.showModal();
}
if (location.search) history.replaceState(null, "", "/");  // /?kal hält das Kalender-Menü nur nach dem Klick offen
const loaded = Date.now();
setInterval(async () => {
  try {
    const s = await fetch("/state", {cache: "no-store"});
    if (!s.ok || (await s.text() === state && Date.now() - loaded < 300000)) return;
    if ((await fetch("/", {cache: "no-store"})).ok) location.reload();
  } catch (e) {}
}, 3000);
"""


def urlaub(title):
    return "urlaub" in title.lower()


def html_items(items):
    out = ""
    for timed, t, title, cal in items:
        cls = ["ev"] + ["allday"] * (not timed) + ["teams"] * (cal == "Teams") + ["urlaub"] * urlaub(title)
        out += f'<div class="{" ".join(cls)}"{farbe_style(cal)}>{f"<b>{t}</b> " if t else ""}{escape(title)}</div>'
    return out


def farbe_style(cal):
    """Termin aus TERMINE mit Farbe: Klasse und --c für das CSS; cal ist dann "#rrggbb" statt eines Kalendernamens."""
    return f' data-farbe style="--c:{cal}"' if cal.startswith("#") else ""


def day_class(day, today, c, n, items, other=False):
    """items: Termine des Tages; ein Urlaubstermin färbt die ganze Zelle pink wie ein Feiertag."""
    return " ".join(["day"] + ["we"] * frei(day) + ["feiertag"] * (day in feiertage(day.year))
                    + ["urlaubstag"] * any(urlaub(title) for _, _, title, _ in items)
                    + ["today"] * (day == today) + ["other"] * other
                    + ["end"] * (c == n - 1))


def html_month(shown, today):
    title, weeks, evs = month_data(shown)
    n = len(weeks[0])
    cells = [f'<div class="wd">{t}</div>' for t in TAGE[:n]]
    cells += [f'<div class="{day_class(day, today, c, n, evs.get(day, []), day.month != shown.month)}"'
              f' data-d="{TAGE[c]} {day.day}.{day.month}.">'  # Überschrift in der Handy-Liste (JS)
              f'<span class="num">{day.day}</span>{f"<span class=kwm>KW {day.isocalendar().week}</span>" * (c == 0)}'
              f'<span class="at">{tag_label(day)}</span>{html_items(evs.get(day, []))}</div>'
              for week in weeks for c, day in enumerate(week)]
    return title, (f'<main class="month" style="--n:{n};grid-template-rows:auto repeat({len(weeks)},minmax(0,1fr))">'
                   f'{"".join(cells)}</main>')


def html_week(shown, today):
    title, days, evs = week_data(shown)
    n = len(days)
    a, b = days[0], days[-1]
    title = (f'<span class=lang>{title}</span>'
             f'<span class=kurz>KW {a.isocalendar().week} · {a.day}.{a.month}.–{b.day}.{b.month}.</span>')  # Handy
    cols = [f'<div class="{day_class(day, today, c, n, evs.get(day, []))}"><div class="head">{TAGE[c]}'
            f'<span class="at">{tag_label(day)}</span><span class="num">{day.day}</span></div>'
            f'{html_items([i for i in evs.get(day, []) if not (raster and i[0])])}</div>'  # Raster: oben nur Ganztägige
            for c, day in enumerate(days)]
    if not raster:
        return title, f'<main class="week" style="--n:{n}">{"".join(cols)}</main>'
    bl = blocks(days)
    hours = "".join(f"<div>{h:02d}:00</div>" if h else "<div></div>" for h in range(24))
    grid = ""
    for c, day in enumerate(days):
        inner = "".join(
            f'<div class="{" ".join(["blk"] + ["teams"] * (cal == "Teams") + ["urlaub"] * urlaub(t2))}"{" data-farbe" * cal.startswith("#")}'
            f' style="{f"--c:{cal};" * cal.startswith("#")}top:{a / 14.4:.3f}%;'
            f'height:{(b - a) / 14.4:.3f}%;left:{100 * k / m:.3f}%;width:{100 / m:.3f}%">{f"<b>{t}</b> " if t else ""}{escape(t2)}</div>'
            for (a, b, t, t2, cal), k, m in columns(bl.get(day, [])))  # 14.4 = 1440 min / 100 %
        if day == today:
            now = datetime.now(TZ)
            inner += f'<div class="now" style="top:{(now.hour * 60 + now.minute) / 14.4:.3f}%"></div>'
        grid += f'<div class="col{day_class(day, today, c, n, evs.get(day, []))[3:]}">{inner}</div>'  # [3:]: ohne "day"
    return title, (f'<main class="week raster" style="--n:{n}"><div class="corner"></div>{"".join(cols)}'
                   f'<div class="scroll"><div class="grid"><div class="hours">{hours}</div>{grid}</div></div></main>')


VIEWS = {"month": ("Monat", render_month, html_month), "week": ("Woche", render_week, html_week)}
START = int(time.time())  # ändert /state nach Neustart, Browser lädt dann neuen Code
view = "month"  # ponytail: view/weekend nur im RAM, nach Neustart wieder Monat mit Wochenende; persistieren falls das stört
weekend = True
offset = 0  # Pfeile: Monate bzw. Wochen ab heute; Ansichtswechsel setzt auf 0 zurück
ZEITEN = {"start": "Start", "bis": "von–bis", "dauer": "Dauer"}  # testweise, /zeit schaltet reihum
zeit = "start"
raster = True  # Woche als Outlook-Raster; /raster schaltet auf Liste um
dark = os.environ.get("DARK") == "1"  # Dunkelmodus der Webseite (CSS .dark, eigene Farben per THEME_CSS)
hidden = set()  # Nummern (ab 0) der ausgeblendeten Kalender aus ICS_URLS; /cal/<n> schaltet


def shown_date(today):
    """Angezeigtes Datum: im Monat der 1. des Monats heute + offset, in der Woche heute + offset Wochen."""
    if view == "month":
        m = today.month - 1 + offset
        return date(today.year + m // 12, m % 12 + 1, 1)
    return today + timedelta(weeks=offset)


def state(today):
    return (f"{view}|{weekend:d}|{offset}|{zeit}|{raster:d}|{dark:d}|{','.join(map(str, sorted(hidden)))}|{termine_stand()}"
            f"|{today}|{START}")


def theme_css():
    """Inhalt der Datei THEME_CSS, bei jedem Seitenaufruf frisch gelesen; ohne Datei bleibt das eingebaute CSS."""
    try:
        return open(THEME_CSS, encoding="utf-8").read() if THEME_CSS else ""
    except OSError:
        return ""


def page(today, kal=False):
    title, body = VIEWS[view][2](shown_date(today), today)
    nav = "".join(f'<a href="/view/{k}"{" class=on" if k == view else ""}>{label}</a>'
                  for k, (label, _, _) in VIEWS.items())
    nav += f'<a href="/weekend">{"☑" if weekend else "☐"} Wochenende</a><a href="/zeit">Zeit: {ZEITEN[zeit]}</a>'
    nav += f'<a href="/raster">{"☑" if raster else "☐"} Raster</a>' * (view == "week")
    if len(ICS_URLS) > 1:
        links = "".join(f'<a href="/cal/{i + 1}">{"☐" if i in hidden else "☑"} {escape(cal_name(i))}</a>'
                        for i in range(len(ICS_URLS)))
        nav += (f'<details{" open" * kal}><summary{" class=aus" * bool(hidden)}>Kalender</summary>'
                f'<div>{links}</div></details>')
    nav += f'<a href="/dark">{"☑" if dark else "☐"} Dunkel</a>'
    nav += '<a href="#" onclick="tdlg.showModal();return false">+ Termin</a>' * bool(TERMINE)
    return (f'<!doctype html><html lang="de"{" class=dark" * dark}><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kalender</title>'
            f'<style>{CSS}{theme_css()}</style></head><body>'
            f'<header><div class="titel"><a class="heute" href="/today">Heute</a>'  # links vom Titel, damit nichts springt
            f'<a class="arrow" href="/prev" aria-label="zurück">‹</a><a class="arrow" href="/next" aria-label="weiter">›</a>'
            f'<h1>{title}</h1></div><nav>{nav}<span{" class=stand" * (not _failed)}>{stand()}</span></nav></header>{body}'
            f'{termin_dialog() if TERMINE else ""}'
            f'<script>const state = "{state(today)}";{JS}</script></body></html>')


def regel_wann(tag):
    """Tag einer Regel lesbar: "4." -> "jeden 4.", "4.AT" -> "jeden 4. AT", "25.12." -> "jährlich 25.12.", sonst Datum."""
    t = [x for x in tag.upper().split(".") if x]
    if len(t) == 1:
        return f"jeden {t[0]}."
    if t[1] == "AT":
        return f"jeden {t[0]}. AT"
    return f"jährlich {t[0]}.{t[1]}." if len(t) == 2 else f"{t[0]}.{t[1]}.{t[2] if len(t[2]) == 4 else '20' + t[2]}"


def termin_dialog():
    """Formular „+ Termin“ und die gespeicherten Regeln mit ✕ zum Löschen."""
    liste = ""
    for m in regeln():
        tag, h1, m1, h2, m2, farbe, title = m.groups()
        zeit = f"<b>{h1}:{m1}{f'–{h2}:{m2}' if h2 else ''}</b> " if h1 else ""
        punkt = f'<i class=punkt style="--c:{farbe}"></i>' if farbe else ""
        liste += (f'<li><span class=wann>{regel_wann(tag)}</span><span class=was>{punkt}{zeit}{escape(title)}</span>'
                  f'<form method=post action=/termine/weg onsubmit="return confirm(\'Termin löschen?\')">'
                  f'<input type=hidden name=zeile value="{escape(m.string)}"><button class=weg aria-label=löschen>✕</button></form></li>')
    return ('<dialog id=tdlg><form method=dialog class=x><button aria-label=Schließen>✕</button></form><h3>Neuer Termin</h3>'
            '<form method=post action=/termine class=neu>'
            '<label>Titel<input name=titel required placeholder="z. B. Reisekosten abrechnen"></label>'
            '<label>Wiederholung<select name=art><option value=einmal>einmalig</option><option value=jahr>jedes Jahr</option>'
            '<option value=monat>jeden Monat am Tag …</option><option value=at>jeden Monat am Arbeitstag …</option></select></label>'
            '<label>Datum<input type=date name=datum required></label>'
            '<label class=nr>Tag im Monat<input type=number name=nr min=1 max=31 required placeholder="4"></label>'
            '<div class=zeit><label>von<input type=time name=von></label><label>bis<input type=time name=bis></label></div>'
            '<small>Ohne Uhrzeit ganztägig, ohne „bis“ 30 Minuten.</small>'
            '<fieldset class=farben><legend>Farbe</legend><label title=keine><input type=radio name=farbe value="" checked><i></i></label>'
            + "".join(f'<label title={n}><input type=radio name=farbe value="{c}"><i style="--c:{c}"></i></label>'
                      for c, n in FARBEN.items()) + '</fieldset>'
            '<button class=primary>Speichern</button></form>'
            + (f'<h4>Gespeichert</h4><ul>{liste}</ul>' if liste else "") + '</dialog>')


# ---------- HTTP ----------

def poke_device():
    try:
        urllib.request.urlopen(urllib.request.Request(DEVICE_REFRESH_URL, method="POST"), timeout=5).close()
    except Exception as e:
        print(f"Display nicht erreichbar: {e}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global view, weekend, offset, zeit, raster, dark, hidden
        path = self.path.split("?")[0].strip("/")
        today = datetime.now(TZ).date()
        if self.command == "POST" and TERMINE and path in ("termine", "termine/weg"):
            f = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()).items()}
            neu = termin_zeile(f) if path == "termine" else None
            if path == "termine" and not neu:
                return self.send_error(400, "Termin unvollständig")
            termine_schreiben(neu, f.get("zeile") if path == "termine/weg" else None)
            if DEVICE_REFRESH_URL:
                threading.Thread(target=poke_device, daemon=True).start()
            self.send_response(303)
            self.send_header("Location", "/?termine")  # Dialog bleibt offen, die Liste zeigt das Ergebnis
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "":
            self.reply(page(today, "kal" in self.path).encode(), "text/html; charset=utf-8")
        elif path.split("/")[0] == "termin" and (path[7:] or "1").isdigit():  # nicht /next, das blättert weiter
            self.reply(next_text(datetime.now(TZ), int(path[7:] or 1)).encode(), "text/plain; charset=utf-8")
        elif path == "state":
            self.reply(state(today).encode(), "text/plain")
        elif path == "image.png":
            buf = io.BytesIO()
            VIEWS[view][1](shown_date(today), today).save(buf, "PNG")
            self.reply(buf.getvalue(), "image/png")
        elif path in ("weekend", "prev", "next", "today", "zeit", "raster", "dark") or path.startswith("view/") and path[5:] in VIEWS \
                or path in [f"cal/{i + 1}" for i in range(len(ICS_URLS))]:
            if path == "weekend":
                weekend = not weekend
            elif path == "raster":
                raster = not raster
            elif path == "dark":
                dark = not dark
            elif path == "zeit":
                zeit = list(ZEITEN)[(list(ZEITEN).index(zeit) + 1) % len(ZEITEN)]
            elif path.startswith("cal/"):
                hidden ^= {int(path[4:]) - 1}
            elif path in ("prev", "next", "today"):
                offset = {"prev": offset - 1, "next": offset + 1, "today": 0}[path]
            else:
                view, offset = path[5:], 0
            if DEVICE_REFRESH_URL:
                threading.Thread(target=poke_device, daemon=True).start()
            if "text/html" in self.headers.get("Accept", ""):  # Klick auf der Webseite: zurück zur Ansicht
                self.send_response(303)
                self.send_header("Location", "/?kal" if path.startswith("cal/") else "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.reply(b"ok\n", "text/plain")
        else:
            self.send_error(404)

    do_POST = do_GET

    def reply(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if not self.path.startswith(("/state", "/termin")):  # Webseite alle 3 s, Stream Deck alle 15 s: nicht ins Log
            super().log_message(fmt, *args)


if __name__ == "__main__":
    ThreadingHTTPServer(("", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
