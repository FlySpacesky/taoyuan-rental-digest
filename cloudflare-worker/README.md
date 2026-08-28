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

部署後可開啟 `/health`。HTTP 200 且 `githubTokenConfigured`、
`facebookInboxConfigured` 皆為 `true` 代表 Worker 程式與必要 Secret 均已載入；
缺少 Secret 時會回傳HTTP 503，且不會洩漏權杖值。

## Facebook 私人收件匣

- `POST /facebook-inbox`：只接受唯寫 Bearer Token。貼文必須是最近7天內的社團永久
  網址、包含完整原文，並確認已取得作者或管理員的電子報再公開授權。
- `GET /facebook-inbox-feed`：只接受另一把唯讀 Bearer Token，供 GitHub Actions 匯入。
- 投稿資料存於 `FB_INBOX` KV，30天自動到期；相同永久網址會更新同一筆。
- 收件成功後會用既有 GitHub 權杖觸發一次 `skip_line=true` 的網頁更新；不會因此
  額外發送 LINE。若 GitHub 暫時無法接受觸發，資料仍已保存並會在下個排程讀取。
- 端點拒絕帳號、密碼、Cookie、Session、Access Token等欄位。Facebook登入與社團
  會員狀態只保留在使用者自己的瀏覽器，Worker不接觸也不保存。

## 永慶公開資料摘要

`/yungching-feed` 使用 Cloudflare Browser Run 瀏覽固定的永慶公開搜尋頁與每筆
詳細頁，解決 GitHub Runner 出口被 CloudFront 回應403而使永慶區塊為0的問題。
摘要只接受桃園區、中壢區、平鎮區、八德區、整層住家、4房以上、有租金與詳細頁
更新日期的物件；照片只取該物件相簿的 `yccdn.yungching.com.tw` 真實圖片，不取
地圖或其他推薦房源圖片，支援新版 `yc-ng-album-v2-carousel` 相簿。

每輪傳入不同的 `validation_id`，只可重用同一輪的成功結果（最長2小時）；
Python 會核對回傳輪次與每筆實際 `validated_at`，不把前輪快取重新蓋上本輪時間。
同一 isolate 的同輪併發請求共用工作；不宣稱跨機房全域鎖。

列表與詳細頁重用同一瀏覽器，僅真正失敗時重建；不再每六筆啟動新瀏覽器。
詳細頁使用伺服器已輸出的資訊，停用不必要的前端地圖與推薦模組，減少執行資源及日期被收合的競態。
429 依 `Retry-After`／Browser Run limits 等待，至少21秒、單次最多60秒；
每日額度耗盡不會快速重試。後續失敗保留本輪已驗證項目並回 `partial`；
未知空白／受阻頁回 `degraded`，不能當成市場零物件。

`HTTP 200` 只代表 JSON 可讀，還須檢查 `fresh_validation.successful`、
`validated_count` 與 `errors`。Cloudflare 1102（CPU／記憶體資源限制）由平台
終止執行，Worker 的 catch 無法攔截；需正式驗證資源用量，不能將它改寫成健康零筆。

此功能不使用永慶帳號、密碼、Cookie或私人Session。`wrangler.jsonc` 中的
`BROWSER` binding 由正式部署流程建立及更新。

## Cloudflare Secret

Worker 需要三個 Secret：

- `GITHUB_TOKEN`：GitHub fine-grained personal access token，只授權
  `FlySpacesky/taoyuan-rental-digest`，Repository permissions 的 Actions 設為
  Read and write。
- `FB_INBOX_WRITE_TOKEN`：私人投稿頁的唯寫權杖。
- `FB_INBOX_READ_TOKEN`：GitHub Actions讀取私人feed的唯讀權杖。

不要將 Token 寫入 `wrangler.jsonc` 或 GitHub repository。

## GitHub Actions 部署 Secret

若使用 repository 內的自動部署流程，需建立：

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`：授權 Workers Scripts Edit 與 Account Settings Read。
  `FB_INBOX` KV namespace 已由 Cloudflare Dashboard 一次性建立並以ID綁定，部署權杖
  不需要建立KV的額外權限。
- `FB_INBOX_WRITE_TOKEN`
- `FB_INBOX_READ_TOKEN`

## 測試

PR #44 的指定分支可部署 `taoyuan-rental-yungching-cpu-preview` 隔離診斷版。
它使用獨立 `wrangler.preview.jsonc`，不含正式 KV、GitHub／LINE Secret 或 Cron。
只有健康檢查可匿名讀取；來源測試需每次部署重新產生、1小時失效的專用 Secret，
只讀取固定的永慶公開測試物件，不能指定任意網址。先測原始HTML串流，解析在CI端
執行，不使用Browser Run額度。備用單筆瀏覽器探測最多20秒、不重試，遇到其他
活動session就略過，絕不接管或關閉正式session。整輪抓取的CPU與完整率仍需另外驗證。

隔離端點不發布電子報；`preview-audit/result.json` 區分部署成功與房源讀取成功。
測試版與正式版同帳戶，Browser Run額度仍共用，不能把獨立Worker誤稱為獨立配額。

```bash
node --test tests/*.test.mjs
```
