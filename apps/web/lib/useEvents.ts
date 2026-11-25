"use client";
import { useEffect, useRef } from "react";
import { API_BASE } from "./api";

export type ArenaEvent = { type: string; data: Record<string, unknown> };

const TYPES = [
  "market.open", "market.close", "order.accepted", "order.filled", "order.partially_filled", "order.canceled",
  "order.expired", "limit_up.queue", "limit_up.broken", "limit_up.sealed", "limit_down.broken",
  "conflict.detected", "badge.awarded", "roast.daily",
];

/** Subscribes to the arena SSE stream; EventSource reconnects by itself and resends Last-Event-ID. */
export function useEvents(onEvent: (e: ArenaEvent) => void) {
  const cb = useRef(onEvent);
  cb.current = onEvent;
  useEffect(() => {
    const es = new EventSource(`${API_BASE}/api/v1/events/stream`);
    const handler = (type: string) => (msg: MessageEvent) => {
      try {
        cb.current({ type, data: JSON.parse(msg.data) });
      } catch {
        /* ignore malformed frames */
      }
    };
    const handlers = TYPES.map((t) => [t, handler(t)] as const);
    handlers.forEach(([t, h]) => es.addEventListener(t, h as EventListener));
    return () => es.close();
  }, []);
}

/** Polls `fn` every `ms` while the tab is visible. */
export function usePoll(fn: () => void, ms: number) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    ref.current();
    const id = setInterval(() => {
      if (document.visibilityState === "visible") ref.current();
    }, ms);
    return () => clearInterval(id);
  }, [ms]);
}
