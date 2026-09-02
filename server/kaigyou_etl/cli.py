"""``kaigyou-etl`` -- the command line for everything that touches data.

    kaigyou-etl migrate
    kaigyou-etl list
    kaigyou-etl run <source> [--input FILE] [--offline]
    kaigyou-etl run-all [--offline]
    kaigyou-etl load-local <dir> [--dry-run]
    kaigyou-etl generate-sample / drop-sample
    kaigyou-etl refresh-stats
    kaigyou-etl compute-scores [--profile NAME]
    kaigyou-etl build-topology
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

from kaigyou_core.analysis import DEFAULT_CATEGORY

EXIT_OK = 0
EXIT_PARTIAL = 2
EXIT_ERROR = 1


def _json_default(obj: Any) -> str:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


# --------------------------------------------------------------------- commands
def cmd_migrate(args: argparse.Namespace) -> int:
    """マイグレーションを当てて、**手元に何があるか**まで出す。

    「applied: 026」とだけ出ていたので、027 以降が当たっていないのか、
    そもそもファイルが無いのかが読めませんでした。実際には手元のチェック
    アウトが古く、027 以降が**存在しなかった**ケースです。当てた数だけを
    出すと、そこが見えません。
    """
    from kaigyou_etl.migrate import migrate, migrations_dir

    files = sorted(migrations_dir().glob("*.sql"))
    with connect() as conn:
        applied = migrate(conn, force=args.force)
    if applied:
        print("applied:", ", ".join(applied))
    else:
        print("schema already up to date")

    newest = files[-1].name if files else "（なし）"
    print(f"  {migrations_dir()} に {len(files)} 件（最新 {newest}）")
    if files and applied and applied[-1] != newest:
        print("  注意: 最新のファイルが当たっていません。")
    return EXIT_OK


def cmd_list(_args: argparse.Namespace) -> int:
    sources = (cfg.sources_config().get("sources") or {})
    for source_id, spec in sources.items():
        print(f"{source_id:26s} {spec.get('adapter','?'):20s} "
              f"{spec.get('dataset_kind','official'):9s} {spec.get('publisher','')}")
    return EXIT_OK


def _spec_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """この 1 回だけ設定を差し替えるもの。

    設定ファイルを書き換えて戻し忘れると、次の取り込みが黙って別の設定で
    走ります。1 回きりの指定は 1 回きりで終わるほうが安全です。
    """
    raw = getattr(args, "bbox", None)
    if not raw:
        return {}
    parts = [p.strip() for p in str(raw).replace(" ", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise SystemExit(
            "error: --bbox は 4 つの数値です: "
            "--bbox 138.94,35.50,139.92,35.90"
            "（min_lng,min_lat,max_lng,max_lat）")
    return {"bbox": [float(v) for v in parts]}


def cmd_run(args: argparse.Namespace) -> int:
    from kaigyou_etl.pipeline import run_source

    result = run_source(
        args.source,
        input_path=Path(args.input) if args.input else None,
        offline=args.offline,
        spec_overrides=_spec_overrides(args),
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
                 found.municipalities, found.land_prices, found.future_population):
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
        ("mhlw_dental_specialties", found.specialties, None),
        ("estat_population_mesh", found.mesh_current, found.mesh_baseline),
        ("estat_business_mesh", found.mesh_business, None),
        ("osm_walk_network", found.walk_network, None),
        ("mlit_stations", found.stations, None),
        ("mlit_municipalities", found.municipalities, None),
        ("mlit_land_prices", found.land_prices, None),
        ("mlit_future_population", found.future_population, None),
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
    # **取り込まれている業態すべてを採点します。** 既定の業態だけを回すと、
    # 医科のファイルを入れたのにランキングが空、という状態になります。しかも
    # load-local は成功と表示するので、気づけるのは画面を見たときです。
    # 歯科しか入っていない環境では、今までどおり 1 業態です。
    categories = _loaded_categories(prefecture)
    summary: dict[str, Any] = {}
    for category in categories:
        # プロファイルも業態ごとです（config/<業態>/scoring.yaml）。
        names = list(cfg.scoring_config(category).get("profiles") or {})
        label = "" if category == DEFAULT_CATEGORY else f"（{category}）"
        print(f"\nスコア基準を再計算しています{label}（数分かかります）...")
        with connect() as conn:
            refresh_stats(conn, prefecture_code=prefecture,
                          facility_category=category, progress=print)
        # Every configured profile, not just the active one: the UI offers all of
        # them in a dropdown, and a profile with no scores renders an empty ranking
        # and a note telling the reader to run a command. The trade-area sweep is
        # shared between them, so the extra profiles cost little.
        print(f"メッシュスコアを再計算しています{label}"
              f"（プロファイル {len(names)} 件）...")
        with connect() as conn:
            summary[category] = compute_mesh_scores(
                conn, profiles=names, prefecture_code=prefecture,
                facility_category=category, progress=print)
    print(json.dumps(summary if len(summary) > 1 else
                     summary.get(DEFAULT_CATEGORY, summary),
                     ensure_ascii=False, default=_json_default))

    print()
    return cmd_status(argparse.Namespace(json=False))


def cmd_new_analysis(args: argparse.Namespace) -> int:
    """分析ジョブを1件作る。

    API にも同じ入口（POST /api/analysis）がありますが、こちらは HTTP
    クライアントが要りません。PowerShell の `curl` は Invoke-WebRequest の
    別名で -X を受け付けないなど、環境ごとの作法の違いが最初の障害になります。
    このプロジェクトの操作は全部 `python -m kaigyou_etl` で済むので、
    ジョブ作成も同じ入口に置きます。
    """
    from kaigyou_api.deps import DISCLAIMER
    from kaigyou_core.analysis import (
        default_prefecture,
        prefecture_at,
        resolve_mesh_size,
    )
    from kaigyou_core.dataset import build_dataset
    from kaigyou_core.scoring import ScoringModel
    from kaigyou_intel import jobs
    from kaigyou_intel.projection import base_data_hash, to_jsonable

    category = _category(args)
    scoring = cfg.scoring_config(category)
    profile = args.profile or scoring.get("active_profile")
    model = ScoringModel(scoring, profile)

    with connect() as conn:
        prefecture_code = default_prefecture(conn, prefecture_at(conn, args.lat, args.lng))
        mesh_size_m = resolve_mesh_size(conn, None, prefecture_code)
        print(f"基礎データを作成しています（{prefecture_code} / メッシュ {mesh_size_m}m）...")
        dataset = to_jsonable(build_dataset(
            conn, args.lat, args.lng, args.radius,
            prefecture_code=prefecture_code, mesh_size_m=mesh_size_m,
            profile=profile, category=category,
            max_clinics=int((cfg.analysis_config().get("limits") or {})
                            .get("max_clinics_in_projection", 20)),
            disclaimer=DISCLAIMER))

        job_id = jobs.create_job(
            conn, lat=args.lat, lng=args.lng, radius_m=args.radius,
            dataset=dataset, base_hash=base_data_hash(dataset),
            business_type=category, location_name=args.name, profile=profile)

    population = ((dataset.get("demand") or {}).get("residents") or {}) \
        .get("by_radius", {}).get(str(args.radius), {}).get("population")
    clinics = ((dataset.get("competition") or {}).get("clinics_in_radius") or {}).get("count")
    print(f"ジョブを作成しました: {job_id}")
    print(f"  地点        {args.lat}, {args.lng} / 半径 {args.radius}m"
          + (f" / {args.name}" if args.name else ""))
    print(f"  プロファイル {profile}")
    if category != DEFAULT_CATEGORY:
        print(f"  業態        {category}")
    if population is not None:
        print(f"  商圏人口    {population:,} 人 / 歯科医院 {clinics} 件")
    print()
    print("次のどちらかを実行してください:")
    print("  python -m kaigyou_etl analyze --dry-run step1.txt   # 送信内容の確認（課金なし）")
    print("  python -m kaigyou_etl analyze --once                # STEP1 を実行（要 APIキー）")
    return EXIT_OK


def _step_cost(step: dict) -> float | None:
    """単価表は kaigyou_intel.pricing に 1 つだけ置いています。

    CLI と API で別々に持つと、端末と画面で違う金額が出ます。
    """
    from kaigyou_intel.pricing import step_cost

    return step_cost(step)


def _step_seconds(step: dict) -> float | None:
    """その段に何秒かかったか。

    表示するのは、**どこに時間が溶けているかが分からないと縮めようがない**
    からです。実測でレポート1本32分かかったとき、内訳は記録されていたのに
    どこにも出ていませんでした。
    """
    start, end = step.get("started_at"), step.get("completed_at")
    if not start or not end:
        return None
    return (end - start).total_seconds()


def _format_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}秒"
    return f"{int(seconds // 60)}分{seconds % 60:.0f}秒"


def _print_timing_summary(steps: list[dict]) -> None:
    """通しの所要時間と、その内訳。

    段ごとの合計ではなく、最初の開始から最後の完了までを「所要」とします。
    ホスティングされた worker では段の間に cron の待ちが入るので、
    「実行 4分」と「所要 12分」が両方本当のことがあります。**どちらが
    効いているかで、打つ手がまったく違います。**
    """
    spans = [(s["started_at"], s["completed_at"]) for s in steps
             if s.get("started_at") and s.get("completed_at")]
    if not spans:
        print("  まだ完了した段がありません。")
        return
    wall = (max(e for _s, e in spans) - min(s for s, _e in spans)).total_seconds()
    worked = sum((e - s).total_seconds() for s, e in spans)
    waiting = max(0.0, wall - worked)
    print(f"  所要 {_format_seconds(wall)}"
          f"（うち実行 {_format_seconds(worked)}、待ち {_format_seconds(waiting)}）")
    if waiting > worked:
        print("  ※ 待ちのほうが長い。段の間で cron を待っています"
              "（worker.reserve_seconds を下げるか、手元の --once で回す）")


def _print_step_cost_line(step: dict) -> None:
    """使ったトークンと概算。

    キャッシュに入ったぶんは input_tokens から抜けます。そこを足さずに出すと
    「入力 2 tok」になり、数え損ねたように見えます（実測、STEP3）。費用の
    計算は合っていたので、表示だけの問題でした。
    """
    read = step.get("cache_read_tokens") or 0
    written = step.get("cache_write_tokens") or 0
    counted = (step.get("input_tokens") or 0) + read + written
    detail = []
    if written:
        detail.append(f"キャッシュ書 {written:,}")
    if read:
        detail.append(f"キャッシュ読 {read:,}")
    price = _step_cost(step)
    seconds = _step_seconds(step)
    print(f"    入力 {counted:,} tok"
          + (f"（{' / '.join(detail)}）" if detail else "")
          + f" / 出力 {step.get('output_tokens') or 0:,} tok"
          + (f"  ≒ ${price:.3f}" if price else "")
          + (f" / {_format_seconds(seconds)}" if seconds is not None else ""))


def _print_step_output(number: int, output: dict) -> None:
    """ステップの出力を畳んで見せる。未実装のステップはまだ JSON のまま。"""
    if number == 2:
        _print_step2(output)
        return
    if number == 3:
        _print_step3(output)
        return
    if number == 4:
        _print_step4(output)
        return
    if number != 1:
        import json as _j
        print("    " + _j.dumps(output, ensure_ascii=False)[:600])
        return

    facts = output.get("facts") or []
    patterns = output.get("patterns") or []
    print(f"\n    FACT（{len(facts)}件）")
    for fact in facts:
        place = ""
        if fact.get("position_label"):
            place = f"  {fact['position_label']}（{fact.get('benchmark_type', '')}）"
        print(f"      {fact.get('id')}  {fact.get('statement')}")
        print(f"            [{fact.get('measure_key')}]{place}")

    print(f"\n    PATTERN（{len(patterns)}件）")
    for pattern in patterns:
        print(f"      {pattern.get('id')} [{pattern.get('importance')}] "
              f"{pattern.get('title')}")
        print(f"            根拠: {' + '.join(pattern.get('evidence') or [])}")
        if pattern.get("evidence_summary"):
            print(f"            要約: {pattern['evidence_summary']}")
        for question in pattern.get("research_questions") or []:
            print(f"            調査: {question}")

    if output.get("not_determinable"):
        print("\n    基礎データからは判断できなかったこと")
        for item in output["not_determinable"]:
            print(f"      - {item}")


#: 仮説の判定。否定された仮説も残すのが要件 §11 の要点なので、
#: 「見つからなかった」を目立たせます。
_STATUS_LABEL = {
    "SUPPORTED": "裏付けあり",
    "PARTIALLY_SUPPORTED": "部分的",
    "UNSUPPORTED": "裏付けなし",
}


def _print_step2(output: dict) -> None:
    facts = output.get("external_facts") or []
    hypotheses = output.get("hypotheses") or []

    print(f"\n    EXTERNAL FACT（{len(facts)}件）")
    for fact in facts:
        print(f"      {fact.get('id')} [{fact.get('pattern_id')}] {fact.get('statement')}")
        print(f"            出典: {fact.get('source_title')}")
        print(f"                  {fact.get('source_url')}"
              f"  confidence={fact.get('confidence')}")

    print(f"\n    HYPOTHESIS（{len(hypotheses)}件）")
    for item in hypotheses:
        status = _STATUS_LABEL.get(item.get("status", ""), item.get("status"))
        print(f"      {item.get('id')} [{item.get('pattern_id')}] {status}"
              f"  confidence={item.get('confidence')}")
        print(f"            {item.get('statement')}")
        print(f"            根拠: {' + '.join(item.get('evidence') or [])}")
        if item.get("reasoning"):
            print(f"            判定: {item['reasoning']}")

    if output.get("unanswered"):
        print("\n    調べたが確認できなかったこと")
        for entry in output["unanswered"]:
            print(f"      - {entry}")


def _print_step3(output: dict) -> None:
    mechanisms = output.get("demand_mechanisms") or []
    segments = output.get("patient_segments") or []

    print(f"\n    需要形成メカニズム（{len(mechanisms)}件）")
    for item in mechanisms:
        print(f"      {item.get('id')} {item.get('title')}"
              f"  confidence={item.get('confidence')}")
        for i, step in enumerate(item.get("chain") or []):
            print(f"            {'└→' if i else '  '} {step}")
        print(f"            根拠: {' + '.join(item.get('evidence') or [])}")

    print(f"\n    患者セグメント（{len(segments)}件）")
    for item in segments:
        print(f"      {item.get('id')} [{item.get('importance')}] {item.get('name')}"
              f"  confidence={item.get('confidence')}")
        print(f"            筋道: {item.get('mechanism_id')}"
              f"  根拠: {' + '.join(item.get('evidence') or [])}")
        if item.get("note"):
            print(f"            補足: {item['note']}")

    if output.get("insights"):
        print("\n    横断して見えること")
        for item in output["insights"]:
            print(f"      {item.get('id')} {item.get('statement')}")
            print(f"            根拠: {' + '.join(item.get('evidence') or [])}")

    if output.get("not_supported"):
        # 要件 §13。「書かれていない層」は「検討して該当なし」ではありません。
        print("\n    根拠が無いため出さなかった患者層")
        for entry in output["not_supported"]:
            print(f"      - {entry}")

    _print_decision(output)


_DECISION_FIELDS = (("主要患者", "primary_patients"),
                    ("主要に置かない層", "secondary_patients"),
                    ("競争しない領域", "avoid_competing_on"),
                    ("患者獲得エリア", "acquisition_area"),
                    ("来院理由", "reason_to_visit"),
                    ("医院モデル", "clinic_model"))


def _print_decision(output: dict) -> None:
    """要件 §17 の答え。STEP3 の出力に入っています。"""
    decision = output.get("decision") or {}
    if not decision:
        return
    print(f"\n    開業方針  confidence={decision.get('confidence')}")
    for label, key in _DECISION_FIELDS:
        item = decision.get(key) or {}
        print(f"      {label}: {item.get('statement')}")
        print(f"            根拠: {' + '.join(item.get('evidence') or [])}")

    for label, key in (("開業上のメリット", "advantages"), ("リスク", "risks")):
        entries = decision.get(key) or []
        print(f"\n    {label}（{len(entries)}件）")
        for item in entries:
            print(f"      - {item.get('statement')}")
            print(f"            根拠: {' + '.join(item.get('evidence') or [])}")

    if output.get("actions"):
        print("\n    次に取るべき行動")
        for action in output["actions"]:
            print(f"      - {action.get('statement')}")


def _print_step4(output: dict) -> None:
    """顧客に渡す文書。端末では骨格だけ見せて、全文は --report に譲ります。"""
    verdict = output.get("verdict") or {}
    print(f"\n    {output.get('title')}")
    print(f"\n    評価: {verdict.get('label')}")
    for line in str(verdict.get("statement", "")).splitlines():
        print(f"      {line}")
    if verdict.get("counterpoint"):
        print(f"      外れるとしたら: {verdict['counterpoint']}")

    print("\n    章立て")
    for section in output.get("sections") or []:
        print(f"      - {section.get('heading')}"
              + (f"  … {section['takeaway']}" if section.get("takeaway") else ""))

    support = output.get("support_needed") or []
    print(f"\n    開業に必要なこと（{len(support)}件）")
    for item in support:
        print(f"      [{item.get('category')}] {item.get('item')}")

    print("\n    全文: python -m kaigyou_etl analyze --report")


def cmd_analyze(args: argparse.Namespace) -> int:
    """商圏インテリジェンスの worker。

    Vercel の関数には実行時間の上限があり、Web検索を伴う 4 ステップは
    そこに収まりません（要件 §31）。API は Job を作るだけで、実行はここです。
    """
    import json as _json

    from kaigyou_intel import client as llm
    from kaigyou_intel import jobs as _jobs
    from kaigyou_intel.steps import step1_features
    from kaigyou_intel.worker import serve

    if args.list:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, location_name, latitude, longitude, radius_m, profile,
                       status, current_step, created_at
                FROM analysis_jobs ORDER BY created_at DESC LIMIT 20
            """)
            rows = cur.fetchall()
        if not rows:
            print("ジョブはありません。")
            return EXIT_OK
        print(f"{'作成日時':16s} {'状態':10s} {'地点':14s} ジョブID")
        for i, r in enumerate(rows):
            when = r["created_at"].strftime("%m-%d %H:%M")
            name = (r["location_name"] or f"{r['latitude']:.4f},{r['longitude']:.4f}")[:14]
            mark = " ←次に実行" if r["status"] == "queued" and r is rows[-1] else ""
            print(f"{when:16s} {r['status']:10s} {name:14s} {r['id']}{mark}")
        print("\nworker は古い順に処理します。不要なものは --cancel <id> で取り下げてください。")
        return EXIT_OK

    if args.timing is not None:
        # 段ごとに何秒かかったか**だけ**。--show は各段の出力も全部出すので、
        # 「どこに時間が溶けているか」を見るには長すぎます。縮めようがない
        # ものは縮められないので、まずここを見ます。
        with connect() as conn:
            with conn.cursor() as cur:
                if args.timing:
                    cur.execute("SELECT id, location_name FROM analysis_jobs "
                                "WHERE id = %s", (args.timing,))
                else:
                    cur.execute("SELECT id, location_name FROM analysis_jobs "
                                "ORDER BY created_at DESC LIMIT 1")
                row = cur.fetchone()
            if row is None:
                print("ジョブが見つかりません。")
                return EXIT_OK
            job_id = str(row["id"])
            steps = _jobs.get_steps(conn, job_id)

        print(f"ジョブ {job_id}（{row['location_name'] or job_id[:8]}）")
        _print_timing_summary(steps)
        for step in steps:
            seconds = _step_seconds(step)
            head = f"  STEP{step['step_number']} {step['step_name']}"
            if seconds is None:
                print(f"{head}  — {step['status']}")
                continue
            price = _step_cost(step)
            read = step.get("cache_read_tokens") or 0
            written = step.get("cache_write_tokens") or 0
            counted = (step.get("input_tokens") or 0) + read + written
            print(f"{head}  {_format_seconds(seconds)}"
                  f"  / 入力 {counted:,} tok"
                  f" / 出力 {step.get('output_tokens') or 0:,} tok"
                  + (f" / 検索 {step['web_searches']}回"
                     if step.get("web_searches") else "")
                  + (f"  ≒ ${price:.3f}" if price else ""))
            if read or written:
                # 区切りが可変ブロックに当たっていると、書き込みだけが毎回
                # 増えて読み出しが 0 のままになります。そこが見えるように。
                print(f"        キャッシュ 読 {read:,} / 書 {written:,}")
            if step.get("attempts") and step["attempts"] > 1:
                print(f"        試行 {step['attempts']} 回"
                      "（やり直したぶんの時間は上に含まれません）")
        return EXIT_OK

    if args.show is not None:
        # STEP の出力を人が読める形で出す。プロンプトを直すかどうかの判断は
        # ここを読んでするので、JSON をそのまま出すより畳んで見せます。
        with connect() as conn:
            with conn.cursor() as cur:
                if args.show:
                    cur.execute("SELECT id, location_name FROM analysis_jobs "
                                "WHERE id = %s", (args.show,))
                else:
                    cur.execute("SELECT id, location_name FROM analysis_jobs "
                                "ORDER BY created_at DESC LIMIT 1")
                row = cur.fetchone()
            if row is None:
                print("ジョブが見つかりません。")
                return EXIT_OK
            job_id = str(row["id"])
            steps = _jobs.get_steps(conn, job_id)

        name = row["location_name"] or job_id[:8]
        print(f"ジョブ {job_id}（{name}）")
        _print_timing_summary(steps)
        for step in steps:
            if step["status"] != "completed":
                print(f"\n  STEP{step['step_number']} {step['step_name']}: "
                      f"{step['status']}"
                      + (f" — {step['error_message'][:120]}"
                         if step["error_message"] else ""))
                continue
            print(f"\n  STEP{step['step_number']} {step['step_name']}  "
                  f"{step['model']} / {step['prompt_version']}")
            _print_step_cost_line(step)
            _print_step_output(step["step_number"], step["output_json"] or {})
        return EXIT_OK

    if getattr(args, "questions", False):
        return _print_question_quality()

    if getattr(args, "export", None) is not None:
        from kaigyou_intel import report as _report

        with connect() as conn:
            with conn.cursor() as cur:
                if args.export:
                    cur.execute("SELECT id FROM analysis_jobs WHERE id = %s",
                                (args.export,))
                else:
                    cur.execute("SELECT id FROM analysis_jobs "
                                "ORDER BY created_at DESC LIMIT 1")
                row = cur.fetchone()
            if row is None:
                print("ジョブが見つかりません。")
                return EXIT_OK
            written = _report.write_all_step_files(conn, str(row["id"]),
                                                   args.out or None)
        if not written:
            print("書き出せる段がありません（完了した段がまだ無いか、"
                  "書き込めない場所です）。")
            return EXIT_OK
        for path in written:
            print(f"  {path}")
        return EXIT_OK

    if args.report is not None:
        from kaigyou_intel import report as _report

        with connect() as conn:
            with conn.cursor() as cur:
                if args.report:
                    cur.execute("SELECT id FROM analysis_jobs WHERE id = %s",
                                (args.report,))
                else:
                    cur.execute("SELECT id FROM analysis_jobs "
                                "ORDER BY created_at DESC LIMIT 1")
                row = cur.fetchone()
            if row is None:
                print("ジョブが見つかりません。")
                return EXIT_OK
            markdown = _report.markdown_for(conn, str(row["id"]))
        if markdown is None:
            print("レポートはまだありません。STEP4 まで完了させてください。")
            return EXIT_OK
        if args.out:
            Path(args.out).write_text(markdown, encoding="utf-8")
            print(f"書き出しました: {args.out}")
        else:
            print(markdown)
        return EXIT_OK

    if args.cancel:
        with connect() as conn, conn.cursor() as cur:
            if args.cancel == "all":
                cur.execute("UPDATE analysis_jobs SET status = 'cancelled' "
                            "WHERE status IN ('queued','failed') RETURNING id")
            else:
                cur.execute("UPDATE analysis_jobs SET status = 'cancelled' "
                            "WHERE id = %s RETURNING id", (args.cancel,))
            cancelled = cur.fetchall()
            conn.commit()
        print(f"{len(cancelled)} 件を取り下げました。")
        return EXIT_OK

    if args.dry_run:
        # 課金する前に、何が送られるかを見るための道具。API は叩きません。
        with connect() as conn:
            with conn.cursor() as cur:
                if args.job:
                    cur.execute("SELECT id, location_name FROM analysis_jobs WHERE id = %s",
                                (args.job,))
                    row, waiting = cur.fetchone(), 1
                else:
                    # いちばん新しいもの。`--dry-run` を打つのは、たいてい
                    # ジョブを作った直後に「いま作ったものを見たい」ときです。
                    # worker の順（古い順）に合わせると、前に作って忘れていた
                    # ジョブが表示され、消えていないように見えます。
                    cur.execute(
                        "SELECT id, location_name FROM analysis_jobs "
                        "WHERE status IN ('queued','running','failed') "
                        "ORDER BY created_at DESC LIMIT 1")
                    row = cur.fetchone()
                    cur.execute("SELECT count(*) AS n FROM analysis_jobs "
                                "WHERE status = 'queued'")
                    waiting = cur.fetchone()["n"]
            if row is None:
                print("待っているジョブがありません。")
                print("  python -m kaigyou_etl new-analysis --lat 35.6717 --lng 139.7650")
                return EXIT_OK
            job_id = str(row["id"])
            job = _jobs.get_job(conn, job_id, include_base_data=True)

        payload = step1_features.build_input(job["base_data"])
        settings = llm.step_settings(1)
        system = cfg.prompt_text(settings["prompt"])
        body = _json.dumps(payload, ensure_ascii=False, indent=1)

        if waiting > 1 and not args.job:
            # worker は古い順に処理します。いま見ているものが次に実行される
            # とは限らないので、そこを黙っていると想定と違う結果になります。
            print(f"待っているジョブが {waiting} 件あります。ここに表示するのは"
                  "**いちばん新しいもの**です。")
            print("  worker は古い順に処理します。"
                  "`analyze --list` で一覧、`--cancel all` で不要分を取り下げ。")
        out = Path(args.dry_run)
        out.write_text(f"===== system ({settings['prompt_version']}) =====\n"
                       f"{system}\n\n===== user =====\n{body}\n", encoding="utf-8")
        size = len(system.encode()) + len(body.encode())
        label = f"ジョブ {job_id}" + (f"（{row['location_name']}）"
                                       if row.get("location_name") else "")
        print(f"{label} / STEP1 に送る内容を書き出しました: {out}")
        print(f"  モデル      {settings['model']}（effort={settings['effort']}）")
        print(f"  入力サイズ  {size:,} bytes  ≒ {size // 2:,} トークン前後（日本語の概算）")
        print(f"  Web検索     {'あり' if settings['web_search'] else 'なし'}")
        print("\n中身を確認してから、キーを設定して --once を実行してください。")
        return EXIT_OK

    if not llm.is_configured():
        print("error: ANTHROPIC_API_KEY が設定されていません。", file=sys.stderr)
        print("  $env:ANTHROPIC_API_KEY = 'sk-ant-...'  を設定してください。",
              file=sys.stderr)
        return EXIT_ERROR

    target = args.job
    if args.retry is not None:
        # やり直す前に、対象を確定させます。指定が無ければいちばん新しいもの。
        with connect() as conn:
            if target is None:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM analysis_jobs "
                                "ORDER BY created_at DESC LIMIT 1")
                    row = cur.fetchone()
                if row is None:
                    print("ジョブがありません。")
                    return EXIT_OK
                target = str(row["id"])
            _jobs.reset_step(conn, target, args.retry)
        print(f"ジョブ {target} を STEP{args.retry} からやり直します"
              f"（STEP{args.retry} 以降の出力は消えました）。")

    if args.once or target:
        handled = serve(connect, once=True, job_id=target, progress=print)
        if handled:
            print(f"{handled} 件を処理しました。")
            return EXIT_OK
        # 「0 件」だけだと、待っているジョブが無いのか、あるのに拾えないのかが
        # 分かりません。実測：失敗した Job が残ったまま 0 件が出続けました。
        print("0 件を処理しました。")
        _explain_why_nothing_ran()
        return EXIT_OK

    print("worker を起動しました。Ctrl+C で終了します。")
    try:
        serve(connect, poll_seconds=args.poll, progress=print)
    except KeyboardInterrupt:
        print("\n終了しました。")
    return EXIT_OK


def _explain_why_nothing_ran() -> None:
    """待っているジョブが無い理由を、状態別に言う。

    worker は queued しか拾いません（失敗した Job を自動で拾い直すと、
    壊れたまま何度も課金されるため）。拾われない Job があるなら、
    再開の仕方まで書きます。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT status, count(*) AS n, max(created_at) AS latest,
                   (array_agg(id ORDER BY created_at DESC))[1] AS newest
            FROM analysis_jobs GROUP BY status
        """)
        rows = {r["status"]: r for r in cur.fetchall()}

    if not rows:
        print("  ジョブがありません。")
        print("    python -m kaigyou_etl new-analysis --lat 35.6717 --lng 139.7650")
        return
    if "blocked" in rows:
        # 失敗ではありません。材料は揃っていて、続きを実装していないだけです。
        row = rows["blocked"]
        print(f"  未実装のステップで待機中のジョブが {row['n']} 件あります"
              "（失敗ではありません）。")
        print("    そのステップを実装すると、次の `analyze --once` が自動で再開します。")
        print(f"    途中まで読む: python -m kaigyou_etl analyze --show {row['newest']}")

    stuck = [rows[s] for s in ("failed", "running") if s in rows]
    if not stuck:
        if "blocked" not in rows:
            summary = "、".join(f"{s} {r['n']}件" for s, r in rows.items())
            print(f"  待っているジョブがありません（{summary}）。")
        return
    for row in stuck:
        label = {"failed": "失敗したまま", "running": "実行中のまま"}[row["status"]]
        print(f"  {label}のジョブが {row['n']} 件あります。"
              "worker は queued のものだけを自動で拾います。")
        print(f"    再開:     python -m kaigyou_etl analyze --job {row['newest']}")
        print(f"    やり直し: python -m kaigyou_etl analyze --job {row['newest']} --retry 1")
        print(f"    取り下げ: python -m kaigyou_etl analyze --cancel {row['newest']}")


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
        summary = refresh_stats(conn, prefecture_code=args.prefecture,
                                facility_category=_category(args), progress=print)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return EXIT_OK


def cmd_compute_scores(args: argparse.Namespace) -> int:
    from kaigyou_etl.scores import compute_mesh_scores

    names = (list(cfg.scoring_config(_category(args)).get("profiles") or {})
             if getattr(args, "all_profiles", False) else None)
    with connect() as conn:
        summary = compute_mesh_scores(conn, profile=args.profile, profiles=names,
                                      prefecture_code=args.prefecture,
                                      facility_category=_category(args),
                                      progress=print)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return EXIT_OK



def cmd_build_topology(args: argparse.Namespace) -> int:
    """取り込み済みの道路網からグラフを作り直す。取り込みはやり直さない。

    ``run osm_walk_network`` は取り込みの最後にこれを呼びます。それとは別に
    ここからも呼べるようにしてあるのは、**グラフ作りだけが落ちたときに、
    道路の取り込みからやり直すのが高くつくから**です。

    取り込みは shapefile を 2 回読みます（走行可能な道を数えるときと、行に
    変換するとき）。関東の抽出ファイルでは、この 2 回だけで数十分かかります。
    辺はコミット済みなので、作り直すのはグラフだけで足ります。

    グラフ作りは書き込みが多く（分割後の辺は元の 2〜3 倍になります）、落ちる
    としたらたいていここです。容量や実行時間の上限に当たったあと、条件を
    直してここだけを再実行できます。
    """
    from kaigyou_etl.adapters.osm_walk_network import build_topology

    spec = (cfg.sources_config().get("sources") or {}).get("osm_walk_network") or {}
    tolerance = float(args.tolerance if args.tolerance is not None
                      else (spec.get("topology_tolerance_deg") or 0.00001))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM walk_network")
            edges = cur.fetchone()["n"]
        if not edges:
            print("walk_network が空です。先に `run osm_walk_network` で道路を"
                  "取り込んでください。")
            return EXIT_ERROR
        summary = build_topology(conn, tolerance_deg=tolerance, progress=print)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return EXIT_OK if summary.get("topology") == "built" else EXIT_PARTIAL

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
            # **鍵は pref13 で終わりません。** 形は
            # `mesh:500:r1000:pref13:catdental_clinic:with_clinics` で、
            # 後ろに目盛りの種類と業態が続きます。`%pref13` で終わる条件に
            # していたので、この削除はこれまで 1 件も消していませんでした
            # （残った目盛りが、入れ直した別の県のデータに使われます）。
            cur.execute("DELETE FROM metric_distributions WHERE scope LIKE %s",
                        (f"%:pref{code}:%",))
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
def _loaded_categories(prefecture_code: str) -> list[str]:
    """その県に実際に入っている業態。**無ければ既定を 1 つ返します。**

    施設をまだ取り込んでいない環境で空を返すと、スコアの再計算が丸ごと
    飛ばされて、原因の分からない「ランキングが空」になります。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT facility_category AS c FROM facilities "
            "WHERE prefecture_code = %s ORDER BY 1", (prefecture_code,))
        found = [r["c"] for r in cur.fetchall() if r["c"]]
    return found or [DEFAULT_CATEGORY]


def _add_category(parser: argparse.ArgumentParser) -> None:
    """業態を選ぶ口。**既定は歯科で、省略したときのふるまいは今までどおりです。**

    口が無いと、内科を入れても「歯科として」採点されます。しかも成功と
    表示されるので、気づけるのはレポートの中身を読んだときです。
    """
    parser.add_argument(
        "--category", default=None, metavar="ID",
        help=f"施設の業態（既定 {DEFAULT_CATEGORY}）。"
             "facilities.facility_category と同じ値を指定します")


def _category(args: argparse.Namespace) -> str:
    return getattr(args, "category", None) or DEFAULT_CATEGORY


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
    p.add_argument("--bbox", default=None, metavar="LNG0,LAT0,LNG1,LAT1",
                   help="切り取る範囲をこの 1 回だけ指定する"
                        "（街路ネットワーク用。省略時は取り込む県の"
                        "市区町村境界から作ります）")
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

    p = sub.add_parser("new-analysis", help="商圏分析ジョブを1件作る")
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lng", type=float, required=True)
    p.add_argument("--radius", type=int, default=1000, help="商圏半径（m）")
    p.add_argument("--name", default=None, help="レポートに載せる地点名")
    p.add_argument("--profile", default=None,
                   help="スコアリングプロファイル（省略時は active_profile）")
    _add_category(p)
    p.set_defaults(func=cmd_new_analysis)

    p = sub.add_parser("analyze", help="商圏インテリジェンスの worker を動かす")
    p.add_argument("--once", action="store_true",
                   help="待っているジョブを1件処理して終了する")
    p.add_argument("--poll", type=float, default=5.0,
                   help="ジョブが無いときの待ち時間（秒）")
    p.add_argument("--dry-run", metavar="FILE",
                   help="APIを呼ばずに、STEP1へ送る内容をファイルへ書き出す")
    p.add_argument("--job", metavar="ID", default=None,
                   help="対象のジョブID（省略時はいちばん新しいもの）。"
                        "指定するとそのジョブを状態にかかわらず実行します")
    p.add_argument("--retry", type=int, metavar="STEP", default=None,
                   help="このステップからやり直す（それ以降の出力は消えます）")
    p.add_argument("--report", nargs="?", const="", metavar="ID",
                   help="レポート（Markdown）を表示する（省略時は最新のジョブ）")
    p.add_argument("--out", metavar="FILE", default=None,
                   help="--report の書き出し先")
    p.add_argument("--list", action="store_true", help="ジョブの一覧を表示する")
    p.add_argument("--show", nargs="?", const="", metavar="ID",
                   help="各ステップの出力を読める形で表示（省略時は最新のジョブ）")
    p.add_argument("--timing", nargs="?", const="", metavar="ID",
                   help="段ごとの所要時間・トークン・費用だけを表示"
                        "（省略時は最新のジョブ）")
    p.add_argument("--cancel", metavar="ID", default=None,
                   help="ジョブを取り下げる（all で待機中すべて）")
    p.add_argument("--questions", action="store_true",
                   help="立てた問いが実際に何を動かしたかを、保存済みのジョブから数える")
    p.add_argument("--export", nargs="?", const="", metavar="ID",
                   help="段ごとの入力と出力を JSON ファイルに書き出す"
                        "（省略時は最新のジョブ）")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("generate-sample", help="generate synthetic development data")
    p.set_defaults(func=cmd_generate_sample)

    p = sub.add_parser("drop-sample", help="delete synthetic data")
    p.add_argument("source", nargs="*",
                   help="sample source ids to drop; omit to drop all")
    p.set_defaults(func=cmd_drop_sample)

    p = sub.add_parser("refresh-stats", help="recompute score normalisation statistics")
    p.add_argument("--prefecture", default="13")
    _add_category(p)
    p.set_defaults(func=cmd_refresh_stats)

    p = sub.add_parser(
        "compute-benchmarks",
        help="母集団の分布を事前計算する（地点評価の往復を減らす）")
    p.add_argument("--prefecture", metavar="CODE", default=None,
                   help="対象の都道府県コード（省略時は読み込み済みすべて）")
    p.add_argument("--profile", metavar="NAME", default=None,
                   help="対象のプロファイル（省略時は mesh_scores にあるすべて）")
    p.set_defaults(func=cmd_compute_benchmarks)

    p = sub.add_parser("compute-scores", help="score every mesh (ranking + heat map)")
    p.add_argument("--profile", default=None)
    p.add_argument("--all-profiles", action="store_true",
                   help="設定済みのプロファイルすべてを計算する（商圏集計は共有されるため追加分は軽い）")
    p.add_argument("--prefecture", default="13")
    _add_category(p)
    p.set_defaults(func=cmd_compute_scores)

    p = sub.add_parser(
        "build-topology",
        help="取り込み済みの道路網からグラフを作り直す（徒歩圏の経路探索用）")
    p.add_argument("--tolerance", type=float, default=None,
                   help="端点をつなぐ許容誤差（度）。既定は sources.yaml の値")
    p.set_defaults(func=cmd_build_topology)

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


def _print_question_quality() -> int:
    """立てた問いが、実際に何かを動かしたか（指示書 §8-3）。

    **LLM に採点させません。**「この問いは良い問いですか」と聞けばそれらしい
    点数が返りますが、それは問いを出したのと同じモデルの意見で、外から
    確かめる手立てがありません。ここは**実際に何が起きたか**だけを数えます。

    表を足していないので、いつでも数え直せます。材料は
    ``analysis_steps.output_json`` に既にあります。
    """
    from kaigyou_intel import question_quality as _quality

    with connect() as conn:
        summary = _quality.across_jobs(conn)

    if not summary["jobs"]:
        print("問いを記録したジョブがまだありません。")
        print("  （問いを第一級にする前のジョブは、母数に入れていません。"
              "混ぜると版の違いが「答えが出た割合の低下」に見えます）")
        return EXIT_OK

    total = summary["questions"]
    print(f"問い {total} 件（ジョブ {summary['jobs']} 件）")
    print()
    print(f"  答えが出た              {summary['settled']:4d} 件"
          f"{_pct(summary['settled'], total)}")
    print(f"    うち一次資料が根拠    {summary['primary_evidence']:4d} 件")
    print(f"    うち 2 周目で決着     {summary['needed_a_second_round']:4d} 件")
    print(f"  現地で確かめる項目に    {summary['left_to_the_field']:4d} 件"
          f"{_pct(summary['left_to_the_field'], total)}")
    print("    （検索では決着しない問い。失敗ではありません）")
    print()
    # ここがこの表の要です。答えが出ることと、判断が動くことは違います。
    print(f"  判断が動いた            {summary['moved_a_decision']:4d} 件"
          f"{_pct(summary['moved_a_decision'], total)}")
    print("    答えが出ても動かない問いがあります。「区画整理で計画的に形成")
    print("    された市街地か」は、正しくても診療コンセプトも設備も動きません。")

    if summary["levers"]:
        print()
        print("  動いたもの")
        for lever, count in summary["levers"]:
            print(f"    {lever}　{count} 件")

    # **良い問いの定義そのものを数えます。** 既に持っているデータから
    # 見落とされている前提を発見し、それを外部情報で検証できた問い。
    questioned = summary.get("questioned_an_assumption", 0)
    if questioned:
        print()
        print(f"  前提を疑って生まれた問い    {questioned:4d} 件"
              f"{_pct(questioned, total)}")
        print(f"    うち判断が動いた          "
              f"{summary.get('moved_and_questioned', 0):4d} 件")

    triggers = [t for t in summary.get("by_trigger", []) if t["questions"]]
    if len(triggers) > 1:
        print()
        print("  問いの生まれ方ごと")
        print("    生まれ方                          問い  答えが出た  判断が動いた")
        for row in triggers:
            print(f"    {row['trigger_type']:<28} {row['questions']:4d}"
                  f"  {row['settled']:9d}  {row['moved_a_decision']:11d}")

    versions = summary["by_prompt_version"]
    if len(versions) > 1:
        # 版を跨いで平均しません。直した効果が薄まって見えます。
        print()
        print("  プロンプト版ごと（問いを出す STEP1 の版）")
        print("    版              問い  答えが出た  判断が動いた")
        for row in versions:
            print(f"    {row['prompt_version']:<14} {row['questions']:4d}"
                  f"  {row['settled']:9d}  {row['moved_a_decision']:11d}")

    # 検索に回して繰り返し空振りしている問い。**台帳に足す候補です。**
    candidates = summary.get("dead_end_candidates") or []
    if candidates:
        print()
        print("  検索に回して、繰り返し空振りしている問い")
        print("    → config/dead_ends.yaml に足すと、次からは検索せずに")
        print("      現地確認へ回ります。**足すかどうかは人が決めてください。**")
        print("      機械が勝手に塞ぐと、公表され始めたものに気づけません。")
        for row in candidates[:8]:
            print(f"    {row['times']} 回  {row['examples'][0][:56]}")

    dead = [r for r in summary["rows"]
            if not r["settled"] and not r["left_to_the_field"]]
    if dead:
        # 答えも出ず、現地確認にも回らなかった問い。**いちばん筋の悪い形**
        # です。調べようがなかったのか、そもそも問いとして弱かったのか。
        # 次にプロンプトを直すときに読む一覧です。
        print()
        print(f"  答えも出ず、現地確認にも回らなかった問い {len(dead)} 件")
        for row in dead[:10]:
            print(f"    {row['question_id']}  {row['question'][:60]}")
        if len(dead) > 10:
            print(f"    ほか {len(dead) - 10} 件")
    return EXIT_OK


def _pct(part: int, whole: int) -> str:
    return f"  ({part * 100 // whole}%)" if whole else ""


def cmd_compute_benchmarks(args: argparse.Namespace) -> int:
    """母集団の分布を貯める（`benchmark_distributions`）。

    **compute-scores のあとに実行してください。** この計算は mesh_scores を
    読みます。順番を逆にすると、古いスコアの分布が残ります。
    """
    from kaigyou_etl import benchmarks as bench

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT pm.prefecture_code AS code, ms.profile,
                       ms.radius_m, ms.facility_category
                FROM mesh_scores ms
                JOIN population_mesh pm ON pm.id = ms.mesh_id
                WHERE (%s::text IS NULL OR pm.prefecture_code = %s)
                  AND (%s::text IS NULL OR ms.profile = %s)
                ORDER BY 1, 2, 3
                """,
                (args.prefecture, args.prefecture, args.profile, args.profile))
            combos = [dict(r) for r in cur.fetchall()]
        if not combos:
            print("mesh_scores に該当する行がありません。"
                  "先に `kaigyou-etl compute-scores` を実行してください。")
            return EXIT_OK
        total = 0
        for combo in combos:
            print(f"{combo['code']} / {combo['profile']} / "
                  f"半径{combo['radius_m']}m / {combo['facility_category']}")
            total += bench.compute(
                conn, prefecture_code=combo["code"], profile=combo["profile"],
                radius_m=combo["radius_m"],
                facility_category=combo["facility_category"],
                progress=lambda m: None)
    print(f"\n{total} 行を保存しました。")
    print("  これで、地点をクリックしたときに母集団を測り直しません。")
    return EXIT_OK
