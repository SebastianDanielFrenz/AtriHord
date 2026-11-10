"""Compare the candidates' results_*.json in an eval dir: per set how often the
expected character is among the picks (known set) and how often a model names
anyone (unknown set), pairwise agreement, every disagreement and every find
only one model makes. Look at the disagreements: two optically similar
characters confused is acceptable (the current tagger does it too), a wrong
look is not. Writes <dir>/report.json.

usage: python -I eval_report.py <eval dir> [model=threshold ...]
Runbook: vision/tagger/TAGGER_UPGRADE.md.
"""
import itertools, json, os, re, sys

D = sys.argv[1]
THR = {"wd3": 0.35, "canary": 0.5, "pixai09": 0.85, "camie": 0.5, "pixai10": 0.27}
for a in sys.argv[2:]:
    k, v = a.split("=")
    THR[k] = float(v)
MODELS = [m for m in THR if os.path.exists(os.path.join(D, f"results_{m}.json"))]
R = {m: json.load(open(os.path.join(D, f"results_{m}.json"))) for m in MODELS}
SEL = {str(e["id"]): e for e in json.load(open(os.path.join(D, "tagsel.json")))}


def norm(t):
    return re.sub(r"\s+", "_", t.strip().lower())


def top(m, key):
    chars = R[m].get(key, {}).get("character", {})
    best = sorted(((norm(t), p) for t, p in chars.items()), key=lambda x: -x[1])
    best = [(t, p) for t, p in best if p >= THR[m]]
    return best


def base(t):  # furina_(genshin_impact) -> furina ; drop costume suffixes too
    return re.sub(r"_\(.*?\)", "", t)


def same(a, b):
    return a == b or base(a) == base(b)


rows = []
for key, e in sorted(SEL.items(), key=lambda kv: (kv[1]["set"], kv[1]["expect"] or "", kv[0])):
    if not any(key in R[m] for m in MODELS):
        continue
    row = {"id": key, "set": e["set"], "expect": e["expect"], "rating": e["rating"]}
    for m in MODELS:
        t = top(m, key)
        row[m] = t[:3]
    rows.append(row)

print("models:", MODELS, "thresholds:", {m: THR[m] for m in MODELS})
for s in ("known", "unknown", "cosplay"):
    sub = [r for r in rows if r["set"] == s]
    print(f"\n== {s}: {len(sub)} pictures")
    for m in MODELS:
        named = sum(1 for r in sub if r[m])
        line = f"  {m:8s} names a character in {named:3d}"
        if s == "known":
            hit = sum(1 for r in sub if r[m] and same(r[m][0][0], r["expect"]))
            anyhit = sum(1 for r in sub if any(same(t, r["expect"]) for t, _ in r[m]))
            line += f" | top-1 = expected {hit:3d} | expected among picks {anyhit:3d}"
        print(line)
    print("  pairwise (both name someone: same top-1 / both named / one names, other not):")
    for a, b in itertools.combinations(MODELS, 2):
        both = [r for r in sub if r[a] and r[b]]
        agree = sum(1 for r in both if same(r[a][0][0], r[b][0][0]))
        one = sum(1 for r in sub if bool(r[a]) != bool(r[b]))
        print(f"    {a:8s} vs {b:8s}: {agree:3d}/{len(both):3d} agree, {one:3d} only one names")

print("\n== disagreements (top-1 differs between models that name someone)")
for r in rows:
    tops = {m: r[m][0][0] for m in MODELS if r[m]}
    if len({base(t) for t in tops.values()}) > 1 or (r["expect"] and tops and not all(same(t, r["expect"]) for t in tops.values())):
        print(f"  {r['id']:>8s} {r['set']:7s} {r['rating']:9s} exp={r['expect'] or '-'}")
        for m in MODELS:
            print(f"      {m:8s} " + ", ".join(f"{t} {p:.2f}" for t, p in r[m]) if r[m] else f"      {m:8s} -")

print("\n== unknown/cosplay where a newer model names someone and wd3 does not")
for r in rows:
    if r["set"] != "known" and not r.get("wd3"):
        named = {m: r[m][0] for m in MODELS if r[m]}
        if named:
            print(f"  {r['id']:>8s} {r['set']:7s} {r['rating']:9s} " + " | ".join(f"{m}:{t} {p:.2f}" for m, (t, p) in named.items()))

json.dump(rows, open(os.path.join(D, "report.json"), "w"), indent=1)
