/** Local interaction model. Money is stored as integer haléře; no shop requests. */
export const categoryNames = {
  uncategorized:'Nezařazeno', home:'Domácnost', personal_care:'Drogerie', gifts:'Dárky',
  kitchen:'Kuchyně', clothing:'Oblečení',
};
export const CATEGORIES = Object.values(categoryNames);
export const ZONE = 'Europe/Prague';
export function money(value) {
  if (value == null) return '—';
  if (typeof value === 'number') return new Intl.NumberFormat('cs-CZ', {
    style:'currency', currency:'CZK', maximumFractionDigits:value % 100 ? 2 : 0,
  }).format(value / 100);
  const cents = BigInt(value), remainder = (cents < 0n ? -cents : cents) % 100n;
  const whole = cents / 100n, digits = remainder ? 2 : 0;
  return new Intl.NumberFormat('cs-CZ', {style:'currency',currency:'CZK',minimumFractionDigits:digits,maximumFractionDigits:digits})
    .formatToParts(cents < 0n && whole === 0n ? -0 : whole)
    .map(part => part.type === 'fraction' ? String(remainder).padStart(2,'0') : part.value).join('');
}
export const dayKey = value => new Intl.DateTimeFormat('en-CA', {
  timeZone: ZONE, year: 'numeric', month: '2-digit', day: '2-digit',
}).format(new Date(value));
export const dayShift = (key, days) => new Date(Date.parse(key + 'T12:00:00Z') + days * 86400000).toISOString().slice(0, 10);
export function parseMoney(input) {
  const text = String(input).trim().replace(',', '.');
  if (!/^\d{1,8}(?:\.\d{1,2})?$/.test(text)) throw new Error('Zadej cenu v Kč, nejvýše na dvě desetinná místa.');
  const [whole, fraction = ''] = text.split('.');
  return Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
}
export const inputMoney = cents => cents == null ? '' : (cents / 100).toFixed(2);
export function validURL(input) {
  let url;
  try { url = new URL(input); } catch { throw new Error('Vlož úplný platný odkaz začínající https://.'); }
  if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443')) {
    throw new Error('Vlož úplný odkaz začínající https:// bez přihlašovacích údajů.');
  }
  return url.href;
}
export function priceStats(entry, at = new Date().toISOString(), days = 90) {
  const end = dayKey(at), start = dayShift(end, 1 - days);
  const buckets = new Map();
  for (const observation of entry.observations) {
    const date = dayKey(observation.at);
    if (observation.at > at || date < start || date > end || !Number.isSafeInteger(observation.cents)) continue;
    const values = buckets.get(date) || [];
    values.push(observation.cents);
    buckets.set(date, values);
  }
  const daily = [...buckets].sort(([a], [b]) => a.localeCompare(b)).map(([date, values]) => ({
    date, cents: values.reduce((a, b) => a + b, 0) / values.length,
  }));
  const observations = entry.observations.filter(o => o.at <= at).sort((a, b) => a.at.localeCompare(b.at));
  const latest = observations.at(-1) || null;
  const average = daily.length ? Math.round(daily.reduce((sum, d) => sum + d.cents, 0) / daily.length) : null;
  const prices = entry.observations.filter(o => o.at <= at && dayKey(o.at) >= start).map(o => o.cents);
  const minimum = prices.length ? Math.min(...prices) : null;
  const unavailable = entry.availability === 'out_of_stock';
  const current = unavailable ? null : latest?.cents ?? null;
  const saving = current != null && average != null ? average - current : null;
  return {daily, average, minimum, latest, current, saving,
    percentage: saving != null && average > 0 ? saving / average * 100 : null,
    ...(days === 90 && entry.stats90 ? entry.stats90 : {})};
}
export function comparison(stats) {
  if (stats.percentage == null) return 'Bez srovnání';
  const percent = Math.abs(stats.percentage);
  if (percent < 0.05) return 'Na průměru';
  const value = new Intl.NumberFormat('cs-CZ', {maximumFractionDigits: 1}).format(percent);
  return `O ${value} % ${stats.percentage > 0 ? 'levnější' : 'dražší'}`;
}
export function remainingDays(entry, now = new Date()) {
  return entry.mutedUntil ? Math.max(0, Math.ceil((Date.parse(entry.mutedUntil) - now.getTime()) / 86400000)) : 0;
}
export function entryMatches(entry, filters) {
  const state = entry.removedAt ? 'removed' : entry.state;
  if (filters.state === 'default' ? !['active'].includes(state) : filters.state !== 'all' && state !== filters.state) return false;
  if (filters.state === 'all' && entry.removedAt) return false;
  if (filters.category && entry.category !== filters.category) return false;
  if (filters.shop && new URL(entry.url).hostname !== filters.shop) return false;
  if (filters.mode && entry.mode !== filters.mode) return false;
  if (filters.stock && entry.availability !== filters.stock) return false;
  return entry.name.toLocaleLowerCase('cs').includes(filters.search.trim().toLocaleLowerCase('cs'));
}
export const RECENT_DAYS = 7;

export function signals(entry, now = new Date().toISOString()) {
  const current = entry.availability === 'out_of_stock' || entry.availability === 'discontinued'
    ? null : (entry.stats90?.current ?? entry.observations.at(-1)?.cents ?? null);
  const verified = entry.collection === 'ok';
  let observed = entry.stats90?.price_change ?? null;
  if (!entry.stats90 && entry.observations.length > 1) {
    // The standalone preview owns its entire price history.
    let previous = entry.observations[0];
    observed = {at:previous.at, delta:0};
    for (const observation of entry.observations.slice(1)) {
      if (observation.cents !== previous.cents) {
        observed = {at:observation.at, delta:observation.cents - previous.cents};
      }
      previous = observation;
    }
  }
  const recent = at => {
    const age = Date.parse(now) - Date.parse(at);
    return age >= 0 && age < RECENT_DAYS * 86400000;
  };
  const change = verified && current != null && observed ? {
    ...observed, recent:observed.delta !== 0 && recent(observed.at),
  } : null;
  const stockChange = verified && recent(entry.stockChange?.at) ? entry.stockChange : null;
  return {current, change, stockChange};
}

export function visibleEntries(entries, filters, now = new Date().toISOString(), measured = null) {
  const data = measured ?? new Map(entries.map(entry => [entry.id, signals(entry, now)]));
  const recentAt = value => [value.change?.recent ? value.change.at : '', value.stockChange?.at || ''].sort().at(-1);
  return entries.filter(entry => {
    if (!entryMatches(entry,filters)) return false;
    const value = data.get(entry.id);
    if (filters.event === 'drop') return value.change?.recent && value.change.delta < 0;
    if (filters.event === 'rise') return value.change?.recent && value.change.delta > 0;
    if (filters.event === 'stock') return Boolean(value.stockChange);
    return true;
  }).sort((a, b) => {
    const group = CATEGORIES.indexOf(a.category) - CATEGORIES.indexOf(b.category);
    if (group) return group;
    let order = 0;
    if (filters.sort === 'price') order = (data.get(a.id).current ?? Infinity) - (data.get(b.id).current ?? Infinity);
    if (filters.sort === 'newest') order = b.createdAt.localeCompare(a.createdAt);
    if (filters.sort === 'recent') order = recentAt(data.get(b.id)).localeCompare(recentAt(data.get(a.id)));
    return order || a.name.localeCompare(b.name, 'cs');
  });
}

export function historyCoverage(entry, at, range) {
  const end = dayKey(at), windowStart = dayShift(end, 1 - range);
  const added = dayKey(entry.createdAt);
  const start = added > windowStart ? added : windowStart;
  const observations = entry.observations.filter(observation => observation.at <= at && dayKey(observation.at) >= start);
  const days = new Set(observations.map(observation => dayKey(observation.at))).size;
  const missing = Math.max(0, Math.round((Date.parse(end) - Date.parse(start)) / 86400000) + 1 - days);
  return {start, end, count:observations.length, days, missing};
}
export function statisticsPeriod(period, anchor) {
  if (!['month','year','all'].includes(period) || !/^\d{4}-\d{2}-\d{2}$/.test(anchor)
      || new Date(anchor+'T12:00:00Z').toISOString().slice(0,10) !== anchor) throw new Error('Neplatné období.');
  if (period === 'all') return {start:null,end:null};
  const year = anchor.slice(0,4), month = anchor.slice(5,7);
  const next = new Date(anchor+'T12:00:00Z'); next.setUTCDate(1); next.setUTCMonth(next.getUTCMonth()+1);
  return period === 'year' ? {start:year+'-01-01',end:year+'-12-31'}
    : {start:year+'-'+month+'-01',end:dayShift(next.toISOString().slice(0,10),-1)};
}
export function shiftStatisticsDate(anchor, period, offset) {
  const [year,month] = anchor.split('-').map(Number);
  const shifted = new Date(anchor+'T12:00:00Z');
  shifted.setUTCDate(1);
  if (period === 'year') shifted.setUTCFullYear(year+offset);
  else shifted.setUTCMonth(month-1+offset);
  return shifted.toISOString().slice(0,10);
}
export function purchaseStatistics(entries, {period='month',date=dayKey(new Date()),filters}) {
  const {start,end} = statisticsPeriod(period,date);
  const empty = () => ({count:0,unknown_price:0,unknown_reference:0,spending:0n,savings:0n});
  const groups = new Map(CATEGORIES.filter(c => !filters.category || filters.category===c).map(c=>[c,empty()]));
  for (const entry of entries) {
    if (!entryMatches(entry,{...filters,category:''})) continue;
    for (const purchase of entry.purchases) {
      const group = groups.get(purchase.category || 'Nezařazeno');
      if (!group || purchase.voidedAt || start && (purchase.date < start || purchase.date > end)) continue;
      group.count++;
      if (purchase.unitCents == null) group.unknown_price++;
      else {
        group.spending += BigInt(purchase.unitCents)*BigInt(purchase.quantity);
        if (purchase.reference == null) group.unknown_reference++;
        else group.savings += (BigInt(purchase.reference)-BigInt(purchase.unitCents))*BigInt(purchase.quantity);
      }
    }
  }
  const total=empty();
  for (const group of groups.values()) for (const key of Object.keys(total)) total[key]+=group[key];
  const project=group=>({...group,
    spending:!group.count || group.count>group.unknown_price ? String(group.spending) : null,
    savings:!group.count || group.count>group.unknown_price+group.unknown_reference ? String(group.savings) : null});
  return {period,date,start,end,total:project(total),categories:[...groups].map(([category,group])=>({category,...project(group)}))};
}
export function createEntry({url, name, category, mode, note = ''}, now = new Date().toISOString()) {
  if (!CATEGORIES.includes(category) || !['once', 'recurring'].includes(mode)) throw new Error('Vyber kategorii a způsob nákupu.');
  return {id: crypto.randomUUID(), name: name.trim() || new URL(url).hostname, url: validURL(url), category, mode,
    note: note.trim(), createdAt: now, checkedAt: null, availability: 'unknown', collection: 'pending',
    observations: [], purchases: [], state: 'active', mutedUntil: null, removedAt: null, lifecycleRevision: 0};
}
export function updateEntry(entry, patch) {
  if (Object.keys(patch).some(key => !['name', 'category', 'mode', 'note'].includes(key))) throw new Error('Odkaz položky nelze změnit.');
  if (!patch.name?.trim() || !CATEGORIES.includes(patch.category) || !['once', 'recurring'].includes(patch.mode)) {
    throw new Error('Vyplň název, kategorii a způsob nákupu.');
  }
  Object.assign(entry, {...patch, name: patch.name.trim(), note: (patch.note || '').trim()});
}
export function endOfDay(date) {
  // Find the end of a Prague calendar day, including both DST transitions.
  let low = Date.parse(date + 'T00:00:00Z') - 86400000;
  let high = low + 3 * 86400000;
  while (high - low > 1) {
    const middle = Math.floor((low + high) / 2);
    if (dayKey(middle) <= date) low = middle; else high = middle;
  }
  return new Date(low).toISOString();
}
function purchaseValues(entry, values, now) {
  const quantity = Number(values.quantity);
  if (!Number.isInteger(quantity) || quantity < 1 || quantity > 9999) throw new Error('Počet kusů musí být celé číslo od 1 do 9 999.');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(values.date) || values.date > dayKey(now)
      || dayKey(values.date + 'T12:00:00Z') !== values.date) throw new Error('Vyber platné datum, nejpozději dnes.');
  const at = values.date === dayKey(now) ? now : endOfDay(values.date);
  // Reference is captured once at the purchase date, never recalculated on reads.
  const reference = priceStats(entry, at).average;
  return {quantity, unitCents: parseMoney(values.price), date: values.date, reference};
}
export function recordPurchase(entry, values, now = new Date().toISOString()) {
  if (entry.removedAt || entry.state !== 'active') throw new Error('Tuto položku teď nelze označit jako koupenou.');
  const purchase = {id: crypto.randomUUID(), ...purchaseValues(entry, values, now), category:entry.category, createdAt: now,
    previous: {state: entry.state, mutedUntil: entry.mutedUntil}, voidedAt: null};
  entry.lifecycleRevision++;
  purchase.lifecycleRevision = entry.lifecycleRevision;
  entry.purchases.push(purchase);
  if (entry.mode === 'once') {entry.state = 'purchased'; entry.mutedUntil = null;}
  else entry.mutedUntil = new Date(Date.parse(purchase.date + 'T12:00:00Z') + 30 * 86400000).toISOString();
  return purchase;
}
export function correctPurchase(entry, id, values, now = new Date().toISOString()) {
  const purchase = entry.purchases.find(p => p.id === id && !p.voidedAt);
  if (!purchase) throw new Error('Nákup už není k dispozici.');
  const updated = purchaseValues(entry, values, now);
  // Changing only the amount/quantity preserves the original reference.
  if (updated.date === purchase.date) updated.reference = purchase.reference;
  Object.assign(purchase, updated, {correctedAt: now});
  if (entry.mode === 'recurring' && entry.lifecycleRevision === purchase.lifecycleRevision) {
    entry.mutedUntil = new Date(Date.parse(purchase.date + 'T12:00:00Z') + 30 * 86400000).toISOString();
  }
}
export function voidPurchase(entry, id, now = new Date().toISOString()) {
  const purchase = entry.purchases.find(p => p.id === id && !p.voidedAt);
  if (!purchase) throw new Error('Nákup už není k dispozici.');
  if (entry.lifecycleRevision !== purchase.lifecycleRevision) throw new Error('Od nákupu se změnil stav položky. Nejdřív vrať pozdější změny; storno nesmí přepsat novější stav.');
  purchase.voidedAt = now;
  Object.assign(entry, purchase.previous);
  entry.lifecycleRevision++;
  // Permit cancelling an earlier purchase after the latest one has been cancelled.
  const prior = [...entry.purchases].reverse().find(p => !p.voidedAt);
  if (prior) prior.lifecycleRevision = entry.lifecycleRevision;
}
export function removeEntry(entry, now = new Date().toISOString()) {
  if (entry.removedAt) return;
  entry.removedAt = now;
  entry.lifecycleRevision++;
}
export function restoreEntry(entry) {
  if (!entry.removedAt) return;
  entry.removedAt = null;
  entry.lifecycleRevision++;
  const latest = [...entry.purchases].reverse().find(p => !p.voidedAt);
  if (latest) latest.lifecycleRevision = entry.lifecycleRevision;
}
export function resumeNotifications(entry) {
  entry.mutedUntil = null;
  entry.lifecycleRevision++;
}
