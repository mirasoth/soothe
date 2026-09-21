#!/usr/bin/env python3
"""Benchmark Soothe's intent + goal-completion decisions against a TypeSafe endpoint.

Runs Soothe's real question sets — the 4-way intake label (+ language) and the
coverage-audit decision — over a labelled case set, then scores the raw
`/v1/systemone` wire responses. Standalone (stdlib only): no Soothe import, no
`langchain-typesafe` version to pin, so it targets any TypeSafe-compatible
server (local Laya :8770, the NanoJev gateway :8767, or hosted Jev).

The question texts are copied verbatim from Soothe so the benchmark measures
production behaviour:

  packages/soothe/src/soothe/sloop/intention/typesafe_intent.py
  packages/soothe/src/soothe/sloop/eval/typesafe_eval_decision.py

Usage:
  python benchmark_typesafe_decisions.py --url http://127.0.0.1:8770
  python benchmark_typesafe_decisions.py --bench intent --output report.json
"""

import argparse
import json
import os
import statistics
import time
from collections import Counter
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

DEFAULT_CASES = Path(__file__).with_name("cases.json")

INTENT_QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "Classify this user request by how much work it needs from the assistant.",
        "criteria": {
            "chitchat": "Social exchange, greeting, or small talk — no tool work or task execution is expected.",
            "minimal": "A direct single-step action: one tool call or lookup answers it, no planning or decomposition.",
            "simple": "A single deliverable needing a few steps — light planning, one concrete artifact.",
            "complex": "Multi-phase work: needs planning, decomposition, exploration across files, or a full agent loop.",
        },
    },
    "language": {
        "type": "choice",
        "instructions": "Which language is the user writing in?",
        "criteria": {"en": "English", "zh": "Chinese", "ja": "Japanese", "ko": "Korean"},
    },
}

COVERAGE_QUESTIONS = {
    "coverage": {
        "type": "choice",
        "instructions": "Decide whether a coverage audit of the goal is warranted, given the "
        "steps already executed and their reported outcomes.",
        "criteria": {
            "no_eval": "The executed steps fully cover the goal: the close reports and outcomes "
            "show the deliverable is produced and nothing material is left unaddressed.",
            "eval": "Coverage is uncertain or incomplete: something in the goal is unverified, "
            "partially done, failed, or the evidence is missing.",
        },
    },
}


def _ssl_context():
    """Prefer certifi's CA bundle; the stdlib `urllib` on macOS/Homebrew Python
    often cannot verify against the system store."""
    try:
        import ssl

        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def post(url, payload, api_key, timeout=120.0):
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urlrequest.Request(
        url.rstrip("/") + "/v1/systemone",
        data=body,
        headers=headers,
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=timeout, context=_ssl_context()) as response:
        return json.loads(response.read())


def classify_one(url, state, questions, model, api_key, timeout):
    payload = {"state": state, "model": model, "questions": questions}
    t0 = time.perf_counter()
    try:
        body = post(url, payload, api_key, timeout=timeout)
        elapsed = time.perf_counter() - t0
        answers = {qid: a for qid, a in body.get("answers", {}).items()}
        return {
            "ok": True,
            "answers": answers,
            "elapsed": elapsed,
            "usage": body.get("usage"),
            "model": body.get("model"),
        }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed": time.perf_counter() - t0,
        }


def run_intent(url, cases, model, api_key, timeout):
    rows = []
    for case in cases:
        r = classify_one(url, {"query": case["query"]}, INTENT_QUESTIONS, model, api_key, timeout)
        row = {
            "id": case["id"],
            "gold_label": case["gold_label"],
            "gold_language": case.get("gold_language"),
        }
        if not r["ok"]:
            row.update({"error": r["error"]})
        else:
            a = r["answers"].get("intent", {})
            lang = r["answers"].get("language", {})
            row.update(
                {
                    "pred_label": a.get("choice"),
                    "conf": a.get("confidence"),
                    "probs": a.get("probabilities"),
                    "pred_language": lang.get("choice"),
                    "lang_conf": lang.get("confidence"),
                    "elapsed": r["elapsed"],
                }
            )
        rows.append(row)
    return rows


def run_coverage(url, cases, model, api_key, timeout):
    rows = []
    for case in cases:
        state = {
            "goal": case["goal"],
            "step_history": case["step_history"],
            "task_complexity": case.get("task_complexity", "simple"),
        }
        r = classify_one(url, state, COVERAGE_QUESTIONS, model, api_key, timeout)
        row = {"id": case["id"], "gold": case["gold"]}
        if not r["ok"]:
            row.update({"error": r["error"]})
        else:
            a = r["answers"].get("coverage", {})
            row.update(
                {
                    "pred": a.get("choice"),
                    "conf": a.get("confidence"),
                    "probs": a.get("probabilities"),
                    "elapsed": r["elapsed"],
                }
            )
        rows.append(row)
    return rows


def accuracy_confusion(rows, gold_key, pred_key):
    """Per-class accuracy + confusion counts for a categorical task."""
    classes = sorted({r[gold_key] for r in rows})
    correct = Counter()
    total = Counter()
    confusion = Counter()
    for r in rows:
        gold, pred = r[gold_key], r.get(pred_key)
        total[gold] += 1
        if pred == gold:
            correct[gold] += 1
        confusion[(gold, pred)] += 1
    return classes, correct, total, confusion


def confidence_stats(rows, min_confidence, suppress_confidence):
    """Mean/median confidence and the fraction that clears each bar."""
    confs = [r["conf"] for r in rows if r.get("conf") is not None]
    if not confs:
        return None
    return {
        "n": len(confs),
        "mean": statistics.fmean(confs),
        "median": statistics.median(confs),
        "gte_min": sum(1 for c in confs if c >= min_confidence) / len(confs),
        "gte_suppress": sum(1 for c in confs if c >= suppress_confidence) / len(confs),
    }


def calibration(rows, gold_key, pred_key, conf_key="conf"):
    """Reliability buckets: accuracy among answers in each confidence band."""
    buckets = [0.0, 0.2, 0.4, 0.6, 0.8, 0.9, 1.01]
    out = []
    for lo, hi in zip(buckets, buckets[1:]):
        band = [r for r in rows if r.get(conf_key) is not None and lo <= r[conf_key] < hi]
        if not band:
            continue
        acc = sum(1 for r in band if r.get(pred_key) == r[gold_key]) / len(band)
        out.append((f"{lo:.1f}-{hi:.1f}", len(band), acc))
    return out


def latency(rows):
    vals = [r["elapsed"] for r in rows if r.get("elapsed") is not None]
    if not vals:
        return None
    vals_sorted = sorted(vals)
    p95 = vals_sorted[int(len(vals_sorted) * 0.95)] if len(vals_sorted) > 1 else vals_sorted[0]
    return {
        "n": len(vals),
        "mean_ms": statistics.fmean(vals) * 1000,
        "median_ms": statistics.median(vals) * 1000,
        "p95_ms": p95 * 1000,
        "first_ms": vals[0] * 1000,
    }


def print_task(name, rows, gold_key, pred_key, baseline, min_confidence, suppress_confidence):
    errors = [r for r in rows if "error" in r]
    ok_rows = [r for r in rows if "error" not in r]
    print(f"\n== {name} ==  ({len(ok_rows)}/{len(rows)} answered, {len(errors)} errors)")
    if not ok_rows:
        print("  no successful answers")
        for r in errors[:5]:
            print(f"    {r['id']}: {r['error']}")
        return

    classes, correct, total, confusion = accuracy_confusion(ok_rows, gold_key, pred_key)
    hits = sum(correct.values())
    n = sum(total.values())
    print(f"  accuracy  {hits}/{n} = {hits / n:.3f}   (chance {baseline:.3f})")

    print("  per-class")
    for c in classes:
        print(f"    {c:10s} {correct[c]:2d}/{total[c]:2d} = {correct[c] / total[c]:.3f}")

    print("  confusion (gold -> pred)")
    for c in classes:
        row = " ".join(f"{confusion[(c, p)]:3d}" for p in classes)
        print(f"    {c:10s} [{row} ]   <- {classes}")

    cs = confidence_stats(ok_rows, min_confidence, suppress_confidence)
    if cs:
        print(
            f"  confidence mean={cs['mean']:.3f} median={cs['median']:.3f}  "
            f">={min_confidence}: {cs['gte_min']:.0%}  >=0.9: {cs['gte_suppress']:.0%}"
        )
        print(
            f"    -> {cs['gte_min']:.0%} would drive a decision at min_confidence="
            f"{min_confidence}; {1 - cs['gte_min']:.0%} fall back to LLM"
        )

    print("  calibration (confidence band -> accuracy, n)")
    for band, count, acc in calibration(ok_rows, gold_key, pred_key):
        print(f"    {band:7s} {acc:.3f}  (n={count})")

    lat = latency(ok_rows)
    if lat:
        print(
            f"  latency mean={lat['mean_ms']:.1f}ms median={lat['median_ms']:.1f}ms "
            f"p95={lat['p95_ms']:.1f}ms first={lat['first_ms']:.1f}ms"
        )


def dump_json(intent_rows, coverage_rows, path):
    payload = {"intent_rows": intent_rows, "coverage_rows": coverage_rows}
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def language_accuracy(rows):
    """Accuracy of the parallel `language` answer among intent cases."""
    pairs = [
        (r.get("gold_language"), r.get("pred_language"))
        for r in rows
        if r.get("gold_language") and r.get("pred_language")
    ]
    if not pairs:
        return None
    return sum(1 for g, p in pairs if g == p), len(pairs)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="http://127.0.0.1:8770")
    ap.add_argument("--model", default="jev-latest")
    ap.add_argument(
        "--api-key",
        default=os.environ.get("TYPESAFE_API_KEY"),
        help="defaults to $TYPESAFE_API_KEY",
    )
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--bench", choices=["intent", "coverage", "all"], default="all")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument(
        "--min-confidence",
        type=float,
        default=0.75,
        help="decision threshold for the trust gate (default 0.75)",
    )
    ap.add_argument(
        "--suppress-confidence",
        type=float,
        default=0.9,
        help="suppression bar, shown for reference (default 0.9)",
    )
    ap.add_argument("--output", help="write per-case rows + aggregate to this JSON path")
    args = ap.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    intent_rows = coverage_rows = None

    if args.bench in ("intent", "all"):
        intent_rows = run_intent(args.url, cases["intent"], args.model, args.api_key, args.timeout)
        print_task(
            "Intent classification (4-class intake_label + language)",
            intent_rows,
            "gold_label",
            "pred_label",
            0.25,
            args.min_confidence,
            args.suppress_confidence,
        )
        lang = language_accuracy(intent_rows)
        if lang is not None:
            print(f"  language accuracy  {lang[0]}/{lang[1]} = {lang[0] / lang[1]:.3f}")

    if args.bench in ("coverage", "all"):
        coverage_rows = run_coverage(
            args.url, cases["coverage"], args.model, args.api_key, args.timeout
        )
        print_task(
            "Goal-completion confirmation (coverage audit yes/no)",
            coverage_rows,
            "gold",
            "pred",
            0.5,
            args.min_confidence,
            args.suppress_confidence,
        )

    if args.output:
        dump_json(intent_rows, coverage_rows, args.output)
        print(f"\nper-case rows written to {args.output}")


if __name__ == "__main__":
    main()
