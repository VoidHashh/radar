"""D29: vía 1. Para cada necesidad S no cubierta por los líderes: evidencia en el foro, apps especializadas y veredicto.

Las búsquedas son por patrones sobre datos ya descargados (hilos del foro e índice de tarjetas de los listados), sin
`q=`. Los patrones solo preseleccionan candidatos: el juicio lo hace Claude Code (docs/prompts/via1.md).
"""
from __future__ import annotations

import json
import re
import sqlite3

from .handoff import ImportReport, _step_dir, pending_outputs

# need_id -> (patrón para hilos del foro, patrón para apps: nombre + descripción de la tarjeta)
SEARCH = {
    177: (r"upsell|cross[- ]?sell|post[- ]purchase offer|frequently bought",
          r"upsell|cross[- ]?sell|post[- ]purchase|frequently bought|app (health|uptime|monitor)|widget monitor"),
    86: (r"(minimum|maximum|min|max)\.? (order|quantity|qty|purchase|cart)|order limit|quantity limit|limit (the )?quantity|purchase limit",
         r"(min|max|minimum|maximum)\.? (order|quantity|qty|purchase)|order limit|quantity limit|purchase limit|order rules?"),
    327: (r"\bbadges?\b|product labels?|sale label|\blabels? on (product|collection)",
          r"\bbadges?\b|product labels?|\blabels?\b.*(product|sale)|stickers?"),
    135: (r"export (my |all |the )?reviews|reviews? export|backup (my |the )?reviews|migrat\w* reviews|transfer reviews|import reviews",
          r"review (import|export|migrat|backup|transfer)|(import|export|migrat|backup|transfer)\w* reviews?"),
    171: (r"back[- ]in[- ]stock|restock (alert|notif|email)|notify me when|out of stock (alert|notif)|waitlist",
          r"back[- ]in[- ]stock|restock|notify me|waitlist|stock alert"),
    149: (r"review app|product reviews?.*(set ?up|install|configure|not showing|widget)|reviews? widget",
          r"product reviews?|reviews? app|reviews? widget"),
    402: (r"(left ?over|leftover|remaining|residual) code|code (left|remains)|uninstall\w*.*(code|theme)|remove (app )?code|ghost code",
          r"(clean|leftover|residual) code|uninstall|code cleaner|theme (clean|cleanup|audit)|app code"),
    230: (r"tracking (number|info)|fulfil?lment status|mark(ed)? as fulfil?led|auto[- ]?fulfil?l|unfulfilled|fulfil?l(ment)? automatically",
          r"(auto[- ]?)?fulfil?l\w*|tracking (number|sync)|order tracking|mark as fulfil?led"),
    389: (r"seo app.*(uninstall|remov|leftover|code)|(uninstall|remov)\w*.*seo app|seo (settings|edits).*(lost|overwrit|reset)",
          r"\bseo\b"),
    392: (r"\balt[- ]?text\b|\balt tags?\b|image alt",
          r"\balt[- ]?text\b|\balt tags?\b|image alt"),
    179: (r"free gift|gift with purchase|\bgwp\b|buy \w+ get \w+|\bbogo\b|tiered discount|spend \$?\d+ (get|and)|discount threshold",
          r"free gift|gift with purchase|\bgwp\b|buy x get y|\bbogo\b|tiered|volume discount|quantity breaks?"),
    286: (r"digital (download|product|file)|download link|downloadable|e-?book|pdf (delivery|download)",
          r"digital (download|product|file|goods)|downloads?\b|downloadable|file delivery"),
}


def export(conn: sqlite3.Connection, cfg: dict, run_id: str, max_topics: int = 150, max_apps: int = 60) -> dict:
    d = _step_dir(cfg, "via1", run_id)
    written = 0
    for need_id, (forum_rx, app_rx) in SEARCH.items():
        n = conn.execute("SELECT rowid AS id, * FROM needs WHERE rowid = ?", (need_id,)).fetchone()
        frx, arx = re.compile(forum_rx, re.I), re.compile(app_rx, re.I)
        topics = [dict(t) for t in conn.execute(
            "SELECT topic_id, url, title, excerpt, created_at, reply_count, views, solved FROM forum_topics")
            if frx.search(f"{t['title']} {t['excerpt'] or ''}")]
        topics.sort(key=lambda t: -(t["views"] or 0))
        apps = [dict(a) for a in conn.execute("SELECT app_id, name, subtitle, review_count, rating, categories FROM app_cards")
                if arx.search(f"{a['name']} {a['subtitle']}")]
        same_cat = [a for a in apps if n["category"] in a["categories"]]
        other = [a for a in apps if n["category"] not in a["categories"]]
        srx = re.compile(SPECIFIC.get(need_id, r"$^"), re.I)
        key = lambda a: (not srx.search(f"{a['name']} {a['subtitle']}"), -a["review_count"])
        apps = sorted(same_cat, key=key) + sorted(other, key=key)
        apps = sorted(apps, key=lambda a: not srx.search(f"{a['name']} {a['subtitle']}"))   # estable: específicas primero
        examples = conn.execute(
            f"SELECT specific_problem, feature_requested FROM review_llm WHERE review_id IN "
            f"({','.join('?' * len(json.loads(n['review_ids'])))}) LIMIT 8", json.loads(n["review_ids"])).fetchall()
        payload = {"need_id": need_id, "label": n["label"], "category": n["category"], "n_reviews": n["n_reviews"],
                   "n_apps": n["n_apps"], "buildability_reason": n["buildability_reason"],
                   "review_examples": [dict(e) for e in examples],
                   "forum_candidates_total": len(topics), "forum_candidates": topics[:max_topics],
                   "app_candidates_total": len(apps), "app_candidates": apps[:max_apps]}
        f = d / f"need_{need_id}.in.json"
        if not f.exists():
            f.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            written += 1
    return {"dir": str(d), "needs": len(SEARCH), "new_files": written,
            "pending": len(pending_outputs(d, "*.in.json", ".in.json", ".out.json"))}


# Aspecto distintivo de cada necesidad: las apps que lo mencionan van primero en la lista de candidatas, para que las
# especializadas (a menudo nuevas y sin reseñas) no queden fuera por el corte.
SPECIFIC = {
    177: r"monitor|alert|uptime|health|broken|stopp?ed working|silently",
    86: r"(min|max)\w* (order|quantity|qty|purchase)|limit",
    327: r"(badge|label)s?.*(variant|collection|target|condition|rule|specific)",
    135: r"export|backup|migrat|transfer",
    171: r"(reliab|guarantee|deliverab|every subscriber|all subscribers|log|delivery report)",
    149: r"setup|set up|easy|minutes|no code|one[- ]click|onboard",
    402: r"clean|leftover|uninstall|lightweight|no code|theme app extension|residual",
    230: r"(sync|auto|automatic)\w*.*(tracking|fulfil)|(tracking|fulfil)\w*.*(sync|auto|automatic)",
    389: r"uninstall|clean|restore|revert|backup|undo|rollback|history",
    392: r"(bulk|ai|auto|generat)\w*.*alt|alt.*(bulk|ai|auto|generat)",
    179: r"(rule|threshold|limit|condition|tier)",
    286: r"(deliver|download|license|file|email)",
}

VERDICTS = ("hueco real", "cubierta por una app especializada", "evidencia débil")


def import_(conn: sqlite3.Connection, cfg: dict, run_id: str) -> ImportReport:
    conn.execute("CREATE TABLE IF NOT EXISTS via1 (run_id TEXT, need_id INTEGER, verdict TEXT, justification TEXT, "
                 "forum_topic_ids TEXT, specialized_apps TEXT, PRIMARY KEY (run_id, need_id))")
    d = _step_dir(cfg, "via1", run_id)
    rep = ImportReport()
    for fin in sorted(d.glob("*.in.json")):
        fout = d / fin.name.replace(".in.json", ".out.json")
        if not fout.exists():
            rep.pending.append(fin.name)
            continue
        rep.files += 1
        payload = json.loads(fin.read_text(encoding="utf-8"))
        topics = {t["topic_id"] for t in payload["forum_candidates"]}
        apps = {a["app_id"] for a in payload["app_candidates"]}
        try:
            o = json.loads(fout.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            rep.errors.append(f"{fout.name}: JSON inválido ({e})")
            continue
        v = (o.get("verdict") or "").strip().lower()
        tids = [int(x) for x in o.get("forum_topic_ids") or []]
        spec = o.get("specialized_apps") or []
        bad_t = set(tids) - topics
        bad_a = {s.get("app_id") for s in spec} - apps
        if v not in VERDICTS or not (o.get("justification") or "").strip() or bad_t or bad_a:
            rep.errors.append(f"{fout.name}: veredicto '{v}', justificación vacía o ids desconocidos "
                              f"(hilos {sorted(bad_t)[:3]}, apps {sorted(bad_a)[:3]})")
            continue
        conn.execute("INSERT OR REPLACE INTO via1 VALUES (?, ?, ?, ?, ?, ?)",
                     (run_id, payload["need_id"], v, o["justification"].strip(), json.dumps(tids),
                      json.dumps(spec, ensure_ascii=False)))
        rep.imported += 1
    conn.commit()
    return rep
