"""W2 integration against exact Hermes and PTB; fake Bot.send_message, real local sockets."""

from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap

ROOT = Path(__file__).resolve().parents[2]


def run_case(tmp_path, body):
    with tempfile.TemporaryDirectory(prefix="hw2-native-") as temporary:
        program = f"""
import os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
root = Path({str(ROOT)!r})
home = Path({str(temporary)!r}) / 'hermes'
home.mkdir(mode=0o700)
os.environ['HERMES_HOME'] = str(home)
os.environ['HERMES_RUNTIME_DIR'] = str(home / 'runtime')
from scripts.capability_policy import Audit, load_registry
from agent.deterministic_policy import arm_deterministic_policy
arm_deterministic_policy(Audit(home, load_registry()[0]).guard_refusal)
""" + textwrap.dedent(body)
        result = subprocess.run([sys.executable, "-B", "-c", program], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_plugin_and_real_telegram_event_commit_before_plain_text_send(tmp_path):
    run_case(tmp_path, '''
import asyncio, shutil, sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import telegram
from telegram import Bot, Message, User
from ruamel.yaml import YAML
from scripts.model_contract import service_config
from scripts.wishlist_domain import Wishlist
from scripts.wishlist_ingress import check_runtime
from scripts.wishlist_service import Server

assert telegram.__version__ == '22.8'
check_runtime(Path.cwd())
work = home.parent / 'wishlist'
work.mkdir(mode=0o700)
settings = {'socket': str(work / 'ingress.sock'), 'service_uid': os.getuid(), 'bot_id': '200001'}
plugin = home / 'plugins/haven-capabilities'
plugin.mkdir(parents=True)
for name in ('plugin.yaml', '__init__.py'):
    shutil.copyfile(root / 'plugins/haven-capabilities' / name, plugin / name)
config = service_config({'model': {'provider': 'openai-codex', 'name': 'gpt-synthetic'},
    'host': {'service_user': 'haven'}, 'instance': {'timezone': 'Europe/Prague'},
    'whatsapp': {'enabled': False}, 'telegram': {'enabled': True, 'owner_id': '100001'}},
    capabilities=True, wishlist=settings)
with (home / 'config.yaml').open('w') as stream:
    YAML().dump(config, stream)
from hermes_cli.plugins import discover_plugins
discover_plugins()
from hermes_cli.lifecycle import ainvoke_hook, has_hook
assert has_hook('pre_gateway_dispatch') and has_hook('post_gateway_admission')
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.platforms.event import MessageType

bot = Bot('200001:SYNTHETIC_NO_NETWORK_TOKEN')
bot._bot_user = User(200001, 'Synthetic bot', True)
adapter = TelegramAdapter(PlatformConfig(enabled=True, extra={}))
adapter._bot = bot
runner = object.__new__(GatewayRunner)
runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True,
    extra=config['platforms']['telegram']['extra'])})
runner.adapters = {Platform.TELEGRAM: adapter}
runner.pairing_store = MagicMock()
runner.session_store = MagicMock()
runner._is_user_authorized_for_source = lambda source: source.user_id == source.chat_id == '100001'
runner._admit_bot_message_for_source = lambda source: not source.is_bot
runner._delivery_adapter_for = lambda source: adapter
runner._intake_adapter_for = lambda source: adapter
runner._rescue_orphaned_overflow = lambda key, target: None
runner._enqueue_fifo = MagicMock()
runner._handle_message_with_agent = AsyncMock(side_effect=AssertionError('ordinary agent path'))
runner._run_post_turn_hooks = AsyncMock()
domain = Wishlist(work / 'wishlist.sqlite3', bot_id='200001', owner_id='100001')

def make_event(text, message_id):
    message = Message.de_json({'message_id': message_id, 'date': 1794052800,
        'chat': {'id': 100001, 'type': 'private'},
        'from': {'id': 100001, 'is_bot': False, 'first_name': 'Synthetic'}, 'text': text}, bot)
    return adapter._build_message_event(message, MessageType.TEXT, update_id=message_id)

async def main():
    server = Server(domain, work / 'ingress.sock', ingress_uid=os.getuid())
    await server.start()
    replies = []
    async def send(**kwargs):
        assert kwargs['chat_id'] == 100001 and kwargs['parse_mode'] is None
        assert kwargs['disable_web_page_preview'] is True
        with closing(sqlite3.connect(work / 'wishlist.sqlite3')) as db:
            assert db.execute('SELECT COUNT(*) FROM telegram_receipts').fetchone()[0] >= 1
        replies.append(kwargs['text'])
    async def consume(text, message_id):
        event = make_event(text, message_id)
        return True, await runner._handle_message(event)
    try:
        with patch.object(Bot, 'send_message', side_effect=send):
            assert await consume('https://shop.example/item', 1) == (True, None)
            assert 'Uloženo' in replies[-1]
            token = domain.store.db.execute('SELECT token FROM clarifications').fetchone()[0]
            assert await consume('wl dopln ' + token + ' pro mě jednorázově', 2) == (True, None)
            assert domain.store.db.execute('SELECT purpose FROM entries').fetchone()[0] == 'self'
            assert await consume('wl upravit #1 {"label":"[odkaz](tg://user?id=1) <b>text</b>"}', 3) == (True, None)
            assert '[odkaz](tg://user?id=1) <b>text</b>' in replies[-1]
            original = replies[-1]
            assert await consume('wl upravit #1 {"label":"[odkaz](tg://user?id=1) <b>text</b>"}', 3) == (True, None)
            assert replies[-1] == original
            assert domain.store.db.execute('SELECT version FROM entries').fetchone()[0] == 3
            handled, capabilities = await consume('Co umíš?', 4)
            assert handled and 'wl pomoc' in capabilities and 'Jaký je stav systému?' in capabilities
            assert await consume('wl koupeno #1', 5) == (True, None)
            assert domain.store.db.execute('SELECT state FROM entries').fetchone()[0] == 'purchased'
            runner._handle_message_with_agent.assert_not_awaited()
    finally:
        await server.close()
        domain.close()
asyncio.run(main())
''')


def test_missing_capture_and_failed_wishlist_cannot_open_native_model(tmp_path):
    run_case(tmp_path, '''
import asyncio
from scripts.wishlist_ingress import Bridge
from run_agent import AIAgent
bridge = Bridge('100001', {'socket': str(home / 'missing.sock'),
                         'service_uid': os.getuid(), 'bot_id': '200001'})
reply = asyncio.run(bridge.consume(session_key='synthetic', platform='telegram',
    source={'platform': 'telegram', 'chat_type': 'dm', 'user_id': '100001', 'chat_id': '100001'},
    message_id='1', text='wl seznam'))
assert reply['action'] == 'handled' and reply['reply']
try:
    AIAgent(model='gpt-synthetic', api_key='synthetic', enabled_toolsets=[])
except PermissionError:
    pass
else:
    raise AssertionError('wishlist failure opened native model')
''')
