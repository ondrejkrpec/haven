"""Opt-in browser feasibility experiment; not a production fetcher or challenge solver."""

from datetime import datetime, timezone
import importlib.metadata
import json
import os
import re
from pathlib import Path
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ['WISHLIST_BROWSER_WORK']).resolve(strict=True)
if not ROOT.is_absolute() or ROOT.is_relative_to(REPO):
    raise SystemExit('Probe work directory must exist outside the public repository')
sys.path.insert(0, str(REPO))


def sample_tree(root_pid):
    table = {}
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            fields = (p / 'stat').read_text().rsplit(')', 1)[1].split()
            table[int(p.name)] = (int(fields[1]), int(fields[21]) * os.sysconf('SC_PAGE_SIZE'))
        except (OSError, ValueError, IndexError):
            pass
    selected = {root_pid}
    while True:
        more = {pid for pid, (ppid, rss) in table.items() if ppid in selected}
        if more <= selected:
            break
        selected |= more
    pss = 0
    readable = 0
    for pid in selected:
        try:
            line = next(s for s in Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines() if s.startswith('Pss:'))
            pss += int(line.split()[1]) * 1024
            readable += 1
        except (OSError, StopIteration, ValueError):
            pass
    return {'rss': sum(table[p][1] for p in selected if p in table),
            'pss_readable': pss, 'pss_complete': readable == len(selected), 'processes': len(selected)}


def main():
    if len(sys.argv) not in (2, 3) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', sys.argv[1]):
        raise SystemExit('Usage: browser_probe.py run-label [chromium|headed]')
    if len(sys.argv) == 3 and sys.argv[2] not in ('chromium', 'headed'):
        raise SystemExit('Only shell, standard chromium headless and headed modes are supported')
    from playwright.sync_api import sync_playwright
    output = ROOT / sys.argv[1]
    output.mkdir(mode=0o700, exist_ok=False)
    for name in ('home', 'tmp', 'cache'):
        (output / name).mkdir(mode=0o700)
    targets = json.loads(Path(os.environ['WISHLIST_BROWSER_TARGETS']).read_text())
    if not isinstance(targets, list) or len(targets) != 1:
        raise SystemExit('Supply exactly one operator-reviewed target')
    target = targets[0]
    from scripts.wishlist_extract import extract, identity
    identity(target['url'])
    start = time.monotonic()
    samples = []
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            samples.append(sample_tree(os.getpid()))
            stop.wait(0.15)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    mode = sys.argv[2] if len(sys.argv) > 2 else 'headless-shell'
    browser_environment = {'HOME': str(output / 'home'), 'TMPDIR': str(output / 'tmp'),
                           'XDG_CACHE_HOME': str(output / 'cache'), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    if mode == 'headed':
        if not os.environ.get('DISPLAY') or not os.environ.get('XAUTHORITY'):
            raise SystemExit('Headed mode requires an explicit authorized display')
        browser_environment.update({key: os.environ[key] for key in ('DISPLAY', 'XAUTHORITY')})
    result = {'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'playwright': importlib.metadata.version('playwright'),
              'mode': mode, 'sandbox_requested': True, 'fresh_context': True,
              'model_calls': 0, 'target': target['url']}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=mode != 'headed', channel='chromium' if mode in ('chromium', 'headed') else None,
                                         chromium_sandbox=True, timeout=20000,
                                         env=browser_environment)
            result['browser_version'] = browser.version
            context = browser.new_context(java_script_enabled=True, accept_downloads=False)
            result['cookies_before'] = len(context.cookies())
            page = context.new_page()
            page.set_default_timeout(5000)
            navigation = []

            def response_event(response):
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    navigation.append({'status': response.status, 'url': response.url})

            page.on('response', response_event)
            response = page.goto(target['url'], wait_until='domcontentloaded', timeout=30000)
            result['initial_status'] = response.status if response else None
            result['checkpoints'] = []
            began_wait = time.monotonic()
            html = ''
            for seconds in (0, 2, 5, 10, 20):
                delay = seconds - (time.monotonic() - began_wait)
                if delay > 0:
                    page.wait_for_timeout(delay * 1000)
                html = page.content()
                status = navigation[-1]['status'] if navigation else result['initial_status']
                candidate = extract(html, page.url, http_status=status or 0)
                result['checkpoints'].append({'seconds': seconds, 'status': status,
                                              'title': page.title(), 'extraction_status': candidate['status']})
                if candidate['status'] == 'extracted':
                    break
            result['final_url'] = page.url
            result['final_status'] = navigation[-1]['status'] if navigation else result['initial_status']
            result['extraction'] = candidate
            result['browser_features'] = page.evaluate('({javascript:typeof Promise === "function", cookies:navigator.cookieEnabled, user_agent:navigator.userAgent})')
            result['cookies_after'] = len(context.cookies())
            result['cf_clearance_present'] = any(c['name'] == 'cf_clearance' for c in context.cookies())
            visible = page.locator('body').inner_text(timeout=5000)
            result['human_challenge'] = any(m in visible.lower() for m in ('confirm you are human', 'verify you are human', 'ověřte, že jste člověk', 'z masa a kostí'))
            result['navigation'] = navigation
            (output / 'page.html').write_text(html)
            (output / 'visible.txt').write_text(visible[:10000])
            page.screenshot(path=str(output / 'page.png'), timeout=5000)
            context.close()
            browser.close()
            result['result'] = 'completed'
    except Exception as error:
        result.update(result='failed', error_type=type(error).__name__)
        (output / 'error.txt').write_text(str(error))
    finally:
        stop.set()
        thread.join(timeout=2)
        result['seconds'] = round(time.monotonic() - start, 3)
        result['memory'] = {'samples': len(samples),
                            'peak_descendant_rss_bytes': max((s['rss'] for s in samples), default=0),
                            'peak_readable_pss_bytes': max((s['pss_readable'] for s in samples), default=0),
                            'all_pss_samples_complete': all(s['pss_complete'] for s in samples),
                            'peak_process_count': max((s['processes'] for s in samples), default=0)}
        (output / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(json.dumps({k: result.get(k) for k in ('result', 'error_type', 'browser_version', 'initial_status', 'final_status', 'human_challenge', 'cookies_before', 'cookies_after', 'cf_clearance_present', 'seconds', 'memory')}))


if __name__ == '__main__':
    main()
