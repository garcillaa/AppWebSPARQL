# Práctica 7 de Web Semántica — Aplicación Web con SPARQL y Wikidata

La aplicación va a desplegar un globo terráqueo en 3d interactivo. Se va a realizar
una consulta sobre las montañas que miden entre una franja de 500 m de altura
esa franja la va a establecer el usuario y va a poder ver las montañas representandas
en el globo.

🌐 **Aplicación desplegada:** https://appwebsparql.onrender.com

---

## Índice

1. [Estructura del proyecto](#1-estructura-del-proyecto)
2. [Instrucciones para ejecutar la aplicación](#2-instrucciones-para-ejecutar-la-aplicación)
3. [Lógica de programación por archivo](#3-lógica-de-programación-por-archivo)
   - [app.py](#31-apppy)
   - [templates/index.html](#32-templatesindexhtml)
   - [requirements.txt](#33-requirementstxt)
   - [render.yaml](#34-renderyaml)
4. [La consulta SPARQL](#4-la-consulta-sparql)
5. [Decisiones de diseño y optimizaciones](#5-decisiones-de-diseño-y-optimizaciones)

---

## 1. Estructura del proyecto

```
AppWebSPARQL/
├── app.py                 # Servidor Flask: lógica SPARQL, caché y rutas HTTP
├── main.py                # Versión alternativa con http.server (sin Flask)
├── templates/
│   └── index.html         # Interfaz web: globo 3D, sidebar y lógica de cliente
├── requirements.txt       # Dependencias Python necesarias para el despliegue
├── render.yaml            # Configuración de despliegue en Render.com
├── .gitignore             # Archivos excluidos del repositorio
└── CLAUDE.md              # Guía de estilo para el asistente de código
```

---

## 2. Instrucciones para ejecutar la aplicación

### Requisitos previos

- Python 3.9 o superior
- Conexión a internet (la aplicación consulta Wikidata en tiempo real)

---

### Opción A — Ejecución local con Flask (recomendada)

**1. Clonar el repositorio**
```bash
git clone https://github.com/garcillaa/AppWebSPARQL.git
cd AppWebSPARQL
```

**2. Crear un entorno virtual e instalar dependencias**
```bash
python -m venv .venv

# En Windows:
.venv\Scripts\activate

# En macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

**3. Arrancar el servidor**
```bash
python app.py
```

**4. Abrir en el navegador**
```
http://localhost:5000
```

---

### Opción B — Versión desplegada en producción

La aplicación está disponible públicamente sin necesidad de instalación:

```
https://appwebsparql.onrender.com
```

> **Nota:** Al estar en el plan gratuito de Render, la instancia puede tardar hasta 50 segundos en responder si lleva 
> un tiempo sin recibir visitas. para paliar esto hemos creado un uptimerobot que pingea
> la págna cada 5 minutos.

---

## 3. Lógica de programación por archivo

### 3.1 `app.py`

Este archivo es el núcleo del servidor. Se divide en cinco bloques:

---

#### Bloque 1 — Imports y configuración inicial

```python
from flask import Flask, jsonify, render_template, request
app = Flask(__name__)

CACHE_DIR       = os.path.join(os.path.dirname(__file__), ".cache")
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
```

Se importan únicamente los módulos necesarios: Flask para el servidor web y los módulos estándar de Python (`json`, `os`, `threading`, `time`, `urllib`) para el resto de funcionalidad. Se definen dos constantes globales: la carpeta donde se guardará la caché en disco y la URL del endpoint SPARQL de Wikidata.

---

#### Bloque 2 — Sistema de caché en disco

```python
CACHE_TTL = 24 * 3600   # 24 horas en segundos

_cache_path(lo, hi)   →  .cache/7000_8000.json
_cache_load(lo, hi)   →  devuelve datos si existen y tienen menos de 24h
_cache_save(lo, hi)   →  escribe el JSON en disco
```

**Por qué existe este sistema:** el endpoint SPARQL de Wikidata tiene límites de uso y puede ser lento (las consultas tardan entre 5 y 45 segundos). Sin caché, cada vez que el usuario selecciona un rango se lanzaría una consulta completa a Wikidata. Con caché en disco, la segunda vez que se solicita el mismo rango la respuesta es inmediata, sin red.

El TTL de 24 horas garantiza que los datos no queden obsoletos indefinidamente: pasado ese tiempo, la próxima petición actualiza la caché desde Wikidata.

---

#### Bloque 3 — Consulta SPARQL a Wikidata

**`build_sparql(lo, hi)`** construye dinámicamente la consulta SPARQL insertando los límites de altitud del rango seleccionado. La consulta incluye:

- **UNION** para buscar instancias directas de montaña (`wdt:P31 wd:Q8502`) y sus subclases directas (`wdt:P31/wdt:P279 wd:Q8502`), evitando el costoso recorrido transitivo `wdt:P279*`.
- **`psn:P2044`** (normalized statement) para obtener la altitud en metros independientemente de la unidad original en Wikidata (pies, metros, etc.).
- **Subconsulta interna** con `LIMIT 300` y `ORDER BY DESC(?height)` para que el motor de Wikidata acote los resultados antes de resolver las etiquetas de texto.
- **`SERVICE wikibase:label`** fuera de la subconsulta, de forma que solo resuelve etiquetas para las 300 montañas ya filtradas, no para todos los candidatos intermedios.
- **Filtro de coordenadas terrestres** `FILTER(?lat >= -90 && ?lat <= 90 && ?lon >= -180 && ?lon <= 180)` que excluye montañas de otros planetas (Marte, Plutón...) cuyos datos también están en Wikidata.

**`query_wikidata(lo, hi)`** ejecuta el ciclo completo:

1. Comprueba la caché; si hay resultado válido, lo devuelve directamente.
2. Construye la query y la envía a Wikidata con `urllib` (sin dependencias externas).
3. Parsea los bindings JSON de la respuesta.
4. Filtra entradas con etiquetas automáticas de Wikidata (identificadores del tipo `Q12345`).
5. **Deduplica por coordenadas geográficas:** como Wikidata puede devolver varias filas para la misma montaña (distintas mediciones históricas o distintos países en el caso de cimas fronterizas), se agrupa por posición redondeada a 3 decimales. Para cada grupo se conserva la mayor altura registrada y se concatenan los nombres de país.
6. Guarda el resultado en disco y lo devuelve.

---

#### Bloque 4 — Precarga en segundo plano

```python
PREFETCH_RANGES = [(7000,8000), (8000,9000), (6000,7000), ...]

def _prefetch():  # se ejecuta en un hilo daemon al arrancar
    for lo, hi in PREFETCH_RANGES:
        if no está en caché:
            query_wikidata(lo, hi)
            time.sleep(2)   # respeta los límites de Wikidata
```

Al importar el módulo (tanto con `python app.py` como con `gunicorn`), se lanza un hilo daemon que empieza a calentar los rangos de mayor altitud. La pausa de 2 segundos entre peticiones respeta la política de uso del endpoint público de Wikidata. Al ser un hilo daemon, si el servidor se detiene el hilo se cancela automáticamente sin bloquear el cierre.

---

#### Bloque 5 — Rutas Flask

**`GET /`** → devuelve `templates/index.html` renderizado.

**`GET /sparql?lo=X&hi=Y`** → proxy entre el navegador y Wikidata:
- Valida que los parámetros `lo` y `hi` sean enteros en un rango razonable.
- Llama a `query_wikidata` y devuelve el resultado como JSON.
- Si ocurre un error en Wikidata, devuelve HTTP 502 con el mensaje del error.

La razón de existir este proxy es que el navegador no puede llamar directamente al endpoint SPARQL de Wikidata desde JavaScript por restricciones CORS. El servidor Python actúa de intermediario.

---

### 3.2 `templates/index.html`

Este archivo es la interfaz completa. Se divide en tres partes: CSS, HTML y JavaScript.

---

#### Parte 1 — CSS

Se definen variables CSS globales (paleta de colores oscura) y el layout:

- `body` usa `display: flex; flex-direction: column` para apilar cabecera y contenido principal.
- `.main` usa `display: flex` para colocar el sidebar a la izquierda y el globo a la derecha.
- El sidebar tiene anchura fija (300px); el globo ocupa el espacio restante con `flex: 1`.
- El tooltip es un elemento `position: absolute` que sigue al ratón y se muestra/oculta con la clase `.visible`.

---

#### Parte 2 — HTML

```html
<header>         →  barra superior con nombre y subtítulo
<aside.sidebar>  →  lista de botones de rango + indicador de estado
<div.globe-wrap> →  canvas WebGL + tooltip + leyenda + controles
```

Los botones del sidebar se generan dinámicamente en JavaScript, no están escritos en el HTML.

---

#### Parte 3 — JavaScript

**Generación del sidebar:**
Se construyen programáticamente los botones de rango: primero franjas de 500 en 500 metros (desde 500 hasta 9000 m) y luego dos rangos amplios de 1000 m (7000–8000 y 8000–9000) para facilitar la consulta de los ochomiles. Cada botón recibe un color distinto del array `RANGE_COLORS` y un listener de clic que llama a `loadRange(idx)`.

**Configuración de Three.js:**
- Se crea un `WebGLRenderer` sobre el elemento `<canvas>`.
- Se configura la escena con dos luces: una direccional que simula el sol (cálida, desde arriba-derecha) y una de relleno tenue que simula el reflejo de la Tierra en el espacio.
- La esfera del globo usa `MeshPhongMaterial` con tres texturas cargadas desde jsDelivr CDN (repositorio oficial de Three.js r128): mapa de color de la atmósfera terrestre, mapa de normales para relieve en costas y montañas, y mapa especular para que los océanos brillen diferente a los continentes.
- Una segunda esfera ligeramente mayor con `side: THREE.BackSide` y opacidad baja simula la atmósfera azulada.
- El campo de estrellas se genera con `BufferGeometry` de 4000 puntos con posiciones y colores aleatorios para simular variación de temperatura estelar.

**Control de cámara:**
- Eventos `mousedown/mousemove/mouseup` actualizan los ángulos de rotación `rotX` y `rotY`.
- El primer clic desactiva `autoRotate`, que de lo contrario gira el globo suavemente.
- El evento `wheel` controla el zoom dentro de unos límites (1.4× a 5×).

**Raycasting y tooltip:**
En cada movimiento del ratón (cuando no se arrastra), se convierte la posición del cursor a coordenadas normalizadas del dispositivo (NDC) y se lanza un rayo desde la cámara. Si el rayo intersecta algún marcador, se muestran nombre, altura y país de la montaña en el tooltip flotante.

**Ciclo de animación:**
`requestAnimationFrame` crea un bucle continuo que aplica la rotación acumulada al globo y al grupo de marcadores (ambos rotan juntos para mantener los pines en la posición correcta), actualiza la posición Z de la cámara según el zoom y renderiza la escena.

**Conversión de coordenadas:**
`latLonToVec3(lat, lon, r)` convierte latitud y longitud geográficas a un vector 3D sobre la superficie de la esfera usando la transformación esférica estándar. Se usa para posicionar tanto la base como la punta de cada marcador.

**Renderizado de marcadores:**
Por cada montaña se crean dos geometrías: un cilindro (`CylinderGeometry`) orientado radialmente desde la superficie como cuerpo del pin, cuya altura es proporcional a la altitud relativa dentro del rango; y una esfera en la punta. Los marcadores en el cuartil superior de altura del rango se colorean en cian (`#06b6d4`) para destacarlos visualmente. Toda la información de la montaña se almacena en `mesh.userData` para el tooltip.

**Caché en cliente:**
El objeto JavaScript `cache{}` almacena en memoria los resultados ya recibidos durante la sesión. Si el usuario vuelve a seleccionar un rango ya consultado, los marcadores se renderizan instantáneamente sin nueva petición al servidor.

---

### 3.3 `requirements.txt`

```
flask
gunicorn
```

- **Flask:** framework web minimalista que gestiona el enrutamiento HTTP y el renderizado de plantillas. Permite escribir el servidor en pocas líneas y es el estándar en los servicios de hosting gratuitos.
- **Gunicorn:** servidor WSGI de producción. El servidor de desarrollo de Flask (un solo hilo) no es apto para producción. Gunicorn arranca múltiples workers para gestionar peticiones concurrentes. Render lo invoca con `gunicorn app:app`.

---

### 3.4 `render.yaml`

```yaml
services:
  - type: web
    name: mountain-globe
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn app:app
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.0
```

Render lee este archivo al conectar el repositorio de GitHub y configura el servicio automáticamente:

- `buildCommand` se ejecuta una sola vez al desplegar: instala las dependencias.
- `startCommand` arranca el servidor en producción. `app:app` significa "del módulo `app.py`, usa el objeto `app`" (la instancia Flask).
- La versión de Python se fija explícitamente para evitar incompatibilidades entre versiones.

---

## 4. La consulta SPARQL

```sparql
SELECT ?mountain ?mountainLabel ?height ?lat ?lon ?country ?countryLabel WHERE {
  {
    SELECT ?mountain ?height ?lat ?lon ?country WHERE {
      { ?mountain wdt:P31 wd:Q8502 }
      UNION
      { ?mountain wdt:P31/wdt:P279 wd:Q8502 }
      ?mountain p:P2044/psn:P2044/wikibase:quantityAmount ?height .
      FILTER(?height >= 8000 && ?height < 9000)
      ?mountain wdt:P625 ?coord .
      BIND(geof:latitude(?coord)  AS ?lat)
      BIND(geof:longitude(?coord) AS ?lon)
      FILTER(?lat >= -90 && ?lat <= 90 && ?lon >= -180 && ?lon <= 180)
      OPTIONAL { ?mountain wdt:P17 ?country . }
    }
    ORDER BY DESC(?height)
    LIMIT 300
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,es" . }
}
ORDER BY DESC(?height)
```

| Elemento | Propiedad Wikidata | Significado |
|---|---|---|
| `wd:Q8502` | — | Clase "montaña" en Wikidata |
| `wdt:P31` | instancia de | El ítem es una montaña |
| `wdt:P279` | subclase de | Un nivel de subclase (volcán, cumbre...) |
| `p:P2044/psn:P2044` | elevación normalizada | Altitud en unidades SI (metros) |
| `wdt:P625` | coordenadas geográficas | Punto WKT con latitud y longitud |
| `wdt:P17` | país | País al que pertenece la montaña |
| `geof:latitude/longitude` | — | Funciones GeoSPARQL para extraer lat/lon del punto |
| `SERVICE wikibase:label` | — | Resuelve etiquetas de texto en inglés y español |

---

## 5. Decisiones de diseño y optimizaciones

### Problema de rendimiento: timeouts en Wikidata

El endpoint público SPARQL de Wikidata tiene un tiempo límite de 60 segundos. Las consultas que recorren el grafo de subclases de forma transitiva (`wdt:P279*`) son especialmente costosas porque evalúan miles de nodos.

**Solución adoptada:** reemplazar `wdt:P31/wdt:P279*` por una UNION de dos patrones explícitos (instancia directa + un nivel de subclase). Esto reduce el tiempo de consulta de forma significativa manteniendo la cobertura de los principales tipos de montaña en Wikidata.

### Problema de duplicados

Wikidata permite que un mismo ítem tenga múltiples declaraciones para la misma propiedad con diferentes valores (distintas mediciones históricas de la altitud del Everest, por ejemplo) y múltiples países (montañas en fronteras). Esto genera filas duplicadas en los resultados SPARQL.

**Solución adoptada:** deduplicación en Python tras recibir la respuesta, agrupando por coordenadas geográficas redondeadas a 3 decimales. Para cada grupo se conserva la mayor altitud y se concatenan los nombres de país.

### Problema de unidades

Algunos ítems de Wikidata almacenan la altitud en pies en lugar de metros. El prefijo `psn:` (normalized statement) hace que Wikidata devuelva automáticamente el valor convertido a la unidad SI del sistema internacional (metros para la propiedad P2044). Los ítems con unidades no convertibles no generan valor para `?height` y quedan excluidos automáticamente por el FILTER.

### Problema de montañas extraterrestres

Wikidata clasifica también relieves de otros planetas (volcanes de Marte, colinas de Plutón...) como subclases de montaña. El filtro de coordenadas terrestres `FILTER(?lat >= -90 && ?lat <= 90 && ?lon >= -180 && ?lon <= 180)` los excluye, ya que los sistemas de coordenadas de otros cuerpos celestes usan rangos distintos.

### Optimización de etiquetas

El `SERVICE wikibase:label` de Wikidata resuelve las etiquetas de texto para cada ítem del resultado. Colocarlo fuera de la subconsulta garantiza que solo se ejecuta sobre las 300 montañas ya filtradas y ordenadas, no sobre todos los candidatos intermedios que el motor evalúa internamente.

### Caché en disco + precarga

La combinación de caché en disco (persistente entre peticiones) con TTL de 24 horas y precarga en segundo plano de los rangos más usados al arrancar el servidor minimiza el número de consultas a Wikidata y hace que la experiencia del usuario sea fluida en la mayoría de los casos.
