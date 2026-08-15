"""语音确证模块：Zone 2 语音确认流程管理。

Zone 2（跌倒+呼吸正常）时触发语音确证：
1. 边缘层通过 WebSocket 通知前端 TTS 朗读"您还好吗？"
2. 前端显示倒计时 + 两个按钮（我没事 / 我需要帮助）
3. 老人回应 → 正常消警 / 求助升级 / 超时自动升级 Zone 3
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


# 语音确证状态
VOICE_IDLE = "idle"              # 未在确证
VOICE_WAITING = "waiting"        # 等待老人回应
VOICE_OK = "ok"                  # 老人回应"我没事"
VOICE_HELP = "help"              # 老人回应"我需要帮助"
VOICE_TIMEOUT = "timeout"        # 超时无回应


class VoiceConfirmSession:
    """一次语音确证会话。"""

    def __init__(self, elder_name: str = "奶奶", timeout_s: int = 90) -> None:
        self.elder_name = elder_name
        self.timeout_s = timeout_s
        self.state = VOICE_IDLE
        self.started_at: str | None = None
        self.elapsed_s: float = 0.0
        self.greeting: str = ""

    def start(self) -> dict[str, Any]:
        """启动语音确证，返回前端需要的播报数据。"""
        self.state = VOICE_WAITING
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.elapsed_s = 0.0
        self.greeting = f"{self.elder_name}，您还好吗？我是护院鹅，听到请回答。"
        return {
            "active": True,
            "greeting": self.greeting,
            "timeout_s": self.timeout_s,
            "started_at": self.started_at,
        }

    def tick(self, dt: float) -> bool:
        """每 tick 推进超时计时，返回是否超时。"""
        if self.state != VOICE_WAITING:
            return False
        self.elapsed_s += dt
        if self.elapsed_s >= self.timeout_s:
            self.state = VOICE_TIMEOUT
            return True
        return False

    def respond(self, answer: str) -> str:
        """老人回应：ok=没事，help=需要帮助。"""
        if self.state != VOICE_WAITING:
            return self.state
        if answer == "ok":
            self.state = VOICE_OK
        elif answer == "help":
            self.state = VOICE_HELP
        return self.state

    def reset(self) -> None:
        """重置确证会话。"""
        self.state = VOICE_IDLE
        self.started_at = None
        self.elapsed_s = 0.0

    def is_active(self) -> bool:
        return self.state == VOICE_WAITING

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "greeting": self.greeting,
            "timeout_s": self.timeout_s,
            "elapsed_s": round(self.elapsed_s, 1),
            "started_at": self.started_at,
        }
