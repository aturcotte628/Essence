# Essence
Quebec gas-price map. Flask + Google Maps JS.

Data: Régie de l'énergie du Québec via https://regieessencequebec.ca/stations.geojson.gz (refreshed periodically; xlsx fallback).

## Run
```
pip install -r requirements.txt
GOOGLE_MAPS_KEY=your-key gunicorn -b 127.0.0.1:5100 app:app
```
Optional: `ESSENCE_USER_AGENT` to identify your requests to the data source. Health check: `/healthz`.
`data/` (station cache and price history) is created at runtime.
