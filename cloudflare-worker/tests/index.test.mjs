import assert from "node:assert/strict";
import test from "node:test";

import {
  assessWorkflowRuns,
  buildYungchingFeed,
  browserFailureCode,
  launchYungchingBrowser,
  yungchingFeedResponse,
  deliverySlotFor,
  handleScheduled,
  normalizeYungchingWorkerItem,
  submitFacebookInbox,
  yungchingRenderResponse,
  yungchingRenderTarget,
} from "../src/index.js";
import worker from "../src/index.js";


const scheduledTime = Date.parse("2026-08-09T01:35:00Z");
const controller = { cron: "35-59 1 * * *", scheduledTime };
const env = { GITHUB_TOKEN: "test-token" };


class MemoryKv {
  constructor() {
    this.values = new Map();
  }

  async put(key, value) {
    this.values.set(key, value);
  }

  async get(key, type) {
    const value = this.values.get(key);
    if (value === undefined) return null;
    return type === "json" ? JSON.parse(value) : value;
  }

  async list({ prefix = "", limit = 100 } = {}) {
    const keys = [...this.values.keys()]
      .filter((key) => key.startsWith(prefix))
      .slice(0, limit)
      .map((name) => ({ name }));
    return { keys, list_complete: true };
  }
}


function facebookEnv() {
  return {
    GITHUB_TOKEN: "configured",
    FB_INBOX: new MemoryKv(),
    FB_INBOX_WRITE_TOKEN: "write-only-test-token",
    FB_INBOX_READ_TOKEN: "read-only-test-token",
  };
}


function facebookRequest(path, token, body, origin = "https://flyspacesky.github.io") {
  const headers = {
    Authorization: `Bearer ${token}`,
    Origin: origin,
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return new Request(`https://watchdog.example${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}


function validFacebookSubmission(overrides = {}) {
  return {
    url: "https://www.facebook.com/groups/987654321/posts/1234567890123/?tracking=1",
    post_text: "桃園區屋主自租，4房2廳，35坪，租金 28000 元。",
    published_at: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
    publisher: "屋主林先生",
    republish_authorized: true,
    no_facebook_credentials: true,
    ...overrides,
  };
}


function response(status, body = "") {
  return new Response(
    body ? JSON.stringify(body) : null,
    {
      status,
      headers: body ? { "Content-Type": "application/json" } : {},
    },
  );
}


function githubMock(runs, dispatchStatus = 204) {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    if (String(url).endsWith("/dispatches")) {
      return response(dispatchStatus);
    }
    return response(200, { workflow_runs: runs });
  };
  return { calls, fetchImpl };
}


test("maps UTC backup cron to the Taipei delivery slot", () => {
  assert.deepEqual(deliverySlotFor(controller), {
    label: "2026-08-09T09:30+08:00",
    timestamp: Date.parse("2026-08-09T09:30+08:00"),
    time: "09:30",
  });
});


test("does not dispatch when the scheduled workflow already succeeded", async () => {
  const { calls, fetchImpl } = githubMock([
    {
      event: "schedule",
      status: "completed",
      conclusion: "success",
      created_at: "2026-08-09T01:30:10Z",
    },
  ]);

  const result = await handleScheduled(controller, env, fetchImpl);

  assert.equal(result.result, "healthy");
  assert.equal(result.dispatched, false);
  assert.equal(calls.length, 1);
});


test("waits when the normal workflow is still running", async () => {
  const runs = [
    {
      event: "schedule",
      status: "in_progress",
      conclusion: null,
      created_at: "2026-08-09T01:31:00Z",
    },
  ];

  assert.equal(
    assessWorkflowRuns(
      runs,
      Date.parse("2026-08-09T01:30:00Z"),
      scheduledTime,
    ).action,
    "wait",
  );
});


test("dispatches the exact slot when the GitHub run is missing", async () => {
  const { calls, fetchImpl } = githubMock([]);

  const result = await handleScheduled(controller, env, fetchImpl);

  assert.equal(result.result, "missing");
  assert.equal(result.dispatched, true);
  assert.equal(calls.length, 2);
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    ref: "main",
    inputs: { delivery_slot: "2026-08-09T09:30+08:00" },
  });
});


test("retries a failed run at the next backup check", async () => {
  const retryController = {
    cron: "35-59 1 * * *",
    scheduledTime: Date.parse("2026-08-09T01:45:00Z"),
  };
  const { fetchImpl } = githubMock([
    {
      event: "schedule",
      status: "completed",
      conclusion: "failure",
      created_at: "2026-08-09T01:30:00Z",
    },
  ]);

  const result = await handleScheduled(retryController, env, fetchImpl);

  assert.equal(result.result, "retry");
  assert.equal(result.dispatched, true);
});


test("health reports whether the GitHub secret is configured", async () => {
  const healthy = await worker.fetch(
    new Request("https://watchdog.example/health"),
    facebookEnv(),
  );
  const degraded = await worker.fetch(
    new Request("https://watchdog.example/health"),
    {},
  );

  const healthyBody = await healthy.json();
  assert.equal(healthy.status, 200);
  assert.equal(healthyBody.githubTokenConfigured, true);
  assert.equal(healthyBody.browserRunConfigured, false);
  assert.equal(healthyBody.facebookInboxConfigured, true);
  assert.equal(degraded.status, 503);
  assert.equal((await degraded.json()).githubTokenConfigured, false);
});


test("private Facebook inbox stores an authorized post and exposes it only to read token", async () => {
  const privateEnv = facebookEnv();
  const submitted = await worker.fetch(
    facebookRequest("/facebook-inbox", "write-only-test-token", validFacebookSubmission()),
    privateEnv,
  );
  assert.equal(submitted.status, 201);
  const receipt = await submitted.json();
  assert.equal(receipt.status, "accepted");
  assert.match(receipt.id, /^[a-f0-9]{32}$/);

  const denied = await worker.fetch(
    facebookRequest("/facebook-inbox-feed", "write-only-test-token"),
    privateEnv,
  );
  assert.equal(denied.status, 401);

  const feed = await worker.fetch(
    facebookRequest("/facebook-inbox-feed", "read-only-test-token"),
    privateEnv,
  );
  assert.equal(feed.status, 200);
  const payload = await feed.json();
  assert.equal(payload.posts.length, 1);
  assert.equal(
    payload.posts[0].url,
    "https://www.facebook.com/groups/987654321/posts/1234567890123/",
  );
  assert.equal(payload.posts[0].republish_authorized, true);
  assert.equal(payload.posts[0].no_facebook_credentials, undefined);
});


test("private Facebook inbox triggers one web-only refresh when enabled", async () => {
  const privateEnv = {
    ...facebookEnv(),
    FACEBOOK_AUTO_REFRESH: "true",
  };
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return response(204);
  };
  const submitted = await submitFacebookInbox(
    facebookRequest("/facebook-inbox", "write-only-test-token", validFacebookSubmission()),
    privateEnv,
    fetchImpl,
  );
  const receipt = await submitted.json();

  assert.equal(submitted.status, 201);
  assert.equal(receipt.refresh_dispatched, true);
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /actions\/workflows\/rental-digest\.yml\/dispatches$/);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    ref: "main",
    inputs: { skip_line: "true" },
  });
});


test("private Facebook inbox deduplicates the same permanent post URL", async () => {
  const privateEnv = facebookEnv();
  const first = await worker.fetch(
    facebookRequest("/facebook-inbox", "write-only-test-token", validFacebookSubmission()),
    privateEnv,
  );
  const second = await worker.fetch(
    facebookRequest(
      "/facebook-inbox",
      "write-only-test-token",
      validFacebookSubmission({ publisher: "更新後屋主名稱" }),
    ),
    privateEnv,
  );
  assert.equal(first.status, 201);
  assert.equal(second.status, 201);
  assert.equal(privateEnv.FB_INBOX.values.size, 1);
  const feed = await worker.fetch(
    facebookRequest("/facebook-inbox-feed", "read-only-test-token"),
    privateEnv,
  );
  assert.equal((await feed.json()).posts[0].publisher, "更新後屋主名稱");
});


test("private Facebook inbox rejects missing consent, short links, old posts, and credential fields", async () => {
  const cases = [
    validFacebookSubmission({ republish_authorized: false }),
    validFacebookSubmission({ url: "https://www.facebook.com/share/p/abc123/" }),
    validFacebookSubmission({
      published_at: new Date(Date.now() - 8 * 24 * 60 * 60 * 1000).toISOString(),
    }),
    { ...validFacebookSubmission(), cookie: "forbidden" },
  ];
  for (const body of cases) {
    const rejected = await worker.fetch(
      facebookRequest("/facebook-inbox", "write-only-test-token", body),
      facebookEnv(),
    );
    assert.equal(rejected.status, 422);
  }
});


test("private Facebook inbox does not accept the read token for writing", async () => {
  const rejected = await worker.fetch(
    facebookRequest("/facebook-inbox", "read-only-test-token", validFacebookSubmission()),
    facebookEnv(),
  );
  assert.equal(rejected.status, 401);
});


test("private Facebook inbox allows CORS only from the newsletter site", async () => {
  const allowed = await worker.fetch(
    new Request("https://watchdog.example/facebook-inbox", {
      method: "OPTIONS",
      headers: { Origin: "https://flyspacesky.github.io" },
    }),
    facebookEnv(),
  );
  const blocked = await worker.fetch(
    new Request("https://watchdog.example/facebook-inbox", {
      method: "OPTIONS",
      headers: { Origin: "https://evil.example" },
    }),
    facebookEnv(),
  );
  assert.equal(allowed.headers.get("Access-Control-Allow-Origin"), "https://flyspacesky.github.io");
  assert.equal(blocked.headers.get("Access-Control-Allow-Origin"), null);
});


test("normalizes a verified Yungching Browser Run item and preserves official new tab", () => {
  const item = normalizeYungchingWorkerItem(
    {
      source_id: "2411508",
      title: "冠倫大國四房",
      address: "桃園市桃園區大有路",
      layout: "4房(室)2廳2衛",
      size: "50.63坪",
      floor: "9 / 17樓",
      rent: "26,000元/月",
      updated: "2026年08月12日",
      images: [
        "https://yccdn.yungching.com.tw/real-a.jpg",
        "https://rent.yungching.com.tw/list/assets/rent_og.jpg",
      ],
    },
    new Set(["2411508"]),
  );

  assert.equal(item.source_id, "2411508");
  assert.equal(item.rent, 26_000);
  assert.equal(item.floor, "9/17樓");
  assert.deepEqual(item.filter_tags, ["new"]);
  assert.deepEqual(item.images, ["https://yccdn.yungching.com.tw/real-a.jpg"]);
});


test("rejects Browser Run rows outside the requested districts or below four rooms", () => {
  const base = {
    source_id: "2411508",
    title: "房源",
    address: "桃園市桃園區大有路",
    layout: "4房(室)2廳2衛",
    rent: 26_000,
    updated: "2026年08月12日",
    images: [],
  };
  assert.equal(
    normalizeYungchingWorkerItem({ ...base, address: "桃園市龜山區文化路" }),
    null,
  );
  assert.equal(
    normalizeYungchingWorkerItem({ ...base, layout: "3房(室)2廳2衛" }),
    null,
  );
});


test("Yungching feed returns a stable degraded schema when Browser Run is absent", async () => {
  const response = await worker.fetch(
    new Request("https://watchdog.example/yungching-feed"),
    {},
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.status, "degraded");
  assert.equal(payload.healthy, false);
  assert.equal(payload.validated_count, 0);
  assert.deepEqual(payload.items, []);
  assert.equal(payload.fresh_validation.successful, false);
});

test("Yungching feed reuses one browser for list and detail navigation", async () => {
  let launchCount = 0;
  let closeCount = 0;
  const launch = async () => {
    launchCount += 1;
    const session = launchCount;
    let currentUrl = "";
    let evaluateCount = 0;
    const page = {
      async setUserAgent() {},
      async setJavaScriptEnabled(value) { assert.equal(value, false); },
      async goto(url) { currentUrl = url; },
      async waitForSelector() {},
      async $$eval() {
        if (session !== 1) return [];
        return [{
          source_id: "2411508",
          title: "冠倫大國",
          address: "桃園市桃園區大有路",
          layout: "4房(室)2廳2衛",
          size: "46坪",
          floor: "9 / 17樓",
          rent: "26,000元/月",
          image: "https://yccdn.yungching.com.tw/real-a.jpg",
        }];
      },
      async evaluate() {
        evaluateCount += 1;
        if (evaluateCount === 1) return false;
        return {
          text: "桃園市桃園區大有路 4房(室)2廳2衛 坪數46坪 更新日期2026年08月21日",
          title: "冠倫大國",
          publisher: "永慶房屋",
          summary: "四房整層住家",
          images: ["https://yccdn.yungching.com.tw/real-a.jpg"],
          offerPrice: "26000",
        };
      },
    };
    return {
      async newPage() { return page; },
      async close() { closeCount += 1; },
    };
  };

  const payload = await buildYungchingFeed({}, launch);

  assert.equal(payload.candidate_count, 1);
  assert.equal(payload.validated_count, 1);
  assert.equal(payload.items[0].source_id, "2411508");
  assert.equal(launchCount, 1);
  assert.equal(closeCount, launchCount);
  assert.equal(payload.crawl_complete, true);
  assert.ok(payload.items[0].validated_at);
});

test("browser launch respects Retry-After and never rapidly retries daily quota", async () => {
  let calls = 0;
  const delays = [];
  const browser = {};
  const api = {
    async launch() {
      calls += 1;
      if (calls === 1) throw Object.assign(new Error("429 Rate limit exceeded"), { headers: new Headers({ "Retry-After": "30" }) });
      return browser;
    },
    async limits() { return { timeUntilNextAllowedBrowserAcquisition: 10_000 }; },
  };
  assert.equal(await launchYungchingBrowser({}, api, async ms => delays.push(ms)), browser);
  assert.deepEqual(delays, [30_000]);
  calls = 0;
  await assert.rejects(launchYungchingBrowser({}, { async launch() {
    calls += 1;
    throw new Error("429 Browser time limit exceeded for today");
  } }, async () => assert.fail("daily quota must not be retried")), /today/);
  assert.equal(calls, 1);
  assert.equal(browserFailureCode(new Error("429 Browser time limit exceeded for today")), "browser_daily_quota");
});

test("unverified list failure is degraded, never a healthy empty market", async () => {
  const payload = await buildYungchingFeed({}, async () => ({
    async newPage() { return { async setUserAgent() {}, async goto() { throw new Error("upstream_list_http_403"); } }; },
    async close() {},
  }));
  assert.equal(payload.candidate_count, 0);
  assert.equal(payload.crawl_complete, false);
  const response = await yungchingFeedResponse(new Request("https://example.com/yungching-feed?validation_id=round1"), {}, null, async () => payload);
  const body = await response.json();
  assert.equal(response.status, 200);
  assert.equal(body.healthy, false);
  assert.equal(body.fresh_validation.successful, false);
});

test("same validation requests share in-flight work but new rounds never use old results", async () => {
  let calls = 0;
  let finish;
  const build = () => { calls += 1; return new Promise(resolve => { finish = resolve; }); };
  const request = new Request("https://example.com/yungching-feed?validation_id=concurrent1");
  const first = yungchingFeedResponse(request, {}, null, build);
  const second = yungchingFeedResponse(request, {}, null, build);
  finish({ candidate_count: 0, validated_count: 0, crawl_complete: true, items: [], errors: [] });
  const results = await Promise.all([first, second]);
  assert.equal(calls, 1);
  assert.equal((await results[0].json()).validation_id, "concurrent1");
  const next = await yungchingFeedResponse(new Request("https://example.com/yungching-feed?validation_id=concurrent2"), {}, null, async () => {
    calls += 1;
    return { candidate_count: 3, validated_count: 1, crawl_complete: false, items: [{ source_id: "1" }], errors: ["429"] };
  });
  const body = await next.json();
  assert.equal(calls, 2);
  assert.equal(body.status, "partial");
  assert.equal(body.fresh_validation.successful, true);
  assert.equal(body.items.length, 1);
});

test("Yungching render accepts only bounded list and numeric detail targets", () => {
  assert.match(yungchingRenderTarget({ kind: "list", category: "all", page: 1 }).url, /od=80&pg=1$/);
  assert.match(yungchingRenderTarget({ kind: "list", category: "new", page: 5 }).url, /new_filter\?od=80&pg=5$/);
  assert.equal(
    yungchingRenderTarget({ kind: "detail", source_id: "2415719" }).url,
    "https://rent.yungching.com.tw/house/2415719",
  );
  assert.throws(() => yungchingRenderTarget({ kind: "list", category: "all", page: 6 }), /invalid/);
  assert.throws(() => yungchingRenderTarget({ kind: "detail", source_id: "../../admin" }), /invalid/);
  assert.throws(() => yungchingRenderTarget({ kind: "url", url: "https://example.com" }), /invalid/);
});

test("private Yungching render streams one quick action and rejects the wrong token", async () => {
  const calls = [];
  const renderEnv = {
    FB_INBOX_READ_TOKEN: "read-only-test-token",
    BROWSER: {
      async quickAction(...args) {
        calls.push(args);
        return new Response('{"success":true,"result":"<h1>rendered</h1>"}', {
          status: 200,
          headers: { "Content-Type": "application/json", "X-Browser-Ms-Used": "6500" },
        });
      },
    },
  };
  const wrong = await yungchingRenderResponse(
    facebookRequest("/yungching-render", "wrong", { kind: "detail", source_id: "2415719" }),
    renderEnv,
  );
  assert.equal(wrong.status, 401);
  assert.equal(calls.length, 0);
  const result = await yungchingRenderResponse(
    facebookRequest("/yungching-render", "read-only-test-token", { kind: "detail", source_id: "2415719" }),
    renderEnv,
  );
  assert.equal(result.status, 200);
  assert.equal(result.headers.get("X-Rental-Source-Id"), "2415719");
  assert.equal(result.headers.get("X-Browser-Ms-Used"), "6500");
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "content");
  assert.equal(calls[0][1].url, "https://rent.yungching.com.tw/house/2415719");
});

test("browser is closed if page setup fails", async () => {
  let closed = 0;
  const payload = await buildYungchingFeed({}, async () => ({
    async newPage() { throw new Error("TargetCloseError"); },
    async close() { closed += 1; },
  }));
  assert.equal(closed, 1);
  assert.equal(payload.crawl_complete, false);
});

test("real crawl preserves validated items when a later browser cannot be reopened", async () => {
  let launches = 0;
  let evaluation = 0;
  let details = 0;
  const candidates = ["2415719", "2415405"].map(source_id => ({
    source_id, title: "四房", address: "桃園市桃園區上海路", layout: "4房2廳2衛",
    rent: 25000, image: "https://yccdn.yungching.com.tw/photo.jpg",
  }));
  const page = {
    async setUserAgent() {}, async setJavaScriptEnabled() {}, async waitForSelector() {},
    async goto(url) {
      if (url.includes("/house/") && ++details > 1) throw new Error("TargetCloseError");
    },
    async $$eval() { return candidates; },
    async evaluate() {
      if (++evaluation % 2) return false;
      return { title: "四房", text: "桃園市桃園區上海路 4房2廳2衛 更新日期2026年08月27日", images: [], offerPrice: "25000" };
    },
  };
  const result = await buildYungchingFeed({}, async () => {
    if (++launches > 1) throw new Error("429 Rate limit exceeded");
    return { async newPage() { return page; }, async close() {} };
  }, { sleep: async () => {} });
  assert.equal(result.candidate_count, 2);
  assert.equal(result.validated_count, 1);
  assert.equal(result.items[0].source_id, "2415719");
  assert.equal(result.crawl_complete, false);
  assert.ok(result.errors.some(error => error.includes("browser_rate_limited")));
});
