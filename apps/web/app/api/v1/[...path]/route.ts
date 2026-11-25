// Same-origin API: the browser only ever talks to the web app, which forwards /api/v1/* to the arena engine.
// One image works behind any domain (no API URL baked in at build time), and a reverse proxy needs a single
// upstream. Bodies are streamed both ways, so the SSE stream passes straight through.
export const dynamic = "force-dynamic";

const HOP_BY_HOP = ["host", "connection", "keep-alive", "transfer-encoding", "upgrade", "content-length"];

function upstream(): string {
  return (process.env.ARENA_API_INTERNAL_URL || "http://localhost:8000").replace(/\/$/, "");
}

async function proxy(req: Request, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await ctx.params;
  const url = new URL(req.url);
  const target = `${upstream()}/api/v1/${path.map(encodeURIComponent).join("/")}${url.search}`;
  const headers = new Headers(req.headers);
  HOP_BY_HOP.forEach((h) => headers.delete(h));
  let res: Response;
  try {
    res = await fetch(target, {
      method: req.method,
      headers,
      body: req.method === "GET" || req.method === "HEAD" ? undefined : await req.arrayBuffer(),
      redirect: "manual",
      signal: req.signal,
      cache: "no-store",
    });
  } catch {
    return Response.json({ code: "UPSTREAM_DOWN", message: "擂台服务暂时连不上，请稍后再试" }, { status: 502 });
  }
  const out = new Headers(res.headers);
  ["content-encoding", "content-length", "transfer-encoding", "connection"].forEach((h) => out.delete(h));
  return new Response(res.body, { status: res.status, headers: out });
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as DELETE };
