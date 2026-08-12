const DEFAULT_OWNER = "FlySpacesky";
const DEFAULT_REPOSITORY = "taoyuan-rental-digest";
const DEFAULT_WORKFLOW = "rental-digest.yml";
const DEFAULT_BRANCH = "main";
const TAIPEI_OFFSET_MS = 8 * 60 * 60 * 1000;
const RUN_WINDOW_BEFORE_MS = 2 * 60 * 1000;
const RUN_WINDOW_AFTER_MS = 2 * 60 * 1000;
const YUNGCHING_BASE =
  "https://rent.yungching.com.tw/list/" +
  "桃園市-中壢區,桃園市-平鎮區,桃園市-桃園區,桃園市-八德區_c/" +
  "整層住家_use/4-4_room";
const YUNGCHING_FEED_CACHE_SECONDS = 2 * 60 * 60;
const YUNGCHING_ALLOWED_DISTRICTS = ["桃園區", "中壢區", "平鎮區", "八德區"];

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

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function money(value) {
  const digits = String(value || "").replace(/[^0-9]/g, "");
  return digits ? Number(digits) : 0;
}

export function normalizeYungchingWorkerItem(row, newIds = new Set()) {
  const sourceId = String(row.source_id || "").trim();
  const address = String(row.address || "").trim();
  const layout = String(row.layout || "").trim();
  const updated = String(row.updated || "").trim();
  const images = unique(
    (Array.isArray(row.images) ? row.images : []).filter((value) => {
      try {
        return new URL(String(value)).hostname === "yccdn.yungching.com.tw";
      } catch {
        return false;
      }
    }),
  );
  if (!/^\d+$/.test(sourceId)) return null;
  if (!YUNGCHING_ALLOWED_DISTRICTS.some((district) => address.includes(district))) {
    return null;
  }
  const roomMatch = layout.match(/(\d+)房/);
  if (!roomMatch || Number(roomMatch[1]) < 4 || !updated || !money(row.rent)) {
    return null;
  }
  return {
    source_id: sourceId,
    url: `https://rent.yungching.com.tw/house/${sourceId}`,
    title: String(row.title || "").trim(),
    address,
    building_type: String(row.building_type || "").trim(),
    floor: String(row.floor || "").replace(/\s+/g, "").trim(),
    layout,
    size: String(row.size || "").trim(),
    equipment: String(row.equipment || "").trim(),
    rent: money(row.rent),
    updated,
    publisher: String(row.publisher || "永慶房屋").trim(),
    images,
    summary: String(row.summary || "").trim().slice(0, 900),
    raw_text: String(row.raw_text || "").trim().slice(0, 12000),
    filter_tags: newIds.has(sourceId) ? ["new"] : [],
  };
}

async function readYungchingListPage(page, category, pageNo) {
  const suffix = category === "new" ? "/new_filter" : "";
  const url = `${YUNGCHING_BASE}${suffix}?od=80&pg=${pageNo}`;
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30_000 });
  try {
    await page.waitForSelector('a[href*="/house/"]', { timeout: 12_000 });
  } catch {
    return [];
  }
  return page.$$eval('a[href*="/house/"]', (anchors) =>
    anchors.map((anchor) => {
      const href = anchor.href || "";
      const sourceId = href.match(/\/house\/(\d+)/)?.[1] || "";
      const read = (selector) =>
        (anchor.querySelector(selector)?.textContent || "").replace(/\s+/g, " ").trim();
      const image = anchor.querySelector("img");
      const imageUrl =
        image?.currentSrc || image?.getAttribute("src") || image?.getAttribute("data-src") || "";
      return {
        source_id: sourceId,
        title: read(".caseName"),
        address: read(".address"),
        layout: read(".room"),
        size: read(".regArea"),
        floor: read(".floor"),
        rent: read(".price"),
        image: imageUrl,
      };
    }).filter((row) => row.source_id && row.title && row.address),
  );
}

async function readYungchingDetail(page, candidate) {
  await page.goto(`https://rent.yungching.com.tw/house/${candidate.source_id}`, {
    waitUntil: "domcontentloaded",
    timeout: 30_000,
  });
  try {
    await page.waitForSelector("h1", { timeout: 12_000 });
  } catch {
    return null;
  }
  const expanded = await page.evaluate(() => {
    const target = [...document.querySelectorAll("button, a")].find((node) =>
      (node.textContent || "").includes("看詳細基本資訊"),
    );
    if (!target) return false;
    target.click();
    return true;
  });
  if (expanded) await new Promise((resolve) => setTimeout(resolve, 500));
  const detail = await page.evaluate(() => {
    const compact = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const text = compact(document.body?.innerText || "");
    const imageValues = [];
    // 只取本物件相簿；排除地圖與「周邊熱門待租房屋」的其他物件照片。
    document.querySelectorAll(".swiper-slide.gtmPushEvent img").forEach((image) => {
      imageValues.push(
        image.currentSrc,
        image.getAttribute("src"),
        image.getAttribute("data-src"),
        image.getAttribute("data-original"),
      );
      const srcset = image.getAttribute("srcset") || image.getAttribute("data-srcset") || "";
      srcset.split(",").forEach((part) => imageValues.push(part.trim().split(/\s+/)[0]));
    });
    let offerPrice = "";
    document.querySelectorAll('script[type*="ld+json"]').forEach((node) => {
      try {
        const parsed = JSON.parse(node.textContent || "null");
        const stack = Array.isArray(parsed) ? [...parsed] : [parsed];
        while (stack.length) {
          const value = stack.shift();
          if (!value || typeof value !== "object") continue;
          const images = value.image;
          if (typeof images === "string") imageValues.push(images);
          if (Array.isArray(images)) {
            images.forEach((image) =>
              imageValues.push(typeof image === "string" ? image : image?.url),
            );
          }
          if (!offerPrice && value.offers?.price) offerPrice = String(value.offers.price);
          if (Array.isArray(value["@graph"])) stack.push(...value["@graph"]);
        }
      } catch {
        // Ignore invalid third-party JSON-LD blocks.
      }
    });
    return {
      text,
      title: compact(document.querySelector("h1")?.textContent),
      publisher: compact(document.querySelector('a[href*="shop.yungching.com.tw"]')?.textContent),
      summary: compact(
        document.querySelector('meta[name="description"]')?.getAttribute("content") || "",
      ),
      images: imageValues.filter(Boolean),
      offerPrice,
    };
  });

  const text = detail.text || "";
  const match = (pattern, fallback = "") => text.match(pattern)?.[1] || fallback;
  const equipment = [
    "有車位", "近捷運", "可開伙", "有陽台", "有電梯", "可養寵", "冷氣", "冰箱", "洗衣機",
  ].filter((value) => text.includes(value)).join("、");
  const buildingType = ["電梯大樓", "華廈", "公寓", "透天厝", "別墅", "樓中樓"]
    .find((value) => text.includes(value)) || "";
  return {
    ...candidate,
    title: detail.title || candidate.title,
    address: match(/桃園市(桃園區|中壢區|平鎮區|八德區)[^\s｜|]{0,80}/, candidate.address)
      ? (text.match(/桃園市(?:桃園區|中壢區|平鎮區|八德區)[^\s｜|]{0,80}/)?.[0] || candidate.address)
      : candidate.address,
    layout: match(/(\d+房(?:\(室\))?\d*廳\d*衛)/, candidate.layout),
    size: `${match(/坪數\s*(\d+(?:\.\d+)?)\s*坪/, String(candidate.size || "").replace("坪", ""))}坪`,
    floor: match(/((?:B?\d+(?:~|～|-)\d+|\d+)\s*\/\s*\d+樓)/i, candidate.floor),
    updated: match(/更新日期\s*(\d{4}年\d{1,2}月\d{1,2}日)/),
    building_type: buildingType,
    equipment,
    rent: detail.offerPrice || candidate.rent,
    publisher: detail.publisher || "永慶房屋",
    images: unique([candidate.image, ...detail.images]),
    summary: detail.summary,
    raw_text: text,
  };
}

export async function buildYungchingFeed(browserBinding, launchImpl) {
  if (!browserBinding) throw new Error("Missing Cloudflare Browser Run binding");
  const launch = launchImpl || (await import("@cloudflare/puppeteer")).default.launch;
  const browser = await launch(browserBinding);
  const page = await browser.newPage();
  const allCandidates = new Map();
  const newIds = new Set();
  let pagesRead = 0;
  const errors = [];
  try {
    await page.setUserAgent(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    );
    for (const category of ["all", "new"]) {
      for (let pageNo = 1; pageNo <= 5; pageNo += 1) {
        let rows = [];
        try {
          rows = await readYungchingListPage(page, category, pageNo);
        } catch (error) {
          errors.push(`${category}:${pageNo}:${String(error).slice(0, 160)}`);
          break;
        }
        pagesRead += 1;
        if (!rows.length) break;
        let added = 0;
        rows.forEach((row) => {
          if (category === "new") newIds.add(row.source_id);
          if (!allCandidates.has(row.source_id)) {
            allCandidates.set(row.source_id, row);
            added += 1;
          }
        });
        if (!added || rows.length < 20) break;
      }
    }

    const items = [];
    for (const candidate of allCandidates.values()) {
      try {
        const detail = await readYungchingDetail(page, candidate);
        const normalized = detail && normalizeYungchingWorkerItem(detail, newIds);
        if (normalized) items.push(normalized);
      } catch (error) {
        errors.push(`detail:${candidate.source_id}:${String(error).slice(0, 160)}`);
      }
    }
    return {
      generated_at: new Date().toISOString(),
      source: "永慶房屋公開搜尋與詳細頁",
      candidate_count: allCandidates.size,
      validated_count: items.length,
      new_category_count: newIds.size,
      pages_read: pagesRead,
      errors,
      items,
    };
  } finally {
    await browser.close();
  }
}

async function yungchingFeedResponse(request, env, ctx) {
  const cache = globalThis.caches?.default;
  const cacheKey = new Request(`${new URL(request.url).origin}/__cache/yungching-feed-v1`);
  if (cache) {
    const cached = await cache.match(cacheKey);
    if (cached) {
      const response = new Response(cached.body, cached);
      response.headers.set("X-Rental-Feed-Cache", "hit");
      return response;
    }
  }
  const payload = await buildYungchingFeed(env.BROWSER);
  const response = Response.json(payload, {
    headers: {
      "Cache-Control": `public, max-age=${YUNGCHING_FEED_CACHE_SECONDS}`,
      "X-Rental-Feed-Cache": "miss",
    },
  });
  if (cache) {
    const pending = cache.put(cacheKey, response.clone());
    if (ctx?.waitUntil) ctx.waitUntil(pending);
    else await pending;
  }
  return response;
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

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/yungching-feed") {
      try {
        return await yungchingFeedResponse(request, env, ctx);
      } catch (error) {
        console.error(error);
        return Response.json(
          { status: "error", message: String(error).slice(0, 300) },
          { status: 502 },
        );
      }
    }
    if (request.method === "GET" && url.pathname === "/health") {
      const githubTokenConfigured = Boolean(
        String(env.GITHUB_TOKEN || "").trim(),
      );
      return Response.json(
        {
          status: githubTokenConfigured ? "ok" : "degraded",
          service: "taoyuan-rental-line-watchdog",
          githubTokenConfigured,
          browserRunConfigured: Boolean(env.BROWSER),
          backupCronsUtc: Object.keys(CRON_TO_SLOT),
        },
        { status: githubTokenConfigured ? 200 : 503 },
      );
    }
    return new Response("Not found", { status: 404 });
  },
};
