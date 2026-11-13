# wx-log

Hourly weather log for five places in Peninsular Malaysia: what was forecast the
evening before and in the morning, next to what the airport reports and the rain
radar actually showed.

- `collect.py` runs every hour (GitHub Actions) and rewrites `data/latest.json`.
- `data/days/` keeps the raw forecast snapshots and observations per day.
- `clim.py` is a one-off that builds the ten-year daily rain rhythm in `data/clim.json`.

Sources: Open-Meteo (ECMWF, GFS, ICON, ERA5 archive), data.gov.my (MET Malaysia),
aviationweather.gov (METAR), RainViewer (radar).
