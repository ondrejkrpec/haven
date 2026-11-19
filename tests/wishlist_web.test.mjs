import test from 'node:test';
import assert from 'node:assert/strict';
import {createEntry, priceStats, comparison, parseMoney, endOfDay, recordPurchase,
  correctPurchase, voidPurchase, removeEntry, restoreEntry, resumeNotifications,
  remainingDays, visibleEntries, updateEntry, purchaseStatistics, statisticsPeriod,
  shiftStatisticsDate, money} from '../web/wishlist/model.mjs';

const now = '2026-11-18T12:00:00.000Z';
function entry(mode = 'once') {
  return createEntry({url:'https://shop.example.com/product', name:'Test product', category:'Domácnost', mode}, now);
}
const filters = {state:'default',search:'',shop:'',category:'',stock:'',mode:''};
test('first observed price immediately establishes the average and minimum', () => {
  const e = entry(); e.observations.push({at:now,cents:80000});
  const stats = priceStats(e,now);
  assert.equal(stats.average,80000); assert.equal(stats.minimum,80000); assert.equal(stats.saving,0);
  assert.equal(comparison(stats),'Na průměru');
});
test('calendar days have equal weight and missing dates add no invented price', () => {
  const e = entry();e.observations=[
    {at:'2026-11-16T08:00:00.000Z',cents:10000},
    {at:'2026-11-16T16:00:00.000Z',cents:20000},
    {at:now,cents:30000},
    {at:'2026-08-01T12:00:00.000Z',cents:100},
    {at:'2026-11-19T12:00:00.000Z',cents:100},
  ];
  const stats=priceStats(e,now);
  assert.equal(stats.average,22500);assert.equal(stats.daily.length,2);assert.equal(stats.minimum,10000);
  assert.equal(stats.latest.cents,30000);assert.equal(comparison(stats),'O 33,3 % dražší');
});
test('Prague days end correctly in winter, summer and at clock changes', () => {
  assert.equal(endOfDay('2026-11-18'),'2026-11-18T22:59:59.999Z');
  assert.equal(endOfDay('2026-07-18'),'2026-07-18T21:59:59.999Z');
  assert.equal(endOfDay('2026-03-29'),'2026-03-29T21:59:59.999Z');
  assert.equal(endOfDay('2026-10-25'),'2026-10-25T22:59:59.999Z');
});
test('unavailable stock hides current price while retaining priced history', () => {
  const e=entry();e.observations=[{at:now,cents:80000}];e.availability='out_of_stock';
  const stats=priceStats(e,now);assert.equal(stats.current,null);assert.equal(stats.saving,null);
  assert.equal(stats.average,80000);assert.equal(stats.latest.cents,80000);
});
test('failed collection retains the last successfully observed price', () => {
  const e=entry();e.observations=[{at:now,cents:80000}];e.collection='failed';
  assert.equal(priceStats(e,now).current,80000);
});
test('money stays in exact integer haléře and malformed amounts are rejected', () => {
  assert.equal(parseMoney('120,35'),12035);assert.equal(parseMoney('0'),0);
  for(const text of ['1.234','-1','Infinity','1e3','','100000000'])assert.throws(()=>parseMoney(text));
});
test('purchase captures quantity and reference; later prices cannot rewrite saved savings', () => {
  const e=entry();e.observations=[{at:now,cents:100000}];
  const p=recordPurchase(e,{price:'800',quantity:'2',date:'2026-11-18'},now);
  assert.equal(p.unitCents*p.quantity,160000);assert.equal((p.reference-p.unitCents)*p.quantity,40000);
  assert.equal(e.state,'purchased');
  e.observations.push({at:'2026-11-19T12:00:00.000Z',cents:200000});
  correctPurchase(e,p.id,{price:'750',quantity:'2',date:'2026-11-18'},'2026-11-20T12:00:00.000Z');
  assert.equal(p.reference,100000);assert.equal(p.unitCents,75000);
});
test('backdated purchase uses only observations available by that Prague calendar date', () => {
  const e=entry();e.observations=[{at:'2026-07-17T21:00:00.000Z',cents:10000},
    {at:'2026-07-18T21:30:00.000Z',cents:20000},{at:'2026-07-18T22:30:00.000Z',cents:90000}];
  const p=recordPurchase(e,{price:'150',quantity:1,date:'2026-07-18'},now);
  assert.equal(p.reference,15000);
});
test('recurring purchase mutes notifications while keeping entry collectible and visible', () => {
  const e=entry('recurring');recordPurchase(e,{price:'120',quantity:'3',date:'2026-11-18'},now);
  assert.equal(e.state,'active');assert.equal(remainingDays(e,new Date(now)),30);
  assert.equal(visibleEntries([e],filters,now).length,1);
  resumeNotifications(e);assert.equal(e.mutedUntil,null);assert.equal(e.state,'active');
});
test('void restores lifecycle; removal and restoration never reopen a purchased item', () => {
  const e=entry();const p=recordPurchase(e,{price:'120',quantity:1,date:'2026-11-18'},now);
  removeEntry(e,now);restoreEntry(e);assert.equal(e.state,'purchased');
  voidPurchase(e,p.id,now);assert.equal(e.state,'active');assert.ok(p.voidedAt);
});
test('void refuses to overwrite later lifecycle changes', () => {
  const e=entry('recurring');const p=recordPurchase(e,{price:'120',quantity:1,date:'2026-11-18'},now);
  resumeNotifications(e);assert.throws(()=>voidPurchase(e,p.id,now));assert.equal(p.voidedAt,null);
});
test('multiple recurring purchases can be voided newest first without losing original state', () => {
  const e=entry('recurring');const a=recordPurchase(e,{price:'120',quantity:1,date:'2026-11-18'},now);
  const b=recordPurchase(e,{price:'110',quantity:1,date:'2026-11-18'},now);
  voidPurchase(e,b.id,now);voidPurchase(e,a.id,now);assert.equal(e.state,'active');assert.equal(e.mutedUntil,null);
});
test('invalid quantity and future dates cannot partially record a purchase', () => {
  for(const values of [{price:'1',quantity:0,date:'2026-11-18'},{price:'1',quantity:1.2,date:'2026-11-18'},
    {price:'1',quantity:1,date:'2026-11-19'},{price:'1',quantity:1,date:'2026-02-30'}]){
    const e=entry();assert.throws(()=>recordPurchase(e,values,now));assert.equal(e.purchases.length,0);assert.equal(e.state,'active');
  }
});
test('source URL is immutable and text search is case insensitive', () => {
  const e=entry();assert.throws(()=>updateEntry(e,{name:'Other',category:'Dárky',mode:'once',url:'https://other.example.com/'}));
  assert.equal(visibleEntries([e],{...filters,search:'TEST'},now).length,1);
  e.state='purchased';assert.equal(visibleEntries([e],filters,now).length,0);
  removeEntry(e);assert.equal(visibleEntries([e],{...filters,state:'removed'},now).length,1);
});
test('purchase totals use saved categories, current filters and exclude voids', () => {
  const e=entry('recurring');e.observations=[{at:now,cents:100000}];
  const p=recordPurchase(e,{price:'800',quantity:2,date:'2026-11-18'},now);
  updateEntry(e,{name:e.name,category:'Dárky',mode:'recurring',note:''});
  const options={period:'month',date:'2026-11-01',filters:{...filters,category:'Domácnost'}};
  assert.equal(purchaseStatistics([e],options).total.spending,'160000');
  assert.equal(purchaseStatistics([e],options).total.savings,'40000');
  assert.equal(purchaseStatistics([e],{...options,filters:{...filters,category:'Dárky'}}).total.count,0);
  assert.equal(purchaseStatistics([e],{...options,filters:{...filters,search:'other'}}).total.count,0);
  correctPurchase(e,p.id,{price:'1200',quantity:3,date:'2026-11-18'},now);
  assert.equal(purchaseStatistics([e],options).total.savings,'-60000');
  voidPurchase(e,p.id,now);assert.equal(purchaseStatistics([e],options).total.count,0);
});
test('unknown price, missing reference and empty selection never invent known totals', () => {
  const e=entry('recurring');
  e.purchases=[{date:'2026-11-18',quantity:1,unitCents:null,reference:null},
    {date:'2026-11-18',quantity:2,unitCents:10000,reference:null}];
  const options={period:'month',date:'2026-11-01',filters};
  let result=purchaseStatistics([e],options);
  assert.equal(result.categories[0].category,'Nezařazeno');
  assert.deepEqual(result.total,{count:2,unknown_price:1,unknown_reference:1,spending:'20000',savings:null});
  e.purchases.pop();result=purchaseStatistics([e],options);
  assert.equal(result.total.spending,null);assert.equal(result.total.savings,null);
  e.purchases=[];result=purchaseStatistics([e],options);
  assert.equal(result.total.spending,'0');assert.equal(result.total.savings,'0');
});
test('period boundaries cover leap years and navigation crosses years without day clipping', () => {
  assert.deepEqual(statisticsPeriod('month','2024-02-01'),{start:'2024-02-01',end:'2024-02-29'});
  assert.deepEqual(statisticsPeriod('year','2026-11-01'),{start:'2026-01-01',end:'2026-12-31'});
  assert.deepEqual(statisticsPeriod('all','2026-11-01'),{start:null,end:null});
  assert.equal(shiftStatisticsDate('2026-01-31','month',-1),'2025-12-01');
  assert.equal(shiftStatisticsDate('2024-02-29','year',1),'2025-02-01');
  assert.throws(()=>statisticsPeriod('month','2026-02-30'));
});
test('spending totals filter purchase dates rather than creation date', () => {
  const e=entry('recurring');
  for(const date of ['2025-11-18','2026-10-01','2026-11-01']) recordPurchase(e,{price:'10',quantity:1,date},now);
  const options={period:'month',date:'2026-11-01',filters};
  assert.equal(purchaseStatistics([e],options).total.count,1);
  assert.equal(purchaseStatistics([e],{...options,period:'year'}).total.count,2);
  assert.equal(purchaseStatistics([e],{...options,period:'all'}).total.count,3);
});
test('totals and money formatting preserve halere beyond safe integers', () => {
  const e=entry('recurring');
  e.purchases=Array.from({length:101},()=>({category:'Domácnost',unitCents:9999999999,quantity:9999,date:'2026-11-18',reference:0}));
  const result=purchaseStatistics([e],{period:'all',date:'2026-11-01',filters});
  const expected=9999999999n*9999n*101n;
  assert.equal(result.total.spending,String(expected));assert.equal(result.total.savings,String(-expected));
  assert.equal(money(String(expected)).replace(/\s/g,''),'100989899989901,01Kč');
  assert.equal(money('-1').replace(/\s/g,''),'-0,01Kč');
  assert.equal(money(null),'—');
});
test('production adapter requests aggregate statistics without loading entry details', async () => {
  const originalFetch=globalThis.fetch,originalLocation=globalThis.location;
  const calls=[];
  globalThis.location={origin:'https://haven.example.ts.net'};
  globalThis.fetch=async(url,options)=>{
    calls.push({url:String(url),options});
    return {status:200,json:async()=>String(url).endsWith('bootstrap')?{code:'ok',csrf:'a'.repeat(64)}
      :{code:'ok',data:{total:{count:2,spending:'1000',savings:null},categories:[{category:'home',count:2}]}}};
  };
  try {
    const api=await import('../web/wishlist/api.mjs');
    const result=await api.statistics({period:'year',date:'2026-01-01',filters:{...filters,category:'Domácnost'}});
    assert.equal(result.categories[0].category,'Domácnost');
    assert.equal(calls.length,2);
    const request=JSON.parse(calls[1].options.body);
    assert.equal(request.operation,'web.statistics');assert.equal(request.arguments.filters.category,'home');
  } finally {globalThis.fetch=originalFetch;globalThis.location=originalLocation;}
});
