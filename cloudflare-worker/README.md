# 租屋電子報 LINE 備援監督器

GitHub Actions 仍在台北時間 09:30、16:00、22:00 執行正式流程。此 Cloudflare
Worker 在各時段後約 5 到 30 分鐘的備援窗口內，每分鐘檢查 GitHub Actions：

- 已成功：不做任何事。
- 仍在執行：等待下一個檢查點，不重複觸發。
- 沒有執行或已失敗：以 `workflow_dispatch` 補觸發，並傳入固定的
  `delivery_slot`。

LINE 發送程式會由 `delivery_slot` 產生固定 `X-Line-Retry-Key`。即使 GitHub
原排程與 Cloudflare 補觸發重疊，LINE 也只會接受同一時段一次。

三個備援窗口各只占用一個 Cron Trigger，可與同一免費 Cloudflare 帳戶內既有的
科技新聞 Worker 共存，不超過免費方案每帳戶 5 個 Cron Triggers 的限制。

部署後可開啟 `/health`。HTTP 200 且 `githubTokenConfigured: true` 代表 Worker
程式與必要 Secret 均已載入；缺少 Secret 時會回傳HTTP 503，且不會洩漏權杖值。

## 永慶公開資料摘要

`/yungching-feed` 使用 Cloudflare Browser Run 瀏覽固定的永慶公開搜尋頁與每筆
詳細頁，解決 GitHub Runner 出口被 CloudFront 回應403而使永慶區塊為0的問題。
摘要只接受桃園區、中壢區、平鎮區、八德區、整層住家、4房以上、有租金與詳細頁
更新日期的物件；照片只取該物件相簿的 `yccdn.yungching.com.tw` 真實圖片，不取
地圖或其他推薦房源圖片。結果快取2小時以控制免費 Browser Run 用量。

此功能不使用永慶帳號、密碼、Cookie或私人Session。`wrangler.jsonc` 中的
`BROWSER` binding 由正式部署流程建立及更新。

## Cloudflare Secret

Worker 需要一個 Secret：

- `GITHUB_TOKEN`：GitHub fine-grained personal access token，只授權
  `FlySpacesky/taoyuan-rental-digest`，Repository permissions 的 Actions 設為
  Read and write。

不要將 Token 寫入 `wrangler.jsonc` 或 GitHub repository。

## GitHub Actions 部署 Secret

若使用 repository 內的自動部署流程，需建立：

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`：只授權 Workers Scripts Edit 與 Account Settings Read。

## 測試

```bash
node --test tests/*.test.mjs
```
