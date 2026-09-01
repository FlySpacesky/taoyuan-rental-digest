import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { browserDetailProbe, createPreviewWorker, PREVIEW_NAME, quickActionDetailProbe, SAMPLE_ID } from "../src/preview.js";

const token = "a".repeat(64);
const env = () => ({ PREVIEW_PROBE_TOKEN: token, PREVIEW_EXPIRES_AT_MS: String(Date.now() + 60_000) });
const request = (path, secret = token) => new Request(`https://preview.example${path}`, {
  method: "POST", headers: { Authorization: `Bearer ${secret}` },
});

test("preview config cannot bind production resources or scheduled handlers", () => {
  const cfg = JSON.parse(fs.readFileSync(new URL("../wrangler.preview.jsonc", import.meta.url)));
  assert.equal(cfg.name, PREVIEW_NAME);
  assert.deepEqual(cfg.triggers.crons, []);
  assert.equal(cfg.kv_namespaces, undefined);
  assert.equal(cfg.services, undefined);
  assert.equal(cfg.vars.GITHUB_TOKEN, undefined);
  assert.equal(cfg.vars.GITHUB_WORKFLOW, undefined);
  assert.equal(createPreviewWorker().scheduled, undefined);
  const workflow = fs.readFileSync(new URL("../../.github/workflows/cloudflare-watchdog.yml", import.meta.url), "utf8").replaceAll("\r\n", "\n");
  const previewJob = workflow.split("  preview:\n")[1].split("  deploy:\n")[0];
  assert.doesNotMatch(previewJob, /vars\.RENTAL_CPU_PREVIEW_ENABLED == 'true'/);
  assert.match(previewJob, /pull_request\.number == 44/);
  assert.match(previewJob, /head\.repo\.full_name == github\.repository/);
  assert.match(previewJob, /--config wrangler\.preview\.jsonc/);
  assert.match(previewJob, /--secrets-file/);
  assert.doesNotMatch(previewJob, /secret put/);
  assert.doesNotMatch(previewJob, /FB_INBOX|LINE_CHANNEL|secrets\.GITHUB_TOKEN/);
  assert.match(workflow.split("  deploy:\n")[1], /github\.ref == 'refs\/heads\/main'/);
});

test("preview only exposes health and expiring authenticated probe endpoints", async () => {
  const worker = createPreviewWorker({ fetchImpl: () => assert.fail("must not fetch") });
  const health = await worker.fetch(new Request("https://preview.example/health"), {});
  assert.equal((await health.json()).isolated, true);
  assert.equal((await worker.fetch(request("/probe-fetch", "bad"), env())).status, 401);
  assert.equal((await worker.fetch(request("/probe-fetch", "bad"), env())).headers.get("X-Preview-Probe-Started"), "false");
  assert.equal((await worker.fetch(request("/probe-fetch"), {})).status, 401);
  assert.equal((await worker.fetch(request("/probe-fetch"), { ...env(), PREVIEW_EXPIRES_AT_MS: "0" })).status, 410);
  assert.equal((await worker.fetch(request("/ready", "bad"), env())).status, 401);
  assert.equal((await worker.fetch(request("/ready"), { ...env(), PREVIEW_EXPIRES_AT_MS: "0" })).status, 410);
  assert.deepEqual(await (await worker.fetch(request("/ready"), env())).json(), { ready: true });
  assert.equal((await worker.fetch(new Request("https://preview.example/ready"), env())).status, 404);
  for (const path of ["/facebook-inbox", "/yungching-feed", "/dispatch"]) {
    assert.equal((await worker.fetch(request(path), env())).status, 404);
  }
});

test("preview streams only the fixed live public source, preserves upstream failure", async () => {
  for (const status of [200, 403]) {
    const worker = createPreviewWorker({ fetchImpl: async (url, options) => {
      assert.equal(url, `https://rent.yungching.com.tw/house/${SAMPLE_ID}`);
      assert.equal(options.redirect, "manual");
      assert.ok(options.signal);
      return new Response("public source html", { status });
    } });
    const result = await worker.fetch(request("/probe-fetch"), env());
    assert.equal(result.status, status);
    assert.equal(result.headers.get("Cache-Control"), "no-store");
    assert.ok(result.headers.get("X-Preview-Observed-At"));
    assert.equal(await result.text(), "public source html");
  }
});

test("quick action renders only the fixed detail and streams outside Worker parsing", async () => {
  const calls = [];
  const binding = {
    quickAction: async (...args) => {
      calls.push(args);
      return new Response("<h1>rendered</h1>", {
        status: 200, headers: { "Content-Type": "text/html" },
      });
    },
  };
  const result = await quickActionDetailProbe(binding);
  assert.equal(result.status, 200);
  assert.equal(result.headers.get("X-Preview-Mode"), "browser-quick-action-stream");
  assert.equal(await result.text(), "<h1>rendered</h1>");
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "content");
  assert.equal(calls[0][1].url, `https://rent.yungching.com.tw/house/${SAMPLE_ID}`);
  assert.equal(calls[0][1].setJavaScriptEnabled, true);
  assert.equal(calls[0][1].waitForTimeout, 6000);
});

test("browser preview never acquires or closes active production sessions", async () => {
  await assert.rejects(browserDetailProbe({}, {
    sessions: async () => [{ sessionId: "production-session" }],
    launch: async () => assert.fail("must not acquire browser"),
  }), /shared_browser_sessions_active/);
});

test("preview browser is closed when page setup fails, without retrying", async () => {
  let launched = 0;
  let closed = 0;
  await assert.rejects(browserDetailProbe({}, {
    sessions: async () => [],
    launch: async () => {
      launched++;
      return { newPage: async () => { throw new Error("setup failed"); }, close: async () => closed++ };
    },
  }), /setup failed/);
  assert.equal(launched, 1);
  assert.equal(closed, 1);
});
