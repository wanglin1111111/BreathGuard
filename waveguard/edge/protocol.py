"""事件流协议 Event Schema v1（护院鹅）。

这是边缘层输出、认知层/子女端消费的唯一数据契约，
与《01-系统架构.md》§3 严格对齐。任何一端都不得私自增删字段含义。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

# ---- 事件类型字典（Demo 范围，见 01-系统架构.md §3.1）----
EVT_PRESENCE_ON = "presence_on"
EVT_PRESENCE_OFF = "presence_off"
EVT_MOTION_ACTIVE = "motion_active"
EVT_STILL_TOO_LONG = "still_too_long"
EVT_SUSPECTED_FALL = "suspected_fall"
EVT_NO_WAKE_UP = "no_wake_up"
EVT_ACTIVITY_DROP = "activity_drop"
EVT_DEVICE_OFFLINE = "device_offline"
# 呼吸事件（感知驱动决策核心）
EVT_BREATHING_ABNORMAL = "breathing_abnormal"   # 呼吸异常（加快/不整/浅慢）
EVT_BREATHING_LOST = "breathing_lost"           # 呼吸消失（Zone 4 紧急）
EVT_FALL_BREATHING_OK = "fall_breathing_ok"      # 跌倒+呼吸正常（Zone 2 语音确认）
EVT_FALL_BREATHING_BAD = "fall_breathing_bad"    # 跌倒+呼吸异常（Zone 3 立即告警）
# 入侵事件（防盗监测扩展能力）
EVT_INTRUSION_SUSPECTED = "intrusion_suspected"  # 疑似入侵（睡眠/外出时异常运动）
EVT_INTRUSION_CONFIRMED = "intrusion_confirmed"  # 入侵确认（联动摄像头/持续异常）

EVENT_TYPES = {
    EVT_PRESENCE_ON,
    EVT_PRESENCE_OFF,
    EVT_MOTION_ACTIVE,
    EVT_STILL_TOO_LONG,
    EVT_SUSPECTED_FALL,
    EVT_NO_WAKE_UP,
    EVT_ACTIVITY_DROP,
    EVT_DEVICE_OFFLINE,
    EVT_BREATHING_ABNORMAL,
    EVT_BREATHING_LOST,
    EVT_FALL_BREATHING_OK,
    EVT_FALL_BREATHING_BAD,
    EVT_INTRUSION_SUSPECTED,
    EVT_INTRUSION_CONFIRMED,
}

# ---- 守护Zone分级（六区六级状态机）----
ZONE_GRAY = -1   # 灰区：系统异常
ZONE_GREEN = 0   # 绿区：正常
ZONE_YELLOW = 1  # 黄区：关注
ZONE_ORANGE = 2  # 橙区：语音确认（跌倒+呼吸正常）
ZONE_RED = 3     # 红区：立即告警（跌倒+呼吸异常）
ZONE_BLACK = 4   # 黑区：紧急救援（呼吸消失）

ZONE_LABELS = {
    ZONE_GRAY: "系统异常",
    ZONE_GREEN: "正常",
    ZONE_YELLOW: "关注",
    ZONE_ORANGE: "语音确认中",
    ZONE_RED: "立即告警",
    ZONE_BLACK: "紧急救援",
}

# ---- 呼吸状态分类（五级）----
BR_NORMAL = "normal"          # 正常（12-20次/分）
BR_ELEVATED = "elevated"      # 加快（>20次/分，轻度缺氧）
BR_IRREGULAR = "irregular"    # 不整（中度缺氧）
BR_SHALLOW = "shallow"        # 浅慢（重度缺氧/潮式呼吸）
BR_LOST = "lost"              # 消失（呼吸停止）

# ---- 事件来源（三种来源走完全相同的下游链路）----
SRC_CSI_LIVE = "csi_live"          # 硬件轨
SRC_DATASET_REPLAY = "dataset_replay"  # 模拟轨（本 MVP）
SRC_DEMO_INJECT = "demo_inject"    # 演示注入


@dataclass
class Event:
    """一条守护事件。字段与 Event Schema v2 一一对应（v2 增加呼吸+Zone）。"""

    type: str
    device_id: str = "wg-node-01"
    zone: str = "living"
    confidence: float = 1.0
    duration_s: int = 0
    features: dict[str, Any] = field(default_factory=dict)
    capability_level: str = "L1"      # 恒为 L1，预留 L2/L3
    source: str = SRC_DATASET_REPLAY
    ts: str = ""
    event_id: str = ""
    # v2 新增：呼吸感知维度
    breathing_rate: int = 0            # 呼吸频率（次/分），0=未检测
    breathing_state: str = ""          # normal/elevated/irregular/shallow/lost
    # v2 新增：守护Zone分级
    guard_zone: int = 0                # -1~4，对应六区六级状态机

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型: {self.type}")
        if not self.ts:
            self.ts = datetime.now().astimezone().isoformat(timespec="seconds")
        if not self.event_id:
            stamp = self.ts.replace("-", "").replace(":", "").replace("+", "")[:15]
            self.event_id = f"evt_{stamp}_{uuid.uuid4().hex[:6]}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
