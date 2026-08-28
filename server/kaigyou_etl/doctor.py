"""Check that everything the app needs is actually in place.

A 500 from the API says nothing about *why*: a missing config file, an
unreachable database and an unapplied migration all look identical from the
browser. This walks the same dependencies the API has, in order, and names the
first thing that is wrong along with what to do about it.
"""
from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any

from kaigyou_core.analysis import DEFAULT_CATEGORY

OK = "OK"
WARN = "警告"
FAIL = "NG"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", fix: str = "") -> Check:
        check = Check(name, status, detail, fix)
        self.checks.append(check)
        return check

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]


# --------------------------------------------------------------------- checks
def _check_config(report: Report) -> bool:
    from kaigyou_core import config as cfg

    try:
        root = cfg.repo_root()
    except Exception as exc:  # noqa: BLE001
        report.add("設定の場所", FAIL, f"{type(exc).__name__}: {exc}")
        return False

    report.add("リポジトリの場所", OK, str(root))

    ok = True
    for label, loader, filename in (
        ("データソース定義", cfg.sources_config, "config/sources.yaml"),
        ("スコアリング設定", cfg.scoring_config, "config/scoring.yaml"),
    ):
        try:
            data = loader()
        except Exception as exc:  # noqa: BLE001
            ok = False
            report.add(
                label, FAIL, f"{type(exc).__name__}: {exc}",
                "リポジトリ直下から実行するか、環境変数 KAIGYOU_ROOT に "
                "リポジトリのパスを設定してください。",
            )
            continue
        if not data:
            ok = False
            report.add(label, FAIL, f"{filename} が空です")
        else:
            report.add(label, OK, filename)
    return ok


def _check_scoring_model(report: Report) -> None:
    from kaigyou_core import config as cfg
    from kaigyou_core.scoring import ScoringModel

    try:
        scoring = cfg.scoring_config()
        names = list((scoring.get("profiles") or {}))
        for name in names:
            ScoringModel(scoring, name)
    except Exception as exc:  # noqa: BLE001
        report.add("スコアリングモデル", FAIL, f"{type(exc).__name__}: {exc}",
                   "config/scoring.yaml の内容を確認してください。")
        return
    if not names:
        report.add("スコアリングモデル", FAIL, "profiles が1つも定義されていません")
        return
    report.add("スコアリングモデル", OK, f"プロファイル: {', '.join(names)}")


def _check_database(report: Report, stack: ExitStack) -> Any:
    from kaigyou_core.db import connect, dsn

    url = dsn()
    shown = url
    if "@" in url and "//" in url:
        head, tail = url.split("//", 1)
        creds, host = tail.split("@", 1)
        user = creds.split(":", 1)[0]
        shown = f"{head}//{user}:***@{host}"
    source = "DATABASE_URL" if os.getenv("DATABASE_URL") else "既定値"
    report.add("接続先", OK, f"{shown}  ({source})")

    try:
        conn = stack.enter_context(connect())
    except Exception as exc:  # noqa: BLE001
        report.add(
            "データベース接続", FAIL, f"{type(exc).__name__}: {exc}",
            "PostgreSQL が起動しているか、DATABASE_URL が正しいか確認してください。",
        )
        return None
    report.add("データベース接続", OK)
    return conn


def _check_postgis(report: Report, conn: Any) -> bool:
    from kaigyou_core.db import fetch_one

    try:
        row = fetch_one(conn, "SELECT postgis_version() AS v")
    except Exception as exc:  # noqa: BLE001
        report.add("PostGIS", FAIL, f"{type(exc).__name__}: {exc}",
                   "PostGIS 拡張が入ったデータベースを使ってください。")
        return False
    report.add("PostGIS", OK, row["v"])
    return True


def _check_migrations(report: Report, conn: Any) -> bool:
    from kaigyou_etl.migrate import applied, migrations_dir

    available = sorted(p.name for p in migrations_dir().glob("*.sql"))
    try:
        done = applied(conn)
    except Exception as exc:  # noqa: BLE001
        report.add("マイグレーション", FAIL, f"{type(exc).__name__}: {exc}")
        return False

    missing = [name for name in available if name not in done]
    if missing:
        report.add("マイグレーション", FAIL,
                   f"未適用: {', '.join(missing)}",
                   "kaigyou-etl migrate を実行してください。")
        return False
    report.add("マイグレーション", OK, f"{len(available)} 件すべて適用済み")
    return True


#: (table, label, required). Not every dataset is needed for the app to work.
#: The street network only adds a second shape of trade area; without it the
#: analysis runs on circles exactly as it always has, so reporting its absence
#: as a failure would send someone hunting for a problem that is not one.
TABLES = (
    ("facilities", "歯科医院", True),
    ("population_mesh", "人口メッシュ", True),
    ("mesh_business", "事業所・従業者メッシュ", True),
    ("walk_network", "街路ネットワーク", False),
    ("stations", "駅", True),
    ("municipalities", "行政区域", True),
    ("land_prices", "地価公示", False),
    ("facility_specialties", "診療科目", False),
)


def _check_data(report: Report, conn: Any) -> None:
    from kaigyou_core.db import fetch_one

    empty, optional_empty = [], []
    for table, label, required in TABLES:
        try:
            row = fetch_one(
                conn,
                f"""
                SELECT count(*) AS n,
                       count(*) FILTER (
                           WHERE source_id IN (SELECT id FROM data_sources
                                                WHERE dataset_kind = 'sample')
                       ) AS sample
                FROM {table}
                """,
            )
        except Exception as exc:  # noqa: BLE001
            report.add(f"{label}テーブル", FAIL, f"{type(exc).__name__}: {exc}")
            continue
        if row["n"] == 0:
            (empty if required else optional_empty).append(label)
            report.add(f"{label}テーブル", WARN,
                       "0件" if required else "0件（任意。無い場合は円の商圏のみ）")
        else:
            note = f"{row['n']:,}件"
            if row["sample"]:
                note += f"（うち合成データ {row['sample']:,}件）"
            report.add(f"{label}テーブル", OK, note)

    if optional_empty:
        report.add(
            "任意データ", WARN, f"未取得: {', '.join(optional_empty)}",
            "無くても分析は動きます。徒歩圏の商圏には OpenStreetMap の道路データ、"
            "地価の表示には国土数値情報 L01 を download フォルダに置いてください。",
        )
    if empty:
        report.add(
            "実データの投入", FAIL, f"空のテーブル: {', '.join(empty)}",
            "kaigyou-etl load-local <ダウンロードフォルダ> を実行してください。",
        )


def _check_scores(report: Report, conn: Any) -> None:
    from kaigyou_core.db import fetch_one

    try:
        stats = fetch_one(conn, "SELECT count(*) AS n FROM metric_distributions")
        scores = fetch_one(conn, "SELECT count(*) AS n FROM mesh_scores")
    except Exception as exc:  # noqa: BLE001
        report.add("スコア", FAIL, f"{type(exc).__name__}: {exc}")
        return

    if stats["n"] == 0:
        report.add("スコア基準", WARN, "未計算",
                   "kaigyou-etl refresh-stats を実行してください。")
    else:
        report.add("スコア基準", OK, f"{stats['n']} 指標")

    if scores["n"] == 0:
        report.add("メッシュスコア", WARN, "未計算（ランキングとヒートマップが空になります）",
                   "kaigyou-etl compute-scores を実行してください。")
    else:
        report.add("メッシュスコア", OK, f"{scores['n']:,} メッシュ")


def _check_specialties(report: Report, conn: Any) -> None:
    """診療科目データが、どの都道府県のどれだけの医院を覆っているか。

    件数が 0 でないことだけでは足りません。この抽出は都道府県によって全部
    入っていたり一部しか無かったりします。被覆率が低い県では、科目別の件数は
    「その科目が少ない」ではなく「分かっていない」を意味するので、そこを
    黙って通すと読み手が競合を数え違えます。

    科目を絞ったプロファイルが設定にあるのにデータが無い、という組み合わせも
    ここで言います。順位が出ないまま「未算出」だけが並ぶより、なぜ出ないかを
    先に言ったほうがいい。
    """
    from kaigyou_core import config as cfg
    from kaigyou_core.db import fetch_one, table_exists
    from kaigyou_core.scoring import competition_specialties

    scoped = competition_specialties(cfg.scoring_config())
    labels = "、".join(sorted({s for _pop, s in scoped}))

    if not table_exists(conn, "facility_features"):
        report.add("診療科目", WARN, "テーブルがありません",
                   "kaigyou-etl migrate を実行してください。")
        return

    row = fetch_one(conn, "SELECT count(*) AS n FROM facility_features")
    if not row or row["n"] == 0:
        detail = "未取得（競合は歯科医院すべてで数えます）"
        if scoped:
            detail += f"。標榜科目を絞ったプロファイル（{labels}）は順位を算出できません"
        report.add("診療科目", WARN, detail,
                   "032_dental_speciality_hours_*.csv を download フォルダに置いて "
                   "kaigyou-etl load-local を実行してください。")
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.prefecture_code AS pref, count(*)::int AS clinics,
                   count(ff.facility_id)::int AS with_data
            FROM facilities f
            LEFT JOIN facility_features ff ON ff.facility_id = f.facility_id
            WHERE f.facility_category = %s
            GROUP BY f.prefecture_code
            HAVING count(*) > 0
            ORDER BY f.prefecture_code
            """, (DEFAULT_CATEGORY,))
        rows = cur.fetchall()

    covered = [r for r in rows if r["with_data"]]
    thin = [r for r in covered if r["with_data"] / r["clinics"] < 0.8]
    report.add("診療科目", OK,
               f"{row['n']:,} 医院／{len(covered)} 都道府県"
               + (f"（競合を絞るプロファイル: {labels}）" if scoped else ""))
    if thin:
        detail = "、".join(
            f"{r['pref']}: {r['with_data']:,}/{r['clinics']:,}"
            f"（{r['with_data'] / r['clinics'] * 100:.0f}%）" for r in thin[:6])
        report.add("診療科目の被覆", WARN,
                   f"科目が分からない医院が多い都道府県があります — {detail}",
                   "その都道府県では標榜科目別の件数・スコアは算出されません。"
                   "全国版の 032 ファイルを取り込むと解消します。")
    else:
        report.add("診療科目の被覆", OK,
                   "、".join(f"{r['pref']}: {r['with_data'] / r['clinics'] * 100:.0f}%"
                            for r in covered[:8])
                   + ("…" if len(covered) > 8 else ""))


def _check_intelligence(report: Report, conn: Any) -> None:
    """商圏インテリジェンス・エンジンが動かせる状態か。

    足りないものは 3 種類あり、直し方が違います。テーブル（migrate）、
    パッケージ（pip）、APIキー（環境変数）。まとめて「使えません」と言うと、
    どれを直せばよいか分かりません。
    """
    from kaigyou_core.db import table_exists

    tables = ["analysis_jobs", "analysis_steps", "analysis_sources", "analysis_reports"]
    missing = [t for t in tables if not table_exists(conn, t)]
    if missing:
        report.add("商圏分析エンジン", WARN, f"テーブルがありません: {', '.join(missing)}",
                   "kaigyou-etl migrate を実行してください。")
        return

    try:
        import anthropic  # noqa: F401
    except ImportError:
        report.add("商圏分析エンジン", WARN, "anthropic パッケージが未導入",
                   "pip install anthropic を実行してください。")
        return

    from kaigyou_intel import client as llm

    if not llm.is_configured():
        report.add("商圏分析エンジン", WARN, "ANTHROPIC_API_KEY が未設定",
                   "https://console.anthropic.com でキーを作り、"
                   "$env:ANTHROPIC_API_KEY = 'sk-ant-...' を設定してください。"
                   "（設定前でも analyze --dry-run で送信内容は確認できます）")
    else:
        settings = llm.step_settings(1)
        report.add("商圏分析エンジン", OK,
                   f"実行可能（{settings['model']} / {settings['prompt_version']}）")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT status, count(*) AS n FROM analysis_jobs GROUP BY status
        """)
        counts = {r["status"]: r["n"] for r in cur.fetchall()}
    if counts:
        report.add("分析ジョブ", OK,
                   "、".join(f"{k} {v}件" for k, v in sorted(counts.items()))
                   + ("  → kaigyou-etl analyze で実行できます"
                      if counts.get("queued") else ""))


def _check_walk_network(report: Report, conn: Any) -> None:
    """Whether a walking catchment can be produced, and what is missing if not.

    Four things have to line up -- the extension, the loaded streets, the noded
    graph and the routing function -- and when one of them is absent the app
    quietly falls back to a circle. Quietly is right for a user and wrong for
    an operator, who otherwise has to work out from the shape of a polygon
    which of the four it was.

    The last step actually runs the query, at a point taken from the network so
    the walking branch is the one exercised. A message like "function
    pgr_drivingdistance does not exist" belongs here, in a command with a name,
    rather than in a 500 whose text is "Internal Server Error".
    """
    from kaigyou_core.db import fetch_one, table_exists

    if not table_exists(conn, "walk_network"):
        return  # 009 not applied; the migration check has already said so.

    edges = (fetch_one(conn, "SELECT count(*) AS n FROM walk_network") or {})["n"]
    ext = fetch_one(conn, "SELECT extversion AS v FROM pg_extension "
                          "WHERE extname = 'pgrouting'")
    if ext is None:
        report.add(
            "pgRouting", WARN,
            f"未インストール（街路 {edges:,} 本を取り込み済みだが徒歩圏は算出できない）",
            "PostgreSQL に pgRouting を追加し、対象DBで CREATE EXTENSION pgrouting; "
            "を実行してから kaigyou-etl migrate と load-local をやり直してください。"
            "商圏は円のまま使えます。")
        return
    report.add("pgRouting", OK, ext["v"])

    if edges == 0:
        report.add("街路ネットワーク", WARN, "未取得（徒歩圏は円にフォールバック）",
                   "OpenStreetMap の道路 shapefile を download/ に置いて "
                   "kaigyou-etl load-local を実行してください。")
        return

    if not table_exists(conn, "walk_network_noded"):
        report.add("街路トポロジ", WARN, f"未構築（街路 {edges:,} 本）",
                   "kaigyou-etl load-local を再実行してください"
                   "（pgr_nodeNetwork が使えないと構築は飛ばされます）。")
        return

    noded = (fetch_one(conn, "SELECT count(*) AS n FROM walk_network_noded") or {})["n"]
    routable = (fetch_one(conn, "SELECT count(*) AS n FROM walk_network_noded "
                                "WHERE source IS NOT NULL AND target IS NOT NULL")
                or {})["n"]
    if not routable:
        report.add("街路トポロジ", FAIL,
                   f"分割後 {noded:,} 本のどれにも source/target がない",
                   "kaigyou-etl load-local を再実行してください"
                   "（pgr_createTopology が完了していません）。")
        return
    report.add("街路トポロジ", OK, f"街路 {edges:,} 本 / 分割後 {noded:,} 本")

    # The real thing, at a point that definitely has streets around it.
    point = fetch_one(conn, "SELECT ST_Y(the_geom) AS lat, ST_X(the_geom) AS lng "
                            "FROM walk_network_noded_vertices_pgr LIMIT 1")
    if not point:
        return
    mesh = fetch_one(conn, "SELECT mesh_size_m AS m FROM population_mesh LIMIT 1")
    try:
        row = fetch_one(
            conn,
            "SELECT catchment_kind AS kind, catchment_area_km2 AS km2 "
            "FROM kg_analyze_point(%s, %s, 500, %s, %s, 'walk')",
            (point["lat"], point["lng"], DEFAULT_CATEGORY,
             (mesh or {}).get("m") or 500),
        ) or {}
    except Exception as exc:  # noqa: BLE001 - this is the message worth having
        conn.rollback()  # a failed statement poisons the rest of the checks
        report.add("徒歩圏の算出", FAIL, f"{type(exc).__name__}: {exc}",
                   "この行をそのまま報告してください。商圏の形を「円」にすれば"
                   "分析は続けられます。")
        return

    if row.get("kind") != "walk":
        report.add("徒歩圏の算出", WARN,
                   "街路の上の地点でも円になった（到達可能な街路が見つからない）",
                   "pgr_nodeNetwork のトレランス（sources.yaml の "
                   "topology_tolerance_deg）を見直してください。")
        return
    report.add("徒歩圏の算出", OK, f"半径500m で {row['km2']:.2f} km²")


def _check_api_surface(report: Report, conn: Any) -> None:
    """Exercise the query behind the endpoint the UI calls first."""
    from kaigyou_core.status import data_status

    try:
        status = data_status(conn)
    except Exception as exc:  # noqa: BLE001
        report.add("GET /api/data-status", FAIL, f"{type(exc).__name__}: {exc}")
        return
    detail = (f"公的データ {status['official_sources_loaded']}"
              f"/{status['official_sources_configured']}")
    if status["contains_sample_data"]:
        detail += " ・合成データあり"
    report.add("GET /api/data-status", OK, detail)
    for conflict in status.get("mixed_datasets") or []:
        report.add("データ混在", FAIL, conflict["message"],
                   "kaigyou-etl drop-sample を実行してください。")

    _check_the_analysis_itself(report, conn)


def _check_prefectures(report: Report, conn: Any) -> None:
    """What is loaded, per prefecture, and at what resolution.

    Two prefectures published at different mesh resolutions is not an error and
    is invisible until it produces "no population here" in the middle of a city:
    a point analysed under one prefecture's resolution finds nothing in the
    other's meshes. Printing both makes the mismatch a line of output instead
    of a mystery.
    """
    from kaigyou_core.db import fetch_one

    with conn.cursor() as cur:
        cur.execute("""
            SELECT prefecture_code AS code, mesh_size_m AS size,
                   count(*)::int AS meshes, sum(COALESCE(population, 0))::bigint AS pop
            FROM population_mesh
            GROUP BY prefecture_code, mesh_size_m
            ORDER BY prefecture_code, mesh_size_m
        """)
        rows = cur.fetchall()
    if not rows:
        return

    from kaigyou_core.analysis import prefecture_name

    for row in rows:
        name = prefecture_name(conn, row["code"])
        # The spread, not just the count. Scores computed against another
        # prefecture's distribution all clamp to the same end of the scale, and
        # a heat map of one flat colour is indistinguishable from a heat map of
        # nothing -- both look like the feature is broken.
        scored = fetch_one(conn, """
            SELECT count(*) AS n,
                   round(min(ms.overall_score)::numeric, 1) AS lo,
                   round(max(ms.overall_score)::numeric, 1) AS hi,
                   count(DISTINCT round(ms.overall_score::numeric, 0)) AS steps
            FROM mesh_scores ms
            JOIN population_mesh pm ON pm.id = ms.mesh_id
            WHERE pm.prefecture_code = %s AND ms.overall_score IS NOT NULL
        """, (row["code"],)) or {"n": 0}
        detail = (f"{row['meshes']:,}メッシュ / {row['size']}m / "
                  f"人口 {row['pop']:,} / スコア済 {scored['n']:,}")
        if scored["n"]:
            detail += f"（{scored['lo']}〜{scored['hi']}）"
        if scored["n"] and (scored["steps"] or 0) < 5:
            report.add(f"  {name}({row['code']})", WARN,
                       detail + " ← ほぼ同じ値。ヒートマップが一色になります",
                       f"別の都道府県の分布で正規化された可能性があります。"
                       f"refresh-stats --prefecture {row['code']} を実行してから "
                       f"compute-scores --all-profiles --prefecture {row['code']} "
                       "をやり直してください。")
        elif scored["n"]:
            report.add(f"  {name}({row['code']})", OK, detail)
        else:
            report.add(f"  {name}({row['code']})", WARN, detail + "（ランキング・ヒートマップが空）",
                       f"kaigyou-etl refresh-stats --prefecture {row['code']} と "
                       f"compute-scores --all-profiles --prefecture {row['code']} を実行してください。")

    sizes = {row["size"] for row in rows}
    if len(sizes) > 1:
        report.add("メッシュ解像度", WARN,
                   f"都道府県によって異なります: {sorted(sizes)}",
                   "解像度が違うと、地点分析は地点のある都道府県の解像度で行われます。"
                   "揃えたい場合は e-Stat で同じ解像度のファイルを取り直してください。")


def _check_the_analysis_itself(report: Report, conn: Any) -> None:
    """Run the query the map runs, at a point where there is data.

    Every check above this one asks whether something *exists*. That is not
    the same question as whether it works: a migration can apply cleanly and
    leave kg_analyze_point raising on every call, and then `doctor` reports no
    problems while the app answers 500 to the one request it is for. The
    difference matters most right after a migration, which is exactly when
    nobody is looking for it.

    Deployed, the API withholds the exception text on purpose, so this may be
    the only place the operator can see it at all.
    """
    from kaigyou_core.analysis import analyze_point
    from kaigyou_core.db import fetch_one

    point = fetch_one(conn, """
        SELECT ST_Y(centroid) AS lat, ST_X(centroid) AS lng, mesh_size_m AS m
        FROM population_mesh
        WHERE COALESCE(population, 0) > 0
        LIMIT 1
    """)
    if not point:
        return  # nothing loaded; the data check has already said so

    try:
        row = analyze_point(conn, point["lat"], point["lng"], 1000,
                            mesh_size_m=point["m"])
    except Exception as exc:  # noqa: BLE001 - the message is the whole point
        conn.rollback()
        report.add("GET /api/candidate-analysis", FAIL, f"{type(exc).__name__}: {exc}",
                   "分析の本体クエリが失敗しています。この行をそのまま報告してください。"
                   "マイグレーション直後なら kaigyou-etl migrate の再実行で直ることがあります。")
        return

    report.add("GET /api/candidate-analysis", OK,
               f"半径1km で人口 {row.get('population') or 0:,.0f} 人 / "
               f"歯科 {row.get('facility_count') or 0} 件")


# ----------------------------------------------------------------------- run
def run() -> Report:
    report = Report()
    if not _check_config(report):
        return report
    _check_scoring_model(report)

    with ExitStack() as stack:
        conn = _check_database(report, stack)
        if conn is None:
            return report
        if not _check_postgis(report, conn):
            return report
        if not _check_migrations(report, conn):
            return report
        _check_data(report, conn)
        _check_prefectures(report, conn)
        _check_scores(report, conn)
        _check_specialties(report, conn)
        _check_intelligence(report, conn)
        _check_walk_network(report, conn)
        _check_api_surface(report, conn)
    return report


def render(report: Report) -> list[str]:
    width = max((len(c.name) for c in report.checks), default=10)
    lines = ["セットアップ診断", "=" * 72]
    for check in report.checks:
        lines.append(f"[{check.status:<4}] {check.name:<{width}}  {check.detail}")
        if check.fix:
            lines.append(f"         → {check.fix}")
    lines.append("=" * 72)
    if report.failed:
        lines.append(f"{len(report.failed)} 件の問題があります。上の → を順に実行してください。")
    elif report.warned:
        lines.append("動作しますが、未実施の手順があります。")
    else:
        lines.append("問題ありません。")
    return lines
