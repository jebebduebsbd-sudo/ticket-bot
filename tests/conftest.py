"""Test setup: import bot.py safely.

bot.py requires DISCORD_TOKEN / DATABASE_URL / GUILD_ID at import time and calls
bot.run() only under `if __name__ == "__main__"`, so with dummy env vars set here
the module imports cleanly (no network, no bot start) and its pure helpers can be
unit-tested. No real credentials are used.
"""
import importlib.util
import os
import sys

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/test")
os.environ.setdefault("GUILD_ID", "1")
# Pin the sourcing markups to their documented defaults so assertions are stable.
os.environ.setdefault("RESALE_MULTIPLIER", "2.5")
os.environ.setdefault("HIGH_BUDGET_EUR", "30")
os.environ.setdefault("HIGH_BUDGET_MULTIPLIER", "3")


@pytest.fixture(scope="session")
def bot():
    """Import bot.py once and hand the module to tests."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "bot.py")
    spec = importlib.util.spec_from_file_location("bot", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bot"] = module
    spec.loader.exec_module(module)
    return module
