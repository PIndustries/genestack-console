#!/usr/bin/env python3
"""Export the OpenAPI spec from the running FastAPI app.

Usage:
    python scripts/export_openapi.py              # exports to openapi.json
    python scripts/export_openapi.py --md          # also generates API_REFERENCE.md
"""
import argparse
import json
import sys
import os

# Add parent dir to path so app module is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.main import app


def export_json(path: str):
    spec = app.openapi()
    with open(path, 'w') as f:
        json.dump(spec, f, indent=2)
    print(f"OpenAPI spec written to {path}")


def main():
    parser = argparse.ArgumentParser(description="Export OpenAPI spec")
    parser.add_argument("output", nargs="?", default="openapi.json", help="Output file path")
    args = parser.parse_args()
    export_json(args.output)


if __name__ == "__main__":
    main()
