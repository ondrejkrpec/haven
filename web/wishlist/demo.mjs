import {dayKey, dayShift} from './model.mjs';

/** Entirely synthetic products, shops, prices and purchases. */
export function demoState(now = new Date()) {
  const today = dayKey(now);
  const specs = [
    ['Keramická stolní lampa', 'Domácnost', 'once', 189000, 139000, 'in_stock', 'ok', 'Příjemné teplé světlo do pracovny.'],
    ['Nerezová pánev · 24 cm', 'Domácnost', 'once', 249000, 199000, 'in_stock', 'ok', ''],
    ['Lněné povlečení · přírodní', 'Domácnost', 'once', 289000, 299000, 'in_stock', 'failed', 'Rozměr 140 × 200 cm.'],
    ['Jemný sprchový gel · 500 ml', 'Drogerie', 'recurring', 22900, 15900, 'in_stock', 'ok', ''],
    ['Náhradní hlavice · 4 kusy', 'Drogerie', 'recurring', 64900, 54900, 'in_stock', 'ok', ''],
    ['Vlněná šála · tmavě zelená', 'Dárky', 'once', 129000, 99000, 'out_of_stock', 'ok', ''],
    ['Kniha o zahradě', 'Dárky', 'once', 59900, 44900, 'in_stock', 'ok', ''],
  ];
  const entries = specs.map(([name, category, mode, base, last, availability, collection, note], index) => {
    const observations = Array.from({length: 73 - index * 4}, (_, day) => {
      const delta = Math.round(Math.sin(day / 5 + index) * base * .025 / 100) * 100;
      const cents = day > 55 - index * 4 ? last : base + delta;
      return {at: dayShift(today, day - (72 - index * 4)) + 'T08:00:00.000Z', cents};
    }).filter(o => Date.parse(o.at) <= now.getTime());
    if (collection === 'failed') observations.pop();
    return {id: `example-${index}`, name, category, mode, note,
      url: `https://${index % 2 ? 'shop' : 'store'}.example.com/products/item-${index + 1}`,
      createdAt: observations[0].at, checkedAt: availability === 'out_of_stock' ? now.toISOString() : observations.at(-1).at,
      availability, collection, observations, purchases: [], state: 'active', removedAt: null,
      mutedUntil: index === 4 ? new Date(now.getTime() + 23 * 86400000).toISOString() : null, lifecycleRevision: 0};
  });
  return {version: 1, entries};
}
