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
