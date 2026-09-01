"""都市計画決定情報（A55）の取り込み。

この層が答えるのは「そこに何人いるか」ではなく **「そこに何が建てられるか」**
です。用途地域・容積率・建蔽率は地価公示も持っていますが、地価公示は点で、
静岡県で 3,221 点しかありません。A55 は面なので、候補地がどの区域に入るかが
決まります。

守りたい失敗は 3 つ。どれも**成功と表示されたまま間違う**種類のものです。

1. 1 市の 1 層だけ読んで成功と出る（A55 は 1 県 233 ファイル）
2. 2 県目を入れた時点で 1 県目が消える（012・031 と同じ形）
3. 容積率の空欄が 0 で入る（「定めが無い」と「建てられない」は別）
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from kaigyou_core import config as cfg
from kaigyou_core.db import connect, table_exists
from kaigyou_etl.adapters import AdapterContext, get_adapter

FIXTURE = Path(__file__).parent / "fixtures" / "mlit_a55_city_planning.zip"
SOURCE_ID = "__a55_test__"

#: 沼津市の商業地域の中（フィクスチャの箱の真ん中）。
IN_COMMERCIAL = (35.095, 138.805)
#: 沼津市の市街化調整区域の**穴の中**。穴が落ちると、ここが調整区域に見えます。
IN_HOLE = (35.095, 138.8215)


def _adapter(prefecture: str = "22"):
    spec = dict(cfg.sources_config()["sources"]["mlit_city_planning"])
    ctx = AdapterContext(source_id=SOURCE_ID, spec=spec, defaults={},
                         raw_dir=Path("data/raw/a55_test"), input_path=FIXTURE,
                         offline=True, prefecture_override=prefecture)
    return get_adapter("mlit_city_planning")(ctx)


@pytest.fixture(scope="module")
def loaded():
    try:
        with connect() as probe:
            if not table_exists(probe, "city_planning_zones"):
                pytest.skip("city_planning_zones not migrated here")
    except psycopg.OperationalError:
        pytest.skip("no database")

    adapter = _adapter()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO data_sources (id, name, publisher, dataset_kind)
                           VALUES (%s, 'test city planning', 'test', 'sample')
                           ON CONFLICT (id) DO NOTHING""", (SOURCE_ID,))
        conn.commit()
        facts = adapter.validate(FIXTURE)
        adapter.load(conn, adapter.transform(FIXTURE))
    yield facts
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM city_planning_zones WHERE source_id = %s", (SOURCE_ID,))
            cur.execute("DELETE FROM data_sources WHERE id = %s", (SOURCE_ID,))
        conn.commit()


def _rows(**where):
    clause = " AND ".join(f"{k} = %s" for k in where)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM city_planning_zones WHERE source_id = %s"
            + (f" AND {clause}" if where else ""),
            (SOURCE_ID, *where.values()))
        return cur.fetchall()


def test_every_municipality_and_every_layer_is_read(loaded):
    """**1 つ読んで終わりにしない。**

    A55 は 1 県のアーカイブが市区町村ごとのフォルダに分かれ、その中に層ごとの
    shapefile が入ります（静岡県で 32 フォルダ・233 ファイル）。最初の 1 つを
    読んで返す実装は、1 市の 1 層を取り込んで「成功」と表示します。
    """
    rows = _rows()
    assert {r["municipality_code"] for r in rows} == {"22203", "22100"}
    assert {r["zone_kind"] for r in rows} == {"youto", "senbiki"}
    assert len(rows) == 5, [r["zone_type"] for r in rows]
    assert loaded["features_by_layer"] == {"youto": 3, "senbiki": 2}


def test_the_prefecture_comes_from_the_municipality_code_not_from_the_run(loaded):
    """走らせるときに何を渡されても、県は市区町村コードの上 2 桁。

    取り違えたまま入ると、静岡の区域が東京の名前で引かれます。しかも
    取り込みは成功と表示します。
    """
    adapter = _adapter(prefecture="13")            # わざと東京だと言って走らせる
    rows = list(adapter.transform(FIXTURE))
    assert {r["prefecture_code"] for r in rows} == {"22"}


def test_an_empty_floor_area_ratio_is_unknown_not_zero(loaded):
    """空欄を 0 で埋めない。

    容積率 0% の土地は存在しません。0 で埋めると「何も建てられない場所」に
    見えます。「定めが無い」と「取り込んでいない」も別の話です。
    """
    commercial = _rows(zone_type="商業地域")[0]
    assert float(commercial["far"]) == 400
    assert float(commercial["bcr"]) == 80

    industrial = _rows(zone_type="工業専用地域")[0]
    assert industrial["far"] is None
    assert industrial["bcr"] is None


def test_a_hole_in_a_zone_stays_a_hole(loaded):
    """内側のリングを落とさない。

    市街化調整区域は市街化区域を囲む輪の形をしています。穴が落ちると、
    **市街化区域の中の候補地が「原則建てられない」と判定されます。**
    """
    lat, lng = IN_HOLE
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT count(*) AS n FROM city_planning_zones
                       WHERE source_id = %s AND zone_kind = 'senbiki'
                         AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s,%s),4326))""",
                    (SOURCE_ID, lng, lat))
        assert cur.fetchone()["n"] == 0, "穴の中が調整区域に入っています"


def test_the_point_finds_the_zone_it_is_in(loaded):
    lat, lng = IN_COMMERCIAL
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT zone_kind, zone_type, far FROM city_planning_zones
                       WHERE source_id = %s
                         AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s,%s),4326))
                       ORDER BY zone_kind""", (SOURCE_ID, lng, lat))
        found = {r["zone_kind"]: r for r in cur.fetchall()}
    assert found["youto"]["zone_type"] == "商業地域"
    assert float(found["youto"]["far"]) == 400
    assert found["senbiki"]["zone_type"] == "市街化調整区域"


def test_loading_one_prefecture_leaves_another_alone(loaded):
    """**2 県目が 1 県目を消さないこと。**

    取り込みは source_id で全置換します。source_id に県は入っていないので、
    絞らずに消すと東京を入れた時点で静岡が消えます——しかも成功と表示します。
    メッシュ統計（012）と街路網（031）で同じことが起きました。
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO city_planning_zones (
                    source_id, prefecture_code, municipality_code, zone_kind,
                    zone_kind_label, zone_type, geom)
                VALUES (%s, '13', '13101', 'youto', '用途地域', '商業地域',
                        ST_Multi(ST_GeomFromText(
                          'POLYGON((139.76 35.68,139.76 35.69,139.77 35.69,139.77 35.68,139.76 35.68))',
                          4326)))""", (SOURCE_ID,))
        conn.commit()
        adapter = _adapter()
        adapter.load(conn, adapter.transform(FIXTURE))   # 静岡をもう一度

    assert len(_rows(prefecture_code="13")) == 1, "東京の行が静岡の取り込みで消えました"
    assert len(_rows(prefecture_code="22")) == 5, "静岡が入れ替わっていません"

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM city_planning_zones "
                        "WHERE source_id = %s AND prefecture_code = '13'", (SOURCE_ID,))
        conn.commit()


def test_the_summary_names_what_was_not_loaded(loaded):
    """入れなかったものを黙って落とさない。

    都市計画道路は線なので、面の表には入りません。要約に名前が出ていないと、
    「A55 を取り込んだ」と読んだ人はそれも入っていると思います。
    """
    assert "douro" in loaded["not_loaded"]


# --------------------------------------------------------------- 地図レイヤー
def _client():
    from fastapi.testclient import TestClient
    from kaigyou_api.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_the_map_only_offers_layers_that_have_something_in_them(loaded):
    """**固定の一覧を画面に持たせない。**

    都市計画を取り込んでいない県で「用途地域」と書いた選択肢を出すと、
    選んでも何も出ない画面になります。件数を返すので 0 件の層は消せます。
    """
    body = _client().get("/api/city-planning/kinds",
                         params={"prefecture_code": "22"}).json()
    assert body["available"]
    kinds = {k["kind"]: k for k in body["kinds"]}
    assert "youto" in kinds and kinds["youto"]["features"] > 0
    assert all(k["features"] > 0 for k in body["kinds"])
    assert all(k["label"] for k in body["kinds"])


def test_the_layer_returns_one_kind_at_a_time(loaded):
    """層を混ぜて返さない。

    用途地域・区域区分・誘導区域は同じ場所に重なって存在するので、まとめて
    塗ると下の色が見えず、押してもどれを押したのか分かりません。
    """
    body = _client().get("/api/city-planning",
                         params={"kind": "senbiki", "bbox": "138.79,35.07,138.84,35.12"}).json()
    assert body["features"]
    assert set(body["zones"]) <= {"市街化区域", "市街化調整区域"}, body["zones"].keys()


def test_each_feature_carries_the_words_the_popup_needs(loaded):
    """吹き出しの文はサーバが組み立てる。

    画面で文言を作ると、地図とレポートが別のことを言い出します。区分の説明も
    建築の可否も、データセットが使うのと同じ設定から来ます。
    """
    body = _client().get("/api/city-planning",
                         params={"kind": "youto", "bbox": "138.79,35.08,138.83,35.11"}).json()
    props = {f["properties"]["zone_type"]: f["properties"] for f in body["features"]}
    zones = body["zones"]

    assert props["商業地域"]["zone_key"] == "商業地域"
    assert props["商業地域"]["far"] == 400 and props["商業地域"]["bcr"] == 80
    assert "容積率が高く" in zones["商業地域"]["description"]
    assert zones["商業地域"]["buildable"] is True
    assert body["facility_label"] == "診療所"

    assert zones["工業専用地域"]["buildable"] is False
    assert zones["工業専用地域"]["note"]
    # **空欄は 0 ではありません。** 容積率 0% の土地はありません。
    assert props["工業専用地域"]["far"] is None

    assert body["disclaimer"] and "特定行政庁" in body["disclaimer"]


def test_the_colour_key_is_normalised_by_the_server(loaded):
    """画面が色を引く鍵は、正規化済みのものだけ。

    公表データは県によって「第１種」と「第一種」で揺れます。画面でもう一度
    正規化すると、直したはずの表記ゆれが片側に残ります。
    """
    from kaigyou_core import city_planning as plan

    body = _client().get("/api/city-planning",
                         params={"kind": "youto", "bbox": "138.79,35.08,138.83,35.11"}).json()
    for feature in body["features"]:
        props = feature["properties"]
        assert props["zone_key"] == plan.canonical(props["zone_type"])


def test_no_verdict_is_offered_for_a_zone_with_no_rule(loaded):
    """規則の無い区分に可否を書かない。**空欄のほうが安全です。**"""
    body = _client().get("/api/city-planning",
                         params={"kind": "senbiki", "bbox": "138.79,35.07,138.84,35.12",
                                 "category": "__no_such_business__"}).json()
    assert body["features"]
    assert body["zones"]
    assert all(z["buildable"] is None for z in body["zones"].values())


def test_the_words_are_sent_once_not_on_every_feature(loaded):
    """**容量の問題であって、見た目の問題ではありません。**

    説明文を面 1 件ずつに付けていました。静岡県全域の用途地域 2,946 件で
    応答は 3.5MB になり、そのうち 2.5MB が繰り返された同じ文でした。
    **Vercel の Serverless Function は応答が 4.5MB を超えると失敗します。**
    「都市計画を表示すると時折エラー」はこれです。
    """
    import json

    body = _client().get("/api/city-planning",
                         params={"kind": "youto", "bbox": "138.79,35.08,138.83,35.11"}).json()
    assert body["features"]
    for feature in body["features"]:
        props = feature["properties"]
        # 説明・可否・免責が 1 件ずつに乗っていないこと。
        assert not (set(props) & {"description", "buildable", "buildable_note",
                                  "facility_label", "zone_kind_label"}), props
        assert props["zone_key"]

    # 引く先は区分ごとに 1 回だけ。
    zone = body["zones"][body["features"][0]["properties"]["zone_key"]]
    assert set(zone) == {"label", "description", "buildable", "note"}
    assert body["facility_label"] == "診療所"

    # 面の数より区分の数がずっと少ないこと（そうでなければ畳めていない）。
    assert len(body["zones"]) <= len(body["features"])
    assert len(json.dumps(body, ensure_ascii=False).encode()) < 3_500_000


def test_a_wide_viewport_cannot_blow_past_the_payload_limit(loaded):
    """件数ではなく容量で切ること。

    同じ 3,000 件でも、区域区分の大きな面と用途地域の小さな面では容量が桁で
    違います。件数の上限だけでは 4.5MB を守れません。
    """
    import json
    from kaigyou_api.routers.layers import CITY_PLANNING_BYTE_BUDGET

    body = _client().get("/api/city-planning",
                         params={"kind": "youto", "bbox": "120,20,150,50"}).json()
    geometry = sum(len(json.dumps(f["geometry"], ensure_ascii=False).encode())
                   for f in body["features"])
    assert geometry <= CITY_PLANNING_BYTE_BUDGET * 1.2


def test_a_wide_viewport_is_simplified_even_if_the_caller_forgets(loaded):
    """画面が細かい許容誤差を渡してきても、広い範囲なら粗くします。

    画面の縮尺の判断に応答の大きさを預けません。**古い画面を開いたままの
    利用者が、直したはずの不具合を踏み続けます。**
    """
    from kaigyou_api.routers.layers import _simplify_floor

    wide = (137.5, 34.5, 139.2, 35.7)     # 県ほどの広さ
    close = (138.89, 35.16, 138.93, 35.19)  # 市街地
    assert _simplify_floor(wide, 0.00005) >= 0.0005
    assert _simplify_floor(close, 0.00005) == 0.00005
    # 呼ぶ側がもっと粗いものを求めたら、そちらを尊重します。
    assert _simplify_floor(close, 0.001) == 0.001
