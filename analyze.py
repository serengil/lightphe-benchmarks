"""
Turns results/raw/*.jsonl into
  results/summary.csv          one row per (language, run, operation) with statistics
  results/runs.csv             one row per (language, run): status, memory, wall time
  README.md                    the full report, which is the repository's front page
  figures/*.png (+ .pdf)       figures (PDF with --pdf)
  tables/*.md|*.tex            standalone tables, only with --tables

Usage: python analyze.py [--raw results/raw]
"""
import argparse
import csv
import json
import math
import os
import statistics
import sys
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
# folder holding the LightPHE, LightECC, lightphe-go, lightphe4j and lightphe-ts checkouts;
# defaults to a "github" folder next to this benchmark repository
GITHUB = os.environ.get("LIGHTPHE_GITHUB", os.path.join(os.path.dirname(HERE), "github"))
LANGS = ["python", "go", "java", "ts"]
SAVE_PDF = False  # set by --pdf: vector figures for a paper, the Markdown report only needs PNG
FIG_DIR = os.path.join(HERE, "figures")
SAVE_TABLES = False  # set by --tables: standalone .md/.tex tables for a paper, the report embeds them


def save_table(out_dir, name, content):
    if not SAVE_TABLES:
        return
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
        f.write(content)
LANG_LABEL = {"python": "Python", "go": "Go", "java": "Java", "ts": "TypeScript"}
# validated categorical palette (light): blue, orange, aqua, yellow - plus hatches for print
LANG_COLOR = {"python": "#2a78d6", "go": "#eb6834", "java": "#1baf7a", "ts": "#eda100"}
LANG_MARKER = {"python": "o", "go": "s", "java": "D", "ts": "^"}
LANG_HATCH = {"python": "", "go": "////", "java": "\\\\\\\\", "ts": "...."}
TIERS = ["T80", "T112", "T128"]
TIER_LABEL = {"T80": "80-bit", "T112": "112-bit", "T128": "128-bit"}
OPS = ["keygen", "encrypt", "decrypt", "add", "multiply", "multiply_by_constant", "xor", "and", "reencrypt"]
OP_LABEL = {
    "keygen": "Key generation", "encrypt": "Encryption", "decrypt": "Decryption",
    "add": "Homomorphic addition", "multiply": "Homomorphic multiplication",
    "multiply_by_constant": "Scalar multiplication", "xor": "Homomorphic XOR",
    "and": "Homomorphic AND", "reencrypt": "Re-encryption",
}
SHORT = {
    "RSA": "RSA", "ElGamal": "ElGamal", "Exponential-ElGamal": "Exp-ElGamal",
    "Paillier": "Paillier", "Damgard-Jurik": "Damgård-Jurik", "Okamoto-Uchiyama": "Okamoto-Uchiyama",
    "Benaloh": "Benaloh", "Naccache-Stern": "Naccache-Stern", "Goldwasser-Micali": "Goldwasser-Micali",
    "Sander-Young-Yung": "Sander-Young-Yung", "Boneh-Goh-Nissim": "BGN", "Joye-Libert": "Joye-Libert",
    "Castagnos-Laguillaumie": "Castagnos-Laguillaumie", "Ishai-Paskin": "Ishai-Paskin",
    "Damgard-Geisler-Kroigaard": "DGK", "Bresson-Catalano-Pointcheval": "BCP",
    "Camenisch-Shoup": "Camenisch-Shoup", "EllipticCurve-Paillier": "EC-Paillier",
    "EllipticCurve-ElGamal (weierstrass)": "EC-ElGamal (Weierstrass)",
    "EllipticCurve-ElGamal (koblitz)": "EC-ElGamal (Koblitz)",
    "EllipticCurve-ElGamal (edwards)": "EC-ElGamal (Edwards)",
    "Freeman-Boneh-Goh-Nissim": "Freeman-BGN", "Regev": "Regev",
    "Brakerski-Vaikuntanathan": "BV", "Lyubashevsky-Peikert-Regev": "LPR",
    "Gentry-Sahai-Waters": "GSW", "ML-KEM": "ML-KEM", "Gentry-Halevi-Vaikuntanathan": "GHV",
}


# ------------------------------------------------------------------ loading


def load(raw_dir):
    cases = load_cases()
    runs = {r["id"]: r for r in cases["runs"]}
    records = {}
    for lang in LANGS:
        path = os.path.join(raw_dir, f"{lang}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                records[(lang, rec["run"])] = rec  # last one wins
    env = {}
    env_path = os.path.join(raw_dir, "environment.json")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            env = json.load(f)
    fp = {}
    fp_path = os.path.join(HERE, "results/footprint.json")
    if os.path.exists(fp_path):
        with open(fp_path, encoding="utf-8") as f:
            fp = json.load(f)
    return cases, runs, records, env, fp


def load_cases():
    """config/cases.json, generated from gen_cases.py when missing"""
    path = os.path.join(HERE, "config", "cases.json")
    if not os.path.exists(path):
        sys.path.insert(0, HERE)
        import gen_cases  # pylint: disable=import-outside-toplevel
        gen_cases.main()
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def describe(samples):
    s = sorted(samples)
    n = len(s)
    q = statistics.quantiles(s, n=4, method="inclusive") if n >= 2 else [s[0]] * 3
    return {
        "n": n,
        "median_ns": statistics.median(s),
        "mean_ns": statistics.fmean(s),
        "std_ns": statistics.stdev(s) if n >= 2 else 0.0,
        "p25_ns": q[0],
        "p75_ns": q[2],
        "min_ns": s[0],
        "max_ns": s[-1],
    }


def cell(records, lang, run_id, op):
    """status + statistics of one (language, run, op)"""
    rec = records.get((lang, run_id))
    if rec is None:
        return {"status": "not_run"}
    if rec["status"] == "excluded_slow":
        return {"status": "excluded_slow", "timeout_s": rec["timeout_s"]}
    ev = rec["events"]
    if op == "keygen":
        kg = [e["ns"] for e in ev if e["ev"] == "keygen"]
        fails = sum(1 for e in ev if e["ev"] == "keygen_error")
        if not kg:
            if rec["status"] == "timeout":
                return {"status": "timeout", "timeout_s": rec["timeout_s"]}
            if rec["status"] == "keygen_error":
                msg = next((e["msg"] for e in ev if e["ev"] == "keygen_error"), "")
                return {"status": "keygen_error", "msg": msg}
            return {"status": rec["status"]}
        # first key generation absorbs class loading / JIT / caches: drop it when >= 3 samples
        used = kg[1:] if len(kg) >= 3 else kg
        return {"status": "ok", **describe(used), "first_ns": kg[0], "fails": fails}
    for e in ev:
        if e["ev"] == "op_unsupported" and e["op"] == op:
            return {"status": "unsupported", "msg": e["msg"]}
    for e in ev:
        if e["ev"] == "op" and e["op"] == op:
            ok = next((c["ok"] for c in ev if c["ev"] == "check" and c["op"] == op), None)
            return {"status": "ok", **describe(e["ns"]), "warmup": e["warmup"], "check": ok}
    if rec["status"] == "timeout":
        return {"status": "timeout", "timeout_s": rec["timeout_s"]}
    if rec["status"] in ("keygen_error", "crash"):
        return {"status": rec["status"]}
    return {"status": "not_measured"}


# ------------------------------------------------------------------ formatting


def fmt_time(ns, latex=False):
    if ns is None:
        return ""
    mu = r"\textmu{}s" if latex else "µs"
    if ns < 1e3:
        return f"{ns:.0f} ns"
    if ns < 1e6:
        v, u = ns / 1e3, mu
    elif ns < 1e9:
        v, u = ns / 1e6, "ms"
    else:
        v, u = ns / 1e9, "s"
    digits = 0 if v >= 100 else (1 if v >= 10 else 2)
    return f"{v:.{digits}f} {u}"


def fmt_cell(c, latex=False, spread=False):
    st = c["status"]
    if st == "ok":
        txt = fmt_time(c["median_ns"], latex)
        if spread and c.get("n", 0) >= 2:
            dash = "--" if latex else "–"
            txt += f" ({fmt_time(c['min_ns'], latex)}{dash}{fmt_time(c['max_ns'], latex)})"
        if c.get("fails"):
            txt += f" ({c['fails']} fail)"
        if c.get("check") is False:
            txt += "†"if not latex else r"$^\dagger$"
        return txt
    if st in ("timeout", "excluded_slow"):
        return f"excl. (>{c['timeout_s']:.0f} s)" if not latex else f"excl.\\,($>${c['timeout_s']:.0f}\\,s)"
    return {"unsupported": "–", "keygen_error": "fail", "crash": "crash", "not_run": "",
            "na": "n/a", "not_measured": "?"}.get(st, st)


def fmt_bytes(b):
    if b is None:
        return ""
    if b < 1024:
        return f"{b} B"
    if b < 1024**2:
        return f"{b / 1024:.1f} KiB"
    return f"{b / 1024**2:.1f} MiB"


def tex_escape(s):
    return s.replace("&", r"\&").replace("_", r"\_").replace("%", r"\%")


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def tex_table(header_rows, rows, colspec, caption, label):
    lines = [r"\begin{table*}[t]", r"\centering", r"\scriptsize", rf"\caption{{{caption}}}",
             rf"\label{{{label}}}", rf"\begin{{tabular}}{{{colspec}}}", r"\toprule"]
    lines += header_rows
    lines.append(r"\midrule")
    lines += [" & ".join(r) + r" \\" for r in rows]
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table*}"]
    return "\n".join(lines)


# ------------------------------------------------------------------ analysis


def tier_rows(cases):
    """ordered unique labels and the run id of each (label, tier)"""
    labels, grid, notes, algo = [], {}, {}, {}
    for t in cases["tiers"]:
        if t["label"] not in labels:
            labels.append(t["label"])
        grid[(t["label"], t["tier"])] = t["run"]
        notes[(t["label"], t["tier"])] = t["note"]
        algo[t["label"]] = t["algorithm"]
    return labels, grid, notes, algo


def write_summary(runs, records, path):
    rows = []
    for (lang, rid), rec in sorted(records.items()):
        if rid == "__baseline__":
            continue
        run = runs.get(rid)
        if run is None:
            continue
        for op in ["keygen", "encrypt", "decrypt"] + run["ops"]:
            c = cell(records, lang, rid, op)
            rows.append({
                "language": lang, "run": rid, "algorithm": run["algorithm"],
                "key_size": run["key_size"], "curve": run["curve"] or "", "op": op,
                "status": c["status"], "n": c.get("n", ""),
                **{k: (round(c[k]) if c.get(k) is not None else "") for k in
                   ["median_ns", "mean_ns", "std_ns", "p25_ns", "p75_ns", "min_ns", "max_ns"]},
                "check": c.get("check", ""),
            })
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return rows


def baselines(records):
    return {lang: records[(lang, "__baseline__")]["maxrss_bytes"]
            for lang in LANGS if (lang, "__baseline__") in records}


def write_runs(runs, records, path):
    base = baselines(records)
    rows = []
    for (lang, rid), rec in sorted(records.items()):
        if rid == "__baseline__":
            continue
        rows.append({
            "language": lang, "run": rid, "status": rec["status"], "wall_s": rec["wall_s"],
            "maxrss_bytes": rec["maxrss_bytes"],
            "net_rss_bytes": rec["maxrss_bytes"] - base.get(lang, 0),
            "user_cpu_s": round(rec["user_cpu_s"], 3), "sys_cpu_s": round(rec["sys_cpu_s"], 3),
        })
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def op_table(cases, records, op, out_dir):
    labels, grid, notes, algo = tier_rows(cases)
    runs = {r["id"]: r for r in cases["runs"]}
    md_rows, tex_rows = [], []
    for label in labels:
        # skip schemes that do not implement the op at all
        candidate = [grid[(label, t)] for t in TIERS if grid.get((label, t))]
        if op not in ("keygen", "encrypt", "decrypt"):
            if not candidate or op not in runs[candidate[0]]["ops"]:
                continue
            if all(cell(records, "python", rid, op)["status"] == "unsupported" for rid in candidate):
                continue
        if not candidate:
            continue
        md_row, tex_row = [SHORT.get(label, label)], [tex_escape(SHORT.get(label, label))]
        for t in TIERS:
            rid = grid.get((label, t))
            for lang in LANGS:
                c = cell(records, lang, rid, op) if rid else {"status": "na"}
                md_row.append(fmt_cell(c, spread=op == "keygen"))
                tex_row.append(fmt_cell(c, latex=True, spread=op == "keygen"))
        md_rows.append(md_row)
        tex_rows.append(tex_row)
    if not md_rows:
        return None
    header = ["Scheme"] + [f"{TIER_LABEL[t]} {LANG_LABEL[l]}" for t in TIERS for l in LANGS]
    md = md_table(header, md_rows)
    h1 = "Scheme & " + " & ".join(rf"\multicolumn{{4}}{{c}}{{{TIER_LABEL[t]}}}" for t in TIERS) + r" \\"
    cm = " ".join(rf"\cmidrule(lr){{{2 + 4 * i}-{5 + 4 * i}}}" for i in range(3))
    h2 = " & " + " & ".join(["Py", "Go", "Java", "TS"] * 3) + r" \\"
    caption = (f"{OP_LABEL[op]}: median latency per operation (single core). "
               "-- not supported, n/a no parameter at this level.")
    if op == "keygen":
        caption = ("Key generation: median (min--max) over up to 10 fresh key pairs. Key generation time is "
                   "dominated by randomized prime search and by each port's primality-test policy; it is "
                   "NOT an indicator of language or implementation performance.")
    tex = tex_table([h1, cm, h2], tex_rows, "l" + "rrrr" * 3, caption, f"tab:{op}")
    save_table(out_dir, f"{op}.md", md + "\n")
    save_table(out_dir, f"{op}.tex", tex + "\n")
    return md


def speedups(cases, records):
    """per language and op, the geometric mean of python_median / lang_median over the tier cells"""
    labels, grid, _, _ = tier_rows(cases)
    ratios = defaultdict(list)
    for label in labels:
        for t in TIERS:
            rid = grid.get((label, t))
            if not rid:
                continue
            for op in OPS:
                if op == "keygen":
                    continue  # randomness-dominated, not a language indicator
                py = cell(records, "python", rid, op)
                if py["status"] != "ok":
                    continue
                for lang in LANGS[1:]:
                    c = cell(records, lang, rid, op)
                    if c["status"] == "ok" and c["median_ns"] > 0:
                        ratios[(lang, op)].append(py["median_ns"] / c["median_ns"])
    out = {}
    for (lang, op), rs in ratios.items():
        out[(lang, op)] = {
            "geomean": math.exp(sum(math.log(r) for r in rs) / len(rs)),
            "min": min(rs), "max": max(rs), "n": len(rs),
        }
    return out


def unique_tier_runs(cases):
    """tier cells collapse onto shared runs (lattice); count each run once"""
    seen = set()
    out = []
    for t in cases["tiers"]:
        if t["run"] and t["run"] not in seen:
            seen.add(t["run"])
            out.append(t)
    return out


def sizes_table(cases, records, out_dir):
    labels, grid, notes, _ = tier_rows(cases)
    rows, tex_rows = [], []
    for label in labels:
        for t in TIERS:
            rid = grid.get((label, t))
            if not rid:
                continue
            rec = records.get(("python", rid))
            sz = next((e for e in rec["events"] if e["ev"] == "sizes"), None) if rec else None
            if not sz:
                continue
            r = [SHORT.get(label, label), TIER_LABEL[t], notes[(label, t)] or rid.split("|")[1],
                 fmt_bytes(sz["public_key_raw"]), fmt_bytes(sz["private_key_raw"]),
                 fmt_bytes(sz["ciphertext_raw"]), fmt_bytes(sz["ciphertext_json"])]
            rows.append(r)
            tex_rows.append([tex_escape(x) for x in r])
    header = ["Scheme", "Level", "Parameter", "Public key", "Private key", "Ciphertext", "Ciphertext (JSON)"]
    md = md_table(header, rows)
    tex = tex_table([" & ".join(header) + r" \\"], tex_rows, "lllrrrr",
                    "Key and ciphertext sizes. Raw = sum of minimal big-endian encodings of every "
                    "integer; JSON = LightPHE serialization (identical format in all four ports).",
                    "tab:sizes")
    save_table(out_dir, "sizes.md", md + "\n")
    save_table(out_dir, "sizes.tex", tex + "\n")
    return md


def memory_table(cases, records, out_dir):
    labels, grid, _, _ = tier_rows(cases)
    base = baselines(records)
    rows, tex_rows = [], []
    for label in labels:
        r = [SHORT.get(label, label)]
        any_val = False
        for t in TIERS:
            rid = grid.get((label, t))
            for lang in LANGS:
                rec = records.get((lang, rid)) if rid else None
                if rec is None or rec["status"] not in ("ok",):
                    r.append("" if rec is None else rec["status"])
                    continue
                any_val = True
                r.append(f"{max(0, rec['maxrss_bytes'] - base.get(lang, 0)) / 2**20:.1f}")
        if any_val:
            rows.append(r)
            tex_rows.append([tex_escape(x) for x in r])
    header = ["Scheme"] + [f"{TIER_LABEL[t]} {LANG_LABEL[l]}" for t in TIERS for l in LANGS]
    md = "Baseline peak RSS (runtime + library loaded, no work): " + ", ".join(
        f"{LANG_LABEL[l]} {base[l] / 2**20:.1f} MiB" for l in LANGS if l in base) + "\n\n"
    md += md_table(header, rows)
    h1 = "Scheme & " + " & ".join(rf"\multicolumn{{4}}{{c}}{{{TIER_LABEL[t]}}}" for t in TIERS) + r" \\"
    cm = " ".join(rf"\cmidrule(lr){{{2 + 4 * i}-{5 + 4 * i}}}" for i in range(3))
    h2 = " & " + " & ".join(["Py", "Go", "Java", "TS"] * 3) + r" \\"
    tex = tex_table([h1, cm, h2], tex_rows, "l" + "rrrr" * 3,
                    "Peak resident memory of a full benchmark run (MiB) above the runtime's own "
                    "baseline. Garbage-collected runtimes report heap high-water marks, not live data.",
                    "tab:memory")
    save_table(out_dir, "memory.md", md + "\n")
    save_table(out_dir, "memory.tex", tex + "\n")
    return md


def sweep_table(cases, records, out_dir):
    rows = []
    for run in sorted(cases["runs"], key=lambda r: (r["algorithm"] == "Gentry-Halevi-Vaikuntanathan",
                                                    r["algorithm"], r["key_size"] or 0)):
        if "sweep" not in run["roles"] and "learning" not in run["roles"]:
            continue
        for op in ["keygen", "encrypt", "decrypt"] + run["ops"]:
            r = [SHORT.get(run["algorithm"], run["algorithm"]), str(run["key_size"]), OP_LABEL[op]]
            any_ok = False
            for lang in LANGS:
                c = cell(records, lang, run["id"], op)
                any_ok |= c["status"] == "ok"
                r.append(fmt_cell(c))
            if any_ok or op == "keygen":
                rows.append(r)
    md = md_table(["Scheme", "n / set", "Operation"] + [LANG_LABEL[l] for l in LANGS], rows)
    save_table(out_dir, "lattice_sweep.md", md + "\n")
    return md


def footprint_table(fp, out_dir):
    if not fp:
        return ""
    rows = []
    for lang in LANGS:
        d = fp[lang]
        loc = d["source"]["loc"]
        loc_txt = f"{loc:,}"
        if lang == "python" and "source_lightecc" in d:
            loc_txt += f" (+{d['source_lightecc']['loc']:,} LightECC)"
        rows.append([LANG_LABEL[lang], fmt_bytes(d["library_bytes"]), str(d["runtime_dependency_count"]),
                     fmt_bytes(d["runtime_dependency_bytes"]), loc_txt,
                     fmt_bytes(d.get("static_binary_bytes")) or fmt_bytes(d.get("npm_tarball_bytes")) or ""])
    md = md_table(["Language", "Library artifact", "Runtime deps", "Deps size", "Source LOC", "Binary / npm tarball"], rows)
    save_table(out_dir, "footprint.md", md + "\n")
    return md


# ------------------------------------------------------------------ figures


def style():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 8, "axes.titlesize": 9,
        "axes.edgecolor": "#8a8984", "axes.linewidth": 0.6, "axes.labelcolor": "#52514e",
        "xtick.color": "#52514e", "ytick.color": "#52514e", "text.color": "#0b0b0b",
        "axes.spines.top": False, "axes.spines.right": False,
        "grid.color": "#e4e3df", "grid.linewidth": 0.6, "legend.frameon": False,
        "savefig.dpi": 200, "figure.facecolor": "#ffffff", "axes.facecolor": "#ffffff",
        "hatch.linewidth": 0.5,
    })


def bar_figure(cases, records, op, tier, out_dir):
    """dot plot: one row per scheme, one marker per language on a log axis"""
    labels, grid, _, _ = tier_rows(cases)
    runs = {r["id"]: r for r in cases["runs"]}
    data = []
    for label in labels:
        rid = grid.get((label, tier))
        if not rid:
            continue
        if op not in ("keygen", "encrypt", "decrypt") and op not in runs[rid]["ops"]:
            continue
        vals = {lang: cell(records, lang, rid, op) for lang in LANGS}
        if not any(v["status"] == "ok" for v in vals.values()):
            continue
        data.append((SHORT.get(label, label), vals))
    if not data:
        return None
    fig, ax = plt.subplots(figsize=(6.8, 0.34 * len(data) + 1.2))
    dodge = {"python": -0.24, "go": -0.08, "java": 0.08, "ts": 0.24}
    for i, (_, vals) in enumerate(data):
        if i % 2 == 0:
            ax.axhspan(i - 0.5, i + 0.5, color="#f4f3f0", zorder=0, linewidth=0)
    for lang in LANGS:
        ys = [i + dodge[lang] for i, (_, v) in enumerate(data) if v[lang]["status"] == "ok"]
        xs = [v[lang]["median_ns"] / 1e6 for _, v in data if v[lang]["status"] == "ok"]
        ax.scatter(xs, ys, s=30, marker=LANG_MARKER[lang], color=LANG_COLOR[lang],
                   edgecolors="white", linewidths=0.8, zorder=3, label=LANG_LABEL[lang])
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(log_tick))
    ax.set_yticks(range(len(data)))
    ax.set_yticklabels([d[0] for d in data])
    ax.set_ylim(len(data) - 0.5, -0.5)
    ax.set_xlabel("median latency (ms, log scale)")
    ax.grid(axis="x", which="major")
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    # timeouts are written as text at the right edge so they are not mistaken for data
    xmax = ax.get_xlim()[1]
    for i, (_, vals) in enumerate(data):
        out = [LANG_LABEL[l] for l in LANGS if vals[l]["status"] in ("timeout", "excluded_slow")]
        bad = [LANG_LABEL[l] for l in LANGS if vals[l]["status"] == "keygen_error"]
        note = "; ".join(x for x in [
            ("too slow: " + ", ".join(out)) if out else "",
            ("no key pair: " + ", ".join(bad)) if bad else ""] if x)
        if note:
            ax.text(xmax, i, note + " ", ha="right", va="center", fontsize=6, color="#52514e")
    ax.set_title(f"{OP_LABEL[op]} at {TIER_LABEL[tier]} security", loc="left", pad=18)
    ax.legend(ncol=4, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=7,
              handletextpad=0.3, columnspacing=1.2, borderaxespad=0.2)
    fig.tight_layout()
    base = os.path.join(out_dir, f"{op}_{tier}")
    if SAVE_PDF:
        fig.savefig(base + ".pdf")
    fig.savefig(base + ".png")
    plt.close(fig)
    return base + ".png"


def log_tick(x, _pos):
    if x >= 1:
        return f"{x:,.0f}"
    return f"{x:g}"


def scaling_figure(cases, records, op, out_dir):
    labels, grid, _, _ = tier_rows(cases)
    chosen = ["RSA", "ElGamal", "Paillier", "Damgard-Jurik", "Okamoto-Uchiyama", "Joye-Libert",
              "EllipticCurve-ElGamal (weierstrass)", "EllipticCurve-ElGamal (koblitz)", "Freeman-Boneh-Goh-Nissim"]
    chosen = [c for c in chosen if c in labels]
    fig, axes = plt.subplots(3, 3, figsize=(6.8, 5.6), sharex=True)
    for ax, label in zip(axes.flat, chosen):
        for lang in LANGS:
            xs, ys = [], []
            for i, t in enumerate(TIERS):
                rid = grid.get((label, t))
                c = cell(records, lang, rid, op) if rid else {"status": "na"}
                if c["status"] == "ok":
                    xs.append(i)
                    ys.append(c["median_ns"] / 1e6)
            ax.plot(xs, ys, color=LANG_COLOR[lang], linewidth=2, marker=LANG_MARKER[lang], markersize=4.5,
                    markeredgecolor="white", markeredgewidth=1, label=LANG_LABEL[lang])
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(FuncFormatter(log_tick))
        ax.set_title(SHORT.get(label, label), loc="left", fontsize=8)
        ax.grid(axis="y")
        ax.set_xticks(range(3))
        ax.set_xticklabels(["80", "112", "128"])
    for ax in axes[-1]:
        ax.set_xlabel("security (bits)")
    for ax in axes[:, 0]:
        ax.set_ylabel("ms (log)")
    handles, lbls = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, lbls, ncol=4, loc="upper left", bbox_to_anchor=(0.01, 1.0), fontsize=7)
    fig.suptitle(f"{OP_LABEL[op]}: scaling with security level", x=0.01, ha="left", y=0.955, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    base = os.path.join(out_dir, f"scaling_{op}")
    if SAVE_PDF:
        fig.savefig(base + ".pdf")
    fig.savefig(base + ".png")
    plt.close(fig)
    return base + ".png"


def speedup_figure(sp, out_dir):
    ops = [op for op in OPS if any((l, op) in sp for l in LANGS[1:])]
    if not ops:
        return None
    fig, ax = plt.subplots(figsize=(6.8, 2.8))
    w = 0.26
    for j, lang in enumerate(LANGS[1:]):
        xs = [i + (j - 1) * w for i in range(len(ops))]
        ys = [sp.get((lang, op), {}).get("geomean", float("nan")) for op in ops]
        ax.bar(xs, [y - 1 for y in ys], bottom=1, width=w * 0.9, color=LANG_COLOR[lang], hatch=LANG_HATCH[lang],
               edgecolor="white", linewidth=0.3, label=LANG_LABEL[lang])
    ax.axhline(1, color="#52514e", linewidth=0.8)
    ax.set_yscale("log")
    ticks = [0.125, 0.25, 0.5, 1, 2, 4, 8, 16]
    ax.set_yticks(ticks)
    ax.set_yticklabels([f"{t:g}×" for t in ticks])
    ax.minorticks_off()
    ax.text(-0.45, 1.03, "Python = 1×", va="bottom", ha="left", fontsize=6.5, color="#52514e")
    ax.set_xticks(range(len(ops)))
    ax.set_xticklabels([OP_LABEL[o].replace("Homomorphic ", "Hom. ") for o in ops], rotation=20, ha="right")
    ax.set_ylabel("speedup over Python\n(geometric mean, log)")
    ax.grid(axis="y")
    ax.set_axisbelow(True)
    ax.legend(ncol=3, loc="upper right", fontsize=7)
    fig.tight_layout()
    base = os.path.join(out_dir, "speedup_vs_python")
    if SAVE_PDF:
        fig.savefig(base + ".pdf")
    fig.savefig(base + ".png")
    plt.close(fig)
    return base + ".png"


# ------------------------------------------------------------------ report


def problems(cases, runs, records):
    """correctness failures, timeouts, errors and cross-language support mismatches"""
    lines = []
    for (lang, rid), rec in sorted(records.items()):
        if rid == "__baseline__" or rid not in runs:
            continue
        for e in rec["events"]:
            if e["ev"] == "check" and not e["ok"]:
                lines.append(f"- **check failed** {LANG_LABEL[lang]} `{rid}` `{e['op']}` got `{e.get('got')}`")
        if rec["status"] in ("keygen_error", "crash"):
            msg = next((e["msg"] for e in rec["events"] if e["ev"] == "keygen_error"), None)
            msg = msg or " / ".join(rec.get("stderr_tail", [])[-2:])
            lines.append(f"- {rec['status']} {LANG_LABEL[lang]} `{rid}`: {msg[:200]}")
    for rid, run in runs.items():
        for op in run["ops"]:
            st = {lang: cell(records, lang, rid, op)["status"] for lang in LANGS if (lang, rid) in records}
            sup = {l for l, s in st.items() if s == "unsupported"}
            ok = {l for l, s in st.items() if s == "ok"}
            if sup and ok:
                lines.append(f"- support mismatch `{rid}` `{op}`: ok in {sorted(ok)}, unsupported in {sorted(sup)}")
    return lines


def slow_md(runs, records):
    """(run, language) pairs killed at the time limit, in the pilot or in the full run"""
    rows = []
    for (lang, rid), rec in sorted(records.items(), key=lambda x: (x[0][1], LANGS.index(x[0][0]))):
        if rid == "__baseline__" or rid not in runs:
            continue
        if rec["status"] == "excluded_slow":
            rows.append([f"`{rid}`", LANG_LABEL[lang], "pilot", rec.get("excluded", "")])
        elif rec["status"] == "timeout":
            stage = "key generation" if not any(e["ev"] == "keygen" for e in rec["events"]) else "operations"
            rows.append([f"`{rid}`", LANG_LABEL[lang], "full run",
                         f"killed after {rec['timeout_s']:.0f} s during {stage}"])
        elif rec["status"] == "keygen_error":
            errs = [e for e in rec["events"] if e["ev"] == "keygen_error"]
            secs = ", ".join(f"{e.get('ns', 0) / 1e9:.0f} s" for e in errs)
            rows.append([f"`{rid}`", LANG_LABEL[lang], "full run",
                         f"no key pair: {len(errs)} of {len(errs)} attempts failed ({secs}): {errs[0]['msg']}"])
    head = ("Each (scheme, parameter, language) pair has a hard wall-clock limit. Pairs that hit it are "
            "killed, excluded from the result tables (shown as `excl.`) and listed here. Pairs whose "
            "randomized key search exhausted `max_tries` in every attempt within the key generation "
            "budget are listed too (shown as `fail`). The other languages for the same scheme are "
            "still reported.\n\n")
    if not rows:
        return head + "None."
    return head + md_table(["Run", "Language", "Where", "Reason"], rows)


def params_md(runs, records):
    """bit lengths of plaintext / ciphertext moduli per port; flags runs whose ports disagree by > 2 bits"""
    rows, worst = [], (0.0, None)
    for rid in runs:
        vals = {}
        for lang in LANGS:
            rec = records.get((lang, rid))
            ev = next((e for e in rec["events"] if e["ev"] == "params"), None) if rec else None
            if ev:
                vals[lang] = (ev.get("pm_bits"), ev.get("cm_bits"))
        cms = [v[1] for v in vals.values() if v[1]]
        if len(cms) >= 2:
            rel = (max(cms) - min(cms)) / max(cms)
            if rel > worst[0]:
                worst = (rel, rid)
        if len(cms) >= 2 and (max(cms) - min(cms)) / max(cms) > 0.01:
            rows.append([f"`{rid}`"] + [f"{vals[l][0]} / {vals[l][1]}" if l in vals else "" for l in LANGS])
    head = ("Bit lengths of plaintext / ciphertext moduli are compared across ports. Prime generators differ "
            "slightly (e.g. Go forces the top bit, Python draws from a range), so moduli may vary by a few "
            "bits. ")
    head += (f"Largest relative difference of the ciphertext modulus between ports: {worst[0] * 100:.2f}% "
             f"(`{worst[1]}`). " if worst[1] else "")
    head += "Runs differing by more than 1%:\n\n"
    if not rows:
        return head + "None, so all ports run equivalent parameters."
    return head + md_table(["Run"] + [LANG_LABEL[l] for l in LANGS], rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default=os.path.join(HERE, "results/raw"))
    parser.add_argument("--report", default=HERE, help="folder for README.md and figures/")
    parser.add_argument("--pdf", action="store_true", help="also write figures as vector PDF (for a paper)")
    parser.add_argument("--tables", action="store_true",
                        help="also write every table to tables/ as .md and booktabs .tex (for a paper)")
    opts = parser.parse_args()
    global SAVE_PDF, SAVE_TABLES  # pylint: disable=global-statement
    SAVE_PDF = opts.pdf
    SAVE_TABLES = opts.tables

    cases, runs, records, env, fp = load(opts.raw)
    tables = os.path.join(opts.report, "tables")
    figures = os.path.join(opts.report, "figures")
    global FIG_DIR  # pylint: disable=global-statement
    FIG_DIR = figures
    os.makedirs(figures, exist_ok=True)
    results_dir = os.path.dirname(os.path.abspath(opts.raw))
    write_summary(runs, records, os.path.join(results_dir, "summary.csv"))
    write_runs(runs, records, os.path.join(results_dir, "runs.csv"))

    style()
    op_md = {op: op_table(cases, records, op, tables) for op in OPS}
    sp = speedups(cases, records)
    figs = []
    for op in ["keygen", "encrypt", "decrypt", "add", "multiply", "multiply_by_constant", "xor", "and", "reencrypt"]:
        for t in TIERS:
            p = bar_figure(cases, records, op, t, figures)
            if p:
                figs.append(p)
    for op in ["encrypt", "decrypt"]:  # keygen is randomness-dominated, no scaling figure
        scaling_figure(cases, records, op, figures)
    speedup_figure(sp, figures)

    sp_rows = [[OP_LABEL[op]] + [
        (f"{sp[(l, op)]['geomean']:.1f}× ({sp[(l, op)]['min']:.1f}–{sp[(l, op)]['max']:.1f}, n={sp[(l, op)]['n']})"
         if (l, op) in sp else "") for l in LANGS[1:]] for op in OPS if any((l, op) in sp for l in LANGS[1:])]
    sp_md = md_table(["Operation"] + [LANG_LABEL[l] for l in LANGS[1:]], sp_rows)
    save_table(tables, "speedup.md", sp_md + "\n")

    report = build_report(env, fp, cases, records, runs, op_md, sp_md,
                          sizes_table(cases, records, tables), memory_table(cases, records, tables),
                          sweep_table(cases, records, tables), footprint_table(fp, tables),
                          problems(cases, runs, records), os.path.join(opts.report, "README.md"))
    with open(os.path.join(opts.report, "README.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print(f"report written to {os.path.join(opts.report, 'README.md')}")


def build_report(env, fp, cases, records, runs, op_md, sp_md, sizes_md, mem_md, sweep_md, fp_md, probs,
                 readme):
    timing = env.get("timing", {})
    commits = env.get("commits", {})
    n_ok = sum(1 for (l, r), rec in records.items() if r != "__baseline__" and rec["status"] == "ok")
    n_all = sum(1 for (l, r) in records if r != "__baseline__")
    parts = [
        "# LightPHE cross-language benchmark\n",
        INTRO,
        f"{n_ok} of {n_all} (run, language) executions completed. This page is generated by "
        "`analyze.py`; see [Reproduce](#reproduce) at the end.\n",
        highlights(readme),
        "## Environment\n",
        md_table(["Item", "Value"], [
            ["Date", env.get("date", "")], ["CPU", env.get("cpu", "")],
            ["Logical cores", env.get("cores_logical", "")],
            ["Memory", fmt_bytes(env.get("memory_bytes") or 0)], ["OS", env.get("os", "")],
            ["Python", env.get("python", "")], ["Go", env.get("go", "")],
            ["Java", (env.get("java") or "").splitlines()[0] if env.get("java") else ""],
            ["Node.js", env.get("node", "")],
        ] + [[k, v] for k, v in library_versions(records).items()]),
        "\n## Methodology\n",
        METHODOLOGY.format(**{k: timing.get(k, "?") for k in
                              ["kg_reps", "kg_budget", "warm_s", "warm_max", "op_reps", "op_budget", "op_min", "max_tries"]}),
        "\n## Speedup over Python\n",
        "Geometric mean over every (scheme, security level) cell where both Python and the port finished; "
        "range and cell count in parentheses. Key generation is deliberately left out: its time is "
        "dominated by randomness, see *Key generation (not a performance indicator)*.\n\n" + sp_md + "\n\n![speedup](figures/speedup_vs_python.png)\n",
    ]
    for op in [o for o in OPS if o != "keygen"]:
        if op_md.get(op):
            parts.append(f"\n## {OP_LABEL[op]}\n\nMedian latency. `–` not supported by the scheme, "
                         "`n/a` no parameter at that level or not run, `excl.` killed at the time limit, `fail` no key pair within `max_tries`, `(k fail)` k key generation attempts exhausted `max_tries`, "
                         "`†` decryption of the result did not match.\n\n" + op_md[op] + "\n")
            for t in TIERS:
                if os.path.exists(os.path.join(FIG_DIR, f"{op}_{t}.png")):
                    parts.append(f"\n![{op} {t}](figures/{op}_{t}.png)\n")
            if os.path.exists(os.path.join(FIG_DIR, f"scaling_{op}.png")):
                parts.append(f"\n![scaling {op}](figures/scaling_{op}.png)\n")
    if op_md.get("keygen"):
        parts.append("\n## Key generation (not a performance indicator)\n\n" + KEYGEN_CAVEAT
                     + "\n" + op_md["keygen"] + "\n")
        for t in TIERS:
            if os.path.exists(os.path.join(FIG_DIR, f"keygen_{t}.png")):
                parts.append(f"\n![keygen {t}](figures/keygen_{t}.png)\n")
    parts += [
        "\n## Key and ciphertext sizes\n\nComputed once from the Python run: the four ports use the "
        "same parameters and the same JSON serialization format, so sizes do not depend on the language. "
        "Plaintexts are 17 and 23 (single bits for bitwise schemes).\n\n" + sizes_md + "\n",
        "\n## Peak memory\n\n" + mem_md + "\n",
        "\n## Package footprint\n\n" + fp_md + "\n",
        "\n## Lattice parameter sweep, ML-KEM parameter sets, learning-only GHV\n\n" + sweep_md + "\n",
        "\n## ⚠ Excluded: too slow or no key pair\n\n" + slow_md(runs, records) + "\n",
        "\n## Excluded schemes\n\n" + "\n".join(
            f"- **{k}**: {v}" for k, v in cases.get("excluded", {}).items()) + "\n",
        "\nRun at small keys only:\n\n" + "\n".join(
            f"- **{k}**: 80-bit level only; {v}." for k, v in cases.get("small_keys_only", {}).items()) + "\n",
        "\n## Parameter equivalence across ports\n\n" + params_md(runs, records) + "\n",
        "\n## Failures and inconsistencies\n\n" + ("\n".join(probs) if probs else "None.") + "\n",
        "\n## Threats to validity\n\n" + THREATS,
        "\n" + REPRODUCE,
        CITATION,
        LICENSE_SECTION,
    ]
    return "\n".join(parts)


def setup_version(path):
    import re
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'version="([^"]+)"', f.read())
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def library_versions(records):
    """library versions reported by each driver; Python's from its setup.py"""
    reported = {}
    for (lang, _), rec in records.items():
        ev = next((e for e in rec["events"] if e["ev"] == "start"), None)
        if ev and ev.get("library") not in (None, "unknown"):
            reported[lang] = ev["library"]
    gh = "https://github.com/serengil"
    return {
        "LightPHE (Python)": f"[{setup_version(GITHUB + '/LightPHE/setup.py')}]({gh}/LightPHE)",
        "LightECC (Python dependency)": f"[{setup_version(GITHUB + '/LightECC/setup.py')}]({gh}/LightECC)",
        "lightphe-go": f"[{reported.get('go', 'unknown')}]({gh}/lightphe-go)",
        "lightphe4j": f"[{reported.get('java', 'unknown')}]({gh}/lightphe4j)",
        "lightphe-ts": f"[{reported.get('ts', 'unknown')}]({gh}/lightphe-ts)",
    }


def highlights(readme):
    """the hand-written ## Highlights section of the existing README, kept across regenerations"""
    if not os.path.exists(readme):
        return ""
    with open(readme, encoding="utf-8") as f:
        text = f.read()
    head = "\n## Highlights\n"
    i = text.find(head)
    if i < 0:
        return ""
    j = text.find("\n## ", i + len(head))
    return text[i + 1:j if j >= 0 else len(text)].rstrip() + "\n"


INTRO = """\
Benchmarks the four implementations of LightPHE, a lightweight partially homomorphic encryption
library, against each other. It covers encryption, decryption, every homomorphic operation each scheme
supports and key generation at three security levels (80, 112 and 128 bit), and also reports
key/ciphertext sizes, peak memory and package footprint.

| Language | Repository |
|---|---|
| Python (reference) | [serengil/LightPHE](https://github.com/serengil/LightPHE), with its elliptic curve dependency [serengil/LightECC](https://github.com/serengil/LightECC) |
| Go | [serengil/lightphe-go](https://github.com/serengil/lightphe-go) |
| Java | [serengil/lightphe4j](https://github.com/serengil/lightphe4j) |
| TypeScript | [serengil/lightphe-ts](https://github.com/serengil/lightphe-ts) |
"""

REPRODUCE = """\
## Reproduce

| Path | What |
|---|---|
| `build.sh` | builds the Go, Java and TypeScript drivers against local checkouts of the ports |
| `gen_cases.py` | schemes, security tiers, parameters and exclusions (generated into `config/cases.json` on first use) |
| `drivers/{python,go,java,ts}/` | one benchmark driver per language, same protocol (`key=value` args, `@@BENCH` JSON events) |
| `run.py` | orchestrator: one process per (run, language), hard timeout, peak RSS via `wait4`, resumable |
| `footprint.py` | package size, dependencies, lines of code |
| `analyze.py` | statistics, figures and this README |

The *Highlights* section is written by hand: edit it directly in this README, and `analyze.py`
keeps it when it regenerates the rest of the page. Measurements are written to `results/` (raw samples, `summary.csv` with full statistics,
`runs.csv`), which is not committed. `python analyze.py --pdf` also writes vector figures, and
`--tables` writes every table as `.md` and booktabs `.tex`, both meant for a paper.

Clone the implementations side by side. By default this is a `github` folder next to this repository:

```
<parent>/
  lightphe-benchmarks/   (this repo)
  github/LightPHE  github/LightECC  github/lightphe-go  github/lightphe4j  github/lightphe-ts
```

Requirements: Python with LightPHE installed (e.g. `pip install -e github/LightPHE`) plus
matplotlib, Go, JDK 17+, Node.js. Settings, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `LIGHTPHE_GITHUB` | `../github` | folder holding the five checkouts |
| `LIGHTPHE_PYTHON` | the interpreter running `run.py` | Python used for the Python driver |
| `LIGHTPHE_JAVA_HOME` | `lightphe4j/scripts/java-home.sh`, then `JAVA_HOME` | JDK 17+ |
| `LIGHTPHE_TS_DIST` | `$LIGHTPHE_GITHUB/lightphe-ts/dist/esm/index.js` | lightphe-ts build |

```sh
./build.sh                       # Go binary, lightphe4j jar + classpath + Java driver, lightphe-ts dist
caffeinate -i -s python run.py --quick --timeout 300 --out results/pilot    # pilot, 1 sample each
caffeinate -i -s python run.py --timeout 900 --skip-slow-from results/pilot  # full run
python footprint.py
python analyze.py                # this README and figures/
```

`run.py` resumes after an interruption. `--redo` starts over, and `--only <substring>` and `--langs` select
subsets. Keep the machine awake (`caffeinate` on macOS): sleep pauses the timers and corrupts
the samples in progress. Don't run the benchmark in hosted CI. It is hours of CPU-bound work and
belongs on a dedicated machine.
"""


LICENSE_SECTION = '# License\n\nThis benchmark is licensed under the MIT License - see [`LICENSE`](LICENSE) for more details.\n'


CITATION = '# Citation\n\nPlease cite LightPHE in your publications if it helps your research. Here is its BibTex entry:\n\n```BibTeX\n@article{lightphe,\n  title          = {Sustainable Cryptography: Carbon Asymmetry in Partially Homomorphic Encryption in the Cloud},\n  author         = {Ozpinar, Alper and Serengil, Sefik Ilkin},\n  journal        = {Symmetry},\n  volume         = {18},\n  number         = {5},\n  pages          = {832},\n  url            = {https://www.mdpi.com/2073-8994/18/5/832},\n  doi            = {10.3390/sym18050832},\n  note           = {Special Issue: Symmetry in Cryptography and Cybersecurity},\n  year           = {2026}\n}\n```\n'


KEYGEN_CAVEAT = """\
> **⚠ Key generation times are not an indicator of language or implementation performance.**
> Most schemes generate keys by randomized search: they draw random candidates until one is
> prime, or until the primes satisfy extra conditions (safe primes, a block size r dividing
> p - 1, and so on). The number of draws is a random variable, so the same implementation in the same
> language can finish quickly on a lucky draw or take far longer on an unlucky one. For example,
> Benaloh draws primes until r (about 1,000-2,000) divides p - 1, which takes about r attempts on average
> with a geometric distribution, so one Benaloh key generation can take 1 s and the next 200 s
> in the same language. This run measured the same effect directly: Java needed between 0.17 s and
> 6.13 s (37x) for Joye-Libert-3072 keys across 9 draws, and several schemes vary 13-20x within
> one language. The ports also use different primality tests (sympy BPSW in Python, `crypto/rand.Prime`
> in Go, `isProbablePrime(40)` in Java, 40 Miller-Rabin rounds in TypeScript), and some differ in the
> search procedure itself. Key generation is also a one-time cost per key pair.
>
> The numbers below are reported for completeness, as median (min-max) over up to 10 fresh key
> pairs. They are left out of the speedup summary. `(k fail)` marks k attempts that exhausted
> `max_tries`.
"""


METHODOLOGY = """\
**What is timed.** The raw cryptosystem primitives of each port (Python/TypeScript `cs`,
Go `Scheme()`, Java `cs()`), so the wrapper layers (fixed-point encoding, tensors,
Ciphertext objects) are excluded. Key generation is timed through each port's public
constructor (`LightPHE(...)`, `lightphe.New`, `LightPHE.builder()`, `new LightPHE({{...}})`),
since that is the only public key generation entry point.

**Process isolation.** Every (scheme, parameter, language) runs in a fresh OS process.
Languages are interleaved per scheme (Python, Go, Java, TS, next scheme) so slow drift of
the machine affects all languages alike. Each process has a hard wall-clock timeout; a
timeout means the run was killed, and the table shows the limit instead of a value.

**Key generation.** Reported separately and **not** used as a performance indicator,
because its time is dominated by randomness (see that section). Up to {kg_reps} fresh key pairs or until {kg_budget} s have been spent
(at least one). When at least three samples exist the first one is discarded, since it
absorbs class loading, JIT compilation and lazy initialization. Randomized key searches are
bounded by `max_tries={max_tries}` (library default) in every port.

**Operations.** For encryption, decryption and each homomorphic operation the scheme
supports: warm-up for {warm_s} s or {warm_max} calls (at least one), then up to {op_reps}
individually timed calls, stopping early once {op_budget} s have passed and at least
{op_min} samples exist. The same two ciphertexts are reused for all calls. Reported values
are medians. The analysis also computes mean, standard deviation, quartiles, min and max per measurement.

**Inputs.** Plaintexts m1 = 17, m2 = 23, scalar k = 3. Goldwasser-Micali,
Sander-Young-Yung and Gentry-Sahai-Waters encrypt bit by bit, so they are measured on single
bits (m1 = m2 = 1), i.e. per-bit cost. Exponential and elliptic curve ElGamal, BGN and Freeman-BGN
decrypt by solving a small discrete log, so their decryption time depends on the plaintext
magnitude. Small plaintexts are the intended use of these schemes.

**Correctness.** Every homomorphic result is decrypted once outside the timed loop and
compared with the expected plaintext (sum, product, scaled value, XOR, AND or the original
value for re-encryption, modulo the plaintext modulus).

**Security levels.** NIST SP 800-57 equivalences: 80-bit = RSA/DL 1024 & ECC 160,
112-bit = 2048 & 224, 128-bit = 3072 & 256. Factoring and discrete-log schemes use these
modulus sizes. EC-ElGamal uses one standard curve per form and level: Weierstrass
secp160r1/P-224/P-256, Koblitz K-163/K-233/K-283, Edwards E-222/Ed25519 (no ~160-bit
Edwards curve is available). Freeman-BGN uses a field size of half the RSA-equivalent modulus,
since the pairing target group is F_{{p^2}}. Lattice schemes use the smallest power-of-two
dimension whose estimated security meets the level, using the HomomorphicEncryption.org
standard table (lambda ~ 3.4 n / log q for small secrets). With log q = 50 (Regev, LPR, GSW)
that is n = 2048 at every level (~139-bit estimate). With log q = 100 (BV) it is n = 4096.
ML-KEM-512 (NIST category 1) already meets all three levels. Lattice and ML-KEM cells therefore repeat
one run across the three levels. A sweep over other dimensions and the ML-KEM-768/1024 sets
is reported separately. Gentry-Halevi-Vaikuntanathan supports only toy dimensions (n <= 8) and is
learning-only. It is reported once, at n = 4, outside the security-level tables.

**Memory.** Peak resident set size of each process from `wait4` rusage, reported above a
per-language baseline process that only loads the runtime and the library. For Java and
Node.js this is a heap high-water mark governed by the garbage collector, not live data.
"""

THREATS = """\
- Single machine, a laptop CPU with turbo boost and thermal throttling. Use absolute numbers
  as indicative, and ratios between languages, which are measured interleaved, as the more robust result.
- Go 1.17 is an old toolchain. Newer Go releases have faster `math/big` assembly on some
  platforms.
- The JVM runs with default flags (tiered JIT, G1). Long warm-ups could favour Java more on
  sub-millisecond operations.
- Key generation time of prime-search based schemes has heavy-tailed variance, so medians over
  at most 10 samples carry wide uncertainty. The key generation section shows the min-max spread.
- Lattice security levels are estimates from a closed-form rule, not from the lattice-estimator.
  Validate them with the lattice-estimator before quoting bit-security in a paper.
- Okamoto-Uchiyama's modulus n = p^2 q gives slightly weaker factoring security than an RSA
  modulus of the same size.
"""


if __name__ == "__main__":
    main()
