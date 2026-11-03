"""Run the full extraction: SARB API -> tidy BA900 -> reports -> statements -> reconciliation.

    uv run python -m bankdata.pipeline            # everything (cached steps are skipped)
    uv run python -m bankdata.pipeline --no-sarb  # skip the SARB download/refresh
"""
from __future__ import annotations

import sys

from . import ba900, build, extract, reports, sarb


def main(argv: list[str]) -> None:
    if "--no-sarb" not in argv:
        sarb.download_all()
        sarb.build_tidy()
    ba900.build()
    reports.run()
    extract.run()
    build.main()


if __name__ == "__main__":
    main(sys.argv[1:])
