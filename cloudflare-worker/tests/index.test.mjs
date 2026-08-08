import assert from "node:assert/strict";
import test from "node:test";

import {
  assessWorkflowRuns,
  deliverySlotFor,
  handleScheduled,
} from "../src/index.js";


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
