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
GET /state             Ansicht, Wochenende, Verschiebung, Zeitanzeige, Raster, Datum, Serverstart; fragt die Webseite alle 3 s ab

Env: ICS_URLS (Leerzeichen-getrennt), DEVICE_REFRESH_URL, TZ, PORT, DARK=1 (startet im Dunkelmodus),
     THEME_CSS (eigene CSS-Datei, kommt nach dem eingebauten CSS, z. B. eigene Farben für .dark)
"""
import calendar
import functools
import io
import os
import threading
import time
import urllib.request
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
DEVICE_REFRESH_URL = os.environ.get("DEVICE_REFRESH_URL", "")
THEME_CSS = os.environ.get("THEME_CSS", "")
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
        return list(_cals.values())


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
.now{position:absolute;left:0;right:0;border-top:2px solid var(--red);z-index:1}
.now::before{content:"";position:absolute;left:-5px;top:-6px;width:10px;height:10px;border-radius:50%;background:var(--red)}
@media (max-width:700px){.week{grid-template-columns:1fr;overflow-y:auto}.week .day{border-right:0}.month .at{float:none;display:block;margin:0}}
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
        out += f'<div class="{" ".join(cls)}">{f"<b>{t}</b> " if t else ""}{escape(title)}</div>'
    return out


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
    cells += [f'<div class="{day_class(day, today, c, n, evs.get(day, []), day.month != shown.month)}">'
              f'<span class="num">{day.day}</span>{f"<span class=kwm>KW {day.isocalendar().week}</span>" * (c == 0)}'
              f'<span class="at">{tag_label(day)}</span>{html_items(evs.get(day, []))}</div>'
              for week in weeks for c, day in enumerate(week)]
    return title, (f'<main class="month" style="--n:{n};grid-template-rows:auto repeat({len(weeks)},minmax(0,1fr))">'
                   f'{"".join(cells)}</main>')


def html_week(shown, today):
    title, days, evs = week_data(shown)
    n = len(days)
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
            f'<div class="{" ".join(["blk"] + ["teams"] * (cal == "Teams") + ["urlaub"] * urlaub(t2))}" style="top:{a / 14.4:.3f}%;'
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


def shown_date(today):
    """Angezeigtes Datum: im Monat der 1. des Monats heute + offset, in der Woche heute + offset Wochen."""
    if view == "month":
        m = today.month - 1 + offset
        return date(today.year + m // 12, m % 12 + 1, 1)
    return today + timedelta(weeks=offset)


def state(today):
    return f"{view}|{weekend:d}|{offset}|{zeit}|{raster:d}|{dark:d}|{today}|{START}"


def theme_css():
    """Inhalt der Datei THEME_CSS, bei jedem Seitenaufruf frisch gelesen; ohne Datei bleibt das eingebaute CSS."""
    try:
        return open(THEME_CSS, encoding="utf-8").read() if THEME_CSS else ""
    except OSError:
        return ""


def page(today):
    title, body = VIEWS[view][2](shown_date(today), today)
    nav = "".join(f'<a href="/view/{k}"{" class=on" if k == view else ""}>{label}</a>'
                  for k, (label, _, _) in VIEWS.items())
    nav += f'<a href="/weekend">{"☑" if weekend else "☐"} Wochenende</a><a href="/zeit">Zeit: {ZEITEN[zeit]}</a>'
    nav += f'<a href="/raster">{"☑" if raster else "☐"} Raster</a>' * (view == "week")
    nav += f'<a href="/dark">{"☑" if dark else "☐"} Dunkel</a>'
    return (f'<!doctype html><html lang="de"{" class=dark" * dark}><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kalender</title>'
            f'<style>{CSS}{theme_css()}</style></head><body>'
            f'<header><div class="titel"><a class="heute" href="/today">Heute</a>'  # links vom Titel, damit nichts springt
            f'<a class="arrow" href="/prev" aria-label="zurück">‹</a><a class="arrow" href="/next" aria-label="weiter">›</a>'
            f'<h1>{title}</h1></div><nav>{nav}<span>{stand()}</span></nav></header>{body}'
            f'<script>const state = "{state(today)}";{JS}</script></body></html>')


# ---------- HTTP ----------

def poke_device():
    try:
        urllib.request.urlopen(urllib.request.Request(DEVICE_REFRESH_URL, method="POST"), timeout=5).close()
    except Exception as e:
        print(f"Display nicht erreichbar: {e}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global view, weekend, offset, zeit, raster, dark
        path = self.path.split("?")[0].strip("/")
        today = datetime.now(TZ).date()
        if path == "":
            self.reply(page(today).encode(), "text/html; charset=utf-8")
        elif path == "state":
            self.reply(state(today).encode(), "text/plain")
        elif path == "image.png":
            buf = io.BytesIO()
            VIEWS[view][1](shown_date(today), today).save(buf, "PNG")
            self.reply(buf.getvalue(), "image/png")
        elif path in ("weekend", "prev", "next", "today", "zeit", "raster", "dark") or path.startswith("view/") and path[5:] in VIEWS:
            if path == "weekend":
                weekend = not weekend
            elif path == "raster":
                raster = not raster
            elif path == "dark":
                dark = not dark
            elif path == "zeit":
                zeit = list(ZEITEN)[(list(ZEITEN).index(zeit) + 1) % len(ZEITEN)]
            elif path in ("prev", "next", "today"):
                offset = {"prev": offset - 1, "next": offset + 1, "today": 0}[path]
            else:
                view, offset = path[5:], 0
            if DEVICE_REFRESH_URL:
                threading.Thread(target=poke_device, daemon=True).start()
            if "text/html" in self.headers.get("Accept", ""):  # Klick auf der Webseite: zurück zur Ansicht
                self.send_response(303)
                self.send_header("Location", "/")
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
        if not self.path.startswith("/state"):  # die Webseite fragt alle 3 s, das muss nicht ins Log
            super().log_message(fmt, *args)


if __name__ == "__main__":
    ThreadingHTTPServer(("", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
