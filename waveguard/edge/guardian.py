"""告警派生（Qwen 认知层 + 病历个性化 + 规则版降级）。

v3 核心升级：
- 接入病历个性化模块：7种病史调整Zone分级、语音超时、告警内容
- 呼吸模式五级→三级告警细分映射
- Qwen 上下文注入病历信息，输出个性化告警理由
- 规则版降级也包含病历个性化逻辑
"""
from __future__ import annotations

import json
import logging
import urllib.request
import uuid
from datetime import datetime
from typing import Any

from . import config as C
from .medical import MedicalProfile, adjust_zone_for_profile, HX_HEART
from .protocol import (
    EVT_NO_WAKE_UP,
    EVT_STILL_TOO_LONG,
    EVT_SUSPECTED_FALL,
    EVT_BREATHING_ABNORMAL,
    EVT_BREATHING_LOST,
    EVT_FALL_BREATHING_OK,
    EVT_FALL_BREATHING_BAD,
    EVT_INTRUSION_SUSPECTED,
    EVT_INTRUSION_CONFIRMED,
    ZONE_ORANGE,
    ZONE_RED,
    ZONE_BLACK,
    ZONE_YELLOW,
)

logger = logging.getLogger("waveguard.guardian")

# ---- 呼吸模式→告警级别细分映射（五级→三级）----
BREATHING_ALERT_MAP = {
    "normal":   None,           # 正常 → 不告警
    "elevated": ("warning", "关注", "呼吸加快（{br_rate}次/分），提示轻度缺氧或发热/焦虑，持续关注"),
    "irregular": ("warning", "关注", "呼吸节律不整（{br_rate}次/分），呼吸中枢可能受累，通知子女"),
    "shallow":  ("emergency", "紧急", "呼吸浅慢（{br_rate}次/分），重度缺氧/潮式呼吸，紧急呼叫"),
    "lost":     ("emergency", "紧急", "呼吸消失（{br_rate}次/分），呼吸骤停，建议立即拨打120"),
}

# ---- 规则版降级表（Qwen 不可用时使用）----
_RULES: dict[str, tuple[str, str, str]] = {
    EVT_FALL_BREATHING_OK: (
        "warning", "voice_checking",
        "检测到跌倒（尖峰{amp_var_peak}、静止{still_after_s}s），"
        "呼吸频率{breathing_rate}次/分处于正常范围，"
        "已发起语音确证「您还好吗」，{timeout}秒无回应将升级告警。",
    ),
    EVT_FALL_BREATHING_BAD: (
        "emergency", "notifying_family",
        "检测到跌倒且呼吸异常（{breathing_state}，{breathing_rate}次/分），"
        "跳过语音确证，立即告警子女。"
        "医学依据：跌倒后呼吸异常提示可能存在脑缺氧或心源性病因。",
    ),
    EVT_BREATHING_LOST: (
        "emergency", "emergency_call",
        "呼吸信号消失（{breathing_rate}次/分），"
        "可能发生呼吸骤停或严重脑缺氧，建议立即拨打120。",
    ),
    EVT_BREATHING_ABNORMAL: (
        "warning", "monitoring",
        "呼吸异常（{breathing_state}，{breathing_rate}次/分），"
        "已提升监测频率，持续关注。",
    ),
    EVT_SUSPECTED_FALL: (
        "emergency", "voice_checking",
        "检测到剧烈波动后陷入持续静止（尖峰{amp_var_peak}、静止{still_after_s}s），"
        "符合疑似跌倒特征，已发起语音确证。",
    ),
    EVT_STILL_TOO_LONG: (
        "warning", "voice_checking",
        "{zone} 区域持续静止 {still_s}s 超过分区阈值，发起语音问候确认状态。",
    ),
    EVT_NO_WAKE_UP: (
        "warning", "pending",
        "超过日常起床时间仍无活动，需关注。",
    ),
    EVT_INTRUSION_SUSPECTED: (
        "warning", "notifying_family",
        "防盗模式下检测到异常运动（静默{silence_before_s}s后持续运动{motion_s}s），"
        "疑似非法入侵，已通知子女。"
        "可联动摄像头确认物体形态。",
    ),
    EVT_INTRUSION_CONFIRMED: (
        "emergency", "notifying_family",
        "入侵确认：异常运动持续且伴随异常信号特征，"
        "已通知子女并建议查看摄像头画面。",
    ),
}

# ---- Qwen 系统提示词（医学知识+病历上下文注入）----
_SYSTEM_PROMPT = """你是护院鹅系统的认知决策引擎，负责分析老人的守护事件并做出告警决策。

## 核心原则
1. 呼吸感知优先：呼吸异常比活动异常更危险，因为脑缺氧后老人可能无法呼救
2. 病历个性化：不同病史的老人跌倒原因概率不同，需结合病历调整策略
3. 分级告警（必须严格遵守，不得自行降级或升级）

## level 严格映射表（必须按此表返回 level）
| 事件场景 | level | state |
|---------|-------|-------|
| 跌倒+呼吸正常+无心梗史 | warning | voice_checking |
| 跌倒+呼吸正常+心梗/心律失常史 | emergency | notifying_family |
| 跌倒+呼吸异常（elevated/irregular） | emergency | notifying_family |
| 跌倒+呼吸异常（shallow） | emergency | emergency_call |
| 呼吸消失（lost） | emergency | emergency_call |
| 呼吸加快（elevated，无跌倒） | warning | monitoring |
| 呼吸节律不整（irregular，无跌倒） | warning | monitoring |
| 呼吸浅慢（shallow，无跌倒） | emergency | emergency_call |
| 久滞不动 | warning | voice_checking |
| 异常时段未起床 | warning | pending |
| 防盗模式异常运动 | warning | notifying_family |
| 入侵确认 | emergency | notifying_family |

## state 标准值（只能从以下列表中选择）
- voice_checking: 正在语音确认
- notifying_family: 已通知子女
- emergency_call: 紧急呼叫120
- monitoring: 持续监测中
- pending: 待确认

## 呼吸模式五级分类
- normal（12-20次/分）→ 不告警
- elevated（>20次/分，轻度缺氧）→ 关注
- irregular（节律不整，中度缺氧）→ 通知子女
- shallow（浅慢/潮式呼吸，重度缺氧）→ 紧急呼叫
- lost（消失）→ 紧急呼叫

## 输出格式（严格JSON，不要加markdown代码块）
{"level": "warning或emergency", "state": "上述标准值之一", "reason": "决策理由（中文，含病史上下文和医学依据，80字内）", "alert_tag": "告警标签（如心源性/脑源性/环境性，无则空字符串）", "suspected_cause": "疑似跌倒原因（如心源性晕厥/脑缺血/低血糖等，无则空字符串）"}
"""


def decide(event: dict[str, Any], profile: MedicalProfile | None = None) -> dict[str, Any] | None:
    """把一条事件映射成告警；无需告警则返回 None。

    v3 新增：profile 参数，传入病历信息实现个性化告警。
    """
    if profile is None:
        profile = MedicalProfile()

    # 病历个性化调整（跌倒类事件）
    adjusted = None
    if event["type"] in (EVT_FALL_BREATHING_OK, EVT_FALL_BREATHING_BAD, EVT_SUSPECTED_FALL):
        adjusted = adjust_zone_for_profile(event, profile)

    # 入侵类事件不需要呼吸/病历调整，直接走规则/Qwen
    if event["type"] in (EVT_INTRUSION_SUSPECTED, EVT_INTRUSION_CONFIRMED):
        adjusted = None

    # 独立呼吸异常告警细分
    if event["type"] == EVT_BREATHING_ABNORMAL:
        br_state = event.get("breathing_state", "elevated")
        br_rate = event.get("breathing_rate", 0)
        alert_map = BREATHING_ALERT_MAP.get(br_state)
        if alert_map and alert_map[0] == "emergency":
            # shallow 级别升级为 emergency
            event = {**event, "guard_zone": ZONE_RED}

    rule = _RULES.get(event["type"])
    if rule is None:
        return None

    # 尝试 Qwen 认知层
    if C.QWEN_ENABLED:
        qwen_result = _qwen_decide(event, profile, adjusted)
        if qwen_result is not None:
            return qwen_result

    # 降级：规则版（含病历个性化）
    return _rule_decide(event, rule, profile, adjusted)


def _rule_decide(event: dict[str, Any], rule: tuple[str, str, str],
                 profile: MedicalProfile | None = None,
                 adjusted: dict[str, Any] | None = None) -> dict[str, Any]:
    """规则版告警派生（含病历个性化）。"""
    level, state, reason_tpl = rule
    ctx = {**event, **(event.get("features") or {})}

    # 注入病历个性化信息
    if adjusted:
        # 心梗→跳过语音，升级为emergency
        if adjusted.get("skip_voice"):
            level = "emergency"
            state = "notifying_family"
        # 调整后的Zone
        ctx["guard_zone"] = adjusted.get("zone", ctx.get("guard_zone", 0))
        timeout = adjusted.get("voice_timeout", 90)
        ctx["timeout"] = timeout if timeout > 0 else "跳过"
    else:
        ctx["timeout"] = profile.voice_timeout if profile else 90

    try:
        reason = reason_tpl.format(**{k: ctx.get(k, "?") for k in _fields(reason_tpl)})
    except Exception:
        reason = reason_tpl

    # 附加病历信息到告警
    if adjusted and adjusted.get("suspected_cause"):
        reason += f"\n疑似原因：{adjusted['suspected_cause']}"
        if adjusted.get("alert_tag"):
            reason += f"（{adjusted['alert_tag']}）"
    if adjusted and adjusted.get("advice"):
        reason += "\n建议处理：" + "；".join(adjusted["advice"][:3])

    return {
        "alert_id": f"alt_{uuid.uuid4().hex[:8]}",
        "event_id": event.get("event_id"),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "level": level,
        "state": state,
        "agent_reason": reason,
        "agent_source": "rule",
        "alert_tag": (adjusted or {}).get("alert_tag", ""),
        "suspected_cause": (adjusted or {}).get("suspected_cause", ""),
        "elder_name": profile.elder_name if profile else "妈妈",
        "closed_at": None,
        "closed_by": None,
    }


def _qwen_decide(event: dict[str, Any], profile: MedicalProfile,
                 adjusted: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """调用 Qwen-plus 大模型进行认知决策（含病历上下文）。"""
    user_msg = _build_event_context(event, profile, adjusted)
    payload = {
        "model": C.QWEN_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.3,
        "max_tokens": 300,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {C.QWEN_API_KEY}",
    }
    req = urllib.request.Request(
        f"{C.QWEN_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=C.QWEN_TIMEOUT_S) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        decision = _parse_qwen_response(content)
        if decision is None:
            logger.warning("Qwen 返回格式异常，降级规则版: %s", content)
            return None
        # Qwen 返回的 alert_tag/suspected_cause 优先，adjusted 作为兜底
        qwen_tag = decision.get("alert_tag", "") or (adjusted or {}).get("alert_tag", "")
        qwen_cause = decision.get("suspected_cause", "") or (adjusted or {}).get("suspected_cause", "")
        return {
            "alert_id": f"alt_{uuid.uuid4().hex[:8]}",
            "event_id": event.get("event_id"),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "level": decision["level"],
            "state": decision["state"],
            "agent_reason": decision["reason"],
            "agent_source": "qwen-plus",
            "alert_tag": qwen_tag,
            "suspected_cause": qwen_cause,
            "elder_name": profile.elder_name,
            "closed_at": None,
            "closed_by": None,
        }
    except Exception as e:
        logger.warning("Qwen API 调用失败，降级规则版: %s", e)
        return None


def _build_event_context(event: dict[str, Any], profile: MedicalProfile,
                         adjusted: dict[str, Any] | None = None) -> str:
    """构建发送给 Qwen 的事件上下文（含病历信息）。"""
    features = event.get("features") or {}
    ctx = {
        "事件类型": event.get("type", ""),
        "老人姓名": profile.elder_name,
        "年龄": profile.age,
        "既往疾病": profile.conditions,
        "当前用药": profile.medications,
        "多重用药": profile.is_multi_medication,
        "语音超时配置": f"{profile.voice_timeout}秒" if profile.voice_timeout > 0 else "跳过语音",
        "分区": event.get("zone", ""),
        "置信度": event.get("confidence", 0),
        "呼吸频率": event.get("breathing_rate", 0),
        "呼吸状态": event.get("breathing_state", "未检测"),
        "守护Zone": event.get("guard_zone", 0),
        "持续秒数": event.get("duration_s", 0),
        "特征": features,
    }
    if adjusted:
        ctx["病历调整"] = {
            "疑似原因": adjusted.get("suspected_cause", ""),
            "告警标签": adjusted.get("alert_tag", ""),
            "建议处理": adjusted.get("advice", []),
        }
    return json.dumps(ctx, ensure_ascii=False, indent=2)


def _parse_qwen_response(content: str) -> dict[str, str] | None:
    """解析 Qwen 返回的 JSON 决策。"""
    try:
        d = json.loads(content)
        if "level" in d and "reason" in d:
            d.setdefault("state", "pending")
            return d
    except json.JSONDecodeError:
        pass
    import re
    m = re.search(r'\{[^}]+\}', content)
    if m:
        try:
            d = json.loads(m.group())
            if "level" in d and "reason" in d:
                d.setdefault("state", "pending")
                return d
        except json.JSONDecodeError:
            pass
    return None


def _fields(tpl: str) -> list[str]:
    import string
    return [f for _, f, _, _ in string.Formatter().parse(tpl) if f]
