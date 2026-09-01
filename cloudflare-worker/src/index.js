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
const YUNGCHING_IN_FLIGHT = new Map();
const YUNGCHING_ALLOWED_DISTRICTS = ["桃園區", "中壢區", "平鎮區", "八德區"];
const FACEBOOK_INBOX_PREFIX = "facebook:";
const FACEBOOK_INBOX_TTL_SECONDS = 30 * 24 * 60 * 60;
const FACEBOOK_INBOX_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const FACEBOOK_INBOX_MAX_BODY_BYTES = 64 * 1024;
const FACEBOOK_INBOX_MAX_FEED_ROWS = 200;
const FACEBOOK_SUBMISSION_ORIGIN =
  "https://flyspacesky.github.io";
const FACEBOOK_ALLOWED_FIELDS = new Set([
  "url",
  "post_text",
  "published_at",
  "publisher",
  "title",
  "district",
  "address",
  "house_type",
  "building_type",
  "floor",
  "layout",
  "size",
  "equipment",
  "rent",
  "old_rent",
  "total_cost",
  "image",
  "summary",
  "republish_authorized",
  "no_facebook_credentials",
]);
const FACEBOOK_FORBIDDEN_FIELD =
  /(?:password|passwd|cookie|session|access.?token|authorization|credential|帳號|密碼|權杖)/i;

export const CRON_TO_SLOT = Object.freeze({
  "35-59 1 * * *": "09:30",
  "5-30 8 * * *": "16:00",
  "5-30 14 * * *": "22:00",
});

function facebookCorsHeaders(request) {
  const origin = request.headers.get("Origin") || "";
  return origin === FACEBOOK_SUBMISSION_ORIGIN
    ? {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Max-Age": "86400",
        Vary: "Origin",
      }
    : {};
}

function facebookJson(request, payload, status = 200) {
  return Response.json(payload, {
    status,
    headers: {
      ...facebookCorsHeaders(request),
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

async function tokenMatches(request, expectedToken) {
  const expected = String(expectedToken || "").trim();
  const supplied = (request.headers.get("Authorization") || "")
    .replace(/^Bearer\s+/i, "")
    .trim();
  if (!expected || !supplied) return false;
  const encode = (value) => new TextEncoder().encode(value);
  const [expectedDigest, suppliedDigest] = await Promise.all([
    crypto.subtle.digest("SHA-256", encode(expected)),
    crypto.subtle.digest("SHA-256", encode(supplied)),
  ]);
  const left = new Uint8Array(expectedDigest);
  const right = new Uint8Array(suppliedDigest);
  if (left.length !== right.length) return false;
  let mismatch = 0;
  for (let index = 0; index < left.length; index += 1) {
    mismatch |= left[index] ^ right[index];
  }
  return mismatch === 0;
}

function normalizeFacebookPostUrl(value) {
  try {
    const url = new URL(String(value || "").trim());
    if (!["facebook.com", "www.facebook.com", "m.facebook.com"].includes(url.hostname)) {
      return "";
    }
    const parts = url.pathname.split("/").filter(Boolean);
    if (
      parts.length < 4 ||
      parts[0] !== "groups" ||
      !/^[A-Za-z0-9._-]+$/.test(parts[1]) ||
      !["posts", "permalink"].includes(parts[2]) ||
      !/^[A-Za-z0-9._-]+$/.test(parts[3])
    ) {
      return "";
    }
    return `https://www.facebook.com/groups/${parts[1]}/${parts[2]}/${parts[3]}/`;
  } catch {
    return "";
  }
}

function hasForbiddenFacebookField(value) {
  if (!value || typeof value !== "object") return false;
  return Object.entries(value).some(
    ([key, child]) =>
      (key !== "no_facebook_credentials" && FACEBOOK_FORBIDDEN_FIELD.test(key)) ||
      (child && typeof child === "object" && hasForbiddenFacebookField(child)),
  );
}

function cleanFacebookValue(value, maxLength) {
  return String(value ?? "")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "")
    .trim()
    .slice(0, maxLength);
}

async function normalizeFacebookInboxSubmission(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return { error: "投稿資料必須是 JSON 物件。" };
  }
  if (hasForbiddenFacebookField(payload)) {
    return { error: "收件匣禁止接收帳密、Cookie、Session 或 Access Token。" };
  }
  const unknownFields = Object.keys(payload).filter(
    (key) => !FACEBOOK_ALLOWED_FIELDS.has(key),
  );
  if (unknownFields.length) {
    return { error: `投稿包含不支援的欄位：${unknownFields.slice(0, 5).join("、")}` };
  }
  if (payload.republish_authorized !== true) {
    return { error: "必須確認已取得貼文作者或社團管理員的電子報再公開授權。" };
  }
  if (payload.no_facebook_credentials !== true) {
    return { error: "必須確認投稿中不含 Facebook 帳密、Cookie 或 Session。" };
  }
  const url = normalizeFacebookPostUrl(payload.url);
  if (!url) {
    return { error: "請使用 /groups/{社團}/posts/{貼文}/ 或 /permalink/{貼文}/ 永久網址。" };
  }
  const postText = cleanFacebookValue(payload.post_text, 20_000);
  if (!postText) return { error: "請貼上完整原始貼文文字。" };

  const publishedAt = new Date(String(payload.published_at || ""));
  const now = Date.now();
  if (
    !Number.isFinite(publishedAt.getTime()) ||
    publishedAt.getTime() < now - FACEBOOK_INBOX_MAX_AGE_MS ||
    publishedAt.getTime() > now + 10 * 60 * 1000
  ) {
    return { error: "原始貼文時間必須是最近 7 天內的有效時間。" };
  }

  const row = {
    url,
    post_text: postText,
    published_at: publishedAt.toISOString(),
    submitted_at: new Date(now).toISOString(),
    republish_authorized: true,
    _submission_source: "Cloudflare 私人收件匣（人工授權）",
  };
  const fieldLimits = {
    publisher: 100,
    title: 200,
    district: 30,
    address: 160,
    house_type: 60,
    building_type: 60,
    floor: 60,
    layout: 60,
    size: 40,
    equipment: 500,
    rent: 30,
    old_rent: 30,
    total_cost: 30,
    image: 2_000,
    summary: 1_000,
  };
  Object.entries(fieldLimits).forEach(([field, limit]) => {
    const value = cleanFacebookValue(payload[field], limit);
    if (value) row[field] = value;
  });
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(url));
  const id = [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 32);
  return { id, row };
}

export async function dispatchFacebookRefresh(env, fetchImpl = fetch) {
  const token = String(env.GITHUB_TOKEN || "").trim();
  if (!token) return false;
  const owner = env.GITHUB_OWNER || DEFAULT_OWNER;
  const repository = env.GITHUB_REPOSITORY || DEFAULT_REPOSITORY;
  const workflow = env.GITHUB_WORKFLOW || DEFAULT_WORKFLOW;
  const branch = env.GITHUB_BRANCH || DEFAULT_BRANCH;
  const response = await fetchImpl(
    `https://api.github.com/repos/${owner}/${repository}/actions/workflows/` +
      `${encodeURIComponent(workflow)}/dispatches`,
    {
      method: "POST",
      headers: {
        ...githubHeaders(token),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref: branch,
        inputs: { skip_line: "true" },
      }),
    },
  );
  if (response.status !== 204) {
    throw new Error(
      `Facebook refresh dispatch failed: ${response.status} ` +
        (await response.text()).slice(0, 200),
    );
  }
  return true;
}

export async function submitFacebookInbox(request, env, fetchImpl = fetch) {
  if (!env.FB_INBOX || !String(env.FB_INBOX_WRITE_TOKEN || "").trim()) {
    return facebookJson(request, { status: "error", message: "私人收件匣尚未完成設定。" }, 503);
  }
  if (!(await tokenMatches(request, env.FB_INBOX_WRITE_TOKEN))) {
    return facebookJson(request, { status: "error", message: "投稿權杖無效。" }, 401);
  }
  const contentLength = Number(request.headers.get("Content-Length") || 0);
  if (contentLength > FACEBOOK_INBOX_MAX_BODY_BYTES) {
    return facebookJson(request, { status: "error", message: "投稿內容超過 64KB。" }, 413);
  }
  let payload;
  try {
    const raw = await request.text();
    if (!raw || new TextEncoder().encode(raw).length > FACEBOOK_INBOX_MAX_BODY_BYTES) {
      return facebookJson(request, { status: "error", message: "投稿內容為空或超過 64KB。" }, 413);
    }
    payload = JSON.parse(raw);
  } catch {
    return facebookJson(request, { status: "error", message: "投稿內容不是有效 JSON。" }, 400);
  }
  const normalized = await normalizeFacebookInboxSubmission(payload);
  if (normalized.error) {
    return facebookJson(request, { status: "error", message: normalized.error }, 422);
  }
  await env.FB_INBOX.put(
    `${FACEBOOK_INBOX_PREFIX}${normalized.id}`,
    JSON.stringify(normalized.row),
    { expirationTtl: FACEBOOK_INBOX_TTL_SECONDS },
  );
  let refreshDispatched = false;
  let refreshWarning = "";
  if (String(env.FACEBOOK_AUTO_REFRESH || "").toLowerCase() === "true") {
    try {
      refreshDispatched = await dispatchFacebookRefresh(env, fetchImpl);
    } catch (error) {
      refreshWarning = "房源已安全收件，但即時更新暫時無法啟動；下個排程仍會自動讀取。";
      console.error(String(error));
    }
  }
  return facebookJson(request, {
    status: "accepted",
    id: normalized.id,
    refresh_dispatched: refreshDispatched,
    message: refreshDispatched
      ? "已安全收件並開始更新電子報；仍會套用地區、房數、日期與包租代管同行排除規則。"
      : refreshWarning || "已安全收件；電子報仍會套用地區、房數、日期與包租代管同行排除規則。",
  }, 201);
}

async function readFacebookInbox(request, env) {
  if (!env.FB_INBOX || !String(env.FB_INBOX_READ_TOKEN || "").trim()) {
    return facebookJson(request, { status: "error", message: "私人 feed 尚未完成設定。" }, 503);
  }
  if (!(await tokenMatches(request, env.FB_INBOX_READ_TOKEN))) {
    return facebookJson(request, { status: "error", message: "讀取權杖無效。" }, 401);
  }
  const posts = [];
  let cursor;
  do {
    const listed = await env.FB_INBOX.list({
      prefix: FACEBOOK_INBOX_PREFIX,
      limit: Math.min(100, FACEBOOK_INBOX_MAX_FEED_ROWS - posts.length),
      ...(cursor ? { cursor } : {}),
    });
    for (const key of listed.keys || []) {
      if (posts.length >= FACEBOOK_INBOX_MAX_FEED_ROWS) break;
      const row = await env.FB_INBOX.get(key.name, "json");
      if (row && row.republish_authorized === true) posts.push(row);
    }
    cursor = listed.list_complete === false ? listed.cursor : undefined;
  } while (cursor && posts.length < FACEBOOK_INBOX_MAX_FEED_ROWS);
  posts.sort((left, right) =>
    String(right.published_at || "").localeCompare(String(left.published_at || "")),
  );
  return facebookJson(request, { posts });
}

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
  const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30_000 });
  if (response && response.status() >= 400) throw new Error(`upstream_list_http_${response.status()}`);
  try {
    await page.waitForSelector('a[href*="/house/"]', { timeout: 12_000 });
  } catch {
    const text = await page.evaluate(() => document.body?.innerText || "");
    if (/(?:查無|沒有找到|無符合|共\s*0\s*筆)/.test(text)) return [];
    throw new Error("upstream_list_unverified: no listing cards or explicit empty result");
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

export async function readYungchingDetail(page, candidate) {
  const response = await page.goto(`https://rent.yungching.com.tw/house/${candidate.source_id}`, {
    waitUntil: "domcontentloaded",
    timeout: 30_000,
  });
  if (response && [404, 410].includes(response.status())) return null;
  if (response && response.status() >= 400) throw new Error(`upstream_detail_http_${response.status()}`);
  try {
    await page.waitForSelector("h1", { timeout: 12_000 });
  } catch {
    throw new Error("upstream_detail_unverified: missing listing heading");
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
    document.querySelectorAll(".swiper-slide.gtmPushEvent img, .yc-ng-album-v2-carousel__main-img, .yc-ng-album-v2-carousel__thumb img").forEach((image) => {
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
    updated: match(/更新日期\s*(\d{4}[年\/-]\d{1,2}[月\/-]\d{1,2}日?)/),
    building_type: buildingType,
    equipment,
    rent: detail.offerPrice || candidate.rent,
    publisher: detail.publisher || "永慶房屋",
    images: unique([candidate.image, ...detail.images]),
    summary: detail.summary,
    raw_text: text,
    validated_at: new Date().toISOString(),
  };
}

export function yungchingRenderTarget(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("invalid_render_request");
  }
  if (payload.kind === "list") {
    const category = String(payload.category || "");
    const page = Number(payload.page);
    if (!["all", "new"].includes(category) || !Number.isInteger(page) || page < 1 || page > 5) {
      throw new Error("invalid_render_list_target");
    }
    const suffix = category === "new" ? "/new_filter" : "";
    return {
      kind: "list",
      category,
      page,
      url: `${YUNGCHING_BASE}${suffix}?od=80&pg=${page}`,
    };
  }
  if (payload.kind === "detail") {
    const sourceId = String(payload.source_id || "").trim();
    if (!/^\d{5,12}$/.test(sourceId)) throw new Error("invalid_render_detail_target");
    return {
      kind: "detail",
      source_id: sourceId,
      url: `https://rent.yungching.com.tw/house/${sourceId}`,
    };
  }
  throw new Error("invalid_render_kind");
}

export async function renderYungchingQuickAction(binding, target) {
  if (!binding?.quickAction) throw new Error("browser_quick_action_not_configured");
  const upstream = await binding.quickAction("content", {
    url: target.url,
    setJavaScriptEnabled: true,
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    waitForTimeout: 6_000,
  });
  const browserMs = upstream.headers.get("X-Browser-Ms-Used");
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": upstream.headers.get("Content-Type") || "application/json; charset=utf-8",
      "X-Rental-Render-Kind": target.kind,
      ...(target.source_id ? { "X-Rental-Source-Id": target.source_id } : {}),
      ...(target.category ? { "X-Rental-Category": target.category } : {}),
      ...(target.page ? { "X-Rental-Page": String(target.page) } : {}),
      ...(browserMs ? { "X-Browser-Ms-Used": browserMs } : {}),
    },
  });
}

export async function yungchingRenderResponse(request, env) {
  if (!(await tokenMatches(request, env.FB_INBOX_READ_TOKEN))) {
    return Response.json({ error: "unauthorized" }, {
      status: 401,
      headers: { "Cache-Control": "no-store" },
    });
  }
  const contentLength = Number(request.headers.get("Content-Length") || 0);
  if (contentLength > 1_024) {
    return Response.json({ error: "request_too_large" }, { status: 413 });
  }
  let target;
  try {
    target = yungchingRenderTarget(await request.json());
  } catch (error) {
    return Response.json({ error: String(error?.message || error).slice(0, 100) }, {
      status: 400,
      headers: { "Cache-Control": "no-store" },
    });
  }
  try {
    return await renderYungchingQuickAction(env.BROWSER, target);
  } catch (error) {
    const code = browserFailureCode(error);
    const status = code === "browser_rate_limited" || code === "browser_daily_quota" ? 429 : 502;
    return Response.json({ error: code, detail: String(error?.message || error).slice(0, 180) }, {
      status,
      headers: { "Cache-Control": "no-store" },
    });
  }
}

export function browserFailureCode(error) {
  const message = String(error?.message || error);
  if (/time limit exceeded for today/i.test(message)) return "browser_daily_quota";
  if (/429|rate limit|too many requests/i.test(message)) return "browser_rate_limited";
  if (/Missing Cloudflare/i.test(message)) return "browser_not_configured";
  if (/upstream_/.test(message)) return "upstream_unverified";
  return "browser_failure";
}

export async function launchYungchingBrowser(binding, api, sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      return await api.launch(binding);
    } catch (error) {
      // Daily quotas cannot be solved by rapid retries. Short-window limits can.
      if (browserFailureCode(error) !== "browser_rate_limited" || attempt === 2) throw error;
      const retryAfter = Number(error?.headers?.get?.("Retry-After") || 0);
      let limits = null;
      try { limits = await api.limits?.(binding); } catch { /* use conservative backoff */ }
      const delay = Math.max(21_000, retryAfter * 1000,
        Number(limits?.timeUntilNextAllowedBrowserAcquisition || 0) + 1000);
      if (delay > 60_000) throw error;
      await sleep(delay);
    }
  }
}

export async function buildYungchingFeed(browserBinding, launchImpl, options = {}) {
  if (!browserBinding) throw new Error("Missing Cloudflare Browser Run binding");
  const api = launchImpl ? { launch: launchImpl, limits: options.limits } : (await import("@cloudflare/puppeteer")).default;
  const allCandidates = new Map();
  const newIds = new Set();
  let pagesRead = 0;
  const errors = [];
  let rejectedCount = 0;
  let session = null;

  const openSession = async (details = false) => {
    const browser = await launchYungchingBrowser(browserBinding, api, options.sleep);
    try {
      const page = await browser.newPage();
      if (details) await page.setJavaScriptEnabled(false);
      await page.setUserAgent(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
      );
      return { browser, page };
    } catch (error) {
      await browser.close().catch(() => {});
      throw error;
    }
  };

  const closeSession = async () => {
    if (session) await session.browser.close().catch(() => {});
    session = null;
  };
  const items = [];
  try {
    session = await openSession();
    // Tabs in one active browser do not consume new-browser acquisition quota.
    // Reopen only after a real failure, not once per six successful details.
    for (const category of ["all", "new"]) {
      for (let pageNo = 1; pageNo <= 5; pageNo += 1) {
        let rows = [];
        try {
          rows = await readYungchingListPage(session.page, category, pageNo);
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
    const orderedCandidates = [...allCandidates.values()].sort((first, second) =>
      Number(newIds.has(second.source_id)) - Number(newIds.has(first.source_id)),
    );
    // Detail pages expose update date and album in server-rendered HTML.
    // Avoid hydrating maps/recommendations and collapsing the date before reading.
    await session.page.setJavaScriptEnabled(false);
    for (const candidate of orderedCandidates) {
      let completed = false;
      for (let attempt = 1; attempt <= 2 && !completed; attempt += 1) {
        try {
          if (!session) session = await openSession(true);
          const detail = await readYungchingDetail(session.page, candidate);
          const normalized = detail && normalizeYungchingWorkerItem(detail, newIds);
          if (normalized) items.push({ ...normalized, validated_at: detail.validated_at });
          else if (detail) rejectedCount += 1;
          completed = true;
        } catch (error) {
          errors.push(
            `detail:${candidate.source_id}:attempt-${attempt}:${String(error).slice(0, 160)}`,
          );
          await closeSession();
          if (["browser_daily_quota", "browser_rate_limited"].includes(browserFailureCode(error))) {
            // Preserve already validated rows; do not restart the whole crawl.
            throw error;
          }
        }
      }
    }
  } catch (error) {
    errors.push(`${browserFailureCode(error)}:${String(error?.message || error).slice(0, 180)}`);
  } finally {
    await closeSession();
  }

  return {
    generated_at: new Date().toISOString(),
    source: "永慶房屋公開搜尋與詳細頁",
    candidate_count: allCandidates.size,
    validated_count: items.length,
    rejected_count: rejectedCount,
    new_category_count: newIds.size,
    pages_read: pagesRead,
    crawl_complete: errors.length === 0,
    errors,
    items,
  };
}

export async function yungchingFeedResponse(request, env, ctx, build = buildYungchingFeed) {
  const url = new URL(request.url);
  const validationId = url.searchParams.get("validation_id") || "public";
  if (!/^[A-Za-z0-9_-]{1,100}$/.test(validationId)) {
    return Response.json({ error: "invalid validation_id" }, { status: 400 });
  }
  const cache = globalThis.caches?.default;
  // An explicit validation round never receives an earlier round's cached feed.
  const cacheKey = new Request(`${url.origin}/__cache/yungching-feed-v3/${validationId}`);
  if (cache) {
    const cached = await cache.match(cacheKey);
    if (cached) {
      const response = new Response(cached.body, cached);
      response.headers.set("X-Rental-Feed-Cache", "hit");
      return response;
    }
  }
  let pending = YUNGCHING_IN_FLIGHT.get(cacheKey.url);
  if (!pending) {
    pending = build(env.BROWSER);
    YUNGCHING_IN_FLIGHT.set(cacheKey.url, pending);
  }
  let payload;
  try {
    payload = await pending;
  } catch (error) {
    return Response.json({
      status: "degraded",
      healthy: false,
      generated_at: new Date().toISOString(),
      source: "永慶房屋公開搜尋與詳細頁",
      validation_id: validationId,
      candidate_count: 0,
      validated_count: 0,
      fresh_validation: { attempted: true, successful: false, published_eligible: 0 },
      errors: [{ code: browserFailureCode(error), error: String(error?.message || error).slice(0, 180) }],
      items: [],
    }, {
      status: 200,
      headers: { "Cache-Control": "no-store", "X-Rental-Feed-Cache": "error" },
    });
  } finally {
    if (YUNGCHING_IN_FLIGHT.get(cacheKey.url) === pending) YUNGCHING_IN_FLIGHT.delete(cacheKey.url);
  }
  const healthy = payload.crawl_complete === true && (payload.candidate_count === 0 || payload.validated_count > 0);
  payload = {
    status: healthy ? "ok" : payload.validated_count > 0 ? "partial" : "degraded",
    healthy,
    ...payload,
    validation_id: validationId,
    fresh_validation: {
      attempted: true,
      successful: healthy || payload.validated_count > 0,
      published_eligible: payload.validated_count,
    },
  };
  const response = Response.json(payload, {
    headers: {
      "Cache-Control": healthy
        ? `public, max-age=${YUNGCHING_FEED_CACHE_SECONDS}`
        : "no-store",
      "X-Rental-Feed-Cache": healthy ? "miss" : "error",
    },
  });
  if (cache && healthy) {
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
    if (request.method === "OPTIONS" && url.pathname === "/facebook-inbox") {
      return new Response(null, { status: 204, headers: facebookCorsHeaders(request) });
    }
    if (request.method === "POST" && url.pathname === "/facebook-inbox") {
      return submitFacebookInbox(request, env);
    }
    if (request.method === "GET" && url.pathname === "/facebook-inbox-feed") {
      return readFacebookInbox(request, env);
    }
    if (request.method === "POST" && url.pathname === "/yungching-render") {
      return yungchingRenderResponse(request, env);
    }
    if (request.method === "GET" && url.pathname === "/yungching-feed") {
      try {
        return await yungchingFeedResponse(request, env, ctx);
      } catch (error) {
        console.error(error);
        return Response.json({
          status: "degraded",
          healthy: false,
          generated_at: new Date().toISOString(),
          source: "永慶房屋公開搜尋與詳細頁",
          candidate_count: 0,
          validated_count: 0,
          fresh_validation: { attempted: true, successful: false, published_eligible: 0 },
          errors: [{ attempt: 0, ok: false, error: String(error?.message || error).slice(0, 180) }],
          items: [],
        }, { status: 200, headers: { "Cache-Control": "no-store" } });
      }
    }
    if (request.method === "GET" && url.pathname === "/health") {
      const githubTokenConfigured = Boolean(
        String(env.GITHUB_TOKEN || "").trim(),
      );
      const facebookInboxConfigured = Boolean(
        env.FB_INBOX &&
        String(env.FB_INBOX_WRITE_TOKEN || "").trim() &&
        String(env.FB_INBOX_READ_TOKEN || "").trim(),
      );
      return Response.json(
        {
          status: githubTokenConfigured && facebookInboxConfigured ? "ok" : "degraded",
          service: "taoyuan-rental-line-watchdog",
          githubTokenConfigured,
          browserRunConfigured: Boolean(env.BROWSER),
          facebookInboxConfigured,
          backupCronsUtc: Object.keys(CRON_TO_SLOT),
        },
        { status: githubTokenConfigured && facebookInboxConfigured ? 200 : 503 },
      );
    }
    return new Response("Not found", { status: 404 });
  },
};
