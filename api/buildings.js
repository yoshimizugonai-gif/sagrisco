// Vercel Serverless Function — MS Buildings tile pipeline (server-side)
// Streams each tile (gzipped NDJSON) without buffering the full file,
// filters features to the requested bbox, and returns a compact JSON array.
// Avoids Vercel's 4.5 MB response limit that would apply to a raw proxy.

import { createGunzip } from 'zlib';
import https from 'https';
import http from 'http';

const CSV_URL = 'https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv';
const ALLOWED_HOSTS = [
  'minedbuildings.z5.web.core.windows.net',
  'minedbuildings.blob.core.windows.net',
];
const MAX_FEATURES = 8000;

// ── Quadkey helpers (mirrors browser-side _bboxToQuadkeys) ───────────────────
function latLonToTile(lat, lon, zoom) {
  const sinLat = Math.sin(lat * Math.PI / 180);
  const n = Math.pow(2, zoom);
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

// ── Bbox intersection check ───────────────────────────────────────────────────
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

// ── Stream a tile URL, decompress if .gz, parse NDJSON, filter to bbox ───────
// Uses Node.js http/https so we never buffer the full decompressed file.
function streamTileFeatures(tileUrl, south, west, north, east, maxFeat, redirectsLeft = 3) {
  return new Promise((resolve) => {
    const features = [];
    const parsed = new URL(tileUrl);
    const mod = parsed.protocol === 'https:' ? https : http;

    const req = mod.get(tileUrl, (res) => {
      // Follow redirects
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location && redirectsLeft > 0) {
        res.resume();
        streamTileFeatures(res.headers.location, south, west, north, east, maxFeat, redirectsLeft - 1)
          .then(resolve);
        return;
      }
      if (res.statusCode !== 200) { res.resume(); resolve([]); return; }

      const isGzip = /\.gz$/i.test(parsed.pathname);
      const stream = isGzip ? res.pipe(createGunzip()) : res;

      let buf = '';
      stream.on('data', chunk => {
        if (features.length >= maxFeat) return;
        buf += chunk.toString('utf8');
        const nl = buf.lastIndexOf('\n');
        if (nl < 0) return;
        for (const line of buf.slice(0, nl).split('\n')) {
          if (!line.trim() || features.length >= maxFeat) continue;
          try {
            const feat = JSON.parse(line);
            if (featInBbox(feat, south, west, north, east)) features.push(feat);
          } catch (_) {}
        }
        buf = buf.slice(nl + 1);
      });
      stream.on('end', () => {
        if (buf.trim() && features.length < maxFeat) {
          try {
            const feat = JSON.parse(buf);
            if (featInBbox(feat, south, west, north, east)) features.push(feat);
          } catch (_) {}
        }
        resolve(features);
      });
      stream.on('error', () => resolve(features));
    });

    req.on('error', () => resolve([]));
    req.setTimeout(30000, () => { req.destroy(); resolve(features); });
  });
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

  // Step 1 — fetch CSV and collect tile URLs matching our quadkeys
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
    return res.status(502).json({ error: err.message });
  }

  if (!tileUrls.length) {
    res.setHeader('Access-Control-Allow-Origin', '*');
    return res.json([]);
  }

  // Step 2 — stream each tile, filter features to bbox, collect results
  const allFeatures = [];
  for (const url of tileUrls) {
    if (allFeatures.length >= MAX_FEATURES) break;
    const tf = await streamTileFeatures(url, s, w, n, e, MAX_FEATURES - allFeatures.length);
    allFeatures.push(...tf);
  }

  res.setHeader('Access-Control-Allow-Origin', '*');
  res.json(allFeatures);
}
