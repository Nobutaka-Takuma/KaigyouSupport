import { useState } from "react";
import type {
  AnalysisReport, AnalysisSource, Evidenced, ReportBlock,
} from "../lib/types";

/**
 * レポートを構造のまま表示する。
 *
 * Markdown を貼るのではなく report_json を描くのは、根拠の id（F001, C002…）を
 * 見える形で残すためです。§25 が求めているのは「INSIGHT から FACT、そして
 * 出典まで辿れること」で、それは本文を流し込んだだけでは満たせません。
 * Markdown は持ち出し用として、そのまま落とせるようにしてあります。
 */

const TAG_LABEL: Record<ReportBlock["tag"], string> = {
  FACT: "事実", BENCHMARK: "比較", PATTERN: "パターン", WHY: "背景",
  INSIGHT: "解釈", IMPLICATION: "示唆", ACTION: "行動",
};

const DECISION_FIELDS: [string, keyof AnalysisReport["report_json"]["decision"]][] = [
  ["主要患者", "primary_patients"],
  ["主要に置かない層", "secondary_patients"],
  ["競争しない領域", "avoid_competing_on"],
  ["患者獲得エリア", "acquisition_area"],
  ["来院理由", "reason_to_visit"],
  ["医院モデル", "clinic_model"],
];

/** 要件 §9 の優先順位。並び順もこの順。 */
const SOURCE_TYPES = [
  ["government", "国・省庁"], ["statistics", "政府統計"], ["prefecture", "都道府県"],
  ["municipality", "市区町村"], ["public_body", "公的機関"], ["transit", "交通事業者"],
  ["academic", "大学・研究機関"], ["company", "企業"], ["news", "ニュース"],
  ["other", "その他"],
] as const;

const SOURCE_LABEL: Record<string, string> =
  Object.fromEntries(SOURCE_TYPES.map(([key, label]) => [key, label]));

/** 上から読んで一次資料に当たれるように並べる。Markdown 側と同じ順。 */
function byPriority(a: AnalysisSource, b: AnalysisSource) {
  const rank = (s: AnalysisSource) => {
    const i = SOURCE_TYPES.findIndex(([key]) => key === (s.source_type ?? "other"));
    return i < 0 ? SOURCE_TYPES.length : i;
  };
  return rank(a) - rank(b) || (a.url ?? "").localeCompare(b.url ?? "");
}

export function AnalysisReportView({ report }: { report: AnalysisReport }) {
  const [open, setOpen] = useState<"decision" | "body" | "sources">("decision");
  const json = report.report_json;
  const cited = report.sources.filter((s) => s.pattern_id).sort(byPriority);

  return (
    <div className="report">
      <div className="report__summary">{json.executive_summary}</div>

      <div className="report__tabs" role="tablist">
        {([["decision", "開業方針"], ["body", "レポート本文"],
           ["sources", `出典 (${cited.length})`]] as const).map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={open === key}
            className={open === key ? "is-active" : ""}
            onClick={() => setOpen(key)}
          >
            {label}
          </button>
        ))}
        {report.report_markdown && (
          <button className="report__download" onClick={() => download(report)}>
            Markdownで保存
          </button>
        )}
      </div>

      {open === "decision" && (
        <div className="report__decision">
          <dl>
            {DECISION_FIELDS.map(([label, key]) => (
              <div key={key}>
                <dt>{label}</dt>
                <dd><Statement item={json.decision[key] as Evidenced} /></dd>
              </div>
            ))}
          </dl>
          <EvidencedList title="開業上のメリット" items={json.decision.advantages} />
          <EvidencedList title="リスク" items={json.decision.risks} />
          <EvidencedList title="次に取るべき行動" items={json.actions} />
          <p className="report__confidence">
            判断の確度: <strong>{json.decision.confidence}</strong>
          </p>
        </div>
      )}

      {open === "body" && (
        <div className="report__body">
          {[...json.sections].sort((a, b) => a.number - b.number).map((section) => (
            <section key={section.number}>
              <h4>{section.number}. {section.title}</h4>
              {section.blocks.map((block, i) => (
                <p key={i} className={`report__block report__block--${block.tag}`}>
                  <span className="report__tag">{TAG_LABEL[block.tag] ?? block.tag}</span>
                  {block.text}
                  <Refs ids={block.evidence} />
                </p>
              ))}
            </section>
          ))}
        </div>
      )}

      {open === "sources" && (
        <div className="report__sources">
          {cited.length === 0 && <p>本文が引用した外部資料はありません。</p>}
          <ul>
            {cited.map((source) => (
              <li key={`${source.pattern_id}-${source.url}`}>
                <span className="report__source-type">
                  {SOURCE_LABEL[source.source_type ?? "other"] ?? "その他"}
                </span>
                <a href={source.url} target="_blank" rel="noreferrer noopener">
                  {shorten(source.title ?? source.url)}
                </a>
              </li>
            ))}
          </ul>
          {report.sources.length > cited.length && (
            <p className="report__source-note">
              このほか {report.sources.length - cited.length} 件を参照しましたが、
              本文の根拠としては引用していません。
            </p>
          )}
        </div>
      )}

      <p className="report__disclaimer">{report.disclaimer}</p>
    </div>
  );
}

function Statement({ item }: { item: Evidenced }) {
  return <>{item.statement}<Refs ids={item.evidence} /></>;
}

function EvidencedList({ title, items }: { title: string; items: Evidenced[] }) {
  if (!items?.length) return null;
  return (
    <>
      <h4>{title}</h4>
      <ul>
        {items.map((item, i) => (
          <li key={i}><Statement item={item} /></li>
        ))}
      </ul>
    </>
  );
}

/** 根拠の id。ここが §25 の追跡の入り口なので、本文から消さない。 */
function Refs({ ids }: { ids?: string[] }) {
  if (!ids?.length) return null;
  return (
    <span className="report__refs">
      {ids.map((id) => <code key={id}>{id}</code>)}
    </span>
  );
}

/** e-Stat の表題は481文字あった。一覧で読めなくなる。 */
function shorten(text: string, limit = 80) {
  const head = text.split(" | ")[0].trim() || text;
  return head.length > limit ? `${head.slice(0, limit - 1)}…` : head;
}

function download(report: AnalysisReport) {
  const blob = new Blob([report.report_markdown ?? ""], {
    type: "text/markdown;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "商圏分析レポート.md";
  link.click();
  URL.revokeObjectURL(url);
}
