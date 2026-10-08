# Fase 0 — Reconocimiento de apps.shopify.com

Fecha del reconocimiento: 2026-10-05, entre 12:45 y 13:02 (hora local, CF-Ray `MAD`).
Cliente usado: `httpx` 0.28.1 con `follow_redirects=True`, cabeceras
`User-Agent: RadarResearchBot/0.1 (internal market research; ~1 req/2.5s; non-commercial)` y
`Accept-Language: en-US,en;q=0.9`. Parser: `selectolax` 1.0.0.

> **selectolax 1.0 requiere el parser lexbor.** `from selectolax.parser import HTMLParser` (Modest)
> lanza `ImportError` desde la 1.0. Hay que usar `from selectolax.lexbor import LexborHTMLParser`.

Todo lo que aparece aquí se ha comprobado con peticiones reales. Lo que no se pudo verificar
está en la sección 9.

---

## 1. robots.txt

URL: `https://apps.shopify.com/robots.txt` (fixture: `tests/fixtures/robots.txt`).

Reglas para `User-agent: *`:

```
Disallow: /internal/
Disallow: /services/
Disallow: *q=*
Disallow: /*?*shpxid=*
Disallow: /*?*auth=*

Sitemap: https://apps.shopify.com/sitemap.xml
```

Conclusiones:

- Están **permitidas** todas las rutas que necesita el pipeline: `/{handle}`, `/{handle}/reviews`,
  `/reviews/{id}`, `/categories/...`, los sitemaps y los parámetros `page`, `sort_by` y `ratings[]`.
- `Disallow: *q=*` bloquea cualquier URL que contenga `q=`. Implica que **no se puede usar la
  búsqueda** (`/search?q=...`). Además, ningún parámetro que construyamos puede terminar en `q`
  (por ejemplo, `freq=`). El cliente HTTP debe comprobar esto antes de cada petición.
- No hay `Crawl-delay`.
- Hay bloques específicos para `ChatGPT-User`, `Claude-User`, `Google-Agent` y `Perplexity-User`,
  con las mismas reglas salvo `*q=*`. No afectan a nuestro User-Agent.

## 2. Sitemap y catálogo

- Índice declarado: `https://apps.shopify.com/sitemap.xml` (fixture: `sitemap_index.xml`).
  Es un `<sitemapindex>` con **184 sitemaps**: 8 tipos × 23 idiomas.
- Tipos en inglés: `sitemap_apps_en.xml`, `sitemap_partners_en.xml`, `sitemap_categories_en.xml`,
  `sitemap_collections_en.xml`, `sitemap_stories_en.xml`, `sitemap_extensions_en.xml`,
  `sitemap_built_in_features_en.xml`, `sitemap_category_features_en.xml`.
- **Catálogo de apps: `https://apps.shopify.com/sitemap_apps_en.xml`**
  - 4,75 MB, una sola descarga, sin paginación.
  - **27.662 entradas**, todas únicas. La cifra de terceros de "más de 22.000" se queda corta.
  - Formato de cada entrada: `<loc>https://apps.shopify.com/{handle}</loc>` más `<lastmod>`
    (AAAA-MM-DD), `<changefreq>daily</changefreq>` y `<priority>0.8</priority>`.
  - Todas las rutas tienen un solo segmento, así que `handle = loc.rsplit("/", 1)[1]`.
  - `lastmod` va de 2026-06-16 a 2026-10-04.
  - Fixture con las primeras 50 entradas: `sitemap_apps_en_sample.xml`.
- Categorías: `sitemap_categories_en.xml` tiene 161 URLs del tipo `/categories/{category_handle}`,
  que incluyen categorías padre e hijas.

## 3. Página de la app: `https://apps.shopify.com/{handle}`

Verificado en tres apps:

| Tamaño | Handle | Reseñas | Nota | % de 1-2★ | Fixture |
|---|---|---|---|---|---|
| Grande | `klaviyo-email-marketing` | 3.352 | 4,7 | 8,9 | `app_klaviyo-email-marketing.html` |
| Mediana | `sufio` | 466 | 4,9 | 2,4 | `app_sufio.html` |
| Pequeña | `dropify-5` | 53 | 3,5 | 34,0 | `app_dropify-5.html` |

### 3.1 JSON-LD (fuente preferida)

Cada página de app (y también cada página de reseñas y cada permalink) lleva **un** bloque
`<script type="application/ld+json">`:

```json
{"@context":"https://schema.org","@type":"SoftwareApplication",
 "name":"Klaviyo: Email Marketing & SMS","description":"...","image":["..."],
 "operatingSystem":"Shopify","applicationCategory":"DeveloperApplication",
 "brand":"Klaviyo",
 "aggregateRating":{"@type":"AggregateRating","ratingValue":4.7,"ratingCount":3352}}
```

- Da `name`, `brand` (nombre del desarrollador), `ratingValue` (1 decimal) y `ratingCount`.
- **No** incluye reseñas, distribución por estrellas, precios ni categorías.
- No hay otro JSON embebido útil. Los demás scripts son analítica e i18n.

### 3.2 Campos que solo están en el HTML

| Campo | Selector o patrón verificado | Notas |
|---|---|---|
| Distribución exacta por estrellas | `a[aria-label$="total reviews"][href*="ratings%5B%5D=N"]`; el número exacto está en `aria-label="2938 total reviews"` y la estrella en el `href` | En las 3 apps la suma coincide con `ratingCount`. Usar esto y no el %. |
| Distribución en % | texto `"(\d+)% of ratings are (\d) stars?"` | Redondeado a entero. Solo como comprobación. |
| Categorías principales | `a[href*="/categories/"][href*="surface_type=app_details"]` **sin** `feature_handles` en el `href` | Klaviyo: Email marketing y SMS marketing. Sufio: Invoices and receipts y Taxes. Dropify: Sourcing options - Other. El handle de la categoría es el último segmento de la ruta. |
| Etiquetas de funciones | mismos enlaces **con** `feature_handles%5B%5D=...` | No son categorías. Hay que excluirlas. |
| Planes de precio | cada `[data-pricing-component-target="cardHeading"]` contiene `[data-test-id="name"]`, `[data-pricing-component-target="cardHeadingPrice"] h3[aria-label]` (por ejemplo `"$19/month"` o `"Free to install"`) y, opcionalmente, `[data-test-id="additional-charges"]` | El `span[data-test-id="price"]` solo existe en planes gratuitos, así que hay que usar el `aria-label` del `h3`. Ejemplo de Sufio: STARTER $7/month, GROWTH $19/month, PROFESSIONAL $49/month y PREMIUM $129/month. |
| Desarrollador | `a[href*="/partners/"]` → `https://apps.shopify.com/partners/{partner_handle}` | El texto coincide con `brand` del JSON-LD. |
| Fecha de lanzamiento | `<p>Launched</p><p>September 20, 2012 · Changelog</p>` | Regex: `Launched\s*</p>\s*<p[^>]*>\s*([A-Z][a-z]+ \d{1,2}, \d{4})`. |
| URL canónica | `link[rel=canonical]` | Coincide con `https://apps.shopify.com/{handle}`. |
| 3 reseñas destacadas | misma estructura que en la página de reseñas | Son las "más relevantes", no las más recientes. No sirven para el pipeline. |

Otros campos visibles que no necesitamos: idiomas, "Works with", permisos de datos,
dirección del desarrollador y apps similares.

### 3.3 Trampas detectadas

- **"Based in Spain" no es el país del desarrollador.** Aparece junto a
  "Popular with stores like yours" y depende de la IP del visitante (Klaviyo tiene su sede en Boston).
  No hay que parsearlo.
- **La nota mostrada no siempre es la media de la distribución.** En Klaviyo la media calculada es
  4,61, pero se muestran 4,7 tanto en el JSON-LD como en el HTML. En Sufio y Dropify sí coinciden con el
  redondeo. Shopify debe de aplicar alguna ponderación. Guardaremos `rating` tal cual aparece y
  calcularemos `pct_*` con los recuentos exactos.
- Bloque **"What merchants think"**: es un resumen generado por Shopify Magic. Solo aparece en apps con
  100 o más reseñas y nota de al menos 4,0. Está fuera de `div[data-merchant-review]`, así que no se confunde con una reseña.
- En la página de reseñas aparece un texto oculto, "We are having trouble loading your results", que es
  una plantilla de error y no indica ningún fallo.

## 4. Página de reseñas: `https://apps.shopify.com/{handle}/reviews`

### 4.1 Parámetros verificados

| Parámetro | Valores | Verificación |
|---|---|---|
| `page` | 1..N | 10 reseñas por página. Klaviyo: 3.352 reseñas, página 336 con 2 reseñas, página 337 con **200 y 0 reseñas** (no hay 404). Dropify: 53 reseñas, página 6 con 3 y página 7 vacía. El enlace a la siguiente página está en `rel="next"`. |
| `sort_by` | `relevance` (por defecto), `newest` | Con `newest`, el `<option value="newest" selected>` aparece marcado en la respuesta y los `review_id` bajan de forma estrictamente monótona en las 15 páginas probadas. Sin `sort_by` (o con `relevance`) el orden no es cronológico. |
| `ratings[]` (codificado `ratings%5B%5D`) | 1..5, **repetible** | `ratings%5B%5D=1&ratings%5B%5D=2` devuelve la unión de 1★ y 2★. Verificado en Sufio con 7 de 1★ y 3 de 2★ en la primera página. Se combina con `sort_by` y `page`. |
| `locale` | `es`, `de`, ... | Existe, pero cambia el idioma de la interfaz y el formato de las fechas. **No usarlo.** Con `Accept-Language: en-US` las fechas llegan en inglés. |

URL canónica para el pipeline:

```
https://apps.shopify.com/{handle}/reviews?sort_by=newest&page={n}
https://apps.shopify.com/{handle}/reviews?ratings%5B%5D=1&ratings%5B%5D=2&sort_by=newest&page={n}
```

Condición de fin de paginación: **no hay `a[rel="next"]`**. Ese enlace existe en las páginas
intermedias (`aria-label="Go to Page 6"`) y falta tanto en la última página como en la vacía.
Como red de seguridad, también se para si la página no contiene ningún `div[data-merchant-review]`.

### 4.2 Estructura de cada reseña

Contenedor: `div[data-merchant-review][data-review-content-id="{review_id}"]`, 10 por página.
Validado con 257 reseñas únicas de las tres apps.

| Campo | Cómo se extrae | Cobertura en la muestra |
|---|---|---|
| `review_id` | atributo `data-review-content-id` (también `data-review-id` del botón de compartir) | 257/257 |
| Permalink | `[data-review-share-link]` → `/reviews/{review_id}` → `https://apps.shopify.com/reviews/{review_id}` | 257/257. Se probó un permalink y devuelve 200 con una única reseña. |
| `rating` | `[aria-label$="out of 5 stars"]` → `"1 out of 5 stars"` | 257/257 |
| Fecha | primer `div` hermano del bloque de estrellas: `"October 1, 2026"` o `"Edited October 2, 2026"` | 257/257. Formato `%B %d, %Y` (inglés). |
| Texto | `[data-truncate-review] [data-truncate-content-copy]`, texto de sus `<p>` | 257/257. **El texto completo está en el HTML.** El botón "Show more" solo afecta al CSS. La reseña más larga de la muestra tiene 2.087 caracteres. |
| Nombre de la tienda | `span[title]` junto al botón de compartir | **No se guarda** (regla 4). |
| País | `div` tras la fila del nombre de tienda | 257/257. Ejemplos: "United States", "Colombia" o "United Kingdom". |
| Tiempo de uso | `div` siguiente: `"{prefijo }{N} {unidad} using the app"` | **252/257**: en 5 reseñas falta. Prefijos: About, Over, Almost o ninguno. Unidades: minute(s), hour(s), day(s), month(s), year(s). |
| Respuesta del desarrollador | `[data-merchant-review-reply]` contiene `[data-reply-id]` | 185/257 con respuesta. |
| Fecha de la respuesta | texto `"{Developer} replied\n{Month D, YYYY}"` en la cabecera de la respuesta, con regex `replied\s+(.+)$` | 185/185 |

**Reseñas sin texto (Fase 1).** Hay valoraciones solo con estrellas: el HTML trae `<p></p>` y el texto no
está en ningún otro lugar de la página. En la muestra de la Fase 1 hay 55 de 118 reseñas sin texto, 45 de
ellas de una sola app (ez-preorder, 45 de 63). Entre las negativas son 6 de 22. Cuentan para `negatives_12m`,
pero Claude no podrá clasificarlas. Queda como pregunta para la Fase 2.

Al ser un orden y no un campo, el país y el tiempo de uso no tienen atributo propio.
Hay que identificarlos por patrón: el tiempo de uso termina en `using the app` y el país es el otro `div`.

### 4.3 Fechas editadas

- 7 de las 257 reseñas muestran `"Edited {fecha}"`. La fecha visible es la de **edición**.
- El orden `newest` sigue la fecha de **creación**, que coincide con el orden de `review_id`.
  Por ejemplo, en Sufio `Edited September 11, 2026` aparece entre reseñas de julio.
- La fecha de creación original **no aparece** en la página.
- Consecuencia para el corte de 365 días: el criterio de parada debe basarse en reseñas
  **no editadas**. La fecha de una reseña editada es siempre igual o posterior a su creación.

### 4.4 Idioma

- Las reseñas aparecen en su **idioma original**, sin traducción. No se encontró ninguna marca de "Translated".
- 16 de las 257 reseñas están en español, casi todas de Dropify (Colombia).
- `keywords.yaml` está en inglés y no las marcará. La clasificación con Claude sí las cubre.

### 4.5 Volumen por app (para dimensionar `max_pages`)

| App | Ritmo observado | Páginas para 365 días (todas) | Páginas para 365 días (solo 1-2★) |
|---|---|---|---|
| Klaviyo | 150 reseñas del 18-ago al 4-oct (≈3,2/día) | **≈117** | ≈6 (20 negativas del 15-may al 1-oct) |
| Sufio | 10 reseñas del 19-ago al 30-sep | ≈9 | 1 |
| Dropify | 53 en total, 6 páginas | 3 (≈25 reseñas desde oct-2025) | 1 |

Con `max_pages = 30`, en una app grande como Klaviyo solo se cubren unos 3 meses, no 365 días.
Ver la pregunta 1 de la sección 9.

## 5. Permalink: `https://apps.shopify.com/reviews/{review_id}`

- Verificado con `/reviews/2380223`: responde 200, sin redirección.
- Muestra la cabecera de la app (nombre, nota, distribución y JSON-LD) y **una sola reseña** con la misma estructura.
- No contiene enlace al handle de la app, pero el JSON-LD da el nombre.
- Fixture: `review_permalink_2380223.html`.

## 6. Listados de categoría: `https://apps.shopify.com/categories/{category_handle}/all`

No lo pedía la Fase 0, pero puede abaratar mucho la Fase 1.

- `/categories/marketing-and-conversion-marketing-email-marketing/all` muestra "412 apps",
  **24 tarjetas por página** y paginación con `?page=N` hasta la 18.
- La página 2 no repite ninguna app de la página 1.
- Cada tarjeta: `div[data-controller="app-card"]` con `data-app-card-handle-value`,
  `data-app-card-name-value` y, en el texto, `"4.7 out of 5 stars"`, `"3352 total reviews"` y el resumen de precio.
- **No** incluye la distribución por estrellas.
- Fixture: `category_email-marketing_all_p1.html`.

### 6.1 Cobertura de los listados frente al sitemap (Fase 1, 2026-10-05)

Comprobado con `radar inventory` sobre todo el catálogo (ejecución `20261005T113229Z`, D2 de `docs/decisions.md`).
Detalle completo en `data/coverage_20261005T113229Z.json`.

| Medida | Valor |
|---|---|
| Apps en el sitemap | 27.662 |
| Categorías con listado propio | 122 |
| Categorías sin listado (intermedias: `/all` devuelve 404) | 32 |
| Categorías raíz (no se piden; `/all` también da 404) | 7 |
| Filas de listado (app × categoría) | 36.464 |
| Apps distintas en los listados | 27.662 |
| Apps que están en varias categorías | 8.802 |
| Duplicados dentro de una misma categoría | 0 |
| Categorías cuyo total anunciado ("N apps") no coincide con lo listado | 0 |
| Apps del sitemap que no aparecen en ningún listado | 11 |
| Apps listadas que no están en el sitemap | 11 |

Conclusiones:

- **Los listados cubren el catálogo.** Las 11 apps del sitemap sin listado existen (ficha con 200) y tienen
  entre 0 y 14 reseñas, así que nunca pasarían el umbral de 50. Las 11 listadas que faltan en el sitemap son
  apps nuevas con 0 reseñas. El sitemap tenía `lastmod` del día anterior.
- Causa probable de esas 11, **sin verificar**: el listado se pagina por posición y tarda una hora entera.
  Si entra o sale una app durante el recorrido, otra puede saltar de página y no verse.
- **Solo 1.870 apps tienen 50 reseñas o más en los listados.** El paso de metadatos del catálogo completo baja
  a unas 1.900 fichas, alrededor de 80 minutos a 2,5 s por petición.
- 553 apps tienen más de 300 reseñas (método `estimated`). 15.845 apps no tienen ninguna reseña.
- El recuento de reseñas de una app es el mismo en todas sus categorías, salvo 4 casos que cambiaron durante el
  recorrido. El paso 2 usa el máximo.
- Un inventario completo son unas 1.550 peticiones y tardó 64 minutos, sin ningún bloqueo de Cloudflare.

**Error corregido:** el primer intento dedujo las categorías hoja por prefijo de nombre y dejó fuera
`orders-and-shipping-shipping-solutions-shipping`, con 1.098 apps. Su nombre es prefijo de su hermana
`...-shipping-rates`, así que parecía su categoría padre. Ahora se piden todas las categorías salvo las raíces
y el 404 identifica las intermedias. Ver el punto 6 de los detalles de implementación en `docs/decisions.md`.

## 7. Comportamiento con rastreo lento (Cloudflare)

apps.shopify.com está detrás de Cloudflare. Las respuestas válidas llevan `server: envoy`.

Registro completo: 56 peticiones.

| Franja | Ritmo | Resultado |
|---|---|---|
| 12:45:47–12:45:57 | 1.ª y 2.ª petición de la sesión | **403** en robots.txt y sitemap.xml |
| 12:46:00–12:46:46 | ≈1 cada 2,5 s | 8 × 200 |
| 12:48:14–12:49:00 | ≈1 cada 2,5 s, tras 90 s de pausa | **10 × 403** seguidos, con y sin parámetros |
| 12:59:12–12:59:14 | tras 10 min de pausa | 2 × 200 |
| 13:00:46–13:01:52 | **1 cada 2 s**, 34 peticiones seguidas | **34 × 200** |

- Los 403 son **desafíos gestionados de Cloudflare**: cabeceras `cf-mitigated: challenge`,
  `server: cloudflare` y un cuerpo HTML con el título "Verifying your connection..."
  (fixture: `cloudflare_challenge_403.html`). No es un 403 de la aplicación.
- Aparecen de forma **intermitente y por rachas**. No dependen de la query string ni del ritmo:
  la racha de 34 peticiones a 2 s pasó entera. Un bloqueo se levantó en 10 minutos o menos.
- No se observó ningún 429 ni 5xx. No hay cabeceras `Retry-After` ni `RateLimit-*`.
- La respuesta válida fija la cookie `_shopify-app_store_session5`.

**Observaciones de la Fase 1.** Ya con la política D5 aplicada en `radar/http.py`:

| Proceso | Primera petición | Resultado |
|---|---|---|
| Inventario completo (13:32, 1.550 peticiones en 64 min) | 200 | ningún bloqueo |
| Muestra de apps no listadas (14:37) | 403 | 1 pausa de 15 min y luego todo 200 |
| Reanudación del inventario (14:54) | 200 | ningún bloqueo |
| `run-all` de Gift cards y de Gift wrap (14:58) | 200 | ningún bloqueo |
| `run-all` de Sourcing options - Other (14:59) | 403 | 1 pausa de 15 min y luego todo 200 |
| `run-all` de Pre-orders (15:14, mismo lote) | 200 | ningún bloqueo |

- Todos los bloqueos de la jornada (7 episodios) cayeron en la **primera petición** de un proceso nuevo o justo
  después de un rato sin actividad. Nunca en mitad de una racha larga.
- Ninguno necesitó un segundo reintento. Es una correlación observada, no una causa verificada.

Recomendaciones para `radar/http.py`:

1. Detectar el desafío por `status == 403` y `cf-mitigated: challenge`, o por el título
   "Verifying your connection" del cuerpo. **No cachear nunca esa respuesta.** El cliente de
   reconocimiento lo hacía y obligó a repetir peticiones.
2. Ante un desafío: pausar 10 minutos y reintentar la misma URL. Si hay 3 pausas seguidas, abortar
   la ejecución, guardar el progreso y registrarlo en `runs.notes`.
3. **No intentar resolver ni esquivar el desafío** (nada de User-Agent de navegador ni de navegadores
   sin interfaz). Mantener el User-Agent identificable.
4. Mantener la cookie de sesión dentro de una misma ejecución (un `httpx.Client` reutilizado).
5. Mantener el ritmo de 1 petición cada 2-3 s, con backoff exponencial ante 429/5xx tal como dice la spec.

## 8. Fixtures guardados en `tests/fixtures/`

Todos los nombres de tienda se han sustituido por `REDACTED STORE` (regla 4). El resto del HTML está intacto.

| Fichero | Contenido | Casos que cubre |
|---|---|---|
| `robots.txt` | robots.txt | reglas `Disallow` |
| `sitemap_index.xml` | índice de 184 sitemaps | localizar `sitemap_apps_en.xml` |
| `sitemap_apps_en_sample.xml` | 50 primeras entradas del sitemap de apps | parser del inventario |
| `app_klaviyo-email-marketing.html` | ficha de app grande | JSON-LD, distribución, 3 planes, 2 categorías, "What merchants think" |
| `app_sufio.html` | ficha de app mediana | 4 planes de pago con precio en `aria-label` |
| `app_dropify-5.html` | ficha de app pequeña | 1 categoría, plan gratuito, nota baja |
| `reviews_klaviyo-email-marketing_p1_default.html` | reseñas en orden por defecto (`relevance`) | fecha "Edited", 10 respuestas |
| `reviews_klaviyo-email-marketing_newest_p5.html` | `sort_by=newest&page=5` | sin respuesta, sin tiempo de uso, "Edited", 1★ |
| `reviews_sufio_r1r2_newest_p1.html` | filtro `ratings[]=1&ratings[]=2` | mezcla de 1★ y 2★, 2 "Edited" |
| `reviews_dropify-5_newest_p6_last.html` | última página (3 reseñas) | página incompleta, 2★, sin respuesta, español |
| `reviews_dropify-5_newest_p7_empty.html` | página posterior a la última | condición de fin (0 reseñas, 200) |
| `review_permalink_2380223.html` | permalink | página de una sola reseña |
| `category_email-marketing_all_p1.html` | listado de categoría | tarjetas con handle y recuento |
| `cloudflare_challenge_403.html` | desafío de Cloudflare | detección de bloqueo |

## 9. Pendiente de decisión (preguntas para ti)

> **Resuelto el 2026-10-05.** Las respuestas están en `docs/decisions.md` (D1 a D4) y aplicadas en `SPEC.md`.

1. **`max_pages = 30` no alcanza los 365 días en apps grandes.** Klaviyo necesitaría unas 117 páginas.
   Opciones:
   - a) Aceptar el recorte. `recent_pain` y `momentum` quedarían calculados sobre menos de 12 meses en las apps grandes.
   - b) Descargar las negativas con `ratings[]=1&ratings[]=2` durante los 365 días completos (unas 6 páginas
     en Klaviyo). El denominador "total de reseñas en 12 meses" se obtendría con una búsqueda binaria de
     la página de corte en el listado `newest` (unas 7 peticiones por app), sin descargar todas las reseñas.
     Esto contradice "guardar todas las reseñas de esa ventana" del paso 4.
   - c) Subir `max_pages` solo para las apps preseleccionadas.
2. **Coste del paso 2 (metadatos).** Con 27.662 apps y 2 peticiones por app (ficha y página 1 de reseñas)
   a 2,5 s, son unas **38 horas**. La ficha ya trae JSON-LD y distribución exacta, así que la página 1 de
   reseñas es redundante. Con una sola petición por app serían unas 19 horas. Opción adicional: usar los
   listados de categoría (24 apps por página, con recuento de reseñas) para descartar antes las apps con
   menos de 50 reseñas, y descargar la ficha solo de las que pasan.
3. **Reseñas no inglesas.** ¿Se aceptan las reseñas en otros idiomas? Las palabras clave no las marcarán,
   pero Claude sí puede clasificarlas. Alternativa: filtrar por idioma.
4. **`rating`**: se propone guardar la nota mostrada (JSON-LD) y calcular `pct_*` con los recuentos exactos,
   aunque en algunas apps (Klaviyo) la nota no coincida con la media de la distribución.

## 10. Notas técnicas

- `selectolax` 1.0 eliminó el backend Modest: `from selectolax.parser import HTMLParser` lanza
  `ImportError`. Hay que usar `from selectolax.lexbor import LexborHTMLParser`.
- El sitemap de apps pesa 4,75 MB. Tardó 4,3 s en descargarse.
- La página HTML de una app pesa entre 200 y 300 KB. Una caché de 27.662 fichas ocuparía unos 7 GB sin
  comprimir. Conviene guardarla comprimida con gzip.

---

## 11. Foro de la comunidad de Shopify (Fase 2b, D27, 2026-10-08)

Mismo cliente educado (1 petición cada 2-3 s, User-Agent identificable, sin login).

### 11.1 robots.txt

Los dos foros son **Discourse**: `community.shopify.com` (comerciantes) y `community.shopify.dev` (desarrolladores).
Para `User-agent: *`, los dos prohíben:

```
/admin/  /auth/  /email/  /session  /user-api-key  /*?api_key*  /*?*api_key*
/badges  /my  /search  /tag/*/l  /g  /t/*/*.rss  /c/*.rss
```

Además, `community.shopify.com` prohíbe `/c/uncategorized/1` y `community.shopify.dev` prohíbe `*?*itcat=*`.
Los dos declaran un sitemap. No hay `Crawl-delay`.

Conclusiones:
- **La búsqueda (`/search`, también `/search.json`) está prohibida** y no se usa. Encaja con la regla de no usar `q=`.
- **Los endpoints JSON están permitidos**, comprobado con el intérprete de `radar/http.py`:
  `/categories.json`, `/latest.json`, `/top.json`, `/c/{slug}/{id}.json`, `/c/{slug}/{id}/l/latest.json`,
  `/t/{slug}/{id}.json`, `/raw/{id}/1`, `/tag/{nombre}.json` y `/sitemap.xml`. Solo `/tag/*/l` está prohibido.

### 11.2 Listados utilizables sin `q=`

- `/categories.json`: 41 categorías con su número de hilos.
- `/c/{slug}/{id}/l/latest.json?order=created&page=N`: 30 hilos por página, ordenados por fecha de creación, con
  `more_topics_url` para la siguiente. Es el que se usa.
- Cada hilo trae `id`, `title`, `slug`, `created_at`, `reply_count`, `posts_count`, `views`, `has_accepted_answer`
  (resuelto), `tags` y `excerpt` (inicio del primer mensaje). No hace falta pedir cada hilo.
- También trae `posters` y `last_poster_username`. **No se guardan** (regla 4: ningún nombre de usuario).

### 11.3 Alcance elegido

- Categorías de comerciantes: `shopify-apps`, `technical-qa`, `store-design`, `shopify-discussion`,
  `payments-shipping-fulfilment`, `shopify-plus`, `retail-point-of-sale`, `shopify-flow`, `accounting-taxes` y
  `store-feedback`.
- `community.shopify.dev` queda fuera: es un foro de desarrolladores sobre APIs, extensiones y publicación de apps, no
  sobre necesidades de comerciantes.
- La estimación de páginas a partir de la página 10 se quedó corta: el ritmo de hilos era mayor hace un año. Por
  ejemplo, `shopify-apps` tiene 3.382 hilos en 24 meses, unas 113 páginas.

---

## 12. Atlassian Marketplace (Fase 4A, reconocimiento, 2026-10-08) — PARADA

Resultado: **no se puede construir el adaptador con el alcance pedido.** robots.txt prohíbe la ruta de la API y los
términos de uso del Marketplace prohíben la recogida masiva. Detalle abajo.

Peticiones hechas: 18 en total, a 1 cada 2-3 s.
- 1 a `marketplace.atlassian.com/rest/2/` (la raíz de la API), en la misma llamada en que se leyó robots.txt, antes de ver
  que `/rest/` está prohibido. No se hizo ninguna más a `/rest/`.
- Las demás: robots.txt, documentación y términos, los sitemaps y una sola ficha de app (draw.io).
- Las respuestas del Marketplace se borraron de la caché después. Solo se conservan los robots.txt como fixtures, en
  `tests/fixtures/atlassian/`.

### 12.1 robots.txt de `marketplace.atlassian.com`

```
User-Agent: *
Disallow: /login  /server/  /rest/  /download/  /admin/  /manage/  /files/  /users/
User-Agent: MauiBot      -> Disallow: /
User-Agent: atlassian-bot -> Allow: /apps/  Disallow: /
Sitemap: https://marketplace.atlassian.com/sitemap.xml
```

- **`/rest/` está prohibido para todos los agentes.** Es la ruta de la API v2 (`/rest/2/`) que pedía la Fase 4A.
- `/apps/` (fichas) y los sitemaps sí están permitidos.

### 12.2 API REST v2: apagada

- La documentación (`developer.atlassian.com/platform/marketplace/rest/v2/intro/`) avisa: "The Marketplace V2 REST APIs
  are deprecated … Requests to these endpoints will fail with an HTTP 410 Gone response and an API_DEPRECATED error."
- El changelog (CHANGE-3257) fija la retirada definitiva el **30 de junio de 2026**: "every endpoint under /rest/2/ is
  affected … No V2 APIs will continue to function beyond June 30, 2026".
- Paginación documentada, por si sirve de referencia: `offset` y `limit` (0-50, 10 por defecto).

### 12.3 API REST v3, la vigente

- Base: `https://api.atlassian.com/marketplace/rest/3`. Documentación en `/platform/marketplace/rest/v4/`.
  `api.atlassian.com/robots.txt` devuelve 404.
- Autenticación: HTTP basic con el email de una cuenta de Atlassian y un token de API.
- **Reseñas** (`GET /rest/3/products/{productId}/reviews`):
  - la documentación dice "This resource requires authentication";
  - admite `sort`, `cursor`, `limit` y `hosting`;
  - devuelve `content`, `responseContent` (respuesta del desarrollador), `stars`, `date`, `productHosting` y también
    `authorName`, un dato personal.
- **Catálogo:** no hay un endpoint para recorrer todas las apps. Solo existen "ficha de una app por `productId`" y
  "fichas de un desarrollador". Tampoco hay filtros por producto (Jira o Confluence) ni por categoría.
- **Límites de uso:** no se encontraron publicados en las páginas revisadas.
- La API está pensada para que cada desarrollador gestione sus propias apps, no para leer el catálogo ajeno.

### 12.4 Términos de uso

`https://www.atlassian.com/licensing/marketplace/termsofuse`, apartado 6.3:

> "Use Restrictions. You may not use any scraping, crawling, data mining, or other bulk collection methods to extract
> data from Atlassian Marketplace."

**Esto prohíbe el uso que plantea la Fase 4A**, sea por la API o por la web. Los Atlassian Developer Terms se
descargaron, pero no se analizaron, porque la cláusula anterior ya basta.

### 12.5 Lo que sí se vio en la web (sin usar `/rest/`)

Antes de leer los términos se miró el sitemap y una ficha. Se documenta solo como contexto. No es una vía recomendada,
porque el apartado 6.3 la prohíbe igualmente.

- `sitemap-listings.xml`: 30.642 URLs, de ellas 8.174 identificadores de app distintos, contando Cloud, Server y Data
  Center. No se pudo separar cuántas son Cloud sin pedir cada ficha.
- Ficha de app, sin autenticar:
  - JSON-LD con `aggregateRating` (nota y `reviewCount`);
  - `hostingOptions` (`CLOUD`, `SERVER`, `DATA_CENTER`), `releaseDate` de la última versión, `paymentModel` y
    `categories`, en `window.__INITIAL_STATE__`.
  - **No aparecen** ni las instalaciones ni el texto de las reseñas: se cargan después en el navegador.
- Por esto no se calcularon los percentiles de reseñas e instalaciones del punto 4 ni se proponen umbrales.

### 12.6 `community.atlassian.com` (posible segunda fuente)

- robots.txt (`tests/fixtures/atlassian/community_robots.txt`) es generado por la plataforma (Khoros). Prohíbe la
  búsqueda (`/forums/searchpage*`, `/forums/forums/searchpage*`), las etiquetas (`/forums/tag/*`), las páginas de
  impresión, de respuesta y de perfil, y la autenticación. No prohíbe las páginas de los hilos.
- **Sin verificar:** los términos de uso de la comunidad. Por coherencia con el apartado 6.3 del Marketplace, habría
  que revisarlos antes de rastrear nada.
