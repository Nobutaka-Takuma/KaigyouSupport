/**
 * レポートの Markdown を、画面でそのまま読める HTML にする。
 *
 * **なぜ既製のライブラリを使わないか。** 描くのは自分たちが
 * `kaigyou_intel/report.py` で組み立てた文書だけで、使っている記法は
 * 見出し・強調・箇条書き・表・引用・裸の URL の 6 つしかありません。汎用の
 * パーサを入れると、扱わない記法のために依存が 2 つ増えます（パーサ本体と
 * 消毒器）。このアプリの実行時依存は 4 つで、そこに釣り合いません。
 *
 * **安全側の作りにしています。** 先に全部エスケープしてから、決まった形
 * だけを HTML に組み立てます。「解析してから危ないものを消す」の逆で、
 * 消し忘れが起こりません。本文には LLM が書いた文と外部サイトの題名が
 * 入るので、ここは信用できない文字列として扱います。
 *
 * 対応していない記法（コード・画像・`[題名](URL)` 形式のリンク）は、
 * 書いたとおりの文字として出ます。**黙って消えるより、そのほうが気づけます。**
 * report.py がそれらを使っていないことは、サーバ側の試験が見張っています。
 */

const ESCAPES: Record<string, string> = {
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
};

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/** 裸の URL を含む行を、リンクつきにする。**エスケープ済みの文字列に当てます。** */
function autolink(escaped: string): string {
  // エスケープ後なので & は &amp; になっています。href に入れてよい形です
  // （ブラウザは属性値の実体参照を戻して読みます）。
  return escaped.replace(
    /https?:\/\/[^\s<]+[^\s<.,)）。、]/g,
    (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`,
  );
}

/** 行の中の記法。`**強調**` と裸の URL だけ。 */
function inline(raw: string): string {
  const escaped = escapeHtml(raw);
  const bolded = escaped.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return autolink(bolded);
}

function isTableRow(line: string): boolean {
  return line.trimStart().startsWith("|");
}

/** `|---|---|` の区切り行か。 */
function isTableRule(line: string): boolean {
  return /^\s*\|[\s:|-]+\|\s*$/.test(line) && line.includes("-");
}

function cells(line: string): string[] {
  const trimmed = line.trim();
  const inner = trimmed.slice(
    trimmed.startsWith("|") ? 1 : 0,
    trimmed.endsWith("|") ? -1 : undefined,
  );
  return inner.split("|").map((c) => c.trim());
}

/**
 * Markdown を HTML にする。返り値は `dangerouslySetInnerHTML` に渡せます
 * ——すべてエスケープ済みの文字列から組み立てているためです。
 */
export function renderMarkdown(markdown: string): string {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const html: string[] = [];
  let paragraph: string[] = [];
  let list: string[] = [];
  let quote: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      html.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list.length) {
      html.push(`<ul>${list.map((item) => `<li>${item}</li>`).join("")}</ul>`);
      list = [];
    }
  };
  // 各節の「要点」が引用で書かれています。本文と同じ見た目にすると、
  // 節の結論が地の文に埋もれます。
  const flushQuote = () => {
    if (quote.length) {
      html.push(`<blockquote>${quote.map((q) => `<p>${q}</p>`).join("")}</blockquote>`);
      quote = [];
    }
  };
  const flush = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];

    if (!line.trim()) {
      flush();
      continue;
    }

    // --- 見出し ---------------------------------------------------------
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      // 本文の見出しは h1 から始まりますが、画面ではパネルの中の一部なので
      // 1 段下げます。ページの見出し階層を壊さないため。
      const level = Math.min(heading[1].length + 1, 6);
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }

    // --- 表 -------------------------------------------------------------
    if (isTableRow(line) && i + 1 < lines.length && isTableRule(lines[i + 1])) {
      flush();
      const header = cells(line);
      const body: string[][] = [];
      let j = i + 2;
      for (; j < lines.length && isTableRow(lines[j]) && !isTableRule(lines[j]); j += 1) {
        body.push(cells(lines[j]));
      }
      // 見出しが全部空の表があります（開業方針の「| | |」）。空の行を
      // 描くと、罫線だけの帯が出ます。
      const head = header.some((c) => c)
        ? `<thead><tr>${header.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>`
        : "";
      const rows = body
        .map((row) => `<tr>${row.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
        .join("");
      // 横に長い表は、本文ではなく表の中でだけ横スクロールさせます。
      html.push(`<div class="md__tablewrap"><table>${head}<tbody>${rows}</tbody></table></div>`);
      i = j - 1;
      continue;
    }

    // --- 箇条書き -------------------------------------------------------
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      flushParagraph();
      flushQuote();
      list.push(inline(bullet[1]));
      continue;
    }
    // 箇条書きの続き（2 文字下げ）。出典は題名の次の行に URL が来ます。
    if (list.length && /^\s{2,}\S/.test(line)) {
      list[list.length - 1] += `<br>${inline(line.trim())}`;
      continue;
    }

    // --- 引用（節の要点） -----------------------------------------------
    const quoted = /^\s*>\s?(.*)$/.exec(line);
    if (quoted) {
      flushParagraph();
      flushList();
      quote.push(inline(quoted[1]));
      continue;
    }

    // --- 区切り線 -------------------------------------------------------
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
      flush();
      html.push("<hr>");
      continue;
    }

    // --- 段落 -----------------------------------------------------------
    // **引用も閉じます。** 閉じずに段落だけ溜めると、flush の順番の都合で
    // あとから来た段落が引用より前に出ます。
    flushList();
    flushQuote();
    paragraph.push(line.trim());
  }

  flush();
  return html.join("\n");
}
