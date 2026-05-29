#!/usr/bin/env python3
import argparse
import os
import platform
import random
import shutil
import sqlite3
import statistics
import string
import sys
import time
from datetime import datetime

try:
    import humanize
    HAS_HUMANIZE = True
except ImportError:
    HAS_HUMANIZE = False


SENSOR_NAMES = [f"sensor.{s}" for s in [
    "temperature", "humidity", "pressure", "co2", "voc", "lux",
    "motion", "power", "energy", "voltage", "current", "frequency",
]]

PHASES = 3
PHASE_TIMEOUT = 120  # seconds — abort a phase early if it exceeds this


def rand_sensor():
    return random.choice(SENSOR_NAMES)


def rand_value():
    return round(random.uniform(0.0, 100.0), 4)


def rand_ts():
    return datetime.now().isoformat()


def fmt_bytes(n):
    if HAS_HUMANIZE:
        return humanize.naturalsize(n, binary=True)
    return f"{n / 1_048_576:.1f} MiB"


def fmt_rows(n):
    if HAS_HUMANIZE:
        return humanize.intcomma(n)
    return f"{n:,}"


def db_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def mount_info(path):
    try:
        stat = shutil.disk_usage(path)
        return stat.total, stat.free
    except Exception:
        return 0, 0


def get_device(path):
    try:
        import subprocess
        result = subprocess.run(
            ["df", "-P", path], capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            return lines[1].split()[0]
    except Exception:
        pass
    return "unknown"


def get_fs(path):
    try:
        import subprocess
        result = subprocess.run(
            ["df", "-PT", path], capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 2:
                return parts[1]
    except Exception:
        pass
    return "unknown"


def apply_pragmas(conn, config):
    if config == "A":
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA cache_size=-2000")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-65536")


def create_table(conn):
    conn.execute("DROP TABLE IF EXISTS measurements")
    conn.execute("""
        CREATE TABLE measurements (
            id         INTEGER PRIMARY KEY,
            sensor     TEXT    NOT NULL,
            value      REAL    NOT NULL,
            recorded_at TEXT   NOT NULL
        )
    """)
    conn.commit()


def run_phase(fn, repeat=PHASES):
    times = []
    result = None
    deadline = time.perf_counter() + PHASE_TIMEOUT * repeat
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        t1 = time.perf_counter()
        times.append(t1 - t0)
        if t1 >= deadline:
            break
    return statistics.median(times), result


# ---------------------------------------------------------------------------
# Benchmark phases
# ---------------------------------------------------------------------------

def phase1_bulk_insert(conn, db_path, n=100_000):
    def run():
        create_table(conn)
        rows = [(rand_sensor(), rand_value(), rand_ts()) for _ in range(n)]
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO measurements(sensor, value, recorded_at) VALUES (?,?,?)", rows
        )
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        fd = os.open(db_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return n
    return run_phase(run)


def phase2_single_tx_insert(conn, db_path, n=10_000):
    def run():
        create_table(conn)
        deadline = time.perf_counter() + PHASE_TIMEOUT
        done = 0
        for _ in range(n):
            conn.execute(
                "INSERT INTO measurements(sensor, value, recorded_at) VALUES (?,?,?)",
                (rand_sensor(), rand_value(), rand_ts()),
            )
            conn.commit()
            done += 1
            if time.perf_counter() >= deadline:
                print(f"    [timeout after {done} rows]", flush=True)
                break
        fd = os.open(db_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return done
    return run_phase(run)


def phase3_full_scan(conn):
    def run():
        rows = conn.execute("SELECT * FROM measurements").fetchall()
        return len(rows)
    return run_phase(run)


def phase4_filtered_select(conn):
    def run():
        rows = conn.execute(
            "SELECT * FROM measurements WHERE value > 50.0"
        ).fetchall()
        return len(rows)
    return run_phase(run)


def phase5_random_select(conn, n=10_000):
    all_ids = [r[0] for r in conn.execute("SELECT id FROM measurements").fetchall()]
    if not all_ids:
        return 0.0, []
    sample = [random.choice(all_ids) for _ in range(n)]
    latencies = []
    for pk in sample:
        t0 = time.perf_counter()
        conn.execute("SELECT * FROM measurements WHERE id=?", (pk,)).fetchone()
        latencies.append(time.perf_counter() - t0)
    return statistics.median(latencies), latencies


def phase6_bulk_update(conn, db_path):
    suffix = "".join(random.choices(string.ascii_lowercase, k=4))
    def run():
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE measurements SET sensor=sensor||?, value=value*1.001, recorded_at=?",
            (suffix, rand_ts()),
        )
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        fd = os.open(db_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return conn.execute("SELECT changes()").fetchone()[0]
    return run_phase(run)


def phase7_random_update(conn, db_path, n=10_000):
    all_ids = [r[0] for r in conn.execute("SELECT id FROM measurements").fetchall()]
    if not all_ids:
        return 0.0, 0
    sample = random.sample(all_ids, min(n, len(all_ids)))
    def run():
        conn.execute("BEGIN")
        for pk in sample:
            conn.execute(
                "UPDATE measurements SET value=?, recorded_at=? WHERE id=?",
                (rand_value(), rand_ts(), pk),
            )
        conn.execute("COMMIT")
        fd = os.open(db_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return len(sample)
    return run_phase(run)


def phase8_delete(conn, db_path):
    total = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    half = total // 2
    def run():
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM measurements WHERE id IN "
            "(SELECT id FROM measurements ORDER BY RANDOM() LIMIT ?)", (half,)
        )
        conn.execute("COMMIT")
        fd = os.open(db_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return half
    return run_phase(run)


def phase9_reinsert(conn, db_path, n=50_000):
    def run():
        rows = [(rand_sensor(), rand_value(), rand_ts()) for _ in range(n)]
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO measurements(sensor, value, recorded_at) VALUES (?,?,?)", rows
        )
        conn.execute("COMMIT")
        fd = os.open(db_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return n
    return run_phase(run)


def phase12_fsync(conn, db_path, n=1_000):
    def run():
        deadline = time.perf_counter() + PHASE_TIMEOUT
        done = 0
        for _ in range(n):
            conn.execute(
                "INSERT INTO measurements(sensor, value, recorded_at) VALUES (?,?,?)",
                (rand_sensor(), rand_value(), rand_ts()),
            )
            conn.commit()
            fd = os.open(db_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            done += 1
            if time.perf_counter() >= deadline:
                print(f"    [timeout after {done} tx]", flush=True)
                break
        return done
    return run_phase(run)


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def throughput(count, seconds):
    if seconds <= 0:
        return 0
    return int(count / seconds)


def us(seconds):
    return seconds * 1_000_000


def section(f, title):
    f.write(f"\n{'-'*59}\n  {title}\n{'-'*59}\n\n")


def header(f, prefix, db_path):
    total, free = mount_info(os.path.dirname(db_path))
    device = get_device(os.path.dirname(db_path))
    fs = get_fs(os.path.dirname(db_path))
    f.write("=" * 59 + "\n")
    f.write("  SQLite Benchmark Results\n")
    f.write("=" * 59 + "\n")
    f.write(f"Date:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    f.write(f"Prefix:    {prefix}\n")
    f.write(f"Hostname:  {platform.node()}\n")
    f.write(f"Platform:  {platform.system()}-{platform.release()}-{platform.machine()}\n")
    f.write(f"Python:    {platform.python_version()}\n")
    f.write(f"SQLite:    {sqlite3.sqlite_version}\n")
    section(f, "Hardware / Mount Info")
    f.write(f"Mount point:  {os.path.dirname(db_path)}\n")
    f.write(f"Device:       {device}\n")
    f.write(f"Filesystem:   {fs}\n")
    f.write(f"Total space:  {fmt_bytes(total)}\n")
    f.write(f"Free space:   {fmt_bytes(free)}\n")


def write_config_results(f, config_label, results, db_path):
    section(f, f"Config {config_label[0]}: {config_label[1]}")

    p1_t, p1_n = results["p1"]
    f.write(f"Phase 1 - Sequential INSERT (100k rows, 1 transaction)\n")
    f.write(f"  Duration:   {p1_t*1000:,.0f} ms\n")
    f.write(f"  Throughput: {fmt_rows(throughput(p1_n, p1_t))} rows/sec\n")
    f.write(f"  DB size:    {fmt_bytes(db_size(db_path))}\n\n")

    p2_t, p2_n = results["p2"]
    f.write(f"Phase 2 - Sequential INSERT (10k rows, 1 tx/row)\n")
    f.write(f"  Duration:   {p2_t*1000:,.0f} ms\n")
    f.write(f"  Throughput: {fmt_rows(throughput(p2_n, p2_t))} rows/sec\n\n")

    p3_t, p3_n = results["p3"]
    f.write(f"Phase 3 - Full table scan SELECT\n")
    f.write(f"  Duration:   {p3_t*1000:,.0f} ms\n")
    f.write(f"  Throughput: {fmt_rows(throughput(p3_n, p3_t))} rows/sec\n\n")

    p4_t, p4_n = results["p4"]
    f.write(f"Phase 4 - Filtered SELECT (WHERE value > 50.0)\n")
    f.write(f"  Duration:   {p4_t*1000:,.0f} ms\n")
    f.write(f"  Rows returned: {fmt_rows(p4_n)}\n\n")

    latencies = results["p5_raw"]
    p5_med = results["p5_med"]
    if latencies:
        sorted_l = sorted(latencies)
        p50 = sorted_l[int(len(sorted_l) * 0.50)]
        p95 = sorted_l[int(len(sorted_l) * 0.95)]
        p99 = sorted_l[int(len(sorted_l) * 0.99)]
        f.write(f"Phase 5 - Random SELECT by PK (10k queries)\n")
        f.write(f"  Avg latency: {us(p5_med):.1f} µs/query\n")
        f.write(f"  p50: {us(p50):.1f} µs  |  p95: {us(p95):.1f} µs  |  p99: {us(p99):.1f} µs\n\n")

    p6_t, p6_n = results["p6"]
    f.write(f"Phase 6 - Sequential UPDATE (all rows, 1 transaction)\n")
    f.write(f"  Duration:   {p6_t*1000:,.0f} ms\n")
    f.write(f"  Throughput: {fmt_rows(throughput(p6_n, p6_t))} rows/sec\n\n")

    p7_t, p7_n = results["p7"]
    f.write(f"Phase 7 - Random UPDATE (10k rows, 1 tx/row)\n")
    f.write(f"  Duration:   {p7_t*1000:,.0f} ms\n")
    f.write(f"  Throughput: {fmt_rows(throughput(p7_n, p7_t))} rows/sec\n\n")

    p8_t, p8_n = results["p8"]
    f.write(f"Phase 8 - DELETE (50% of rows)\n")
    f.write(f"  Duration:   {p8_t*1000:,.0f} ms\n")
    f.write(f"  Throughput: {fmt_rows(throughput(p8_n, p8_t))} rows/sec\n\n")

    p9_t, p9_n = results["p9"]
    f.write(f"Phase 9 - Re-INSERT (50k rows)\n")
    f.write(f"  Duration:   {p9_t*1000:,.0f} ms\n")
    f.write(f"  Throughput: {fmt_rows(throughput(p9_n, p9_t))} rows/sec\n\n")

    p12_t, p12_n = results["p12"]
    f.write(f"Phase 12 - fsync test (1k transactions)\n")
    f.write(f"  Duration:   {p12_t*1000:,.0f} ms\n")
    f.write(f"  Throughput: {fmt_rows(throughput(p12_n, p12_t))} tx/sec\n")


def write_summary(f, cfg_a, cfg_b):
    section(f, "Summary")

    def tput(r, key):
        t, n = r[key]
        return throughput(n, t)

    la = cfg_a["p5_raw"]
    lb = cfg_b["p5_raw"]
    med_a = us(cfg_a["p5_med"]) if la else 0
    med_b = us(cfg_b["p5_med"]) if lb else 0

    rows = [
        ("INSERT bulk (rows/s)",       tput(cfg_a, "p1"),  tput(cfg_b, "p1")),
        ("INSERT 1tx/row (rows/s)",    tput(cfg_a, "p2"),  tput(cfg_b, "p2")),
        ("Full scan (rows/s)",         tput(cfg_a, "p3"),  tput(cfg_b, "p3")),
        ("UPDATE bulk (rows/s)",       tput(cfg_a, "p6"),  tput(cfg_b, "p6")),
        ("UPDATE rand (rows/s)",       tput(cfg_a, "p7"),  tput(cfg_b, "p7")),
        ("DELETE (rows/s)",            tput(cfg_a, "p8"),  tput(cfg_b, "p8")),
        ("fsync (tx/s)",               tput(cfg_a, "p12"), tput(cfg_b, "p12")),
    ]

    col = 30
    f.write(f"{'Metric':<{col}} {'Config A':>12} {'Config B':>12}\n")
    f.write("-" * (col + 26) + "\n")
    for label, va, vb in rows:
        f.write(f"{label:<{col}} {fmt_rows(va):>12} {fmt_rows(vb):>12}\n")
    f.write(f"{'Random SELECT (µs/query)':<{col}} {med_a:>11.1f}  {med_b:>11.1f}\n")

    f.write("\n" + "=" * 59 + "\n")
    f.write("  END OF RESULTS\n")
    f.write("=" * 59 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_config(conn, db_path, config):
    apply_pragmas(conn, config)
    create_table(conn)

    print(f"  Phase 1  bulk INSERT 100k ...", flush=True)
    p1 = phase1_bulk_insert(conn, db_path)

    print(f"  Phase 2  single-tx INSERT 10k ...", flush=True)
    p2 = phase2_single_tx_insert(conn, db_path)

    # ensure 100k rows for scan phases
    cur_count = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    if cur_count < 100_000:
        need = 100_000 - cur_count
        rows = [(rand_sensor(), rand_value(), rand_ts()) for _ in range(need)]
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO measurements(sensor, value, recorded_at) VALUES (?,?,?)", rows
        )
        conn.execute("COMMIT")

    print(f"  Phase 3  full scan SELECT ...", flush=True)
    p3 = phase3_full_scan(conn)

    print(f"  Phase 4  filtered SELECT ...", flush=True)
    p4 = phase4_filtered_select(conn)

    print(f"  Phase 5  random SELECT 10k ...", flush=True)
    p5_med, p5_raw = phase5_random_select(conn)

    print(f"  Phase 6  bulk UPDATE ...", flush=True)
    p6 = phase6_bulk_update(conn, db_path)

    print(f"  Phase 7  random UPDATE 10k ...", flush=True)
    p7 = phase7_random_update(conn, db_path)

    print(f"  Phase 8  DELETE 50% ...", flush=True)
    p8 = phase8_delete(conn, db_path)

    print(f"  Phase 9  re-INSERT 50k ...", flush=True)
    p9 = phase9_reinsert(conn, db_path)

    print(f"  Phase 12 fsync 1k tx ...", flush=True)
    p12 = phase12_fsync(conn, db_path)

    return {
        "p1": p1, "p2": p2, "p3": p3, "p4": p4,
        "p5_med": p5_med, "p5_raw": p5_raw,
        "p6": p6, "p7": p7, "p8": p8, "p9": p9, "p12": p12,
    }


def main():
    parser = argparse.ArgumentParser(description="SQLite I/O benchmark")
    parser.add_argument("--db",     required=True, help="Path to SQLite database file")
    parser.add_argument("--prefix", required=True, help="Test run prefix")
    parser.add_argument("--output", required=True, help="Path to result text file")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.db), exist_ok=True)

    configs = [
        ("A", "journal=DELETE  sync=FULL   cache=2MB"),
        ("B", "journal=WAL     sync=NORMAL cache=64MB"),
    ]

    all_results = {}
    for cfg_id, cfg_label in configs:
        print(f"\n[Config {cfg_id}: {cfg_label}]", flush=True)
        if os.path.exists(args.db):
            os.remove(args.db)
        conn = sqlite3.connect(args.db)
        try:
            all_results[cfg_id] = run_config(conn, args.db, cfg_id)
        finally:
            conn.close()

    with open(args.output, "w", encoding="utf-8") as f:
        header(f, args.prefix, args.db)
        for cfg_id, cfg_label in configs:
            write_config_results(f, (cfg_id, cfg_label), all_results[cfg_id], args.db)
        write_summary(f, all_results["A"], all_results["B"])

    print(f"\nResults written to: {args.output}", flush=True)


if __name__ == "__main__":
    main()
