"""Synthetic connection assessment, run by pinned Hermes's canonical runner.

Requires the exact connector checkout at build/whatsapp-agent-plugin. HTTP is
replaced at the transport; real adapter, client, scoped secrets and state run.
This is not the Haven family authorization layer or a production deployment.
"""

import asyncio
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio


KEYS = ("synthetic-haven-agent-a", "synthetic-haven-agent-b")
MEMBERS = ("user:synthetic-member-a", "user:synthetic-member-b")


@contextmanager
def member_scope(home, key):
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_token = set_hermes_home_override(home)
    secret_token = set_secret_scope({
        "WHATSAPP_AGENT_PLATFORM_API_KEY": key,
        "WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS": "",
        "WHATSAPP_AGENT_PLATFORM_ALLOW_ALL_USERS": "false",
    }, profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


@pytest_asyncio.fixture
async def connections(tmp_path, monkeypatch):
    plugin_root = Path(__file__).resolve().parents[2] / "build/whatsapp-agent-plugin"
    monkeypatch.syspath_prepend(str(plugin_root / "tests"))
    import wap_helpers as helpers
    import agent.secret_scope as secrets
    from gateway.config import PlatformConfig
    from gateway.platform_registry import PlatformEntry, platform_registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "unused-home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setattr(secrets, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setattr(secrets, "_AUTO_PINNED_HOME", None)
    platform_registry.register(PlatformEntry(
        name="whatsapp_agent_platform", label="Synthetic Agent Platform",
        adapter_factory=lambda cfg: None, check_fn=lambda: True,
    ))
    plugin = helpers.load_plugin()
    fakes = [helpers.FakeMeta(), helpers.FakeMeta()]
    http_clients = []

    def client(key, **kwargs):
        fake = fakes[KEYS.index(key)]
        http = fake.http_client()
        http_clients.append(http)
        real = plugin.client.AgentPlatformClient(key, http=http, **kwargs)
        real.limits = {name: plugin.client.RateWindow(10**6) for name in real.limits}
        return real

    monkeypatch.setattr(plugin.adapter, "AgentPlatformClient", client)
    homes = [tmp_path / "member-a", tmp_path / "member-b"]
    adapters = []
    for home, key in zip(homes, KEYS):
        home.mkdir(mode=0o700)
        with member_scope(home, key):
            adapter = plugin.adapter.WhatsAppAgentPlatformAdapter(
                PlatformConfig(enabled=True, extra={"media_enabled": False})
            )
        adapter._poll_min_interval = 0.01
        adapter._poll_timeout = 0
        adapter.handle_message = AsyncMock()
        adapters.append(adapter)
    try:
        yield helpers, plugin, homes, adapters, fakes
    finally:
        await asyncio.gather(*(adapter.disconnect() for adapter in adapters))
        await asyncio.gather(*(http.aclose() for http in http_clients))


async def connect_all(homes, adapters):
    for home, key, adapter in zip(homes, KEYS, adapters):
        with member_scope(home, key):
            assert await adapter.connect()


@pytest.mark.asyncio
async def test_two_connections_keep_credentials_recipients_and_restart_state_separate(connections):
    helpers, plugin, homes, adapters, fakes = connections
    await connect_all(homes, adapters)
    for index, (fake, member) in enumerate(zip(fakes, MEMBERS)):
        fake.queue("updates", helpers.updates_response(
            helpers.text_message("wamid.equal-across-connections", f"private-{index}", sender=member),
            next_offset=11 + index, contacts=[],
        ))
    await helpers.until(lambda: all(a.handle_message.await_count == 1 for a in adapters))
    for index, (home, key, adapter, member) in enumerate(zip(homes, KEYS, adapters, MEMBERS)):
        event = adapter.handle_message.await_args.args[0]
        assert (event.text, event.source.chat_id) == (f"private-{index}", member)
        state = plugin.state.PollState.load(plugin.state.state_path(home, key))
        assert state.creator == member and state.next_offset == 11 + index

    # The caller's active profile must not replace an already-bound connection's key.
    for index, adapter in enumerate(adapters):
        other = 1 - index
        with member_scope(homes[other], KEYS[other]):
            assert (await adapter.send("self", f"reply-{index}")).success
    for index, (fake, key, member) in enumerate(zip(fakes, KEYS, MEMBERS)):
        assert [(body["to"], body["text"]["body"]) for body in fake.bodies("messages")] == [
            (member, f"reply-{index}")
        ]
        assert all(r.headers["Authorization"] == f"Bearer {key}" for r in fake.requests)

    await adapters[0].disconnect()
    with member_scope(homes[0], KEYS[0]):
        assert await adapters[0].connect()
    # Replaying an accepted message must not replay its private conversation handoff.
    fakes[0].queue("updates", helpers.updates_response(
        helpers.text_message("wamid.equal-across-connections", "private-0", sender=MEMBERS[0]),
        helpers.text_message("wamid.after-restart", "new-private-a", sender=MEMBERS[0]),
        next_offset=21, contacts=[],
    ))
    fakes[1].queue("updates", helpers.updates_response(
        helpers.text_message("wamid.after-restart", "new-private-b", sender=MEMBERS[1]),
        next_offset=22, contacts=[],
    ))
    await helpers.until(lambda: all(a.handle_message.await_count == 2 for a in adapters))
    for index, (adapter, fake) in enumerate(zip(adapters, fakes)):
        assert [call.args[0].text for call in adapter.handle_message.await_args_list] == [
            f"private-{index}", f"new-private-{'ab'[index]}"
        ]
        if index == 0:
            assert any(r.url.params.get("offset") == "11" for r in fake.calls("updates"))


@pytest.mark.asyncio
async def test_member_of_other_connection_is_not_implicitly_authorized(connections):
    helpers, plugin, homes, adapters, fakes = connections
    await connect_all(homes, adapters)
    for index, fake in enumerate(fakes):
        fake.not_creator_wamids.add("wamid.foreign")
        fake.queue("updates", helpers.updates_response(
            helpers.text_message("wamid.foreign", "foreign-private", sender=MEMBERS[1 - index]),
            helpers.text_message("wamid.owner", f"owner-{index}", sender=MEMBERS[index]),
            next_offset=31, contacts=[],
        ))
    await helpers.until(lambda: all(
        plugin.state.PollState.load(plugin.state.state_path(home, key)).next_offset == 31
        for home, key in zip(homes, KEYS)
    ))
    for index, adapter in enumerate(adapters):
        assert [call.args[0].text for call in adapter.handle_message.await_args_list] == [f"owner-{index}"]


@pytest.mark.asyncio
async def test_same_key_cannot_poll_twice_even_with_different_home(connections):
    _helpers, _plugin, homes, adapters, _fakes = connections
    with member_scope(homes[0], KEYS[0]):
        assert await adapters[0].connect()
    with member_scope(homes[1], KEYS[0]):
        assert not await adapters[1].connect()
    assert adapters[1].fatal_error_code == "whatsapp_agent_platform_lock"
    await adapters[0].disconnect()
    with member_scope(homes[1], KEYS[0]):
        assert await adapters[1].connect()
