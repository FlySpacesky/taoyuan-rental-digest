// Isolated, short-lived diagnostics only. Never import the production fetch or
// scheduled handlers, bind its KV, or provision its GitHub/LINE secrets here.
import { normalizeYungchingWorkerItem, readYungchingDetail } from "./index.js";

export const PREVIEW_NAME = "taoyuan-rental-yungching-cpu-preview";
export const SAMPLE_ID = "2415719";
const SAMPLE_URL = `https://rent.yungching.com.tw/house/${SAMPLE_ID}`;
const NO_STORE = { "Cache-Control": "no-store" };

function authorized(request, env) {
  const expected = String(env.PREVIEW_PROBE_TOKEN || "");
  const supplied = request.headers.get("Authorization") || "";
  if (expected.length < 32 || supplied.length !== expected.length + 7) return false;
  const wanted = `Bearer ${expected}`;
  let mismatch = 0;
  for (let i = 0; i < wanted.length; i++) mismatch |= wanted.charCodeAt(i) ^ supplied.charCodeAt(i);
  return mismatch === 0;
}

function audit(stage, extra = {}) {
  console.log(JSON.stringify({ service: PREVIEW_NAME, stage, ...extra }));
}

export async function sourceFetchProbe(fetchImpl = fetch) {
  audit("source_fetch_start");
  const upstream = await fetchImpl(SAMPLE_URL, {
    redirect: "manual",
    signal: AbortSignal.timeout(15_000),
    headers: {
      "Accept": "text/html,application/xhtml+xml",
      "Accept-Language": "zh-TW,zh;q=0.9",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    },
  });
  audit("source_fetch_response", { upstream_status: upstream.status });
  // Stream the original public HTML. Parsing outside the Worker avoids both
  // Chromium protocol overhead and buffering/JSON parsing in its CPU budget.
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      ...NO_STORE,
      "Content-Type": upstream.headers.get("Content-Type") || "text/html; charset=utf-8",
      "X-Preview-Source-Status": String(upstream.status),
      "X-Preview-Observed-At": new Date().toISOString(),
      "X-Preview-Source-Url": SAMPLE_URL,
      "X-Preview-Mode": "fetch-stream",
    },
  });
}

export async function browserDetailProbe(binding, api) {
  if (!binding) throw new Error("preview_browser_not_configured");
  api ||= (await import("@cloudflare/puppeteer")).default;
  // Account-wide Browser Run is shared. Never take over or close a production
  // session, and skip this test when any other session is currently active.
  const sessions = await api.sessions(binding);
  if (sessions.length) throw new Error("shared_browser_sessions_active: preview skipped");
  audit("browser_launch_start");
  let browser;
  let timer;
  try {
    browser = await api.launch(binding); // one attempt only, no quota retries
    audit("browser_launched");
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {
        browser.close().catch(() => {});
        reject(new Error("preview_wall_budget_exceeded"));
      }, 20_000);
    });
    const work = (async () => {
      const page = await browser.newPage();
      await page.setJavaScriptEnabled(false);
      audit("detail_start", { source_id: SAMPLE_ID });
      const detail = await readYungchingDetail(page, { source_id: SAMPLE_ID });
      const item = detail && normalizeYungchingWorkerItem(detail);
      audit("detail_complete", { validated_count: item ? 1 : 0 });
      return {
        service: PREVIEW_NAME,
        scope: "single-detail-only",
        crawl_complete: false,
        candidate_count: 1,
        validated_count: item ? 1 : 0,
        items: item ? [{ ...item, validated_at: detail.validated_at }] : [],
      };
    })();
    return await Promise.race([work, deadline]);
  } finally {
    clearTimeout(timer);
    if (browser) await browser.close().catch(() => {});
    audit("browser_closed");
  }
}

export function createPreviewWorker({ fetchImpl, browserProbe = browserDetailProbe } = {}) {
  return {
    async fetch(request, env) {
      const path = new URL(request.url).pathname;
      if (request.method === "GET" && path === "/health") {
        return Response.json({
          status: "ok", service: PREVIEW_NAME, isolated: true,
          production_handlers: false, cron: false, kv: false, line: false,
          commit: env.PREVIEW_COMMIT || "unknown",
        }, { headers: NO_STORE });
      }
      if (request.method !== "POST" || !["/probe-fetch", "/probe-browser"].includes(path)) {
        return Response.json({ error: "not_found" }, { status: 404, headers: NO_STORE });
      }
      if (!authorized(request, env)) return new Response("Unauthorized", { status: 401, headers: NO_STORE });
      if (!(Number(env.PREVIEW_EXPIRES_AT_MS) > Date.now())) {
        return Response.json({ error: "preview_expired" }, { status: 410, headers: NO_STORE });
      }
      try {
        if (path === "/probe-fetch") return await sourceFetchProbe(fetchImpl);
        return Response.json(await browserProbe(env.BROWSER), { headers: NO_STORE });
      } catch (error) {
        const message = String(error?.message || error).slice(0, 300);
        audit("probe_failed", { error: message });
        return Response.json({ service: PREVIEW_NAME, status: "degraded", error: message }, {
          status: 502, headers: NO_STORE,
        });
      }
    },
  };
}

export default createPreviewWorker();
