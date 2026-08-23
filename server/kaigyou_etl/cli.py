"""``kaigyou-etl`` -- the command line for everything that touches data.

    kaigyou-etl migrate
    kaigyou-etl list
    kaigyou-etl run <source> [--input FILE] [--offline]
    kaigyou-etl run-all [--offline]
    kaigyou-etl load-local <dir> [--dry-run]
    kaigyou-etl generate-sample / drop-sample
    kaigyou-etl refresh-stats
    kaigyou-etl compute-scores [--profile NAME]
    kaigyou-etl status [--json]
    kaigyou-etl doctor

The API server never invokes any of these; loading data is an operator
action.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from kaigyou_core import config as cfg
from kaigyou_core.db import connect
from kaigyou_core.status import data_status

EXIT_OK = 0
EXIT_PARTIAL = 2
EXIT_ERROR = 1


def _json_default(obj: Any) -> str:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


# --------------------------------------------------------------------- commands
def cmd_migrate(args: argparse.Namespace) -> int:
    from kaigyou_etl.migrate import migrate

    with connect() as conn:
        applied = migrate(conn, force=args.force)
    if applied:
        print("applied:", ", ".join(applied))
    else:
        print("schema already up to date")
    return EXIT_OK


def cmd_list(_args: argparse.Namespace) -> int:
    sources = (cfg.sources_config().get("sources") or {})
    for source_id, spec in sources.items():
        print(f"{source_id:26s} {spec.get('adapter','?'):20s} "
              f"{spec.get('dataset_kind','official'):9s} {spec.get('publisher','')}")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    from kaigyou_etl.pipeline import run_source

    result = run_source(
        args.source,
        input_path=Path(args.input) if args.input else None,
        offline=args.offline,
        # Both meanings, because --prefecture answers both questions: which
        # rows to keep from a nationwide file, and which prefecture a
        # single-prefecture file is *of* (the e-Stat mesh tables say so only in
        # their file name).
        prefecture_filter=args.prefecture,
        prefecture_code=args.prefecture,
        baseline_path=Path(args.baseline) if args.baseline else None,
    )
    _print_run(result)
    return EXIT_OK if result.ok else EXIT_PARTIAL


def cmd_run_all(args: argparse.Namespace) -> int:
    from kaigyou_etl.pipeline import run_source

    sources = (cfg.sources_config().get("sources") or {})
    failures = 0
    for source_id in sources:
        try:
            result = run_source(source_id, offline=args.offline)
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            print(f"[{source_id}] ERROR {type(exc).__name__}: {exc}")
            failures += 1
            continue
        _print_run(result)
        failures += 0 if result.ok else 1

    if failures:
        print(
            f"\n{failures}/{len(sources)} 件の情報源を取得できませんでした。"
            f"\n該当テーブルは空のままです（代替データは投入していません）。"
            f"\n詳細: kaigyou-etl status"
        )
        return EXIT_PARTIAL
    return EXIT_OK


def _print_run(result: Any) -> None:
    steps = " ".join(f"{k}={v}" for k, v in result.steps.items())
    status = "OK" if result.ok else "FAILED"
    print(f"[{result.source_id}] {status}  {steps}  records={result.record_count}")
    if result.error_message:
        print(f"    -> {result.error_type}: {result.error_message}")
    # Scale is worth seeing for the street network: it decides how long the
    # next step runs and whether the extract was clipped as intended.
    facts = (result.details or {}).get("validate") or {}
    if "edges" in facts:
        clipped = facts.get("excluded_outside_bbox") or 0
        print(f"    エッジ {facts['edges']:,} 本"
              + (f"（bbox 外として除外 {clipped:,} 本）" if clipped else ""))



def _prefecture_for(found: Any, requested: str | None) -> tuple[str, str | None]:
    """Which prefecture a folder of downloads is, and why.

    The prefecture-specific files name themselves after it. Trusting that over
    a flag is not a nicety: the mesh tables say nothing about the prefecture
    anywhere else, so a wrong flag writes real figures under another
    prefecture's label -- and the replace, being scoped by that label, deletes
    the prefecture it was mislabelled as.
    """
    from kaigyou_etl.adapters._util import prefecture_from_filename

    named: dict[str, list[str]] = {}
    for path in (found.mesh_current, found.mesh_baseline, found.mesh_business,
                 found.municipalities, found.land_prices):
        if path is None:
            continue
        code = prefecture_from_filename(path)
        if code:
            named.setdefault(code, []).append(Path(path).name)

    if len(named) > 1:
        detail = "、".join(f"{code}: {', '.join(files)}" for code, files in sorted(named.items()))
        return "", (
            f"error: このフォルダに複数の都道府県のファイルが混在しています（{detail}）。\n"
            "都道府県ごとにフォルダを分けて、1回ずつ取り込んでください。")

    detected = next(iter(named), None)
    if requested and detected and requested != detected:
        return "", (
            f"error: --prefecture {requested} が指定されていますが、"
            f"ファイル名は都道府県コード {detected} です"
            f"（{', '.join(named[detected])}）。\n"
            f"このまま取り込むと {requested} のデータが {detected} の数値で"
            "置き換わります。--prefecture を外すか、正しい値を指定してください。")
    if requested:
        return requested, None
    if detected:
        return detected, None
    return "", (
        "error: 都道府県を判定できませんでした。ファイル名に都道府県コードが"
        "含まれていません（例: tblT001141H13.txt、N03-20240101_13_GML.zip）。\n"
        "--prefecture 13 のように明示してください。")


def _pending_migrations() -> list[str]:
    """Migration files not yet recorded as applied. Empty when unreachable."""
    from kaigyou_etl.migrate import applied, migrations_dir

    try:
        with connect() as conn:
            done = applied(conn)
    except Exception:  # noqa: BLE001 - connection problems surface elsewhere
        return []
    return sorted(p.name for p in migrations_dir().glob("*.sql") if p.name not in done)


def cmd_load_local(args: argparse.Namespace) -> int:
    """Load every dataset found in a directory, then rebuild the scores.

    Files are matched on their contents rather than their names: the same S12
    archive is published as S12-25_GML.zip and arrives as S1225_GML.zip
    depending on the browser, and guessing wrong would look like a missing
    download rather than a naming mismatch.
    """
    from kaigyou_etl.discover import describe, discover
    from kaigyou_etl.pipeline import run_source

    directory = Path(args.directory).expanduser()
    try:
        found = discover(directory, cfg.sources_config().get("sources") or {})
    except NotADirectoryError:
        print(f"error: フォルダが見つかりません: {directory}", file=sys.stderr)
        return EXIT_ERROR

    print(f"{directory} の中身:")
    for line in describe(found):
        print(line)
    for path in found.unmatched:
        print(f"  [--  ] 判別できないファイル       {path.name}")
    for note in found.notes:
        print(f"  * {note}")
    print()

    if found.missing:
        print("次のファイルが見つかりません: " + "、".join(found.missing))
        print("README の「実データを表示するまで」を参照してください。")
        if not args.partial:
            print("見つかった分だけ取り込むには --partial を付けてください。")
            return EXIT_PARTIAL

    # Which prefecture this folder is. The published file names say so --
    # tblT001141H22 is Shizuoka, N03-20240101_22_GML is Shizuoka -- and until
    # this read them, `load-local download/22` without --prefecture tagged
    # Shizuoka's figures as Tokyo and deleted Tokyo on the way in. The load
    # reported success; only the population totals gave it away, days later.
    prefecture, problem = _prefecture_for(found, args.prefecture)
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_ERROR
    print(f"都道府県: {prefecture}"
          + ("（ファイル名から判定）" if not args.prefecture else "（--prefecture の指定）"))
    print()

    if args.dry_run:
        print("--dry-run のため、ここで終了します。")
        return EXIT_OK

    # A release that adds a dataset adds a table for it, and the load then dies
    # on `relation "..." does not exist` after parsing the whole file --
    # a database error for what is really a missed step. Say so before the work
    # rather than after it.
    pending = _pending_migrations()
    if pending:
        print("未適用のマイグレーションがあります: " + "、".join(pending))
        print("先に kaigyou-etl migrate を実行してください"
              "（新しい情報源はテーブルの追加を伴います）。")
        return EXIT_ERROR

    plan = [
        ("mhlw_dental_clinics", found.clinics, None),
        ("estat_population_mesh", found.mesh_current, found.mesh_baseline),
        ("estat_business_mesh", found.mesh_business, None),
        ("osm_walk_network", found.walk_network, None),
        ("mlit_stations", found.stations, None),
        ("mlit_municipalities", found.municipalities, None),
        ("mlit_land_prices", found.land_prices, None),
    ]
    failures = 0
    for source_id, input_path, baseline in plan:
        if input_path is None:
            continue
        result = run_source(source_id, input_path=input_path, baseline_path=baseline,
                            prefecture_code=prefecture)
        _print_run(result)
        failures += 0 if result.ok else 1

    if failures:
        print(f"\n{failures} 件の取り込みに失敗しました。詳細: kaigyou-etl status")
        return EXIT_PARTIAL

    # Synthetic rows left alongside real ones would be counted twice.
    from kaigyou_etl.sample.generate import drop
    with connect() as conn:
        removed = drop(conn)
    if any(removed.values()):
        print("\n合成（サンプル）データを削除:",
              ", ".join(f"{k}={v}" for k, v in removed.items() if v))

    from kaigyou_etl.scores import compute_mesh_scores, refresh_stats
    print("\nスコア基準を再計算しています（数分かかります）...")
    with connect() as conn:
        refresh_stats(conn, prefecture_code=prefecture, progress=print)
    # Every configured profile, not just the active one: the UI offers all of
    # them in a dropdown, and a profile with no scores renders an empty ranking
    # and a note telling the reader to run a command. The trade-area sweep is
    # shared between them, so the extra profiles cost little.
    names = list(cfg.scoring_config().get("profiles") or {})
    print(f"メッシュスコアを再計算しています（プロファイル {len(names)} 件）...")
    with connect() as conn:
        summary = compute_mesh_scores(conn, profiles=names,
                                      prefecture_code=prefecture,
                                      progress=print)
    print(json.dumps(summary, ensure_ascii=False, default=_json_default))

    print()
    return cmd_status(argparse.Namespace(json=False))


def cmd_generate_sample(_args: argparse.Namespace) -> int:
    from kaigyou_etl.acquisition import AcquisitionLog
    from kaigyou_etl.sample.generate import generate

    with connect() as conn, connect(autocommit=True) as audit_conn:
        counts = generate(conn, AcquisitionLog(audit_conn))
    print("生成した合成データ（開発用・実データではありません）:")
    for table, n in counts.items():
        print(f"  {table:20s} {n}")
    print("\n注意: これはサンプルデータです。API と UI では常にサンプル表示になります。")
    return EXIT_OK


def cmd_drop_sample(args: argparse.Namespace) -> int:
    from kaigyou_etl.sample.generate import drop

    with connect() as conn:
        removed = drop(conn, only=args.source or None)
    print("削除:", ", ".join(f"{k}={v}" for k, v in removed.items()) or "(なし)")
    return EXIT_OK


def cmd_refresh_stats(args: argparse.Namespace) -> int:
    from kaigyou_etl.scores import refresh_stats

    with connect() as conn:
        summary = refresh_stats(conn, prefecture_code=args.prefecture, progress=print)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return EXIT_OK


def cmd_compute_scores(args: argparse.Namespace) -> int:
    from kaigyou_etl.scores import compute_mesh_scores

    names = (list(cfg.scoring_config().get("profiles") or {})
             if getattr(args, "all_profiles", False) else None)
    with connect() as conn:
        summary = compute_mesh_scores(conn, profile=args.profile, profiles=names,
                                      prefecture_code=args.prefecture,
                                      progress=print)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return EXIT_OK



def cmd_drop_prefecture(args: argparse.Namespace) -> int:
    """Remove everything loaded under one prefecture code.

    For undoing a mislabelled load. The rows are real published figures under
    the wrong label, which is worse than missing data: they read as that
    prefecture's numbers. Nothing here reconstructs anything -- it deletes, and
    the correct file is then loaded again.
    """
    code = args.prefecture
    tables = ("population_mesh", "mesh_business", "municipalities")
    with connect() as conn:
        counts = {}
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f"SELECT count(*) AS n FROM {table} WHERE prefecture_code = %s",
                            (code,))
                counts[table] = cur.fetchone()["n"]
            cur.execute("""
                SELECT count(*) AS n FROM mesh_scores ms
                JOIN population_mesh pm ON pm.id = ms.mesh_id
                WHERE pm.prefecture_code = %s
            """, (code,))
            counts["mesh_scores"] = cur.fetchone()["n"]

        if not any(counts.values()):
            print(f"都道府県コード {code} のデータはありません。")
            return EXIT_OK

        print(f"都道府県コード {code} を削除します:")
        for table, n in counts.items():
            print(f"  {table:20s} {n:,} 件")
        if args.dry_run:
            print("--dry-run のため、削除しません。")
            return EXIT_OK
        if not args.yes:
            print("削除するには --yes を付けてください。")
            return EXIT_PARTIAL

        with conn.cursor() as cur:
            # Scores first: they reference the meshes.
            cur.execute("""
                DELETE FROM mesh_scores ms USING population_mesh pm
                WHERE pm.id = ms.mesh_id AND pm.prefecture_code = %s
            """, (code,))
            for table in tables:
                cur.execute(f"DELETE FROM {table} WHERE prefecture_code = %s", (code,))
            cur.execute("DELETE FROM metric_distributions WHERE scope LIKE %s",
                        (f"%pref{code}",))
        conn.commit()
    print("削除しました。正しいファイルを load-local で取り込み直してください。")
    return EXIT_OK


def cmd_doctor(_args: argparse.Namespace) -> int:
    from kaigyou_etl.doctor import render, run

    report = run()
    for line in render(report):
        print(line)
    return EXIT_ERROR if report.failed else EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    with connect() as conn:
        status = data_status(conn)

    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2, default=_json_default))
        return EXIT_OK if status["official_sources_loaded"] else EXIT_PARTIAL

    print("データ取得状況")
    print("=" * 78)
    for src in status["sources"]:
        mark = {"loaded": "OK    ", "failed": "FAILED", "never_attempted": "未取得",
                "empty": "空    ", "incomplete": "途中  "}.get(src["state"], src["state"])
        kind = "SAMPLE" if src["dataset_kind"] == "sample" else "official"
        print(f"[{mark}] {src['source_id']:26s} {kind:8s} rows={src['row_total']:>7}")
        print(f"         {src['name']}")
        if src["reason"]:
            print(f"         理由: {src['reason']}")
        if src.get("configured_url"):
            print(f"         URL : {src['configured_url']}")
    print("=" * 78)
    print(f"公的データを取得できた情報源: {status['official_sources_loaded']}"
          f" / {status['official_sources_configured']}")
    if status["contains_sample_data"]:
        print("⚠ サンプル（合成）データが投入されています。実データではありません。")
    for conflict in status.get("mixed_datasets") or []:
        print(f"⛔ {conflict['message']}")
    return EXIT_OK if status["official_sources_loaded"] else EXIT_PARTIAL


# ----------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kaigyou-etl", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="apply SQL migrations")
    p.add_argument("--force", action="store_true", help="re-run every migration")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("list", help="list configured sources")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("run", help="run one source end to end")
    p.add_argument("source")
    p.add_argument("--input", help="use a locally downloaded file instead of fetching")
    p.add_argument("--offline", action="store_true", help="never touch the network")
    p.add_argument("--prefecture", default=None,
                   help="load only this prefecture code (e.g. 13); default loads all")
    p.add_argument("--baseline",
                   help="prior-period file, for sources that derive a change rate "
                        "from two rounds (e.g. the 2015 census against 2020)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("run-all", help="run every configured source")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_run_all)

    p = sub.add_parser(
        "load-local",
        help="load every dataset found in a folder, then rebuild the scores")
    p.add_argument("directory", help="folder holding the downloaded files")
    p.add_argument("--dry-run", action="store_true",
                   help="only report which file was matched to which source")
    p.add_argument("--partial", action="store_true",
                   help="proceed even when some datasets are missing")
    p.add_argument("--prefecture", default=None,
                   help="都道府県コード。省略時はファイル名から判定します"
                        "（tblT001141H22.txt なら 22）")
    p.set_defaults(func=cmd_load_local)

    p = sub.add_parser("generate-sample", help="generate synthetic development data")
    p.set_defaults(func=cmd_generate_sample)

    p = sub.add_parser("drop-sample", help="delete synthetic data")
    p.add_argument("source", nargs="*",
                   help="sample source ids to drop; omit to drop all")
    p.set_defaults(func=cmd_drop_sample)

    p = sub.add_parser("refresh-stats", help="recompute score normalisation statistics")
    p.add_argument("--prefecture", default="13")
    p.set_defaults(func=cmd_refresh_stats)

    p = sub.add_parser("compute-scores", help="score every mesh (ranking + heat map)")
    p.add_argument("--profile", default=None)
    p.add_argument("--all-profiles", action="store_true",
                   help="設定済みのプロファイルすべてを計算する（商圏集計は共有されるため追加分は軽い）")
    p.add_argument("--prefecture", default="13")
    p.set_defaults(func=cmd_compute_scores)

    p = sub.add_parser(
        "drop-prefecture",
        help="delete everything loaded under one prefecture code（取り込み間違いの取り消し）")
    p.add_argument("--prefecture", required=True)
    p.add_argument("--dry-run", action="store_true", help="件数だけ表示する")
    p.add_argument("--yes", action="store_true", help="実際に削除する")
    p.set_defaults(func=cmd_drop_prefecture)

    p = sub.add_parser(
        "doctor", help="check config, database, migrations and loaded data")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="report what was and was not obtained")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
