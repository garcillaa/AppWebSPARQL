#!/usr/bin/env python3
"""
Mountain Globe Explorer — Flask edition
SPARQL + Wikidata + Three.js
Run locally : python app.py  →  http://localhost:5000
Deploy      : gunicorn app:app
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

CACHE_DIR       = os.path.join(os.path.dirname(__file__), ".cache")
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"


# ─── DISK CACHE ───────────────────────────────────────────────────────────────
CACHE_TTL = 24 * 3600  # seconds — re-query Wikidata after 24 h

def _cache_path(lo: int, hi: int) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{lo}_{hi}.json")

def _cache_load(lo: int, hi: int):
    path = _cache_path(lo, hi)
    if not os.path.exists(path):
        return None
    if time.time() - os.path.getmtime(path) > CACHE_TTL:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def _cache_save(lo: int, hi: int, data: list):
    with open(_cache_path(lo, hi), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ─── SPARQL QUERY ─────────────────────────────────────────────────────────────
def build_sparql(lo: int, hi: int) -> str:
    return f"""
SELECT ?mountain ?mountainLabel ?height ?lat ?lon ?country ?countryLabel WHERE {{
  {{
    SELECT ?mountain ?height ?lat ?lon ?country WHERE {{
      {{ ?mountain wdt:P31 wd:Q8502 }}
      UNION
      {{ ?mountain wdt:P31/wdt:P279 wd:Q8502 }}
      ?mountain p:P2044/psn:P2044/wikibase:quantityAmount ?height .
      FILTER(?height >= {lo} && ?height < {hi})
      ?mountain wdt:P625 ?coord .
      BIND(geof:latitude(?coord)  AS ?lat)
      BIND(geof:longitude(?coord) AS ?lon)
      FILTER(?lat >= -90 && ?lat <= 90 && ?lon >= -180 && ?lon <= 180)
      OPTIONAL {{ ?mountain wdt:P17 ?country . }}
    }}
    ORDER BY DESC(?height)
    LIMIT 300
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,es" . }}
}}
ORDER BY DESC(?height)
"""

def query_wikidata(lo: int, hi: int):
    cached = _cache_load(lo, hi)
    if cached is not None:
        print(f"  ✓ Cache hit: {lo}–{hi} m ({len(cached)} mountains)")
        return cached

    query  = build_sparql(lo, hi)
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url    = f"{SPARQL_ENDPOINT}?{params}"
    req    = urllib.request.Request(
        url,
        headers={
            "Accept":     "application/sparql-results+json",
            "User-Agent": "MountainGlobeExplorer/1.0 (educational project)"
        }
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = json.loads(resp.read().decode())

    # First pass: collect all rows
    rows = []
    for b in raw.get("results", {}).get("bindings", []):
        try:
            name    = b["mountainLabel"]["value"]
            height  = float(b["height"]["value"])
            lat     = float(b["lat"]["value"])
            lon     = float(b["lon"]["value"])
            country = b.get("countryLabel", {}).get("value", "")
            if name.startswith("Q") and name[1:].isdigit():
                continue
            rows.append({"name": name, "height": height,
                         "lat": lat, "lon": lon, "country": country})
        except (KeyError, ValueError):
            continue

    # Deduplicate by geographic position (same coords = same mountain).
    # Multiple statements per mountain collapse into one entry:
    # keep the highest height, merge country names.
    seen = {}
    for r in rows:
        key = (round(r["lat"], 3), round(r["lon"], 3))
        if key not in seen:
            seen[key] = r.copy()
        else:
            entry = seen[key]
            if r["height"] > entry["height"]:
                entry["height"] = r["height"]
                entry["name"]   = r["name"]
            if r["country"] and r["country"] not in entry["country"]:
                entry["country"] = (entry["country"] + ", " + r["country"]).strip(", ")

    results = [
        {**e, "height": round(e["height"])}
        for e in sorted(seen.values(), key=lambda x: -x["height"])
    ]
    _cache_save(lo, hi, results)
    return results


# ─── BACKGROUND PREFETCH ──────────────────────────────────────────────────────
# Priority ranges to warm up: high-altitude bands most likely to be clicked first.
PREFETCH_RANGES = [
    (7000, 8000), (8000, 9000), (6000, 7000),
    (5000, 6000), (4000, 5000), (3000, 4000),
]

def _prefetch():
    print("  Background prefetch started…")
    for lo, hi in PREFETCH_RANGES:
        if _cache_load(lo, hi) is not None:
            print(f"  ✓ Already cached: {lo}–{hi} m")
            continue
        try:
            print(f"  Prefetching {lo}–{hi} m…")
            query_wikidata(lo, hi)
            time.sleep(2)   # respect Wikidata rate limits between requests
        except Exception as e:
            print(f"  ✗ Prefetch failed {lo}–{hi}: {e}")
    print("  Background prefetch complete.")

# Start at module load so gunicorn workers also trigger it
threading.Thread(target=_prefetch, daemon=True).start()


# ─── FLASK ROUTES ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sparql")
def sparql_proxy():
    try:
        lo = int(request.args["lo"])
        hi = int(request.args["hi"])
        if lo < 0 or hi > 10000 or lo >= hi:
            raise ValueError("Invalid range")
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    source = "cache" if _cache_load(lo, hi) is not None else "Wikidata"
    print(f"  /sparql {lo}–{hi} m → {source}")
    try:
        data = query_wikidata(lo, hi)
        print(f"  → {len(data)} mountains")
        return jsonify(data)
    except Exception as e:
        print(f"  ✗ SPARQL error: {e}")
        return jsonify({"error": str(e)}), 502


# ─── MAIN (local dev only — gunicorn ignores this block) ──────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  ⛰  Mountain Globe Explorer")
    print("=" * 55)
    print("  http://localhost:5000")
    print("  Press Ctrl+C to stop")
    print("=" * 55)
    app.run(debug=False, host="0.0.0.0", port=5000)
