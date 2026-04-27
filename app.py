#!/usr/bin/env python3
"""
link: http://localhost:5000
Deploy: gunicorn app:app
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


# ─── MANEJO DE CACHÉ ───────────────────────────────────────────────────────────────
CACHE_TTL = 24 * 3600  # tiempo que tarda hasta volver a mirar Wikidata por si se ha actualizado

def _cache_path(lo: int, hi: int) -> str:
    """Devuelve la ruta al fichero JSON de caché para el rango [lo, hi).
    Crea el directorio .cache/ si no existe."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{lo}_{hi}.json")

def _cache_load(lo: int, hi: int):
    """Carga el resultado cacheado para el rango [lo, hi) si existe y no ha expirado.
    Devuelve la lista de montañas o None si no hay caché válida."""
    path = _cache_path(lo, hi)
    if not os.path.exists(path):
        return None
    if time.time() - os.path.getmtime(path) > CACHE_TTL:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def _cache_save(lo: int, hi: int, data: list):
    """Persiste en disco la lista de montañas para el rango [lo, hi)."""
    with open(_cache_path(lo, hi), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ─── PETICIÓN A WIKIDATA ─────────────────────────────────────────────────────────────
def build_sparql(lo: int, hi: int) -> str:
    """Construye la consulta SPARQL para obtener montañas de la Tierra
    cuya altitud normalizada en metros esté en el rango [lo, hi).
    Usa una subconsulta para limitar resultados antes de resolver etiquetas."""
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
    """Devuelve la lista de montañas para el rango [lo, hi) en metros.
    Comprueba primero la caché en disco; si no hay resultado válido lanza
    la consulta SPARQL contra Wikidata, deduplica por coordenadas y guarda
    el resultado en caché antes de devolverlo."""
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

    # Recoge todas las filas
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

    # Genera solo una montaña si hay varias iguales, pues al realizar la petición obtenemos varias instancias
    # de la misma montaña.
    # De la misma manera si hay más de una montaña con las mismas coords nos quedamos con una
    # Guardamos la altura más grande de cada una y el nombre del país en el que está
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


# ─── ALMACENAMIENTO DE RANGOS MÁS USADOS ──────────────────────────────────────────────────────
# Los rangos que se prevee que van a ser más usados se guardan en una lista para precargarlos
PREFETCH_RANGES = [
    (7000, 8000), (8000, 9000), (6000, 7000),
    (5000, 6000), (4000, 5000), (3000, 4000),
]

def _prefetch():
    """Precarga en segundo plano los rangos de altitud más frecuentes.
    Se ejecuta en un hilo daemon al arrancar el servidor para que las
    primeras peticiones del usuario encuentren los datos ya en caché."""
    print("  Background prefetch started…")
    for lo, hi in PREFETCH_RANGES:
        if _cache_load(lo, hi) is not None:
            print(f"  ✓ Already cached: {lo}–{hi} m")
            continue
        try:
            print(f"  Prefetching {lo}–{hi} m…")
            query_wikidata(lo, hi)
            time.sleep(2)   # para respetas los limites de tiempo en Wikidata entre peticiones
        except Exception as e:
            print(f"  ✗ Prefetch failed {lo}–{hi}: {e}")
    print("  Background prefetch complete.")

# gunicurn funciona a la vez que lo triggerea
threading.Thread(target=_prefetch, daemon=True).start()


# ─── FLASK ROUTES ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    """Sirve la página principal de la aplicación."""
    return render_template("index.html")

# proxy entre wikidata y el navegador
@app.route("/sparql")
def sparql_proxy():
    """Proxy HTTP entre el navegador y Wikidata.
    Recibe los parámetros lo y hi, valida el rango, llama a query_wikidata
    y devuelve el resultado como JSON. Evita que el navegador tenga que
    contactar directamente con Wikidata (problemas de CORS)."""
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


# ─── MAIN ──────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  ⛰  Mountain Globe Explorer")
    print("=" * 55)
    print("  http://localhost:5000")
    print("  Press Ctrl+C to stop")
    print("=" * 55)
    app.run(debug=False, host="0.0.0.0", port=5000)
