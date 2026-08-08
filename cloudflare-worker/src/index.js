const DEFAULT_OWNER = "FlySpacesky";
const DEFAULT_REPOSITORY = "taoyuan-rental-digest";
const DEFAULT_WORKFLOW = "rental-digest.yml";
const DEFAULT_BRANCH = "main";
const TAIPEI_OFFSET_MS = 8 * 60 * 60 * 1000;
const RUN_WINDOW_BEFORE_MS = 2 * 60 * 1000;
const RUN_WINDOW_AFTER_MS = 2 * 60 * 1000;

export const CRON_TO_SLOT = Object.freeze({
  "35-59 1 * * *": "09:30",
  "5-30 8 * * *": "16:00",
  "5-30 14 * * *": "22:00",
});

function githubHeaders(token) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "User-Agent": "taoyuan-rental-line-watchdog/1.0",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

export function deliverySlotFor(controller) {
  const time = CRON_TO_SLOT[controller.cron];
  if (!time) {
    throw new Error(`Unknown watchdog cron: ${controller.cron}`);
  }
  const localDate = new Date(
    Number(controller.scheduledTime) + TAIPEI_OFFSET_MS,
  )
    .toISOString()
    .slice(0, 10);
  const label = `${localDate}T${time}+08:00`;
  return {
    label,
    timestamp: Date.parse(label),
    time,
  };
}

export function assessWorkflowRuns(runs, slotTimestamp, nowTimestamp) {
  const relevant = runs.filter((run) => {
    if (!["schedule", "workflow_dispatch"].includes(run.event)) return false;
    const created = Date.parse(run.created_at);
    return (
      Number.isFinite(created) &&
      created >= slotTimestamp - RUN_WINDOW_BEFORE_MS &&
      created <= nowTimestamp + RUN_WINDOW_AFTER_MS
    );
  });

  if (relevant.some((run) => run.status === "completed" && run.conclusion === "success")) {
    return { action: "healthy", relevant };
  }
  if (relevant.some((run) => run.status !== "completed")) {
    return { action: "wait", relevant };
  }
  return {
    action: relevant.length ? "retry" : "missing",
    relevant,
  };
}

export async function handleScheduled(controller, env, fetchImpl = fetch) {
  const token = String(env.GITHUB_TOKEN || "").trim();
  if (!token) throw new Error("Missing Cloudflare Worker secret GITHUB_TOKEN");

  const owner = env.GITHUB_OWNER || DEFAULT_OWNER;
  const repository = env.GITHUB_REPOSITORY || DEFAULT_REPOSITORY;
  const workflow = env.GITHUB_WORKFLOW || DEFAULT_WORKFLOW;
  const branch = env.GITHUB_BRANCH || DEFAULT_BRANCH;
  const slot = deliverySlotFor(controller);
  const apiBase = `https://api.github.com/repos/${owner}/${repository}`;
  const headers = githubHeaders(token);
  const runsUrl =
    `${apiBase}/actions/workflows/${encodeURIComponent(workflow)}/runs` +
    `?branch=${encodeURIComponent(branch)}&per_page=50`;
  const runsResponse = await fetchImpl(runsUrl, { headers });
  if (!runsResponse.ok) {
    throw new Error(
      `GitHub workflow-runs query failed: ${runsResponse.status} ` +
        (await runsResponse.text()).slice(0, 300),
    );
  }

  const payload = await runsResponse.json();
  const assessment = assessWorkflowRuns(
    Array.isArray(payload.workflow_runs) ? payload.workflow_runs : [],
    slot.timestamp,
    Number(controller.scheduledTime),
  );
  if (["healthy", "wait"].includes(assessment.action)) {
    return {
      result: assessment.action,
      slot: slot.label,
      matchingRuns: assessment.relevant.length,
      dispatched: false,
    };
  }

  const dispatchResponse = await fetchImpl(
    `${apiBase}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`,
    {
      method: "POST",
      headers: {
        ...headers,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref: branch,
        inputs: { delivery_slot: slot.label },
      }),
    },
  );
  if (dispatchResponse.status !== 204) {
    throw new Error(
      `GitHub workflow dispatch failed: ${dispatchResponse.status} ` +
        (await dispatchResponse.text()).slice(0, 300),
    );
  }

  return {
    result: assessment.action,
    slot: slot.label,
    matchingRuns: assessment.relevant.length,
    dispatched: true,
  };
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(
      handleScheduled(controller, env)
        .then((result) => console.log(JSON.stringify(result)))
        .catch((error) => {
          console.error(error);
          throw error;
        }),
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      const githubTokenConfigured = Boolean(
        String(env.GITHUB_TOKEN || "").trim(),
      );
      return Response.json(
        {
          status: githubTokenConfigured ? "ok" : "degraded",
          service: "taoyuan-rental-line-watchdog",
          githubTokenConfigured,
          backupCronsUtc: Object.keys(CRON_TO_SLOT),
        },
        { status: githubTokenConfigured ? 200 : 503 },
      );
    }
    return new Response("Not found", { status: 404 });
  },
};
