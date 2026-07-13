#!/usr/bin/env python3
"""Create explicit sensitivity-separated RoleNavi exports."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store_io


def main() -> int:
    parser = argparse.ArgumentParser()
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--public", action="store_true", help="public opportunities only")
    scope.add_argument("--private", action="store_true", help="private pipeline only")
    parser.add_argument("--xlsx", action="store_true")
    args = parser.parse_args()
    con = store_io.connect()
    try:
        paths = store_io.export_views(con, public=args.public, private=args.private,
                                      xlsx=args.xlsx)
    finally:
        con.close()
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
