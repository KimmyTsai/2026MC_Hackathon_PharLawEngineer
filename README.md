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
| `POST /replay/advance` | 推進模擬時間（`{"seconds":300}` 或 `{"to":"2026-09-23T08:20:00+08:00"}`） |
| `POST /replay/inject` | 回放中插入事件（評審現場回報） |
| `POST /replay/reset` | 一鍵重設回放 |
| `POST /schedule` `POST /report` `POST /confirm/{id}` | 尚未實作，回 501 並註明由哪個里程碑交付 |

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
