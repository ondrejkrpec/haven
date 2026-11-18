/** Local interaction model. Money is stored as integer haléře; no shop requests. */
export const CATEGORIES = ['Nezařazeno', 'Domácnost', 'Drogerie', 'Dárky'];
export const ZONE = 'Europe/Prague';
export const money = value => value == null ? '—' : new Intl.NumberFormat('cs-CZ', {
  style: 'currency', currency: 'CZK', maximumFractionDigits: value % 100 ? 2 : 0,
}).format(value / 100);
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
export function visibleEntries(entries, filters, now = new Date().toISOString()) {
  return entries.filter(entry => {
    const state = entry.removedAt ? 'removed' : entry.state;
    if (filters.state === 'default' ? !['active'].includes(state) : filters.state !== 'all' && state !== filters.state) return false;
    if (filters.state === 'all' && entry.removedAt) return false;
    if (filters.category && entry.category !== filters.category) return false;
    if (filters.shop && new URL(entry.url).hostname !== filters.shop) return false;
    if (filters.mode && entry.mode !== filters.mode) return false;
    if (filters.stock && entry.availability !== filters.stock) return false;
    return entry.name.toLocaleLowerCase('cs').includes(filters.search.trim().toLocaleLowerCase('cs'));
  }).sort((a, b) => {
    const group = CATEGORIES.indexOf(a.category) - CATEGORIES.indexOf(b.category);
    if (group) return group;
    const sa = priceStats(a, now), sb = priceStats(b, now);
    return (sb.percentage ?? -Infinity) - (sa.percentage ?? -Infinity)
      || (sb.saving ?? -Infinity) - (sa.saving ?? -Infinity) || a.name.localeCompare(b.name, 'cs');
  });
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
  const purchase = {id: crypto.randomUUID(), ...purchaseValues(entry, values, now), createdAt: now,
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
