/**
 * どの都道府県を見ているか。地図・ランキング・比較で共有する。
 *
 * 分析できる都道府県はコードではなくデータベースの中身で決まるので、一覧は
 * API に聞く。選択を localStorage に置いているのは、ランキングから地図へ
 * 移動したときに県が戻ってしまわないようにするため。読み込み済みでない県が
 * 保存されていた場合（データを入れ替えたとき）は API の既定値に戻す。
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { Prefecture } from "./types";

const KEY = "kaigyou.prefecture";
const CENTER_KEY = "kaigyou.prefecture.center";

function read(): string | null {
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

/**
 * 前回いた場所。地図は API の応答より先に生成しなければならないので、
 * これが無いと毎回いったん東京都心が描かれてから正しい位置へ飛ぶ。
 * 静岡県しか入れていない人にとっては、毎回よその県が一瞬映ることになる。
 */
export function lastCenter(): [number, number] | null {
  try {
    const raw = window.localStorage.getItem(CENTER_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return Array.isArray(parsed) && parsed.length === 2 &&
      parsed.every((v) => Number.isFinite(v))
      ? [parsed[0], parsed[1]]
      : null;
  } catch {
    return null;
  }
}

function rememberCenter(center: [number, number]) {
  try {
    window.localStorage.setItem(CENTER_KEY, JSON.stringify(center));
  } catch {
    /* プライベートウィンドウなど。次回また一瞬ずれるだけ */
  }
}

function write(code: string) {
  try {
    window.localStorage.setItem(KEY, code);
  } catch {
    /* プライベートウィンドウなど。選択が保存されないだけで動作はする */
  }
  window.dispatchEvent(new CustomEvent("kaigyou:prefecture"));
}

export function usePrefecture() {
  const [list, setList] = useState<Prefecture[]>([]);
  const [code, setCode] = useState<string | null>(read);
  /** 一覧が届くまでは県を指定せずに問い合わせる（サーバ側が既定を決める）。 */
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .prefectures()
      .then((res) => {
        if (cancelled) return;
        setList(res.prefectures);
        const stored = read();
        const known = res.prefectures.some((p) => p.code === stored);
        setCode(known && stored ? stored : res.default);
        setLoaded(true);
      })
      .catch(() => !cancelled && setLoaded(true));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const sync = () => setCode(read());
    window.addEventListener("kaigyou:prefecture", sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener("kaigyou:prefecture", sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  const select = useCallback((next: string) => {
    write(next);
    setCode(next);
  }, []);

  const current = list.find((p) => p.code === code) ?? null;

  useEffect(() => {
    if (current) rememberCenter(current.center);
  }, [current]);

  return { list, code, current, select, loaded };
}
