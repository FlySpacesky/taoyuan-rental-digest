# 桃園四房以上租屋快報

這是一個與 `tech-newsletter` 完全分離的獨立專案。

- 來源：591、Facebook真實貼文、樂屋網、Threads官方API、信義房屋、永慶房屋
- 地區：桃園區、中壢區、平鎮區、八德區
- 格局：房仲網站維持4房以上；FB與Threads接受3房以上
- 驗證：樂屋網逐筆檢查單筆頁；591 只讀一次已包含屋主的官方一般 BFF 清單，並以 `role_name` 驗證屋主、以 `diff_price` 驗證降價；明確 403／429 時不再用同一出口連打 SSR HTML／Chromium，BFF 清單也不逐筆重打詳情頁，以避免加重 Runner 出口 IP 限流
- 分頁：591 公開搜尋網址使用 `page=1,2,3...`，官方 BFF 對應使用 `firstRow=0,30,60...`
- 去重：同一輪跨來源相同房源只顯示一次；近48小時紀錄僅供診斷，不隱藏仍有效物件
- 排程：台灣時間每天09:30、16:00、22:00；Cloudflare Worker 在每個時段後的
  備援窗口監看 GitHub Actions，必要時自動補觸發
- 網站：GitHub Pages
- 通知：LINE Messaging API；排程每次都發送最新快報連結，即使本次沒有新物件也會發送
- 防重複發送：正常排程與 Cloudflare 補觸發使用同一投遞時段產生固定的
  LINE Retry Key，同一時段只會被 LINE 接受一次
- Threads：合併官方 `keyword_search`、`THREADS_ACCESS_TOKEN`、
  `data/threads_posts.json`、`THREADS_POSTS_JSON`、`THREADS_POSTS_JSON_URL` 與
  GitHub公開投稿。留言只合併與原貼文同一username的內容；硬條件為指定四區、
  3房以上、最近收集時間窗及非包租代管／仲介同業，租金、坪數與照片可未提供
- Facebook：程式不登入、不使用帳密／Cookie／Session；資料來源為
  `data/facebook_posts.json`、`FACEBOOK_POSTS_JSON` Actions secret，或
  `FACEBOOK_POSTS_JSON_URL` HTTPS feed（Actions secret／repository variable），
  電子報上的 GitHub 公開投稿表單，以及 Cloudflare KV 私人收件匣。公開貼文須可匿名
  驗證；已加入的私人社團可由使用者在自己的瀏覽器手動開啟並投稿，但必須確認已取得
  貼文作者或社團管理員的電子報再公開授權。私人收件匣使用分離的唯寫／唯讀權杖、
  房源30天自動到期，且拒絕帳密、Cookie、Session與Access Token欄位。
  既有社團連結只作為人工查找入口；這些來源會合併、依永久貼文去重；
  公開投稿會匿名讀取 Facebook 單篇貼文的 Open Graph／公開中繼資料，
  自動補齊欄位並把真實照片保存到本站。硬條件同樣為指定四區、3房以上、
  最近收集時間窗及非包租代管／仲介同業。FB 另採語境判斷：公司、證照、
  服務費或招攬委託等業者廣告排除，但「屋主想找代管／人在外地／沒時間管理」
  會保留並列為高價值訊號。30至35坪以上、租金、照片與屋主訊號只用於評分
- 來源時間窗口：591只保留來源新增或更新在最近2天內的物件；FB、樂屋網、Threads、
  信義房屋與永慶房屋保留來源新增或更新在最近7天內的物件。所有來源仍須本輪重新驗證成功才會發布。
- 新房源：所有來源都與上一封成功投遞的快報比較；上一封未出現且本站首次發現
  未滿24小時才標示「新房源」，標示超過24小時自動移除。
- 顯示：每個來源均為單欄列表、每筆左圖右文；FB提供「全部、A級、B級、C級」
  屋主房源雷達，Threads維持「全部、高符合、30坪以上、資訊待補」與自動符合度，
  其他來源維持各自的分類與排序控制。
  非社群來源只有本日第一次出現的房源會在首圖標示「新房源」
- 591：優選好屋使用官方 BFF `preferred`；租金總費用使用月租加上官方
  `extra_fee`，若來源未提供額外月費則回退為月租
- 樂屋網：搜尋桃園區、中壢區、平鎮區、八德區，分類直接使用現行
  `tab=rkp`（屋主）、`tab=frd`（友善房源）、`tab=low`（最新降價）頁籤
- 永慶房屋：GitHub Runner 被來源 CloudFront 阻擋時，改由 Cloudflare Browser Run
  讀取固定的永慶公開搜尋及詳細頁，再提供2小時快取摘要。只保留指定四區、
  整層住家、4房以上且有詳細頁更新日期的物件；照片僅取該物件相簿，
  不使用帳密、Cookie或私人Session，也不挪用推薦房源照片
- 591 成功抓取後仍會保存 `docs/rental-data/last-success-591.json` 作診斷與租金比較，
  但任何新電子報都不會沿用未重新驗證的舊物件。只有本輪重新驗證成功、確認仍在
  刊登且來源新增或更新時間在2天內的物件可以發布。
- 591 若明確遭遇 403／429（包括已有部分成功資料），正式流程預設等待15分鐘，
  再以新的出口只重驗591一次；同一小時內的 partial checkpoint 會依物件ID去重合併，
  但只有本輪各次確實重新驗證成功的物件會發布。變數
  `RENTAL_591_RETRY_DELAY_SECONDS` 可在300至3600秒間調整，
  `RENTAL_591_REQUEST_INTERVAL_SECONDS` 可在1至10秒間調整。
  初次與重試結果都必須通過「無 `snapshot_used`／無 `fallback`」閘門。
- 若有自有且獲授權的穩定 Linux 執行環境，可把 Repository variable
  `RENTAL_591_RETRY_RUNNER` 設為該 self-hosted runner 的自訂標籤；未設定時仍使用
  `ubuntu-latest`。這比反覆更換大量雲端IP更穩定，也保留網站限流邊界。
- 若有合規且可長期使用的固定出口，可設定 GitHub Actions Secrets
  `RENTAL_591_PROXY_SERVER`、`RENTAL_591_PROXY_USERNAME`、
  `RENTAL_591_PROXY_PASSWORD`。代理只套用於 591 的 HTTP 與 Chromium 請求，
  Facebook、Threads、GitHub Token 與其他來源不會經過該代理。未設定時會自動
  使用上述「延後＋新 Runner」模式。

完整建立步驟請看 `SETUP_GUIDE_zh-TW.md`。
