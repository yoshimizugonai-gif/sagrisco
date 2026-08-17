#!/usr/bin/env python3
"""
fetch_ms_buildings.py
Microsoft Global ML Building Footprints の取得・切り出しスクリプト
SAGRISCO の「Carregar Edificações (GeoJSON)」ボタンで使用するGeoJSONを生成する

使い方:
  python fetch_ms_buildings.py --south -21.3 --west -43.9 --north -21.1 --east -43.7

  ※ SAGRISCOの「bbox をコピー」ボタンを押すとコマンド引数がクリップボードにコピーされます。
"""

import argparse
import csv
import gzip
import io
import json
import math
import sys
import urllib.request
from pathlib import Path

DATASET_LINKS_URL = (
    "https://minedbuildings.z5.web.core.windows.net/"
    "global-buildings/dataset-links.csv"
)
ZOOM = 9  # Microsoft のタイル分割レベル（LOD-9、約78×78km）


# ── クアッドキー計算 ──────────────────────────────────────────────

def _lat_lon_to_tile(lat, lon, zoom):
    sin_lat = math.sin(math.radians(lat))
    x = int((lon + 180) / 360 * (2 ** zoom))
    y = int(
        (1 - math.log((1 + sin_lat) / (1 - sin_lat)) / (2 * math.pi))
        / 2 * (2 ** zoom)
    )
    x = max(0, min(x, 2 ** zoom - 1))
    y = max(0, min(y, 2 ** zoom - 1))
    return x, y


def _tile_to_quadkey(x, y, zoom):
    qk = ""
    for i in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        qk += str(digit)
    return qk


def bbox_to_quadkeys(south, west, north, east, zoom=ZOOM):
    """バウンディングボックスに重なるすべてのクアッドキーを返す"""
    x_sw, y_sw = _lat_lon_to_tile(south, west, zoom)
    x_ne, y_ne = _lat_lon_to_tile(north, east, zoom)
    x_min, x_max = min(x_sw, x_ne), max(x_sw, x_ne)
    y_min, y_max = min(y_sw, y_ne), max(y_sw, y_ne)
    quadkeys = set()
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            quadkeys.add(_tile_to_quadkey(x, y, zoom))
    return quadkeys


# ── bbox フィルタ ─────────────────────────────────────────────────

def _feature_in_bbox(feature, south, west, north, east):
    try:
        geom = feature.get("geometry", {})
        gtype = geom.get("type")
        if gtype == "Polygon":
            rings = [geom["coordinates"][0]]
        elif gtype == "MultiPolygon":
            rings = [p[0] for p in geom["coordinates"]]
        else:
            return False
        for ring in rings:
            lons = [c[0] for c in ring]
            lats = [c[1] for c in ring]
            if (
                max(lons) >= west and min(lons) <= east
                and max(lats) >= south and min(lats) <= north
            ):
                return True
        return False
    except Exception:
        return False


# ── メイン ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Microsoft Building Footprints 取得・切り出しスクリプト"
    )
    parser.add_argument("--south",  type=float, required=True, help="南端緯度")
    parser.add_argument("--west",   type=float, required=True, help="西端経度")
    parser.add_argument("--north",  type=float, required=True, help="北端緯度")
    parser.add_argument("--east",   type=float, required=True, help="東端経度")
    parser.add_argument(
        "--output", default="buildings_ms.geojson",
        help="出力GeoJSONファイル名（デフォルト: buildings_ms.geojson）"
    )
    parser.add_argument(
        "--varname", default="SAGRISCO_BUILDINGS",
        help="JS変数名（デフォルト: SAGRISCO_BUILDINGS）例: SAGRISCO_BUILDINGS_NOVA_FRIBURGO"
    )
    args = parser.parse_args()

    south, west, north, east = args.south, args.west, args.north, args.east

    print("=" * 60)
    print("Microsoft Global ML Building Footprints 取得スクリプト")
    print("=" * 60)
    print(f"[1/4] 検索範囲: S={south}  W={west}  N={north}  E={east}")

    quadkeys = bbox_to_quadkeys(south, west, north, east)
    print(f"      対象クアッドキー (LOD{ZOOM}): {sorted(quadkeys)}")

    # ── dataset-links.csv 取得 ────────────────────────────────────
    print(f"\n[2/4] dataset-links.csv を取得中...")
    print(f"      URL: {DATASET_LINKS_URL}")
    try:
        req = urllib.request.Request(
            DATASET_LINKS_URL,
            headers={"User-Agent": "SAGRISCO/1.0 fetch_ms_buildings"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            links_text = resp.read().decode("utf-8")
        print(f"      取得完了 ({len(links_text):,} bytes)")
    except Exception as e:
        print(f"\nERROR: dataset-links.csv の取得に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)

    # カラム名を自動検出（QuadKey / Url の表記ゆれに対応）
    reader = csv.DictReader(io.StringIO(links_text))
    fieldnames = reader.fieldnames or []
    qk_col  = next((c for c in fieldnames if "quad" in c.lower()), None)
    url_col = next((c for c in fieldnames if "url"  in c.lower()), None)
    if not qk_col or not url_col:
        print(
            f"\nERROR: dataset-links.csv のカラム名が不明です: {fieldnames}",
            file=sys.stderr,
        )
        sys.exit(1)

    matching = [
        (row[qk_col], row[url_col])
        for row in reader
        if row.get(qk_col) in quadkeys
    ]

    if not matching:
        print(
            "\nWARNING: 対象範囲に対応するタイルが見つかりませんでした。\n"
            "  ・Microsoft のデータがその地域をカバーしていない可能性があります。\n"
            "  ・bbox の座標が正しいか確認してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"      {len(matching)} タイル該当: {[q for q, _ in matching]}")

    # ── タイルダウンロード＋フィルタ ──────────────────────────────
    features = []
    for i, (qk, url) in enumerate(matching, 1):
        print(f"\n[3/4] タイル {i}/{len(matching)} ダウンロード中")
        print(f"      QuadKey: {qk}")
        print(f"      URL: {url}")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "SAGRISCO/1.0 fetch_ms_buildings"}
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                with gzip.open(resp, "rt", encoding="utf-8") as f:
                    total = 0
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            feat = json.loads(line)
                            if _feature_in_bbox(feat, south, west, north, east):
                                features.append(feat)
                            total += 1
                            if total % 100_000 == 0:
                                print(
                                    f"      {total:,} フィーチャ処理済み "
                                    f"(bbox内: {len(features):,})"
                                )
                        except json.JSONDecodeError:
                            continue
            print(f"      完了: {total:,} フィーチャ処理、{len(features):,} 棟取得")
        except Exception as e:
            print(f"\nWARNING: タイル {qk} の取得に失敗しました: {e}", file=sys.stderr)

    if not features:
        print(
            "\nWARNING: 対象範囲内に建物フィーチャが見つかりませんでした。",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── GeoJSON 保存 ──────────────────────────────────────────────
    print(f"\n[4/4] {len(features):,} 棟を保存中...")
    geojson = {"type": "FeatureCollection", "features": features}
    geojson_str = json.dumps(geojson, ensure_ascii=False)

    # GeoJSON ファイル
    out_path = Path(args.output)
    out_path.write_text(geojson_str, encoding="utf-8")
    size_kb = out_path.stat().st_size // 1024

    # SAGRISCO 用 JS ファイル（<script src> で読み込み可能、file:// 対応）
    # 都市別変数（例: SAGRISCO_BUILDINGS_NOVA_FRIBURGO）と汎用変数の両方を定義する
    varname = args.varname
    js_path = out_path.with_suffix(".js")
    js_lines = [f"var {varname} = {geojson_str};"]
    if varname != "SAGRISCO_BUILDINGS":
        js_lines.append(f"var SAGRISCO_BUILDINGS = {varname};")
    js_path.write_text("\n".join(js_lines), encoding="utf-8")
    js_kb = js_path.stat().st_size // 1024

    print(f"\n{'=' * 60}")
    print(f"完了！")
    print(f"  GeoJSON : {out_path.resolve()}  ({size_kb:,} KB)")
    print(f"  JS      : {js_path.resolve()}  ({js_kb:,} KB)")
    print(f"  JS変数名: {varname}")
    print(f"  建物数  : {len(features):,} 棟")
    print(f"\n次のステップ:")
    print(f"  {js_path.name} を index.html と同じフォルダに置いてください。")
    print("=" * 60)


if __name__ == "__main__":
    main()
