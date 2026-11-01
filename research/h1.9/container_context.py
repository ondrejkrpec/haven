"""Create an explicit public-only build context for clean recovery acceptance."""

import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
FILES = {
    "research/h1.9/Containerfile": "Containerfile",
    "research/h0.1/patches/upstream-117820.patch": "upstream-117820.patch",
    "research/h1.6.1/deterministic-policy.patch": "deterministic-policy.patch",
}
SCRIPTS = ("backup_runtime", "backup_capture", "backup_restore", "private_enrollment", "model_contract", "model_probe",
           "service_runtime", "capability_runtime", "capability_policy", "capability_gateway", "telegram_runtime", "whatsapp_runtime")
CONFIGS = ("capability-runtime.json", "capabilities.json", "telegram-runtime.json", "whatsapp-runtime.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    destination = args.destination.absolute()
    destination.mkdir(mode=0o700)
    selected = {**FILES,
                **{"scripts/" + name + ".py": "public-tools/scripts/" + name + ".py" for name in SCRIPTS},
                **{"config/" + name: "public-tools/config/" + name for name in CONFIGS},
                **{"plugins/haven-capabilities/" + name: "public-tools/plugins/haven-capabilities/" + name
                   for name in ("plugin.yaml", "__init__.py")}}
    for source, target in selected.items():
        path = ROOT / source
        if path.is_symlink() or not path.is_file():
            raise ValueError("untrusted_public_input")
        output = destination / target
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output)
    print("Explicit public-only recovery context ready.")


if __name__ == "__main__":
    main()
