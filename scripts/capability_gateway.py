"""Trusted same-PID launcher: arm the independent guard before loading Hermes/plugins."""

import argparse
import json
import os
from pathlib import Path
import runpy
import sys

BOOTSTRAP = (
    "import os, sys, runpy; from scripts.capability_gateway import arm; arm(); "
    "sys.argv = ['gateway.run']; "
    "runpy.run_module('gateway.run', run_name='__main__', alter_sys=True)"
)


def _arm():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    from scripts.service_runtime import check
    from scripts.model_contract import capability_mode
    config = check(args.source, args.home)
    if not capability_mode(config):
        raise ValueError("capability_profile_required")
    os.chdir(args.source)
    sys.path.insert(0, str(args.source))
    from agent.deterministic_policy import arm_deterministic_policy
    from scripts.capability_policy import Audit, load_registry
    registry, _ = load_registry()
    audit = Audit(args.home, registry)
    with audit.transaction():
        pass
    arm_deterministic_policy(audit.guard_refusal)


def arm():
    try:
        _arm()
    except Exception:
        print(json.dumps({"result": "failed", "code": "capability_start_failed"}))
        raise SystemExit(78) from None


if __name__ == "__main__":
    arm()
    sys.argv = ["gateway.run"]
    runpy.run_module("gateway.run", run_name="__main__", alter_sys=True)
