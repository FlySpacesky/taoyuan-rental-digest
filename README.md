# 桃園四房以上租屋快報

這是一個與 `tech-newsletter` 完全分離的獨立專案。

- 來源：591、樂屋網、Threads官方API，以及透過檔案或 GitHub Actions secret 安全匯入的真實 Facebook 貼文
- 地區：桃園區、中壢區、平鎮區、八德區
- 格局：4房以上
- 驗證：樂屋網逐筆檢查單筆頁；591 優先讀網站前端使用的官方 BFF，並以 `role_name` 驗證屋主、以 `diff_price` 驗證降價，BFF 失效時才退回 SSR HTML／Chromium；BFF 清單不再逐筆重打詳情頁，以避免 GitHub Runner 出口 IP 被限流
- 分頁：591 公開搜尋網址使用 `page=1,2,3...`，官方 BFF 對應使用 `firstRow=0,30,60...`
- 去重：同一輪跨來源相同房源只顯示一次；近48小時紀錄僅供診斷，不隱藏仍有效物件
- 排程：台灣時間每天08:17、20:17
- 網站：GitHub Pages
- 通知：LINE Messaging API；排程每次都發送最新快報連結，即使本次沒有新物件也會發送
- Threads：使用官方 `keyword_search` 與 `THREADS_ACCESS_TOKEN`；只刊出今天或昨天
  有主貼文／原作者留言活動的桃園區、4房以上且全部照片成功保存的真實物件。
  租金可未提供，頁面會顯示「租金洽詢」；留言只合併與原貼文同一username的內容
- Facebook：不登入、不使用帳密／Cookie／Session；資料來源為
  `data/facebook_posts.json`、`FACEBOOK_POSTS_JSON` Actions secret，或
  `FACEBOOK_POSTS_JSON_URL` HTTPS feed（Actions secret／repository variable），
  以及電子報上的 GitHub 公開投稿表單。這些來源會合併、依永久貼文去重；
  公開投稿會匿名讀取 Facebook 單篇貼文的 Open Graph／公開中繼資料，
  自動補齊欄位並把真實照片保存到本站，驗證失敗的投稿不刊出
- 顯示：每個來源均為單欄列表、每筆左圖右文；591與樂屋網提供各自對應的分類與排序控制
- 591：優選好屋使用官方 BFF `preferred`；租金總費用使用月租加上官方
  `extra_fee`，若來源未提供額外月費則回退為月租
- 樂屋網：搜尋桃園區、中壢區、平鎮區、八德區，分類直接使用現行
  `tab=rkp`（屋主）、`tab=frd`（友善房源）、`tab=low`（最新降價）頁籤
- 591 成功抓取後會保存 `docs/rental-data/last-success-591.json`。兩小時內
  重跑會沿用這份真實快照以避免限流；新一輪受 403／429 阻擋時最多沿用
  72 小時，頁面會明確標示「未重新驗證」，不會把舊資料冒充新抓結果。

完整建立步驟請看 `SETUP_GUIDE_zh-TW.md`。
