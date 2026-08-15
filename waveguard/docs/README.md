# 护院鹅居家安全守护系统 · 场景模拟 MVP

不接硬件，用软件把「独居老人的一天 → 疑似跌倒 → 子女端实时告警」整条守护链路演一遍。
对应《02 执行任务清单》里程碑 **M1 模拟轨闭环**。

## 这条链路怎么跑通的

```
剧本播放器 replay/            边缘服务 edge/                     子女端 web/
按时间线逐秒发出         →   FastAPI 收样本                →   浏览器 WebSocket
"活动强度"样本               状态机判定事件(presence/                实时:
(正常→跌倒尖峰→静止)          motion/still/suspected_fall)          · 守护状态卡变色
                             → SQLite 入库                          · 活动强度曲线
                             → guardian 派生三级告警                 · 告警时间线
                             → WebSocket 广播
```

关键：事件是边缘状态机**实时判定**出来的，不是剧本预先写死的。剧本只提供
「活动强度」原始信号，把硬件轨的 CSI 特征换成了可控的模拟信号，二者走**完全相同**
的下游链路（`source=dataset_replay` vs `csi_live`），未来硬件接入零改动。

## 三步跑起来

```powershell
# 0. 安装依赖（首次）
python -m pip install -r requirements.txt

# 1. 启动边缘服务（窗口一，保持运行）
cd waveguard
python run_edge.py

# 2. 浏览器打开子女端
#    http://127.0.0.1:8000

# 3. 播放剧本（窗口二）
cd waveguard
python -m replay.player
```

播放开始后，子女端会依次出现：🚪有人在家 → 🏃正常活动 → 😴静息 →
🏃起身 → 🚨**疑似跌倒紧急告警**（状态卡变红脉冲 + AI 判断理由）。

## 目录

| 路径 | 作用 |
|------|------|
| `edge/protocol.py`      | 事件流协议 Event Schema v1（唯一数据契约） |
| `edge/config.py`        | 活动强度阈值、状态机时间门槛 |
| `edge/state_machine.py` | 事件状态机（样本流 → 事件流） |
| `edge/guardian.py`      | 告警派生（规则占位，后续换 Qwen） |
| `edge/db.py`            | SQLite 入库/读取 |
| `edge/server.py`        | FastAPI + WebSocket 广播 |
| `replay/player.py`      | 剧本播放器 |
| `replay/scenarios/*.json` | 剧本（时间线 × 活动强度） |
| `web/index.html`        | 子女端网页 |

## 常用命令

```powershell
python -m replay.player --speed 5          # 放慢到 5 倍速看清过程
python -m replay.player --scenario day_fall
# 重置一场演示（清库+重置状态机）：
#   浏览器控制台或 curl:  POST http://127.0.0.1:8000/api/reset
```
