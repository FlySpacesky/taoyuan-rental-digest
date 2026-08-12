# 桃園四房以上租屋快報

這是一個與 `tech-newsletter` 完全分離的獨立專案。

- 來源：591、Facebook真實貼文、樂屋網、Threads官方API、信義房屋、永慶房屋
- 地區：桃園區、中壢區、平鎮區、八德區
- 格局：房仲網站維持4房以上；FB與Threads接受3房以上
- 驗證：樂屋網逐筆檢查單筆頁；591 優先讀網站前端使用的官方 BFF，並以 `role_name` 驗證屋主、以 `diff_price` 驗證降價，BFF 失效時才退回 SSR HTML／Chromium；BFF 清單不再逐筆重打詳情頁，以避免 GitHub Runner 出口 IP 被限流
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
- Facebook：不登入、不使用帳密／Cookie／Session；資料來源為
  `data/facebook_posts.json`、`FACEBOOK_POSTS_JSON` Actions secret，或
  `FACEBOOK_POSTS_JSON_URL` HTTPS feed（Actions secret／repository variable），
  以及電子報上的 GitHub 公開投稿表單。接受任何可匿名驗證的公開社團永久貼文，
  既有社團連結只作為人工查找入口；這些來源會合併、依永久貼文去重；
  公開投稿會匿名讀取 Facebook 單篇貼文的 Open Graph／公開中繼資料，
  自動補齊欄位並把真實照片保存到本站。硬條件同樣為指定四區、3房以上、
  最近收集時間窗及非包租代管／仲介同業。FB 另採語境判斷：公司、證照、
  服務費或招攬委託等業者廣告排除，但「屋主想找代管／人在外地／沒時間管理」
  會保留並列為高價值訊號。30至35坪以上、租金、照片與屋主訊號只用於評分
- 社群收集窗口：FB與Threads首次成功啟用回溯7天，後續每次只讀最近2天；
  同一來源物件過去2天未出現才標示「新房源」
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
- 591 成功抓取後會保存 `docs/rental-data/last-success-591.json`。兩小時內
  重跑會沿用這份真實快照以避免限流；新一輪受 403／429 阻擋時最多沿用
  72 小時，頁面會明確標示「未重新驗證」，不會把舊資料冒充新抓結果。

完整建立步驟請看 `SETUP_GUIDE_zh-TW.md`。
