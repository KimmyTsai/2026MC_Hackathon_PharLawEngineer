# CampusPulse — 2026MC_Hackathon_PharLawEngineer

梅竹黑客松 Google 企業題「智慧特助 (AI Agents)」參賽作品。

CampusPulse 是校園行程 Agent：它從課表知道你今天要去哪、什麼時候到，持續監看天氣、公車、YouBike、淹水與校內設施公告，在計畫失效時主動改道或提早出發，必要時準備對外聯絡的草稿——但寄出前一定要你確認。

移動狀態（profile）決定候選交通方式：`wheelchair` 比較低地板公車與步行，`default` 比較 YouBike 與公車，而**每一個方案的最後一段都是校園內的無障礙步行路線**。

- 實作規格：[CLAUDE.md](CLAUDE.md)（M0–M9 里程碑與驗收條件）
- 交付紀律與安全界線：[SKILL.md](SKILL.md)、[references/](references/)
- 目前進度與證據：[docs/campuspulse-status.md](docs/campuspulse-status.md)
- 架構決策：[docs/adr/0001-stack-and-merged-scope.md](docs/adr/0001-stack-and-merged-scope.md)

## Local setup

不需要任何 API key 就能啟動並跑測試——預設 `MODE=replay`，所有資料源都走 `data/scenarios/` 的回放情境。

```powershell
Copy-Item .env.example .env          # 只填你這項任務需要的值
uv venv .venv --python 3.13          # 或 python -m venv .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

絕對不要 commit `.env`、`API` 或任何金鑰。

## Run

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 3000
```

開 <http://127.0.0.1:3000/>。畫面上方的 badge 會標示每個資料源目前是 `回放`、`即時`、`過期` 還是 `無資料`——回放模式永遠不會顯示「即時」。

| 端點 | 說明 |
| --- | --- |
| `GET /health` | 執行模式、模擬時間、圖資規模、各資料源狀態 |
| `GET /state` | 今日行程、目前計畫、待確認動作、訊號與 Agent 紀錄 |
| `GET /graph` | 圖資 + 目前生效的設施狀態覆寫 |
| `GET /events` | SSE：Agent 紀錄、計畫變更、觸發事件 |
| `POST /replay/start` | 載入情境（`{"scenario":"demo_wed","speed":60}`） |
| `POST /replay/advance` | 推進模擬時間（`{"seconds":300}` 或 `{"to":"2026-09-23T08:20:00+08:00"}`）；會感知期間發生的事件並重新規劃 |
| `POST /plan` | 以目前模擬時間重新規劃，回傳計畫、決策紀錄與 Agent 步數 |
| `POST /replay/next` | 跳到下一個情境事件並讓 Agent 反應（**Demo 用這個**，時間點才對得上錄音檔） |
| `POST /replay/inject` | 回放中插入事件（評審現場回報） |
| `POST /replay/reset` | 一鍵重設回放 |
| `POST /replay/inject` 的 `at` | 省略即為「現在」，會立刻被感知；給未來時間則等時鐘走到 |
| `POST /confirm/{id}` | **唯一能寄出的路徑**（`{"approve":true,"body":"可改寫內文"}`） |
| `GET /outbox` | 實際寄出的信。未確認前一定是空的 |
| `POST /schedule` `POST /report` | 尚未實作，回 501 並註明由哪個里程碑交付 |

## Agent（Gemini）

Agent 迴圈用 Gemini function calling：模型判斷要不要改計畫、該叫哪個工具；**所有時間、距離、ETA 都由 `app/graph/router.py` 與 `app/agent/planner.py` 算**，模型只能引用。`send_email` 不在工具清單裡——對外動作只能進 `pending_confirmations`。

### 免費額度是硬限制

實測（2026-09-19）：

| 限制 | 值 |
| --- | --- |
`GenerateRequestsPerDayPerProjectPerModel-FreeTier` | **每模型每天 20 次** |
`GenerateRequestsPerMinutePerProjectPerModel-FreeTier` | 部分模型 **每分鐘 5 次** |
`gemini-3.1-pro-preview` | 免費方案**沒有額度**，一律 429 |

一次 Agent 迴圈要 2–6 次呼叫，也就是一天只能跑 4～8 次。所以 Demo 走錄音重播：

```powershell
# 錄音（每個模型有獨立的每日額度，可以換模型來錄）
.venv\Scripts\python.exe scripts
ecord_cassette.py --model gemini-3.1-flash-lite
```

### 兩種金鑰，不能混用

Google 把金鑰分成兩類，**一把金鑰不能同時做 Gemini 和 Maps**：

| 用途 | 格式 | 放哪 | 怎麼申請 |
| --- | --- | --- | --- |
Gemini | `AQ.Ab8…`（驗證金鑰） | `GEMINI_API_KEY.txt`，一行一把 | <https://aistudio.google.com/apikey> |
Maps | `AIzaSy…`（標準金鑰） | `MAPS_API_KEY.txt` | Cloud Console →「憑證」→ 建立 API 金鑰 |

把標準金鑰放進 `GEMINI_API_KEY.txt` 會被 `/health` 的 `warnings` 指出來，不會等到呼叫失敗才發現。兩個檔案都被 git 忽略。

### 多把金鑰

金鑰檔（`API` / `API.txt` / `GEMINI_API_KEY.txt`）可以一行放一把。免費額度是**按專案**計算的，所以第二個專案的金鑰等於多一份每日額度：

```powershell
$env:GEMINI_KEY_INDEX = 1   # 用第二把
```

若金鑰回 `403 API_KEY_SERVICE_BLOCKED`，表示那把金鑰設了 API 限制而沒包含 Generative Language API，到 Google Cloud Console →「API 和服務」→「憑證」→ 該金鑰 →「API 限制」加入它（並確認該專案已啟用這個 API）。

`AGENT_MODE` 控制行為：

| 值 | 行為 |
| --- | --- |
`cache`（預設） | 命中錄音檔就重播，沒有才呼叫並錄下來 |
`replay` | 只用錄音檔；沒錄到就退化成確定性規劃（**Demo 用這個**） |
`live` | 每次都呼叫，不讀不寫錄音檔 |
`off` | 完全不呼叫模型 |

改了 prompt、工具宣告、工具輸出格式或情境，就要重錄——錄音檔的鍵是這些東西的 hash。錄音檔存有**指紋**，不符時 badge 會直接顯示「錄音檔已過期，需重錄」，測試 `test_the_shipped_cassette_matches_the_current_questions` 也會擋下來。過期的錄音只會「未命中」然後退化成確定性規劃，不會給錯的答案。

**Demo 一定要從「重設並開始」按鈕進場**（`POST /replay/start`）：錄音是從情境起始時間的那一次規劃開始錄的，少跑那一次，之後每一步的提問都會不同而全部未命中。

模型不可用時（沒金鑰、額度用完、429），畫面上的 Agent badge 會顯示「確定性規劃」並附原因，計畫照樣產生。

## 圖資座標

`data/campus_graph.json` 的座標**只影響地圖顯示，不影響任何 ETA** —— 路徑計算吃的是每段的 `length_m`。

座標的來源記在檔案的 `coordinate_basis`：以 geocode 到的資訊系館屋頂為單一錨點，把整張手繪圖平移 78 公尺對齊上去，相對位置完全保留。Google 索引沒有入口、電梯、斜坡、路口、宿舍與公車站，那些仍是估計位置，所有節點標為 `coordinate_source: derived`，`draft` 維持 true 直到有人實地測繪。

```powershell
.venv\Scripts\python.exe scriptsix_graph_coordinates.py --dry-run
```

## 底圖

`MAP_PROVIDER` 控制底圖來源：

| 值 | 來源 | 需要什麼 |
| --- | --- | --- |
`osm`（預設） | OpenStreetMap | 不需要金鑰，圖磚掛掉時向量圖層仍可讀 |
`google` | Google **Map Tiles API**，由伺服器代理 | `MAPS_API_KEY.txt` + 啟用 Map Tiles API |

Google 圖磚**不會**讓瀏覽器直接向 Google 要——Map Tiles 的請求網址必須帶 `key`，直接用就等於把金鑰印在頁面上。所以走 `/map/tiles/{z}/{x}/{y}.png` 由伺服器轉送，金鑰留在本機。連續 5 張圖磚失敗就自動退回 OSM 並在地圖上標示。

不要把 Leaflet 的圖磚網址指向 `mt0.google.com/vt/...`——那違反 Google Maps 服務條款，圖磚只能透過 Maps API 存取。

圖磚只做小量記憶體快取（平移時不重抓），不落地存檔：持久快取是我無法在這裡確認的條款問題，而離線情境已經由 OSM 退路涵蓋。

## 對外動作的授權界線

`send_email` **不在模型的工具清單裡**——這是結構上的限制，不是 prompt 指示，有測試斷言它不存在。模型只能呼叫 `draft_email`，那只會把草稿放進 `pending_confirmations`。

唯一會真的寄出的程式路徑是 `POST /confirm/{id}`，由使用者在確認視窗按下「寄出」才會呼叫。保證：

- 未確認 → `GET /outbox` 是空的
- 重複確認 → 回 `duplicate: true`，不會寄第二封（以 idempotency key 判定）
- 取消後 → 不能再確認，回 409
- 草稿過期 → 不能寄出

「是否遲到」是確定性的時間計算，不是模型判斷。模型可以寫更好的措辭，但改不了事實，也寄不出去。

## Test

```powershell
.venv\Scripts\python.exe -m pytest
```

## AI Studio smoke test

把 Gemini API key 放進被 git 忽略的 `API` 檔，或設 `GEMINI_API_KEY`，然後：

```powershell
node scripts/test-ai-studio.mjs
```

用 `GEMINI_MODEL` 換模型測試，不需改腳本。模型 ID 請到 AI Studio 確認，不要憑記憶硬寫。

## Team workflow

短命任務分支 + Pull Request。分支分工、每日同步指令與合併規則見 [CONTRIBUTING.md](CONTRIBUTING.md)。
