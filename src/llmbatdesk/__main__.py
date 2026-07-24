from __future__ import annotations

import sys

from llmbatdesk.qt.app import run


def main() -> None:
    raise SystemExit(run(sys.argv))


if __name__ == "__main__":
    main()
