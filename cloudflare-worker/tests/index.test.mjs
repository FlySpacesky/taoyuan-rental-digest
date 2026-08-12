import assert from "node:assert/strict";
import test from "node:test";

import {
  assessWorkflowRuns,
  deliverySlotFor,
  handleScheduled,
  normalizeYungchingWorkerItem,
} from "../src/index.js";
import worker from "../src/index.js";


const scheduledTime = Date.parse("2026-08-09T01:35:00Z");
const controller = { cron: "35-59 1 * * *", scheduledTime };
const env = { GITHUB_TOKEN: "test-token" };


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
    { GITHUB_TOKEN: "configured" },
  );
  const degraded = await worker.fetch(
    new Request("https://watchdog.example/health"),
    {},
  );

  const healthyBody = await healthy.json();
  assert.equal(healthy.status, 200);
  assert.equal(healthyBody.githubTokenConfigured, true);
  assert.equal(healthyBody.browserRunConfigured, false);
  assert.equal(degraded.status, 503);
  assert.equal((await degraded.json()).githubTokenConfigured, false);
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


test("Yungching feed fails honestly when the Browser Run binding is absent", async () => {
  const response = await worker.fetch(
    new Request("https://watchdog.example/yungching-feed"),
    {},
  );
  assert.equal(response.status, 502);
  assert.equal((await response.json()).status, "error");
});
