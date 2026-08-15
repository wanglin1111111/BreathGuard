"""一键启动边缘服务。等价于：
    uvicorn edge.server:app --host 0.0.0.0 --port 8000
在 waveguard/ 目录下运行：  python run_edge.py
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("edge.server:app", host="0.0.0.0", port=8000, reload=False)
