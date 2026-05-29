#!/usr/bin/env python3
"""
Compare speedtest result files and produce a Markdown comparison table.

Usage:
    python compare.py <directory>
    python compare.py <file1> <file2> ...
"""
import os
import re
import sys
from datetime import datetime
from pathlib import Path


# Metrics to extract and their regex patterns (Config A and B sections)
METRIC_PATTERNS = {
    "INSERT bulk (rows/s)":     r"Phase 1.*?\n.*?Throughput:\s*([\d,]+) rows/sec",
    "INSERT 1tx/row (rows/s)":  r"Phase 2.*?\n.*?Throughput:\s*([\d,]+) rows/sec",
    "Full scan (rows/s)":       r"Phase 3.*?\n.*?Throughput:\s*([\d,]+) rows/sec",
    "UPDATE bulk (rows/s)":     r"Phase 6.*?\n.*?Throughput:\s*([\d,]+) rows/sec",
    "UPDATE rand (rows/s)":     r"Phase 7.*?\n.*?Throughput:\s*([\d,]+) rows/sec",
    "DELETE (rows/s)":          r"Phase 8.*?\n.*?Throughput:\s*([\d,]+) rows/sec",
    "fsync (tx/s)":             r"Phase 12.*?\n.*?Throughput:\s*([\d,]+) tx/sec",
    "Random SELECT (µs/query)": r"Avg latency:\s*([\d.]+) µs/query",
}

PREFIX_RE = re.compile(r"^Prefix:\s+(.+)$", re.MULTILINE)
DATE_RE   = re.compile(r"^Date:\s+(.+)$",   re.MULTILINE)

CONFIG_SPLIT_RE = re.compile(
    r"-{3,}\s*\n\s*Config ([AB]):[^\n]*\n-{3,}(.*?)(?=-{3,}\s*\n\s*Config [AB]:|={3,})",
    re.DOTALL,
)


def parse_int(s):
    return int(s.replace(",", ""))


def extract_metric(text, pattern):
    m = re.search(pattern, text, re.DOTALL)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def parse_file(path):
    text = Path(path).read_text(encoding="utf-8")

    prefix_m = PREFIX_RE.search(text)
    date_m   = DATE_RE.search(text)
    prefix   = prefix_m.group(1).strip() if prefix_m else Path(path).stem
    date_str = date_m.group(1).strip()   if date_m   else ""

    configs = {}
    for m in CONFIG_SPLIT_RE.finditer(text):
        cfg_id   = m.group(1)
        cfg_text = m.group(2)
        metrics  = {}
        for label, pattern in METRIC_PATTERNS.items():
            metrics[label] = extract_metric(cfg_text, pattern)
        configs[cfg_id] = metrics

    return {
        "prefix":  prefix,
        "date":    date_str,
        "file":    str(path),
        "configs": configs,
    }


def collect_files(args):
    paths = []
    for arg in args:
        p = Path(arg)
        if p.is_dir():
            paths.extend(sorted(p.glob("*-speedresult.txt")))
        elif p.is_file():
            paths.append(p)
        else:
            print(f"Warning: {arg} not found, skipping", file=sys.stderr)
    return paths


def fmt_val(v, label):
    if v is None:
        return "—"
    if "µs" in label:
        return f"{v:.1f}"
    return f"{int(v):,}"


def build_table(runs, config_id):
    cols = [r["prefix"] for r in runs]
    header  = "| Metric" + "".join(f" | {c}" for c in cols) + " |"
    divider = "|---" + "|---:" * len(cols) + "|"
    lines   = [header, divider]

    for label in METRIC_PATTERNS:
        row = f"| {label}"
        for run in runs:
            v = run["configs"].get(config_id, {}).get(label)
            row += f" | {fmt_val(v, label)}"
        row += " |"
        lines.append(row)

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    files = collect_files(sys.argv[1:])
    if not files:
        print("No result files found.", file=sys.stderr)
        sys.exit(1)

    runs = []
    for f in files:
        try:
            runs.append(parse_file(f))
            print(f"Parsed: {f}")
        except Exception as e:
            print(f"Warning: failed to parse {f}: {e}", file=sys.stderr)

    if not runs:
        print("No valid result files could be parsed.", file=sys.stderr)
        sys.exit(1)

    out_date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_path = Path(sys.argv[1] if Path(sys.argv[1]).is_dir() else ".") / f"comparison-{out_date}.md"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# SQLite Benchmark Comparison\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("## Source Files\n\n")
        for r in runs:
            f.write(f"- **{r['prefix']}** — `{r['file']}` (run: {r['date']})\n")

        for cfg_id, cfg_desc in [
            ("A", "Config A: journal=DELETE, sync=FULL, cache=2MB"),
            ("B", "Config B: journal=WAL, sync=NORMAL, cache=64MB (HomeAssistant-like)"),
        ]:
            f.write(f"\n## {cfg_desc}\n\n")
            f.write(build_table(runs, cfg_id))
            f.write("\n")

    print(f"\nComparison written to: {out_path}")


if __name__ == "__main__":
    main()
