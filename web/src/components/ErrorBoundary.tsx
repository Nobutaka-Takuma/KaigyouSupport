import React from "react";

/**
 * 描画中の例外を画面に出す。
 *
 * React 18 は捕まえられなかった例外でツリー全体をアンマウントするため、
 * 何も囲っていないと画面が真っ白になり、タブのタイトルだけが残る。
 * 「アプリが表示されない」と「アプリが壊れた」は原因も対処もまったく違うのに、
 * 見た目が同じでは切り分けられない。
 */
export class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("render failed", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="boot-error">
        <h1>画面の描画に失敗しました</h1>
        <p className="muted">
          データではなく画面側の不具合です。以下をそのまま伝えてください。
        </p>
        <pre>
          {this.state.error.name}: {this.state.error.message}
          {"\n"}
          {this.state.error.stack?.split("\n").slice(1, 6).join("\n")}
        </pre>
        <p>
          <button onClick={() => location.reload()}>再読み込み</button>
        </p>
      </div>
    );
  }
}
