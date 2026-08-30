#!/usr/bin/env python3
"""SAGRISCO ローカルサーバー + CORSプロキシ + UP42プロキシ"""

import json
import os
import pathlib
import socketserver
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import HTTPServer, SimpleHTTPRequestHandler

PORT = 8888

# ── Google タイルキャッシュ（Carta キャプチャ用プロキシ） ───────────────────────
_tile_cache: dict = {}
_tile_cache_lock = threading.Lock()
_TILE_CACHE_MAX = 500    # 最大500タイル（約25MB）
_TILE_CACHE_TTL = 86400  # 24時間

# ── UP42 ジオメトリ正規化ヘルパー ─────────────────────────────────────────────
def _aoi_bbox_polygon(geom: dict) -> dict:
    """任意の GeoJSON ジオメトリから 2D バウンディングボックス Polygon を返す。
    UP42 カタログ検索の intersects パラメータ用（Polygon のみ受け付け）。"""
    def collect(g):
        t = g.get('type', '')
        c = g.get('coordinates', [])
        if t == 'Point':       return [c[:2]]
        if t == 'MultiPoint':  return [p[:2] for p in c]
        if t == 'LineString':  return [p[:2] for p in c]
        if t == 'MultiLineString': return [p[:2] for ln in c for p in ln]
        if t == 'Polygon':     return [p[:2] for p in c[0]]
        if t == 'MultiPolygon':return [p[:2] for poly in c for p in poly[0]]
        return []
    pts = collect(geom)
    if not pts:
        return geom
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    w, e, s, n = min(lons), max(lons), min(lats), max(lats)
    return {'type': 'Polygon',
            'coordinates': [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def _aoi_to_2d_polygon(geom: dict) -> dict:
    """z 座標を除去し、MultiPolygon は最大リングを Polygon に変換する。
    UP42 estimate / order の featureCollection 用。"""
    t = geom.get('type', '')
    if t == 'Polygon':
        ring = [[p[0], p[1]] for p in geom['coordinates'][0]]
        return {'type': 'Polygon', 'coordinates': [ring]}
    if t == 'MultiPolygon':
        outer = max(geom['coordinates'], key=lambda poly: len(poly[0]))
        ring  = [[p[0], p[1]] for p in outer[0]]
        return {'type': 'Polygon', 'coordinates': [ring]}
    return geom


# ── UP42 非同期ジョブ管理 ─────────────────────────────────────────────────────
_jobs: dict = {}
_jobs_lock = threading.Lock()


def _update_job(job_id: str, status: str, message: str,
                tif_bytes: bytes | None = None) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]['status'] = status
            _jobs[job_id]['message'] = message
            if tif_bytes is not None:
                _jobs[job_id]['tif_bytes'] = tif_bytes


def _run_order_job(job_id: str, email: str, password: str,
                   region: str, aoi_geom: dict) -> None:
    """UP42 NEXTMap 6 DTM 発注・ダウンロードのバックグラウンドジョブ"""
    import traceback as tb
    PRODUCT_ID = '337c01f4-d71a-4f2f-8aee-d21431565893'
    try:
        import requests as req
        import up42
        from up42.utils import get_up42_py_version
        from up42.http import http_adapter

        if region == 'sa':
            auth_url = ('https://auth.sa.up42.com/realms/public/'
                        'protocol/openid-connect/token')
            api_base = 'https://api.sa.up42.com'
        else:
            auth_url = ('https://auth.up42.com/realms/public/'
                        'protocol/openid-connect/token')
            api_base = 'https://api.up42.com'

        ua = (f'up42-py/{get_up42_py_version()} '
              f'(https://github.com/up42/up42-py)')

        # 1. Auth
        _update_job(job_id, 'processing', 'Autenticando no UP42...')
        auth_sess = req.Session()
        auth_sess.mount('https://', http_adapter.create(include_post=True))
        tok_r = auth_sess.post(
            auth_url,
            data={'username': email, 'password': password,
                  'grant_type': 'password', 'client_id': 'up42-sdk',
                  'scope': 'openid'},
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=120,
        )
        if not tok_r.ok:
            _update_job(job_id, 'failed',
                        f'Auth falhou HTTP {tok_r.status_code}')
            return
        token = tok_r.json()['access_token']

        api_sess = req.Session()
        api_sess.mount('https://', http_adapter.create())
        api_sess.headers.update({
            'Content-Type': 'application/json', 'cache-control': 'no-cache',
            'User-Agent': ua, 'Authorization': f'Bearer {token}',
        })

        # workspace ID for order placement
        if region == 'sa':
            user_info_url = ('https://auth.sa.up42.com/realms/public/'
                             'protocol/openid-connect/userinfo')
        else:
            user_info_url = ('https://auth.up42.com/realms/public/'
                             'protocol/openid-connect/userinfo')
        workspace_id = api_sess.get(user_info_url, timeout=15).json().get('sub', '')

        # ジオメトリ正規化（MultiPolygon・3D座標対応）
        aoi_bbox = _aoi_bbox_polygon(aoi_geom)
        aoi_2d   = _aoi_to_2d_polygon(aoi_geom)

        # 2. Catalog search → scene ID
        _update_job(job_id, 'processing',
                    'Buscando cena NEXTMap 6 no catálogo...')
        col_name = host_name = None
        page = 0
        while True:
            cols = api_sess.get(f'{api_base}/v2/collections',
                                params={'page': page}, timeout=30).json()
            for col in cols.get('content', []):
                for dp in col.get('dataProducts', []):
                    if dp.get('id') == PRODUCT_ID:
                        col_name = col['name']
                        for prov in col.get('providers', []):
                            if 'HOST' in prov.get('roles', []):
                                host_name = prov['name']
                        break
                if col_name:
                    break
            if col_name or page >= cols.get('totalPages', 1) - 1:
                break
            page += 1

        if not col_name or not host_name:
            _update_job(job_id, 'failed',
                        'Coleção NEXTMap 6 não encontrada no catálogo UP42')
            return

        search_r = api_sess.post(
            f'{api_base}/catalog/hosts/{host_name}/stac/search',
            json={'intersects': aoi_bbox, 'collections': [col_name],
                  'limit': 1},
            timeout=30,
        )
        scenes = search_r.json().get('features', [])
        if not scenes:
            _update_job(job_id, 'failed',
                        'Nenhuma cena NEXTMap 6 disponível para esta AOI')
            return
        scene_id = scenes[0]['properties']['id']

        # 3. Estimate confirmation
        est_r = api_sess.post(
            f'{api_base}/v2/orders/estimate',
            json={
                'dataProduct': PRODUCT_ID,
                'displayName': 'NEXTMap 6 DTM SAGRISCO',
                'params':      {'id': scene_id},
                'featureCollection': {
                    'type': 'FeatureCollection',
                    'features': [{'type': 'Feature', 'geometry': aoi_2d,
                                  'properties': {}}],
                },
            },
            timeout=30,
        )
        if est_r.ok:
            summary = est_r.json().get('summary', {})
            credits = summary.get('totalCredits', '?')
            size    = summary.get('totalSize', '?')
            _update_job(job_id, 'processing',
                        f'Estimativa confirmada: {credits} créditos '
                        f'({size} km²). Enviando pedido...')
        else:
            _update_job(job_id, 'processing', 'Enviando pedido...')

        # 4. Place order
        order_r = api_sess.post(
            f'{api_base}/v2/orders',
            params={'workspaceId': workspace_id},
            json={
                'dataProduct': PRODUCT_ID,
                'displayName': 'NEXTMap 6 DTM SAGRISCO',
                'params':      {'id': scene_id},
                'tags':        ['sagrisco', 'nextmap6'],
                'featureCollection': {
                    'type': 'FeatureCollection',
                    'features': [{'type': 'Feature', 'geometry': aoi_2d,
                                  'properties': {}}],
                },
            },
            timeout=60,
        )
        print(f'[order] place: {order_r.status_code} {order_r.text[:300]}')
        if not order_r.ok:
            _update_job(job_id, 'failed',
                        f'Pedido rejeitado: HTTP {order_r.status_code} '
                        f'{order_r.text[:200]}')
            return

        order_data = order_r.json()
        # v2 orders returns a list or a single object
        if isinstance(order_data, list):
            order_id = order_data[0].get('id') or order_data[0].get('orderId')
        else:
            order_id = order_data.get('id') or order_data.get('orderId')

        if not order_id:
            _update_job(job_id, 'failed',
                        f'Order ID não encontrado: {order_r.text[:200]}')
            return

        _update_job(job_id, 'processing',
                    f'Pedido enviado (ID: {order_id[:8]}…). '
                    f'Aguardando processamento UP42...')

        # 5. Poll order status
        import time
        for _ in range(120):   # max ~60 min
            time.sleep(30)
            st_r = api_sess.get(f'{api_base}/v2/orders/{order_id}',
                                timeout=30)
            if not st_r.ok:
                continue
            status = st_r.json().get('status', '')
            print(f'[order] status={status}')
            _update_job(job_id, 'processing',
                        f'Status do pedido: {status} (ID: {order_id[:8]}…)')
            if status == 'FULFILLED':
                break
            if status in ('FAILED', 'FAILED_PERMANENTLY',
                          'CANCELLED', 'REJECTED'):
                _update_job(job_id, 'failed', f'Pedido {status}')
                return
        else:
            _update_job(job_id, 'failed', 'Timeout aguardando UP42 (60 min)')
            return

        # 6. Download via STAC (uses up42-py session internally)
        _update_job(job_id, 'processing', 'Dados prontos. Baixando GeoTIFF...')
        up42.authenticate(username=email, password=password, region=region)
        sc = up42.stac_client()
        search_res = sc.search(filter={
            'op': '=',
            'args': [{'property': 'up42-order:id'}, order_id],
        })

        tif_bytes: bytes | None = None
        with tempfile.TemporaryDirectory() as tmpdir:
            for item in search_res.items():
                collection = item.get_collection()
                if not collection:
                    continue
                for asset in collection.assets.values():
                    if not (hasattr(asset, 'file') and asset.file):
                        continue
                    fp   = asset.file.download(pathlib.Path(tmpdir))
                    fp_s = str(fp).lower()
                    if fp_s.endswith('.zip'):
                        with zipfile.ZipFile(fp) as zf:
                            tifs = [n for n in zf.namelist()
                                    if n.lower().endswith(('.tif', '.tiff'))]
                            if tifs:
                                tif_bytes = zf.read(tifs[0])
                                break
                    elif fp_s.endswith(('.tif', '.tiff')):
                        tif_bytes = fp.read_bytes()
                        break
                if tif_bytes:
                    break

        if tif_bytes:
            kb = len(tif_bytes) // 1024
            _update_job(job_id, 'fulfilled',
                        f'GeoTIFF pronto ({kb:,} KB). Importando dados...',
                        tif_bytes)
        else:
            _update_job(job_id, 'failed',
                        'GeoTIFF não encontrado no resultado da entrega UP42')

    except Exception as exc:
        print(f'[order] ERROR:\n{tb.format_exc()}')
        _update_job(job_id, 'failed', f'{type(exc).__name__}: {exc}')


# ── スレッディング HTTP サーバー ───────────────────────────────────────────────
class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class SAGRISCOHandler(SimpleHTTPRequestHandler):

    def _cors(self) -> None:
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def send_response(self, code, message=None):
        super().send_response(code, message)
        # Disable browser caching for all responses so edits to index.html are always picked up
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')

    def do_GET(self) -> None:
        if self.path.startswith('/proxy?url='):
            self._handle_proxy()
        elif self.path.startswith('/api/google-tile'):
            self._handle_google_tile()
        elif self.path.startswith('/up42/status'):
            self._handle_up42_status()
        elif self.path.startswith('/up42/download'):
            self._handle_up42_download()
        else:
            super().do_GET()

    def do_POST(self) -> None:
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)
        if self.path == '/up42/estimate':
            self._handle_up42_estimate(body)
        elif self.path == '/up42/order':
            self._handle_up42_order(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, message: str, status: int = 500) -> None:
        self._send_json({'error': message}, status)

    # ── /api/google-tile（Cartaキャプチャ用・CORS付きプロキシ） ─────────────────
    def _handle_google_tile(self) -> None:
        qs  = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        z, y, x = qs.get('z',['0'])[0], qs.get('y',['0'])[0], qs.get('x',['0'])[0]
        s   = qs.get('s', ['0'])[0]
        key = (s, z, y, x)
        now = time.time()
        with _tile_cache_lock:
            cached = _tile_cache.get(key)
        if cached and (now - cached[0]) < _TILE_CACHE_TTL:
            data, ct = cached[1], cached[2]
        else:
            url = f'https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}'
            try:
                req = urllib.request.Request(
                    url, headers={'User-Agent': 'Mozilla/5.0 (compatible; SAGRISCO/1.0)'})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                    ct   = resp.headers.get('Content-Type', 'image/jpeg')
            except Exception:
                self.send_response(502); self.end_headers(); return
            with _tile_cache_lock:
                if len(_tile_cache) >= _TILE_CACHE_MAX:
                    del _tile_cache[min(_tile_cache, key=lambda k: _tile_cache[k][0])]
                _tile_cache[key] = (now, data, ct)
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'public, max-age=86400')
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    # ── /proxy?url=... ────────────────────────────────────────────────────────
    def _handle_proxy(self) -> None:
        raw = self.path[len('/proxy?url='):]
        url = urllib.parse.unquote(raw)
        try:
            req = urllib.request.Request(
                url, headers={'User-Agent': 'SAGRISCO/1.0'})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
                ct   = resp.headers.get('Content-Type',
                                        'application/octet-stream')
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            msg = str(e).encode('utf-8')
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(msg)))
            self._cors()
            self.end_headers()
            self.wfile.write(msg)

    # ── POST /up42/estimate ───────────────────────────────────────────────────
    def _handle_up42_estimate(self, body: bytes) -> None:
        import traceback
        PRODUCT_ID = '337c01f4-d71a-4f2f-8aee-d21431565893'
        try:
            import requests as req
            from up42.utils import get_up42_py_version
            from up42.http import http_adapter

            data   = json.loads(body)
            email  = data['email']
            passwd = data['password']
            region = data.get('region', 'eu')
            aoi    = data['aoi']
            print(f'[estimate] auth {email} region={region} '
                  f'aoi_type={aoi.get("type")} aoi_len={len(str(aoi))}')

            if region == 'sa':
                auth_url = ('https://auth.sa.up42.com/realms/public/'
                            'protocol/openid-connect/token')
                api_base = 'https://api.sa.up42.com'
            else:
                auth_url = ('https://auth.up42.com/realms/public/'
                            'protocol/openid-connect/token')
                api_base = 'https://api.up42.com'

            ua = (f'up42-py/{get_up42_py_version()} '
                  f'(https://github.com/up42/up42-py)')

            # 1. OAuth2 token
            auth_sess = req.Session()
            auth_sess.mount('https://', http_adapter.create(include_post=True))
            tok_r = auth_sess.post(
                auth_url,
                data={
                    'username':   email,
                    'password':   passwd,
                    'grant_type': 'password',
                    'client_id':  'up42-sdk',
                    'scope':      'openid',
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=120,
            )
            if not tok_r.ok:
                raise Exception(
                    f'Auth HTTP {tok_r.status_code}: {tok_r.text[:300]}')
            token = tok_r.json()['access_token']
            print('[estimate] auth OK')

            api_sess = req.Session()
            api_sess.mount('https://', http_adapter.create())
            api_sess.headers.update({
                'Content-Type':  'application/json',
                'cache-control': 'no-cache',
                'User-Agent':    ua,
                'Authorization': f'Bearer {token}',
            })

            # 2. Find collection + host for NEXTMap 6 DTM
            print('[estimate] searching collection...')
            col_name = host_name = None
            page = 0
            while True:
                cols = api_sess.get(
                    f'{api_base}/v2/collections',
                    params={'page': page}, timeout=30).json()
                for col in cols.get('content', []):
                    for dp in col.get('dataProducts', []):
                        if dp.get('id') == PRODUCT_ID:
                            col_name = col['name']
                            for prov in col.get('providers', []):
                                if 'HOST' in prov.get('roles', []):
                                    host_name = prov['name']
                            break
                    if col_name:
                        break
                if col_name or page >= cols.get('totalPages', 1) - 1:
                    break
                page += 1
            print(f'[estimate] collection={col_name} host={host_name}')
            if not col_name or not host_name:
                raise Exception(
                    'Coleção NEXTMap 6 não encontrada no catálogo UP42')

            # 3. Catalog search
            aoi_bbox = _aoi_bbox_polygon(aoi)
            aoi_2d   = _aoi_to_2d_polygon(aoi)
            print(f'[estimate] catalog search...')
            search_r = api_sess.post(
                f'{api_base}/catalog/hosts/{host_name}/stac/search',
                json={'intersects': aoi_bbox, 'collections': [col_name],
                      'limit': 5},
                timeout=30,
            )
            print(f'[estimate] catalog: {search_r.status_code} '
                  f'{search_r.text[:400]}')
            scenes = search_r.json().get('features', [])
            print(f'[estimate] {len(scenes)} scene(s) found')
            if not scenes:
                raise Exception(
                    f'Nenhuma cena {col_name} disponível para esta AOI.')

            # 4. Estimate
            total_credits  = 0.0
            total_size     = 0.0
            unit_val       = None
            errs_collected = []
            for sc in scenes:
                scene_id = sc['properties']['id']
                est_r = api_sess.post(
                    f'{api_base}/v2/orders/estimate',
                    json={
                        'dataProduct': PRODUCT_ID,
                        'displayName': 'NEXTMap 6 DTM SAGRISCO',
                        'params':      {'id': scene_id},
                        'featureCollection': {
                            'type': 'FeatureCollection',
                            'features': [{'type': 'Feature', 'geometry': aoi_2d,
                                          'properties': {}}],
                        },
                    },
                    timeout=30,
                )
                print(f'[estimate] scene {scene_id[:12]}… → '
                      f'{est_r.status_code} {est_r.text[:300]}')
                if not est_r.ok:
                    continue
                est_body  = est_r.json()
                up42_errs = est_body.get('errors', [])
                if up42_errs:
                    for e in up42_errs:
                        print(f'[estimate] UP42 error: {e.get("message")}')
                    errs_collected.extend(up42_errs)
                    continue
                summary = est_body.get('summary', {})
                total_credits += summary.get('totalCredits', 0) or 0
                total_size    += summary.get('totalSize',    0) or 0
                if unit_val is None:
                    unit_val = summary.get('unit')

            print(f'[estimate] total credits={total_credits} '
                  f'size={total_size} unit={unit_val}')
            if total_credits == 0 and errs_collected:
                raw_msg = errs_collected[0].get('message', '')
                if '250' in raw_msg:
                    raise Exception(
                        'A AOI excede 250 km² de interseção com a cena UP42. '
                        'Reduza a AOI para menos de 250 km² e tente novamente.')
                raise Exception(f'UP42: {raw_msg}')
            self._send_json({
                'credits': total_credits,
                'size':    round(total_size, 2),
                'unit':    unit_val,
                'scenes':  len(scenes),
            })

        except Exception as exc:
            msg = f'{type(exc).__name__}: {exc!r}'
            print(f'[estimate] ERROR:\n{traceback.format_exc()}')
            self._send_error(msg)

    # ── POST /up42/order ──────────────────────────────────────────────────────
    def _handle_up42_order(self, body: bytes) -> None:
        try:
            data   = json.loads(body)
            job_id = str(uuid.uuid4())
            with _jobs_lock:
                _jobs[job_id] = {
                    'status':    'processing',
                    'message':   'Iniciando...',
                    'tif_bytes': None,
                }
            t = threading.Thread(
                target=_run_order_job,
                args=(job_id,
                      data['email'], data['password'],
                      data.get('region', 'eu'),
                      data['aoi']),
                daemon=True,
            )
            t.start()
            self._send_json({'job_id': job_id})
        except Exception as exc:
            self._send_error(str(exc))

    # ── GET /up42/status?job_id=... ───────────────────────────────────────────
    def _handle_up42_status(self) -> None:
        qs     = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query)
        job_id = qs.get('job_id', [None])[0]
        if not job_id:
            self._send_error('Missing job_id', 400)
            return
        with _jobs_lock:
            job = dict(_jobs.get(job_id, {}))
        if not job:
            self._send_error('Job not found', 404)
            return
        self._send_json({
            'status':  job['status'],
            'message': job['message'],
            'ready':   job['tif_bytes'] is not None,
        })

    # ── GET /up42/download?job_id=... ─────────────────────────────────────────
    def _handle_up42_download(self) -> None:
        qs     = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query)
        job_id = qs.get('job_id', [None])[0]
        if not job_id:
            self._send_error('Missing job_id', 400)
            return
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job or job.get('status') != 'fulfilled' \
                or not job.get('tif_bytes'):
            self._send_error('Data not ready', 404)
            return
        tif = job['tif_bytes']
        self.send_response(200)
        self.send_header('Content-Type', 'image/tiff')
        self.send_header('Content-Length', str(len(tif)))
        self.send_header('Content-Disposition',
                         'attachment; filename="nextmap6_dtm.tif"')
        self._cors()
        self.end_headers()
        self.wfile.write(tif)
        with _jobs_lock:
            _jobs.pop(job_id, None)

    def log_message(self, fmt, *args) -> None:
        print(f'  {fmt % args}')


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print('=' * 56)
    print('  SAGRISCO ローカルサーバー + CORS プロキシ')
    print('=' * 56)
    print(f'  http://localhost:{PORT}/index.html')
    print('  Ctrl+C で終了')
    print('=' * 56)
    try:
        _ThreadingHTTPServer(('', PORT), SAGRISCOHandler).serve_forever()
    except KeyboardInterrupt:
        print('\nサーバーを停止しました。')
