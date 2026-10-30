#!/usr/bin/env python3
"""Offline code updater for the CRM chatbot POC.

Carries the latest content of the changed application source files and writes it
over whatever is on this machine. Generated on 2026-10-30 17:09 from branch feature_manoj.

It touches ONLY the Python source files listed in FILES below. Config, .env,
.env.example, pyproject.toml, README.md and the documentation files are never
written and never read.

Usage (run from anywhere, no third-party packages needed):

    python update_code.py --dry-run     show what would change, write nothing
    python update_code.py               back up, then replace the files
    python update_code.py --yes         same, without the confirmation prompt
    python update_code.py --no-backup   replace without keeping copies
    python update_code.py --root PATH   if the project root is not auto-detected

Existing files are copied into a timestamped folder under _code_backup/ at the
project root before anything is overwritten, so a bad run can be undone by
copying them back.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import shutil
import sys
from datetime import datetime
from pathlib import Path

GENERATED_AT = "2026-10-30 17:09"

# The file this script lives next to; used to confirm we found the right project.
ANCHOR = "src/app/main.py"

# relative path -> (sha256 of new content, byte size, gzip+base64 of new content)
FILES = {
    "src/app/main.py": (
        "567b6f4ed32be9418e9bbda4f92bc643da17d874a39bfec4e274797abd505ab5",
        5469,
        "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",
    ),
    "src/app/errors.py": (
        "dde0d81fb956fdd4f5f5b338fd00d3d201f02b9df064b022e17c49c7657adb8c",
        2705,
        "H4sIAAAAAAAC/8VW30/bMBB+r9T/4dSnRoIOTdsLUidlpUIZo2Ut5WWaIse5NBapndkOrPvxv++cHyUUJtqBRB8axz7f"
        "fd9353O6nV6v5+d5JjizQslDkyMXieCAPzjmbgp4xoxBMyDLbqfbSbRaQRgmhS00hiGIVa60BSalsqUPs7Gy61zIZWPh"
        "y7Vb6XZKh0BRx1or3R83kbzjbgfoR4E+MoPA7nC14TCt186tfxEcGpYgxGiZyDYAnYsYE8IopLBh2DeYJQewQmPYEo/B"
        "WH0A6EKHXMXNhCHwhalnhLQHjdtjiAW3X0sjovANfsNESYRh+fDg8EM5qLHX+AOKLFgmfiLYFCF6QMeFrwA3u0yRo+57"
        "gw3sGrDXMiEig3qa4tejrfU7ZmRy97Jl1aJLZq23LbtaBLJpRkrDrz/tRF4Rz7ikVeWzSWwrnTMmDMZwm6IkbblaufxF"
        "hRGSKNBMXlgQbnDjfO2VyBfKEoObDY19sjOsn+2KGvau/M/BiX8ZTCfheDabznr36mv47uhoA3xYP722pCfMMp9zcr2T"
        "pJ/m04nLizsREePXNK/RaoHECZK9z8aLSUpyMmAlj+drSnr6oT8ajefzR0V9/5SoCym+FxhIi0u9e7m641vtBD9wjaHZ"
        "TY3ItTyIEApn7VpRtn4doR8H+GzJF5Pgy2IcBpPL8ens3+X8pPIzVVg68TvrrTRPkeRgloq6lpn8oqa+QXTdbYC8KDnm"
        "zKavorm8j1JXFJ+v+Wy6uAwmp//XN855vm99Mzgn1xmMFG38YeFCK6u4yqB/Prrw6BLQN+iSkDWdpFH6jO5jWs4ZBUMo"
        "7/vHTxgYBeQLSClp3JfAm7yJ4TzSV4SpPDJNulrqW1FhWZTR1anKciBYqCW1MuelBkT+U3rYlG0f0fnJ2aDyNyLQqA30"
        "cbAclFajTKC04BBqxssC8pc04xFBy1OyoVuIyZhEJgZ0NdI/x6TIsrVjwWAlDFcyEUtCHVdRiGAhNTKelphbGCW6/4jW"
        "rk0ZnitiyCsMooUhydTtYJOdVyhlB/pFOwc53KFtvH1YzX8BHZUrTZEKAAA=",
    ),
    "src/app/api/crm_routes.py": (
        "649d6ac3341338fe19ac88011133b90670ff963fbbc1052cf48dade1578eb89e",
        6582,
        "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",
    ),
    "src/app/agents/base_tool.py": (
        "e144b81b2a788893ef33ff21c23cce134ad3513256cfe35e9093f6cc9742ad94",
        6503,
        "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",
    ),
    "src/app/agents/crm_agent.py": (
        "18f244a96d5162fb583dea66bba0c2a951105bbd7494d2fd3dd332d4e1eda50a",
        5382,
        "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",
    ),
    "src/app/agents/data_query_tool.py": (
        "24fa76b9c7ed34bd00e9e8c24248ab0324ae216978d8cafe7492846de98c8c3b",
        12620,
        "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"
        "6EwxAAA=",
    ),
    "src/app/agents/prompts.py": (
        "eb0e79f5f6bf1a5e523731b2634f07e586114fbf326dd17932618bebd85285cf",
        33920,
        "H4sIAAAAAAAC/819a2/c2nX29wD5D4QPUNntzKg+TtJ23PctdGTZViPJriTn9AAFHM6QM8OIQ05IjmS1KNBP/QFFf2F/"
        "SdezLvtCUrKcFEgKpMeaGZJ7r72uz7rw5z979uzZx6be7rqky790yb7Ns2Rxn3SbPGn3i2m6zquundGvfv6zn//s/fX5"
        "2efLk4s3J5enF+8+X346O7lK/l/y/Oc/S+j/nuHrpMmrLG+Kap00+zJvk+dX15enx9cv5v9SPdPfTZM3dVLVXVLvu92+"
        "S87zZpsW2SS5+s27SfLP52eTZJlWt2mbLOssnyT/mN6mV8um2HWT5PjqKlmU9fKmnSR1k6TVfVLTYptkuUmbLnlzdTYL"
        "H3RN+1g1ddXRqpJteo9r8B88XVaaYNUzW1Hatvttzp/R/ne7uuleMzHSqr2jH2/3bZe0XVGWdHma0QKbJl925T1dmezK"
        "tKiEjsUq6dJ1m6QN0XFT31VJWXR5k5blfbS+T60+rK7kHmmV1LuuqKu0TPJqk1bLfEtHkKxo4e0uXxarYpm0y7xKm6Ju"
        "kzvaOi0uabd0a1pY2tHXqyZd80XLMk8bum+x3TX1LR0G1pwuClrLvdvyNr3RNWCfWb5K9yWI0+7qilZHD96mXbToj02+"
        "UsLJunlxG6JVsinWm2lbrLF8ouQ2be71uOjP5QY73NAayqLKk23eNcWSznHfgMuStMybDsfKZN9XRVfk9CfRl76dpsuu"
        "uKV1J7SR5rbI7/BNmYJU7abYJW2V7mgNHbNFhTNY1WVZ3033u3Y2wnt7o/zvcKRdTTunw0y7nK7PigoLX9Ez97SRVZGX"
        "WTtLTqtkR0xWLPdl2kySmzzfJbdpuedFJBmuTYnNmqK9SYjgq6LME2KIgC3AsfR0omhJ/LGg75d5WU6U9eSAwSNMx5L+"
        "15E48iqX9DGJS7yV0xVtA6KGn+iCik6YYU5f7fleNdF6SSJOu4nPJFnkdHK5nnuX0nohJM1NBoaV9dGKF/uyzB9/8k44"
        "Ak9a7fHMZVlDk9w16W5HX9jZE6PSRXTb3/59Vtz+/9/i9r/9+5YkiChIf94V3YZ+st2BMpuizFiKZsnRbV1ktEbaX5WT"
        "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"
        "NKvfSGP8N7nk/ws6+FnHgIQAAA==",
    ),
    "src/app/agents/relationship_manager.py": (
        "fc8fa9d305daf7295b17e5d9b75ab27ec5c4e44ac6b9eb7aef3965b4feca0aed",
        68882,
        "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",
    ),
    "src/app/services/crm_tools.py": (
        "e3b5fad728dc4d98b64f9adea963ed4c7413f286d55c883e26802bcc185233ce",
        6728,
        "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",
    ),
    "src/app/services/unique_toolkit.py": (
        "9cbb3e8e5567ee0220c311eea38d0e9602558acaab9eacc615828126c5e03155",
        15357,
        "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"
        "EkAloQBmdsDccxdTcqnESBf2WNC7Rq9L2GWqywxe+7UWHY7dT1wEAN92QJTis7Ne4LuWcIpK1AnJz1NETL00dg2NUKCY"
        "5p4o4uWGeFGezdo9eNa7mC7wY1VqJPtvh96gV3S0ztmWBIDIsCrd3j29RCxCPcjYD7y+QmlGPlT8UWKM5g7yYmiunWmh"
        "auxAgQL5mVOiQuaVYFN0CmOFSeLUvTGSNnem73Fkd8FuauygXiNpwKR4AnBAXCocEyr/CHayg3xmTKhpuo/aUaOChcQl"
        "r6spImYIeD6cbYm6eRvQREcf8TAa7ZqbmikdTtVJjHM4DY/mmW14vLJeYnp37HKz774zFHojM0xRqWn6BiUlbDAZxLmW"
        "kieOXSHMaYGaL0mMBSMrNn87TcvWfCQyDWKuPnfaiV67aIpOo2y75abvR7q28JsVEydfu0n6Yoj/JOgJnLx6MPUp/ULO"
        "vPsR9vXUjuyYT9zMoJo0ckpfq8L/ZTEg1KP634xA7AffKIB46/NkrHhRuyhKAaL8wqZ7vIZM+fiJotyZNaHJQVlgFHkK"
        "EDxq+cOiSudTXvpDG1KP3HFwr4PVHFnM5p4VjJiQvP1zknZnkJcweXrl3YEBNv2lfdZ5fUgTPJf0ZZ6OKu9ZHrGi06n1"
        "YeAN8Vsl7N2L45/fPY2O3r39d/T45fNXRy9+iY6fUCNN4Kttd4a/e/P0NYylgo2uu+mUXpxs83xHjSsIl0r6xoHh6Viz"
        "dOwHTBfBlquOnALdlDbziHAOgBIHmw0ORprnU+F/CJCNA/qMeARVGUDsYdCzPQup+0xOXM/2AhKCTnj2mXbyjtpSdNdb"
        "86Umby7j+/afX3HXR7lzTRW2cXDrf5Toozj9OwAA",
    ),
}


def decode(key):
    """Return the new content for *key*, verifying it survived the trip intact."""
    digest, size, blob = FILES[key]
    raw = gzip.decompress(base64.b64decode(blob))
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise SystemExit("ERROR: embedded payload for %s is corrupted." % key)
    return raw


def find_root(explicit):
    """Locate the project root (the folder that contains src/app/main.py)."""
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not (root / ANCHOR).is_file():
            raise SystemExit(
                "ERROR: %s does not look like the project root (no %s)." % (root, ANCHOR)
            )
        return root

    here = Path(__file__).resolve()
    # Normal case: this script sits beside main.py, so the root is three levels up.
    candidates = [here.parent.parent.parent, here.parent, Path.cwd()]
    candidates += list(here.parents) + list(Path.cwd().parents)
    for candidate in candidates:
        if (candidate / ANCHOR).is_file():
            return candidate

    raise SystemExit(
        "ERROR: could not find the project root.\n"
        "       Expected a folder containing %s.\n"
        '       Re-run with --root, e.g. python update_code.py --root "<path to the poc folder>"'
        % ANCHOR
    )


def classify(path, new):
    if not path.exists():
        return "new"
    if path.read_bytes() == new:
        return "same"
    return "update"


def main():
    parser = argparse.ArgumentParser(
        description="Replace POC source files with the latest code."
    )
    parser.add_argument("--root", help="project root containing src/app/main.py")
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    parser.add_argument(
        "--no-backup", action="store_true", help="do not keep copies of the old files"
    )
    parser.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    root = find_root(args.root)
    print("Project root : %s" % root)
    print("Code built   : %s" % GENERATED_AT)
    print("Files carried: %d" % len(FILES))
    print("")

    plan = []
    for rel in sorted(FILES):
        new = decode(rel)
        target = root / rel
        plan.append((rel, target, new, classify(target, new)))

    label = {"new": "ADD    ", "same": "SKIP   ", "update": "REPLACE"}
    for rel, _target, new, state in plan:
        note = "  (already identical)" if state == "same" else ""
        print("  %s %-45s %6d bytes%s" % (label[state], rel, len(new), note))

    changing = [row for row in plan if row[3] != "same"]
    print("")
    if not changing:
        print("Nothing to do - every file already matches the latest code.")
        return 0

    if args.dry_run:
        print("Dry run: %d file(s) would be written. Nothing was changed." % len(changing))
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("Not running interactively; re-run with --yes to confirm.")
            return 1
        answer = input("Overwrite %d file(s) under %s? [y/N] " % (len(changing), root))
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted. Nothing was changed.")
            return 1

    backup_dir = None
    if not args.no_backup:
        backup_dir = root / "_code_backup" / datetime.now().strftime("%Y%m%d_%H%M%S")

    written = 0
    for rel, target, new, _state in changing:
        if backup_dir is not None and target.exists():
            copy_to = backup_dir / rel
            copy_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, copy_to)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new)
        written += 1
        print("  wrote %s" % rel)

    print("")
    print("Done: %d file(s) updated." % written)
    if backup_dir is not None:
        print("Previous versions saved in: %s" % backup_dir)
    print("Restart the app (e.g. uvicorn) so the new code is picked up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
