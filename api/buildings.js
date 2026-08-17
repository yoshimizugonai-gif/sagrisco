// Vercel Serverless Function — MS Buildings server-side pipeline
// 1. Download dataset-links.csv, find tile URLs for the requested bbox.
// 2. Download each tile (gzipped NDJSON), decompress with gunzipSync,
//    parse line-by-line, return only features that intersect the bbox.
// The filtered response is ~200 buildings (~100 KB) so Vercel's 4.5 MB
// response limit never applies here (it DID apply to the old raw-proxy).

import { gunzipSync } from 'node:zlib';

const CSV_URL = 'https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv';
const ALLOWED_HOSTS = [
  'minedbuildings.z5.web.core.windows.net',
  'minedbuildings.blob.core.windows.net',
];
const MAX_FEATURES = 8000;

// ── Quadkey helpers ───────────────────────────────────────────────────────────
function latLonToTile(lat, lon, zoom) {
  const sinLat = Math.sin(lat * Math.PI / 180);
  const n = 2 ** zoom;
  const x = Math.floor((lon + 180) / 360 * n);
  const y = Math.floor((1 - Math.log((1 + sinLat) / (1 - sinLat)) / (2 * Math.PI)) / 2 * n);
  return { x: Math.max(0, Math.min(x, n - 1)), y: Math.max(0, Math.min(y, n - 1)) };
}

function tileToQuadkey(x, y, zoom) {
  let qk = '';
  for (let i = zoom; i > 0; i--) {
    let d = 0;
    const mask = 1 << (i - 1);
    if (x & mask) d += 1;
    if (y & mask) d += 2;
    qk += d;
  }
  return qk;
}

function bboxToQuadkeys(south, west, north, east, zoom = 9) {
  const sw = latLonToTile(south, west, zoom);
  const ne = latLonToTile(north, east, zoom);
  const xMin = Math.min(sw.x, ne.x), xMax = Math.max(sw.x, ne.x);
  const yMin = Math.min(sw.y, ne.y), yMax = Math.max(sw.y, ne.y);
  const qks = [];
  for (let x = xMin; x <= xMax; x++)
    for (let y = yMin; y <= yMax; y++)
      qks.push(tileToQuadkey(x, y, zoom));
  return qks;
}

// ── Bbox filter ───────────────────────────────────────────────────────────────
function featInBbox(feat, south, west, north, east) {
  try {
    const geom = feat.geometry;
    if (!geom) return false;
    const rings = geom.type === 'Polygon'
      ? [geom.coordinates[0]]
      : geom.coordinates.map(p => p[0]);
    return rings.some(ring => {
      const lons = ring.map(c => c[0]), lats = ring.map(c => c[1]);
      return Math.max(...lons) >= west && Math.min(...lons) <= east &&
             Math.max(...lats) >= south && Math.min(...lats) <= north;
    });
  } catch (_) { return false; }
}

// ── Fetch one tile, decompress if gzipped, parse NDJSON, filter to bbox ──────
async function fetchAndFilterTile(url, south, west, north, east, maxFeat) {
  let r;
  try {
    r = await fetch(url, { signal: AbortSignal.timeout(45000) });
  } catch (_) { return []; }
  if (!r.ok) return [];

  let buf;
  try {
    buf = Buffer.from(await r.arrayBuffer());
  } catch (_) { return []; }

  let text;
  try {
    // Check gzip magic bytes (0x1F 0x8B)
    text = (buf[0] === 0x1f && buf[1] === 0x8b)
      ? gunzipSync(buf).toString('utf8')
      : buf.toString('utf8');
  } catch (_) { return []; }

  const features = [];
  for (const line of text.split('\n')) {
    if (!line.trim() || features.length >= maxFeat) continue;
    try {
      const feat = JSON.parse(line);
      if (featInBbox(feat, south, west, north, east)) features.push(feat);
    } catch (_) {}
  }
  return features;
}

// ── Handler ───────────────────────────────────────────────────────────────────
export default async function handler(req, res) {
  const { south, west, north, east } = req.query;
  if (!south || !west || !north || !east)
    return res.status(400).json({ error: 'south, west, north, east required' });

  const s = parseFloat(south), w = parseFloat(west),
        n = parseFloat(north), e = parseFloat(east);
  if ([s, w, n, e].some(isNaN))
    return res.status(400).json({ error: 'Invalid coordinates' });

  const quadkeys = bboxToQuadkeys(s, w, n, e);
  const qkSet    = new Set(quadkeys);

  // Step 1 — download CSV and collect matching tile URLs
  let tileUrls = [];
  try {
    const csvResp = await fetch(CSV_URL, { signal: AbortSignal.timeout(12000) });
    if (!csvResp.ok) throw new Error(`CSV HTTP ${csvResp.status}`);
    const text   = await csvResp.text();
    const rows   = text.trim().split('\n');
    const header = rows[0].split(',').map(h => h.trim().replace(/"/g, '').toLowerCase());
    const qkIdx  = header.findIndex(h => h.includes('quad'));
    const urlIdx = header.findIndex(h => h === 'url');
    if (qkIdx < 0 || urlIdx < 0)
      throw new Error(`Unknown CSV columns: ${header.join(', ')}`);
    for (const row of rows.slice(1)) {
      const cols = row.split(',');
      if (cols.length <= Math.max(qkIdx, urlIdx)) continue;
      const qk  = cols[qkIdx]?.trim().replace(/"/g, '');
      const url = cols[urlIdx]?.trim().replace(/"/g, '');
      if (!qkSet.has(qk) || !url) continue;
      try {
        const parsed = new URL(url);
        if (ALLOWED_HOSTS.includes(parsed.hostname)) tileUrls.push(url);
      } catch (_) {}
    }
  } catch (err) {
    return res.status(502).json({ error: `CSV: ${err.message}` });
  }

  res.setHeader('Access-Control-Allow-Origin', '*');
  if (!tileUrls.length) return res.json([]);

  // Step 2 — download, decompress, filter each tile
  const allFeatures = [];
  for (const url of tileUrls) {
    if (allFeatures.length >= MAX_FEATURES) break;
    const tf = await fetchAndFilterTile(url, s, w, n, e, MAX_FEATURES - allFeatures.length);
    allFeatures.push(...tf);
  }

  res.json(allFeatures);
}
