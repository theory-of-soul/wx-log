#!/usr/bin/env python3
"""Hourly weather log: forecast vs. what actually happened, for five places.

Each run
  1. stores the forecast (three global models + the national met office) as the
     "evening" snapshot for tomorrow and the "morning" snapshot for today,
  2. appends observations: airport METAR reports and rain-radar pixels,
  3. rebuilds data/latest.json: risk per part of day, advice, live radar,
     and the forecast-vs-reality report with per-source scores.

All times are local (UTC+8). Raw material lives in data/days/<date>.json.
"""
import io
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DAYS = DATA / "days"

PLACES = [
    dict(key="kl", name="Куала-Лумпур", lat=3.15, lon=101.71, metar=["WMSA", "WMKK"], met="Tn079",
         frm="2026-11-17", to="2026-11-22"),
    dict(key="ipoh", name="Ипох", lat=4.60, lon=101.08, metar=["WMKI"], met="Tn041",
         frm="2026-11-22", to="2026-11-27"),
    dict(key="cameron", name="Камерон", lat=4.47, lon=101.38, metar=[], met="Tn145",
         frm="2026-11-27", to="2026-11-30"),
    dict(key="penang", name="Пенанг", lat=5.41, lon=100.33, metar=["WMKP"], met="Tn013",
         frm="2026-11-30", to="2026-12-04"),
    dict(key="langkawi", name="Лангкави", lat=6.28, lon=99.73, metar=["WMKL"], met="Ds001",
         frm="2026-12-04", to="2026-12-08"),
]

# Parts of the day: label, first hour, hour after the last one.
WINDOWS = [("утро", 7, 11), ("день", 11, 15), ("вечер", 15, 19), ("поздно", 19, 23)]
MODELS = {"ecmwf": "ecmwf_ifs025", "gfs": "gfs_seamless", "icon": "icon_seamless"}
SOURCES = ["ecmwf", "gfs", "icon", "met"]

WET_MM = 1.0          # a model "predicts rain" in a window from this many mm
WET_MIN = 20          # observed rain: radar echo over the place for this many minutes
CLIM_WEIGHT = 0.25    # share of the long-term daily rhythm in the blended risk
MIN_SCORED = 12       # verified windows needed before sources are weighted by skill

ZOOM = 7
KM_PER_PX = 40075.0 / (2 ** ZOOM) / 256
AT_PX = 3             # ~4 km: "over us"
NEAR_PX = 50          # ~60 km: search radius for the nearest rain

ERRORS = []


def http(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wx-log/1.0"})
            return urllib.request.urlopen(req, timeout=40).read()
        except Exception as e:  # network hiccup: retry, then report
            last = e
            time.sleep(2 + 3 * i)
    raise last


def guarded(name, fn, *args):
    """A dead source must not stop the others; the gap is reported in latest.json."""
    try:
        return fn(*args)
    except Exception as e:
        ERRORS.append(f"{name}: {type(e).__name__}: {e}"[:200])
        return None


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return default


def save(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")


def day_file(date):
    return DAYS / f"{date}.json"


def load_day(date):
    d = load(day_file(date), {"date": date, "places": {}})
    for p in PLACES:
        d["places"].setdefault(p["key"], {"fc": {}, "radar": {}, "metar": {}})
    return d


# ---------- forecast: three global models ----------

def fetch_models():
    q = urllib.parse.urlencode({
        "latitude": ",".join(str(p["lat"]) for p in PLACES),
        "longitude": ",".join(str(p["lon"]) for p in PLACES),
        "hourly": "precipitation,precipitation_probability,cloud_cover,apparent_temperature",
        "models": ",".join(MODELS.values()),
        "forecast_days": 3, "timezone": "Asia/Kuala_Lumpur",
    })
    res = json.loads(http("https://api.open-meteo.com/v1/forecast?" + q))
    return {p["key"]: r["hourly"] for p, r in zip(PLACES, res)}


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def model_windows(hourly, date):
    """Per window: {source: [mm, probability 0..1]}, plus mean cloud and peak feels-like."""
    out = []
    for _, h0, h1 in WINDOWS:
        idx = [i for i, t in enumerate(hourly["time"]) if t[:10] == date and h0 <= int(t[11:13]) < h1]
        if len(idx) < h1 - h0:
            return None
        w = {"src": {}}
        clouds, feels = [], []
        for name, mid in MODELS.items():
            mm = [hourly.get(f"precipitation_{mid}", [None] * 999)[i] for i in idx]
            pr = [hourly.get(f"precipitation_probability_{mid}", [None] * 999)[i] for i in idx]
            if all(v is None for v in mm):
                continue
            total = round(sum(v or 0 for v in mm), 1)
            known = [v for v in pr if v is not None]
            prob = max(known) / 100 if known else (0.7 if total >= WET_MM else 0.15)
            w["src"][name] = [total, round(prob, 2)]
            clouds.append(mean([hourly[f"cloud_cover_{mid}"][i] for i in idx]))
            feels.append(max((hourly[f"apparent_temperature_{mid}"][i] or -99) for i in idx))
        w["cloud"] = round(mean(clouds)) if mean(clouds) is not None else None
        w["feels"] = round(mean(feels)) if feels else None
        out.append(w)
    return out


# ---------- forecast: national met office (text per part of day) ----------

MET_WORDS = [("menyeluruh", 0.85, "повсеместно"), ("kebanyakan tempat", 0.7, "в большинстве мест"),
             ("beberapa tempat", 0.45, "местами"), ("satu dua tempat", 0.3, "кое-где")]


def met_phrase(text):
    """Malay forecast phrase -> (probability, Russian)."""
    t = (text or "").lower()
    if not t:
        return None
    if "tiada hujan" in t:
        return [0.05, "без дождя"]
    kind = "гроза" if "ribut petir" in t else "дождь" if "hujan" in t else None
    if kind is None:
        return [0.1, text]
    for word, p, ru in MET_WORDS:
        if word in t:
            return [p, f"{kind} {ru}"]
    return [0.6, kind]


def fetch_met():
    out = {}
    for p in PLACES:
        rows = json.loads(http("https://api.data.gov.my/weather/forecast/?limit=10&contains="
                               + p["met"] + "@location__location_id"))
        out[p["key"]] = {r["date"]: [met_phrase(r.get("morning_forecast")),
                                     met_phrase(r.get("afternoon_forecast")),
                                     met_phrase(r.get("night_forecast"))] for r in rows}
    return out


def add_met(windows, met_day):
    """Morning -> window 0, afternoon -> windows 1 and 2, night -> window 3."""
    if not windows or not met_day:
        return
    for i, part in enumerate([0, 1, 1, 2]):
        ph = met_day[part]
        if ph:
            windows[i]["src"]["met"] = [None, ph[0]]
            windows[i]["met"] = ph[1]


# ---------- observations: airport reports ----------

WX = re.compile(r"^(RE)?(\+|-|VC)?(TS|SH)?(RA|DZ|GR|GS)*$")


def parse_metar(raw):
    rain = ts = vc = False
    body = raw.split(" RMK")[0].split(" TEMPO")[0].split(" BECMG")[0]
    for tok in body.split()[3:]:
        m = WX.match(tok)
        if not m or not (m.group(3) or m.group(4)) or tok in ("RE", "VC"):
            continue
        near = m.group(2) == "VC"
        if "TS" in tok:
            ts = True
        if "RA" in tok or "DZ" in tok or "SH" in tok:
            if near:
                vc = True
            else:
                rain = True
    return rain, ts, vc


def fetch_metar():
    ids = ",".join(s for p in PLACES for s in p["metar"])
    rows = json.loads(http(f"https://aviationweather.gov/api/data/metar?ids={ids}&hours=4&format=json"))
    return [dict(st=r["icaoId"], t=int(r["obsTime"]), temp=r.get("temp"), raw=r["rawOb"]) for r in rows]


def store_metar(days, reports):
    for r in reports:
        date = datetime.fromtimestamp(r["t"], TZ).strftime("%Y-%m-%d")
        rain, ts, vc = parse_metar(r["raw"])
        for p in PLACES:
            if r["st"] in p["metar"]:
                day = days.setdefault(date, load_day(date))
                day["places"][p["key"]]["metar"][f'{r["st"]}:{r["t"]}'] = [int(rain), int(ts), int(vc), r["temp"]]


# ---------- observations: rain radar ----------

def level(px):
    """Radar pixel -> 0 nothing, 1 light, 2 moderate, 3 heavy (RainViewer 'universal blue')."""
    r, g, b, a = px
    if a < 255:
        return 0          # semi-transparent beige: echo too weak to be rain
    if r > b:
        return 3          # yellow / orange / red
    return 1 if g >= 180 else 2


COMPASS = ["север", "северо-восток", "восток", "юго-восток", "юг", "юго-запад", "запад", "северо-запад"]


class Frame:
    def __init__(self, path):
        self.path, self.tiles = path, {}

    def px(self, gx, gy):
        key = (gx // 256, gy // 256)
        if key not in self.tiles:
            raw = http(f"https://tilecache.rainviewer.com{self.path}/256/{ZOOM}/{key[0]}/{key[1]}/2/0_0.png")
            self.tiles[key] = Image.open(io.BytesIO(raw)).convert("RGBA").load()
        return self.tiles[key][gx % 256, gy % 256]

    def sample(self, lat, lon):
        """[strongest level over the place, km to nearest rain or None, compass index or None]"""
        n = 2 ** ZOOM * 256
        cx = int((lon + 180) / 360 * n)
        cy = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
        at_count, at_level, best = 0, 0, None
        for dy in range(-NEAR_PX, NEAR_PX + 1):
            for dx in range(-NEAR_PX, NEAR_PX + 1):
                d2 = dx * dx + dy * dy
                if d2 > NEAR_PX * NEAR_PX:
                    continue
                lv = level(self.px(cx + dx, cy + dy))
                if not lv:
                    continue
                if d2 <= AT_PX * AT_PX:
                    at_count += 1
                    at_level = max(at_level, lv)
                if lv >= 1 and (best is None or d2 < best[0]):
                    best = (d2, dx, dy)
        if at_count < 3:      # one or two stray pixels are noise, not rain
            at_level = 0
        if best is None:
            return [at_level, None, None]
        km = round(math.sqrt(best[0]) * KM_PER_PX)
        bearing = math.degrees(math.atan2(best[1], -best[2])) % 360
        return [at_level, km, int((bearing + 22.5) // 45) % 8]


def store_radar(days):
    maps = json.loads(http("https://api.rainviewer.com/public/weather-maps.json"))
    for fr in maps["radar"]["past"]:
        t = int(fr["time"])
        date = datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d")
        day = days.setdefault(date, load_day(date))
        if all(str(t) in day["places"][p["key"]]["radar"] for p in PLACES):
            continue
        frame = Frame(fr["path"])
        for p in PLACES:
            day["places"][p["key"]]["radar"][str(t)] = frame.sample(p["lat"], p["lon"])


# ---------- warnings ----------

ROUTE = re.compile(r"Kuala Lumpur|Selangor|Perak|Pahang|Cameron|Pulau Pinang|Penang|Kedah|Langkawi", re.I)


def fetch_warnings(now):
    rows = json.loads(http("https://api.data.gov.my/weather/warning/?limit=8"))
    out = []
    for r in rows:
        if (r.get("valid_to") or "") < now.strftime("%Y-%m-%dT%H:%M:%S"):
            continue
        text = r.get("text_en") or ""
        out.append(dict(title=(r.get("warning_issue") or {}).get("title_en"), head=r.get("heading_en"),
                        frm=r.get("valid_from"), to=r.get("valid_to"),
                        route=bool(ROUTE.search(text)), text=text[:700]))
    return out


# ---------- what happened ----------

def observed(place, pday, now, date):
    """Per window: radar minutes of rain, airport rain reports, share of radar frames we hold."""
    out = []
    for _, h0, h1 in WINDOWS:
        start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=TZ, hour=h0)
        end = start + timedelta(hours=h1 - h0)
        if now < start:
            out.append(None)
            continue
        frames = [v for t, v in pday["radar"].items() if start.timestamp() <= int(t) < end.timestamp()]
        wet_min = 10 * sum(1 for v in frames if v[0] >= 1)
        heavy = any(v[0] >= 3 for v in frames)
        primary = place["metar"][0] if place["metar"] else None
        reps = [v for k, v in pday["metar"].items()
                if k.split(":")[0] == primary and start.timestamp() <= int(k.split(":")[1]) < end.timestamp()]
        metar_rain = sum(v[0] for v in reps)
        cover = round(len(frames) / ((h1 - h0) * 6), 2)
        done = now >= end
        wet = wet_min >= WET_MIN or metar_rain >= 1
        out.append(dict(min=wet_min, heavy=heavy, metar=metar_rain if primary else None, cover=cover,
                        done=done, wet=wet, ok=done and (cover >= 0.6 or wet)))
    return out


# ---------- scores, blend, advice ----------

def scores(history):
    """Brier score and hit rate per source, separately for evening and morning forecasts."""
    acc = {}
    for rec in history:
        for i, obs in enumerate(rec["obs"]):
            if not obs or not obs["ok"]:
                continue
            for snap in ("evening", "morning"):
                wins = rec["fc"].get(snap)
                if not wins:
                    continue
                for src, (_, prob) in wins[i]["src"].items():
                    a = acc.setdefault(snap, {}).setdefault(src, [0, 0, 0.0])
                    a[0] += 1
                    a[1] += int((prob >= 0.5) == obs["wet"])
                    a[2] += (prob - (1.0 if obs["wet"] else 0.0)) ** 2
    return {snap: {src: dict(n=a[0], hit=round(a[1] / a[0], 2), brier=round(a[2] / a[0], 3))
                   for src, a in by.items()} for snap, by in acc.items()}


def weights(score):
    by = (score or {}).get("evening", {})
    if not by or min(v["n"] for v in by.values()) < MIN_SCORED:
        return {}
    return {src: 1 / (v["brier"] + 0.05) for src, v in by.items()}


def blend(win, wts, clim):
    src = win["src"]
    if not src:
        return None, None
    total = sum(wts.get(s, 1.0) for s in src)
    p = sum(wts.get(s, 1.0) * v[1] for s, v in src.items()) / total
    if clim is not None:
        p = (1 - CLIM_WEIGHT) * p + CLIM_WEIGHT * clim
    probs = [v[1] for v in src.values()]
    return round(p, 2), round(max(probs) - min(probs), 2)


def advise(ps, spreads):
    morning, evening = ps[0], ps[2]
    pct = lambda x: f"{round(x * 100)}%"
    if max(ps) < 0.35:
        code, text = "dry", "Сухой день: порядок как в плане."
    elif morning >= 0.6 and evening >= 0.6:
        code, text = "wet", f"Мокро и утром ({pct(morning)}), и после 15 ({pct(evening)}): план под крышей, улицу ловить по радару."
    elif evening - morning >= 0.25:
        code, text = "am", f"Улицу и виды — утром (риск {pct(morning)}). После 15:00 риск дождя {pct(evening)}."
    elif morning - evening >= 0.25:
        code, text = "pm", f"Утро мокрое ({pct(morning)}), улицу и виды — после 15:00 (риск {pct(evening)})."
    else:
        code, text = "open", f"Разницы мало: утро {pct(morning)}, после 15 — {pct(evening)}. Как в плане, запасной под рукой."
    spread = mean(spreads) or 0
    conf = "high" if spread < 0.3 else "mid" if spread < 0.5 else "low"
    return code, text, conf


def live_radar(pday_today, pday_yesterday, now):
    frames = {**pday_yesterday["radar"], **pday_today["radar"]}
    recent = sorted((int(t), v) for t, v in frames.items() if now.timestamp() - int(t) <= 3 * 3600)
    if not recent:
        return None
    t, (lv, km, comp) = recent[-1]
    out = dict(t=t, level=lv, km=km, dir=COMPASS[comp] if comp is not None else None, trend=None)
    earlier = [v for tt, v in recent if 20 * 60 <= t - tt <= 40 * 60]
    if earlier and km is not None and earlier[0][1] is not None:
        diff = km - earlier[0][1]
        out["trend"] = "приближается" if diff <= -5 else "уходит" if diff >= 5 else "на месте"
    out["track"] = [[tt, v[0], v[1]] for tt, v in recent[-12:]]
    return out


def live_metar(place, pday_today, pday_yesterday):
    if not place["metar"]:
        return None
    reps = {**pday_yesterday["metar"], **pday_today["metar"]}
    mine = sorted((int(k.split(":")[1]), v) for k, v in reps.items() if k.split(":")[0] == place["metar"][0])
    if not mine:
        return None
    t, (rain, ts, vc, temp) = mine[-1]
    return dict(t=t, st=place["metar"][0], rain=bool(rain), ts=bool(ts), vc=bool(vc), temp=temp)


def main():
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(-1, 3)]
    days = {d: load_day(d) for d in dates}
    clim = load(DATA / "clim.json", {})

    hourly = guarded("open-meteo", fetch_models) or {}
    met = guarded("met", fetch_met) or {}
    reports = guarded("metar", fetch_metar)
    if reports:
        store_metar(days, reports)
    guarded("radar", store_radar, days)
    warnings = guarded("warnings", fetch_warnings, now)

    # Current forecast per place and date; freeze the evening and morning snapshots.
    current = {}
    for p in PLACES:
        for date in dates[1:]:
            wins = model_windows(hourly[p["key"]], date) if p["key"] in hourly else None
            if wins is None:
                continue
            add_met(wins, met.get(p["key"], {}).get(date))
            current[(p["key"], date)] = wins
            fc = days[date]["places"][p["key"]]["fc"]
            stamp = dict(taken=now.strftime("%Y-%m-%dT%H:%M"), w=wins)
            if date == dates[2] and now.hour >= 20 and "evening" not in fc:
                fc["evening"] = stamp
            if date == today and 5 <= now.hour < 8 and "morning" not in fc:
                fc["morning"] = stamp

    for date, day in days.items():
        save(day_file(date), day)

    # Report: every stored day up to today, forecast snapshots next to what happened.
    history = {p["key"]: [] for p in PLACES}
    for f in sorted(DAYS.glob("*.json")):
        day = load(f, None)
        if not day or day["date"] > today:
            continue
        for p in PLACES:
            pday = day["places"].get(p["key"])
            if not pday:
                continue
            history[p["key"]].append(dict(
                date=day["date"],
                fc={k: v["w"] for k, v in pday["fc"].items()},
                obs=observed(p, pday, now, day["date"])))
    score = scores([r for recs in history.values() for r in recs])
    wts = weights(score)

    out = dict(generated=now.strftime("%Y-%m-%dT%H:%M"), today=today, errors=ERRORS, warnings=warnings or [],
               windows=[[w[0], w[1], w[2]] for w in WINDOWS], sources=SOURCES, score=score,
               weighted=bool(wts), places=[])
    for p in PLACES:
        key = p["key"]
        place = dict(key=key, name=p["name"], frm=p["frm"], to=p["to"], station=p["metar"][0] if p["metar"] else None,
                     radar=live_radar(days[today]["places"][key], days[dates[0]]["places"][key], now),
                     metar=live_metar(p, days[today]["places"][key], days[dates[0]]["places"][key]),
                     days=[], report=[])
        for date in dates[1:]:
            wins = current.get((key, date))
            if not wins:
                continue
            ps, spreads = [], []
            for i, w in enumerate(wins):
                w["p"], w["spread"] = blend(w, wts, (clim.get(key) or [None] * 4)[i])
                ps.append(w["p"] or 0)
                spreads.append(w["spread"])
            code, text, conf = advise(ps, spreads)
            place["days"].append(dict(date=date, w=wins, advice=code, text=text, conf=conf,
                                      obs=observed(p, days[date]["places"][key], now, date) if date == today else None))
        for rec in history[key][-8:]:
            for snap in rec["fc"].values():
                for i, w in enumerate(snap):
                    w["p"], w["spread"] = blend(w, {}, (clim.get(key) or [None] * 4)[i])
            place["report"].append(rec)
        place["clim"] = clim.get(key)
        out["places"].append(place)
    save(DATA / "latest.json", out)
    print(f"ok {out['generated']} errors={len(ERRORS)}", *ERRORS, sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
