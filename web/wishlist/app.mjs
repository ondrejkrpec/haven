import {CATEGORIES, money, dayKey, priceStats, comparison, remainingDays, visibleEntries,
  historyCoverage, RECENT_DAYS, signals, validURL,
  createEntry, updateEntry, recordPurchase, correctPurchase, voidPurchase,
  removeEntry, restoreEntry, resumeNotifications, inputMoney, parseMoney} from './model.mjs';
const live = document.querySelector('meta[name="haven-mode"]')?.content === 'production';
const api = live ? await import('./api.mjs') : null;
const {demoState} = live ? {demoState: null} : await import('./demo.mjs');

const $ = selector => document.querySelector(selector);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
  plus:'M12 5v14M5 12h14', close:'m6 6 12 12M6 18 18 6', search:'M21 21l-4.4-4.4M19 10.5a8.5 8.5 0 1 1-17 0 8.5 8.5 0 0 1 17 0Z',
  lock:'M6 10h12v11H6zM8 10V6a4 4 0 0 1 8 0v4',
  package:'m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Zm0 9v9m-8-13.5 8 4.5 8-4.5M8 5.25l8 4.5',
  check:'m5 12 4 4L19 6', external:'M14 3h7v7M21 3 11 13M10 3H3v18h18v-7',
  sliders:'M4 7h16M4 17h16M8 4v6m8 4v6', bag:'M5 8h14l1 12H4L5 8Zm4 0V6a3 3 0 0 1 6 0v2',
  repeat:'m17 2 4 4-4 4M3 11V8a2 2 0 0 1 2-2h16M7 22l-4-4 4-4M21 13v3a2 2 0 0 1-2 2H3',
  muted:'M18 8a6 6 0 0 0-10-4M6 6a6 6 0 0 0 0 2c0 7-3 7-3 9h14M10 21h4M3 3l18 18',
  info:'M12 11v6M12 7h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z',
  edit:'m16 3 5 5-12 12-6 1 1-6Z', trash:'M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7',
  refresh:'M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 14 6M4 12a8 8 0 0 0 14 6', restore:'M3 3v6h6M3 9a9 9 0 1 1 1 9',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.info}"/></svg>`;
function icons(root = document) { root.querySelectorAll('[data-icon]').forEach(node => node.replaceWith(document.createRange().createContextualFragment(icon(node.dataset.icon)))); }
icons();
const dateFormat = new Intl.DateTimeFormat('cs-CZ', {timeZone:'Europe/Prague', day:'numeric', month:'numeric', year:'numeric'});
const date = value => value ? dateFormat.format(new Date(value)) : 'Zatím neověřeno';
const numberFormat = new Intl.NumberFormat('cs-CZ');
const selectTrigger = typeof HTMLSelectedContentElement === 'function' && CSS.supports('appearance', 'base-select')
  ? '<button type="button"><selectedcontent></selectedcontent></button>' : '';
for (const field of document.querySelectorAll('select')) field.insertAdjacentHTML('afterbegin', selectTrigger);
const shopName = url => new URL(url).hostname.replace(/^www\./, '');
const imagePath = /^\/wishlist\/api\/image\/[ip]\/[a-f0-9]{32}$/;
function thumbnail(source, {large = false, lazy = false} = {}) {
  // Only authenticated same-origin raster responses can replace the placeholder.
  const image = imagePath.test(source)
    ? `<img src="${source}" alt="" width="160" height="160"${lazy ? ' loading="lazy"' : ''} decoding="async">` : '';
  return `<span class="thumbnail${large ? ' thumbnail-large' : ''}" aria-hidden="true">${icon('package')}${image}</span>`;
}
document.addEventListener('error', event => {
  if (event.target instanceof HTMLImageElement && event.target.closest('.thumbnail')) event.target.remove();
}, true);
const stock = value => ({in_stock:'Skladem',out_of_stock:'Nedostupné',unknown:'Neověřená dostupnost',preorder:'Předobjednávka',backorder:'Na objednávku',limited:'Omezená dostupnost',discontinued:'Prodej ukončen'}[value] || 'Neověřená dostupnost');
const storageKey = 'haven.wishlist.preview.v1';
let state, lastSaved = null;
if (live) state = {version:1,entries:[]};
else try {
  const saved = localStorage.getItem(storageKey);
  lastSaved = saved;
  state = saved ? JSON.parse(saved) : demoState();
  if (state.version !== 1 || !Array.isArray(state.entries) || state.entries.length > 2000 || state.entries.some(entry =>
    typeof entry.name !== 'string' || !CATEGORIES.includes(entry.category) || !Array.isArray(entry.observations)
    || !Array.isArray(entry.purchases) || !/^https:\/\//.test(entry.url))) throw new Error('Invalid state');
} catch {
  state = demoState();
  $('#storage-error').hidden = false;
  $('#storage-error').textContent = 'Uloženou ukázku se nepodařilo načíst. Zobrazuji výchozí ukázková data.';
}
let selected = null, range = 90, formAction = null, formEntry = null, formPurchase = null, undoAction = null;
let toastTimer, loading = live, loadError = null, detailGeneration = 0, formRequestId = null, formVersion = null;
let eventFilter = '', detailError = null, detailReturn = null, editorReturn = null, formBusy = false, listGeneration = 0;
let editorInitialValues;
let previewTimer, previewRequest, suggestedName = '', suggestedCategory = '';
const pendingActions = new Map(), refreshing = new Set();
let toastReturn, highlightedId, highlightTimer;
const rendered = new WeakMap();
const failedCheck = entry => ['failed','blocked','stale'].includes(entry.collection);

function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

function focusSelector(element) {
  if (!element) return null;
  if (element.id) return '#' + CSS.escape(element.id);
  return ['action', 'id', 'purchase', 'range', 'event', 'close', 'shop']
    .filter(key => element.dataset[key] !== undefined)
    .map(key => `[data-${key}="${CSS.escape(element.dataset[key])}"]`).join('') || null;
}

function restoreFocus(selector, fallback, root = document) {
  const replacement = selector && root.querySelector(selector);
  const target = replacement && !replacement.disabled && !replacement.closest('[hidden]') ? replacement : $(fallback);
  target?.focus({preventScroll:true});
}

function renderMarkup(container, markup, fallback) {
  if (rendered.get(container) === markup) return;
  const active = document.activeElement;
  const hadFocus = container.contains(active);
  const selector = hadFocus ? focusSelector(active) : null;
  const open = [...container.querySelectorAll('details[open][id]')].map(detail => detail.id);
  container.innerHTML = markup;
  rendered.set(container, markup);
  for (const id of open) {
    const detail = container.querySelector('#' + CSS.escape(id));
    if (detail) detail.open = true;
  }
  if (hadFocus) restoreFocus(selector, fallback, container);
}

function dismissToast() {
  clearTimeout(toastTimer);
  const toast = $('#toast');
  const hadFocus = toast.contains(document.activeElement);
  toast.hidden = true;
  undoAction = null;
  if (hadFocus) restoreFocus(toastReturn, $('#detail').open ? '#detail-close' : '#list-title');
}

function toast(message, undo = null) {
  const notice = $('#toast');
  const host = [...document.querySelectorAll('dialog[open]')].at(-1) || document.body;
  if (notice.parentElement !== host) host.append(notice, $('#toast-status'));
  if (!notice.contains(document.activeElement)) toastReturn = focusSelector(document.activeElement);
  clearTimeout(toastTimer);
  undoAction = undo;
  $('#toast-message').textContent = message;
  $('#toast-undo').hidden = !undo;
  notice.hidden = false;
  setText($('#toast-status'), message);
  if (!undo) toastTimer = setTimeout(() => {
    if (!notice.contains(document.activeElement) && !notice.matches(':hover')) dismissToast();
  }, 8000);
}
function commit(change) {
  if (live) throw new Error("Produkční změny musí potvrdit server.");
  const next = structuredClone(state);
  change(next);
  let current;
  try { current = localStorage.getItem(storageKey); } catch { current = lastSaved; }
  if (current !== lastSaved) throw new Error('Ukázka se změnila v jiné kartě. Nejdřív obnov stránku, aby se její změny nepřepsaly.');
  try {
    const serialized = JSON.stringify(next);
    localStorage.setItem(storageKey, serialized);
    lastSaved = serialized;
    $('#storage-error').hidden = true;
  } catch {
    $('#storage-error').hidden = false;
    $('#storage-error').textContent = 'Prohlížeč nedovolil změnu uložit. V této kartě zůstává, po obnovení stránky se může ztratit.';
  }
  state = next; render();
  if (selected && $('#detail').open) renderDetail();
}
const entryById = id => state.entries.find(entry => entry.id === id);
function filters() {
  return {search:$('#search').value,category:$('#filter-category').value,shop:$('#filter-shop').value,
    stock:$('#filter-stock').value,mode:$('#filter-mode').value,state:$('#filter-state').value,
    sort:$('#sort').value,event:eventFilter};
}
function resetFilters() {
  $('#search').value = '';
  for (const key of ['category','shop','stock','mode']) $(`#filter-${key}`).value = '';
  $('#filter-state').value = 'default';
  eventFilter = '';
  render();
  $('#search').focus();
}
$('#filter-category').insertAdjacentHTML('beforeend', CATEGORIES.map(c => `<option>${escape(c)}</option>`).join(''));
const stockValues = ['in_stock','out_of_stock','unknown','preorder','backorder','limited','discontinued'];
$('#filter-stock').insertAdjacentHTML('beforeend', stockValues.map(value => `<option value="${value}">${stock(value)}</option>`).join(''));

const events = {drop:'Zlevnění', rise:'Zdražení', stock:'Změny dostupnosti'};
function renderActivity(entries, measured) {
  const counts = {drop:0,rise:0,stock:0};
  let comparable = false;
  for (const entry of entries) {
    const {change, stockChange} = measured.get(entry.id);
    comparable ||= Boolean(change);
    if (change?.recent) counts[change.delta < 0 ? 'drop' : 'rise']++;
    if (stockChange) counts.stock++;
  }
  const buttons = Object.keys(events).filter(key => counts[key] || eventFilter === key);
  $('#activity').hidden = loading || Boolean(loadError) || !entries.length && !eventFilter;
  setText($('#activity-label'), buttons.length ? `Za posledních ${RECENT_DAYS} dní` :
    comparable ? `Žádné zjištěné změny za posledních ${RECENT_DAYS} dní` : 'Zatím bez srovnatelných cenových měření');
  renderMarkup($('#activity-controls'), buttons.map(key => `<button type="button" class="activity-button"
    data-event="${key}" aria-pressed="${eventFilter === key}">${events[key]} · ${counts[key]}</button>`).join(''), '#search');
}

function render() {
  const selectedShop = $('#filter-shop').value;
  const shops = [...new Set(state.entries.map(entry => new URL(entry.url).hostname))].sort();
  const shopOptions = '<option value="">Všechny</option>' + shops.map(s => `<option>${escape(s)}</option>`).join('');
  if (rendered.get($('#filter-shop')) !== shopOptions) {
    $('#filter-shop').innerHTML = selectTrigger + shopOptions;
    rendered.set($('#filter-shop'), shopOptions);
    $('#filter-shop').value = shops.includes(selectedShop) ? selectedShop : '';
  }
  const f = filters();
  const now = new Date().toISOString();
  const measured = new Map(state.entries.map(entry => [entry.id, signals(entry, now)]));
  const all = visibleEntries(state.entries, {...f,event:''}, now, measured);
  const entries = f.event ? visibleEntries(all, f, now, measured) : all;
  renderActivity(all, measured);
  setText($('#visible-count'), String(entries.length));
  $('#visible-count').hidden = loading || Boolean(loadError);
  const advanced = [f.shop, f.stock, f.mode, f.state !== 'default'].filter(Boolean).length;
  setText($('#more-count'), String(advanced));
  $('#more-count').hidden = advanced === 0;
  const filtered = Boolean(f.search || f.category || advanced || f.event);
  $('#reset-filters').hidden = !filtered;
  const titles = {default:'V hledáčku', purchased:'Koupené položky', removed:'Odstraněné položky', all:'Všechny položky'};
  setText($('#list-label'), titles[f.state]);
  const groups = new Map();
  for (const [index, entry] of entries.entries()) {
    if (!groups.has(entry.category)) groups.set(entry.category, []);
    groups.get(entry.category).push(row(entry, measured.get(entry.id), index >= 5));
  }
  renderMarkup($('#rows'), [...groups].map(([category, rows]) => `<section class="product-group"><h3>${escape(category)}</h3>
    <ul class="product-list" role="list">${rows.join('')}</ul></section>`).join(''), '#list-title');
  const title = loading ? 'Načítám wishlist…' : loadError ? 'Přehled se nepodařilo načíst' :
    entries.length ? '' : filtered ? 'Žádné odpovídající položky' : 'Wishlist je zatím prázdný';
  const message = loading ? '' : loadError || (filtered ? 'Uprav hledání nebo filtry.' : 'Přidej odkaz na první produkt.');
  $('#empty').hidden = !title;
  $('#empty-message').hidden = !message;
  setText($('#empty-title'), title);
  setText($('#empty-message'), title ? message : '');
  $('#empty-action').hidden = loading || !title;
  setText($('#empty-action'), loadError ? 'Zkusit znovu' : filtered ? 'Zrušit filtry' : 'Přidat položku');
  $('#empty-action').onclick = loadError ? reloadList : filtered ? resetFilters : () => openEditor('add');
  setText($('#list-status'), title || `Počet zobrazených položek: ${entries.length}`);
}

function priceChange(change) {
  if (!change) return '';
  if (!change.recent) return '';
  const down = change.delta < 0;
  return `<span class="price-change ${down ? 'drop' : 'rise'}">
    ${down ? '↓ Zlevnilo' : '↑ Zdražilo'} o ${money(Math.abs(change.delta))} · ${date(change.at)}</span>`;
}

function availability(entry, change) {
  const text = change && entry.availability === 'in_stock' && change.previous === 'out_of_stock'
    ? 'Znovu skladem' : stock(entry.availability);
  return `<span class="stock ${escape(entry.availability)}${change ? ' stock-event' : ''}">${failedCheck(entry) && entry.checkedAt ? 'Poslední stav · ' : ''}${text}</span>
    ${change ? `<span class="subtext">Změna ${date(change.at)}</span>` : ''}`;
}

function row(entry, measured, lazy) {
  const {current, change, stockChange} = measured;
  const id = escape(entry.id), name = escape(entry.name), days = remainingDays(entry);
  const type = entry.removedAt ? 'restore' : entry.state === 'active' ? 'purchase' : null;
  const actionLabel = type === 'restore' ? 'Obnovit' : 'Zapsat nákup';
  const latest = entry.observations.at(-1);
  const price = current ?? latest?.cents;
  const knownOnly = price != null && (current == null || failedCheck(entry) || entry.collection === 'pending');
  const status = knownOnly ? 'Poslední známá' :
    entry.collection === 'pending' ? 'Čeká na kontrolu' :
    entry.collection === 'unsupported' ? 'Obchod bez sledování' : price == null ? 'Cena není k dispozici' : '';
  return `<li class="product-row${entry.id === highlightedId ? ' is-new' : ''}">
    ${thumbnail(entry.image, {lazy})}
    <button type="button" class="item-title" data-action="detail" data-id="${id}">${name}</button>
    <div class="product-price">
      ${price == null ? '' : `<span class="price${knownOnly ? ' is-stale' : ''}">${money(price)}</span>`}
      ${status ? `<span class="subtext">${status}</span>` : ''}
    </div>
    <div class="product-signals">${change?.recent ? priceChange(change) : ''}${availability(entry, stockChange)}</div>
    <div class="item-meta">
      <a href="${escape(entry.url)}" data-shop="${id}" target="_blank" rel="noopener noreferrer">${escape(shopName(entry.url))}</a>
      ${entry.mode === 'recurring' && !days ? '<span>Opakovaně</span>' : ''}
      ${days ? `<button type="button" class="text-button mute-button" data-action="unmute" data-id="${id}" aria-label="Obnovit upozornění pro ${name}; pozastavena ještě ${days} dní">Ztlumeno · ${days} d</button>` : ''}
    </div>
    ${type ? `<button type="button" class="row-action text-button" data-action="${type}" data-id="${id}" aria-label="${actionLabel}: ${name}">${icon(type === 'restore' ? 'restore' : 'bag')} ${actionLabel}</button>` : ''}</li>`;
}

async function openDetail(id) {
  if (!entryById(id)) return;
  if (!undoAction) dismissToast();
  detailReturn = focusSelector(document.activeElement);
  selected = id; range = 90; detailError = null; renderDetail();
  setText($('#detail-announcement'), '');
  if (!$('#detail').open) $('#detail').showModal();
  await loadDetail(id);
}

async function loadDetail(id) {
  if (live) {
    const generation = ++detailGeneration;
    detailError = null;
    renderDetail();
    try {
      const entry = await api.detail(id);
      if (selected === id && generation === detailGeneration) {
        state.entries = state.entries.map(e => e.id === id ? entry : e);
        renderDetail();
        setText($('#detail-announcement'), 'Historie ceny je načtená.');
      }
    } catch(error) {
      if (selected === id && generation === detailGeneration) {
        detailError = error.message;
        renderDetail();
        setText($('#detail-announcement'), 'Historii se nepodařilo načíst.');
      }
    }
  }
}

function chart(entry, points) {
  const width = 600, height = 140, inset = 5;
  const low = Math.min(...points.map(p => p.cents)), high = Math.max(...points.map(p => p.cents));
  const padding = Math.max((high - low) * .2, high * .06, 100);
  const minimum = Math.max(0, low - padding), maximum = high + padding;
  const first = Date.parse(points[0].date), last = Date.parse(points.at(-1).date);
  const x = p => inset + (Date.parse(p.date) - first) / (last - first) * (width - 2 * inset);
  const y = p => inset + (maximum - p.cents) / (maximum - minimum) * (height - 2 * inset);
  const segments = [[]];
  points.forEach((p,i) => {
    if (i && Date.parse(p.date) - Date.parse(points[i-1].date) > 86400000) segments.push([]);
    segments.at(-1).push(p);
  });
  return `<figure class="price-figure">
    <figcaption id="chart-title" class="sr-only">Vývoj ceny: ${escape(entry.name)}</figcaption>
    <p id="chart-desc" class="sr-only">${points.length} dní s cenou, od ${calendarDate(points[0].date)} do ${calendarDate(points.at(-1).date)}. Denní průměry v korunách, dny bez ceny vynecháváme.</p>
    <div class="chart-axis" aria-hidden="true">${[maximum, (maximum+minimum)/2, minimum].map(value =>
      `<span>${numberFormat.format(Math.round(value / 100))}</span>`).join('')}</div>
    <svg class="chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-labelledby="chart-title" aria-describedby="chart-desc">
      ${[inset,height/2,height-inset].map(yy => `<line x1="0" x2="${width}" y1="${yy}" y2="${yy}"/>`).join('')}
      ${segments.map(segment => `<polyline points="${segment.map(p => `${x(p)},${y(p)}`).join(' ')}" vector-effect="non-scaling-stroke"/>`).join('')}
      ${points.map(p => `<line class="chart-point" x1="${x(p)}" x2="${x(p)}" y1="${y(p)}" y2="${y(p)}"><title>${calendarDate(p.date)} · ${money(Math.round(p.cents))}</title></line>`).join('')}
    </svg>
    <div class="chart-dates" aria-hidden="true"><time datetime="${points[0].date}">${calendarDate(points[0].date)}</time><time datetime="${points.at(-1).date}">${calendarDate(points.at(-1).date)}</time></div>
  </figure>`;
}

const calendarDate = value => date(value + 'T12:00:00Z');
function history(entry, stats, now, period) {
  if (detailError) return `<p class="detail-status warning">${escape(detailError)}</p>
    <button type="button" class="button secondary" id="retry-history" data-action="retry-history" data-id="${escape(entry.id)}">Načíst znovu</button>`;
  if (live && !entry.historyLoaded) return '<p class="chart-empty">Načítám cenovou historii…</p>';
  const points = priceStats(entry, now, period).daily;
  const {start, end, count, days, missing} = historyCoverage(entry, now, period);
  const coverage = `<p class="chart-caption">Rozsah ${calendarDate(start)} – ${calendarDate(end)} · Měření ${count} · Dní s cenou ${days} · Dní bez ceny ${missing}</p>`;
  if (!points.length) return '<p class="chart-empty">V tomto období nemáme ověřenou cenu.</p>' + coverage;
  const variable = points.some(point => point.cents !== points[0].cents);
  const summary = variable ? chart(entry, points) :
    `<p class="chart-empty">${count === 1 ? 'První cena zaznamenána' : days === 1 ? 'Jeden měřený den' : 'Denní průměr beze změny'} ·
      ${days === 1 ? calendarDate(points[0].date) : money(Math.round(points[0].cents))}</p>`;
  const base = historyCoverage(entry, now, 90);
  return `${summary}${coverage}
    <details class="chart-caption" id="daily-disclosure"><summary id="daily-summary">Denní ceny v tabulce</summary>
      <table class="daily-table"><caption class="sr-only">Denní průměry ceny</caption><thead><tr><th scope="col">Den</th><th scope="col">Cena</th></tr></thead>
      <tbody>${points.map(p => `<tr><td>${calendarDate(p.date)}</td><td>${money(Math.round(p.cents))}</td></tr>`).join('')}</tbody></table>
    </details>
    ${stats.average == null || base.count < 2 || stats.average === stats.minimum ? '' : `<div class="detail-stat-grid">
      <div class="detail-stat"><small>Průměr z měření</small><strong>${money(stats.average)}</strong></div>
      <div class="detail-stat"><small>Naměřené minimum</small><strong>${money(stats.minimum)}</strong></div></div>
      <p class="chart-caption">Srovnávací období ${calendarDate(base.start)} – ${calendarDate(base.end)} · Dní s cenou: ${base.days}</p>`}`;
}
function renderDetail() {
  const entry = entryById(selected);
  if (!entry) {$('#detail').close();return;}
  const now = new Date().toISOString(), stats = priceStats(entry, now), days = remainingDays(entry);
  const {current, change, stockChange} = signals(entry, now);
  const pending = entry.collection === 'pending' || refreshing.has(entry.id);
  const knownOnly = current != null && (pending || failedCheck(entry));
  const firstDay = entry.observations[0] && dayKey(entry.observations[0].at);
  const latestDay = entry.observations.at(-1) && dayKey(entry.observations.at(-1).at);
  const hasHistory = firstDay !== latestDay && (!live || entry.historyLoaded);
  const failed = failedCheck(entry), id = escape(entry.id);
  const edit = `<button type="button" class="button secondary" data-action="edit" data-id="${id}">${icon('edit')} Upravit</button>`;
  const comparisonText = current != null && stats.percentage != null && Math.abs(stats.percentage) >= 0.05
    ? `<p class="chart-caption">Proti průměru z měření · ${escape(comparison(stats))}</p>` : '';
  renderMarkup($('#detail-content'), `<div class="detail-top"><span class="context-label">${escape(entry.category)} · ${entry.mode === 'recurring' ? 'Opakovaně' : 'Jednorázově'}</span><button type="button" class="icon-button" id="detail-close" data-close="detail" aria-label="Zavřít detail">${icon('close')}</button></div>
    <div class="detail-product">${thumbnail(entry.image, {large:true})}<div>
      <h2 id="detail-title" tabindex="-1">${escape(entry.name)}</h2>
      <p class="context-label">${escape(shopName(entry.url))}</p>
      <div class="detail-price"><div><small>${knownOnly || current == null && stats.latest ? 'Poslední známá cena' : 'Aktuální cena'}</small>
        ${current != null || stats.latest ? `<strong>${money(current ?? stats.latest.cents)}</strong>` : '<span>Cena není k dispozici</span>'}</div>
      </div>
      <div class="product-signals">${change?.recent ? priceChange(change) : ''}${availability(entry, stockChange)}</div>
    </div></div>
    ${failed ? '<p class="detail-status warning">Poslední kontrola se nepodařila. Zobrazený stav pochází z posledního úspěšného ověření.</p>' : ''}
    ${pending ? `<p class="detail-status">${live ? stats.latest || entry.checkedAt
      ? 'Čeká na další ověření ceny a dostupnosti.'
      : 'Odkaz je uložený. Čeká na první ověření ceny a dostupnosti.' : 'Odkaz je uložený v ukázce.'}</p>` : ''}
    ${entry.collection === 'unsupported' ? '<p class="detail-status">Odkaz je uložený. Tento obchod Haven zatím neumí sledovat.</p>' : ''}
    ${entry.collection === 'ok' && current != null ? '<p class="sr-only">Cena a dostupnost ověřeny.</p>' : ''}
    ${entry.state === 'purchased' && !entry.removedAt ? '<p class="detail-status">Tato položka už je koupená.</p>' : ''}
    <div class="detail-actions">
      <a class="button primary" id="open-shop" href="${escape(entry.url)}" target="_blank" rel="noopener noreferrer">Otevřít v obchodě ${icon('external')}</a>
      ${entry.removedAt ? `<button type="button" class="button secondary" data-action="restore" data-id="${id}">${icon('restore')} Obnovit položku</button>` :
        `${entry.state === 'active' ? `<button type="button" class="button secondary" data-action="purchase" data-id="${id}">${icon('bag')} Zapsat nákup</button>` : ''}${edit}`}
    </div>
    ${days ? `<div class="detail-quiet">Upozornění pozastavena ještě ${days} dní.<button type="button" class="text-button" data-action="unmute" data-id="${id}">Obnovit</button></div>` : ''}
    <section class="detail-section">
      <div class="section-title"><h3>Vývoj ceny</h3><button type="button" class="text-button" id="history-info">Jak číst historii</button></div>
      <div class="detail-history">${hasHistory ? `<div class="chart-tabs" role="group" aria-label="Období cenové historie">${[[30,'Měsíc'],[90,'90 dní'],[365,'Rok'],[36500,'Vše']].map(([value,label])=>`<button type="button" data-range="${value}" aria-pressed="${range===value}">${label}</button>`).join('')}</div>` : ''}
      ${history(entry, stats, now, hasHistory ? range : 36500)}${comparisonText}</div>
    </section>
    ${entry.note ? `<section class="detail-section"><div class="section-title"><h3>Tvoje poznámka</h3></div><p class="detail-note">${escape(entry.note)}</p></section>` : ''}
    <section class="detail-section"><dl class="detail-meta"><div><dt>Přidáno</dt><dd>${date(entry.createdAt)}</dd></div><div><dt>Poslední úspěšná kontrola</dt><dd>${date(entry.checkedAt)}</dd></div></dl></section>
    ${entry.purchases.length ? `<section class="detail-section"><div class="section-title"><h3>Nákupy této položky</h3></div>${[...entry.purchases].reverse().map(p => `<div class="purchase-card ${p.voidedAt ? 'voided' : ''}"><div><strong>${date(p.date+'T12:00:00Z')}</strong><span>${p.unitCents==null?'Cena nezadaná':money(p.unitCents*p.quantity)}</span></div><p>${p.quantity} × ${money(p.unitCents)} za kus${p.voidedAt ? ' · Stornováno' : ''}<br>${p.reference == null || p.unitCents == null ? 'Srovnání zatím není známé.' : `Průměr při nákupu ${money(p.reference)} · ${p.reference >= p.unitCents ? 'Úspora' : 'Nad průměrem'} ${money(Math.abs(p.reference-p.unitCents)*p.quantity)}`}</p>${!p.voidedAt ? `<div class="purchase-edit-actions"><button class="text-button" data-action="correct" data-id="${escape(entry.id)}" data-purchase="${escape(p.id)}">Opravit</button><button class="text-button" ${p.canVoid===false?'disabled title="Předchozí stav nelze bezpečně obnovit po novější změně nebo u historického nákupu."':''} data-action="void" data-id="${escape(entry.id)}" data-purchase="${escape(p.id)}">Stornovat nákup</button></div>` : ''}</div>`).join('')}</section>` : ''}
    ${!entry.removedAt ? `<section class="detail-section detail-actions" id="detail-refresh">
      ${entry.state === 'active' ? `<button type="button" class="button secondary" aria-disabled="${refreshing.has(entry.id)}" data-action="refresh" data-id="${id}">${icon('refresh')} ${refreshing.has(entry.id) ? 'Kontroluji…' : 'Zkontrolovat cenu'}</button>` : ''}
      <button type="button" class="text-button remove-link" data-action="remove" data-id="${id}">Odstranit položku</button></section>` : ''}`,
    '#detail-close');
}
function field(label, name, value = '', attributes = '', help = '') {
  return `<div class="form-field"><label for="field-${name}">${label}</label>
    <input id="field-${name}" name="${name}" autocomplete="off" value="${escape(value)}"
    ${help ? `aria-describedby="field-help-${name}"` : ''} ${attributes}>
    ${help ? `<small id="field-help-${name}">${help}</small>` : ''}
    <p class="field-error" id="field-error-${name}" hidden></p></div>`;
}

function fieldError(input, message = '') {
  const error = $('#field-error-' + input.name);
  if (!error) return;
  error.textContent = message;
  error.hidden = !message;
  if (message) input.setAttribute('aria-invalid', 'true');
  else input.removeAttribute('aria-invalid');
  const references = [$('#field-help-' + input.name)?.id, message && error.id].filter(Boolean);
  if (references.length) input.setAttribute('aria-describedby', references.join(' '));
  else input.removeAttribute('aria-describedby');
}

function validateForm(form, data) {
  const issues = new Map();
  if (formAction === 'add') {
    try {
      data.url = validURL(data.url);
      if (data.url.length > 4096 || new URL(data.url).hash) {
        throw new Error('Vlož HTTPS odkaz bez části za znakem #, nejvýše 4 096 znaků.');
      }
    } catch (error) {
      issues.set(form.elements.url, error.message);
    }
  }
  if (formAction === 'edit' && !data.name.trim()) issues.set(form.elements.name, 'Zadej název položky.');
  if (['purchase', 'correct'].includes(formAction)) {
    try { parseMoney(data.price); }
    catch (error) { issues.set(form.elements.price, error.message); }
    if (!/^\d{1,4}$/.test(data.quantity) || Number(data.quantity) < 1) {
      issues.set(form.elements.quantity, 'Zadej celé číslo od 1 do 9 999.');
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(data.date) || data.date > dayKey(new Date())) {
      issues.set(form.elements.date, 'Zadej platné datum nákupu, nejpozději dnes.');
    }
  }
  for (const [input, message] of issues) fieldError(input, message);
  const first = issues.keys().next().value;
  first?.focus();
  return !first;
}

function select(label, name, choices, value, description = '') {
  const options = choices.map(([code, title]) => `<option value="${escape(code)}"${code === value ? ' selected' : ''}${code ? '' : ' disabled'}>${escape(title)}</option>`);
  const describedBy = description ? ` aria-describedby="${description}"` : '';
  return `<label class="form-field"><span>${label}</span><select name="${name}"${describedBy}>${selectTrigger}${options.join('')}</select></label>`;
}
const previewPanel = `<p class="sr-only" id="preview-announcement" role="status"></p>
<p class="sr-only" id="category-announcement" role="status"></p>
<section class="product-preview" id="product-preview" aria-label="Náhled produktu" hidden>
  <div class="preview-skeleton" id="preview-skeleton" aria-hidden="true" hidden>
    <span class="skeleton-photo"></span>
    <div class="skeleton-product"><span></span><span></span><span></span></div>
    <div class="skeleton-category"><span></span><span></span></div>
  </div>
  <p id="preview-message"></p>
  <label class="form-field" id="preview-size-field" hidden><span>Velikost</span><select id="preview-size"></select></label>
  <div id="preview-product" class="preview-product" hidden></div>
  <button type="button" class="text-button" id="preview-action" hidden></button>
</section>`;

function stopPreview() {
  clearTimeout(previewTimer);
  previewRequest?.abort();
  previewRequest = null;
}

async function loadPreviewPhoto(source, signal) {
  if (!imagePath.test(source)) return null;
  const photo = new Image(160, 160);
  photo.alt = '';
  const bounded = AbortSignal.any([signal, AbortSignal.timeout(5000)]);
  if (bounded.aborted) return null;
  const cancel = () => photo.removeAttribute('src');
  bounded.addEventListener('abort', cancel, {once:true});
  try {
    photo.src = source;
    await photo.decode();
    return bounded.aborted ? null : photo;
  } catch {
    return null;
  } finally {
    bounded.removeEventListener('abort', cancel);
  }
}

function shortProductName(name) {
  const normalized = name.trim().replace(/\s+/g, ' ');
  const headline = normalized.split(/\s+(?:[-–—]|\|)\s+/, 1)[0];
  const title = normalized.length > 70 && headline.length >= 12 ? headline : normalized;
  if (title.length <= 70) return title;
  const fragment = title.slice(0, 69);
  const space = fragment.lastIndexOf(' ');
  return (space >= 45 ? fragment.slice(0, space) : fragment).trimEnd() + '…';
}

function showPreview(status, message, product = null, variants = []) {
  const panel = $('#product-preview'), content = $('#preview-product'), action = $('#preview-action');
  const focusedAction = document.activeElement === action;
  const canSave = ['ready', 'unsupported', 'retry_later', 'unavailable', 'manual'].includes(status);
  const extra = $('#editor-fields .extra-fields'), submit = $('#editor-submit');
  panel.hidden = !status;
  panel.dataset.state = status;
  $('#preview-skeleton').hidden = status !== 'loading';
  $('#preview-size-field').hidden = status !== 'choose_variant';
  $('#preview-size').innerHTML = variants.length ? selectTrigger +
    '<option value="" disabled selected>Vyber velikost</option>' +
    variants.map(({size, url}) => `<option value="${escape(url)}">${escape(size)}</option>`).join('') : '';
  content.hidden = !product;
  content.innerHTML = product ? `${thumbnail()}<div class="preview-identity">
    <strong>${escape(product.name)}</strong>
    <div class="preview-facts"><span class="preview-price">${product.price == null ? 'Cena nezjištěna' : money(Math.round(Number(product.price) * 100))}</span>
    <span>${stock(product.availability)}</span><span>${escape(product.shop)}</span></div>
    ${product.variant_url ? '<p>Odkaz doplněn o variantu.</p>' : ''}</div>` : '';
  if (product?.photo) content.querySelector('.thumbnail').append(product.photo);
  const skippable = status === 'loading' || status === 'choose_variant';
  action.hidden = !skippable && !['unavailable', 'retry_later', 'manual'].includes(status);
  setText(action, skippable ? 'Přeskočit náhled' : 'Zkusit náhled znovu');
  setText($('#preview-message'), product ? '' : message);
  $('#preview-message').hidden = Boolean(product);
  setText($('#preview-announcement'), product ? `${message} ${content.textContent.replace(/\s+/g, ' ').trim()}` : message);
  $('#add-category').hidden = extra.hidden = submit.hidden = !canSave;
  submit.disabled = !canSave;
  setText(submit, product ? 'Přidat položku' : 'Uložit odkaz');
  if (focusedAction && action.hidden) {
    (canSave ? extra.querySelector('summary') : $('#field-url')).focus();
  }
}

function showCategory(state = '', label = '') {
  const status = $('#category-status');
  if (!status) return;
  const messages = {suggested:'Navrženo automaticky', unavailable:'Bez návrhu. Kategorii můžeš vybrat.'};
  status.dataset.state = state;
  status.hidden = !state;
  setText(status, messages[state] || '');
  setText($('#category-announcement'), state === 'suggested' ? `Návrh kategorie: ${label}.` : messages[state] || '');
}

const previewLoading = 'Načítám údaje o produktu a kategorii…';

function schedulePreview(input, delay = 450) {
  stopPreview();
  const {name, category} = input.form.elements;
  if (suggestedName && name.value === suggestedName) name.value = '';
  suggestedName = '';
  if (!category.value || suggestedCategory && category.value === suggestedCategory) category.value = 'Nezařazeno';
  suggestedCategory = '';
  showPreview('', '');
  showCategory();
  if (!input.value.trim()) return;
  previewTimer = setTimeout(async () => {
    let url;
    try {
      url = validURL(input.value);
      if (url.length > 4096 || new URL(url).hash) throw new Error();
    } catch {
      showPreview('invalid', 'Vlož platný HTTPS odkaz na produkt.');
      input.setAttribute('aria-invalid', 'true');
      input.setAttribute('aria-describedby', 'preview-message');
      return;
    }
    const controller = new AbortController();
    previewRequest = controller;
    showPreview('loading', previewLoading);
    try {
      const result = await api.preview(url, controller.signal);
      if (controller.signal.aborted) return;
      if (result.status === 'ready') {
        const offerCategory = /^[a-f0-9]{32}$/.test(result.category_token) && !category.dataset.edited;
        const [code, photo] = await Promise.all([
          offerCategory ? api.previewCategory(result.category_token, controller.signal).catch(() => null) : null,
          loadPreviewPhoto(result.image, controller.signal),
        ]);
        if (controller.signal.aborted) return;
        const title = shortProductName(result.name);
        if (!name.value.trim()) name.value = suggestedName = title;
        if (result.variant_url) input.value = result.variant_url;
        showPreview('ready', 'Náhled je připravený.', {...result, photo, name: title, shop:shopName(url)});
        if (offerCategory) {
          const label = Object.hasOwn(api.categoryNames, code) && api.categoryNames[code];
          if (label) category.value = suggestedCategory = label;
          showCategory(label ? 'suggested' : 'unavailable', label);
        }
      } else if (result.status === 'choose_variant') {
        showPreview('choose_variant', 'Pro ověření ceny a dostupnosti vyber velikost.', null, result.variants);
      } else if (result.status === 'unsupported') {
        showPreview('unsupported', 'Tento obchod nemá náhled.');
      } else if (result.status === 'retry_later') {
        showPreview('retry_later', 'Obchod požádal o odklad náhledu.');
      } else {
        showPreview('unavailable', 'Náhled se nepodařilo načíst.');
      }
    } catch {
      if (!controller.signal.aborted) {
        showPreview('unavailable', 'Náhled se nepodařilo načíst.');
      }
    } finally {
      if (previewRequest === controller) previewRequest = null;
    }
  }, delay);
}

function openEditor(action, id = null, purchaseId = null) {
  if (formBusy) return;
  if (!undoAction) dismissToast();
  stopPreview();
  suggestedName = '';
  suggestedCategory = '';
  $('#editor-submit').hidden = $('#editor-submit').disabled = false;
  editorReturn=focusSelector(document.activeElement);
  formAction=action;formEntry=id;formPurchase=purchaseId;
  formRequestId=live?api.requestId():null;formVersion=id?entryById(id)?.version:null;
  const entry = id ? entryById(id) : null;
  $('#form-error').hidden=true;
  $('#editor-eyebrow').hidden=!entry || !['purchase','correct'].includes(action);
  if (['purchase','correct'].includes(action)) {
    const p = entry.purchases.find(p=>String(p.id)===String(purchaseId));
    $('#editor-title').textContent=action==='correct'?'Opravit nákup':'Zapsat nákup';
    $('#editor-eyebrow').textContent=entry.name;
    $('#editor-submit').textContent=action==='correct'?'Uložit opravu':'Potvrdit nákup';
    const latest=priceStats(entry).latest;
    $('#editor-fields').innerHTML = `<div class="form-pair">
      ${field('Cena za kus (Kč)', 'price', inputMoney(p?.unitCents), 'required inputmode="decimal" maxlength="11"')}
      ${field('Počet kusů', 'quantity', p?.quantity ?? 1, 'required type="number" min="1" max="9999" step="1"')}</div>
      ${action === 'purchase' && latest ? `<button type="button" class="text-button use-price" id="use-last-price" value="${inputMoney(latest.cents)}">Použít poslední cenu · ${money(latest.cents)}</button>` : ''}
      ${field('Datum nákupu', 'date', p?.date ?? dayKey(new Date()), `required type="date" max="${dayKey(new Date())}"`)}
      <div class="purchase-total"><span>Celkem zaplaceno</span><output id="purchase-total" for="field-price field-quantity">—</output></div>
      <p class="purchase-reference">${entry.mode === 'recurring' ? 'Po nákupu ztlumíme upozornění na 30 dní od data nákupu.' : 'Po nákupu položku najdeš ve filtru Koupené.'}</p>`;
    updatePurchaseTotal();
  } else {
    $('#editor-title').textContent=action==='add'?'Přidat položku':'Upravit položku';
    $('#editor-submit').textContent=action==='add'?'Uložit odkaz':'Uložit změny';
    const url = field('Odkaz na produkt','url',entry?.url||'',`required type="url" maxlength="4096"
      spellcheck="false" placeholder="https://…" ${entry?'readonly':''}`,
      entry ? 'Odkaz patří ke sledované variantě.' : '');
    const liveAdd = live && action === 'add';
    const categoryOptions = CATEGORIES.map(c => [c, c]);
    const category = select('Kategorie', 'category', categoryOptions,
      entry?.category || 'Nezařazeno', liveAdd ? 'category-status' : '');
    const addCategory = liveAdd ? `<div id="add-category" hidden>${category}
      <p class="category-status" id="category-status" hidden></p></div>` : '';
    const mode = select('Nákup','mode',[['once','Jednorázově'],['recurring','Opakovaně']],entry?.mode||'once');
    const extra = field('Název','name',entry?.name||'',`${entry?'required':''} maxlength="200"`) +
      (liveAdd ? mode : `<div class="form-pair">${category}${mode}</div>`) +
      `<label class="form-field"><span>Poznámka</span><textarea name="note" maxlength="2000">${escape(entry?.note||'')}</textarea></label>`;
    $('#editor-fields').innerHTML = url + (action === 'add'
      ? `${liveAdd ? previewPanel + addCategory : ''}<details class="extra-fields"><summary>Další údaje</summary>
        <div class="disclosure-content"><div class="extra-field-inputs">${extra}</div></div></details>` : extra);
    if (liveAdd) showPreview('', '');
  }
  editorInitialValues = editorValues();
  if (!$('#editor').open) $('#editor').showModal();
  const initialFocus = matchMedia('(pointer: coarse)').matches ? '#editor-title' : '#editor-fields input:not([readonly])';
  $(initialFocus)?.focus({preventScroll:true});
}
function editorValues() {
  return JSON.stringify([...new FormData($('#editor-form'))]);
}

function editorChanged() {
  return editorValues() !== editorInitialValues;
}

function requestEditorClose() {
  if (formBusy || $('#discard-dialog').open) return;
  if (!editorChanged()) {
    $('#editor').close();
    return;
  }
  const confirmation = $('#discard-dialog');
  confirmation.returnValue = '';
  confirmation.showModal();
}

$('#discard-dialog').addEventListener('close', event => {
  if (event.target.returnValue === 'discard') $('#editor').close();
});

function updatePurchaseTotal(){
  if(!$('#purchase-total'))return;
  try{const data=new FormData($('#editor-form'));const quantity=Number(data.get('quantity'));$('#purchase-total').textContent=Number.isInteger(quantity)&&quantity>0?money(parseMoney(data.get('price'))*quantity):'—';}catch{$('#purchase-total').textContent='—';}
}
$('#editor-form').addEventListener('input', event => {
  fieldError(event.target);
  if (live && formAction === 'add' && event.target.name === 'url') schedulePreview(event.target);
  if (event.target.name === 'name') suggestedName = '';
  $('#form-error').hidden = true;
  updatePurchaseTotal();
});
$('#editor-form').addEventListener('change', event => {
  if (event.target.id === 'preview-size' && event.target.value) {
    const input = event.target.form.elements.url;
    input.value = event.target.value;
    input.focus({preventScroll:true});
    // The follow-up fetch still uses the shop's preview cooldown.
    schedulePreview(input, 2000);
    showPreview('loading', previewLoading);
  }
  if (event.target.name === 'category') {
    event.target.dataset.edited = 'true';
    suggestedCategory = '';
    showCategory();
  }
});
$('#editor-form').addEventListener('submit',async event=>{
  event.preventDefault();
  if (formBusy || $('#editor-submit').disabled) return;
  const data=Object.fromEntries(new FormData(event.target));
  const submit = $('#editor-submit');
  const label = submit.textContent;
  const inputs = [...$('#editor-fields').querySelectorAll('input,select,textarea')].filter(input => !input.disabled);
  if (!validateForm(event.target, data)) return;
  let addedId = null, saved = false;
  try{
    if (formAction === 'add') stopPreview();
    formBusy = true;
    submit.disabled = true;
    submit.textContent = 'Ukládám…';
    setText($('#editor-status'), 'Ukládám…');
    for (const input of inputs) input.disabled = true;
    if(live){addedId=await submitLiveForm(data);}
    else if(formAction==='add'){const entry = createEntry(data); commit(next=>next.entries.push(entry)); addedId = entry.id;}
    else if(formAction==='edit'){delete data.url;commit(next=>updateEntry(next.entries.find(e=>e.id===formEntry),data));}
    else if(formAction==='purchase'){commit(next=>recordPurchase(next.entries.find(e=>e.id===formEntry),data));}
    else if(formAction==='correct'){commit(next=>correctPurchase(next.entries.find(e=>e.id===formEntry),formPurchase,data));}
    saved = true;
    $('#editor').close();
  }catch(error){
    $('#form-error').textContent=error.code === 'invalid_request' && ['add','edit'].includes(formAction)
      ? 'Údaje se nepodařilo uložit. Zkontroluj odkaz a vyplněná pole.' : error.message;
    $('#form-error').hidden=false;
    setText($('#editor-status'), '');
  }finally{
    formBusy=false;
    submit.disabled=false;
    submit.textContent=label;
    for (const input of inputs) input.disabled = false;
    if (!$('#form-error').hidden) $('#form-error').focus();
  }
  if (addedId && entryById(addedId)) {
    highlightedId = addedId;
    clearTimeout(highlightTimer);
    render();
    $('#add-button').focus({preventScroll:true});
    openDetail(addedId);
  }
  if (saved) toast({add:'Položka je přidaná.', edit:'Změny jsou uložené.', purchase:'Nákup je zapsaný.', correct:'Nákup je opravený.'}[formAction]);
});
function action(button){
  const id=button.dataset.id,kind=button.dataset.action;
  if(button.getAttribute('aria-disabled') === 'true')return;
  if(kind==='detail'){openDetail(id);return;}
  if(kind==='retry-history'){loadDetail(id);return;}
  if(live && !['purchase','edit','correct'].includes(kind)){liveAction(button).catch(error=>toast(error.message));return;}
  if(['purchase','edit','correct'].includes(kind)){openEditor(kind,id,button.dataset.purchase);return;}
  try{
    if(kind==='remove'){
      commit(next=>removeEntry(next.entries.find(e=>e.id===id)));
      $('#detail').close();toast('Položka je odstraněná. Historie zůstává.',()=>{commit(next=>restoreEntry(next.entries.find(e=>e.id===id)));toast('Položka je zpět.');});
    }else if(kind==='restore'){commit(next=>restoreEntry(next.entries.find(e=>e.id===id)));toast('Položka je obnovená.');}
    else if(kind==='unmute'){commit(next=>resumeNotifications(next.entries.find(e=>e.id===id)));toast('Upozornění jsou znovu zapnutá.');}
    else if(kind==='void'){commit(next=>voidPurchase(next.entries.find(e=>e.id===id),button.dataset.purchase));toast('Nákup je stornovaný a předchozí stav obnovený.');}
    else if(kind==='refresh')toast('V této ukázce se živé ceny nenačítají. Ruční kontrola se připojí k produkčnímu sběrači.');
  }catch(error){toast(error.message);}
}
document.addEventListener('click',event=>{
  const usePrice = event.target.closest('#use-last-price');
  if (usePrice) {
    const price = $('#field-price');
    price.value = usePrice.value.replace('.', ',');
    fieldError(price);
    updatePurchaseTotal();
    price.focus();
    return;
  }
  if (event.target.closest('#preview-action')) {
    const input = $('#editor-form').elements.url;
    input.focus({preventScroll:true});
    if (['loading', 'choose_variant'].includes($('#product-preview').dataset.state)) {
      stopPreview();
      showPreview('manual', 'Náhled přeskočen.');
    } else schedulePreview(input, 0);
    return;
  }
  const close=event.target.closest('[data-close]');
  if(close){
    if (close.dataset.close === 'editor') requestEditorClose();
    else $('#'+close.dataset.close).close();
    return;
  }
  if (event.target.closest('#history-info')) {$('#info-dialog').showModal();return;}
  if (event.target.closest('[data-action="void"]')) {
    const button = event.target.closest('[data-action="void"]');
    const confirmation = $('#confirm-dialog');
    confirmation.returnValue = '';
    confirmation.addEventListener('close', () => { if (confirmation.returnValue === 'confirm') action(button); }, {once:true});
    confirmation.showModal();
    return;
  }
  const eventButton = event.target.closest('[data-event]');
  if (eventButton) {
    eventFilter = eventFilter === eventButton.dataset.event ? '' : eventButton.dataset.event;
    render();
    return;
  }
  const button=event.target.closest('[data-action]');if(button){action(button);return;}
  const tab=event.target.closest('[data-range]');if(tab){range=Number(tab.dataset.range);renderDetail();}
});
for(const dialog of document.querySelectorAll('dialog')){
  dialog.addEventListener('click',event=>{
    if (event.target !== dialog || dialog.id === 'discard-dialog') return;
    if (dialog.id === 'editor' && (formBusy || editorChanged())) return;
    const rect=dialog.getBoundingClientRect();
    if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();
  });
  dialog.addEventListener('close', () => {
    if ($('#toast').parentElement === dialog) {
      dismissToast();
      document.body.append($('#toast'), $('#toast-status'));
    }
  });
}
$('#editor').addEventListener('cancel', event => {
  event.preventDefault();
  requestEditorClose();
});
$('#editor').addEventListener('close', () => {
  if ($('#editor').open) return;
  stopPreview();
  setText($('#editor-status'), '');
  if ($('#detail').open) {
    if (!$('#detail').contains(document.activeElement)) restoreFocus(editorReturn, '#detail-close', $('#detail'));
  } else restoreFocus(editorReturn, '#list-title');
});
$('#detail').addEventListener('close', () => {
  if ($('#detail').open) return;
  if (selected === highlightedId) {
    highlightTimer = setTimeout(() => { highlightedId = null; render(); }, 5500);
  }
  selected = null;
  detailGeneration++;
  if (!$('#editor').open) restoreFocus(detailReturn, '#list-title');
});
$('#add-button').onclick=()=>openEditor('add');
$('#reset-filters').onclick=resetFilters;
$('#search').addEventListener('input',render);
for(const select of document.querySelectorAll('.filters select,.secondary-filters select'))select.addEventListener('change',render);
$('#toast-close').onclick = dismissToast;
$('#toast-undo').onclick = async () => {
  const undo = undoAction;
  dismissToast();
  try { if (undo) await undo(); }
  catch (error) { toast(error.message); }
};
$('#reset-demo').onclick=()=>{
  const previous=structuredClone(state);commit(next=>Object.assign(next,demoState()));
  toast('Ukázková data jsou obnovená.',()=>{commit(next=>Object.assign(next,previous));toast('Obnovení ukázky je vrácené.');});
};
window.addEventListener('storage',event=>{
  if(!live && event.key===storageKey)toast('Ukázka se změnila v jiné kartě. Obnov stránku, než budeš pokračovat.');
});
render();

function showLoadError(error) {
  loading=false;loadError=error.message;render();
}
function reloadList() {
  if (loading) return;
  loading = true;
  loadError = null;
  render();
  loadLive().catch(showLoadError);
}
async function loadLive() {
  const generation = ++listGeneration;
  let entries;
  try { entries = await api.list(); }
  catch (error) {
    if (generation === listGeneration) throw error;
    return;
  }
  if (generation !== listGeneration) return;
  const detail = selected && $('#detail').open ? entryById(selected) : null;
  if (detail?.historyLoaded) {
    entries = entries.map(entry => entry.id === detail.id ? {
      ...entry, historyLoaded:true,
      observations:detail.observations, purchases:detail.purchases,
    } : entry);
  }
  state={version:1,entries};loading=false;loadError=null;render();
  if(selected && $('#detail').open) {
    if (entryById(selected)) await loadDetail(selected);
    else $('#detail').close();
  }
}
async function submitLiveForm(data) {
  const entry=formEntry?entryById(formEntry):null;
  const ref=entry?{entry_id:entry.id,expected_version:formVersion}:{};
  let operation,args;
  if(formAction==='add'||formAction==='edit'){
    const fields={category:api.categoryCode(data.category),purchase_mode:data.mode,note:data.note||null};
    if(data.name.trim())fields.label=data.name.trim();
    operation=formAction==='add'?'add':'update';args=operation==='add'?{url:data.url,details:fields}:{...ref,patch:fields};
  }else{
    operation=formAction==='purchase'?'purchase':'correct_purchase';
    args={...ref,unit_price:data.price,quantity:Number(data.quantity),purchase_date:data.date};
    if(operation==='correct_purchase')args.purchase_id=Number(formPurchase);
  }
  const result=await api.command(operation,args,formRequestId);
  const saved = result.data?.entry;
  if (saved && !entryById(saved.entry_id)) state.entries.push(api.toUI(saved));
  await loadLive().catch(showLoadError);
  if(operation==='add' && result.entry_id && saved?.collection_status==='pending') {
    pollRefresh(result.entry_id, saved.last_attempt_at).catch(error=>toast(error.message));
  }
  return operation === 'add' ? result.entry_id : null;
}
async function liveAction(button) {
  const id=button.dataset.id,kind=button.dataset.action,entry=entryById(id);
  if(kind==='refresh' && refreshing.has(id))return;
  const operation={remove:'remove',restore:'restore',unmute:'unmute',void:'void_purchase',refresh:'refresh'}[kind];
  if(!operation)return;
  const args={entry_id:id,expected_version:entry.version,...(kind==='void'?{purchase_id:Number(button.dataset.purchase)}:{})};
  const binding=JSON.stringify([operation,args]);
  if(!pendingActions.has(binding))pendingActions.set(binding,{id:api.requestId(),busy:false});
  const pending=pendingActions.get(binding);if(pending.busy)return;
  pending.busy=true;button.disabled=true;
  try{
    const result=await api.command(operation,args,pending.id);
    pendingActions.delete(binding);await loadLive();
    if(kind==='remove'){
      $('#detail').close();toast('Položka je odstraněná. Historie zůstává.',async()=>{
        const current=entryById(id);await api.command('restore',{entry_id:id,expected_version:current.version});await loadLive();toast('Položka je zpět.');
      });
    }else if(kind==='refresh'){
      if(result.data?.refresh_status==='postponed')toast('Další kontrola může proběhnout nejdříve '+new Date(result.data.retry_at).toLocaleString('cs-CZ')+'.');
      else{toast('Kontrola ceny je zařazená.');pollRefresh(id).catch(error=>toast(error.message));}
    }else toast({restore:'Položka je obnovená.',unmute:'Upozornění jsou znovu zapnutá.',void:'Nákup je stornovaný.'}[kind]);
  }catch(error){if(!error.uncertain)pendingActions.delete(binding);if(error.code==='conflict')await loadLive();throw error;}
  finally{pending.busy=false;button.disabled=false;}
}
async function pollRefresh(id, start = entryById(id)?.lastAttemptAt) {
  if(refreshing.has(id))return;
  refreshing.add(id);if(selected===id && $('#detail').open)renderDetail();
  try{
    for(let attempt=0;attempt<35;attempt++){
      await new Promise(resolve=>setTimeout(resolve,2000));
      const response=await api.command('get',{entry_id:id}),entry=api.toUI(response.data.entry);
      if(entry.lastAttemptAt && entry.lastAttemptAt!==start){
        await loadLive();
        const message = entry.collection==='ok' ? 'Kontrola ceny a dostupnosti je dokončená.' :
          entry.collection==='unsupported' ? 'Odkaz je uložený. Tento obchod Haven zatím neumí sledovat.' :
          'Kontrola skončila bez ověřené ceny. Podrobnosti najdeš v detailu.';
        if (selected === id && $('#detail').open) setText($('#detail-announcement'), message);
        toast(message);
        return;
      }
    }
    const message = 'Kontrola zatím čeká na dokončení. Výsledek se objeví v přehledu.';
    if (selected === id && $('#detail').open) setText($('#detail-announcement'), message);
    toast(message);
  }finally{refreshing.delete(id);if(selected===id && $('#detail').open)renderDetail();}
}
if(live){
  $('.preview-note').hidden=true;
  loadLive().catch(showLoadError);
  setInterval(()=>{if(!document.hidden&&!document.querySelector('dialog[open]')&&!loading)loadLive().catch(showLoadError);},30000);
}
