"""Comprueba ficheros de agrupación: python -m radar.check_cluster <carpeta> <app_id> [<app_id> ...]"""
import json
import sys
from pathlib import Path

from .handoff import validate_clusters

d = Path(sys.argv[1])
bad = 0
for app in sys.argv[2:]:
    fin, fout = d / f"{app}.in.json", d / f"{app}.out.json"
    if not fout.exists():
        print(f"{app}: falta {fout.name}"); bad += 1; continue
    ids = {r["review_id"] for r in json.loads(fin.read_text(encoding="utf-8"))["reviews"]}
    try:
        res, err = validate_clusters(json.loads(fout.read_text(encoding="utf-8")), ids)
    except json.JSONDecodeError as e:
        res, err = None, f"JSON inválido ({e})"
    if err:
        print(f"{app}: {err}"); bad += 1
print(f"{len(sys.argv) - 2 - bad}/{len(sys.argv) - 2} correctos")
sys.exit(1 if bad else 0)
