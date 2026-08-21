// Vercel Serverless Function — OpenTopography DEM proxy
// Keeps the API key server-side (OPENTOPO_KEY env var).
// Supports demtype: COP30, AW3D30, SRTMGL1, etc.
export const config = { maxDuration: 60 };

const ALLOWED_DEMTYPES = new Set(['COP30', 'AW3D30', 'SRTMGL1', 'SRTMGL3']);

export default async function handler(req, res) {
  const { demtype, south, north, east, west } = req.query;

  if (!demtype || !south || !north || !east || !west)
    return res.status(400).json({ error: 'demtype, south, north, east, west required' });

  if (!ALLOWED_DEMTYPES.has(demtype))
    return res.status(400).json({ error: `demtype not allowed: ${demtype}` });

  const s = parseFloat(south), n = parseFloat(north),
        w = parseFloat(west),  e = parseFloat(east);
  if ([s, n, w, e].some(isNaN))
    return res.status(400).json({ error: 'Invalid coordinates' });

  const key = process.env.OPENTOPO_KEY;
  if (!key)
    return res.status(503).json({ error: 'OPENTOPO_KEY not configured on server' });

  const params = new URLSearchParams({
    demtype,
    south:        s.toFixed(5),
    north:        n.toFixed(5),
    west:         w.toFixed(5),
    east:         e.toFixed(5),
    outputFormat: 'GTiff',
    API_Key:      key,
  });

  let upstream;
  try {
    upstream = await fetch(
      `https://portal.opentopography.org/API/globaldem?${params}`,
      { signal: AbortSignal.timeout(55000) }
    );
  } catch (err) {
    return res.status(502).json({ error: `Upstream fetch failed: ${err.message}` });
  }

  if (!upstream.ok) {
    const txt = await upstream.text().catch(() => '');
    return res.status(upstream.status).json({ error: txt.slice(0, 300) });
  }

  const buf = await upstream.arrayBuffer();
  res.setHeader('Content-Type', 'image/tiff');
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Cache-Control', 'public, max-age=86400');
  res.send(Buffer.from(buf));
}
