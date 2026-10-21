# Essence
Quebec gas-price map. Flask + Google Maps JS.

Data: Régie de l'énergie du Québec via https://regieessencequebec.ca/stations.geojson.gz (refreshed periodically; xlsx fallback).

## Quick start
Tested on Python 3.12 (Linux). Needs a Google Maps JavaScript API key (create one in Google Cloud; billing must be enabled).

```bash
git clone https://github.com/aturcotte628/Essence.git
cd Essence
python3 -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
GOOGLE_MAPS_KEY=your-key gunicorn -b 127.0.0.1:5100 app:app
```

Open <http://127.0.0.1:5100>. Station prices (about 2,400 stations) download at start-up and refresh every hour.
Without a key the page loads but shows "Google Maps key missing". Check `http://127.0.0.1:5100/healthz` for status.

Optional: `ESSENCE_USER_AGENT` identifies your requests to the data source. `data/` (station cache and price history) is created at runtime.
To serve it publicly, put nginx or another reverse proxy in front of gunicorn.
