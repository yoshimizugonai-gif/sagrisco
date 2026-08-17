// Vercel Serverless Function
// Fetches MS Buildings dataset-links.csv internally, filters by quadkeys, returns matching URLs.
// This avoids sending the full 6.8 MB CSV to the browser (exceeds Vercel 4.5 MB response limit).

const CSV_URL = 'https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv';
const ALLOWED_HOSTS = [
  'minedbuildings.z5.web.core.windows.net',
  'minedbuildings.blob.core.windows.net',
];

export default async function handler(req, res) {
  const { quadkeys } = req.query;
  if (!quadkeys) return res.status(400).json({ error: 'quadkeys parameter required' });

  const qkSet = new Set(
    quadkeys.split(',').map(q => q.trim()).filter(Boolean)
  );

  let text;
  try {
    const r = await fetch(CSV_URL, { signal: AbortSignal.timeout(9000) });
    if (!r.ok) throw new Error(`CSV fetch failed: HTTP ${r.status}`);
    text = await r.text();
  } catch (e) {
    return res.status(502).json({ error: e.message });
  }

  const rows = text.trim().split('\n');
  const header = rows[0].split(',').map(h => h.trim().replace(/"/g, '').toLowerCase());
  const qkIdx  = header.findIndex(h => h.includes('quad'));
  const urlIdx = header.findIndex(h => h === 'url');

  if (qkIdx < 0 || urlIdx < 0) {
    return res.status(502).json({ error: `Unknown CSV columns: ${header.join(', ')}` });
  }

  const results = [];
  for (const row of rows.slice(1)) {
    const cols = row.split(',');
    if (cols.length <= Math.max(qkIdx, urlIdx)) continue;
    const qk  = cols[qkIdx]?.trim().replace(/"/g, '');
    const url = cols[urlIdx]?.trim().replace(/"/g, '');
    if (!qkSet.has(qk) || !url) continue;
    try {
      const parsed = new URL(url);
      if (ALLOWED_HOSTS.includes(parsed.hostname)) results.push({ qk, url });
    } catch {}
  }

  res.setHeader('Access-Control-Allow-Origin', '*');
  res.json(results);
}
