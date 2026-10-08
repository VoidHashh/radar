"""Comprueba ficheros de necesidades: python -m radar.check_needs <carpeta> <categoría> [<categoría> ...]"""
import json
import sys
from pathlib import Path

from .handoff import validate_needs

d = Path(sys.argv[1])
bad = 0
for cat in sys.argv[2:]:
    fin, fout = d / f"{cat}.in.json", d / f"{cat}.out.json"
    if not fout.exists():
        print(f"{cat}: falta {fout.name}"); bad += 1; continue
    items = {i["review_id"]: i for i in json.loads(fin.read_text(encoding="utf-8"))["items"]}
    try:
        res, err = validate_needs(json.loads(fout.read_text(encoding="utf-8")), items)
    except json.JSONDecodeError as e:
        err = f"JSON inválido ({e})"
    if err:
        print(f"{cat}: {err}"); bad += 1
print(f"{len(sys.argv) - 2 - bad}/{len(sys.argv) - 2} correctos")
sys.exit(1 if bad else 0)
