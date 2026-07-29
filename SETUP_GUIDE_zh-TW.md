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

- 台灣時間08:17
- 台灣時間20:17

設定位置：

`.github/workflows/rental-digest.yml`

```yaml
schedule:
  - cron: "17 8,20 * * *"
    timezone: "Asia/Taipei"
```

選17分而不是整點，是為了降低GitHub Actions整點高負載造成延遲的機率。

要改成10:00與22:00：

```yaml
schedule:
  - cron: "0 10,22 * * *"
    timezone: "Asia/Taipei"
```

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

格式參考：

`data/facebook_posts.example.json`

建立方式：

1. 從你有權使用的外部資料來源取得真實貼文資料；本程式不登入或匿名繞過Facebook存取限制。
2. 依範例填入永久貼文網址、公開照片網址與房源欄位。
3. 可將JSON存為 `data/facebook_posts.json`；若不希望資料檔進入版本控制，
   則把完整JSON陣列設為Repository Actions secret `FACEBOOK_POSTS_JSON`。
4. 若資料由其他合法系統持續整理，可把HTTPS JSON feed網址設為
   Repository Actions variable（或secret）`FACEBOOK_POSTS_JSON_URL`，
   workflow每次執行時會重新讀取，不必反覆commit資料檔。
5. 手動執行workflow並在來源診斷確認 `candidate_links`、`validated` 與拒絕原因。

程式只接受設定清單內社團的單篇貼文網址，並驗證4房以上、指定地區、租金與公開圖片；
同時排除代租代管、包租代管、租管通、租賃服務業及代理人。未提供上述任一真實資料來源時，
FB統計會維持0並顯示可操作的錯誤訊息，不會製造替代房源。

## 11. 常見錯誤

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
4. 如果本次48小時去重後沒有新物件，程式會刻意不發LINE。

### 定時排程沒有執行

1. 工作流程必須存在於預設分支main。
2. 到Actions確認工作流程未被停用。
3. 公開儲存庫60天無活動時，GitHub可能自動停用排程；手動啟用或提交一次更新即可。
4. GitHub排程可能延遲，不保證精確到秒。
