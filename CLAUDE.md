# CampusPulse — 實作規格（給 Claude Code）

> 梅竹黑客松 Google 企業題「智慧特助 (AI Agents)」參賽作品。
> 本文件是實作依據；產品提案全文見 CampusPulse 提案計畫文件。
> 比賽時限為一天，所有決策以「能穩定 Demo」優先於「功能完整」。

---

## 1. 產品一句話

為行動不便學生（輪椅、拄拐杖、暫時受傷）設計的校園行程 Agent：讀懂課表與校內公告，在校園無障礙圖上規劃確實能通行的路線；電梯停用、下雨、快遲到時主動改道、調整出發時間，必要時經使用者確認後代為寄信。

## 2. 不可違反的設計原則

1. **模型判斷、程式計算**：路徑搜尋、移動時間、是否遲到一律由確定性 Python 程式計算。Gemini 只負責理解與決策（讀信、讀圖、判斷事件影響、選擇策略、產生文字）。不要讓 LLM 算時間或距離。
2. **輸出須有依據**：Agent 陳述的設施狀態必須來自圖資、公告或回報紀錄，並附來源與更新時間。資料不足時說「不確定」，不要推測。
3. **對外行動先確認**：`send_email` 等對外動作只能產生草稿並進入 `pending_confirmations`，使用者按確認後才執行。改道、提醒可自動執行。
4. **Demo 不依賴即時世界**：所有外部資料源都要有 mock／回放實作，透過同一個介面切換。Demo 預設走回放模式。
5. **健康相關資訊不上雲**：使用者的行動狀態（如「暫時行動不便」）只在本地處理，送往雲端的只有路線條件（例如 `avoid_stairs=true`）。

## 3. 技術選型

| 層 | 選擇 | 備註 |
| --- | --- | --- |
| 後端 | Python 3.11 + FastAPI | 單一服務，`uvicorn` 啟動 |
| LLM | Gemini 3 Flash／Gemini 3 Pro，經 Google AI Studio API key | 用 `google-genai` SDK；模型 ID 放環境變數，實際字串請到 AI Studio 確認 |
| 本地模型 | Gemma（經 Ollama 在筆電上跑，模擬端上部署） | 最後做，可砍 |
| 路徑規劃 | `networkx` | 自訂權重函式 |
| 前端 | 單頁 HTML + Vanilla JS + Leaflet（OSM 圖磚） | 免 Maps API key；即時更新用 SSE |
| 資料 | JSON 檔（圖資、情境、mock 信箱） | 不用資料庫 |
| 測試 | `pytest` | 路徑規劃與時間計算必須有單元測試 |

若改用 Vertex AI：區域用 `us-central1`（`asia-east1` 可能沒有部分 Gemini 模型），並用 ADC 驗證而非 API key。

## 4. 目錄結構

```
campuspulse/
├── CLAUDE.md
├── .env.example
├── requirements.txt
├── app/
│   ├── main.py              FastAPI 入口、路由、SSE
│   ├── config.py            環境變數、模式切換（live / replay）
│   ├── clock.py             SimClock / RealClock（回放模式用模擬時間）
│   ├── models.py            Pydantic 資料模型（見第 5 節）
│   ├── graph/
│   │   ├── loader.py        讀取 data/campus_graph.json → networkx 圖
│   │   ├── router.py        plan_route：依 profile 與天氣算權重、最短路徑、ETA
│   │   └── status.py        設施狀態覆寫層（公告、回報造成的停用）
│   ├── agent/
│   │   ├── orchestrator.py  事件 → Agent 迴圈 → 行動；最多 N 步工具呼叫
│   │   ├── tools.py         工具函式實作 + Gemini function declarations
│   │   ├── prompts.py       system prompt 與各任務 prompt
│   │   └── state.py         當日行程、目前路線、待確認動作、事件紀錄
│   ├── sources/             外部資料源；每個都有 live 與 mock 兩種實作
│   │   ├── mailbox.py       公告信讀取 / 寄信（mock：data/mailbox/*.json, outbox）
│   │   ├── weather.py       中央氣象署雨量（mock：回放情境內的天氣事件）
│   │   └── calendar.py      出發提醒（mock：寫入 state 即可）
│   ├── vision/
│   │   ├── schedule.py      課表截圖 → 課程清單（Gemini Flash）
│   │   └── report.py        回報照片 → 設施狀態判讀（Gemini Flash）
│   └── privacy/
│       └── local_profile.py 行動狀態 → 路線條件（Gemma；未啟用時用規則對應）
├── web/
│   ├── index.html           地圖 + 時間軸 + Agent 紀錄 + 確認視窗
│   ├── app.js
│   └── style.css
├── data/
│   ├── campus_graph.json    校園無障礙圖（節點、邊、教室）
│   ├── rooms.json           教室代碼 → 建築、樓層、可用電梯與入口
│   ├── mailbox/             mock 公告信
│   ├── photos/              實拍照片（建圖用）與 Demo 用回報照片
│   └── scenarios/
│       └── demo_wed.json    Demo 回放情境
├── scripts/
│   └── build_graph_draft.py 用 Gemini Pro 讀友善校園地圖圖檔 → 產生圖資草稿（人工校正後存入 data/）
└── tests/
    ├── test_router.py
    ├── test_eta.py
    └── test_orchestrator_replay.py
```

## 5. 資料模型

### 5.1 校園無障礙圖 `data/campus_graph.json`

```json
{
  "nodes": [
    {
      "id": "CSIE_W_ENT",
      "name": "資訊系館西側入口",
      "type": "entrance",
      "building": "CSIE",
      "floor": 1,
      "lat": 22.9969, "lng": 120.2210,
      "step_free": true,
      "source": "official_map",
      "updated_at": "2026-09-20"
    }
  ],
  "edges": [
    {
      "id": "e_012",
      "from": "JCT_03", "to": "CSIE_W_ENT",
      "length_m": 85,
      "stairs": false,
      "slope": "gentle",
      "covered": true,
      "source": "photo_survey",
      "updated_at": "2026-09-20"
    }
  ]
}
```

- `type`：`entrance` | `elevator` | `ramp` | `junction` | `dorm` | `stop`
- `slope`：`flat` | `gentle` | `steep`
- `source`：`official_map` | `photo_survey` | `user_report` | `notice`
- 電梯以節點表示，`floor` 表示可到達樓層範圍時用 `floors: [1,2,3,4]`。
- **座標與節點內容是範例值**，實際資料由 `scripts/build_graph_draft.py` 產生草稿後人工校正。

### 5.2 設施狀態覆寫

公告與回報不直接改圖檔，而是寫入覆寫層，方便回放重置與追溯來源：

```json
{ "target_id": "CSIE_E_ELEV", "status": "closed", "reason": "電梯保養",
  "source": "notice", "source_ref": "mail_003",
  "valid_from": "2026-09-23T08:00", "valid_to": "2026-09-23T17:00" }
```

### 5.3 移動狀態 profile（數值可調，集中放在 `router.py` 常數）

| profile | 速度 (m/s) | 階梯 | 陡坡 | 下雨時無遮蔽路段 |
| --- | --- | --- | --- | --- |
| `wheelchair` | 0.8 | 禁止 | 禁止 | 權重 ×1.5 |
| `crutches` | 0.6 | 禁止 | 權重 ×2 | 權重 ×2，陡坡禁止 |
| `default` | 1.3 | 允許 | 允許 | 權重 ×1.1 |

電梯每次換層加 60 秒；ETA = 路徑時間 + 電梯時間 + 緩衝 3 分鐘。

### 5.4 課程

```json
{ "course": "計算機組織", "weekday": 3, "start": "09:00", "end": "11:50",
  "room": "CSIE-4263", "note": "報告日" }
```

### 5.5 回放情境 `data/scenarios/demo_wed.json`

```json
{
  "date": "2026-09-23",
  "user": { "profile": "crutches", "home_node": "DORM_A" },
  "schedule": "data/scenarios/demo_schedule.json",
  "events": [
    { "t": "07:50", "type": "day_start" },
    { "t": "08:05", "type": "notice_received", "payload": { "mail_id": "mail_003" } },
    { "t": "08:15", "type": "weather_update", "payload": { "rain_from": "08:30", "mm_per_hr": 15 } },
    { "t": "08:25", "type": "user_not_departed" },
    { "t": "08:27", "type": "report_submitted", "payload": { "photo": "data/photos/ramp_blocked.jpg", "node_hint": "RAMP_07" } }
  ]
}
```

回放時 `SimClock` 可調倍速，並支援「現場注入事件」（評審現場拍照上傳即插入 `report_submitted`）。

## 6. 工具函式（Gemini Function Calling）

| 函式 | 回傳 | 實作重點 |
| --- | --- | --- |
| `parse_schedule(image_path)` | 課程清單 | Gemini Flash + 結構化輸出（JSON schema） |
| `locate_room(room_id)` | 建築、樓層、可用入口與電梯 | 查 `rooms.json`，純程式 |
| `plan_route(from_node, room_id, depart_at)` | 路徑節點、ETA、使用到的設施、風險註記 | 純程式；profile 由 state 帶入，不由 LLM 傳 |
| `get_facility_status(ids)` | 各設施目前狀態與來源 | 查覆寫層 |
| `check_facility_notices(since)` | 新公告的結構化摘要 | 讀信後由 Gemini Flash 抽取「哪個設施、何時、何事」 |
| `get_rain_forecast(time_range)` | 降雨時段與強度 | 氣象署或回放資料 |
| `update_facility(id, status, evidence)` | 覆寫紀錄 | 寫入覆寫層並推播給受影響使用者 |
| `set_departure_reminder(time, message)` | 成功與否 | 寫入 state，前端顯示；live 模式可接 Calendar |
| `draft_email(to, purpose, context)` | 草稿 id | 產生草稿放入 `pending_confirmations`，**不寄出** |

`send_email` 不給 LLM 呼叫，只由 `/confirm/{id}` 端點觸發。

## 7. Agent 迴圈

```
事件進來 → orchestrator
  1. 更新 state（事件紀錄、資料覆寫）
  2. 呼叫 Gemini Pro，帶入：system prompt、當日行程、目前路線、事件內容、工具宣告
  3. 手動處理 function calling 迴圈（關閉自動呼叫），最多 6 步
  4. 每一步的「思考摘要、工具呼叫、結果」寫入 agent log 並經 SSE 推給前端
  5. 結束條件：模型回傳最終說明；或達步數上限時回傳目前最佳計畫
```

- System prompt 要點：你是行動不便學生的校園行程代理；時間與距離只能引用工具結果；遇到不確定的設施狀態要說明並建議使用者確認；對外聯絡只能產生草稿。
- 何時用 Flash、何時用 Pro：事件分類、讀信、讀圖、產生通知文字用 Flash；決定要不要改道、提早出發、是否詢問寄信用 Pro。
- 低信心的判讀結果（照片、公告）不自動套用，改為詢問使用者。

## 8. API 端點

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| POST | `/schedule` | 上傳課表截圖 |
| GET | `/state` | 當日行程、目前路線、待確認動作 |
| GET | `/graph` | 圖資 + 目前覆寫狀態（前端畫地圖） |
| POST | `/report` | 上傳回報照片（可帶位置提示） |
| POST | `/replay/start` | 載入情境並開始回放（參數：情境名、倍速） |
| POST | `/replay/inject` | 回放中插入事件 |
| POST | `/confirm/{id}` | 確認或拒絕待確認動作 |
| GET | `/events` | SSE：agent log、路線更新、通知 |

## 9. 前端需求

- 左側地圖：節點與路線；停用設施標紅、回報點標橘；路線改變時有明顯動畫或顏色變化。
- 右上時間軸：顯示模擬時間與已發生事件，附「開始回放」與「現場回報」按鈕。
- 右下 Agent 紀錄：每一步的判斷與工具呼叫，給評審看 Agent 在想什麼。
- 確認視窗：顯示信件草稿，按鈕「寄出」「修改」「取消」。
- 手機寬度可用（Demo 可能用手機拍照回報）。

## 10. 環境變數 `.env.example`

```
GEMINI_API_KEY=
GEMINI_FLASH_MODEL=        # 到 AI Studio 確認 Gemini 3 Flash 的模型 ID
GEMINI_PRO_MODEL=          # 到 AI Studio 確認 Gemini 3 Pro 的模型 ID
CWA_API_KEY=               # 中央氣象署開放資料平台
MODE=replay                # replay | live
USE_GEMMA=false
OLLAMA_MODEL=              # 例如本機的 Gemma 模型名稱
```

氣象署的資料集 ID 與欄位請實作時到開放資料平台確認，不要憑記憶硬寫。

## 11. 實作順序與驗收條件

依序完成；時間不夠時從最後面開始砍。每個里程碑完成後 commit。

| # | 里程碑 | 驗收條件 |
| --- | --- | --- |
| M0 | 專案骨架、設定、SimClock、mock 資料源介面 | `uvicorn` 可啟動；`pytest` 可跑 |
| M1 | 圖資載入與 `plan_route` | 測試：拐杖 profile 不走階梯；電梯關閉時改用另一台；下雨時優先有遮蔽路段；ETA 計算正確 |
| M2 | Agent 迴圈 + 公告信事件 | 回放注入電梯保養公告後，Agent 呼叫工具並產生新路線，log 可見 |
| M3 | 前端地圖、時間軸、Agent 紀錄、SSE | 回放 `demo_wed.json` 可完整看到路線變化 |
| M4 | 天氣事件 | 降雨事件造成路線改走遮蔽路段並提早出發提醒 |
| M5 | 未出門偵測 + 寄信確認流程 | 出現確認視窗；確認後信件進入 outbox；未確認不會寄出 |
| M6 | 課表截圖辨識 | 上傳範例課表截圖可產生正確課程清單 |
| M7 | 照片回報 | 上傳斜坡被擋照片 → 判讀 → 覆寫 → 路線更新；低信心時詢問 |
| M8 | Gemma 本地處理行動狀態 | `USE_GEMMA=true` 時行動狀態不出現在任何雲端請求內容中 |
| M9 | live 模式（Gmail、氣象署即時） | 可選，Demo 不依賴 |

## 12. 資料準備（由隊友平行進行）

- 圖資來源：成大友善校園地圖 PDF（https://main-oga.ncku.edu.tw/var/file/60/1060/img/1868/971171077.pdf），含無障礙斜坡、電梯、廁所、車位。建物位置可參考成大校園 GIS（https://nckumap.ncku.edu.tw/map.php）；內容著作權屬成大，僅供內部建圖參考。
- 範圍：一個校區、5 到 8 棟建築、一間宿舍作為起點。
- 實拍照片：各入口、斜坡、電梯、遮蔽路段，檔名以節點 id 命名。
- mock 公告信：至少 3 封（電梯保養、施工封路、無關的一般公告，用來測試分類）。
- Demo 回報照片：至少 1 張「斜坡被機車擋住」。

## 13. 待隊伍確認

- 隊伍人數、比賽時數與分工
- 比賽規則是否允許賽前準備資料與實拍
- 選定的校區與建築清單
- 是否需要 live 模式，或 Demo 全用回放
