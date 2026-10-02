"""SessionStart launcher. Placeholder until milestone 6.

Hard rules (SPEC 8.2): never write to stdout, always exit 0.
"""

import sys


def main() -> None:
    try:
        sys.stdin.read()
        print("cc-mission-control launcher stub ran", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"cc-mission-control launcher: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
    sys.exit(0)
