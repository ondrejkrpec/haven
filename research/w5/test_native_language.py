"""Exact native no-tools interpretation with a synthetic SDK boundary; no live account."""

from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap

ROOT = Path(__file__).resolve().parents[2]


def run_case(body):
    with tempfile.TemporaryDirectory(prefix="hw5-native-") as temporary:
        program = f'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
root = Path({str(ROOT)!r})
home = Path({temporary!r}) / 'hermes'
home.mkdir(mode=0o700)
os.environ['HERMES_HOME'] = str(home)
os.environ['HERMES_RUNTIME_DIR'] = str(home / 'tools')
from ruamel.yaml import YAML
from scripts.model_contract import native_config, CODEX_BASE_URL, CODEX_PROVIDER
config = native_config({{'model': {{'provider': CODEX_PROVIDER, 'name': 'gpt-synthetic'}},
    'instance': {{'timezone': 'Europe/Prague'}}, 'host': {{'service_user': 'haven'}},
    'whatsapp': {{'enabled': False}}}})
with (home / 'config.yaml').open('w') as stream:
    YAML().dump(config, stream)
(home / 'config.yaml').chmod(0o600)
(home / 'auth.json').write_text('{{}}')
(home / 'auth.json').chmod(0o600)
from types import SimpleNamespace as NS
from unittest.mock import patch
from scripts.wishlist_model import native_request
prepared = {{'request_id': 'a'*64, 'attempt_id': 'b'*32, 'text': 'Ukaž můj wishlist',
    'now': '2026-11-07T12:00:00Z', 'timezone': 'Europe/Prague', 'entries': [], 'context': {{}}}}
credentials = {{'provider': CODEX_PROVIDER, 'base_url': CODEX_BASE_URL, 'api_key': 'synthetic'}}
''' + textwrap.dedent(body)
        result = subprocess.run([sys.executable, '-B', '-c', program], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_codex_adapter_sends_one_bounded_no_tools_request_and_guard_stays_armed():
    run_case('''
calls = []
class Client:
    api_key = 'synthetic'
    base_url = CODEX_BASE_URL
    def __init__(self):
        self.responses = self
        self.closed = False
    def create(self, **kwargs):
        kwargs = {**kwargs, **kwargs.get('extra_body', {})}
        calls.append(kwargs)
        assert kwargs['stream'] is True and kwargs['store'] is False
        assert not kwargs.get('tools')
        assert kwargs['timeout'] == 10
        assert 'wishlist.record_purchase' in kwargs['instructions']
        assert 'entryVersion' in kwargs['instructions']
        assert len(kwargs['input']) == 1
        decision = json.dumps({'kind':'command', 'command':{'operation':'wishlist.list', 'arguments':{}}})
        return NS(status='completed', output=[NS(type='message', role='assistant',
            content=[NS(type='output_text', text=decision)])], usage=NS(input_tokens=100, output_tokens=20, total_tokens=120))
    def close(self):
        self.closed = True
client = Client()
def factory(**kwargs):
    assert kwargs == {'api_key':'synthetic', 'base_url':CODEX_BASE_URL, 'max_retries':0, 'timeout':10}
    return client
with patch('hermes_cli.auth_codex.resolve_codex_runtime_credentials', return_value=credentials), \
     patch('agent.auxiliary_client._create_openai_client', side_effect=factory):
    result = native_request(prepared, source=Path.cwd(), home=home)
assert result['code'] == 'ok' and len(calls) == 1 and client.closed
assert result['decision']['command']['arguments'] == {'limit':10}
assert result['usage'] == {'input_tokens':100,'output_tokens':20}
from run_agent import AIAgent
try:
    AIAgent(model='gpt-synthetic', api_key='synthetic', enabled_toolsets=[])
except PermissionError:
    pass
else:
    raise AssertionError('ordinary agent escaped interpretation guard')
''')


def test_native_provider_failure_has_no_retry_or_fallback():
    run_case('''
calls = []
class Client:
    api_key = 'synthetic'
    base_url = CODEX_BASE_URL
    def __init__(self):
        self.responses = self
        self.closed = False
    def create(self, **kwargs):
        calls.append(kwargs)
        raise TimeoutError('synthetic provider unavailable')
    def close(self):
        self.closed = True
client = Client()
with patch('hermes_cli.auth_codex.resolve_codex_runtime_credentials', return_value=credentials), \
     patch('agent.auxiliary_client._create_openai_client', return_value=client):
    try:
        native_request(prepared, source=Path.cwd(), home=home)
    except TimeoutError:
        pass
    else:
        raise AssertionError('provider failure accepted')
assert len(calls) == 1 and client.closed
''')


def test_profile_expansion_is_denied_before_credentials_or_provider():
    run_case('''
config['mcp_servers'] = {'unwanted': {'command':'synthetic'}}
with (home / 'config.yaml').open('w') as stream:
    YAML().dump(config, stream)
with patch('hermes_cli.auth_codex.resolve_codex_runtime_credentials', side_effect=AssertionError('credential resolution')), \
     patch('agent.auxiliary_client._create_openai_client', side_effect=AssertionError('provider access')):
    try:
        native_request(prepared, source=Path.cwd(), home=home)
    except ValueError as exc:
        assert str(exc) == 'language_profile_mismatch'
    else:
        raise AssertionError('expanded profile accepted')
''')


def test_natural_message_uses_real_admission_and_literal_reply_without_opening_coordinator_model(tmp_path):
    sys.path.insert(0, str(ROOT))
    from research.w2.test_native_wishlist import run_case as guarded_case
    guarded_case(tmp_path, '''
import asyncio, shutil, sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Bot, Message, User
from ruamel.yaml import YAML
from scripts.model_contract import service_config
from scripts.wishlist_domain import Wishlist
from scripts.wishlist_service import Server
def command(operation, **arguments):
    return {'kind':'command','command':{'operation':'wishlist.'+operation,'arguments':arguments}}
class Interpreter:
    def __init__(self, domain):
        self.domain, self.requests, self.decision = domain, [], None
    async def interpret(self, prepared):
        assert not self.domain.store.db.in_transaction
        self.requests.append(prepared)
        if isinstance(self.decision, Exception):
            raise self.decision
        return {'request_id':prepared['request_id'],'attempt_id':prepared['attempt_id'],
                'code':'ok','decision':self.decision,'usage':None}
work = home.parent / 'wishlist'
work.mkdir(mode=0o700)
settings = {'socket': str(work / 'ingress.sock'), 'service_uid': os.getuid(),
            'bot_id': '200001', 'natural_language': True}
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
backend = Interpreter(domain)
backend.decision = command('add', url='https://shop.example/item',
    details={'label':'Pánev','purpose':'gift','purchase_mode':'once','recipient':'máma'})
def make_event(text, number):
    message = Message.de_json({'message_id':number, 'date':1794052800,
        'chat':{'id':100001,'type':'private'},
        'from':{'id':100001,'is_bot':False,'first_name':'Synthetic'},'text':text}, bot)
    return adapter._build_message_event(message, MessageType.TEXT, update_id=number)
async def main():
    server = Server(domain, work / 'ingress.sock', ingress_uid=os.getuid(), language=backend)
    await server.start()
    replies=[]
    async def send(**kwargs):
        assert kwargs['parse_mode'] is None and kwargs['disable_web_page_preview'] is True
        with closing(sqlite3.connect(work / 'wishlist.sqlite3')) as db:
            assert db.execute('SELECT COUNT(*) FROM telegram_receipts').fetchone()[0] >= 1
        replies.append(kwargs['text'])
    try:
        with patch.object(Bot, 'send_message', side_effect=send):
            text='Přidej mi pánev jako dárek pro mámu https://shop.example/item'
            assert await runner._handle_message(make_event(text,1)) is None
            assert 'Přidala' in replies[-1] and 'wl ' not in replies[-1]
            assert len(backend.requests) == 1
            assert await runner._handle_message(make_event(text,1)) is None
            assert len(backend.requests) == 1
            capabilities = await runner._handle_message(make_event('Co umíš?',2))
            assert 'wishlist' in capabilities and len(backend.requests) == 1
            backend.decision=TimeoutError('synthetic')
            assert await runner._handle_message(make_event('Ukaž wishlist',3)) is None
            assert 'neprovedu' in replies[-1]
            assert await runner._handle_message(make_event('https://shop.example/other',4)) is None
            assert domain.store.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0] == 2
            runner._handle_message_with_agent.assert_not_awaited()
    finally:
        await server.close()
        domain.close()
asyncio.run(main())
''')
