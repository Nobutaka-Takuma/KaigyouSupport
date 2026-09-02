/**
 * 1 件のレポートを、画面で読むためのページ。
 *
 * これまでマイレポートには「Markdownをダウンロード」しかありませんでした。
 * **提出する文書を確かめるのに、毎回ファイルを落として別のアプリで開く
 * 必要がありました。** 地図の画面にはレポートが出ますが、そこに出るのは
 * いま分析している 1 件だけで、過去のものは開けません。
 */
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { AnalysisReportView } from "../components/AnalysisReport";
import { api, ApiError } from "../lib/api";
import type { AnalysisReport } from "../lib/types";

export function ReportPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    setReport(null);
    setError(null);
    api.analysis
      .report(jobId)
      .then((res) => !cancelled && setReport(res))
      .catch((err) => {
        if (cancelled) return;
        // **まだ無いのか、見せられないのかを分けます。** どちらも「開けない」
        // ですが、待てばよいのか、そうでないのかが違います。
        setError(
          err instanceof ApiError && err.status === 404
            ? "このレポートはまだありません。分析が終わると読めるようになります。"
            : err instanceof Error ? err.message : String(err),
        );
      });
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  return (
    <section className="page">
      <div className="page__head">
        <h2>レポート</h2>
        <Link className="analysis__ghost" to="/reports">マイレポートに戻る</Link>
      </div>
      {error && <p className="reportpage__error">{error}</p>}
      {!report && !error && <p className="muted">読み込み中…</p>}
      {report && jobId && <AnalysisReportView report={report} jobId={jobId} />}
    </section>
  );
}
