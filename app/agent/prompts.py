"""System prompt and the state briefing handed to the model.

Kept short on purpose: every extra sentence is tokens on every step of the loop,
and the hard rules are enforced in code anyway — the prompt only has to stop the
model from *trying* to do arithmetic or send mail.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

SYSTEM_PROMPT = """你是 CampusPulse，行動不便學生的校園行程代理。

鐵則：
1. 時間、距離、ETA 只能引用工具回傳的數字。你自己不要算，也不要估。
2. 設施狀態（電梯、斜坡、入口）只能引用 get_facility_status 或 check_facility_notices 的結果，並說出來源。資料不足就說「不確定」，不要推測。
3. 要換方案時，先呼叫 compare_travel_options 看候選，再用 commit_plan 送出你選的方案。程式會驗證可行性，選到不可行的方案會被駁回。
4. 對外聯絡（寄信）你不能執行，只能交給使用者確認的流程。
5. 使用者的行動狀態不要寫進任何文字輸出，只談路線條件。

風格：最後用兩到三句繁體中文說明你做了什麼、為什麼改變、使用者現在該做什麼。不要列出工具名稱。"""


def state_briefing(
    *,
    now: datetime,
    profile: str,
    commitment: dict[str, Any] | None,
    current_plan: dict[str, Any] | None,
    signals: dict[str, dict[str, Any]],
    trigger: dict[str, Any] | None,
    origin: str,
) -> str:
    """One compact user message describing the world as it is right now."""
    lines = [f"現在時間 {now:%H:%M}（{now:%Y-%m-%d}）。使用者目前位置 {origin}，路線條件 profile={profile}。"]

    if commitment:
        lines.append(
            f"下一個行程：{commitment['title']}，{commitment['start'][11:16]} 於 {commitment['room']}，"
            f"重要性 {commitment['importance']}。"
        )
    else:
        lines.append("今日沒有後續行程。")

    if current_plan:
        selected = current_plan["selected"]
        lines.append(
            f"目前計畫：{selected['mode']}，{selected['depart_at'][11:16]} 出發，"
            f"預計 {selected['conservative_eta'][11:16]} 抵達，狀態 {current_plan['status']}。"
        )
    else:
        lines.append("目前還沒有計畫。")

    if trigger:
        lines.append(
            f"剛發生的事件：{trigger.get('kind')}，內容 {trigger.get('new_value')}，"
            f"預期影響 {trigger.get('materiality')}。"
        )

    readable = []
    for kind, signal in signals.items():
        if signal.get("error"):
            readable.append(f"{kind}=無資料（{signal['error']}）")
        else:
            readable.append(
                f"{kind}={signal.get('value')}（{signal.get('freshness', 'fixture')}，"
                f"觀測 {signal['observed_at'][11:16]}）"
            )
    lines.append("目前訊號：" + "；".join(readable))
    lines.append("請判斷目前計畫是否還成立。若需要更動，用工具取得依據後 commit_plan；若不需要，直接說明理由。")
    return "\n".join(lines)


NOTICE_EXTRACTION_PROMPT = """從這封校內公告判斷它是否影響某個無障礙設施的可用性。

只在公告明確指出某個設施停用、封閉或恢復時，才回報 affects_facility=true。
一般活動公告、報名通知、與設施無關的訊息一律 affects_facility=false。
facility_hint 請用公告原文描述的設施名稱（例如「資訊系館西側電梯」），不要自己編節點代碼。
時間請用 ISO 8601，含 +08:00 時區；公告沒寫結束時間就留空。"""

NOTICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "affects_facility": {"type": "boolean"},
        "facility_hint": {"type": "string"},
        "status": {"type": "string", "enum": ["closed", "open", "degraded", "unknown"]},
        "reason": {"type": "string"},
        "valid_from": {"type": "string"},
        "valid_to": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["affects_facility", "confidence"],
}
