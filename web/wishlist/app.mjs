import {CATEGORIES, money, dayKey, priceStats, comparison, remainingDays, visibleEntries,
  createEntry, updateEntry, recordPurchase, correctPurchase, voidPurchase,
  removeEntry, restoreEntry, resumeNotifications, inputMoney, parseMoney} from './model.mjs';
const live = document.querySelector('meta[name="haven-mode"]')?.content === 'production';
const api = live ? await import('./api.mjs') : null;
const {demoState} = live ? {demoState: null} : await import('./demo.mjs');

const $ = selector => document.querySelector(selector);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
  heart:'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z',
  plus:'M12 5v14M5 12h14', close:'m6 6 12 12M6 18 18 6', search:'M21 21l-4.4-4.4M19 10.5a8.5 8.5 0 1 1-17 0 8.5 8.5 0 0 1 17 0Z',
  trend:'m3 17 6-6 4 4 8-10M15 5h6v6', lock:'M6 10h12v11H6zM8 10V6a4 4 0 0 1 8 0v4',
  leaf:'M20 3C8 2 2 8 5 16s16 5 15-13ZM4 21 16 9',
  check:'m5 12 4 4L19 6', external:'M14 3h7v7M21 3 11 13M10 3H3v18h18v-7',
  repeat:'m17 2 4 4-4 4M3 11V8a2 2 0 0 1 2-2h16M7 22l-4-4 4-4M21 13v3a2 2 0 0 1-2 2H3',
  muted:'M18 8a6 6 0 0 0-10-4M6 6a6 6 0 0 0 0 2c0 7-3 7-3 9h14M10 21h4M3 3l18 18',
  info:'M12 11v6M12 7h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z',
  edit:'m16 3 5 5-12 12-6 1 1-6Z', trash:'M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7',
  refresh:'M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 14 6M4 12a8 8 0 0 0 14 6', restore:'M3 3v6h6M3 9a9 9 0 1 1 1 9',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.info}"/></svg>`;
function icons(root = document) { root.querySelectorAll('[data-icon]').forEach(node => node.replaceWith(document.createRange().createContextualFragment(icon(node.dataset.icon)))); }
icons();
const date = value => value ? new Intl.DateTimeFormat('cs-CZ', {timeZone:'Europe/Prague', day:'numeric', month:'numeric', year:'numeric'}).format(new Date(value)) : 'Zatím neověřeno';
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
const pendingActions = new Map(), refreshing = new Set();
function toast(message, undo = null) {
  clearTimeout(toastTimer); undoAction = undo;
  $('#toast-message').textContent = message; $('#toast-undo').hidden = !undo; $('#toast').hidden = false;
  if (!undo) toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 8000);
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
    stock:$('#filter-stock').value,mode:$('#filter-mode').value,state:$('#filter-state').value};
}
function resetFilters() {
  $('#search').value = '';
  for (const key of ['category','shop','stock','mode']) $(`#filter-${key}`).value = '';
  $('#filter-state').value = 'default'; render();
}
$('#filter-category').insertAdjacentHTML('beforeend', CATEGORIES.map(c => `<option>${escape(c)}</option>`).join(''));
function render() {
  const selectedShop = $('#filter-shop').value;
  const shops = [...new Set(state.entries.map(entry => new URL(entry.url).hostname))].sort();
  $('#filter-shop').innerHTML = '<option value="">Všechny obchody</option>' + shops.map(s => `<option>${escape(s)}</option>`).join('');
  $('#filter-shop').value = selectedShop;
  const f = filters(), entries = visibleEntries(state.entries, f);
  const active = state.entries.filter(entry => !entry.removedAt && entry.state === 'active');
  $('#active-count').textContent = active.length;
  $('#nav-count').textContent = active.length;
  $('#discount-count').textContent = active.filter(entry => (priceStats(entry).saving ?? 0) > 0).length;
  $('#visible-count').textContent = entries.length;
  $('#reset-filters').hidden = !f.search && !f.category && !f.shop && !f.stock && !f.mode && f.state === 'default';
  const titles = {default:'V hledáčku', purchased:'Koupené položky', removed:'Odstraněné položky', all:'Všechny položky'};
  $('.list-heading h2').firstChild.textContent = (titles[f.state] || 'Položky') + ' ';
  let category = null;
  $('#rows').innerHTML = entries.map(entry => {
    let heading = '';
    if (category !== entry.category) {
      category = entry.category;
      heading = `<tr class="category-row"><th scope="rowgroup" colspan="7">${escape(category)} <span>${entries.filter(e => e.category === category).length}</span></th></tr>`;
    }
    return heading + row(entry);
  }).join('');
  $('#empty').hidden = entries.length !== 0;
  if (live && (loading || loadError)) {
    $('#empty').hidden = false;
    $('#empty-message').textContent = loading ? 'Načítám tvůj wishlist…' : loadError;
    $('#empty-action').textContent = 'Zkusit znovu';
    $('#empty-action').hidden = loading;
    $('#empty-action').onclick = () => loadLive().catch(showLoadError);
    return;
  }
  $('#empty-action').hidden = false;
  const isUnfilteredEmpty = !state.entries.some(e => !e.removedAt && e.state === 'active') && $('#reset-filters').hidden;
  $('#empty-message').textContent = isUnfilteredEmpty ? 'Přidej první odkaz a začni si skládat svůj wishlist.' : 'Zkus změnit hledání nebo filtry.';
  $('#empty-action').textContent = isUnfilteredEmpty ? 'Přidat položku' : 'Zrušit filtry';
  $('#empty-action').onclick = isUnfilteredEmpty ? () => openEditor('add') : resetFilters;
}
function row(entry) {
  const stats = priceStats(entry), days = remainingDays(entry), failed = ['failed','blocked','stale'].includes(entry.collection);
  let action = `<button class="row-action" data-action="purchase" data-id="${escape(entry.id)}" aria-label="Označit jako koupené: ${escape(entry.name)}" title="Koupeno">${icon('check')}</button>`;
  if (entry.removedAt) action = `<button class="row-action" data-action="restore" data-id="${escape(entry.id)}" aria-label="Obnovit: ${escape(entry.name)}" title="Obnovit">${icon('restore')}</button>`;
  else if (entry.state === 'purchased') action = `<button class="row-action" data-action="detail" data-id="${escape(entry.id)}" aria-label="Detail: ${escape(entry.name)}">${icon('external')}</button>`;
  return `<tr class="${entry.availability === 'out_of_stock' ? 'unavailable' : ''}">
    <td><button class="item-title" data-action="detail" data-id="${escape(entry.id)}">${escape(entry.name)}</button>
    <div class="item-meta"><a class="external" href="${escape(entry.url)}" target="_blank" rel="noopener noreferrer">${escape(new URL(entry.url).hostname.replace(/^www\./,''))}</a><span>·</span><span>${entry.mode === 'recurring' ? 'Opakovaně' : 'Jednorázově'}</span>
    ${days ? `<button class="mute-button" data-action="unmute" data-id="${escape(entry.id)}" title="Upozornění pozastavena ještě ${days} dní. Kliknutím obnovit." aria-label="Obnovit upozornění pro ${escape(entry.name)}; pozastavena ještě ${days} dní">${icon('muted')}<span>${days} d</span></button>` : ''}</div><span class="subtext">Přidáno ${date(entry.createdAt)}</span></td>
    <td><span class="stock ${escape(entry.availability)}">${stock(entry.availability)}</span></td>
    <td><span class="price">${money(stats.current)}</span><span class="subtext">${failed ? 'Poslední známá · ' : entry.checkedAt ? 'Ověřeno ' : ''}${entry.checkedAt ? date(entry.checkedAt) : 'Čeká na první cenu'}</span></td>
    <td>${money(stats.average)}</td><td>${money(stats.minimum)}</td>
    <td><span class="deal ${stats.saving < 0 ? 'higher' : !stats.saving ? 'neutral' : ''}">${escape(comparison(stats))}</span>${failed && stats.current != null ? '<span class="subtext">Z poslední známé ceny</span>' : ''}</td><td>${action}</td></tr>`;
}
async function openDetail(id) {
  selected = id; range = 90; renderDetail();
  if (!$('#detail').open) $('#detail').showModal();
  if (live) {
    const generation = ++detailGeneration;
    try {
      const entry = await api.detail(id);
      if (selected === id && generation === detailGeneration) {
        state.entries = state.entries.map(e => e.id === id ? entry : e); renderDetail();
      }
    } catch(error) {toast(error.message);}
  }
}
function chart(entry) {
  if(live && !entry.historyLoaded)return '<div class="chart-empty">Načítám cenovou historii…</div>';
  const stats = priceStats(entry, new Date().toISOString(), range);
  if (!stats.daily.length) return '<div class="chart-empty">Zatím tu není žádná ověřená cena.<br>Graf začne už prvním měřením.</div>';
  const points = stats.daily, width = 440, height = 190, left = 54, right = 12, top = 14, bottom = 30;
  const low = Math.min(...points.map(p => p.cents)), high = Math.max(...points.map(p => p.cents));
  const padding = Math.max((high - low) * .2, high * .06, 100);
  const minimum = Math.max(0, low - padding), maximum = high + padding;
  const first = Date.parse(points[0].date), last = Date.parse(points.at(-1).date);
  const x = p => left + (last === first ? .5 : (Date.parse(p.date) - first) / (last - first)) * (width - left - right);
  const y = p => top + (maximum - p.cents) / (maximum - minimum) * (height - top - bottom);
  const segments = [[]];
  points.forEach((p,i) => {if(i && Date.parse(p.date)-Date.parse(points[i-1].date)>86400000) segments.push([]);segments.at(-1).push(p);});
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="chart-title chart-desc"><title id="chart-title">Vývoj ceny: ${escape(entry.name)}</title><desc id="chart-desc">${points.length} dní s cenou, od ${points[0].date} do ${points.at(-1).date}. Denní průměry; dny bez ceny vynecháváme.</desc>
    ${[0,.5,1].map(f => {const value = maximum - (maximum-minimum)*f, yy = top+f*(height-top-bottom);return `<line x1="${left}" x2="${width-right}" y1="${yy}" y2="${yy}" stroke="#e8eddf" stroke-dasharray="3 4"/><text x="${left-9}" y="${yy+4}" text-anchor="end">${Math.round(value/100).toLocaleString('cs-CZ')}</text>`;}).join('')}
    ${segments.map(segment => `<polyline points="${segment.map(p => `${x(p)},${y(p)}`).join(' ')}" fill="none" stroke="#7f9c62" stroke-width="2" stroke-linejoin="round"/>`).join('')}
    ${points.map(p => `<circle cx="${x(p)}" cy="${y(p)}" r="${points.length === 1 ? 4 : 2}" fill="#7f9c62"><title>${p.date}: ${money(Math.round(p.cents))}</title></circle>`).join('')}
    <text x="${left}" y="${height-5}">${date(points[0].date+'T12:00:00Z')}</text><text x="${width-right}" y="${height-5}" text-anchor="end">${points.length > 1 ? date(points.at(-1).date+'T12:00:00Z') : ''}</text></svg>
    <p class="chart-caption">${points.length} dní s cenou · ${points[0].date === points.at(-1).date ? 'První zjištěná cena' : `${date(points[0].date+'T12:00:00Z')} – ${date(points.at(-1).date+'T12:00:00Z')}`}<br>Body jsou denní průměry. Chybějící dny nedoplňujeme.</p>
    <details class="chart-caption"><summary>Zobrazit naměřené ceny jako tabulku</summary><table><caption class="sr-only">Denní cenová historie</caption><thead><tr><th>Den</th><th>Cena</th></tr></thead><tbody>${points.map(p=>`<tr><td>${date(p.date+'T12:00:00Z')}</td><td>${money(Math.round(p.cents))}</td></tr>`).join('')}</tbody></table></details>`;
}
function renderDetail() {
  const entry = entryById(selected);
  if (!entry) {$('#detail').close();return;}
  const stats = priceStats(entry), days = remainingDays(entry), failed = ['failed','blocked','stale'].includes(entry.collection);
  $('#detail-content').innerHTML = `<div class="detail-top"><span class="eyebrow">${escape(entry.category)} · ${entry.mode === 'recurring' ? 'OPAKOVANĚ' : 'JEDNORÁZOVĚ'}</span><button class="icon-button" data-close="detail" aria-label="Zavřít detail">${icon('close')}</button></div>
    <h2 id="detail-title">${escape(entry.name)}</h2><a class="shop-link" href="${escape(entry.url)}" target="_blank" rel="noopener noreferrer">Otevřít v obchodě ${icon('external')}</a>
    <div class="detail-price"><div><small>${failed ? 'Poslední známá cena' : 'Aktuální cena'}</small><strong>${money(stats.current)}</strong></div><span class="stock ${escape(entry.availability)}">${stock(entry.availability)}</span></div>
    <span class="deal ${stats.saving < 0 ? 'higher' : !stats.saving ? 'neutral' : ''}">${escape(comparison(stats))}${stats.saving ? ` · ${money(Math.abs(stats.saving))}` : ''}</span>
    ${entry.availability === 'out_of_stock' && stats.latest ? `<div class="detail-status">Poslední známá cena ${money(stats.latest.cents)} z ${date(stats.latest.at)} je historická. Aktuální cenu nedostupného zboží neznáme.</div>` : ''}
    ${failed ? '<div class="detail-status warning">Poslední kontrola se nepodařila. Uvedená cena a srovnání vycházejí z posledního úspěšného načtení.</div>' : ''}
    ${entry.collection === 'pending' ? `<div class="detail-status">${live?'Odkaz je uložený. Čeká na ověření ceny.':'Odkaz je uložený. V tomto návrhu se živé ceny nenačítají; připojení sběru přijde s produkční integrací.'}</div>` : ''}
    ${entry.collection === 'unsupported' ? '<div class="detail-status">Tento obchod zatím neumíme načítat. Odkaz zůstává uložený.</div>' : ''}
    ${days ? `<div class="detail-quiet">${icon('muted')} Upozornění pozastavena ještě ${days} dní.<button class="text-button" data-action="unmute" data-id="${escape(entry.id)}">Obnovit</button></div><p class="chart-caption">Ceny se dál sbírají do historie.</p>` : ''}
    <div class="detail-actions">${entry.removedAt ? `<button class="button primary" data-action="restore" data-id="${escape(entry.id)}">${icon('restore')} Obnovit položku</button>` : entry.state === 'active' ? `<button class="button primary" data-action="purchase" data-id="${escape(entry.id)}">${icon('check')} Koupeno</button><button class="button secondary" data-action="edit" data-id="${escape(entry.id)}">${icon('edit')} Upravit</button>` : `<span class="detail-status">Tato položka už je koupená.</span><button class="button secondary" data-action="edit" data-id="${escape(entry.id)}">${icon('edit')} Upravit</button>`}</div>
    <section class="detail-section"><div class="section-title"><h3>Vývoj ceny</h3><div class="chart-tabs" aria-label="Období cenového grafu">${[[30,'Měsíc'],[90,'90 dní'],[365,'Rok'],[36500,'Vše']].map(([value,label])=>`<button data-range="${value}" aria-pressed="${range===value}">${label}</button>`).join('')}</div></div>${chart(entry)}
    <div class="detail-stat-grid"><div class="detail-stat"><small>Průměr za 90 dní</small><strong>${money(stats.average)}</strong></div><div class="detail-stat"><small>Minimum za 90 dní</small><strong>${money(stats.minimum)}</strong></div></div></section>
    ${entry.note ? `<section class="detail-section"><div class="section-title"><h3>Tvoje poznámka</h3></div><p class="detail-note">${escape(entry.note)}</p></section>` : ''}
    <section class="detail-section"><dl class="detail-meta"><div><dt>Přidáno</dt><dd>${date(entry.createdAt)}</dd></div><div><dt>Poslední úspěšná kontrola</dt><dd>${date(entry.checkedAt)}</dd></div><div><dt>Kategorie</dt><dd>${escape(entry.category)}</dd></div><div><dt>Nákup</dt><dd>${entry.mode === 'recurring' ? 'Opakovaný' : 'Jednorázový'}</dd></div></dl></section>
    ${entry.purchases.length ? `<section class="detail-section"><div class="section-title"><h3>Nákupy této položky</h3></div>${[...entry.purchases].reverse().map(p => `<div class="purchase-card ${p.voidedAt ? 'voided' : ''}"><div><strong>${date(p.date+'T12:00:00Z')}</strong><span>${p.unitCents==null?'Cena nezadaná':money(p.unitCents*p.quantity)}</span></div><p>${p.quantity} × ${money(p.unitCents)} za kus${p.voidedAt ? ' · Stornováno' : ''}<br>${p.reference == null || p.unitCents == null ? 'Srovnání zatím není známé.' : `Průměr při nákupu ${money(p.reference)} · ${p.reference >= p.unitCents ? 'Úspora' : 'Nad průměrem'} ${money(Math.abs(p.reference-p.unitCents)*p.quantity)}`}</p>${!p.voidedAt ? `<div class="purchase-edit-actions"><button class="text-button" data-action="correct" data-id="${escape(entry.id)}" data-purchase="${escape(p.id)}">Opravit</button><button class="text-button" ${p.canVoid===false?'disabled title="Předchozí stav nelze bezpečně obnovit po novější změně nebo u historického nákupu."':''} data-action="void" data-id="${escape(entry.id)}" data-purchase="${escape(p.id)}">Stornovat nákup</button></div>` : ''}</div>`).join('')}</section>` : ''}
    <section class="detail-section"><button class="button secondary" ${refreshing.has(entry.id)?'disabled':''} data-action="refresh" data-id="${escape(entry.id)}">${icon('refresh')} ${refreshing.has(entry.id)?'Kontroluji…':'Zkontrolovat cenu'}</button>${!entry.removedAt ? `<button class="text-button remove-link" data-action="remove" data-id="${escape(entry.id)}">Odstranit položku</button>` : ''}</section>`;
}
function field(label, name, value = '', attributes = '', help = '') {
  return `<label class="form-field"><span>${label}</span><input name="${name}" value="${escape(value)}" ${attributes}>${help ? `<small>${help}</small>` : ''}</label>`;
}
function select(label, name, choices, value) {
  return `<label class="form-field"><span>${label}</span><select name="${name}">${choices.map(([v,l])=>`<option value="${escape(v)}" ${v===value?'selected':''}>${escape(l)}</option>`).join('')}</select></label>`;
}
function openEditor(action, id = null, purchaseId = null) {
  formAction=action;formEntry=id;formPurchase=purchaseId;
  formRequestId=live?api.requestId():null;formVersion=id?entryById(id)?.version:null;
  const entry = id ? entryById(id) : null;
  $('#form-error').hidden=true;
  if (['purchase','correct'].includes(action)) {
    const p = entry.purchases.find(p=>String(p.id)===String(purchaseId));
    $('#editor-title').textContent=action==='correct'?'Opravit nákup':'Máš to doma!';
    $('#editor-eyebrow').textContent=entry.name;
    $('#editor-submit').textContent=action==='correct'?'Uložit opravu':'Potvrdit nákup';
    const latest=priceStats(entry).latest;
    $('#editor-fields').innerHTML=`<div class="form-pair">${field('Cena za kus · Kč','price',inputMoney(p?.unitCents??latest?.cents),'required inputmode="decimal" maxlength="11"')}${field('Počet kusů','quantity',p?.quantity??1,'required type="number" min="1" max="9999" step="1"')}</div>${field('Datum nákupu','date',p?.date??dayKey(new Date()),`required type="date" max="${dayKey(new Date())}"`)}<div class="purchase-total"><span>Celkem zaplaceno</span><strong id="purchase-total">—</strong></div><p class="purchase-reference">Cenu můžeš upravit podle skutečné platby. ${entry.mode==='recurring'?'Po nákupu pozastavíme upozornění na 30 dní od data nákupu. Ceny se dál sbírají.':'Po nákupu položka zmizí z aktuálního přehledu. Historie zůstane.'}</p>`;
    updatePurchaseTotal();
  } else {
    $('#editor-title').textContent=action==='add'?'Něco do hledáčku':'Upravit položku';
    $('#editor-eyebrow').textContent='TVŮJ WISHLIST';
    $('#editor-submit').textContent=action==='add'?'Přidat na wishlist':'Uložit změny';
    $('#editor-fields').innerHTML=field('Odkaz na výrobek','url',entry?.url||'',`required type="url" maxlength="4096" placeholder="https://obchod.cz/vyrobek" ${entry?'readonly':''}`,entry?'Odkaz zachovává identitu výrobku a jeho cenovou historii.':'Vlož odkaz na konkrétní výrobek a jeho variantu.')+
      field(action==='add'?'Název · nepovinný':'Název','name',entry?.name||'',`${entry?'required':''} maxlength="200" placeholder="Například lampa do pracovny"`)+
      `<div class="form-pair">${select('Kategorie','category',CATEGORIES.map(c=>[c,c]),entry?.category||'Nezařazeno')}${select('Nákup','mode',[['once','Jednorázově'],['recurring','Opakovaně']],entry?.mode||'once')}</div>`+
      `<label class="form-field"><span>Poznámka · nepovinná</span><textarea name="note" maxlength="2000" placeholder="Cokoliv, co si chceš pamatovat…">${escape(entry?.note||'')}</textarea></label>`;
  }
  if(!$('#editor').open)$('#editor').showModal();
}
function updatePurchaseTotal(){
  if(!$('#purchase-total'))return;
  try{const data=new FormData($('#editor-form'));const quantity=Number(data.get('quantity'));$('#purchase-total').textContent=Number.isInteger(quantity)&&quantity>0?money(parseMoney(data.get('price'))*quantity):'—';}catch{$('#purchase-total').textContent='—';}
}
$('#editor-form').addEventListener('input',updatePurchaseTotal);
$('#editor-form').addEventListener('submit',async event=>{
  event.preventDefault();const data=Object.fromEntries(new FormData(event.target));
  try{
    $('#editor-submit').disabled=true;
    if(live){await submitLiveForm(data);$('#editor').close();return;}
    if(formAction==='add'){commit(next=>next.entries.push(createEntry(data)));toast('Položka je přidaná do místní ukázky.');}
    else if(formAction==='edit'){delete data.url;commit(next=>updateEntry(next.entries.find(e=>e.id===formEntry),data));toast('Změny jsou uložené.');}
    else if(formAction==='purchase'){commit(next=>recordPurchase(next.entries.find(e=>e.id===formEntry),data));toast('Nákup je zapsaný.');}
    else if(formAction==='correct'){commit(next=>correctPurchase(next.entries.find(e=>e.id===formEntry),formPurchase,data));toast('Nákup je opravený.');}
    $('#editor').close();
  }catch(error){$('#form-error').textContent=error.message;$('#form-error').hidden=false;}
  finally{$('#editor-submit').disabled=false;}
});
function action(button){
  const id=button.dataset.id,kind=button.dataset.action;
  if(kind==='detail'){openDetail(id);return;}
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
  const close=event.target.closest('[data-close]');if(close){$('#'+close.dataset.close).close();return;}
  const button=event.target.closest('[data-action]');if(button){action(button);return;}
  const tab=event.target.closest('[data-range]');if(tab){range=Number(tab.dataset.range);renderDetail();$(`[data-range="${range}"]`)?.focus();}
});
for(const dialog of document.querySelectorAll('dialog')){
  dialog.addEventListener('click',event=>{
    if(event.target!==dialog)return;
    const rect=dialog.getBoundingClientRect();
    if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();
  });
  dialog.addEventListener('close',()=>{document.body.style.overflow=document.querySelector('dialog[open]')?'hidden':'';});
}
new MutationObserver(()=>{document.body.style.overflow=document.querySelector('dialog[open]')?'hidden':'';}).observe(document.body,{subtree:true,attributes:true,attributeFilter:['open']});
$('#add-button').onclick=()=>openEditor('add');
$('#history-info').onclick=()=>$('#info-dialog').showModal();
$('#reset-filters').onclick=resetFilters;
$('#search').addEventListener('input',render);
for(const select of document.querySelectorAll('.filter-controls select'))select.addEventListener('change',render);
$('#toast-close').onclick=()=>{$('#toast').hidden=true;undoAction=null;};
$('#toast-undo').onclick=async()=>{const undo=undoAction;undoAction=null;try{if(undo)await undo();}catch(error){toast(error.message);}};
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
async function loadLive() {
  const entries=await api.list();
  if(selected && $('#detail').open){
    const detail=await api.detail(selected);
    const index=entries.findIndex(e=>e.id===selected);if(index>=0)entries[index]=detail;
  }
  state={version:1,entries};loading=false;loadError=null;render();
  if(selected && $('#detail').open)renderDetail();
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
  await loadLive();toast(operation==='add'?'Položka je uložená.':operation==='update'?'Změny jsou uložené.':'Nákup je uložený.');
  if(operation==='add' && result.entry_id)pollRefresh(result.entry_id).catch(error=>toast(error.message));
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
async function pollRefresh(id) {
  if(refreshing.has(id))return;
  refreshing.add(id);if(selected===id && $('#detail').open)renderDetail();
  const start=entryById(id)?.lastAttemptAt;
  try{
    for(let attempt=0;attempt<35;attempt++){
      await new Promise(resolve=>setTimeout(resolve,2000));
      const response=await api.command('get',{entry_id:id}),entry=api.toUI(response.data.entry);
      if(entry.lastAttemptAt && entry.lastAttemptAt!==start){
        await loadLive();toast(entry.collection==='ok'?'Kontrola ceny je dokončená.':'Kontrola skončila; výsledek najdeš v detailu.');return;
      }
    }
    toast('Kontrola zatím čeká na dokončení. Výsledek se objeví v přehledu.');
  }finally{refreshing.delete(id);if(selected===id && $('#detail').open)renderDetail();}
}
if(live){
  $('.preview-note').hidden=true;
  loadLive().catch(showLoadError);
  setInterval(()=>{if(!document.hidden&&!document.querySelector('dialog[open]')&&!loading)loadLive().catch(showLoadError);},30000);
}
