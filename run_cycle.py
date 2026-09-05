"""
Entry point: python run_cycle.py

Loads config from config.yaml at the repo root and runs one full cycle.
"""

from edgedash.config import load_config
from edgedash.orchestrator import run_cycle

if __name__ == "__main__":
    config = load_config()
    run_cycle(config)
