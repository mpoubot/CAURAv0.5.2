from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


VERSION = "AURA v0.5.3.9"

BASE_DIR = Path(__file__).resolve().parent

OBS_FILE = (
    BASE_DIR
    / "regime_output"
    / "prospective_holdout"
    / "prospective_candidate_observations.csv"
)

OUT_DIR = (
    BASE_DIR
    / "regime_output"
    / "prospective_integrity"
)

REPORT_FILE = OUT_DIR / "prospective_integrity_report.json"
HASH_FILE = OUT_DIR / "prospective_ledger_sha256.txt"


FROZEN_ATR_THRESHOLD = 0.596
FROZEN_EMA = "EMA50"
FROZEN_CANDIDATE = "BEAR x LOW ATR x POSITIVE bar-2"


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def parse_ts(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except Exception:
        return None


def sha256_file(path):
    h = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def read_rows():
    if not OBS_FILE.exists():
        return []

    with OBS_FILE.open(
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as f:
        return list(csv.DictReader(f))


def numeric(row, key):
    value = row.get(key)

    if value is None or str(value).strip() == "":
        return None

    try:
        return float(value)
    except Exception:
        return None


def audit_rows(rows):
    result = {
        "rows": len(rows),
        "valid_rows": 0,
        "invalid_rows": 0,
        "duplicate_keys": 0,
        "timestamp_errors": 0,
        "chronology_errors": 0,
        "incomplete_forward_observations": 0,
        "lookahead_errors": 0,
        "parameter_errors": 0,
        "candidate_link_errors": 0,
        "symbol_errors": 0,
        "errors": [],
    }

    seen = set()

    # Chronology is checked independently for each market symbol.
    previous_timestamp_by_symbol = {}

    for index, row in enumerate(rows, start=2):

        symbol = str(row.get("symbol", "")).strip()

        if symbol not in {"BTC/USD", "ETH/USD"}:
            result["symbol_errors"] += 1
            result["errors"].append(
                f"row {index}: invalid symbol={symbol!r}"
            )

        timestamp = parse_ts(row.get("timestamp"))

        if timestamp is None:
            result["timestamp_errors"] += 1
            result["errors"].append(
                f"row {index}: invalid timestamp"
            )
            continue

        key = (symbol, timestamp.isoformat())

        if key in seen:
            result["duplicate_keys"] += 1
            result["errors"].append(
                f"row {index}: duplicate {symbol} {timestamp}"
            )

        seen.add(key)

        previous_timestamp = previous_timestamp_by_symbol.get(symbol)

        if previous_timestamp is not None:
            if timestamp < previous_timestamp:
                result["chronology_errors"] += 1
                result["errors"].append(
                    f"row {index}: {symbol} timestamp moved backwards"
                )

        previous_timestamp_by_symbol[symbol] = timestamp

        forward_1h = numeric(
            row,
            "forward_1h_return_pct"
        )

        forward_4h = numeric(
            row,
            "forward_4h_return_pct"
        )

        forward_hours = numeric(
            row,
            "forward_hours_available"
        )

        candidate = str(
            row.get("candidate", "")
        ).strip().lower()

        if forward_4h is None:
            result["incomplete_forward_observations"] += 1

        if forward_hours is not None:
            if forward_hours < 4:
                result["incomplete_forward_observations"] += 1

        # A completed 4H observation must have the 4H return.
        if forward_4h is not None:
            result["valid_rows"] += 1
        else:
            result["invalid_rows"] += 1

        # Frozen candidate linkage.
        if candidate:
            expected_tokens = [
                "bear",
                "low",
                "positive",
            ]

            if not all(
                token in candidate
                for token in expected_tokens
            ):
                result["candidate_link_errors"] += 1
                result["errors"].append(
                    f"row {index}: unexpected candidate={candidate!r}"
                )

        # Look-ahead protection:
        #
        # The stored forward returns must not be accompanied
        # by a forward observation duration greater than the
        # available market horizon.
        if forward_4h is not None:
            if forward_hours is not None and forward_hours < 4:
                result["lookahead_errors"] += 1
                result["errors"].append(
                    f"row {index}: 4H return exists with "
                    f"forward_hours_available={forward_hours}"
                )

        # Frozen ATR threshold.
        atr_threshold = numeric(
            row,
            "atr_threshold_pct"
        )

        if atr_threshold is not None:
            if abs(
                atr_threshold - FROZEN_ATR_THRESHOLD
            ) > 1e-9:
                result["parameter_errors"] += 1
                result["errors"].append(
                    f"row {index}: ATR threshold changed: "
                    f"{atr_threshold}"
                )

    return result


def evidence_status(n):
    if n < 10:
        return "INSUFFICIENT_EVIDENCE"

    if n < 20:
        return "PRELIMINARY_INCONCLUSIVE"

    return "FORMAL_HOLDOUT_EVALUATION"


def main():

    OUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    print()
    print("=" * 88)
    print(f"{VERSION} — PROSPECTIVE EVIDENCE INTEGRITY AUDITOR")
    print("=" * 88)

    print()
    print("MODE                    : RESEARCH ONLY")
    print("ORDERS                  : DISABLED")
    print("PAPER EXECUTION         : DISABLED")
    print("LIVE EXECUTION          : DISABLED")
    print()
    print(f"FROZEN CANDIDATE        : {FROZEN_CANDIDATE}")
    print(f"FROZEN ATR THRESHOLD    : {FROZEN_ATR_THRESHOLD:.3f}%")
    print(f"FROZEN 4H EMA           : {FROZEN_EMA}")

    rows = read_rows()

    print()
    print("LEDGER")
    print("-" * 88)
    print(f"FILE                    : {OBS_FILE}")
    print(f"ROWS                    : {len(rows)}")

    if OBS_FILE.exists():
        digest = sha256_file(OBS_FILE)

        HASH_FILE.write_text(
            digest + "\n",
            encoding="utf-8"
        )

        print(f"SHA256                  : {digest}")

    audit = audit_rows(rows)

    n = audit["valid_rows"]

    # Pending observations are expected in a live prospective ledger.
    # A row without a completed 4H return is NOT an integrity failure.
    # It simply has not reached its observation horizon yet.

    integrity_pass = (
        audit["timestamp_errors"] == 0
        and audit["chronology_errors"] == 0
        and audit["duplicate_keys"] == 0
        and audit["lookahead_errors"] == 0
        and audit["parameter_errors"] == 0
        and audit["candidate_link_errors"] == 0
        and audit["symbol_errors"] == 0
    )

    report = {
        "version": VERSION,
        "audit_timestamp": iso(utc_now()),

        "frozen_configuration": {
            "candidate": FROZEN_CANDIDATE,
            "atr_threshold_pct": FROZEN_ATR_THRESHOLD,
            "ema_4h": FROZEN_EMA,
            "forward_observation": "4H",
        },

        "orders_allowed": False,
        "paper_execution": False,
        "live_execution": False,

        "ledger": {
            "file": str(OBS_FILE),
            "sha256": (
                sha256_file(OBS_FILE)
                if OBS_FILE.exists()
                else None
            ),
        },

        "audit": audit,

        "evidence": {
            "valid_holdout_n": n,
            "evidence_status": evidence_status(n),
        },

        "integrity": {
            "pass": integrity_pass,
            "status": (
                "INTEGRITY_PASS"
                if integrity_pass
                else "INTEGRITY_FAIL"
            ),
        },
    }

    REPORT_FILE.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print()
    print("INTEGRITY CHECK")
    print("-" * 88)

    print(
        f"DUPLICATE KEYS          : "
        f"{audit['duplicate_keys']}"
    )

    print(
        f"TIMESTAMP ERRORS        : "
        f"{audit['timestamp_errors']}"
    )

    print(
        f"CHRONOLOGY ERRORS       : "
        f"{audit['chronology_errors']}"
    )

    print(
        f"INCOMPLETE OBSERVATIONS : "
        f"{audit['incomplete_forward_observations']}"
    )

    print(
        f"LOOK-AHEAD ERRORS       : "
        f"{audit['lookahead_errors']}"
    )

    print(
        f"PARAMETER ERRORS        : "
        f"{audit['parameter_errors']}"
    )

    print(
        f"CANDIDATE LINK ERRORS   : "
        f"{audit['candidate_link_errors']}"
    )

    print(
        f"SYMBOL ERRORS           : "
        f"{audit['symbol_errors']}"
    )

    print()
    print("PROSPECTIVE EVIDENCE")
    print("-" * 88)
    print(f"VALID HOLDOUT N         : {n}")
    print(
        f"EVIDENCE STATUS         : "
        f"{evidence_status(n)}"
    )

    print()
    print("FINAL INTEGRITY STATUS")
    print("-" * 88)

    if integrity_pass:
        print("INTEGRITY STATUS        : PASS")
    else:
        print("INTEGRITY STATUS        : FAIL")

        if audit["errors"]:
            print()
            print("FIRST ERRORS")
            for error in audit["errors"][:10]:
                print(f"  - {error}")

    print()
    print("FILES")
    print("-" * 88)
    print(f"REPORT                  : {REPORT_FILE}")
    print(f"LEDGER HASH             : {HASH_FILE}")

    print("=" * 88)


if __name__ == "__main__":
    main()

