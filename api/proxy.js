// Vercel Serverless Function — CORS proxy for Microsoft Buildings Azure Blob Storage
const ALLOWED_HOSTS = [
  'minedbuildings.z5.web.core.windows.net',
  'minedbuildings.blob.core.windows.net',
];

export default async function handler(req, res) {
  const { url } = req.query;

  if (!url) {
    return res.status(400).json({ error: 'url parameter required' });
  }

  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return res.status(400).json({ error: 'Invalid URL' });
  }

  if (!ALLOWED_HOSTS.includes(parsed.hostname)) {
    return res.status(403).json({ error: 'URL not allowed' });
  }

  try {
    const upstream = await fetch(url, { signal: AbortSignal.timeout(9000) });
    if (!upstream.ok) {
      return res.status(upstream.status).json({ error: `Upstream: ${upstream.status}` });
    }
    const buffer = await upstream.arrayBuffer();
    res.setHeader('Content-Type',
      upstream.headers.get('Content-Type') || 'application/octet-stream');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.send(Buffer.from(buffer));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
}
