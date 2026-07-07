from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config_store import load_settings, save_settings
from backend.config_store import read_env, read_secrets, MASK
from backend.jobs import manager
from backend.media import detect_materials
from backend.media_tools import gpt_sovits_python
from backend.schemas import AppSettings, JobContinue, JobInfo, MaterialInfo


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
FRONTEND = ROOT / "frontend" / "dist"
GPT_SOVITS_TEST_LOCK = threading.Lock()
NET_SAMPLE = {"time": time.time(), "sent": 0, "recv": 0}


@asynccontextmanager
async def lifespan(_: FastAPI):
    manager.bind_loop(asyncio.get_running_loop())
    yield


APP_NAME = "DabaoAI-JLP 纪录片全自动智能剪辑神器"


app = FastAPI(title=APP_NAME, version="2.0.0", lifespan=lifespan)


@app.get("/api/health")
def health():
    return {"ok": True, "name": APP_NAME, "version": "2.0.0"}


@app.get("/api/system-stats")
def system_stats():
    try:
        import psutil
    except ImportError as exc:
        raise HTTPException(500, "缺少 psutil，请安装后查看本机性能监测") from exc

    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    net = psutil.net_io_counters()
    now = time.time()
    elapsed = max(0.001, now - float(NET_SAMPLE.get("time", now)))
    upload = max(0, net.bytes_sent - int(NET_SAMPLE.get("sent", net.bytes_sent))) / elapsed
    download = max(0, net.bytes_recv - int(NET_SAMPLE.get("recv", net.bytes_recv))) / elapsed
    NET_SAMPLE.update({"time": now, "sent": net.bytes_sent, "recv": net.bytes_recv})

    cpu_temp = None
    try:
        temps = psutil.sensors_temperatures()
        readings = [item.current for group in temps.values() for item in group
                    if getattr(item, "current", None) is not None]
        if readings:
            cpu_temp = round(max(readings), 1)
    except Exception:
        cpu_temp = None

    return {
        "cpu_percent": round(cpu_percent, 1),
        "cpu_temperature": cpu_temp,
        "memory_percent": round(memory.percent, 1),
        "memory_used_gb": round(memory.used / 1024 ** 3, 2),
        "memory_total_gb": round(memory.total / 1024 ** 3, 2),
        "net_upload_bps": round(upload),
        "net_download_bps": round(download),
    }


@app.get("/api/config", response_model=AppSettings)
def get_config():
    return load_settings(mask_keys=True)


@app.put("/api/config", response_model=AppSettings)
def put_config(settings: AppSettings):
    return save_settings(settings)


@app.post("/api/materials/detect", response_model=MaterialInfo)
def detect(payload: dict):
    try:
        return detect_materials(str(payload.get("folder", "")))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/voices")
def voices():
    profile_file = ROOT / "voice_dabao_bailian.json"
    clone = ""
    if profile_file.exists():
        import json
        clone = json.loads(profile_file.read_text("utf-8")).get("voice", "")
    return {
        "system": [
            {"id": "Cherry", "name": "芊悦 · 阳光自然女声"},
            {"id": "Serena", "name": "苏瑶 · 温柔女声"},
            {"id": "Chelsie", "name": "千雪 · 稳重女声"},
            {"id": "Ethan", "name": "晨煦 · 自然男声"},
        ],
        "clones": ([{"id": clone, "name": "大宝 · 纪录片克隆音色"}] if clone else []),
    }


@app.get("/api/voices/list")
def list_bailian_voices():
    key = read_secrets().get("dashscope_api_key") or read_env().get("DASHSCOPE_API_KEY", "")
    if not key:
        raise HTTPException(400, "百炼 API Key 未配置")
    req = urllib.request.Request(
        "https://dashscope.aliyuncs.com/api/v1/tts/voices",
        headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        voices = []
        for v in data.get("voices", []):
            voices.append({
                "id": v.get("voice_id", ""),
                "name": v.get("voice_name", ""),
                "gender": v.get("gender", ""),
                "description": v.get("description", ""),
            })
        return {"voices": voices}
    except Exception as exc:
        raise HTTPException(502, f"获取百炼音色列表失败: {exc}")


@app.post("/api/voices/test-gpt-sovits")
def test_gpt_sovits(payload: dict):
    engine_path = Path(str(payload.get("engine_path", "")))
    reference_audio = Path(str(payload.get("reference_audio", "")))
    reference_text = str(payload.get("reference_text", ""))
    speed = float(payload.get("speed", 1.0))
    polish = bool(payload.get("polish", False))

    if not engine_path.exists():
        raise HTTPException(400, f"引擎路径不存在: {engine_path}")
    if not reference_audio.exists():
        raise HTTPException(400, f"参考音频不存在: {reference_audio}")

    python_exe = gpt_sovits_python(engine_path)

    test_dir = RUNTIME / "gpt_sovits_test"
    test_dir.mkdir(parents=True, exist_ok=True)

    test_text = "欢迎使用DabaoAI-JLP纪录片全自动智能剪辑神器，可以通过作者在抖音、小红书、B站、视频号的：徐艾伦 获得最新支持"

    fingerprint = hashlib.sha1(json.dumps({
        "reference": str(reference_audio.resolve()),
        "reference_mtime": reference_audio.stat().st_mtime_ns,
        "reference_text": reference_text,
        "text": test_text,
        "speed": speed,
        "polish": polish,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    output_name = f"test_{fingerprint}.wav"
    output_audio = test_dir / output_name
    if output_audio.exists() and output_audio.stat().st_size > 1000:
        return FileResponse(output_audio, media_type="audio/wav",
                            filename="gpt_sovits_test.wav")

    job_data = {
        "engine": str(engine_path),
        "reference": str(reference_audio),
        "prompt_text": reference_text,
        "speed": speed,
        "polish": polish,
        "device": "auto",
        "output_dir": str(test_dir),
        "items": [{"text": test_text, "filename": output_name}],
    }

    job_file = test_dir / "jobs.json"
    job_file.write_text(json.dumps(job_data, ensure_ascii=False, indent=2), "utf-8")

    batch_script = ROOT / "gpt_sovits_batch.py"

    if not GPT_SOVITS_TEST_LOCK.acquire(blocking=False):
        raise HTTPException(409, "GPT-SoVITS 正在生成另一段测试配音，请稍候")
    log_file = test_dir / f"test_{fingerprint}.log"
    process = None
    try:
        with log_file.open("w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                [str(python_exe), str(batch_script), str(job_file)],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                # CPU cold starts load four large models. Slow machines may need more than
                # five minutes, so use a bounded 15-minute ceiling and cache the result.
                return_code = process.wait(timeout=900)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True)
                else:
                    process.kill()
                raise HTTPException(504, "GPT-SoVITS 合成超时（超过15分钟），请检查测试日志")
        if return_code != 0:
            detail = log_file.read_text("utf-8", errors="replace")[-8000:]
            raise HTTPException(500, f"GPT-SoVITS 合成失败：\n{detail}")
    finally:
        GPT_SOVITS_TEST_LOCK.release()

    if not output_audio.exists() or output_audio.stat().st_size <= 1000:
        detail = log_file.read_text("utf-8", errors="replace")[-4000:]
        raise HTTPException(500, f"GPT-SoVITS 未生成有效音频文件：\n{detail}")

    return FileResponse(output_audio, media_type="audio/wav",
                        filename="gpt_sovits_test.wav")


@app.get("/api/fonts")
def fonts():
    public_fonts = ROOT / "frontend" / "public" / "fonts"
    system_fonts = Path(r"C:\Windows\Fonts")
    catalog = [
        ("Arial", "英文常用字幕", system_fonts / "arial.ttf"),
        ("Calibri", "英文常用字幕", system_fonts / "calibri.ttf"),
        ("Verdana", "英文常用字幕", system_fonts / "verdana.ttf"),
        ("Comic Sans MS", "英文手写广告体", system_fonts / "comic.ttf"),
        ("Impact", "英文手写广告体", system_fonts / "impact.ttf"),
        ("Microsoft YaHei", "中文常用字幕", system_fonts / "msyh.ttc"),
        ("SimHei", "中文常用字幕", system_fonts / "simhei.ttf"),
        ("SimSun", "中文常用字幕", system_fonts / "simsun.ttc"),
        ("KaiTi", "中文常用字幕", system_fonts / "simkai.ttf"),
        ("DengXian", "中文常用字幕", system_fonts / "Deng.ttf"),
        ("Ma Shan Zheng", "中文手写广告体", public_fonts / "MaShanZheng-Regular.ttf"),
        ("ZCOOL KuaiLe", "中文手写广告体", public_fonts / "ZCOOLKuaiLe-Regular.ttf"),
        ("ZCOOL XiaoWei", "中文手写广告体", public_fonts / "ZCOOLXiaoWei-Regular.ttf"),
        ("Long Cang", "中文手写广告体", public_fonts / "LongCang-Regular.ttf"),
        ("Liu Jian Mao Cao", "中文手写广告体", public_fonts / "LiuJianMaoCao-Regular.ttf"),
    ]
    return {"fonts": [
        {"name": name, "category": category, "file": str(path) if path.exists() else ""}
        for name, category, path in catalog
    ]}


@app.post("/api/api-test")
def test_api(payload: dict):
    provider = str(payload.get("provider", ""))
    supplied = str(payload.get("key", ""))
    field_map = {"deepseek": "deepseek_api_key", "dashscope": "dashscope_api_key",
                 "siliconflow": "siliconflow_api_key", "seedream": "seedream_api_key"}
    env_map = {"deepseek": "DEEPSEEK_API_KEY", "dashscope": "DASHSCOPE_API_KEY",
               "siliconflow": "SILICONFLOW_API_KEY", "seedream": "SEEDREAM_API_KEY"}
    if provider not in field_map:
        raise HTTPException(400, "未知 API 服务")
    if supplied in ("", MASK):
        supplied = read_secrets().get(field_map[provider]) or read_env().get(env_map[provider], "")
    if not supplied:
        raise HTTPException(400, "API Key 未配置")
    urls = {
        "deepseek": "https://api.deepseek.com/v1/models",
        "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        "siliconflow": "https://api.siliconflow.cn/v1/models",
        "seedream": "https://ark.cn-beijing.volces.com/api/v3/models",
    }
    try:
        request = urllib.request.Request(urls[provider], headers={"Authorization": f"Bearer {supplied}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return {"ok": response.status < 400, "provider": provider}
    except Exception as exc:
        raise HTTPException(400, f"连接失败：{exc}") from exc


@app.post("/api/narration/generate")
def generate_narration(payload: dict):
    try:
        if payload.get("settings"):
            settings = AppSettings.model_validate(payload["settings"])
        else:
            material_folder = str(payload.get("material_folder", ""))
            if not material_folder:
                raise HTTPException(400, "material_folder is required")
            settings = load_settings(mask_keys=False)
            settings.material_folder = material_folder
        detect_materials(settings.material_folder)
        save_settings(settings)
        narration_text = manager.generate_narration_only(settings)
        return {"narration_text": narration_text}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/jobs", response_model=JobInfo)
def create_job(payload: dict):
    try:
        settings = AppSettings.model_validate(payload.get("settings"))
        detect_materials(settings.material_folder)
        save_settings(settings)
        narration_text = str(payload.get("narration_text", "")).strip()
        if narration_text:
            return manager.create_from_script(settings, narration_text)
        return manager.create(settings)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/jobs/{job_id}", response_model=JobInfo)
def get_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job.info


@app.post("/api/jobs/{job_id}/continue", response_model=JobInfo)
def continue_job(job_id: str, payload: JobContinue):
    try:
        return manager.continue_job(job_id, payload.narration_text)
    except KeyError as exc:
        raise HTTPException(404, "任务不存在") from exc
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    if not manager.cancel(job_id):
        raise HTTPException(409, "任务无法取消")
    return {"ok": True}


@app.websocket("/ws/jobs/{job_id}")
async def job_socket(websocket: WebSocket, job_id: str):
    await websocket.accept()
    job = manager.get(job_id)
    if not job:
        await websocket.send_json({"type": "error", "message": "任务不存在"})
        await websocket.close()
        return
    for line in job.logs[-200:]:
        await websocket.send_json({"type": "log", "level": "info", "line": line})
    await websocket.send_json({"type": "status", "job": job.info.model_dump()})
    queue = manager.subscribe(job_id)
    try:
        while queue:
            await websocket.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        if queue:
            manager.unsubscribe(job_id, queue)


@app.get("/icon.png")
def serve_icon():
    icon = ROOT / "frontend" / "public" / "icon.png"
    if not icon.exists():
        icon = ROOT / "frontend" / "dist" / "icon.png"
    if icon.exists():
        return FileResponse(icon)
    raise HTTPException(404, "图标文件不存在")


if FRONTEND.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        candidate = FRONTEND / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND / "index.html")
else:
    @app.get("/")
    def frontend_missing():
        return {"message": "前端尚未构建，请运行 npm --prefix frontend run build"}
