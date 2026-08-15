"""边缘服务：FastAPI + WebSocket 广播 + SQLite 入库 + 事件状态机 + 告警派生。

数据入口三选一，走完全相同的下游链路（架构 §3.1）：
  POST /ingest/sample  逐秒活动强度样本 → 状态机判定事件（模拟轨/硬件轨共用）
  POST /ingest/event   直接注入成型事件（演示注入）
下游：事件入库 → guardian 派生告警 → WebSocket 广播给所有子女端。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import config as C
from . import guardian
from . import medical
from .db import open_store
from .protocol import Event, EVT_FALL_BREATHING_OK
from .state_machine import SampleProcessor
from .voice import VoiceConfirmSession

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="护院鹅 Edge")
store = open_store(C.DB_PATH)
processor = SampleProcessor(zone="living")
voice_session = VoiceConfirmSession(elder_name="奶奶", timeout_s=90)


class ConnectionManager:
    """WebSocket 广播（FastAPI 官方 ConnectionManager 模式）。"""

    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, payload: dict) -> None:
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


class SampleIn(BaseModel):
    ts: str
    sim_t: float
    intensity: float
    zone: str | None = None
    breathing_rate: int = 0           # 呼吸频率（次/分），0=未检测
    breathing_state: str = ""          # normal/elevated/irregular/shallow/lost


async def _handle_event(ev: Event) -> None:
    """事件统一处理：入库 → 派生告警 → 广播。"""
    store.insert_event(ev)
    await manager.broadcast({"kind": "event", "data": ev.to_dict()})
    # Zone 2 语音确证触发
    if ev.type == EVT_FALL_BREATHING_OK:
        voice_data = voice_session.start()
        await manager.broadcast({"kind": "voice_confirm", "data": voice_data})
    # 加载病历，实现个性化告警
    profile = medical.load_profile(store._conn)
    alert = guardian.decide(ev.to_dict(), profile)
    if alert:
        store.insert_alert(alert)
        await manager.broadcast({"kind": "alert", "data": alert})


@app.post("/ingest/sample")
async def ingest_sample(s: SampleIn):
    events = processor.process(s.ts, s.sim_t, s.intensity, s.zone,
                               s.breathing_rate, s.breathing_state)
    for ev in events:
        await _handle_event(ev)
    # 广播实时活动强度+呼吸状态，供子女端画曲线
    await manager.broadcast({
        "kind": "sample",
        "data": {"ts": s.ts, "sim_t": s.sim_t, "intensity": s.intensity,
                 "zone": s.zone or processor.zone, "present": processor.present,
                 "breathing_rate": s.breathing_rate,
                 "breathing_state": s.breathing_state or "normal",
                 "guard_zone": processor.guard_zone},
    })
    return {"events": [e.type for e in events]}


@app.post("/ingest/event")
async def ingest_event(ev: dict):
    event = Event(**ev)
    await _handle_event(event)
    return {"ok": True, "event_id": event.event_id}


@app.post("/api/reset")
async def api_reset():
    global processor
    store.reset()
    processor = SampleProcessor(zone="living")
    await manager.broadcast({"kind": "reset", "data": {}})
    return {"ok": True}


@app.get("/api/events")
async def api_events(limit: int = 50):
    return JSONResponse(store.recent_events(limit))


@app.get("/api/status")
async def api_status():
    return {"present": processor.present, "zone": processor.zone,
            "guard_zone": processor.guard_zone}


# ---- 病历管理 API ----
class ProfileIn(BaseModel):
    # 字段名和前端对齐
    name: str = "妈妈"
    age: int = 75
    diseases: list[str] = []
    medications: list[str] = []
    fall_count: int = 0
    syncope_count: int = 0
    family_sudden_cardiac_death: bool = False
    wake_time: str = "06:30"
    sleep_time: str = "21:30"


@app.get("/api/profile")
async def api_get_profile():
    """获取老人健康档案。"""
    profile = medical.load_profile(store._conn)
    d = profile.to_dict()
    # 转换字段名和前端对齐
    return {
        "name": profile.elder_name,
        "age": profile.age,
        "diseases": profile.conditions,
        "medications": profile.medications,
        "fall_count": profile.fall_history,
        "syncope_count": profile.syncope_history,
        "family_sudden_cardiac_death": profile.family_sudden_death,
        "wake_time": profile.wake_time,
        "sleep_time": profile.bed_time,
        "is_multi_medication": profile.is_multi_medication,
        "is_high_risk": profile.is_high_risk,
        "voice_timeout": profile.voice_timeout,
    }


@app.post("/api/profile")
async def api_save_profile(p: ProfileIn):
    """保存老人健康档案。"""
    profile = medical.MedicalProfile(
        elder_name=p.name, age=p.age,
        conditions=p.diseases, medications=p.medications,
        fall_history=p.fall_count, syncope_history=p.syncope_count,
        family_sudden_death=p.family_sudden_cardiac_death,
        wake_time=p.wake_time, bed_time=p.sleep_time,
    )
    medical.save_profile(store._conn, profile)
    # 更新状态机的语音超时
    processor._zone2_voice_timeout = profile.voice_timeout
    # 高血压病史：Zone 3 停留超过10分钟自动升级
    if medical.HX_HYPERTENSION in profile.conditions:
        processor._zone3_max_stay = 600  # 10分钟
    else:
        processor._zone3_max_stay = 0
    await manager.broadcast({"kind": "profile_updated", "data": profile.to_dict()})
    return {"ok": True, "profile": {
        "name": profile.elder_name,
        "diseases": profile.conditions,
        "is_high_risk": profile.is_high_risk,
        "voice_timeout": profile.voice_timeout,
    }}


# ---- 子女确认接口 ----
@app.post("/api/family/confirm")
async def api_family_confirm():
    """子女确认收到告警，阻止 Zone 3→4 超时升级。"""
    processor._family_confirmed = True
    await manager.broadcast({"kind": "family_confirmed", "data": {"confirmed": True}})
    return {"ok": True, "message": "已确认收到告警"}


# ---- 护家模式控制接口 ----
@app.get("/api/guard-mode")
async def api_get_guard_mode():
    """获取护家模式状态。"""
    return {
        "enabled": processor._intrusion_mode,
        "intrusion_fired": processor._intrusion_fired,
        "silence_s": int(processor._silence_accum),
        "can_toggle": True,
    }


@app.post("/api/guard-mode/toggle")
async def api_toggle_guard_mode():
    """子女远程开关护家模式（访客到访时手动关闭，访客离开后恢复）。"""
    if processor._intrusion_mode:
        # 关闭护家模式
        processor._intrusion_mode = False
        processor._silence_accum = 0.0
        processor._intrusion_motion_accum = 0.0
        processor._intrusion_fired = False
        msg = "护家模式已关闭（访客模式）"
    else:
        # 手动启动护家模式
        processor._intrusion_mode = True
        processor._intrusion_fired = False
        msg = "护家模式已启动"
    await manager.broadcast({"kind": "guard_mode", "data": {"enabled": processor._intrusion_mode}})
    return {"ok": True, "enabled": processor._intrusion_mode, "message": msg}


# ---- 语音确证 API ----
@app.get("/api/voice-confirm")
async def api_get_voice_confirm():
    """获取当前语音确证状态。"""
    return voice_session.to_dict()


@app.post("/api/voice-confirm/respond")
async def api_voice_respond(answer: str = "ok"):
    """老人回应语音确证：ok=我没事，help=我需要帮助。"""
    state = voice_session.respond(answer)
    await manager.broadcast({"kind": "voice_responded", "data": {"state": state, "answer": answer}})
    if state == "ok":
        # 老人说没事 → 消警，重置到绿区
        processor._reset_to_green()
        processor._fall_watch = None
        processor._still_too_long_fired = False
        await manager.broadcast({"kind": "alert_cleared", "data": {"reason": "老人回应正常"}})
    elif state == "help":
        # 老人求助 → 升级 Zone 3
        from .protocol import ZONE_RED
        processor._set_zone(ZONE_RED)
        await manager.broadcast({"kind": "alert_escalated", "data": {"reason": "老人求助，升级告警"}})
    return {"ok": True, "state": state}


# ---- 开放式疾病管理 API ----
class DiseaseIn(BaseModel):
    code: str
    name: str
    category: str = ""
    description: str = ""
    fall_risk_note: str = ""
    breathing_impact: str = ""
    advice: list[str] = []
    voice_timeout_override: int = 0
    skip_voice: bool = False
    zone3_max_stay: int = 0


@app.get("/api/diseases")
async def api_list_diseases():
    """列出所有可用疾病策略（含预设和自定义）。"""
    # 预设疾病
    preset = []
    for code in medical.ALL_CONDITIONS:
        strategy = medical.get_disease_strategy(code)
        if strategy["source"] != "unknown":
            preset.append({"code": code, **strategy})
    # 自定义疾病
    custom = medical.list_custom_diseases()
    return {"preset": preset, "custom": custom}


@app.post("/api/diseases")
async def api_add_disease(d: DiseaseIn):
    """注册一个自定义疾病。"""
    disease = medical.CustomDisease(
        code=d.code, name=d.name, category=d.category,
        description=d.description, fall_risk_note=d.fall_risk_note,
        breathing_impact=d.breathing_impact, advice=d.advice,
        voice_timeout_override=d.voice_timeout_override,
        skip_voice=d.skip_voice, zone3_max_stay=d.zone3_max_stay,
    )
    medical.register_disease(disease)
    return {"ok": True, "disease": disease.to_dict()}


@app.delete("/api/diseases/{code}")
async def api_remove_disease(code: str):
    """移除一个自定义疾病。"""
    removed = medical.unregister_disease(code)
    return {"ok": removed, "code": code}


@app.get("/api/diseases/{code}/strategy")
async def api_disease_strategy(code: str):
    """获取指定疾病的告警策略。"""
    return medical.get_disease_strategy(code)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # 子女端只收不发，收到即忽略
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")
