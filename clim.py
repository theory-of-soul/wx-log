#!/usr/bin/env python3
"""One-off: long-term daily rain rhythm per place (share of days with rain in each
part of the day, 10 Nov - 10 Dec, last ten years, ERA5 reanalysis). Writes data/clim.json."""
import json
import urllib.parse
import urllib.request

from collect import DATA, PLACES, WET_MM, WINDOWS, save

wet = {p["key"]: [0] * len(WINDOWS) for p in PLACES}
total = {p["key"]: 0 for p in PLACES}
for year in range(2016, 2026):
    q = urllib.parse.urlencode({
        "latitude": ",".join(str(p["lat"]) for p in PLACES),
        "longitude": ",".join(str(p["lon"]) for p in PLACES),
        "start_date": f"{year}-11-10", "end_date": f"{year}-12-10",
        "hourly": "precipitation", "timezone": "Asia/Kuala_Lumpur",
    })
    req = urllib.request.Request("https://archive-api.open-meteo.com/v1/archive?" + q,
                                 headers={"User-Agent": "wx-log/1.0"})
    res = json.loads(urllib.request.urlopen(req, timeout=60).read())
    for p, r in zip(PLACES, res):
        rain = r["hourly"]["precipitation"]
        for d in range(len(rain) // 24):
            total[p["key"]] += 1
            for i, (_, h0, h1) in enumerate(WINDOWS):
                if sum(v or 0 for v in rain[d * 24 + h0:d * 24 + h1]) >= WET_MM:
                    wet[p["key"]][i] += 1
    print(year, "ok")
clim = {k: [round(w / total[k], 2) for w in wet[k]] for k in wet}
save(DATA / "clim.json", clim)
print(clim)
