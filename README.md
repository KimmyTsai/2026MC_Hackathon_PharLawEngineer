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
| `POST /schedule` `POST /report` `POST /confirm/{id}` | 尚未實作，回 501 並註明由哪個里程碑交付 |

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
