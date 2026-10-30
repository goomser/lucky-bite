// Lucky Bite worker. Runs on Cloudflare's edge, close to whoever is using the app.
//
// Why it exists:
//   1. The Google key lives here as a secret instead of inside the public web page.
//   2. Google answers are cached in KV for a week, shared by everyone, so a restaurant costs one
//      Google call per week no matter how many people pull it (Google permits short-term caching).
//   3. The hit counter lives here too, so no third-party counting service is involved.
//
// Secrets (set once, never in code):   npx wrangler secret put GOOGLE_KEY

const CACHE_TTL = 7 * 24 * 3600;          // seconds a Google answer stays fresh
const FIELDS = 'places.id,places.displayName,places.formattedAddress,places.location,places.businessStatus,'
  + 'places.currentOpeningHours,places.priceLevel,places.rating,places.userRatingCount,places.googleMapsUri,'
  + 'places.nationalPhoneNumber,places.websiteUri';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = corsHeaders(request, env);
    if (request.method === 'OPTIONS') return new Response(null, { headers: cors });
    if (!originAllowed(request, env)) return json({ error: 'forbidden' }, 403, cors);

    try {
      if (url.pathname === '/places' && request.method === 'POST') return await places(request, env, cors);
      if (url.pathname === '/nearby' && request.method === 'POST') return await nearby(request, env, cors);
      if (url.pathname === '/count') return await count(request, env, cors);
      return json({ error: 'not found' }, 404, cors);
    } catch (e) {
      return json({ error: String(e) }, 500, cors);
    }
  },
};

// ---------- /nearby : { lat, lon, radiusMi, types? } -> { places: [...], cached } ----------
// Google's Nearby Search returns at most 20 places per call, nearest first. To cover a real radius
// we ask at a small ring of centers (the middle plus up to 6 around it) and merge. The answer is
// cached for a day per rounded location / radius / types, so a household's repeat opens are free.
const NEARBY_TTL = 24 * 3600;
const NEARBY_FIELDS = 'places.id,places.displayName,places.formattedAddress,places.location,places.businessStatus,'
  + 'places.primaryType,places.types,places.priceLevel,places.rating,places.userRatingCount,'
  + 'places.currentOpeningHours.openNow,places.googleMapsUri,places.nationalPhoneNumber,places.websiteUri';
const EAT_TYPES = ['restaurant', 'fast_food_restaurant', 'cafe', 'coffee_shop', 'bar', 'pub', 'meal_takeaway', 'pizza_restaurant',
  'sandwich_shop', 'bakery', 'ice_cream_shop', 'diner', 'bar_and_grill', 'steak_house', 'sushi_restaurant', 'ramen_restaurant',
  'hamburger_restaurant', 'breakfast_restaurant', 'brunch_restaurant', 'food_court', 'tea_house', 'dessert_shop'];

async function nearby(request, env, cors) {
  const body = await request.json().catch(() => null);
  if (!body || typeof body.lat !== 'number' || typeof body.lon !== 'number') return json({ error: 'bad request' }, 400, cors);
  const radiusMi = Math.min(31, Math.max(0.5, Number(body.radiusMi) || 5));   // Google caps a search at 50 km
  const types = Array.isArray(body.types) && body.types.length ? body.types.slice(0, 10) : null;

  // Cache key: location rounded to ~0.7 mi so the whole household shares one entry.
  const key = `n:${body.lat.toFixed(2)},${body.lon.toFixed(2)}:${radiusMi}:${types ? types.join('+') : 'all'}`;
  const cached = await env.CACHE.get(key, 'json');
  if (cached) return json({ places: cached, cached: true }, 200, cors);

  // Centers: the origin plus a ring at ~55% of the radius when the radius is big enough to need it.
  const centers = [[body.lat, body.lon]];
  if (radiusMi >= 3) {
    const r = radiusMi * 0.55, dLat = r / 69, dLon = r / (69 * Math.cos(body.lat * Math.PI / 180));
    for (let k = 0; k < 6; k++) { const a = k * Math.PI / 3; centers.push([body.lat + dLat * Math.sin(a), body.lon + dLon * Math.cos(a)]); }
  }
  const perCallRadiusM = Math.round(Math.min(50000, (radiusMi >= 3 ? radiusMi * 0.6 : radiusMi) * 1609));
  const seen = new Map();
  await Promise.all(centers.map(async ([lat, lon]) => {
    const r = await fetch('https://places.googleapis.com/v1/places:searchNearby', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Goog-Api-Key': env.GOOGLE_KEY, 'X-Goog-FieldMask': NEARBY_FIELDS },
      body: JSON.stringify({ includedPrimaryTypes: types || EAT_TYPES, maxResultCount: 20, rankPreference: 'DISTANCE',
        locationRestriction: { circle: { center: { latitude: lat, longitude: lon }, radius: perCallRadiusM } } }),
    });
    if (!r.ok) return;
    for (const g of (await r.json()).places || []) {
      if (g.businessStatus && g.businessStatus !== 'OPERATIONAL') continue;
      // Airport concourses: real, but you can't walk in without a boarding pass. Skip unless the search is centered there.
      const txt = ((g.displayName && g.displayName.text) || '') + ' ' + (g.formattedAddress || '');
      if (/\b(airport|terminal|concourse|CVG|gate [A-Z]?\d)\b/i.test(txt) && miles(body.lat, body.lon, g.location.latitude, g.location.longitude) > 1) continue;
      if (!g.location || miles(body.lat, body.lon, g.location.latitude, g.location.longitude) > radiusMi) continue;
      if (!seen.has(g.id)) seen.set(g.id, slim(g));
    }
  }));
  const places = [...seen.values()];
  if (places.length) await env.CACHE.put(key, JSON.stringify(places), { expirationTtl: NEARBY_TTL });
  return json({ places, cached: false }, 200, cors);
}
// Keep only what the app shows, so cached entries stay small.
function slim(g) {
  return { id: g.id, name: g.displayName && g.displayName.text, addr: g.formattedAddress, lat: g.location.latitude, lon: g.location.longitude,
    type: g.primaryType || '', types: g.types || [], price: g.priceLevel || '', rating: g.rating, ratingCount: g.userRatingCount,
    openNow: g.currentOpeningHours ? g.currentOpeningHours.openNow : null, maps: g.googleMapsUri, phone: g.nationalPhoneNumber, website: g.websiteUri };
}

// ---------- /places : { id, name, addr, lat, lon } -> { place: <Google record> | null } ----------
async function places(request, env, cors) {
  const body = await request.json().catch(() => null);
  if (!body || !body.id || !body.name || typeof body.lat !== 'number') return json({ error: 'bad request' }, 400, cors);

  const key = 'g:' + body.id;                       // our place id (OSM/Overture) is the cache key
  const cached = await env.CACHE.get(key, 'json');
  if (cached) return json({ place: cached.place, cached: true }, 200, cors);

  const r = await fetch('https://places.googleapis.com/v1/places:searchText', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Goog-Api-Key': env.GOOGLE_KEY, 'X-Goog-FieldMask': FIELDS },
    body: JSON.stringify({
      textQuery: body.name + (body.addr ? ', ' + body.addr : ''),
      maxResultCount: 3,
      locationBias: { circle: { center: { latitude: body.lat, longitude: body.lon }, radius: 1500 } },
    }),
  });
  if (!r.ok) return json({ error: 'google ' + r.status }, 502, cors);
  const data = await r.json();
  const place = nearest(data.places || [], body);

  // Remember the answer (including "Google has nothing" as null) so we never ask twice in a week.
  await env.CACHE.put(key, JSON.stringify({ place, at: Date.now() }), { expirationTtl: CACHE_TTL });
  return json({ place, cached: false }, 200, cors);
}

// Same matching rule as the app: within ~0.6 mi AND a name that resembles ours.
function nearest(list, p) {
  let best = null, bd = 0.6;
  for (const g of list) {
    const loc = g.location; if (!loc) continue;
    const d = miles(p.lat, p.lon, loc.latitude, loc.longitude);
    if (d < bd && namesMatch(p.name, g.displayName && g.displayName.text)) { bd = d; best = g; }
  }
  return best;
}
function miles(aLat, aLon, bLat, bLon) {
  const R = 3958.8, toR = x => x * Math.PI / 180;
  const dLat = toR(bLat - aLat), dLon = toR(bLon - aLon);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toR(aLat)) * Math.cos(toR(bLat)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}
const STOP = new Set(['and', 'the', 'of', 'restaurant', 'grill', 'bar', 'cafe', 'kitchen']);
function norm(s) { return (s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, ''); }
function namesMatch(rawA, rawB) {
  const a = norm(rawA).replace(/[^a-z0-9]+/g, ''), b = norm(rawB).replace(/[^a-z0-9]+/g, '');
  if (!a || !b) return false;
  if (a === b || a.includes(b) || b.includes(a)) return true;
  const words = x => norm(x).split(/[^a-z0-9]+/).filter(w => w && !STOP.has(w));
  const wa = words(rawA), wb = words(rawB);
  if (wa.length && wb.length) { const [s, l] = wa.length <= wb.length ? [wa, wb] : [wb, wa]; if (s.length >= 2 && s.every(w => l.includes(w))) return true; }
  const grams = x => { const g = new Set(); for (let i = 0; i + 3 <= x.length; i++) g.add(x.slice(i, i + 3)); return g; };
  const ta = grams(a), tb = grams(b); let c = 0; for (const t of ta) if (tb.has(t)) c++;
  return c / Math.min(ta.size, tb.size) >= 0.6;
}

// ---------- /count : GET reads, POST adds one ----------
async function count(request, env, cors) {
  let n = parseInt(await env.CACHE.get('count:spins') || '0', 10);
  if (request.method === 'POST') { n += 1; await env.CACHE.put('count:spins', String(n)); }
  return json({ value: n }, 200, cors);
}

// ---------- plumbing ----------
function originAllowed(request, env) {
  const o = request.headers.get('Origin') || '';
  return o === env.ALLOWED_ORIGIN || o === '' /* curl / server-side */ || o.startsWith('http://localhost') || o.startsWith('http://127.0.0.1');
}
function corsHeaders(request, env) {
  const o = request.headers.get('Origin') || '';
  const allow = o.startsWith('http://localhost') || o.startsWith('http://127.0.0.1') ? o : env.ALLOWED_ORIGIN;
  return { 'Access-Control-Allow-Origin': allow, 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Max-Age': '86400' };
}
function json(obj, status, cors) {
  return new Response(JSON.stringify(obj), { status, headers: { 'Content-Type': 'application/json', ...cors } });
}
