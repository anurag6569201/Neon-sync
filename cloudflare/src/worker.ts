/**
 * Neon Sync Cloudflare Edge Gateway Worker
 *
 * Streams traffic cleanly to the Neon Sync origin without buffering unbounded
 * media or SSE responses, adds edge trace IDs, security headers, and shields
 * origin details behind the unified Neon Sync brand.
 */

export interface Env {
  NEON_SYNC_ORIGIN: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const incomingUrl = new URL(request.url);
    const originBase = (env.NEON_SYNC_ORIGIN || "").replace(/\/+$/, "");
    if (!originBase) {
      return new Response(
        JSON.stringify({
          detail: {
            code: "ERR_EDGE_ORIGIN_UNCONFIGURED",
            message: "Neon Sync edge origin is not configured.",
            service: "neon-sync",
          },
        }),
        { status: 503, headers: { "content-type": "application/json; charset=utf-8" } },
      );
    }

    // Handle lightweight edge health probe directly
    if (incomingUrl.pathname === "/_edge/health") {
      return new Response(
        JSON.stringify({
          status: "ok",
          service: "neon-sync-edge",
          colo: (request.cf as { colo?: string } | undefined)?.colo ?? "EDGE",
        }),
        {
          status: 200,
          headers: {
            "content-type": "application/json; charset=utf-8",
            "x-powered-by": "Neon Sync",
          },
        },
      );
    }

    const targetUrl = `${originBase}${incomingUrl.pathname}${incomingUrl.search}`;
    const edgeReqId = request.headers.get("x-request-id") || `ns_edge_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;

    const upstreamHeaders = new Headers(request.headers);
    upstreamHeaders.set("x-request-id", edgeReqId);
    upstreamHeaders.set("x-forwarded-host", incomingUrl.host);
    upstreamHeaders.set("x-forwarded-proto", incomingUrl.protocol.replace(":", ""));
    const cfIp = request.headers.get("cf-connecting-ip");
    if (cfIp) {
      upstreamHeaders.set("x-forwarded-for", cfIp);
    }

    // Stream request & response bodies directly (never buffer video/SSE payloads in memory!)
    const upstreamReq = new Request(targetUrl, {
      method: request.method,
      headers: upstreamHeaders,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
      redirect: "manual",
    });

    let upstreamResp: Response;
    try {
      upstreamResp = await fetch(upstreamReq);
    } catch {
      return new Response(
        JSON.stringify({
          detail: {
            code: "ERR_EDGE_UPSTREAM_UNREACHABLE",
            message: "Neon Sync origin is temporarily unreachable.",
            service: "neon-sync",
            request_id: edgeReqId,
          },
        }),
        {
          status: 502,
          headers: {
            "content-type": "application/json; charset=utf-8",
            "x-request-id": edgeReqId,
            "x-powered-by": "Neon Sync",
          },
        },
      );
    }

    const outHeaders = new Headers(upstreamResp.headers);
    outHeaders.set("x-request-id", outHeaders.get("x-request-id") || edgeReqId);
    outHeaders.set("x-powered-by", "Neon Sync");
    outHeaders.set("x-neon-edge", "cloudflare");
    outHeaders.set("x-content-type-options", "nosniff");
    outHeaders.set("referrer-policy", "strict-origin-when-cross-origin");
    outHeaders.delete("server");

    return new Response(upstreamResp.body, {
      status: upstreamResp.status,
      statusText: upstreamResp.statusText,
      headers: outHeaders,
    });
  },
};
