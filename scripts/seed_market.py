#!/usr/bin/env python3
"""Convenience CLI wrapper for app.scripts.seed_market."""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.scripts.seed_market import seed_market

if __name__ == "__main__":
    seed_market(silent=False)
