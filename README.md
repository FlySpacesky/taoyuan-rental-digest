# 桃園四房以上租屋快報

這是一個與 `tech-newsletter` 完全分離的獨立專案。

- 來源：591、樂屋網、透過檔案或 GitHub Actions secret 安全匯入的真實 Facebook 貼文
- 地區：桃園區、中壢區、平鎮區、八德區
- 格局：4房以上
- 驗證：樂屋網逐筆檢查單筆頁；591 優先讀網站前端使用的官方 BFF，並以 `role_name` 驗證屋主、以 `diff_price` 驗證降價，BFF 失效時才退回 SSR HTML／Chromium；詳情頁仍被阻擋時使用同輪嚴格列表快照，404／410 或明確失效物件不發布
- 分頁：591 公開搜尋網址使用 `page=1,2,3...`，官方 BFF 對應使用 `firstRow=0,30,60...`
- 去重：同一輪跨來源相同房源只顯示一次；近48小時紀錄僅供診斷，不隱藏仍有效物件
- 排程：台灣時間每天08:17、20:17
- 網站：GitHub Pages
- 通知：LINE Messaging API；排程每次都發送最新快報連結，即使本次沒有新物件也會發送
- Threads：不納入本快報，也不需要設定 Threads Access Token
- Facebook：不登入、不使用帳密／Cookie／Session；資料來源為
  `data/facebook_posts.json`、`FACEBOOK_POSTS_JSON`，或
  `FACEBOOK_POSTS_JSON_URL` HTTPS feed Actions secret

完整建立步驟請看 `SETUP_GUIDE_zh-TW.md`。
