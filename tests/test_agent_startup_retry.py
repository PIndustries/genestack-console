"""Only an explicitly configured token provisioning file should delay setup."""
import asyncio

import pytest

from agent import main as agent


def test_missing_credentials_without_file_fails_immediately(monkeypatch):
    async def forbidden_sleep(*args):
        raise AssertionError("Unconfigured credentials should not wait for provisioning")

    monkeypatch.setattr(agent.asyncio, "sleep", forbidden_sleep)
    with pytest.raises(agent.ConfigError, match="TOKEN"):
        asyncio.run(agent._load_config_with_retry({"GSC_HUB_URL":"ws://hub.test"}))


def test_configured_token_file_retains_provisioning_retry(monkeypatch):
    calls = []
    expected = agent.AgentConfig(hub_url="ws://hub.test", token="synthetic", name="node")

    def load(env):
        calls.append(1)
        if len(calls) == 1:
            raise agent.ConfigError("TOKEN file not readable")
        return expected

    async def no_wait(*args):
        pass

    monkeypatch.setattr(agent, "load_config", load)
    monkeypatch.setattr(agent.asyncio, "sleep", no_wait)
    result = asyncio.run(agent._load_config_with_retry({"GSC_HUB_URL":"ws://hub.test", "GSC_AGENT_TOKEN_FILE":"/fixture/token"}))
    assert result is expected
    assert len(calls) == 2
