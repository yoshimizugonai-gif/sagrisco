export default async function handler(req, res) {
  const { z = '0', y = '0', x = '0', s = '0' } = req.query;
  const url = `https://mt${s}.google.com/vt/lyrs=s&x=${x}&y=${y}&z=${z}`;
  try {
    const upstream = await fetch(url, {
      headers: { 'User-Agent': 'Mozilla/5.0 (compatible; SAGRISCO/1.0)' }
    });
    const buf = await upstream.arrayBuffer();
    res.setHeader('Content-Type', upstream.headers.get('Content-Type') || 'image/jpeg');
    res.setHeader('Cache-Control', 'public, max-age=86400');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.status(200).send(Buffer.from(buf));
  } catch (e) {
    res.status(502).end();
  }
}
