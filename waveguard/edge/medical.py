"""病历个性化模块 — 病史驱动检测策略调整。

基于陈林团队《跌倒原因与病历个性化方案》实现：
- 7种病史的Zone调整规则（心梗/脑梗/癫痫/糖尿病/帕金森/多重用药/无特殊病史）
- 语音超时时间个性化（心梗跳过语音，脑梗60s，标准90s）
- 个性化告警模板（告警内容含病史上下文+疑似原因+建议处理）
- 病历数据模型 + SQLite 持久化
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


# ---- 病史类型枚举 ----
HX_HEART = "heart"           # 心梗/心律失常/心力衰竭
HX_STROKE = "stroke"         # 脑梗/TIA/脑出血
HX_EPILEPSY = "epilepsy"     # 癫痫
HX_DIABETES = "diabetes"     # 糖尿病
HX_PARKINSON = "parkinson"   # 帕金森
HX_ALZHEIMER = "alzheimer"   # 阿尔茨海默病
HX_HYPERTENSION = "hypertension"  # 高血压
HX_ANEMIA = "anemia"         # 严重贫血

ALL_CONDITIONS = [
    HX_HEART, HX_STROKE, HX_EPILEPSY, HX_DIABETES,
    HX_PARKINSON, HX_ALZHEIMER, HX_HYPERTENSION, HX_ANEMIA,
]

# ---- 用药类型枚举 ----
MED_ANTIHYPERTENSIVE = "antihypertensive"   # 降压药
MED_HYPOGLYCEMIC = "hypoglycemic"            # 降糖药/胰岛素
MED_SEDATIVE = "sedative"                    # 镇静催眠药
MED_ANTIDEPRESSANT = "antidepressant"        # 抗抑郁药
MED_ANTIPARKINSONIAN = "antiparkinsonian"    # 抗帕金森药
MED_ANTIEPILEPTIC = "antiepileptic"          # 抗癫痫药
MED_DIURETIC = "diuretic"                    # 利尿剂
MED_ANALGESIC = "analgesic"                  # 镇痛药

ALL_MEDICATIONS = [
    MED_ANTIHYPERTENSIVE, MED_HYPOGLYCEMIC, MED_SEDATIVE,
    MED_ANTIDEPRESSANT, MED_ANTIPARKINSONIAN, MED_ANTIEPILEPTIC,
    MED_DIURETIC, MED_ANALGESIC,
]

MULTI_MED_THRESHOLD = 4  # 多重用药阈值（≥4种）


# ---- 开放式疾病注册表 ----
@dataclass
class CustomDisease:
    """自定义疾病定义（开放式接口，覆盖预设7种之外的疾病）。"""
    code: str                        # 疾病代码（英文唯一标识，如 "copd"）
    name: str                        # 疾病名称（中文，如"慢性阻塞性肺病"）
    category: str = ""               # 疾病类别（如"呼吸系统"/"心血管"/"代谢"/"神经"）
    description: str = ""            # 疾病特征描述
    fall_risk_note: str = ""         # 对跌倒的影响说明
    breathing_impact: str = ""       # 对呼吸的影响说明
    advice: list[str] = field(default_factory=list)  # 告警时的建议处理
    voice_timeout_override: int = 0  # 语音超时覆盖（0=不覆盖，用默认值）
    skip_voice: bool = False         # 是否跳过语音确认
    zone3_max_stay: int = 0          # Zone 3 最大停留时间（0=不限）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 全局注册表：自定义疾病（运行时可动态增删）
_custom_diseases: dict[str, CustomDisease] = {}


def register_disease(disease: CustomDisease) -> None:
    """注册一个自定义疾病到全局表。"""
    _custom_diseases[disease.code] = disease


def unregister_disease(code: str) -> bool:
    """移除一个自定义疾病。"""
    if code in _custom_diseases:
        del _custom_diseases[code]
        return True
    return False


def get_custom_disease(code: str) -> CustomDisease | None:
    """获取一个自定义疾病定义。"""
    return _custom_diseases.get(code)


def list_custom_diseases() -> list[dict[str, Any]]:
    """列出所有已注册的自定义疾病。"""
    return [d.to_dict() for d in _custom_diseases.values()]


def get_disease_strategy(code: str) -> dict[str, Any]:
    """获取疾病的告警策略（含预设疾病和自定义疾病）。

    优先查自定义注册表，找不到再查预设规则。
    """
    custom = _custom_diseases.get(code)
    if custom:
        return {
            "source": "custom",
            "name": custom.name,
            "category": custom.category,
            "description": custom.description,
            "fall_risk_note": custom.fall_risk_note,
            "breathing_impact": custom.breathing_impact,
            "advice": custom.advice,
            "voice_timeout_override": custom.voice_timeout_override,
            "skip_voice": custom.skip_voice,
            "zone3_max_stay": custom.zone3_max_stay,
        }

    # 预设疾病策略
    preset_map = {
        HX_HEART: {"source": "preset", "name": "心梗/心律失常", "category": "心血管",
                    "fall_risk_note": "心源性晕厥风险极高，恶化极快（20-30秒内呼吸可能停止）",
                    "breathing_impact": "心衰可导致呼吸加快或潮式呼吸",
                    "advice": ["立即电话联系老人确认意识", "如无应答立即前往查看", "准备拨打120"],
                    "voice_timeout_override": 0, "skip_voice": True, "zone3_max_stay": 0},
        HX_STROKE: {"source": "preset", "name": "脑梗/TIA", "category": "神经",
                     "fall_risk_note": "脑源性晕厥，可能意识清醒但无法回应",
                     "breathing_impact": "脑干梗死可导致呼吸节律异常",
                     "advice": ["立即电话联系老人", "可能清醒但肢体无法动弹", "不建议等待语音回应"],
                     "voice_timeout_override": 45, "skip_voice": False, "zone3_max_stay": 0},
        HX_EPILEPSY: {"source": "preset", "name": "癫痫", "category": "神经",
                       "fall_risk_note": "癫痫发作期可跌倒，发作后意识模糊",
                       "breathing_impact": "发作期呼吸可能暂停>30秒",
                       "advice": ["保护老人头部", "保持侧卧位防止误吸", "如呼吸>30s未恢复拨打120"],
                       "voice_timeout_override": 60, "skip_voice": False, "zone3_max_stay": 0},
        HX_DIABETES: {"source": "preset", "name": "糖尿病", "category": "代谢",
                       "fall_risk_note": "低血糖可导致跌倒和意识丧失",
                       "breathing_impact": "低血糖昏迷时呼吸可能浅慢",
                       "advice": ["立即电话联系", "可能为低血糖", "准备含糖食物", "如无法唤醒拨打120"],
                       "voice_timeout_override": 60, "skip_voice": False, "zone3_max_stay": 0},
        HX_PARKINSON: {"source": "preset", "name": "帕金森", "category": "神经",
                        "fall_risk_note": "体位性低血压和步态异常导致跌倒",
                        "breathing_impact": "通常不影响呼吸频率",
                        "advice": ["确认意识是否清醒", "缓慢扶起防止再次跌倒"],
                        "voice_timeout_override": 90, "skip_voice": False, "zone3_max_stay": 0},
        HX_HYPERTENSION: {"source": "preset", "name": "高血压", "category": "心血管",
                           "fall_risk_note": "高血压急症可导致跌倒",
                           "breathing_impact": "通常不影响呼吸",
                           "advice": ["确认老人意识和血压", "如意识模糊立即前往"],
                           "voice_timeout_override": 90, "skip_voice": False, "zone3_max_stay": 600},
        HX_ALZHEIMER: {"source": "preset", "name": "阿尔茨海默病", "category": "神经",
                        "fall_risk_note": "认知障碍导致判断力下降，跌倒风险增加",
                        "breathing_impact": "通常不影响呼吸",
                        "advice": ["确认老人意识和位置", "可能无法准确描述情况"],
                        "voice_timeout_override": 90, "skip_voice": False, "zone3_max_stay": 0},
        HX_ANEMIA: {"source": "preset", "name": "严重贫血", "category": "血液",
                     "fall_risk_note": "贫血导致脑供氧不足，可引起晕厥跌倒",
                     "breathing_impact": "代偿性呼吸加快",
                     "advice": ["确认老人意识", "可能为脑供氧不足", "如意识模糊拨打120"],
                     "voice_timeout_override": 60, "skip_voice": False, "zone3_max_stay": 0},
    }
    return preset_map.get(code, {"source": "unknown", "name": code, "category": "",
                                  "description": "", "fall_risk_note": "", "breathing_impact": "",
                                  "advice": [], "voice_timeout_override": 0,
                                  "skip_voice": False, "zone3_max_stay": 0})


@dataclass
class MedicalProfile:
    """老人健康档案（病历）。"""
    elder_name: str = "妈妈"
    age: int = 75
    conditions: list[str] = field(default_factory=list)    # 既往疾病
    medications: list[str] = field(default_factory=list)   # 当前用药
    fall_history: int = 0          # 既往跌倒次数
    syncope_history: int = 0       # 既往晕厥次数
    family_sudden_death: bool = False  # 心脏猝死家族史
    wake_time: str = "06:30"       # 起床时间
    bed_time: str = "21:30"        # 就寝时间
    updated_at: str = ""

    @property
    def is_multi_medication(self) -> bool:
        return len(self.medications) >= MULTI_MED_THRESHOLD

    @property
    def is_high_risk(self) -> bool:
        """高危人群：心梗/心律失常 → 跌倒时跳过语音直接Zone 3"""
        return HX_HEART in self.conditions

    @property
    def voice_timeout(self) -> int:
        """语音确认超时时间（秒）。"""
        if HX_HEART in self.conditions:
            return 0   # 跳过语音
        if HX_STROKE in self.conditions or HX_EPILEPSY in self.conditions or HX_DIABETES in self.conditions:
            return 60  # 高危缩短至60s
        return 90      # 标准90s

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["is_multi_medication"] = self.is_multi_medication
        d["is_high_risk"] = self.is_high_risk
        d["voice_timeout"] = self.voice_timeout
        return d


# ---- 病史→Zone调整规则 ----
def adjust_zone_for_profile(fall_event: dict[str, Any], profile: MedicalProfile) -> dict[str, Any]:
    """根据病历调整跌倒事件的Zone分级和告警内容。

    返回: {
        "zone": int,               # 调整后的Zone
        "skip_voice": bool,        # 是否跳过语音确认
        "voice_timeout": int,      # 语音超时秒数
        "suspected_cause": str,    # 疑似原因
        "advice": list[str],       # 建议处理方式
        "alert_tag": str,          # 告警标签（心源性/脑源性/药源性等）
    }
    """
    br_state = fall_event.get("breathing_state", "normal")
    br_rate = fall_event.get("breathing_rate", 0)

    # 默认值
    result = {
        "zone": fall_event.get("guard_zone", 2),
        "skip_voice": False,
        "voice_timeout": profile.voice_timeout,
        "suspected_cause": "",
        "advice": [],
        "alert_tag": "",
    }

    # 心梗/心律失常 → 跳过语音，直接Zone 3
    if HX_HEART in profile.conditions:
        result["skip_voice"] = True
        result["voice_timeout"] = 0
        result["alert_tag"] = "心源性"
        if br_state in ("elevated", "irregular", "shallow"):
            result["zone"] = 3
            result["suspected_cause"] = "心源性晕厥（心律失常/心肌缺血）"
            result["advice"] = [
                "立即电话联系老人确认意识",
                "如无应答，立即前往查看",
                "检查脉搏和呼吸",
                "准备拨打120",
            ]
        elif br_state == "lost":
            result["zone"] = 4
            result["suspected_cause"] = "阿斯综合征/心源性猝死"
            result["advice"] = [
                "立即前往老人身边",
                "如发现无意识无呼吸，立即开始CPR",
                "如有AED，立即使用",
            ]
        else:
            # 心梗+呼吸正常 → 仍然直接Zone 3（可能是恶化前的窗口）
            result["zone"] = 3
            result["suspected_cause"] = "心源性晕厥（可能处于恶化前窗口期）"
            result["advice"] = [
                "立即电话联系老人确认意识",
                "心源性晕厥恶化极快（20-30秒内呼吸可能停止）",
                "准备拨打120",
            ]

    # 脑梗/中风 → 语音超时60s
    elif HX_STROKE in profile.conditions:
        result["voice_timeout"] = 60
        result["alert_tag"] = "脑源性"
        if br_state in ("elevated", "irregular", "shallow"):
            result["zone"] = 3
            result["suspected_cause"] = "脑源性晕厥（脑缺血加重）"
            result["advice"] = [
                "立即电话联系老人",
                "可能意识清醒但无法回应（肢体无力/言语障碍）",
                "立即前往查看",
            ]
        else:
            result["suspected_cause"] = "脑梗/中风（可能清醒但无法回应）"
            result["advice"] = [
                "立即电话联系老人",
                "脑梗老人可能意识清醒但肢体无法动弹",
                "不建议等待语音回应",
            ]

    # 癫痫 → 语音超时60s
    elif HX_EPILEPSY in profile.conditions:
        result["voice_timeout"] = 60
        result["alert_tag"] = "癫痫"
        if br_state == "lost":
            result["zone"] = 4
            result["suspected_cause"] = "癫痫发作，呼吸暂停"
            result["advice"] = [
                "癫痫发作期呼吸可能暂停>30秒",
                "保护老人头部，清理周围危险物品",
                "如呼吸超过30秒未恢复，立即拨打120",
            ]
        elif br_state in ("elevated", "irregular", "shallow"):
            result["zone"] = 3
            result["suspected_cause"] = "癫痫发作后状态"
            result["advice"] = [
                "癫痫发作后呼吸可能仍不规律",
                "持续监测呼吸状态",
                "保持侧卧位防止误吸",
            ]

    # 糖尿病 → 检查用药时间
    elif HX_DIABETES in profile.conditions:
        result["voice_timeout"] = 60
        result["alert_tag"] = "代谢性"
        if br_state == "normal":
            result["suspected_cause"] = "低血糖（排查用药后/餐前）"
            result["advice"] = [
                "立即电话联系老人",
                "如意识模糊，可能为低血糖",
                "准备含糖食物/葡萄糖",
                "如无法唤醒，拨打120",
            ]
        elif br_state == "lost":
            result["zone"] = 4
            result["suspected_cause"] = "低血糖昏迷"
            result["advice"] = [
                "可能低血糖昏迷",
                "立即前往查看",
                "如确认低血糖，静脉注射葡萄糖",
            ]

    # 帕金森 → 标准流程
    elif HX_PARKINSON in profile.conditions:
        result["alert_tag"] = "神经源性"
        result["suspected_cause"] = "体位性低血压/步态异常"
        result["advice"] = [
            "帕金森跌倒多为体位性低血压",
            "确认老人意识是否清醒",
            "缓慢扶起，防止再次跌倒",
        ]

    # 多重用药 → 标注药源性
    elif profile.is_multi_medication:
        result["alert_tag"] = "药源性"
        med_count = len(profile.medications)
        result["suspected_cause"] = f"药源性跌倒（服用{med_count}种药物）"
        result["advice"] = [
            f"老人当前服用{med_count}种药物，可能为药物副作用导致跌倒",
            "排查近期用药变化",
            "确认是否有头晕/嗜睡等副作用",
        ]

    # 无特殊病史
    else:
        if br_state in ("elevated", "irregular", "shallow"):
            result["zone"] = 3
            result["suspected_cause"] = "跌倒+呼吸异常"
            result["advice"] = ["立即联系老人确认情况", "如无应答前往查看"]
        elif br_state == "lost":
            result["zone"] = 4
            result["suspected_cause"] = "呼吸骤停"
            result["advice"] = ["立即拨打120", "前往老人身边"]
        else:
            result["suspected_cause"] = "环境性跌倒"
            result["advice"] = ["联系老人确认是否需要帮助"]

    return result


# ---- SQLite 病历存储 ----
_MEDICAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS medical_profile (
    id          INTEGER PRIMARY KEY DEFAULT 1,
    elder_name  TEXT NOT NULL DEFAULT '妈妈',
    age         INTEGER DEFAULT 75,
    conditions  TEXT DEFAULT '[]',
    medications TEXT DEFAULT '[]',
    fall_history INTEGER DEFAULT 0,
    syncope_history INTEGER DEFAULT 0,
    family_sudden_death INTEGER DEFAULT 0,
    wake_time   TEXT DEFAULT '06:30',
    bed_time    TEXT DEFAULT '21:30',
    updated_at  TEXT
);
"""


def init_medical_db(conn: sqlite3.Connection) -> None:
    """在现有 SQLite 连接上建病历表。"""
    conn.executescript(_MEDICAL_SCHEMA)
    # 插入默认记录（如果表为空）
    count = conn.execute("SELECT COUNT(*) FROM medical_profile").fetchone()[0]
    if count == 0:
        conn.execute(
            "INSERT INTO medical_profile (id, elder_name, age, conditions, medications, updated_at) "
            "VALUES (1, '妈妈', 75, '[]', '[]', ?)",
            (datetime.now().astimezone().isoformat(timespec="seconds"),),
        )
    conn.commit()


def load_profile(conn: sqlite3.Connection) -> MedicalProfile:
    """从数据库加载病历。"""
    row = conn.execute("SELECT * FROM medical_profile WHERE id=1").fetchone()
    if row is None:
        return MedicalProfile()
    return MedicalProfile(
        elder_name=row["elder_name"],
        age=row["age"],
        conditions=json.loads(row["conditions"]),
        medications=json.loads(row["medications"]),
        fall_history=row["fall_history"],
        syncope_history=row["syncope_history"],
        family_sudden_death=bool(row["family_sudden_death"]),
        wake_time=row["wake_time"],
        bed_time=row["bed_time"],
        updated_at=row["updated_at"] or "",
    )


def save_profile(conn: sqlite3.Connection, profile: MedicalProfile) -> None:
    """保存病历到数据库。"""
    conn.execute(
        "UPDATE medical_profile SET "
        "elder_name=?, age=?, conditions=?, medications=?, "
        "fall_history=?, syncope_history=?, family_sudden_death=?, "
        "wake_time=?, bed_time=?, updated_at=? "
        "WHERE id=1",
        (profile.elder_name, profile.age,
         json.dumps(profile.conditions, ensure_ascii=False),
         json.dumps(profile.medications, ensure_ascii=False),
         profile.fall_history, profile.syncope_history,
         int(profile.family_sudden_death),
         profile.wake_time, profile.bed_time,
         datetime.now().astimezone().isoformat(timespec="seconds")),
    )
    conn.commit()
