/** Same-origin production adapter. It never loads or persists demonstration data. */
export const categoryNames = {uncategorized:'Nezařazeno',home:'Domácnost',personal_care:'Drogerie',gifts:'Dárky'};
export const categoryCode = name => Object.entries(categoryNames).find(([, label]) => label === name)?.[0] || 'uncategorized';
let csrf;
export const requestId = () => crypto.randomUUID().replaceAll('-', '');
const messages = {
  denied:'K tomuto wishlistu nemáš přístup. Ověř připojení ke svému Tailscale účtu.',
  conflict:'Položka se mezitím změnila. Otevři její aktuální detail a změnu zadej znovu.',
  not_found:'Položka nebo nákup už nejsou k dispozici. Obnov přehled.',
  invalid_request:'Zadané údaje nelze uložit. Zkontroluj cenu, množství a datum.',
  unavailable:'Výsledek se nepodařilo ověřit. Zkontroluj přehled; opakování stejného požadavku změnu nezdvojí.',
};
async function http(path, options = {}) {
  const {signal, timeout = 12000, ...requestOptions} = options;
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeout);
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  signal?.addEventListener('abort', abort, {once:true});
  try {
    const response = await fetch(new URL('/wishlist/api/' + path, location.origin), {...requestOptions, credentials:'same-origin', cache:'no-store', signal:controller.signal});
    const result = await response.json();
    if (response.status >= 500) throw Object.assign(new Error(messages.unavailable), {code:'unavailable',uncertain:true});
    return result;
  } catch (error) {
    if (error.code) throw error;
    throw Object.assign(new Error(messages.unavailable), {code:'unavailable',uncertain:true});
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}
export async function bootstrap() {
  const response = await http('bootstrap');
  if (response.code !== 'ok' || !/^[a-f0-9]{64}$/.test(response.csrf)) throw Object.assign(new Error(messages.denied), {code:'denied'});
  csrf = response.csrf;
}
export async function command(operation, arguments_, id = requestId(), retry = true, signal) {
  if (!csrf) await bootstrap();
  const response = await http('command', {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
    body:JSON.stringify({protocol_version:1,operation:'web.'+operation,arguments:arguments_,request_id:id}),
    signal,timeout:operation === 'preview' ? 18000 : 12000});
  if (response.code === 'csrf_expired' && retry && !signal?.aborted) {
    await bootstrap();
    return command(operation,arguments_,id,false,signal);
  }
  if (response.code !== 'ok') throw Object.assign(new Error(messages[response.code] || messages.unavailable), {code:response.code,uncertain:response.code==='unavailable'});
  return response;
}
export async function preview(url, signal) {
  const response = await command('preview', {url}, requestId(), true, signal);
  return response.data;
}
export function toUI(entry) {
  const observation = entry.latest_observation;
  const latest = observation ? {at:observation.observed_at,cents:Math.round(Number(observation.price)*100)} : null;
  return {id:entry.entry_id,version:entry.version,name:entry.label || new URL(entry.url).hostname,url:entry.url,
    category:categoryNames[entry.category] || 'Nezařazeno',mode:entry.purchase_mode,note:entry.note || '',
    state:entry.state,removedAt:entry.removed_at,mutedUntil:entry.notifications_muted_until,createdAt:entry.created_at,
    checkedAt:entry.latest_availability?.checked_at || observation?.observed_at || null,
    availability:entry.latest_availability?.availability || observation?.availability || 'unknown',
    collection:entry.collection_status,collectionReason:entry.collection_reason,nextCollectionAt:entry.next_collection_at,
    lastAttemptAt:entry.last_attempt_at,stats90:entry.stats90,stockChange:entry.stock_change,
    observations:latest?[latest]:[],purchases:[]};
}
export async function list() {
  const entries = [];
  for (const state of ['all','removed']) {
    let cursor;
    do {
      const response = await command('list',{state,limit:15,...(cursor?{cursor}:{})});
      entries.push(...response.data.entries.map(toUI));cursor=response.data.cursor;
      if(entries.length>10000)throw new Error('Wishlist je příliš velký pro tento přehled. Zobrazení nebylo zkráceno.');
    } while(cursor);
  }
  return entries;
}
export async function statistics({period,date,filters}) {
  const response=await command('statistics',{period,date,filters:{...filters,category:filters.category?categoryCode(filters.category):''}});
  return {...response.data,categories:response.data.categories.map(group=>({...group,category:categoryNames[group.category]}))};
}
export async function detail(id) {
  const response=await command('get',{entry_id:id}),entry=toUI(response.data.entry);
  for(const kind of ['history','purchases']){
    let cursor;
    const values=[];
    do{
      const page=await command(kind,{entry_id:id,...(cursor?{cursor}:{})});
      values.push(...page.data[kind]);cursor=page.data.cursor;
      if(values.length>128000)throw new Error('Historie je příliš velká pro tento graf. Zobrazení nebylo zkráceno.');
    }while(cursor);
    entry[kind==='history'?'observations':'purchases']=kind==='purchases'
      ? values.map(p=>({...p,category:categoryNames[p.category] || 'Nezařazeno'})) : values;
  }
  entry.historyLoaded=true;
  return entry;
}
