# 桃園租屋快報：全新GitHub專案建立教學

## 建議儲存庫名稱

`taoyuan-rental-digest`

完成後網址會是：

`https://你的GitHub帳號.github.io/taoyuan-rental-digest/`

## 1. 建立新儲存庫

1. 登入GitHub。
2. 右上角按 `＋`。
3. 選 `New repository`。
4. Owner選自己的帳號。
5. Repository name輸入 `taoyuan-rental-digest`。
6. Description輸入 `桃園四房以上租屋快報`。
7. Visibility建議選 `Public`。
8. 不要勾選README、.gitignore或License，避免與壓縮包發生檔案衝突。
9. 按 `Create repository`。

## 2. 解壓縮與上傳

1. 下載並解壓縮 `taoyuan-rental-digest-starter.zip`。
2. 進入新儲存庫首頁。
3. 按 `uploading an existing file`，或 `Add file → Upload files`。
4. 把解壓縮後資料夾內的所有檔案與資料夾拖入上傳區。
5. 必須看到以下根目錄結構：

```text
.github/
data/
docs/
scripts/
README.md
SETUP_GUIDE_zh-TW.md
requirements.txt
```

6. Commit message輸入 `initial rental digest setup`。
7. 選 `Commit directly to the main branch`。
8. 按 `Commit changes`。

### 特別注意隱藏資料夾

`.github` 是以句點開頭的隱藏資料夾。Windows檔案總管若看不到：

1. 點上方 `檢視`。
2. 選 `顯示`。
3. 勾選 `隱藏的項目`。

若瀏覽器拖曳整個資料夾後沒有 `.github/workflows/rental-digest.yml`，Actions不會出現。

## 3. 開啟GitHub Actions寫入權限

工作流程需要把48小時去重紀錄寫回儲存庫。

1. 開啟新儲存庫。
2. 點 `Settings`。
3. 左側選 `Actions → General`。
4. 往下找到 `Workflow permissions`。
5. 選 `Read and write permissions`。
6. 按 `Save`。

若沒有這一步，保存去重紀錄時可能出現403。

## 4. 設定GitHub Pages

本專案使用GitHub Actions直接部署Pages：

1. 點 `Settings`。
2. 左側選 `Pages`。
3. `Build and deployment`區域的 `Source` 選 `GitHub Actions`。
4. 不需要選main分支或/docs。
5. 返回儲存庫首頁。

## 5. 設定LINE Token

若要沿用科技電子報相同的LINE官方帳號，可以把同一個Channel access token再存一份到新儲存庫；兩個儲存庫的Secret彼此獨立。

1. 點新儲存庫的 `Settings`。
2. 左側選 `Secrets and variables → Actions`。
3. 點 `New repository secret`。
4. Name輸入：

```text
LINE_CHANNEL_ACCESS_TOKEN
```

5. Secret貼上LINE Messaging API的長期Channel access token。
6. 點 `Add secret`。

不要把Token貼進程式、README或公開Issue。

## 6. 第一次手動測試

1. 點儲存庫上方 `Actions`。
2. 左側選 `桃園租屋快報`。
3. 點右側 `Run workflow`。
4. Branch選 `main`。
5. 再按一次綠色 `Run workflow`。
6. 點進最新執行紀錄。
7. 確認以下步驟都是綠色勾勾：
   - 下載儲存庫
   - 設定Python
   - 安裝套件
   - 抓取、驗證、去重並產生網頁
   - 保存48小時去重與租金快照
   - 設定GitHub Pages
   - 上傳Pages網站
   - 部署GitHub Pages
   - 發送LINE

## 7. 查看網站

第一次部署成功後：

1. 點 `Settings → Pages`。
2. GitHub會顯示公開網址。
3. 預設應為：

```text
https://你的帳號.github.io/taoyuan-rental-digest/
```

LINE訊息也會帶入此次部署真正產生的Pages網址，不需要手動修改帳號名稱。

## 8. 排程時間

預設排程：

- 台灣時間09:30
- 台灣時間16:00
- 台灣時間22:00

設定位置：

`.github/workflows/rental-digest.yml`

```yaml
schedule:
  - cron: "30 9 * * *"
    timezone: "Asia/Taipei"
  - cron: "0 16 * * *"
    timezone: "Asia/Taipei"
  - cron: "0 22 * * *"
    timezone: "Asia/Taipei"
```

GitHub排程可能因平台負載延遲，因此另有 Cloudflare Worker 作為備援監看器。
它不直接發送LINE，而是每5分鐘檢查正式 `main` 是否已有該時段的有效LINE投遞
收據，監看範圍為各時段後5分鐘至5小時50分鐘。只有收據存在且內容核對正確才算完成；
沒有收據且沒有執行中工作時會補觸發，單一時段最多補觸發4次。正常流程與備援
流程使用同一個LINE Retry Key，因此即使兩者短暫重疊，同一時段也不會重複廣播。

### Cloudflare備援部署設定

GitHub repository 的 Actions Secrets 需要：

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`：Cloudflare帳戶的 `Workers Scripts Write` 與
  `Account Settings Read` 權限。`FB_INBOX` KV已在Dashboard一次性建立並以ID綁定，
  部署權杖不需要建立KV的額外權限
- `FB_INBOX_WRITE_TOKEN`：私人收件匣唯寫權杖
- `FB_INBOX_READ_TOKEN`：私人收件匣唯讀權杖

Worker 本身另需 Secret：

- `GITHUB_TOKEN`：只授權 `FlySpacesky/taoyuan-rental-digest`，Repository
  permissions 的 Actions 設為 Read and write

Worker程式、三個UTC Cron Triggers與測試都在 `cloudflare-worker/`。正式main
更新後，由 `.github/workflows/cloudflare-watchdog.yml` 自動部署。

## 9. 兩個專案如何完全分離

### 科技電子報

- Repository：`tech-newsletter`
- Pages：`https://你的帳號.github.io/tech-newsletter/`
- Workflow：科技新聞排程
- 去重資料：只存科技文章

### 租屋快報

- Repository：`taoyuan-rental-digest`
- Pages：`https://你的帳號.github.io/taoyuan-rental-digest/`
- Workflow：591、樂屋網與FB資料排程
- 去重資料：`docs/rental-data/history.json`

兩邊不會共用程式、頁面、排程或去重紀錄。只有在你選擇沿用同一個LINE官方帳號時，兩個儲存庫會各自保存同一個LINE Token副本。

## 10. Facebook安全處理

不要把Facebook帳號、密碼、Cookie或Session貼到GitHub。

目前安全模式可使用以下任一來源：

- `data/facebook_posts.json`
- Repository Actions secret：`FACEBOOK_POSTS_JSON`
- Repository Actions secret 或 variable：`FACEBOOK_POSTS_JSON_URL`
  （可匿名讀取的HTTPS JSON feed；URL本身不敏感時建議用 variable）
- Cloudflare私人收件匣：電子報的「私人社團授權投稿」入口。您本人可在自己的
  Facebook瀏覽器手動登入、加入社團並開啟單篇貼文，但只有已取得作者或社團管理員
  同意再公開的內容可提交。收件匣不接收Facebook登入資料。

私人收件匣採兩把分離權杖：投稿頁只有寫入權，GitHub Actions的
`FB_INBOX_READ_TOKEN`只有讀取權。房源30天自動到期；投稿頁把唯寫權杖保存在使用者
自己的瀏覽器中，不會把它寫入GitHub Pages。Facebook的登入與入社狀態仍由Facebook
管理，若Facebook登出，必須由使用者本人重新登入。

格式參考：

`data/facebook_posts.example.json`

### 直接交給Codex代為建立

你可以直接提供每一筆真實房源的：

1. Facebook社團的「單篇永久貼文網址」（不是社團首頁或分享短網址）。
2. 貼文文字，或你有權使用、內容清楚可辨識的貼文截圖。
3. 最近7天內的原始刊登時間、3房以上格局、桃園區／中壢區／平鎮區／八德區其中一區。
4. 可公開讀取的原始照片HTTPS網址。
5. 若貼文有提供，也可附上坪數、樓層、刊登者、更新時間、瀏覽人氣與原租金。

收到上述資料後，Codex可以協助整理成 `data/facebook_posts.json`、執行驗證、
更新GitHub並確認Actions結果。請勿提供Facebook帳號、密碼、Cookie或Session。
私人社團貼文需由已加入社團的使用者手動貼上完整文字，並確認已取得再公開授權；
只有社團首頁、沒有單篇永久網址或沒有授權的內容，不能刊登。

`https://www.facebook.com/share/...` 分享短網址不能證明貼文來自允許清單內的社團；
請從貼文選單複製可看出社團ID與貼文ID的永久網址，例如
`https://www.facebook.com/groups/{社團ID}/posts/{貼文ID}/`。
`https://www.facebook.com/photo?...` 是照片網頁而不是圖片檔，不能放在 `image`；
請提供可直接顯示且已授權的公開圖片網址。貼文仍須明確包含3房以上，並通過
包租代管／仲介同業等排除條件；租金、坪數與照片可缺少，但不會補造。

### 使用HTTPS feed持續更新

若你或合法資料供應系統能持續輸出JSON，請提供一個不需登入、GitHub Actions可直接讀取、
回傳內容小於2MB的HTTPS網址。最外層可為陣列，或為包含 `posts` 陣列的物件。
Codex可以協助把網址設定成Repository variable `FACEBOOK_POSTS_JSON_URL` 並執行驗證。

建立方式：

1. 從你有權使用的外部資料來源取得真實貼文資料；本程式不登入或匿名繞過Facebook存取限制。
2. 依範例填入永久貼文網址、公開照片網址與房源欄位。
3. 可將JSON存為 `data/facebook_posts.json`；若不希望資料檔進入版本控制，
   則把完整JSON陣列設為Repository Actions secret `FACEBOOK_POSTS_JSON`。
4. 若資料由其他合法系統持續整理，可把HTTPS JSON feed網址設為
   Repository Actions variable（或secret）`FACEBOOK_POSTS_JSON_URL`，
   workflow每次執行時會重新讀取，不必反覆commit資料檔。
5. 手動執行workflow並在來源診斷確認 `candidate_links`、`validated` 與拒絕原因。

程式接受任何社團的單篇永久貼文網址，並驗證3房以上、指定地區、最近時間窗；
同時排除代租代管與仲介同業廣告。未提供上述任一真實資料來源時，
FB統計會維持0並顯示可操作的錯誤訊息，不會製造替代房源。

## 11. 設定Threads官方搜尋

Threads區塊只使用Meta官方API，不使用Threads帳號密碼、Cookie或瀏覽器Session。

1. 在Meta for Developers建立具有Threads使用案例的App。
2. 由使用者完成OAuth授權，Access Token必須包含
   `threads_basic` 與 `threads_keyword_search` 權限。若要讀取權杖帳號自己貼文的
   留言，另加入 `threads_read_replies`。
3. 到GitHub儲存庫的 `Settings → Secrets and variables → Actions`。
4. 建立Repository secret：

```text
THREADS_ACCESS_TOKEN
```

5. 手動執行「桃園租屋快報」workflow驗證。

程式會使用官方 `keyword_search` 搜尋桃園區租屋相關關鍵字，再逐筆確認：

- 主貼文或原作者本人留言明確包含桃園區
- 主貼文或原作者本人留言可確認格局為4房以上
- 主貼文或原作者本人留言的最新活動日期為台灣時區的今天或昨天
- 是出租／租屋貼文
- 主貼文與API可讀取的原作者留言，其單圖或輪播中的全部照片都能下載並保存到本站

租金不是必要欄位；未提供時電子報顯示「租金洽詢」，並在租金排序中排在有價格的
物件之後。系統只合併username與原貼文作者相同的留言，不採用其他人的留言。
Meta官方Reply Moderation API只允許完整讀取權杖帳號自己貼文的回覆，因此其他公開
作者貼文的留言若未由官方搜尋結果提供，程式會在來源診斷標示API限制，不會使用帳密、
Cookie、Session或繞過登入保護。

任一條件不足或任一張照片保存失敗時，該物件不會刊出。未設定
`THREADS_ACCESS_TOKEN` 時，Threads統計維持0並在來源訊息顯示設定方式。
Token只能放在GitHub Actions secret，不要寫入程式、README、Issue或commit。

## 12. 常見錯誤

### Actions頁面沒有工作流程

檢查檔案是否真的位於：

`.github/workflows/rental-digest.yml`

不能是：

`taoyuan-rental-digest/.github/workflows/...`

也不能把整個最外層資料夾再多包一層上傳。

### 保存去重紀錄時403

到：

`Settings → Actions → General → Workflow permissions`

選 `Read and write permissions`。

### Pages顯示404

確認：

1. `Settings → Pages → Source` 是 `GitHub Actions`。
2. Actions內 `部署GitHub Pages` 是綠色。
3. `docs/index.html` 存在。

### LINE沒有收到

1. Secret名稱必須完全是 `LINE_CHANNEL_ACCESS_TOKEN`。
2. Token不可有前後空白。
3. 查看Actions的 `發送LINE` 步驟。
4. 查看記錄中的投遞時段；若顯示LINE先前已接受相同 Retry Key，代表備援成功
   阻止重複發送，不是錯誤。
5. 確認 `docs/rental-data/delivery/日期-時段.json` 已提交到正式 `main`；其中
   `status` 應為 `accepted` 或 `already_accepted`，且 `delivery_slot` 必須是
   當次09:30、16:00或22:00時段。缺少這份收據時，Cloudflare會繼續檢查及補觸發。

### 定時排程沒有執行

1. 工作流程必須存在於預設分支main。
2. 到Actions確認工作流程未被停用。
3. 公開儲存庫60天無活動時，GitHub可能自動停用排程；手動啟用或提交一次更新即可。
4. GitHub排程可能延遲，不保證精確到秒。
5. 到Cloudflare確認 `taoyuan-rental-line-watchdog` 的Cron Trigger為每5分鐘、
   `GITHUB_TOKEN` Secret仍存在，並查看Observability是否有錯誤。
