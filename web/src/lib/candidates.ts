/** Candidate points shared between the map and the comparison screen. */
import { useCallback, useEffect, useState } from "react";
import type { CandidatePoint } from "./types";

const KEY = "kaigyou.candidates";
export const MAX_CANDIDATES = 3;

function read(): CandidatePoint[] {
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as CandidatePoint[]) : [];
  } catch {
    return [];
  }
}

function write(points: CandidatePoint[]) {
  window.localStorage.setItem(KEY, JSON.stringify(points));
  window.dispatchEvent(new CustomEvent("kaigyou:candidates"));
}

export function useCandidates() {
  const [points, setPoints] = useState<CandidatePoint[]>(read);

  useEffect(() => {
    const sync = () => setPoints(read());
    window.addEventListener("kaigyou:candidates", sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener("kaigyou:candidates", sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  const add = useCallback((lat: number, lng: number, label?: string) => {
    const current = read();
    if (current.length >= MAX_CANDIDATES) return { ok: false as const, points: current };
    const next = [
      ...current,
      {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        lat,
        lng,
        label: label ?? `候補地 ${String.fromCharCode(65 + current.length)}`,
      },
    ];
    write(next);
    return { ok: true as const, points: next };
  }, []);

  const remove = useCallback((id: string) => {
    write(read().filter((p) => p.id !== id));
  }, []);

  const clear = useCallback(() => write([]), []);

  return { points, add, remove, clear };
}
