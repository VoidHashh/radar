# SPEC — Radar de oportunidades en Shopify App Store

> **Estado (2026-10-08): radar cerrado (D33).** Los Términos del servicio de Shopify prohíben acceder a los Servicios o
> vigilar su contenido con robots, spiders, scrapers u otros medios automatizados. Se cancela la Fase 3, no se programan
> ejecuciones y se han borrado la caché y los textos literales de las reseñas. Se conservan los informes, las tablas
> agregadas y las clasificaciones sin texto literal. Ver `docs/decisions.md`.

Lee este documento completo antes de escribir código. Trabaja por fases. Al final de cada fase, PARA y muéstrame el resultado.

Las decisiones tomadas tras cada fase, y su motivo, están en `docs/decisions.md`.

## 1. Objetivo

Herramienta interna (no es un producto) que detecta apps de Shopify con muchos usuarios y quejas recientes repetidas, para encontrar problemas que una app pequeña pueda resolver en 4 semanas o menos.

Salida final: un informe semanal en Markdown con las 20 mejores oportunidades. Cada afirmación del informe enlaza a las reseñas originales.

## 2. Reglas para Claude Code

1. No inventes selectores, URLs ni parámetros. Todo lo que dependa de la web de Shopify se verifica en la Fase 0 y queda documentado en `docs/recon.md`. Si algo no se pudo verificar, pregúntame.
2. Prefiere datos estructurados (JSON-LD, JSON embebido) a selectores CSS, si existen.
3. Rastreo educado:
   - Ritmo base: 1 petición cada 2-3 s.
   - User-Agent identificable.
   - Reintentos con backoff exponencial ante 429/5xx.
   - Caché en disco del HTML descargado. Nunca se cachea una página de verificación de Cloudflare.
   - Si `robots.txt` prohíbe una ruta necesaria, PARA e infórmame.
   - Cloudflare (403 de verificación, cabecera `cf-mitigated: challenge`):
     - pausa de 15 min y reintento de la misma URL;
     - tras 3 bloqueos seguidos: guardar estado, parar y reanudar en la siguiente ejecución;
     - prohibido esquivarlo: sin proxies, sin rotación de User-Agent, sin navegadores automatizados.
4. Datos mínimos. No guardes nombre de tienda ni ningún dato del autor de la reseña, salvo país y tiempo de uso. Tampoco se guarda el país del desarrollador: el texto "Based in" de la ficha depende de la IP de quien visita.
5. Dependencias: Python 3.12, `httpx`, `selectolax`, `typer`, `pyyaml`, `pytest`. Nada más sin preguntarme. (`anthropic` ya no hace falta: no se usa la API, D10.)
6. Sin Docker. Usa venv y cron/systemd timer.
7. No se usa la API de Anthropic (D10). Las clasificaciones las hace Claude Code en la sesión.
   Las credenciales de GitHub y del servidor OVH están en `accesos.txt`, que también queda fuera de git.
8. Los tests usan fixtures HTML guardados en la Fase 0, sin red.
9. **Términos de uso antes que nada (D33).** Cualquier fuente nueva exige revisar primero sus términos de uso (y los de su
   API, si la tiene). Si prohíben la recogida o el acceso automatizado, la fuente no se usa, aunque robots.txt lo permita.
   Se cita el texto literal en `docs/decisions.md` antes de escribir código.

## 3. Hechos verificados en la Fase 0

El detalle, con selectores y ejemplos, está en `docs/recon.md`.

- Catálogo: `https://apps.shopify.com/sitemap_apps_en.xml`, con 27.662 apps el 2026-10-05.
- Listados de categoría: `/categories/{category}/all?page=N`, con 24 apps por página. Cada tarjeta trae el handle, la nota y el número de reseñas, pero no la distribución por estrellas.
- La ficha de la app (`/{handle}`) trae JSON-LD con nombre, desarrollador, nota y número de reseñas. En el HTML trae la distribución con recuentos exactos por estrella, las categorías y los planes de precio.
- Reseñas: `/{handle}/reviews` con 10 por página. Parámetros verificados:
  - `page`,
  - `sort_by=newest`,
  - `ratings[]=N`, que se puede repetir.
- Cada reseña muestra:
  - fecha, que puede ser la de edición ("Edited ..."),
  - país,
  - tiempo de uso, que a veces falta,
  - texto completo,
  - respuesta del desarrollador con su fecha, si existe.
- Enlace permanente: `https://apps.shopify.com/reviews/{review_id}`.
- Las reseñas aparecen en su idioma original, sin traducción.
- La nota mostrada no siempre coincide con la media de la distribución.

## 4. Estructura del proyecto

```
radar/
  pyproject.toml
  config.yaml          # umbrales, pesos, presupuesto, modelos
  keywords.yaml        # palabras clave por categoría
  radar/
    cli.py             # comandos typer
    pipeline.py        # pasos del pipeline, reanudables
    http.py            # cliente: rate limit, caché, reintentos, Cloudflare
    db.py              # esquema SQLite y helpers
    stores/base.py     # interfaz StoreAdapter
    stores/shopify.py  # adaptador Shopify (parsers + descargas)
    keywords.py        # marcado por palabras clave
    handoff.py         # exporta lotes para Claude Code e importa y valida sus resultados
    score.py
    competition.py     # competencia por categoría (D15, D19)
    flags.py           # plataformas (D17) e incidentes puntuales (D18)
    report.py
  tests/fixtures/      # HTML guardado en Fase 0
  docs/recon.md
  docs/decisions.md
  docs/prompts/        # instrucciones que sigue Claude Code en los pasos 6, 7 y 8b
  data/llm/            # lotes exportados y resultados de Claude Code (gitignored)
  data/radar.sqlite    # gitignored
  cache/               # gitignored
  reports/             # informes semanales
```

`StoreAdapter` define estos métodos:

- `list_apps()`,
- `list_category(category)`,
- `fetch_app_meta(app)`,
- `fetch_reviews(app, since, max_pages, ratings=None)`,
- `estimate_total_since(app, since, review_count)`.

Así, la Chrome Web Store se añade después sin tocar el resto.

## 5. Esquema SQLite

```sql
CREATE TABLE apps (
  app_id TEXT PRIMARY KEY,          -- slug/handle
  store TEXT NOT NULL DEFAULT 'shopify',
  name TEXT, developer TEXT, url TEXT,
  categories TEXT,                  -- JSON
  pricing TEXT,                     -- JSON con planes
  first_seen TEXT, last_seen TEXT
);
CREATE TABLE app_listings (         -- tarjetas de los listados de categoría
  app_id TEXT, category TEXT, run_id TEXT,
  rating_shown REAL, review_count INTEGER, position INTEGER,
  PRIMARY KEY (app_id, category, run_id)
);
CREATE TABLE app_snapshots (        -- serie temporal para tendencias
  app_id TEXT, taken_at TEXT, run_id TEXT,
  rating_shown REAL,                -- nota que muestra Shopify (JSON-LD)
  rating_computed REAL,             -- media calculada con n_1..n_5
  review_count INTEGER,
  n_5 INTEGER, n_4 INTEGER, n_3 INTEGER, n_2 INTEGER, n_1 INTEGER,
  PRIMARY KEY (app_id, taken_at)
);
CREATE TABLE app_windows (          -- ventana de 365 días por app y ejecución
  app_id TEXT, run_id TEXT,
  total_12m INTEGER,
  method TEXT,                      -- 'exact' | 'estimated'
  negatives_12m INTEGER,
  PRIMARY KEY (app_id, run_id)
);
CREATE TABLE reviews (
  review_id TEXT PRIMARY KEY,
  app_id TEXT, rating INTEGER,
  date_shown TEXT,                  -- fecha visible; puede ser la de edición
  edited INTEGER,                   -- 1 si la página muestra "Edited"
  country TEXT, usage_duration TEXT,
  body TEXT, has_dev_reply INTEGER, dev_reply_date TEXT,
  fetched_at TEXT
);
CREATE TABLE review_flags (         -- resultado de keywords
  review_id TEXT, category TEXT, matched TEXT,
  PRIMARY KEY (review_id, category)
);
CREATE TABLE review_llm (           -- clasificación de Claude
  review_id TEXT PRIMARY KEY,
  category TEXT, specific_problem TEXT,
  feature_requested TEXT, alternative_mentioned TEXT,
  severity INTEGER, language TEXT,  -- ISO 639-1
  model TEXT, batch_id TEXT, created_at TEXT
);
CREATE TABLE clusters (
  app_id TEXT, run_id TEXT, cluster_label TEXT,
  n_reviews INTEGER, review_ids TEXT,  -- JSON
  buildability TEXT,                   -- S/M/L
  buildability_reason TEXT
);
CREATE TABLE scores (
  app_id TEXT, run_id TEXT,
  demand REAL, pain REAL, recent_pain REAL, momentum REAL,
  recurrence REAL, buildability REAL, neglect REAL,
  total REAL,
  PRIMARY KEY (app_id, run_id)
);
CREATE TABLE replicables (         -- paso 8b
  app_id TEXT, run_id TEXT,
  core TEXT,                        -- qué hace el núcleo, 1-2 frases
  buildability TEXT, buildability_reason TEXT,
  improvements TEXT,                -- JSON: lista de mejoras
  min_paid_usd_month REAL,
  demand REAL, price REAL, buildability_score REAL, total REAL,
  PRIMARY KEY (app_id, run_id)
);
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
  phase TEXT, notes TEXT, llm_cost_usd REAL
);
```

## 6. Pipeline

1. **Inventario.**
   - Leer el sitemap y guardar todas las apps en `apps`.
   - Recorrer los listados `/all` de todas las categorías salvo las raíces y guardar cada tarjeta en `app_listings`, con nota y número de reseñas. Las categorías intermedias devuelven 404 y se ignoran.
   - Con `--category X`, recorrer solo esa categoría.
2. **Metadatos.**
   - Una sola petición por app: la ficha. No se descarga la primera página de reseñas.
   - Solo se pide la ficha si el número de reseñas del listado es `>= meta.min_listing_reviews` (por defecto 50).
   - Guardar un registro en `app_snapshots` con `rating_shown`, `rating_computed` y `n_1..n_5`.
3. **Preselección** (umbrales en `config.yaml`, valores por defecto):
   - `review_count >= 50`
   - `100 * (n_1 + n_2) / review_count >= 10` o `rating_computed <= 4.2`
   - Filtro opcional por categoría.
4. **Reseñas.** Para las apps preseleccionadas, ventana de 365 días ordenada con `sort_by=newest`:
   - Si `review_count <= 300` (`window.exact_max_reviews`): descargar todas las reseñas de la ventana. `total_12m` es exacto (`method = 'exact'`).
   - Si `review_count > 300`:
     - descargar solo las negativas (`ratings[]=1&ratings[]=2`) de la ventana, con un máximo de `window.max_pages` páginas (por defecto 30);
     - estimar `total_12m` con una búsqueda binaria sobre las páginas `sort_by=newest` (`method = 'estimated'`).
   - Guardar `total_12m`, `method` y `negatives_12m` en `app_windows`.
   - Se conservan todas las reseñas descargadas, en cualquier idioma.
   - Solo las de 1-2 estrellas pasan a los pasos 5 y 6.
5. **Palabras clave.** Marcar las reseñas de 1-2 estrellas según `keywords.yaml`: coincidencia insensible a mayúsculas y por palabra completa. Es una señal más: no filtra lo que pasa a Claude.
6. **Clasificación con Claude Code** (sin API, D10):
   - Se clasifican todas las reseñas de 1-2 estrellas **con texto** de las apps preseleccionadas, tengan o no marcas de palabras clave y estén en el idioma que estén.
   - Las reseñas que solo tienen estrellas (texto vacío) no se clasifican: sin contenido no se puede identificar el problema.
   - `radar classify export` escribe lotes JSONL en `data/llm/classify/`. Claude Code los procesa siguiendo `docs/prompts/classify.md` y escribe un fichero de resultados por lote. `radar classify import` valida e importa.
   - Salida JSON estricta por reseña:
     ```json
     {"review_id": "123",
      "category": "broken|pricing|support|missing_feature|abandoned|performance|other",
      "specific_problem": "≤20 palabras, concreto, en inglés",
      "feature_requested": "texto o null",
      "alternative_mentioned": "nombre de app o null",
      "severity": 1,
      "language": "en"}
     ```
     `severity` va de 1 a 3. `language` es el código ISO 639-1 del texto de la reseña.
   - `review_llm.model = 'claude-code'` y `batch_id` = nombre del lote, para poder reanudar por lote.
7. **Agrupación con Claude Code** (un fichero por app en `data/llm/cluster/`, instrucciones en `docs/prompts/cluster.md`):
   - Se agrupan todas las apps preseleccionadas, aunque tengan pocas reseñas en la ventana: pueden aportar información valiosa.
   - Agrupar los `specific_problem` de la app en 5 grupos como máximo, cada uno con su número de reseñas y sus `review_ids`.
   - Para el grupo principal, estimar `buildability`:
     - S: 1 desarrollador, 4 semanas o menos.
     - M: 1-3 meses.
     - L: más.
   - Incluir una justificación de 1-2 frases.
8b. **Apps replicables** (sección aparte, D12):
   - Candidatas: `review_count >= replicables.min_reviews`, algún plan de pago con precio mensual `>= replicables.min_paid_usd_month` y desarrollador distinto de Shopify.
   - Claude Code lee la ficha de cada candidata (`docs/prompts/replicables.md`) y devuelve: qué hace el núcleo, `buildability` S/M/L con justificación y qué se mejoraría.
   - Puntuación propia (pesos en `config.yaml`): `demand` (igual que en el paso 8), `price = min(1, log10(precio mensual más bajo de pago) / log10(300))`, `buildability` y `competition` (D15). El informe muestra los competidores fuertes y las alternativas gratuitas de su categoría.
   - Se guarda en la tabla `replicables`.
8. **Puntuación.** Cada componente va normalizado de 0 a 1 y se guarda por separado, para que el ranking sea auditable:
   - `demand = min(1, log10(review_count)/4)`
   - `pain = (n_1 + n_2) / review_count`
   - `recent_pain = negatives_12m / total_12m` (de `app_windows`)
   - `momentum` = tasa diaria de negativas de los últimos 90 días / tasa diaria de los días 90-365. Se escala a 0-1 con tope en 3x.
   - `recurrence` = tamaño del grupo principal / total de negativas clasificadas (solo las que tienen texto)
   - `buildability`: S = 1, M = 0,6, L = 0,2
   - `neglect` = proporción de negativas sin respuesta del desarrollador en los últimos 12 meses
   - Donde haga falta una nota, se usa `rating_computed`.
   - `negatives_12m`, `recent_pain`, `momentum` y `neglect` cuentan todas las negativas, también las que no tienen texto.
   - No hay un mínimo de `total_12m`: una app con pocas reseñas recientes se puntúa igual que las demás.
   - Ajustes D13-D16 (docs/decisions.md):
     - `recent_pain`, `recurrence` y `neglect` con media bayesiana `(x·n + p0·m)/(n + m)`, `m = scoring.bayes_m` (10) y `p0` la proporción agregada de las apps preseleccionadas.
     - `volume = log10(1 + negatives_12m)`, normalizado por el máximo de la ejecución.
     - `competition = 1 - presión` de la categoría principal: 0,6·norm(log10(1 + apps con ≥ 100 reseñas)) + 0,4·cuota del líder (D19), con los listados ya descargados.
     - Apps de plataforma, canal o pago (`platforms.developers`) se puntúan, pero quedan fuera del ranking y se muestran como contexto (D17).
     - Incidente puntual (≥ 60 % de las negativas de 12 meses en 30 días y del mismo grupo, con ≥ 5 negativas): `recurrence` y `momentum` ×0,3 (D18).
     - `low_sample` marca las apps con menos de 5 negativas en 12 meses; no se excluyen.
   - `total` = suma ponderada. Pesos por defecto en `config.yaml`:
     | Componente | Peso |
     |---|---|
     | demand | 0,105 |
     | pain | 0,07 |
     | recent_pain | 0,105 |
     | momentum | 0,07 |
     | recurrence | 0,14 |
     | buildability | 0,14 |
     | neglect | 0,07 |
     | volume | 0,15 |
     | competition | 0,15 |
8c. **Necesidades no cubiertas** (D20): Claude Code une, dentro de cada categoría, las funciones pedidas y los fallos (`missing_feature`, `broken`) equivalentes de apps distintas, sin plataformas ni incidentes, y estima la construibilidad de una app independiente que haga solo eso. El informe muestra las que tienen ≥ 5 reseñas y ≥ 2 apps.
9. **Informe.** `reports/AAAA-SS.md`:
   - Tabla con las 20 primeras: app, enlace, reseñas, nota, % de 1-2 estrellas, puntuación total y componentes.
   - Por cada oportunidad:
     - grupos de quejas con su número de reseñas y 2-3 enlaces permanentes de ejemplo,
     - funciones pedidas,
     - alternativas mencionadas,
     - `buildability` con su justificación.
   - Sección aparte con las apps replicables: app, enlace, reseñas, nota, plan de pago más barato, núcleo, `buildability` y mejoras.
   - Exportar también `reports/AAAA-SS.csv` (oportunidades) y `reports/AAAA-SS-replicables.csv`.
   - `radar report --check-links` comprueba un enlace permanente por oportunidad con una petición real.

## 7. keywords.yaml inicial (ampliable)

```yaml
broken: ["stopped working", "doesn't work", "not working", "broke", "broken", "bug", "glitch", "crash", "error"]
pricing: ["too expensive", "price increase", "raised the price", "overpriced", "hidden fee", "charged me", "refund", "billing"]
support: ["no response", "no support", "never replied", "never responded", "ignored", "terrible support", "slow support"]
missing_feature: ["wish it had", "would be great if", "missing", "lacks", "no option to", "doesn't support", "can't customize"]
abandoned: ["no updates", "abandoned", "outdated", "not maintained"]
switching: ["switched to", "moved to", "uninstalled", "alternative", "better app"]
performance: ["slow", "slows down", "page speed", "load time"]
```

## 8. Comandos CLI

```
radar inventory [--category X]
radar meta [--category X] [--limit N]
radar reviews [--since-days 365]
radar keywords
radar classify export [--batch-size 150] | radar classify import
radar cluster export | radar cluster import
radar replicables export | radar replicables import
radar needs export | radar needs import
radar score
radar report [--check-links]
radar run-all [--category X]     # todo en orden, reanudable
```

## 9. Fases y criterios de terminado

**Fase 0. Reconocimiento.** HECHA (2026-10-05). Resultado en `docs/recon.md`.
- Revisar `robots.txt` de apps.shopify.com: rutas permitidas y sitemap declarado.
- Localizar el sitemap de apps y contar las entradas.
- En 3 apps (una grande, una mediana, una pequeña) documentar:
  - campos disponibles en la página de la app,
  - paginación, orden y filtro por estrellas en las reseñas,
  - bloque de distribución por estrellas,
  - JSON-LD o JSON embebido, si existe.
- Comprobar el comportamiento a 1 petición cada 2 s: códigos de respuesta y bloqueos.
- Guardar 5 fixtures en `tests/fixtures/`.
- Terminado cuando `docs/recon.md` esté completo. PARA.

**Fase 1. Recogida.**
- Pasos 1-5 sobre 2 categorías pequeñas de prueba.
- Tests de los parsers con fixtures.
- Comprobar y documentar si los listados de categoría cubren las apps del sitemap, sin duplicados ni apps fuera.
- Terminado cuando `radar run-all --category X` funcione de principio a fin y muestre:
  - número de apps preseleccionadas,
  - número de reseñas descargadas,
  - recuento de marcas por categoría.

  PARA.

**Fase 2. Análisis.** HECHA (2026-10-05). Primer informe: `reports/2026-41.md`.
- Pasos 6-9.
- Terminado cuando:
  - el primer informe esté generado,
  - cada oportunidad tenga enlaces de ejemplo que funcionan,
  - se muestre cuántas reseñas y apps procesó Claude Code (sustituye al coste de la API, D10),
  - el informe incluya la sección de apps replicables.

  PARA.

**Fase 3. Automatización.** CANCELADA (D33): no se programan ejecuciones.
- Ejecución semanal con un timer.
- Ejecución incremental: solo reseñas nuevas desde la última ejecución.
- Snapshots semanales para ver tendencias.
- Log de errores.

**Fase 4 (después).** Adaptador para otras tiendas, sobre la misma interfaz. Fase 4A (Atlassian) parada en el reconocimiento por sus términos de uso (D32).
