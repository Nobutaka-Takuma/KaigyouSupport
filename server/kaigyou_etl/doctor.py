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


TABLES = (
    ("facilities", "歯科医院"),
    ("population_mesh", "人口メッシュ"),
    ("stations", "駅"),
    ("municipalities", "行政区域"),
)


def _check_data(report: Report, conn: Any) -> None:
    from kaigyou_core.db import fetch_one

    empty = []
    for table, label in TABLES:
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
            empty.append(label)
            report.add(f"{label}テーブル", WARN, "0件")
        else:
            note = f"{row['n']:,}件"
            if row["sample"]:
                note += f"（うち合成データ {row['sample']:,}件）"
            report.add(f"{label}テーブル", OK, note)

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
        _check_scores(report, conn)
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
