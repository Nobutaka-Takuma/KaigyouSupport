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
        prefecture_filter=args.prefecture,
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

    if args.dry_run:
        print("--dry-run のため、ここで終了します。")
        return EXIT_OK

    plan = [
        ("mhlw_dental_clinics", found.clinics, None),
        ("estat_population_mesh", found.mesh_current, found.mesh_baseline),
        ("mlit_stations", found.stations, None),
        ("mlit_municipalities", found.municipalities, None),
    ]
    failures = 0
    for source_id, input_path, baseline in plan:
        if input_path is None:
            continue
        result = run_source(source_id, input_path=input_path, baseline_path=baseline)
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
        refresh_stats(conn, prefecture_code=args.prefecture)
    print("メッシュスコアを再計算しています...")
    with connect() as conn:
        summary = compute_mesh_scores(conn, prefecture_code=args.prefecture)
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
        summary = refresh_stats(conn, prefecture_code=args.prefecture)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return EXIT_OK


def cmd_compute_scores(args: argparse.Namespace) -> int:
    from kaigyou_etl.scores import compute_mesh_scores

    with connect() as conn:
        summary = compute_mesh_scores(conn, profile=args.profile,
                                      prefecture_code=args.prefecture)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return EXIT_OK


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
    p.add_argument("--prefecture", default="13")
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
    p.add_argument("--prefecture", default="13")
    p.set_defaults(func=cmd_compute_scores)

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
