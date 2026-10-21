"""Essence — Quebec gas prices on a Google map.

Data: Régie de l'énergie du Québec, published through regieessencequebec.ca
(stations.geojson.gz, which also names the current stations-YYYYMMDDHHMMSS.xlsx).
"""
import gzip
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, Response, jsonify, redirect, render_template, request

BASE = "https://regieessencequebec.ca"
GEOJSON_URL = BASE + "/stations.geojson.gz"
REFRESH_SECONDS = 60 * 60
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "stations.json")
HISTORY_DB = os.path.join(HERE, "data", "history.db")
HISTORY_BRAND = "Petro-Canada"
HISTORY_DAYS = 30
UA = os.environ.get("ESSENCE_USER_AGENT", "essence-map/1.0 (price map)")
FUELS = {"Régulier": "regular", "Super": "super", "Diesel": "diesel"}

log = logging.getLogger("essence")
app = Flask(__name__)
app.json.ensure_ascii = False

_lock = threading.Lock()
_state = {"stations": [], "generated_at": None, "fetched_at": None, "source": None, "error": None}


def _price(raw):
    """'179.9¢' -> 179.9 ; anything unparsable -> None."""
    m = re.search(r"\d+(?:[.,]\d+)?", str(raw or ""))
    return float(m.group(0).replace(",", ".")) if m else None


def parse_geojson(doc):
    """GeoJSON feature collection -> flat station dicts (name, brand, address, lat/lon, prices).

    Features without coordinates are skipped. `prices` maps fuel key (regular/super/diesel)
    to cents per litre and only holds fuels that are available and pass the sanity check.
    """
    out = []
    for f in doc.get("features", []):
        p = f.get("properties", {})
        try:
            lon, lat = f["geometry"]["coordinates"][:2]
        except (KeyError, TypeError, ValueError):
            continue
        prices = {}
        for entry in p.get("Prices") or []:
            key = FUELS.get(entry.get("GasType"))
            if key and entry.get("IsAvailable", True):
                val = _price(entry.get("Price"))
                if val and 50 < val < 500:  # sanity: cents per litre
                    prices[key] = val
        out.append({
            "id": len(out),
            "name": (p.get("Name") or "").strip(),
            "brand": (p.get("brand") or "").strip(),
            "address": (p.get("Address") or "").strip(),
            "postal": (p.get("PostalCode") or "").strip(),
            "region": (p.get("Region") or "").strip(),
            "status": (p.get("Status") or "").strip(),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "prices": prices,
        })
    return out


def fetch_geojson():
    """Download and decode the stations GeoJSON published by the Régie."""
    r =requests.get(GEOJSON_URL, headers={"User-Agent": UA, "Accept-Encoding": "gzip"}, timeout=30)
    r.raise_for_status()
    raw = r.content
    if raw[:2] == b"\x1f\x8b":  # server sometimes hands the gzip through untouched
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8-sig"))


def fetch_xlsx_fallback(excel_path=None):
    """Same data from the published spreadsheet if the geojson is unavailable."""
    import openpyxl
    if not excel_path:
        raise RuntimeError("no xlsx path known")
    r = requests.get(BASE + excel_path, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    ws = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True).active
    rows = ws.iter_rows(values_only=True)
    next(rows)
    feats = []
    for n, (name, brand, addr, region, postal, lat, lon, reg, sup, die) in enumerate(rows):
        if lat is None or lon is None:
            continue
        feats.append({"geometry": {"coordinates": [lon, lat]}, "properties": {
            "Name": name, "brand": brand, "Address": addr, "Region": region, "PostalCode": postal,
            "Status": "En opération",
            "Prices": [{"GasType": g, "Price": v, "IsAvailable": bool(v)}
                       for g, v in (("Régulier", reg), ("Super", sup), ("Diesel", die))]}})
    return {"features": feats, "metadata": {}}


def record_history(stations, now=None):
    """Append price changes for HISTORY_BRAND stations; keep HISTORY_DAYS of history.

    Only changes are stored (one row per station/fuel when the price differs from the last
    stored one), so the table stays small. Pruning keeps the last row at or before the
    cutoff so the price in effect at the start of the window is still known.
    """
    now = int(now or time.time())
    cutoff = now - HISTORY_DAYS * 86400
    db = sqlite3.connect(HISTORY_DB, timeout=30)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS station (id INTEGER PRIMARY KEY, lat REAL, lon REAL, address TEXT, name TEXT, UNIQUE(lat, lon, address))")
            db.execute("CREATE TABLE IF NOT EXISTS price (station_id INTEGER, fuel TEXT, ts INTEGER, cents REAL, PRIMARY KEY (station_id, fuel, ts)) WITHOUT ROWID")
            last = {(sid, fuel): cents for sid, fuel, cents in db.execute(
                "SELECT p.station_id, p.fuel, p.cents FROM price p JOIN (SELECT station_id, fuel, MAX(ts) m FROM price GROUP BY 1, 2) l "
                "ON p.station_id = l.station_id AND p.fuel = l.fuel AND p.ts = l.m")}
            added = 0
            for st in stations:
                if st["brand"] != HISTORY_BRAND:
                    continue
                db.execute("INSERT OR IGNORE INTO station (lat, lon, address, name) VALUES (?, ?, ?, ?)",
                           (st["lat"], st["lon"], st["address"], st["name"]))
                sid = db.execute("SELECT id FROM station WHERE lat = ? AND lon = ? AND address = ?",
                                 (st["lat"], st["lon"], st["address"])).fetchone()[0]
                for fuel, cents in st["prices"].items():
                    if last.get((sid, fuel)) != cents:
                        db.execute("INSERT OR REPLACE INTO price VALUES (?, ?, ?, ?)", (sid, fuel, now, cents))
                        added += 1
            db.execute("DELETE FROM price WHERE ts < ? AND EXISTS (SELECT 1 FROM price n WHERE n.station_id = price.station_id "
                       "AND n.fuel = price.fuel AND n.ts > price.ts AND n.ts <= ?)", (cutoff, cutoff))
        log.info("history: %d price changes recorded", added)
    finally:
        db.close()


def refresh():
    """Fetch fresh data (geojson, else xlsx), update the in-memory state, history and disk cache.

    Raises if the data looks truncated so the previous good data stays in place.
    """
    try:
        doc = fetch_geojson()
        source = "geojson"
    except Exception as exc:  # noqa: BLE001
        log.warning("geojson fetch failed (%s); trying xlsx", exc)
        excel = (_state.get("excel_path") or None)
        doc = fetch_xlsx_fallback(excel)
        source = "xlsx"
    stations = parse_geojson(doc)
    if len(stations) < 500:  # refuse obviously truncated data
        raise RuntimeError(f"only {len(stations)} stations parsed, keeping previous data")
    meta = doc.get("metadata", {})
    with _lock:
        _state.update(
            stations=stations,
            generated_at=meta.get("generated_at"),
            excel_path=meta.get("excel_url"),
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source=source,
            error=None,
        )
        snapshot = dict(_state)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    try:
        record_history(stations)
    except Exception:  # noqa: BLE001 - history must never break the live map
        log.exception("history update failed")
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False)
    os.replace(tmp, CACHE)
    log.info("loaded %d stations from %s", len(stations), source)


def _loop():
    """Background thread: refresh every REFRESH_SECONDS, recording any error for /healthz."""
    while True:
        time.sleep(REFRESH_SECONDS)
        try:
            refresh()
        except Exception as exc:  # noqa: BLE001
            log.error("refresh failed: %s", exc)
            with _lock:
                _state["error"] = str(exc)


def _boot():
    """On startup: load the disk cache so the map works at once, refresh, start the update thread."""
    try:
        with open(CACHE, encoding="utf-8") as fh:
            _state.update(json.load(fh))
    except (OSError, ValueError):
        pass
    try:
        refresh()
    except Exception as exc:  # noqa: BLE001
        log.error("initial refresh failed: %s", exc)
        _state["error"] = str(exc)
    threading.Thread(target=_loop, daemon=True).start()


_boot()


@app.before_request
def force_https():
    """Redirect plain-HTTP visitors to HTTPS (health checks are exempt)."""
    # Cloudflare terminates TLS and talks plain HTTP to nginx; CF-Visitor carries the real scheme.
    if '"scheme":"http"' in request.headers.get("CF-Visitor", "").replace(" ", "") and request.path != "/healthz":
        return redirect("https://" + request.host + request.full_path.rstrip("?"), 301)


@app.route("/")
def index():
    """Map page; the Google Maps key is injected from the GOOGLE_MAPS_KEY environment variable."""
    return render_template("index.html", maps_key=os.environ.get("GOOGLE_MAPS_KEY", ""))


@app.route("/api/stations")
def stations():
    """All stations as JSON; ETag lets clients get a 304 until the source data changes."""
    with _lock:
        body = {
            "generated_at": _state["generated_at"],
            "fetched_at": _state["fetched_at"],
            "count": len(_state["stations"]),
            "stations": _state["stations"],
        }
    etag = f'"{body["generated_at"] or body["fetched_at"]}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)
    resp = jsonify(body)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route("/api/history")
def history():
    """30-day price steps for one tracked station: {fuel: [[ts, cents], ...]}."""
    try:
        lat, lon = float(request.args["lat"]), float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify(error="lat/lon required"), 400
    address = request.args.get("a", "")
    cutoff = int(time.time()) - HISTORY_DAYS * 86400
    out = {}
    try:
        db = sqlite3.connect(HISTORY_DB, timeout=10)
        try:
            row = db.execute("SELECT id FROM station WHERE lat = ? AND lon = ? AND address = ?", (lat, lon, address)).fetchone()
            if row:
                # the last point at or before the cutoff is the price in effect when the window opens
                for fuel, ts, cents in db.execute(
                        "SELECT fuel, ts, cents FROM price WHERE station_id = ? ORDER BY fuel, ts", (row[0],)):
                    out.setdefault(fuel, []).append([ts, cents])
        finally:
            db.close()
    except sqlite3.Error:
        pass
    resp = jsonify(since=cutoff, now=int(time.time()), series=out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route("/healthz")
def healthz():
    """Liveness check: 200 if stations are loaded and data is under 4 refresh cycles old, else 503."""
    with _lock:
        age = None
        if _state["fetched_at"]:
            age = int((datetime.now(timezone.utc) - datetime.fromisoformat(_state["fetched_at"])).total_seconds())
        ok = bool(_state["stations"]) and (age is None or age < REFRESH_SECONDS * 4)
        return jsonify(ok=ok, stations=len(_state["stations"]), age_seconds=age,
                       source=_state["source"], error=_state["error"]), (200 if ok else 503)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run("127.0.0.1", 5100)
else:
    logging.basicConfig(level=logging.INFO)
