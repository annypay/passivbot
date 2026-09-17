"""Live REST/websocket clients inherit a proxy from the environment.

The README documents that passivbot's CCXT clients pick up `HTTP_PROXY` / `HTTPS_PROXY`,
and `utils.load_ccxt_instance` implements it for the research and downloader clients. The
live bot builds its own clients, so this pins the same contract there: an operator behind
a proxy must not have to add a CCXT-only field to `api-keys.json` for the bot to reach the
exchange, and an explicit user setting still wins.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from exchanges.ccxt_bot import CCXTBot  # noqa: E402
from utils import ccxt_should_trust_environment  # noqa: E402

PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def make_bot(**user_fields) -> CCXTBot:
    bot = CCXTBot.__new__(CCXTBot)
    bot.user_info = {"exchange": "binance", "key": "key", "secret": "secret", **user_fields}
    bot.exchange = "binance"
    return bot


def clear_proxy_env(monkeypatch) -> None:
    for name in PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_a_proxy_in_the_environment_is_inherited(monkeypatch):
    clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:10808")
    assert ccxt_should_trust_environment() is True
    assert make_bot()._build_ccxt_config()["aiohttp_trust_env"] is True


def test_lower_case_proxy_variables_count_too(monkeypatch):
    clear_proxy_env(monkeypatch)
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:10808")
    assert make_bot()._build_ccxt_config()["aiohttp_trust_env"] is True


def test_no_proxy_environment_leaves_trust_env_alone(monkeypatch):
    clear_proxy_env(monkeypatch)
    assert ccxt_should_trust_environment() is False
    assert "aiohttp_trust_env" not in make_bot()._build_ccxt_config()


def test_an_explicit_user_setting_wins(monkeypatch):
    clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:10808")
    config = make_bot(aiohttp_trust_env=False)._build_ccxt_config()
    assert config["aiohttp_trust_env"] is False