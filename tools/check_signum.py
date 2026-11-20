"""Run one Signum delivery evidence check with a bounded deadline."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
EXIT_CODES = {"healthy": 0, "degraded": 1, "unknown": 2}


def run(project, delivery_id, deadline, basis):
    try:
        command = [sys.executable, "-B", str(ROOT / "tools/observe_evidence.py"),
                   "--project", str(project), "--delivery", delivery_id]
        if basis:
            command.extend(("--propose", "--basis", basis))
        proc = subprocess.run(command, capture_output=True, text=True, timeout=deadline)
        try:
            observation = json.loads(proc.stdout)
        except ValueError as error:
            raise ValueError(f"Observer returned no valid JSON (exit {proc.returncode}).") from error
        if (not isinstance(observation, dict) or observation.get("delivery_id") != delivery_id
                or observation.get("status") not in EXIT_CODES
                or EXIT_CODES[observation["status"]] != proc.returncode):
            raise ValueError("The observer's result and exit status are inconsistent.")
        if observation["status"] == "healthy":
            card = observation.get("card")
            report = observation.get("report")
            if (observation.get("service") != "available" or not isinstance(card, dict)
                    or card.get("owner_accepted") is not True or card.get("green") is not True
                    or not isinstance(report, dict) or report.get("status") != "verified"
                    or report.get("manifest") != "declared" or observation.get("findings") != []):
                raise ValueError("The observer did not establish the accepted report and its attachments.")
        return observation, proc.returncode
    except subprocess.TimeoutExpired:
        error_message = f"Observer exceeded its {deadline}-second deadline."
    except (KeyError, OSError, TypeError, ValueError) as error:
        error_message = str(error)
    return {"status": "unknown", "delivery_id": delivery_id, "service": "unknown", "card": None,
            "error": error_message, "component": "observer", "proposal_outcome": "unverified"}, 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--delivery", required=True, help="full delivery version ID")
    parser.add_argument("--deadline-seconds", type=int, default=5, choices=range(1, 31), metavar="1..30")
    parser.add_argument("--propose", action="store_true", help="record a pending proposal for a diagnosed fault")
    parser.add_argument("--basis", help="full ID of the current owner decision authorizing repair proposals")
    args = parser.parse_args()
    if args.propose != bool(args.basis):
        parser.error("--propose and --basis must be used together")
    result, code = run(args.project, args.delivery, args.deadline_seconds, args.basis)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(code)
