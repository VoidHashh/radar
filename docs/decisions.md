# Registro de decisiones

Cada entrada indica qué se decidió, por qué y qué cambió en `SPEC.md`.

---

## 2026-10-05 — Decisiones tras la Fase 0

Origen: las preguntas de la sección 9 de `docs/recon.md`. Las decisiones D1-D6 son tuyas. La sección
"Detalles de implementación" recoge lo que añadí yo para aplicarlas y se puede revertir.

### D1. Ventana de reseñas

**Problema.** Con `max_pages = 30`, una app grande (Klaviyo, unas 117 páginas al año) solo cubría unos
3 meses de la ventana de 365 días.

**Decisión.**
- Si `review_count <= 300`: descargar todas las reseñas de los últimos 365 días. El total es exacto.
- Si `review_count > 300`:
  - descargar solo las negativas (`ratings[]=1&ratings[]=2`, `sort_by=newest`) hasta 365 días, con un
    máximo de 30 páginas;
  - estimar `total_12m` con una búsqueda binaria sobre las páginas `sort_by=newest`.
- Nueva tabla `app_windows(app_id, run_id, total_12m, method 'exact'|'estimated', negatives_12m)`.
- `recent_pain = negatives_12m / total_12m`.

**Cambios en la spec.** Sección 5 (tabla `app_windows`), paso 4 y fórmula de `recent_pain` en el paso 8.
Umbral y máximo de páginas configurables (`window.exact_max_reviews`, `window.max_pages`).

### D2. Metadatos

**Problema.** Dos peticiones por app para 27.662 apps suponían unas 38 h de rastreo. La primera página
de reseñas repetía datos que ya están en la ficha.

**Decisión.**
- Una sola petición por app: la ficha. Se elimina la primera página de reseñas.
- Preseleccionar con los listados de categoría: solo se pide la ficha si `review_count >= 50`
  (configurable).
- Comprobar y documentar si los listados cubren las 27.662 apps del sitemap, sin duplicados ni apps fuera.
  El resultado irá en `docs/recon.md` al terminar la Fase 1.

**Cambios en la spec.** Pasos 1 y 2, sección 3 y criterios de la Fase 1.

### D3. Idioma

**Problema.** Hay reseñas en otros idiomas (16 de 257 en la muestra, en español) y `keywords.yaml` solo
está en inglés.

**Decisión.**
- Se conservan todas las reseñas.
- Las palabras clave son una señal más: no filtran lo que pasa a Claude.
- Se clasifican todas las reseñas de 1-2 estrellas.
- Se añade `language` (ISO 639-1) a la salida JSON de Claude y a la tabla `review_llm`.

**Cambios en la spec.** Pasos 4, 5 y 6, y tabla `review_llm`.

### D4. Nota

**Problema.** La nota mostrada no siempre es la media de la distribución: Klaviyo muestra 4,7 y su
media calculada es 4,61.

**Decisión.**
- `app_snapshots` guarda `rating_shown`, `rating_computed` y los recuentos `n_1..n_5` en lugar de `pct_*`.
- La puntuación usa `rating_computed` y los recuentos.

**Cambios en la spec.** Tabla `app_snapshots`, preselección (paso 3) y `pain` en el paso 8.

### D5. Cloudflare

**Problema.** Bloqueos intermitentes con 403 de verificación (`cf-mitigated: challenge`).

**Decisión.**
- Ante un 403 de verificación: pausa de 15 minutos y reintento.
- Tras 3 bloqueos seguidos: guardar estado, parar y reanudar en la siguiente ejecución.
- Prohibido esquivarlo: sin proxies ni rotación de User-Agent.
- Ritmo base: 1 petición cada 2-3 s.

**Cambios en la spec.** Regla 3 de la sección 2.

### D6. Otros

- No se guarda el país del desarrollador, porque "Based in" depende de la IP. Regla 4.
- `reviews.review_date` pasa a llamarse `date_shown`. Puede ser la fecha de edición. Sección 5.
- Se quita `radar recon` del CLI. Sección 8.
- `docs/recon.md` indica que selectolax 1.0 requiere el parser lexbor.
- `SPEC_radar_shopify.md` pasa a llamarse `SPEC.md`.

### Detalles de implementación (míos, revisables)

Estos cambios no estaban en tus decisiones. Los añadí porque las decisiones los necesitan para funcionar.

1. **Tabla `app_listings(app_id, category, run_id, rating_shown, review_count, position)`.** D2 preselecciona
   con los listados, así que hace falta guardar lo que dicen. Además permite comprobar la cobertura.
2. **Columna `reviews.edited`.** Marca las reseñas cuya fecha visible es la de edición. Sirve para el corte de
   365 días: el orden `newest` sigue la fecha de creación, así que solo se para al encontrar una reseña
   *no editada* más antigua que el corte.
3. **Columna `app_snapshots.run_id`.** Permite saber qué ejecución tomó cada snapshot y reanudar el paso de
   metadatos sin repetir fichas.
4. **Módulo `radar/pipeline.py`.** Separa la orquestación de los pasos (reanudable) de la CLI.
5. **`StoreAdapter` gana `list_category()` y `estimate_total_since()`, y `fetch_reviews()` acepta `ratings`.**
   D1 y D2 los necesitan. La Chrome Web Store también tiene categorías, así que la interfaz sigue siendo genérica.
6. **Inventario: se piden todas las categorías salvo las 7 raíces.** El sitemap de categorías tiene 161 entradas.
   - El primer intento dedujo las hojas por prefijo de nombre y fallaba:
     `orders-and-shipping-shipping-solutions-shipping` (1.098 apps) es prefijo de su hermana `...-shipping-rates`,
     así que se tomó por categoría padre y sus apps quedaron fuera.
   - Ahora se pide `/all` de todas las categorías salvo las raíces. Las intermedias devuelven 404 y quedan
     registradas como "sin listado", así que no se vuelven a pedir al reanudar.
   - Coste: unas 30 peticiones extra por inventario completo.
7. **Preselección con `rating_computed`.** D4 dice que la puntuación usa la nota calculada. Apliqué lo mismo al
   umbral `rating <= 4.2` del paso 3, por coherencia.
8. **Palabras clave solo sobre reseñas de 1-2 estrellas.** Ya lo decía la spec ("solo las de 1-2 estrellas
   pasan a los pasos 5 y 6"). Lo dejo explícito en el paso 5.
9. **Normalización del apóstrofo.** Antes de buscar palabras clave, `’` se sustituye por `'`, porque muchas
   reseñas usan el apóstrofo tipográfico ("doesn’t work").

---

## 2026-10-05 — Fase 1: categorías de prueba y preguntas abiertas

### Categorías de prueba

- **Primer intento:** "Gift cards" (55 apps) y "Gift wrap and messages" (61 apps). Ambas funcionaron de principio
  a fin, pero preseleccionaron 0 y 1 apps, sin ninguna marca de palabra clave. No servían para probar los pasos 4 y 5.
- **Categorías usadas para dar la fase por buena:** "Sourcing options - Other" (102 apps) y "Pre-orders" (112 apps).
  Son las categorías de unas 100 apps con más apps de nota baja según los listados. Ejercitan los dos métodos de
  ventana (`exact` y `estimated`) y el marcado por palabras clave.
- Las cuatro ejecuciones quedan en la tabla `runs`. Su salida está en `data/runall_*.out`.

### Preguntas abiertas para la Fase 2 (resueltas el 2026-10-05)

**D7. Negativas sin texto.**
- Pregunta: 6 de las 22 negativas de la muestra son solo estrellas. ¿Se excluyen del lote de Claude y de
  `recurrence`, aunque cuenten para `negatives_12m`, `recent_pain` y `neglect`?
- Decisión: se excluyen de la clasificación con Claude, porque lo que se necesita es el contenido para
  identificar problemas. Siguen contando en las proporciones, como proponía la pregunta.
- Cambios en la spec: pasos 6 y 8.

**D8. Muestras pequeñas.**
- Pregunta: "pre-order" tiene 541 reseñas, pero solo 4 en los últimos 12 meses (3 negativas), lo que da
  `recent_pain = 0,75`. ¿Se exige un mínimo de `total_12m` o se pondera por tamaño?
- Decisión: no hay mínimo ni ponderación. Esas apps también se agrupan, por si aportan información valiosa.
- Cambios en la spec: pasos 7 y 8.

**D9. Credenciales.**
- `accesos.txt` contiene los datos de GitHub y del servidor OVH donde se probará la app más adelante.
- Se añade a `.gitignore` y la regla 7 de la spec lo menciona. Claude no lo lee salvo que se le pida.

---

## 2026-10-05 — Antes de la Fase 2

**D10. Sin API de Anthropic.**
- Decisión: no se usa la API. El proyecto trabaja sin `ANTHROPIC_API_KEY`.
- Método elegido: **todo con Claude Code**. La clasificación de todas las negativas con texto, la agrupación y la
  estimación de construibilidad las hace Claude Code en la sesión, no un script contra la API.
- Mecánica: `radar` exporta lotes JSONL a `data/llm/`; Claude Code (con subagentes en paralelo) escribe un fichero de
  resultados por lote siguiendo las instrucciones de `docs/prompts/`; `radar` valida e importa. Reanudable por lote.
- Consecuencias: no hay coste de API ni presupuesto en dólares; el criterio de la Fase 2 "mostrar el coste real de la
  API" se sustituye por mostrar cuántas reseñas y apps procesó Claude Code. Consume el plan de Claude del usuario.
  La ejecución semanal de la Fase 3 no podrá ser 100 % desatendida en estos pasos.

**D11. Primer informe sobre el catálogo completo.**
- Decisión: opción A. Primero se ejecuta la recogida (pasos 1-5) sobre todas las categorías y después el análisis.
- Lanzado el 2026-10-05 con `radar run-all`. Salida en `data/runall_full.out`.

**D12. Nueva fuente de oportunidades: apps muy usadas que se puedan replicar o mejorar.**
- Propuesta tuya: además de las apps con quejas, incluir apps muy utilizadas que se puedan replicar o mejorar.
- Decisión: **sección aparte** del informe, independiente del ranking de quejas.
- Criterios propuestos por Claude (configurables en `config.yaml`, sección `replicables`):
  - candidatas: muchas reseñas (`min_reviews`), algún plan de pago con precio mensual (`min_paid_usd_month`),
    excluidas las apps del propio Shopify (no se compite con una app oficial gratuita);
  - Claude Code lee la ficha de cada candidata y estima construibilidad (S/M/L), qué hace el núcleo y qué se mejoraría;
  - puntuación propia: demanda, precio y construibilidad (pesos en `config.yaml`).

### Piloto de la Fase 2 (2026-10-05)

- Circuito completo probado con datos reales de la categoría Pre-orders (ejecución `20261005T131458Z`):
  exportar → Claude Code clasifica 4 negativas → importar → agrupar 3 apps → evaluar 3 replicables → puntuar →
  informe en `data/pilot/2026-41.md`. Sin errores de validación.
- Observación para revisar con el catálogo completo: con D8 (sin mínimo de `total_12m`), una app con 1 sola negativa
  en 12 meses obtiene `recent_pain`, `recurrence` y `neglect` al máximo y encabeza el ranking del piloto.

---

## 2026-10-05 — Resultado de la Fase 2

- Primer informe: `reports/2026-41.md` (+ `2026-41.csv` y `2026-41-replicables.csv`), ejecución `20261005T161954Z`.
- Claude Code procesó 1.555 negativas con texto (11 lotes, subagentes Sonnet), agrupó 177 apps y evaluó 60 replicables
  (subagentes con el modelo principal). Cero errores de validación al importar.
- `radar report --check-links`: 20 de 20 enlaces de ejemplo devolvieron 200.
- **Problema detectado (pendiente de decisión):** con D8 (sin mínimo de muestra), las 20 primeras tienen entre 1 y 5
  negativas en 12 meses; con un solo grupo, `recurrence` y `neglect` salen a 1,00. La primera app con 10 o más
  negativas clasificadas está en el puesto 24.

---

## 2026-10-06 — Ajuste del ranking (D13-D16)

Origen: en el primer informe las 20 primeras tenían entre 1 y 5 negativas en 12 meses.

**D13. Media bayesiana** en `recent_pain`, `recurrence` y `neglect`: `(x·n + p0·m) / (n + m)`, `m = 10`
(`scoring.bayes_m`).
- `n` es el tamaño de muestra de cada proporción: `total_12m`, negativas clasificadas y negativas de la ventana.
- `p0` (decisión de implementación): proporción **agregada** de las apps preseleccionadas de la ejecución,
  suma de numeradores entre suma de denominadores. Se guarda en `runs.notes.scoring_p0`.
- Ejecución `20261005T161954Z`: recent_pain 0,173 · recurrence 0,488 · neglect 0,730.

**D14. Componente `volume`** = `log10(1 + negatives_12m)`, normalizado por el máximo de la ejecución. Peso 0,15.

**D15. Componente `competition`** por categoría, con los listados y fichas ya descargados (`radar/competition.py`):
- presión = 0,4·norm(log10(1 + nº de apps)) + 0,4·norm(log10(1 + nº de apps con ≥ 500 reseñas))
  + 0,2·proporción de apps con plan gratuito (entre las fichas descargadas); `competition = 1 - presión`.
- Cada app se mide en su **categoría principal**, la primera de su ficha (decisión de implementación).
- Peso 0,15 en el ranking de quejas. También en replicables, donde el informe muestra la categoría, los
  competidores fuertes (excluida la propia app) y las alternativas con plan gratuito.

**Redistribución de pesos.**

| Componente | Antes | Ahora |
|---|---|---|
| demand | 0,15 | 0,105 |
| pain | 0,10 | 0,07 |
| recent_pain | 0,15 | 0,105 |
| momentum | 0,10 | 0,07 |
| recurrence | 0,20 | 0,14 |
| buildability | 0,20 | 0,14 |
| neglect | 0,10 | 0,07 |
| volume | — | 0,15 |
| competition | — | 0,15 |

Los 7 originales se escalan ×0,7 para mantener sus proporciones. En replicables: demand 0,34, price 0,255,
buildability 0,255 (×0,85) y competition 0,15.

**D16. "Muestra baja"**: se marcan con ⚠ las apps con menos de 5 negativas en 12 meses
(`scoring.low_sample_negatives`). No se excluyen.

**Resultado:** `reports/2026-41b.md`, recalculado sin rastrear ni clasificar. En el top 20, 10 apps tienen muestra baja
(antes eran las 20).

---

## 2026-10-06 — Ajustes de la Fase 2 (D17-D20)

**D17. Plataformas fuera del ranking.**
- Apps de desarrolladores de plataforma, canal o pago: lista editable `platforms.developers` en `config.yaml`.
- Coincidencia sin mayúsculas, por nombre exacto o seguido de espacio ("Google LLC", "PINTEREST inc"; "Xero" no es "X").
- Se puntúan igual (columna `scores.platform`), pero el informe las saca del ranking y las muestra en
  "Contexto: apps de plataforma, canal o pago". En la ejecución `20261005T161954Z` son 23 de 228.

**D18. Incidentes puntuales.**
- Marca "incidente puntual" si >= 60 % de las negativas de 12 meses (`incident.share`) caen en una ventana de 30 días
  (`incident.window_days`) y son del mismo grupo de quejas (`incident.same_cluster`).
- Entonces `recurrence` y `momentum` se multiplican por 0,3 (`incident.factor`).
- Umbral añadido por Claude: `incident.min_negatives = 5`. Sin él, cualquier app con 1 sola negativa sería siempre un
  "incidente" (100 % en una ventana).
- Detectados: Orderly Emails (69 de 92 negativas entre el 29-mar y el 20-abr, "update deleted collections"),
  Xero (5 de 6) y Shein Importer (3 de 5).

**D19. Competencia revisada.** Se elimina "alternativas gratis", que no distinguía: las 60 replicables tenían alternativas gratuitas.
- presión = 0,6·norm(log10(1 + nº de apps con >= 100 reseñas)) + 0,4·cuota del líder.
- Cuota del líder = reseñas de la app con más reseñas / reseñas totales de la categoría.
- `competition = 1 - presión`. Umbrales en `scoring.competition`: `strong_min_reviews: 100`, `strong: 0.6`,
  `leader_share: 0.4`.
- Una cuota alta del líder cuenta como más presión: un líder dominante es más difícil de desplazar.
- El informe de replicables muestra los competidores (>= 100 reseñas), el líder y su cuota.

**D20. Necesidades no cubiertas.**
- Material: negativas clasificadas con `feature_requested`, o de categoría `missing_feature` o `broken`.
- Exclusiones: apps de plataforma y reseñas del grupo de un incidente puntual.
- Se agrupan por la categoría principal de la app. Subagentes de Claude Code unen las necesidades equivalentes entre
  apps distintas (`docs/prompts/needs.md`, `radar needs export|import`, tabla `needs`) y estiman la construibilidad
  de una app independiente que haga solo eso.
- Filtros del informe: >= 5 reseñas y >= 2 apps (`needs.*`). Orden: reseñas y después apps.
- Resultado: 30 necesidades en 13 categorías, de las que **7 pasan los filtros** (se pidieron 15).
  - El material útil es poco: 216 quejas, frente a 527 de plataformas y 85 de incidentes.
  - Muchas necesidades repetidas vienen de una sola app; por ejemplo, la conexión de WhatsApp con 23 reseñas.

Informe: `reports/2026-41c.md` (+ `-needs.csv`), recalculado sin rastrear.

---

## 2026-10-07 — Ajustes de la Fase 2 (D21-D23)

**D21. Incidentes fuera del ranking; ranking congelado.**
- Las apps con incidente puntual salen del ranking de quejas y pasan a "Contexto", junto a las plataformas.
- Se elimina el factor 0,3. Se sigue detectando el incidente (`scores.incident`), pero ya no cambia la puntuación.
- El ranking de quejas por app queda congelado: no se recalcula ni se ajusta más. Los informes 41 a 41c quedan como están.

**D22. Material ampliado solo para "Necesidades no cubiertas".**
- Fuente: las 1.870 fichas con 50 o más reseñas, salvo las 41 de plataforma.
- Reseñas de 1, 2 y 3 estrellas de los últimos 24 meses, con `ratings[]` de una sola estrella y como máximo
  10 páginas por app y estrella. Comando: `radar needs collect`, reanudable con la tabla `needs_crawl`.
- Estimación previa: hasta 5.034 peticiones (unas 3,5 h) y hasta 25.452 reseñas.
- Resultado: 3.783 pares app-estrella, 11.263 reseñas, ningún bloqueo de Cloudflare y 1 par truncado en 10 páginas.
  Duró unas 2 h 55 min.
- Clasificación sin API: 8.648 reseñas nuevas con texto, en 39 lotes de hasta 250 con subagentes Sonnet
  (`radar needs classify-export|classify-import`). Cero errores de validación.
- Para las de 3 estrellas basta con `specific_problem` y `feature_requested`; la validación es más laxa para ellas.
- Exclusiones:
  - apps de plataforma;
  - reseñas del grupo de un incidente;
  - reseñas de apps con incidente fechadas dentro de la ventana del incidente (decisión de implementación: las reseñas
    nuevas no tienen grupo asignado).
- Material de necesidades:
  - 1-2★ con función pedida o de categoría `missing_feature` o `broken`;
  - todas las 3★.
  - Resultado: 5.094 quejas en 72 categorías (`radar needs wide-export|wide-import`, tabla `needs` con `scope = 'wide'`).

**D23. Informe `reports/2026-41d.md`, solo con "Necesidades no cubiertas".**
- Filtros: 10 o más reseñas y 3 o más apps.
- Sin regulación: cada necesidad lleva `regulated`, que marca Claude Code (impuestos, aduanas, pagos, datos de salud).
  Además se excluyen siempre las categorías de `needs_wide.regulated_categories`.
- Columnas: necesidad, reseñas, apps, categoría, construibilidad con justificación y 2-3 enlaces.
- Al final, "sí" o "no": ¿hay al menos una necesidad S que pase los filtros?

**Resultado de D22-D23 (2026-10-07).**
- `reports/2026-41d.md`: 433 necesidades en 72 categorías.
- Pasan los filtros 114, de ellas 22 con construibilidad S. Otras 5 pasarían por tamaño, pero son reguladas.
- Respuesta final del informe: **sí** hay al menos una necesidad S que pasa los filtros.
- Aviso: en 25 de las 114, una sola app concentra la mitad o más de las reseñas. Pasan el filtro de 3 apps, pero en
  la práctica son problemas de esa app. Ejemplos: WhatFlow con 47 de 49, Cozy Image Gallery con 27 de 33 y el
  conector de QuickBooks con 34 de 49.

---

## 2026-10-07 — Cierre de la Fase 2 (D24-D26)

**D24. "Problemas de una app".**
- Se apartan las necesidades en las que una sola app reúne el 50 % o más de las reseñas
  (`close.single_app_share`; columnas `needs.top_app` y `needs.top_share`).
- De las 114 que pasaban D23 se apartan 25. Quedan 89, de ellas 16 con construibilidad S.

**D25. "¿Lo cubre el líder?"**
- Para cada una de las 11 categorías con necesidades S, se toman las 3 apps con más reseñas del listado. Decisión
  literal: son las apps con más reseñas que aparecen en el listado de esa categoría, aunque su categoría principal sea
  otra (Judge.me como líder de SEO, por ejemplo).
- Ficha completa desde la caché: lema, descripción, funciones, etiquetas de funciones (nuevo campo `feature_tags`)
  y planes.
- Subagentes (`docs/prompts/leaders.md`) emitieron 48 veredictos: cubierta, parcial o no cubierta, con cita literal de
  la ficha. Tabla `need_leaders`.
- "No cubierta" significa que ningún líder la cubre del todo; un "parcial" no la cubre. Resultado: **13 de 16**, y en
  1 de ellas ningún líder la menciona siquiera de forma parcial.
- Regla aplicada: una necesidad de fiabilidad solo está cubierta si la ficha promete expresamente monitorización,
  alertas o garantía.

**D26. Nicho de monitorización.**
- Búsqueda por nombre y descripción en las 27.662 tarjetas de los listados cacheados, sin `q=`.
- 91 coincidencias en total. Claude eligió a mano 32 de monitorización funcional y dejó fuera copias de seguridad,
  enlaces rotos, auditorías SEO y alertas de ventas.
- Ejecución propia `20261007T122817Z` (alcance "monitoring"), para no tocar la del ranking.
- Las 32 suman 56 reseñas, todas de 5★. No hay reseñas de 1-3★ que pasar por la clasificación.
- Los subagentes (`docs/prompts/monitoring.md`) confirmaron 23 del nicho y marcaron qué comprueba cada una: caída,
  carrito, checkout, widgets de apps y datos que llegan al pedido.
- Resultado: 18 de las 23 se lanzaron en 2026.

Informe: `reports/2026-41e.md`. Última línea: necesidades S no cubiertas = 13.

---

## 2026-10-08 — Fase 2b: vías 1 y 2 (D27-D30)

**D27. Reconocimiento del foro.** Detalle en `docs/recon.md`, sección 11.
- `community.shopify.com` y `community.shopify.dev` son Discourse.
- robots.txt prohíbe `/search` y permite los endpoints JSON de categorías, listados y temas. No hizo falta parar.
- Se usa `/c/{slug}/{id}/l/latest.json?order=created&page=N`, sin `q=` ni login, a 1 petición cada 2-3 s.
- Datos mínimos por hilo (tabla `forum_topics`): URL, título, extracto, fecha, respuestas, vistas, resuelto y
  etiquetas. Ningún nombre de usuario.
- `community.shopify.dev` queda fuera: es un foro de desarrolladores.

**D28. Descubrimiento en el foro.**
- 10 categorías de comerciantes, últimos 24 meses: 38.371 hilos en 1.276 peticiones, sin bloqueos.
- Decisión de implementación: tope de 400 páginas por categoría. Solo `store-design` lo alcanzó, y cubre desde el
  19-11-2024 en lugar del 08-10-2024.
- Un patrón ("is there an app", "any app that", "how can I automatically", "workaround"...) sobre título y extracto marca
  607 candidatos. Subagentes Sonnet confirmaron 464 como peticiones de una app o de un apaño (`forum_llm`).
- Un subagente las agrupó en 91 necesidades de 2 o más hilos (`forum_needs`).
- Para cada una se buscaron apps candidatas en el índice de las 27.662 tarjetas (`app_cards`: nombre y descripción de
  los listados cacheados) y otros subagentes juzgaron si alguna lo promete explícitamente (`forum_need_apps`).

**D29. Vía 1.**
- Las 12 necesidades S no cubiertas por los líderes. Se aparta la de personalización, ya analizada.
- Por cada una, un patrón preselecciona hilos del foro y apps candidatas. Las apps que mencionan el aspecto distintivo
  de la necesidad van primero, para que no queden fuera las especializadas nuevas y sin reseñas.
- Un subagente elige los hilos que tratan esa necesidad y las apps que la resuelven explícitamente, y da el veredicto:
  "hueco real", "cubierta por una app especializada" o "evidencia débil" (`docs/prompts/via1.md`, tabla `via1`).
- Resultado:

| Veredicto | Necesidades |
|---|---|
| hueco real | 4: upsell que avisa si deja de mostrarse; lógica de regalos y umbrales; estado de envío y seguimiento devuelto a Shopify con control del autocumplimiento; insignias solo en productos elegidos |
| cubierta por una app especializada | 4: reglas de cantidad, alertas de reposición fiables, código residual de page builders y SEO que se desinstala limpio. Todas las apps que las cubren son nuevas y sin reseñas |
| evidencia débil | 4: exportar reseñas, configuración sencilla de reseñas, entrega de archivos digitales y texto alternativo masivo |

**D30. Filtro D25 sobre las necesidades M.**
- 60 necesidades M (≥ 10 reseñas, ≥ 3 apps, sin regulación ni concentración) y 180 veredictos de líderes
  (`data/llm/leaders_m/`).
- Ningún líder cubre del todo 41. Se listan sin análisis profundo.

---

## 2026-10-08 — Cierre del radar de Shopify y paso a Atlassian (D31)

**D31. Radar de Shopify cerrado.**
- Los 4 "huecos reales" de `reports/2026-41f.md` se descartaron tras una verificación externa hecha por el usuario:
  - **Lógica de regalos y umbrales:** mercado saturado.
  - **Estado de envío y seguimiento devuelto a Shopify:** ya lo cubren las plataformas de envío.
  - **Upsell que avisa si deja de mostrarse:** evidencia débil.
  - **Insignias solo en productos elegidos:** evidencia débil.
- No se añaden más fases al radar de Shopify. El código, los datos y los informes 2026-41 a 41f se conservan como están.
- Siguiente: Fase 4A, un adaptador para el Atlassian Marketplace con la misma interfaz `StoreAdapter`, sin tocar el
  de Shopify. Solo apps Cloud. Ritmo máximo de 1 petición por segundo, o el que marquen los límites de la API si es
  más lento. Sin datos personales de quienes escriben las reseñas. Clasificación con subagentes, sin API.

**D32. Fase 4A (Atlassian Marketplace) parada en el reconocimiento.**
- `robots.txt` de `marketplace.atlassian.com` prohíbe `/rest/`, la ruta de la API v2 pedida.
- La API v2 está apagada desde el 30-06-2026 (410 Gone, CHANGE-3257).
- La v3 (`api.atlassian.com/marketplace/rest/3`) exige autenticación para las reseñas, devuelve el nombre del autor y no
  tiene un endpoint para recorrer el catálogo.
- Los términos de uso del Marketplace (apartado 6.3) prohíben "scraping, crawling, data mining, or other bulk
  collection methods".
- Conforme a la regla 3 de la spec y a la instrucción "si prohíben este uso, PARA", no se construye el `StoreAdapter`
  ni se guardan fixtures de la API. Solo se guardan los robots.txt.
- Detalle en `docs/recon.md`, sección 12.

---

## 2026-10-08 — Cierre del radar por términos de uso (D33)

**Texto verificado hoy en las páginas oficiales.**
- **Términos del servicio de Shopify** (`https://www.shopify.com/legal/terms`, "Last updated on: August 1, 2026"):
  > "You agree not to access the Services or monitor any material or information from the Services using any robot,
  > spider, scraper, or other automated means."
- En el mismo apartado:
  > "You agree not to reproduce, duplicate, copy, sell, resell or exploit any portion of the Service, use of the Services,
  > or access to the Services without the express written permission by Shopify."
- **Política de uso aceptable** (`https://www.shopify.com/legal/aup`): su texto **no** contiene una cláusula propia
  sobre acceso automatizado. Remite a los Términos del servicio ("You need to follow our agreements with you … our
  Terms of Service are important"), y los Términos incorporan la AUP de forma expresa. La prohibición está, por tanto,
  en los Términos del servicio y no en la AUP.

**Decisión.**
- Se cierra el radar. Se cancela la Fase 3: no se programan ejecuciones.
- **Borrado:**
  - `cache/`: 19.620 ficheros, 267 MB, con todo el HTML y el JSON descargados de la App Store y del foro.
  - `reviews.body` en `data/radar.sqlite`: 12.684 reseñas con texto pasan a `NULL`, y se compacta el fichero con
    `VACUUM`.
  - El campo `body` de los lotes de clasificación en `data/llm/classify*/`: 51 ficheros y 10.203 reseñas.
  - El texto de las reseñas y de las respuestas de los desarrolladores en los fixtures HTML: se sustituye por
    `REDACTED REVIEW TEXT`, en 72 bloques. Los tests de los parsers se ajustaron y siguen pasando.
- **Se conserva:**
  - `reports/`;
  - las tablas agregadas: `apps`, `app_snapshots`, `app_windows`, `app_listings`, `scores`, `clusters`, `needs`,
    `need_leaders`, `replicables`, `via1`, `forum_needs`...;
  - las clasificaciones sin texto literal: `review_llm` (problema parafraseado en inglés), `forum_llm` y los ficheros
    de salida de `data/llm/`.
- Nueva regla 9 en `SPEC.md`: cualquier fuente nueva exige revisar primero sus términos de uso. Si prohíben la recogida
  automatizada, no se usa.

**Texto literal que queda y no estaba en el encargo** (resuelto en D34: se borra):
- `forum_topics.title` y `forum_topics.excerpt`: título e inicio del primer mensaje de 38.371 hilos del foro.
  Las copias de los extractos siguen en `data/llm/forum_classify/`, `data/llm/forum_needs/` y `data/llm/via1/`.
- Las descripciones de las fichas de apps (lema, descripción, funciones) en `data/llm/replicables/`,
  `data/llm/leaders*/` y `data/llm/monitoring/`, y los subtítulos de las tarjetas en `app_cards`.
- El resumen "What merchants think" (generado por Shopify) dentro de los fixtures de fichas de apps.

## 2026-10-08 — Sin texto literal de terceros (D34)

**Decisión.** No se guarda texto literal de terceros. Se borra el que quedaba pendiente en D33 y se cierra ese
pendiente. Se conservan las URLs, las cifras, las fechas y las clasificaciones parafraseadas.

**Base de datos** (`data/radar.sqlite`, compactada con `VACUUM`, de 55,5 MB a 42,8 MB):
- `forum_topics.title` y `forum_topics.excerpt` pasan a `NULL` en los 38.371 hilos. Se conservan la URL, la fecha,
  las respuestas, las vistas, las etiquetas y la clasificación de `forum_llm`.
- `app_cards.subtitle` pasa a `NULL` en las 27.662 tarjetas.
- Citas de las fichas: `need_leaders.quote` (228 filas), las citas de `monitoring_apps.checks` (22 apps) y las de
  `forum_need_apps.apps` (71 necesidades). Se conservan los veredictos.
- El detalle de los planes de precio en `apps.pricing` (754 apps). Se conservan el nombre y el precio de cada plan.

**Ficheros de intercambio** (`data/llm/`, 268 ficheros): se vacían `title`, `excerpt`, `subtitle`, `tagline`,
`description`, `features`, `quote` y el detalle de los planes de precio. Las salidas parafraseadas (problema,
etiqueta, justificación, `why`, `summary`, `weaknesses`) se conservan. Los ficheros ya no sirven para reimportar los
pasos que exigían una cita, y no se van a reimportar.

**Fixtures** (`tests/fixtures/`): 215 sustituciones por `REDACTED APP TEXT`. Cubren el lema, la descripción y las
funciones de la ficha; el resumen "What merchants think"; las descripciones de las tarjetas de apps; las funciones y
el detalle de los planes de precio; las etiquetas `meta` de descripción; la descripción del JSON-LD; el subtítulo del
`<title>` y del `og:title`; y los textos alternativos de las capturas. Se conservan los nombres, los precios, las
cifras y las etiquetas de funciones de la taxonomía de Shopify. Los 76 tests pasan sin cambios.

**Informes:**
- `reports/2026-41e.md` pierde la columna "Evidencia en la ficha" de las tablas de líderes y las líneas "Evidencia"
  del mapa de monitorización.
- `reports/2026-41f.md` pierde las citas de las apps que ya resuelven necesidades del foro.
- `close_report.py` y `report_f.py` ya no imprimen citas. Regenerados en un directorio aparte, dan el mismo texto,
  salvo la fecha de lanzamiento del mapa de monitorización, que se leía de la caché borrada en D33.
