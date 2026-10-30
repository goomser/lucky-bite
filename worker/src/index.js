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
      if (url.pathname === '/count') return await count(request, env, cors);
      return json({ error: 'not found' }, 404, cors);
    } catch (e) {
      return json({ error: String(e) }, 500, cors);
    }
  },
};

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
