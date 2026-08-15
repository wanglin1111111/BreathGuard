"""边缘层配置：分区、活动强度阈值、状态机时间门槛。

时间门槛以「仿真秒（sim-seconds）」为单位。回放器带一个 speed 加速因子，
让"卫生间静止 15 分钟"这类真实门槛在演示时几秒内走完，而阈值语义不变。

活动强度 intensity 语义（对齐硬件实测 12.2:1 信噪比叙事）：
    静止基线 ≈ 0.02   |   正常活动 ≈ 0.3~0.7   |   跌倒瞬间尖峰 ≥ 0.9
"""
from __future__ import annotations

# ---- 活动强度阈值（归一化 0~1）----
STILL_MAX = 0.08      # 低于此值视为「静止」
ACTIVE_MIN = 0.25     # 高于此值视为「活动」
SPIKE_MIN = 0.90      # 高于此值视为「剧烈波动尖峰」（跌倒候选）

# ---- 存在检测去抖（借鉴 ESPresense 迟滞逻辑）----
PRESENCE_ON_SECONDS = 3    # 连续活动多少 sim-秒判定「有人进入」
PRESENCE_OFF_SECONDS = 20  # 连续静止多少 sim-秒判定「无人离开」

# ---- 疑似跌倒判定 ----
FALL_STILL_SECONDS = 10    # 尖峰后持续静止多少 sim-秒确认「疑似跌倒」
FALL_WATCH_WINDOW = 6      # 尖峰后多少 sim-秒内进入静止才算跌倒（否则视为普通活动）

# ---- 久滞阈值（按分区，sim-秒）----
STILL_TOO_LONG_BY_ZONE = {
    "bathroom": 15 * 60,   # 卫生间 15 分钟
    "bedroom": 120 * 60,   # 卧室白天 120 分钟
    "living": 90 * 60,     # 客厅 90 分钟
    "default": 60 * 60,
}

# ---- 呼吸频率阈值（次/分，基于医学OSINT研究）----
BR_RATE_MIN = 12       # 正常下限
BR_RATE_MAX = 20       # 正常上限
BR_RATE_ELEVATED = 20  # >此值=加快（轻度缺氧）
BR_RATE_SLOW = 8       # <此值=浅慢（重度缺氧/潮式呼吸）
BR_RATE_LOST = 3       # <此值=呼吸消失（呼吸停止）

# ---- Zone 超时配置（sim-秒）----
ZONE2_VOICE_TIMEOUT = 90    # Zone 2 语音确认超时（跌倒+呼吸正常），超时升级Zone 3
ZONE2_VOICE_TIMEOUT_HIGH_RISK = 60  # 高危人群（心脑血管病史）超时缩短至60s
ZONE1_STILL_THRESHOLD = 30 * 60  # Zone 1 静止超时（30分钟），升级关注

# ---- Qwen 认知层配置 ----
# 安全说明：API Key 与工作空间端点均通过环境变量注入，禁止硬编码进源码。
# 未配置 API Key 时 QWEN_ENABLED 自动置为 False，系统降级为规则版（功能不受影响）。
# 本地使用请复制根目录 .env.example 为 .env 并填入真实值，或直接设置系统环境变量。
import os
QWEN_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
# 工作空间专属端点（sk-ws- 格式 Key 使用），标准 DashScope 公共端点为：
#   https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
QWEN_MODEL = os.environ.get("DASHSCOPE_MODEL", "qwen-plus")
QWEN_ENABLED = bool(QWEN_API_KEY)   # 无 Key 自动关闭 Qwen，降级规则版
QWEN_TIMEOUT_S = 10

# ---- 采样周期（回放器每隔多少 sim-秒发一个样本）----
SAMPLE_INTERVAL_S = 1

DB_PATH = "waveguard.db"

# ---- 防盗监测配置 ----
INTRUSION_ENABLED = True              # 是否启用防盗监测
INTRUSION_SILENCE_SECONDS = 300       # 无人/睡眠静默超过多少sim-秒后进入防盗模式
INTRUSION_MOTION_CONFIRM_S = 10       # 防盗模式下持续运动多少sim-秒确认入侵
INTRUSION_CAMERA_LINK = True          # 是否联动摄像头确认


def still_too_long_threshold(zone: str) -> int:
    return STILL_TOO_LONG_BY_ZONE.get(zone, STILL_TOO_LONG_BY_ZONE["default"])
