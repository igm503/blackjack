#!/usr/bin/env python3
"""Benchmark runner for the exact blackjack engine.

Times the fixed benchmark suite (see CASES) with one of the solver iterations
and appends the result to bench/history.json, which bench/server.py charts.

  python3 bench/run.py record --label "what changed" [--note "..."]
      time the current build (all threads) and append it as a new iteration
  python3 bench/run.py backfill
      time the earlier solver iterations that are still selectable in the
      code (see VARIANTS) so the history starts from the beginning
  python3 bench/run.py show
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
HISTORY = BENCH / "history.json"
BJCA = ROOT / "bjca"

CASES = [
    {
        "id": "hand-88vT-6d",
        "title": "One decision: split 8,8 vs 10",
        "detail": "6 decks, resplit to 4 hands. Exact EV of splitting with perfect play of every hand.",
    },
    {
        "id": "hand-22v6-8d",
        "title": "Hardest single decision: split 2,2 vs 6",
        "detail": "8 decks, resplit to 4 hands. Low pairs make the longest hands, so this is the worst case.",
    },
    {
        "id": "round-6d-spl1",
        "title": "Whole round, one split",
        "detail": "6 decks. Every starting hand against every upcard, one split allowed. Exact EV of the round.",
    },
    {
        "id": "round-6d-spl3",
        "title": "Whole round, full resplits",
        "detail": "6 decks. Every starting hand against every upcard, resplit to 4 hands. The full problem.",
    },
]

# Earlier iterations of the solver that can still be selected at run time.
VARIANTS = [
    {
        "variant": "carry-all",
        "label": "Memoised game tree",
        "note": "First exact solver: recursion over every line of play, memoised on the cards drawn, "
                "with the bets on every finished split hand carried in the state. One thread.",
    },
    {
        "variant": "settle-early",
        "label": "Settle finished hands early",
        "note": "A finished hand's expected payoff cannot be changed by later play (the dealer's outcome "
                "distribution is a martingale in the cards seen), so finished hands are settled at once "
                "and dropped from the state.",
    },
    {
        "variant": "layered",
        "label": "Layered streaming solver",
        "note": "States grouped by cards drawn and solved layer by layer over sorted arrays: no hash "
                "table, children found by merge cursors, one dealer evaluation per card set. One thread.",
    },
    {
        "variant": "bfs-dealer",
        "label": "Breadth-first dealer evaluation",
        "note": "Dealer probabilities were 90% of the time and bound by one long dependency chain. "
                "Laying the dealer trie out level by level lets the CPU overlap the multiplies. One thread.",
    },
    {
        "variant": "threads",
        "label": "Multi-threaded layers",
        "note": "Each layer is split across all cores, forward and backward; value buffers are recycled "
                "between layers.",
    },
    {
        "variant": "shared-plan",
        "label": "One forward pass per pair",
        "note": "Which states are reachable does not depend on the dealer's upcard, so the forward pass is "
                "done once per pair and its ten upcards are evaluated side by side. (Whole-round cases only.)",
    },
    {
        "variant": "simd",
        "label": "SIMD dealer evaluation",
        "note": "Four card compositions are pushed through the dealer trie per pass (one vector multiply "
                "per trie node), and the value loops read children through small dense tables.",
    },
    {
        "variant": "chunks",
        "label": "Dynamic scheduling",
        "note": "Work is handed to threads in small chunks instead of one fixed share each, and the forward "
                "pass precomputes what each node sends to its children once instead of once per card. "
                "No measurable gain on the whole-round cases.",
    },
    {
        "variant": "current",
        "label": "All upcards in one pass",
        "note": "For the big whole-round problems, a pair's upcards are evaluated together: the node is "
                "decoded and its children located once, the values of the upcards sit side by side, and "
                "dealer evaluations run eight card compositions per pass. Profiling shows the dealer "
                "evaluation is now most of the remaining time.",
    },
    {
        "variant": "stand17",
        "label": "Skip hits on hard 17+ (certified)",
        "note": "Two thirds of the states only exist to confirm that hitting a hard 17-20 in a split hand is "
                "bad. They are no longer enumerated; instead every skipped move is checked against a sound "
                "upper bound. The bound is complete on the last hand to be played. While other hands are "
                "still waiting it holds as long as seeing one extra card is worth less than the certified "
                "margin (a few percent of a bet) to them; the largest value measured in full enumerations "
                "is under 0.1%. Positions whose margin is below 2% are solved by full enumeration.",
        "assumption": "Relies on: one extra seen card is worth less than the certified margin to the hands "
                      "still to be played. Off by default (--assume-stand17).",
    },
]

ORIGINAL = {
    "id": "original",
    "label": "Original code (main branch)",
    "note": "paths.cpp / all_paths.cpp build one node for every possible shoe composition: 33 million nodes "
            "and 8.8 GB for a single deck, before any EV is computed. Six decks would need about 4e14 nodes, "
            "so none of these cases can run.",
    "variant": None,
    "results": {c["id"]: {"status": "infeasible"} for c in CASES},
}


def machine() -> str:
    try:
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    except OSError:
        cpu = ""
    return f"{cpu or platform.processor() or platform.machine()}, {os.cpu_count()} threads"


def commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT)
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], capture_output=True, text=True, cwd=ROOT)
        return out.stdout.strip() + ("+" if dirty.stdout.strip() else "")
    except OSError:
        return ""


class History:
    """history.json with a file lock so several runners can update it."""

    def __enter__(self):
        self.lock = open(str(HISTORY) + ".lock", "w")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        if HISTORY.exists():
            self.data = json.loads(HISTORY.read_text())
        else:
            self.data = {"machine": machine(), "cases": CASES, "iterations": []}
        self.data["cases"] = CASES
        return self.data

    def __exit__(self, *exc):
        if exc[0] is None:
            tmp = HISTORY.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1) + "\n")
            tmp.replace(HISTORY)
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()


def frozen_binary() -> Path:
    """A private copy of the binary, so rebuilding mid-benchmark changes nothing."""
    import shutil
    import tempfile
    path = Path(tempfile.mkdtemp(prefix="bjbench-")) / "bjca"
    shutil.copy2(BJCA, path)
    return path


def run_case(binary: Path, case_id: str, variant: str, timeout: float) -> dict:
    """Best of up to three runs (one if it is slow)."""
    best = None
    for attempt in range(3):
        t0 = time.time()
        try:
            proc = subprocess.run([str(binary), "bench-case", "--case", case_id, "--variant", variant],
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "limit_seconds": timeout}
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"status": "error", "message": (proc.stderr or proc.stdout)[-300:]}
        if "error" in out:
            status = "state-limit" if "state limit" in out["error"] else "error"
            return {"status": status, "message": out["error"], "gave_up_after_seconds": out.get("seconds")}
        if best is None or out["seconds"] < best["seconds"]:
            best = {"status": "ok", "seconds": out["seconds"], "ev": out["ev"], "nodes": out["nodes"],
                    "threads": out["threads"]}
            for k in ("cert_info", "cert_fallbacks"):
                if k in out:
                    best[k] = out[k]
        best["runs"] = attempt + 1
        if time.time() - t0 > 8:
            break
    return best


def record(variant: str, label: str, note: str, iteration_id: str | None, timeout: float, cases=None,
           log=print, binary: Path | None = None) -> str:
    binary = binary or frozen_binary()
    iteration_id = iteration_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{variant}"
    with History() as h:
        entry = next((it for it in h["iterations"] if it["id"] == iteration_id), None)
        if entry is None:
            entry = {"id": iteration_id, "results": {}}
            h["iterations"].append(entry)
        entry.update(label=label, note=note, variant=variant, commit=commit(),
                     recorded_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        assumption = next((v.get("assumption") for v in VARIANTS if v["variant"] == variant and v["label"] == label), None)
        if assumption:
            entry["assumption"] = assumption
        else:
            entry.pop("assumption", None)
        for c in CASES:
            if cases is None or c["id"] in cases:
                entry["results"][c["id"]] = {"status": "running"}
    for c in CASES:
        if cases is not None and c["id"] not in cases:
            continue
        log(f"[{label}] {c['id']} ...", flush=True)
        res = run_case(binary, c["id"], variant, timeout)
        with History() as h:
            entry = next(it for it in h["iterations"] if it["id"] == iteration_id)
            entry["results"][c["id"]] = res
        shown = f"{res['seconds']:.3f}s" if res["status"] == "ok" else res["status"]
        log(f"[{label}] {c['id']}: {shown}", flush=True)
    return iteration_id


def build():
    subprocess.run(["make", "-s", "bjca"], cwd=ROOT, check=True)


def backfill(timeout: float):
    build()
    with History() as h:
        have = {it["id"] for it in h["iterations"]}
        if "original" not in have:
            h["iterations"].insert(0, dict(ORIGINAL, commit="198c838", recorded_at=time.strftime("%Y-%m-%d %H:%M:%S")))
        # fix the order up front so the chart reads left to right
        for i, v in enumerate(VARIANTS):
            vid = f"v{i + 1}-{v['variant']}"
            if vid not in have:
                h["iterations"].append({"id": vid, "label": v["label"], "note": v["note"], "variant": v["variant"],
                                        "results": {c["id"]: {"status": "queued"} for c in CASES}})
    ids = {v["variant"]: f"v{i + 1}-{v['variant']}" for i, v in enumerate(VARIANTS)}
    by = {v["variant"]: v for v in VARIANTS}

    binary = frozen_binary()

    def go(name):
        v = by[name]
        record(name, v["label"], v["note"], ids[name], timeout, binary=binary)

    # The multi-threaded build needs the machine to itself; the single-threaded
    # iterations can then share it (two memory-hungry hash solvers in series).
    for name in ("threads", "shared-plan", "simd", "chunks", "current", "stand17"):
        go(name)
    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(go, "bfs-dealer"), pool.submit(go, "layered"),
                   pool.submit(lambda: (go("settle-early"), go("carry-all")))]
        for f in futures:
            f.result()


def show():
    with History() as h:
        print(h["machine"])
        for it in h["iterations"]:
            cells = []
            for c in CASES:
                r = it["results"].get(c["id"], {})
                cells.append(f"{r['seconds']:9.3f}s" if r.get("status") == "ok" else f"{r.get('status', '-'):>10}")
            print(f"{it['label'][:34]:34} " + " ".join(cells))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--label", required=True)
    r.add_argument("--note", default="")
    r.add_argument("--variant", default="current")
    r.add_argument("--id")
    r.add_argument("--timeout", type=float, default=3600)
    r.add_argument("--cases", nargs="*")
    b = sub.add_parser("backfill")
    b.add_argument("--timeout", type=float, default=5400)
    sub.add_parser("show")
    a = ap.parse_args()
    if a.cmd == "record":
        build()
        record(a.variant, a.label, a.note, a.id, a.timeout, a.cases)
    elif a.cmd == "backfill":
        backfill(a.timeout)
    else:
        show()


if __name__ == "__main__":
    sys.exit(main())
