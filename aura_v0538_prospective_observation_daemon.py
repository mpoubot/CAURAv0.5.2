from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


VERSION = "AURA v0.5.3.8"
BASE_DIR = Path(__file__).resolve().parent

ENGINE = BASE_DIR / "aura_v0537_prospective_observation_daemon.py"

OBS_FILE = (
    BASE_DIR
    / "regime_output"
    / "prospective_holdout"
    / "prospective_candidate_observations.csv"
)

OUT_DIR = (
    BASE_DIR
    / "regime_output"
    / "prospective_daemon"
)

HEARTBEAT_JSON = OUT_DIR / "aura_heartbeat.json"
HEARTBEAT_LOG = OUT_DIR / "aura_heartbeat.log"

POLL_SECONDS = 300


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    if dt is None:
        return "N/A"
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_dirs():
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def parse_engine_output(text):
    result = {
        "latest_closed_hour": None,
        "new_closed_bars": None,
        "completed_observations": None,
        "btc_bars": None,
        "btc_first": None,
        "btc_last": None,
        "eth_bars": None,
        "eth_first": None,
        "eth_last": None,
        "engine_error": None,
    }

    m = re.search(
        r"LATEST CLOSED HOUR\s*:\s*(.+)",
        text,
        re.IGNORECASE,
    )
    if m:
        result["latest_closed_hour"] = m.group(1).strip()

    m = re.search(
        r"NEW CLOSED BARS\s*:\s*(\d+)",
        text,
        re.IGNORECASE,
    )
    if m:
        result["new_closed_bars"] = int(m.group(1))

    m = re.search(
        r"COMPLETED OBSERVATIONS\s*:\s*(\d+)",
        text,
        re.IGNORECASE,
    )
    if m:
        result["completed_observations"] = int(m.group(1))

    for symbol, key in [("BTC/USD", "btc"), ("ETH/USD", "eth")]:
        pattern = (
            re.escape(symbol)
            + r"\s*:\s*(\d+)\s+bars\s*\|\s*(\S+)\s*->\s*(\S+)"
        )

        m = re.search(pattern, text, re.IGNORECASE)

        if m:
            result[f"{key}_bars"] = int(m.group(1))
            result[f"{key}_first"] = m.group(2)
            result[f"{key}_last"] = m.group(3)

    if "DAEMON ERROR:" in text:
        m = re.search(
            r"DAEMON ERROR:\s*(.+)",
            text,
            re.IGNORECASE,
        )
        if m:
            result["engine_error"] = m.group(1).strip()

    return result


def read_observation_ledger():
    data = {
        "ledger_exists": OBS_FILE.exists(),
        "ledger_rows": 0,
        "valid_forward_observations": 0,
        "positive_forward_observations": 0,
        "mean_forward_return_pct": None,
        "median_forward_return_pct": None,
        "hit_rate_pct": None,
        "last_observation_timestamp": None,
    }

    if not OBS_FILE.exists():
        return data

    try:
        with OBS_FILE.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        data["ledger_rows"] = len(rows)

        returns = []

        for row in rows:
            raw = row.get("forward_return_pct", "")

            if raw is None or str(raw).strip() == "":
                continue

            try:
                value = float(raw)
            except (ValueError, TypeError):
                continue

            returns.append(value)

            ts = row.get("timestamp")

            if ts:
                current = data["last_observation_timestamp"]

                if current is None or str(ts) > str(current):
                    data["last_observation_timestamp"] = ts

        data["valid_forward_observations"] = len(returns)

        if returns:
            data["positive_forward_observations"] = sum(
                1 for x in returns if x > 0
            )

            data["mean_forward_return_pct"] = (
                sum(returns) / len(returns)
            )

            ordered = sorted(returns)
            n = len(ordered)

            if n % 2:
                median = ordered[n // 2]
            else:
                median = (
                    ordered[n // 2 - 1]
                    + ordered[n // 2]
                ) / 2

            data["median_forward_return_pct"] = median

            data["hit_rate_pct"] = (
                data["positive_forward_observations"]
                / len(returns)
                * 100
            )

    except Exception as exc:
        data["ledger_error"] = f"{type(exc).__name__}: {exc}"

    return data


def evidence_status(n):
    if n < 10:
        return "INSUFFICIENT_EVIDENCE"

    if n < 20:
        return "PRELIMINARY_INCONCLUSIVE"

    return "FORMAL_HOLDOUT_EVALUATION"


def run_engine_once():
    command = [
        sys.executable,
        str(ENGINE),
        "--once",
    ]

    print()
    print("=" * 88)
    print("RUNNING AURA v0.5.3.7 RESEARCH ENGINE")
    print("=" * 88)

    try:
        completed = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if completed.stdout:
            print(completed.stdout, end="")

        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")

        return completed.returncode, completed.stdout + "\n" + completed.stderr

    except Exception as exc:
        print(
            f"ENGINE LAUNCH ERROR: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1, ""


def build_heartbeat(engine_output, return_code):
    parsed = parse_engine_output(engine_output)
    ledger = read_observation_ledger()

    n = ledger["valid_forward_observations"]

    status = evidence_status(n)

    heartbeat = {
        "version": VERSION,
        "heartbeat_timestamp": iso(utc_now()),

        "mode": "RESEARCH_ONLY",
        "control": "C0_FROZEN",

        "candidate": (
            "BEAR x LOW ATR x POSITIVE bar-2"
        ),

        "atr_threshold_pct": 0.596,
        "ema_4h": "EMA50",
        "forward_observation": "4H",

        "orders_allowed": False,
        "paper_execution": False,
        "live_execution": False,

        "engine": {
            "version": "AURA v0.5.3.7",
            "return_code": return_code,
            "healthy": return_code == 0,
        },

        "market": {
            "latest_closed_hour": parsed["latest_closed_hour"],

            "new_closed_bars": parsed["new_closed_bars"],

            "btc": {
                "bars": parsed["btc_bars"],
                "first": parsed["btc_first"],
                "last": parsed["btc_last"],
            },

            "eth": {
                "bars": parsed["eth_bars"],
                "first": parsed["eth_first"],
                "last": parsed["eth_last"],
            },
        },

        "evidence": {
            "completed_observations": (
                parsed["completed_observations"]
                if parsed["completed_observations"] is not None
                else n
            ),

            "valid_holdout_n": n,

            "positive_forward_observations": (
                ledger["positive_forward_observations"]
            ),

            "mean_forward_return_pct": (
                ledger["mean_forward_return_pct"]
            ),

            "median_forward_return_pct": (
                ledger["median_forward_return_pct"]
            ),

            "hit_rate_pct": (
                ledger["hit_rate_pct"]
            ),

            "last_observation_timestamp": (
                ledger["last_observation_timestamp"]
            ),

            "evidence_status": status,
        },

        "guardrails": {
            "strategy_changed": False,
            "parameters_changed": False,
            "orders_allowed": False,
            "paper_execution": False,
            "live_execution": False,
        },

        "files": {
            "observation_ledger": str(OBS_FILE),
            "heartbeat_json": str(HEARTBEAT_JSON),
            "heartbeat_log": str(HEARTBEAT_LOG),
        },

        "engine_error": parsed["engine_error"],
    }

    return heartbeat


def print_heartbeat(h):
    print()
    print("=" * 88)
    print(f"{VERSION} — AUDIT / HEARTBEAT")
    print("=" * 88)

    print(f"MODE                    : {h['mode']}")
    print(f"CONTROL                 : {h['control']}")
    print(f"CANDIDATE               : {h['candidate']}")
    print(f"ATR THRESHOLD           : {h['atr_threshold_pct']:.3f}%")
    print(f"4H EMA                  : {h['ema_4h']}")
    print(f"FORWARD OBSERVATION     : {h['forward_observation']}")

    print()
    print("MARKET DATA")
    print("-" * 88)

    print(
        f"LATEST CLOSED HOUR     : "
        f"{h['market']['latest_closed_hour']}"
    )

    print(
        f"NEW CLOSED BARS        : "
        f"{h['market']['new_closed_bars']}"
    )

    btc = h["market"]["btc"]
    eth = h["market"]["eth"]

    print(
        f"BTC/USD                : "
        f"{btc['bars']} bars | {btc['first']} -> {btc['last']}"
    )

    print(
        f"ETH/USD                : "
        f"{eth['bars']} bars | {eth['first']} -> {eth['last']}"
    )

    print()
    print("PROSPECTIVE EVIDENCE")
    print("-" * 88)

    e = h["evidence"]

    print(
        f"COMPLETED OBSERVATIONS : "
        f"{e['completed_observations']}"
    )

    print(
        f"VALID HOLDOUT N        : "
        f"{e['valid_holdout_n']}"
    )

    print(
        f"POSITIVE OBSERVATIONS  : "
        f"{e['positive_forward_observations']}"
    )

    mean = e["mean_forward_return_pct"]
    median = e["median_forward_return_pct"]
    hit = e["hit_rate_pct"]

    print(
        f"MEAN FORWARD RETURN    : "
        f"{mean:.4f}%" if mean is not None
        else "MEAN FORWARD RETURN    : N/A"
    )

    print(
        f"MEDIAN FORWARD RETURN  : "
        f"{median:.4f}%" if median is not None
        else "MEDIAN FORWARD RETURN  : N/A"
    )

    print(
        f"HIT RATE               : "
        f"{hit:.1f}%" if hit is not None
        else "HIT RATE               : N/A"
    )

    print(
        f"EVIDENCE STATUS        : "
        f"{e['evidence_status']}"
    )

    print()
    print("GUARDRAILS")
    print("-" * 88)

    g = h["guardrails"]

    print(
        f"STRATEGY CHANGED       : "
        f"{'YES' if g['strategy_changed'] else 'NO'}"
    )

    print(
        f"PARAMETERS CHANGED     : "
        f"{'YES' if g['parameters_changed'] else 'NO'}"
    )

    print(
        f"ORDERS ALLOWED         : "
        f"{'YES' if g['orders_allowed'] else 'NO'}"
    )

    print(
        f"PAPER EXECUTION        : "
        f"{'YES' if g['paper_execution'] else 'NO'}"
    )

    print(
        f"LIVE EXECUTION         : "
        f"{'YES' if g['live_execution'] else 'NO'}"
    )

    print()
    print("STATUS")
    print("-" * 88)

    if h["engine"]["healthy"]:
        print("ENGINE STATUS          : HEALTHY")
    else:
        print("ENGINE STATUS          : ERROR")

    print(
        f"HEARTBEAT              : "
        f"{h['heartbeat_timestamp']}"
    )

    print(
        f"NEXT POLL              : "
        f"{POLL_SECONDS} seconds"
    )

    print()
    print("FILES")
    print("-" * 88)
    print(f"OBSERVATIONS           : {OBS_FILE}")
    print(f"HEARTBEAT JSON         : {HEARTBEAT_JSON}")
    print(f"HEARTBEAT LOG          : {HEARTBEAT_LOG}")

    print("=" * 88)


def write_heartbeat(h):
    with HEARTBEAT_JSON.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            h,
            f,
            indent=2,
            ensure_ascii=False,
        )

    with HEARTBEAT_LOG.open(
        "a",
        encoding="utf-8",
    ) as f:
        f.write(
            json.dumps(
                h,
                ensure_ascii=False,
            )
            + "\n"
        )


def run_once():
    return_code, output = run_engine_once()

    heartbeat = build_heartbeat(
        output,
        return_code,
    )

    write_heartbeat(heartbeat)
    print_heartbeat(heartbeat)

    return return_code


def main():
    ensure_dirs()

    print()
    print(f"{VERSION} starting...")
    print(
        "Audit wrapper around v0.5.3.7. "
        "No trading functionality is implemented."
    )
    print(
        "Orders are permanently disabled."
    )

    if "--once" in sys.argv:
        run_once()
        return

    while True:
        try:
            run_once()

        except KeyboardInterrupt:
            print()
            print("AURA v0.5.3.8 stopped by user.")
            return

        except Exception as exc:
            print()
            print(
                "HEARTBEAT ERROR:",
                type(exc).__name__,
                str(exc),
            )
            print(
                "The next cycle will retry."
            )

        print()
        print(
            f"Sleeping {POLL_SECONDS} seconds "
            f"until next audit cycle..."
        )

        try:
            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            print()
            print("AURA v0.5.3.8 stopped by user.")
            return


if __name__ == "__main__":
    main()
