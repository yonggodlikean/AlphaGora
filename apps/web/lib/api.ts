/** "" = same origin (the web app forwards /api/v1 to the engine). Set only to talk to an API on another origin. */
export const API_BASE = (process.env.NEXT_PUBLIC_ARENA_API_URL || "").replace(/\/$/, "");

/** Absolute API root for copy-paste snippets shown to bot authors. */
export function publicApiRoot(): string {
  const origin = API_BASE || (typeof window !== "undefined" ? window.location.origin : "");
  return `${origin}/api/v1`;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

const TOKEN_KEY = "agora_token";

export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string, name: string) {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
    window.localStorage.setItem("agora_name", name);
  } catch {
    /* private mode: token lives only in this tab's memory */
  }
}

export function clearToken() {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem("agora_name");
  } catch {
    /* ignore */
  }
}

/** Anonymous per-browser id: counts unique visitors and one cheer per team per day. Never tied to a person. */
export function visitorId(): string {
  try {
    let v = window.localStorage.getItem("agora_visitor");
    if (!v) {
      v = crypto.randomUUID();
      window.localStorage.setItem("agora_visitor", v);
    }
    return v;
  } catch {
    return "";
  }
}

export async function api<T>(path: string, init: RequestInit & { auth?: boolean } = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const v = typeof window !== "undefined" ? visitorId() : "";
  if (v) headers["X-Visitor"] = v;
  if (init.auth) {
    const t = getToken();
    if (t) headers.Authorization = `Bearer ${t}`;
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...init, headers: { ...headers, ...(init.headers as object) } });
  } catch {
    throw new ApiError(0, "NETWORK", "连不上擂台服务器，请稍后再试");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, body.code || "HTTP_ERROR", body.message || `请求失败（${res.status}）`);
  return body as T;
}

// ---------------------------------------------------------------- response types
export type Phase = "holiday" | "pre_open" | "auction" | "trading" | "waiting" | "lunch" | "closed";
export type Market = { trading: boolean; phase?: Phase; day_open: boolean; trade_date: string | null; clock: string; now?: string; simulated: boolean; feed: string; online?: number; visitors_today?: number };
export type Driver = { kind: "rules" | "external"; name: string | null; online: boolean; last_seen: string | null } | null;
export type Group = "official_ai" | "community_ai" | "benchmark" | "human" | "hybrid" | "bot";
export type BoardRow = {
  portfolio_id: string; name: string; group: Group; rank: number | null; cum_return: number;
  excess_return: number; day_return: number; spark: number[]; equity: string; driver: Driver; cheers: number;
};
export type Conflict = {
  symbol: string; symbol_name: string; ts: string; buyer: string; buyer_id: string; buy_qty: number; buy_thesis: string;
  seller: string; seller_id: string; sell_qty: number; sell_thesis: string;
};
export type OrderView = {
  order_id: string; portfolio_id: string; portfolio_name?: string; symbol: string; symbol_name?: string;
  side: "buy" | "sell"; order_type: "limit" | "market"; price: string | null; qty: number; filled_qty: number;
  avg_fill_price: string | null; status: string; queue_initial: number | null; queue_ahead: number | null;
  queue_progress_pct: number | null; thesis: string | null; debate_id: string | null; created_at?: string;
  sealed?: boolean; duplicate?: boolean;
};
export type Feed = {
  conflicts: Conflict[]; roast: { trade_date: string; text: string; lines: Record<string, string> } | null;
  queues: OrderView[]; market: Market; season: { slug: string; name: string; trading_days: number };
};
export type SymbolRow = {
  symbol: string; name: string; last: string; prev_close: string; chg_pct: number; upper_limit: string;
  lower_limit: string; limit_up_sealed: boolean; limit_down_sealed: boolean; suspended: boolean;
};
export type Snapshot = {
  symbol: string; name: string; bids: [string, number][]; asks: [string, number][]; last_price: string;
  upper_limit: string; lower_limit: string; prev_close: string; limit_up_sealed: boolean;
};
export type Position = { symbol: string; name: string; qty: number; sellable_qty: number; frozen_qty: number; reserved_qty: number; cost_basis: string; market_value: string };
export type AgentState = {
  portfolio_id: string; portfolio_name: string; cash: string; available_cash: string; total_equity: string;
  positions: Position[]; open_orders: OrderView[]; rank: number | null; cum_return: number; beaten_ai: number;
  ai_total: number; chase: { name: string; gap: number } | null; market: Market;
};
export type Badge = { code: string; name: string; desc: string; glyph: string; count: number };
export type Profile = {
  portfolio_id: string; name: string; group: Group; brief: string | null; is_official: boolean; glyph: string; color: string;
  rank: number | null; cum_return: number; excess_return: number; max_drawdown: number; win_rate: number | null;
  closed_trades: number; queue_success: number | null; queue_count: number; equity: string; cash: string;
  badges: Badge[]; positions: { symbol: string; name: string; qty: number; frozen_qty: number; cost: string; pnl_pct: number }[];
  roast_line: string | null; driver: Driver; cheers: number; status: "active" | "suspended" | "banned";
};
export type Seats = { invite_required: boolean; human: { taken: number; total: number }; ai: { taken: number; total: number } };
export type TimelineOrder = {
  order_id: string; side: "buy" | "sell"; order_type: string; price: string | null; qty: number; filled_qty: number;
  avg_fill_price: string | null; fees_paid: string; status: string; thesis: string | null; queue_initial: number | null;
  liquidity: string[]; reason: string | null; created_at: string; queue_progress_pct?: number | null;
};
export type TimelineItem = {
  kind: "debate" | "order" | "corporate"; id: string; symbol: string; symbol_name: string; created_at: string;
  memo?: string; cash?: string; bonus_shares?: number;
  rounds?: { role: string; stance: "buy" | "sell" | "hold"; argument: string; model_id?: string }[];
  final_decision?: { title?: string; summary?: string; action?: string }; has_conflict?: boolean; orders: TimelineOrder[];
};
