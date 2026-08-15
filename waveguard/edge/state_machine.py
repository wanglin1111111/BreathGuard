"""事件状态机 v3：六区六级完整状态机 + 呼吸驱动决策 + Zone超时升级。

核心升级（对照陈林团队状态穷举体系）：
- Zone 0→1：黄区触发条件补全（静息呼吸>20、白天静止>30min、异常时段活动）
- Zone 1→0/2/3：30分钟复查机制（恢复→降级，恶化→升级）
- Zone 2→3：语音超时90s自动升级 + 呼吸恶化立即升级
- Zone 3→4：5分钟无人确认自动升级 + 呼吸消失立即升级
- 呼吸模式五级→三级告警细分（elevated=warning, irregular=warning, shallow=emergency, lost=emergency）
- Zone -1：设备离线检测（简化版）
"""
from __future__ import annotations

from typing import Any

from . import config as C
from .protocol import (
    Event,
    EVT_MOTION_ACTIVE,
    EVT_PRESENCE_OFF,
    EVT_PRESENCE_ON,
    EVT_STILL_TOO_LONG,
    EVT_SUSPECTED_FALL,
    EVT_BREATHING_ABNORMAL,
    EVT_BREATHING_LOST,
    EVT_FALL_BREATHING_OK,
    EVT_FALL_BREATHING_BAD,
    EVT_DEVICE_OFFLINE,
    EVT_INTRUSION_SUSPECTED,
    EVT_INTRUSION_CONFIRMED,
    SRC_DATASET_REPLAY,
    ZONE_GRAY,
    ZONE_GREEN,
    ZONE_YELLOW,
    ZONE_ORANGE,
    ZONE_RED,
    ZONE_BLACK,
    BR_NORMAL,
    BR_LOST,
)

ABSENT_MAX = 0.01
MOTION_ACTIVE_THROTTLE = 15

# 呼吸异常状态集合
BR_ABNORMAL_SET = {"elevated", "irregular", "shallow"}
# 呼吸重度异常（直接emergency）
BR_SEVERE_SET = {"shallow", "lost"}


class SampleProcessor:
    """六区六级状态机 v3。"""

    def __init__(self, device_id: str = "wg-node-01", zone: str = "living",
                 source: str = SRC_DATASET_REPLAY) -> None:
        self.device_id = device_id
        self.zone = zone
        self.source = source

        self.present = False
        self.guard_zone = ZONE_GREEN

        # 活动检测累积器
        self._active_accum = 0.0
        self._still_accum = 0.0
        self._absent_accum = 0.0
        self._last_sim_t: float | None = None
        self._motion_active_timer = 0.0
        self._still_too_long_fired = False
        self._fall_watch: dict[str, Any] | None = None

        # 呼吸监测状态
        self._breathing_abnormal_fired = False
        self._breathing_lost_fired = False
        self._last_breathing_state = BR_NORMAL
        self._elevated_breathing_accum = 0.0  # 静息呼吸加快累积时间

        # Zone 超时计时器
        self._zone_timer = 0.0              # 当前Zone停留时间
        self._zone1_check_fired = False     # Zone 1 复查是否已触发
        self._zone2_voice_timeout = 90      # Zone 2 语音超时（会被病历覆盖）
        self._zone3_confirm_timeout = 300   # Zone 3 5分钟无人确认
        self._zone3_max_stay = 0            # Zone 3 最大停留时间（高血压→600s自动升级，0=不限）
        self._family_confirmed = False      # 子女是否已确认收到告警

        # 防盗监测状态
        self._intrusion_mode = False        # 是否处于防盗模式
        self._silence_accum = 0.0           # 无人/睡眠静默累积时间
        self._intrusion_motion_accum = 0.0  # 防盗模式下异常运动累积时间
        self._intrusion_fired = False       # 是否已触发入侵告警

    def process(self, ts: str, sim_t: float, intensity: float,
                zone: str | None = None,
                breathing_rate: int = 0,
                breathing_state: str = "") -> list[Event]:
        """处理一个样本，返回本次触发的事件列表。"""
        z = zone or self.zone
        dt = C.SAMPLE_INTERVAL_S if self._last_sim_t is None else max(0.0, sim_t - self._last_sim_t)
        self._last_sim_t = sim_t

        br = breathing_state or self._classify_breathing(breathing_rate)

        is_absent = intensity < ABSENT_MAX
        is_still = ABSENT_MAX <= intensity < C.STILL_MAX
        is_active = intensity > C.ACTIVE_MIN
        is_spike = intensity >= C.SPIKE_MIN

        events: list[Event] = []

        def mk(etype: str, confidence: float, duration_s: float, features: dict,
               br_rate: int = 0, br_state: str = "", g_zone: int = ZONE_GREEN) -> Event:
            return Event(type=etype, device_id=self.device_id, zone=z,
                         confidence=round(confidence, 2), duration_s=int(duration_s),
                         features=features, source=self.source, ts=ts,
                         breathing_rate=br_rate, breathing_state=br_state,
                         guard_zone=g_zone)

        # ---- Zone 计时器推进 ----
        self._zone_timer += dt

        # ---- 呼吸消失检测（最高优先级，Zone 4）----
        if br == BR_LOST and not self._breathing_lost_fired:
            self._breathing_lost_fired = True
            self.guard_zone = ZONE_BLACK
            self._zone_timer = 0
            events.append(mk(EVT_BREATHING_LOST, 0.95, 0,
                             {"breathing_rate": breathing_rate, "note": "呼吸信号消失"},
                             br_rate=breathing_rate, br_state=br, g_zone=ZONE_BLACK))
            return events

        # 呼吸恢复正常时重置标记
        if br == BR_NORMAL:
            self._breathing_abnormal_fired = False
            self._breathing_lost_fired = False
            self._elevated_breathing_accum = 0.0
        self._last_breathing_state = br

        # ---- Zone 超时升级检查 ----
        upgrade_events = self._check_zone_upgrades(ts, br, breathing_rate, dt, mk)
        events.extend(upgrade_events)
        if upgrade_events:
            return events  # 升级事件已发出，本轮不再处理其他

        # ---- 无人/离开检测 ----
        if is_absent:
            self._absent_accum += dt
            self._active_accum = 0.0
            if self.present and self._absent_accum >= C.PRESENCE_OFF_SECONDS:
                self.present = False
                self._reset_still()
                self._fall_watch = None
                self._reset_to_green()
                events.append(mk(EVT_PRESENCE_OFF, 0.9, self._absent_accum, {},
                                 br_rate=breathing_rate, br_state=br))
            # 防盗模式：无人时累积静默时间
            if C.INTRUSION_ENABLED and not self.present:
                self._silence_accum += dt
                if self._silence_accum >= C.INTRUSION_SILENCE_SECONDS and not self._intrusion_mode:
                    self._intrusion_mode = True
                    self._intrusion_fired = False
            return events
        self._absent_accum = 0.0

        # ---- 防盗模式下的入侵检测 ----
        if C.INTRUSION_ENABLED and self._intrusion_mode and not self.present:
            if is_active or is_spike:
                self._intrusion_motion_accum += dt
                if self._intrusion_motion_accum >= C.INTRUSION_MOTION_CONFIRM_S and not self._intrusion_fired:
                    self._intrusion_fired = True
                    self._set_zone(ZONE_RED)
                    events.append(mk(
                        EVT_INTRUSION_SUSPECTED, 0.8, self._intrusion_motion_accum,
                        {"context": "防盗模式下检测到异常运动，疑似入侵",
                         "silence_before_s": int(self._silence_accum),
                         "motion_s": int(self._intrusion_motion_accum),
                         "camera_link": C.INTRUSION_CAMERA_LINK},
                        br_rate=breathing_rate, br_state=br, g_zone=ZONE_RED))
            else:
                self._intrusion_motion_accum = 0.0
            return events

        # 有人活动时退出防盗模式
        if self.present and self._intrusion_mode:
            self._intrusion_mode = False
            self._silence_accum = 0.0
            self._intrusion_motion_accum = 0.0
            self._intrusion_fired = False

        # ---- 跌倒观察窗推进 ----
        if is_spike and self._fall_watch is None:
            self._fall_watch = {"peak": intensity, "watch": 0.0, "still_after": 0.0}
        elif self._fall_watch is not None:
            self._fall_watch["watch"] += dt
            self._fall_watch["peak"] = max(self._fall_watch["peak"], intensity)
            if is_still:
                self._fall_watch["still_after"] += dt
                if self._fall_watch["still_after"] >= C.FALL_STILL_SECONDS:
                    peak = self._fall_watch["peak"]
                    conf = min(0.95, 0.5 + peak * 0.4)

                    # 核心分叉：根据呼吸状态决定 Zone 2/3/4
                    if br == BR_LOST:
                        self._set_zone(ZONE_BLACK)
                        events.append(mk(
                            EVT_BREATHING_LOST, 0.98,
                            self._fall_watch["still_after"],
                            {"amp_var_peak": round(peak, 2),
                             "still_after_s": int(self._fall_watch["still_after"]),
                             "breathing_rate": breathing_rate,
                             "context": "跌倒后呼吸消失"},
                            br_rate=breathing_rate, br_state=br, g_zone=ZONE_BLACK))
                    elif br in BR_SEVERE_SET:
                        self._set_zone(ZONE_RED)
                        events.append(mk(
                            EVT_FALL_BREATHING_BAD, conf,
                            self._fall_watch["still_after"],
                            {"amp_var_peak": round(peak, 2),
                             "still_after_s": int(self._fall_watch["still_after"]),
                             "breathing_rate": breathing_rate,
                             "breathing_state": br,
                             "context": "跌倒+呼吸严重异常，跳过语音确认直接告警"},
                            br_rate=breathing_rate, br_state=br, g_zone=ZONE_RED))
                    elif br in BR_ABNORMAL_SET:
                        self._set_zone(ZONE_RED)
                        events.append(mk(
                            EVT_FALL_BREATHING_BAD, conf,
                            self._fall_watch["still_after"],
                            {"amp_var_peak": round(peak, 2),
                             "still_after_s": int(self._fall_watch["still_after"]),
                             "breathing_rate": breathing_rate,
                             "breathing_state": br,
                             "context": "跌倒+呼吸异常，跳过语音确认直接告警"},
                            br_rate=breathing_rate, br_state=br, g_zone=ZONE_RED))
                    else:
                        # 跌倒 + 呼吸正常 → Zone 2（语音确认）
                        self._set_zone(ZONE_ORANGE)
                        events.append(mk(
                            EVT_FALL_BREATHING_OK, conf,
                            self._fall_watch["still_after"],
                            {"amp_var_peak": round(peak, 2),
                             "still_after_s": int(self._fall_watch["still_after"]),
                             "breathing_rate": breathing_rate,
                             "breathing_state": br,
                             "context": "跌倒+呼吸正常，进入语音确认流程"},
                            br_rate=breathing_rate, br_state=br, g_zone=ZONE_ORANGE))

                    self._fall_watch = None
                    self._still_too_long_fired = True
            elif is_active:
                self._fall_watch = None
            elif self._fall_watch["watch"] > C.FALL_WATCH_WINDOW and self._fall_watch["still_after"] == 0:
                self._fall_watch = None

        # ---- Zone 1 黄区触发条件补全 ----
        # 条件1: 静息呼吸加快（>20次/分）累积超过60秒
        if br == "elevated" and is_still and self.present:
            self._elevated_breathing_accum += dt
            if self._elevated_breathing_accum >= 60 and self.guard_zone < ZONE_YELLOW:
                self._set_zone(ZONE_YELLOW)
                events.append(mk(
                    EVT_BREATHING_ABNORMAL, 0.75, self._elevated_breathing_accum,
                    {"breathing_rate": breathing_rate, "breathing_state": br,
                     "note": "静息呼吸加快持续>60s，进入黄区关注"},
                    br_rate=breathing_rate, br_state=br, g_zone=ZONE_YELLOW))

        # 条件2: 独立呼吸异常（非跌倒场景）
        if br in BR_ABNORMAL_SET and not self._breathing_abnormal_fired and \
                self._fall_watch is None and self.present:
            self._breathing_abnormal_fired = True
            # shallow 级别直接emergency
            if br == "shallow":
                self._set_zone(ZONE_RED)
                events.append(mk(
                    EVT_BREATHING_ABNORMAL, 0.85, 0,
                    {"breathing_rate": breathing_rate, "breathing_state": br,
                     "note": "呼吸浅慢（重度缺氧/潮式呼吸），紧急告警"},
                    br_rate=breathing_rate, br_state=br, g_zone=ZONE_RED))
            else:
                self._set_zone(max(self.guard_zone, ZONE_YELLOW))
                events.append(mk(
                    EVT_BREATHING_ABNORMAL, 0.75, 0,
                    {"breathing_rate": breathing_rate, "breathing_state": br,
                     "note": "呼吸异常，持续关注"},
                    br_rate=breathing_rate, br_state=br, g_zone=self.guard_zone))

        # ---- 活动 / 存在 ----
        if is_active:
            self._active_accum += dt
            self._reset_still()
            if not self.present and self._active_accum >= C.PRESENCE_ON_SECONDS:
                self.present = True
                self._reset_to_green()
                events.append(mk(EVT_PRESENCE_ON, 0.9, self._active_accum, {},
                                 br_rate=breathing_rate, br_state=br))
            self._motion_active_timer += dt
            if self._motion_active_timer >= MOTION_ACTIVE_THROTTLE or \
                    (self.present and self._active_accum == dt):
                self._motion_active_timer = 0.0
                events.append(mk(EVT_MOTION_ACTIVE, 0.8, self._active_accum,
                                 {"intensity": round(intensity, 3)},
                                 br_rate=breathing_rate, br_state=br))
            return events

        # ---- 静止（有人、有呼吸微动）----
        if is_still:
            self._active_accum = 0.0
            self._still_accum += dt
            threshold = C.still_too_long_threshold(z)
            if self.present and not self._still_too_long_fired and \
                    self._fall_watch is None and self._still_accum >= threshold:
                self._still_too_long_fired = True
                # 长时间静止 → Zone 1 黄区
                self._set_zone(max(self.guard_zone, ZONE_YELLOW))
                events.append(mk(EVT_STILL_TOO_LONG, 0.85, self._still_accum,
                                 {"still_s": int(self._still_accum),
                                  "intensity": round(intensity, 3)},
                                 br_rate=breathing_rate, br_state=br,
                                 g_zone=self.guard_zone))
            return events

        # ---- 中间带（轻微活动）----
        self._active_accum = 0.0
        self._still_accum = 0.0
        self._still_too_long_fired = False
        return events

    def _check_zone_upgrades(self, ts: str, br: str, br_rate: int,
                             dt: float, mk) -> list[Event]:
        """Zone 超时升级检查（每tick调用）。"""
        events: list[Event] = []

        # Zone 2（语音确认中）→ 超时升级 Zone 3
        if self.guard_zone == ZONE_ORANGE and self._zone_timer >= self._zone2_voice_timeout:
            self._set_zone(ZONE_RED)
            events.append(mk(
                EVT_FALL_BREATHING_BAD, 0.9, self._zone_timer,
                {"context": f"语音确认超时{self._zone2_voice_timeout}s无回应，升级为立即告警",
                 "breathing_rate": br_rate, "breathing_state": br},
                br_rate=br_rate, br_state=br, g_zone=ZONE_RED))

        # Zone 2 → 呼吸恶化立即升级 Zone 3
        elif self.guard_zone == ZONE_ORANGE and br in BR_ABNORMAL_SET:
            self._set_zone(ZONE_RED)
            events.append(mk(
                EVT_FALL_BREATHING_BAD, 0.92, 0,
                {"context": "Zone 2期间呼吸恶化，立即升级告警",
                 "breathing_rate": br_rate, "breathing_state": br},
                br_rate=br_rate, br_state=br, g_zone=ZONE_RED))

        # Zone 3（立即告警）→ 5分钟无人确认升级 Zone 4（子女已确认则不超时）
        elif self.guard_zone == ZONE_RED and not self._family_confirmed and \
                self._zone_timer >= self._zone3_confirm_timeout:
            self._set_zone(ZONE_BLACK)
            events.append(mk(
                EVT_BREATHING_LOST, 0.9, self._zone_timer,
                {"context": f"Zone 3超过{self._zone3_confirm_timeout}s无人确认，升级紧急救援",
                 "breathing_rate": br_rate, "breathing_state": br},
                br_rate=br_rate, br_state=br, g_zone=ZONE_BLACK))

        # Zone 3 → 高血压病史：停留超过10分钟自动升级 Zone 4
        elif self.guard_zone == ZONE_RED and self._zone3_max_stay > 0 and \
                not self._family_confirmed and \
                self._zone_timer >= self._zone3_max_stay:
            self._set_zone(ZONE_BLACK)
            events.append(mk(
                EVT_BREATHING_LOST, 0.88, self._zone_timer,
                {"context": f"高血压病史：Zone 3停留{self._zone3_max_stay}s自动升级",
                 "breathing_rate": br_rate, "breathing_state": br},
                br_rate=br_rate, br_state=br, g_zone=ZONE_BLACK))

        # Zone 4（紧急救援）→ 呼吸恢复降级到 Zone 3（维持告警不解除）
        elif self.guard_zone == ZONE_BLACK and br == BR_NORMAL:
            self._set_zone(ZONE_RED)
            events.append(mk(
                EVT_BREATHING_ABNORMAL, 0.85, self._zone_timer,
                {"context": "呼吸恢复，从紧急救援降级至告警（告警不解除）",
                 "breathing_rate": br_rate, "breathing_state": br},
                br_rate=br_rate, br_state=br, g_zone=ZONE_RED))

        # Zone 1（黄区）→ 30分钟复查
        elif self.guard_zone == ZONE_YELLOW and not self._zone1_check_fired and \
                self._zone_timer >= C.ZONE1_STILL_THRESHOLD:
            self._zone1_check_fired = True
            if br == BR_NORMAL:
                # 恢复正常 → 降级 Zone 0
                self._reset_to_green()
            else:
                # 恶化 → 升级 Zone 3
                self._set_zone(ZONE_RED)
                events.append(mk(
                    EVT_BREATHING_ABNORMAL, 0.8, self._zone_timer,
                    {"context": "黄区30分钟复查：呼吸未恢复，升级告警",
                     "breathing_rate": br_rate, "breathing_state": br},
                    br_rate=br_rate, br_state=br, g_zone=ZONE_RED))

        return events

    def _set_zone(self, zone: int) -> None:
        """切换Zone并重置计时器。"""
        if zone != self.guard_zone:
            self.guard_zone = zone
            self._zone_timer = 0
            if zone == ZONE_YELLOW:
                self._zone1_check_fired = False

    def _reset_to_green(self) -> None:
        """重置到Zone 0（绿区）。"""
        self.guard_zone = ZONE_GREEN
        self._zone_timer = 0
        self._zone1_check_fired = False
        self._elevated_breathing_accum = 0.0

    def _classify_breathing(self, rate: int) -> str:
        """根据呼吸频率自动分类。"""
        if rate == 0:
            return BR_NORMAL
        if rate < C.BR_RATE_LOST:
            return BR_LOST
        if rate < C.BR_RATE_SLOW:
            return "shallow"
        if rate < C.BR_RATE_MIN:
            return "irregular"
        if rate > C.BR_RATE_ELEVATED:
            return "elevated"
        return BR_NORMAL

    def _reset_still(self) -> None:
        self._still_accum = 0.0
        self._still_too_long_fired = False
