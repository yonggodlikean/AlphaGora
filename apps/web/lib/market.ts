"use client";
import { useEffect, useState } from "react";
import { api, type Market, type Phase } from "./api";

/** One shared poll of /market/status for the whole page (the nav and any page that needs the phase). */
type Listener = (m: Market | null, failed: boolean) => void;
const listeners = new Set<Listener>();
let last: Market | null = null;
let failures = 0;
let timer: ReturnType<typeof setInterval> | null = null;

function poll() {
  if (document.visibilityState !== "visible") return;
  api<Market>("/api/v1/market/status").then((m) => {
    failures = 0;
    last = m;
    listeners.forEach((l) => l(m, false));
  }).catch(() => {
    failures += 1;
    if (failures >= 3) {          // one dropped request should not flash "连接中"
      last = null;
      listeners.forEach((l) => l(null, true));
    }
  });
}

export function subscribeMarket(l: Listener): () => void {
  listeners.add(l);
  if (last) l(last, false);
  if (!timer) {
    poll();
    timer = setInterval(poll, 5000);
    document.addEventListener("visibilitychange", poll);
  }
  return () => {
    listeners.delete(l);
    if (!listeners.size && timer) {
      clearInterval(timer);
      timer = null;
      document.removeEventListener("visibilitychange", poll);
    }
  };
}

/** Phases in which prices, queues and the board actually move. Outside them pages stop polling. */
const LIVE: Phase[] = ["auction", "open_wait", "trading", "waiting", "closing_auction"];

export function useMarket(): Market | null {
  const [m, setM] = useState<Market | null>(last);
  useEffect(() => subscribeMarket((x) => setM(x)), []);
  return m;
}

export function isLive(m: Market | null): boolean {
  return !!m && (m.phase ? LIVE.includes(m.phase) : m.trading);
}
