"""Comprueba un lote de clasificación antes de importarlo: python -m radar.check_batch <fichero.in.jsonl>"""
import json
import sys
from pathlib import Path

from .handoff import validate_classification

fin = Path(sys.argv[1])
fout = fin.with_name(fin.name.replace(".in.jsonl", ".out.jsonl"))
inputs = [json.loads(l) for l in fin.read_text(encoding="utf-8").splitlines() if l.strip()]
expected = [r["review_id"] for r in inputs]
ratings = {r["review_id"]: r.get("rating") for r in inputs}
if not fout.exists():
    sys.exit(f"Falta {fout.name}")
errors, got = [], []
for n, line in enumerate(fout.read_text(encoding="utf-8").splitlines(), 1):
    if not line.strip():
        continue
    try:
        obj = json.loads(line)
        row, err = validate_classification(obj, relaxed=ratings.get(str(obj.get("review_id"))) == 3)
    except json.JSONDecodeError as e:
        errors.append(f"línea {n}: JSON inválido ({e})")
        continue
    if err:
        errors.append(f"línea {n}: {err}")
    else:
        got.append(row["review_id"])
missing = sorted(set(expected) - set(got))
extra = sorted(set(got) - set(expected))
print(f"{fout.name}: {len(got)}/{len(expected)} válidas, {len(errors)} errores, {len(missing)} faltan, {len(extra)} sobran")
for e in errors[:20]:
    print("  ", e)
if missing:
    print("   faltan:", missing[:20])
sys.exit(1 if errors or missing or extra else 0)
