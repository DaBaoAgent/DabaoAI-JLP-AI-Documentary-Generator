from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .concurrency import get_concurrency
from .config_store import runtime_env, safe_settings_dump
from .media import detect_materials
from .schemas import AppSettings, JobInfo
from .postprocess import run_postprocess


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
JOBS_DIR = RUNTIME / "jobs"
LOGS_DIR = RUNTIME / "logs"


@dataclass
class JobRuntime:
    info: JobInfo
    settings: AppSettings
    logs: list[str] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    process: subprocess.Popen | None = None
    cancel_requested: bool = False


class JobManager:
    def __init__(self):
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, JobRuntime] = {}
        self.lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def create(self, settings: AppSettings) -> JobInfo:
        with self.lock:
            if any(x.info.status in ("queued", "running") for x in self.jobs.values()):
                raise RuntimeError("已有智能成片任务正在运行，请等待或先取消")
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        info = JobInfo(id=job_id, status="queued", stage="等待启动", progress=0,
                       message="任务已加入队列")
        runtime = JobRuntime(info=info, settings=settings)
        with self.lock:
            self.jobs[job_id] = runtime
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "settings.json").write_text(safe_settings_dump(settings), "utf-8")
        threading.Thread(target=self._run_full, args=(runtime, False), daemon=True).start()
        return info

    def create_from_script(self, settings: AppSettings, narration_text: str) -> JobInfo:
        with self.lock:
            if any(x.info.status in ("queued", "running") for x in self.jobs.values()):
                raise RuntimeError("已有智能成片任务正在运行，请等待或先取消")
        if not narration_text.strip():
            raise RuntimeError("文案不能为空")
        manifest_path = Path(settings.material_folder) / "_narration_manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("没有找到文案锚点，请先点击 AI生成文案")
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        info = JobInfo(id=job_id, status="queued", stage="等待启动", progress=0,
                       message="已载入当前文案，任务已加入队列",
                       narration_text=narration_text.strip())
        runtime = JobRuntime(info=info, settings=settings)
        self._apply_narration_text(runtime, narration_text)
        with self.lock:
            self.jobs[job_id] = runtime
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "settings.json").write_text(safe_settings_dump(settings), "utf-8")
        self._log(runtime, "已使用文案窗口中的内容创建成片任务")
        threading.Thread(target=self._run_full, args=(runtime, False), daemon=True).start()
        return info

    def _apply_narration_text(self, job: JobRuntime, narration_text: str) -> None:
        lines = [x.strip() for x in narration_text.splitlines() if x.strip()]
        if not lines:
            raise RuntimeError("文案不能为空")
        manifest_path = Path(job.settings.material_folder) / "_narration_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text("utf-8"))
            original = manifest["segments"]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError("文案锚点文件损坏，请重新生成文案") from exc
        if not original:
            raise RuntimeError("文案没有可用的画面锚点")
        edited = []
        for index, text in enumerate(lines):
            source_index = (round(index * (len(original) - 1) / (len(lines) - 1))
                            if len(lines) > 1 else 0)
            segment = dict(original[source_index])
            segment["segment_id"] = index + 1
            segment["text"] = text
            edited.append(segment)
        manifest["segments"] = edited
        temp = manifest_path.with_suffix(".review.tmp")
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(manifest_path)
        (Path(job.settings.material_folder) / "配音稿.txt").write_text("\n".join(lines), "utf-8")
        job.info.narration_text = "\n".join(lines)

    def continue_job(self, job_id: str, narration_text: str) -> JobInfo:
        job = self.get(job_id)
        if not job:
            raise KeyError(job_id)
        if job.info.status in ("queued", "running"):
            raise RuntimeError("任务已经在运行")
        self._apply_narration_text(job, narration_text)
        job.cancel_requested = False
        self._update(job, status="queued", stage="等待启动", progress=0,
                     message="修改后的文案已保存，任务已加入队列")
        threading.Thread(target=self._run_full, args=(job, False), daemon=True).start()
        return job.info

    def get(self, job_id: str) -> JobRuntime | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.info.status not in ("queued", "running"):
            return False
        job.cancel_requested = True
        if job.process and job.process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(job.process.pid), "/T", "/F"],
                                   capture_output=True)
                else:
                    job.process.terminate()
            except OSError:
                pass
        self._update(job, status="cancelled", stage="已取消", message="用户取消了任务")
        return True

    def subscribe(self, job_id: str) -> asyncio.Queue | None:
        job = self.get(job_id)
        if not job:
            return None
        queue: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue):
        job = self.get(job_id)
        if job and queue in job.subscribers:
            job.subscribers.remove(queue)

    def _emit(self, job: JobRuntime, payload: dict):
        if not self.loop:
            return
        for queue in list(job.subscribers):
            asyncio.run_coroutine_threadsafe(queue.put(payload), self.loop)

    def _log(self, job: JobRuntime, message: str, level: str = "info"):
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        job.logs.append(line)
        (LOGS_DIR / f"{job.info.id}.log").open("a", encoding="utf-8").write(line + "\n")
        self._emit(job, {"type": "log", "level": level, "line": line})

    def _update(self, job: JobRuntime, **values):
        for key, value in values.items():
            setattr(job.info, key, value)
        self._emit(job, {"type": "status", "job": job.info.model_dump()})

    def _command(self, job: JobRuntime, script_only: bool = False) -> list[str]:
        media = detect_materials(job.settings.material_folder)
        target_seconds = min(job.settings.video.target_minutes * 60,
                             media.duration - job.settings.video.trim_head - job.settings.video.trim_tail)
        ratio = max(0.05, min(1.0, target_seconds / media.duration))
        voice = job.settings.voice
        if voice.mode == "clone" and voice.provider == "qwen":
            backend, voice_args = "qwen-clone", ["--qwen-voice", voice.clone_voice_id]
        elif voice.mode == "clone" and voice.provider == "gpt_sovits":
            reference = Path(voice.gpt_sovits_reference_audio)
            engine = Path(voice.gpt_sovits_engine_path)
            if not engine.is_dir():
                raise RuntimeError(f"本地 GPT-SoVITS 引擎不存在：{engine}")
            if not reference.is_file():
                raise RuntimeError(f"GPT-SoVITS 参考音频不存在：{reference}")
            if not voice.gpt_sovits_reference_text.strip():
                raise RuntimeError("请填写参考音频对应文字")
            backend = "gpt-sovits"
            voice_args = ["--gpt-sovits", str(engine), "--reference", str(reference),
                          "--prompt-text", voice.gpt_sovits_reference_text]
        elif voice.mode == "system":
            backend = "qwen-clone"
            voice_args = ["--qwen-voice", voice.system_voice,
                          "--qwen-model", "qwen3-tts-flash-realtime"]
        else:
            backend, voice_args = "cosyvoice", []
        cmd = [sys.executable, "-u", str(ROOT / "anchored_pipeline.py"),
               job.settings.material_folder, "--ratio", f"{ratio:.8f}",
               "--target-seconds", f"{target_seconds:.3f}",
               "--tts-backend", backend, "--speech-rate", str(voice.speech_rate),
               "--trim-head", str(job.settings.video.trim_head),
               "--trim-tail", str(job.settings.video.trim_tail),
               "--style", job.settings.narration.style,
               "--custom-prompt", job.settings.narration.custom_prompt, *voice_args]
        if script_only:
            cmd.append("--script-only")
        elif not job.settings.video.mute_source and not job.settings.video.separate_vocals_bgm:
            cmd += ["--include-source-audio", "--source-volume", str(job.settings.video.source_volume)]
        if job.settings.video.exclude_interviews:
            cmd.append("--exclude-interviews")
        if voice.mode == "clone" and voice.provider == "gpt_sovits" and voice.polish_audio:
            cmd.append("--polish")
        cmd.extend(["--concurrency", str(get_concurrency())])
        return cmd

    def _run_process(self, job: JobRuntime, cmd: list[str]) -> None:
        env = runtime_env(job.settings)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = ((subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                         if os.name == "nt" else 0)
        job.process = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                       errors="replace", env=env, creationflags=creationflags)
        assert job.process.stdout
        recent_lines: list[str] = []
        for line in job.process.stdout:
            if job.cancel_requested:
                break
            line = line.rstrip()
            if line:
                recent_lines.append(line)
                recent_lines = recent_lines[-12:]
                self._log(job, line)
                progress = self._parse_progress(line)
                if progress:
                    self._update(job, **progress)
        code = job.process.wait()
        if not job.cancel_requested and code != 0:
            detail = "\n".join(recent_lines[-8:])
            raise RuntimeError(f"成片内核退出，代码 {code}" + (f"\n{detail}" if detail else ""))
            raise RuntimeError(f"成片内核退出，代码 {code}")

    def _run_full(self, job: JobRuntime, script_only: bool = False):
        try:
            if script_only:
                (Path(job.settings.material_folder) / "_narration_manifest.json").unlink(missing_ok=True)
                self._update(job, status="running", stage="生成文案", progress=2,
                             message="正在分析素材并生成完整文案")
                self._log(job, "开始分析素材并生成来源锚定文案")
                self._run_process(job, self._command(job, script_only=True))
                if job.cancel_requested:
                    return
                script = (Path(job.settings.material_folder) / "配音稿.txt").read_text("utf-8").strip()
                if not script:
                    raise RuntimeError("生成的文案为空")
                self._update(job, status="success", stage="文案完成", progress=100,
                             message="完整文案已生成，可按当前文案成片",
                             narration_text=script)
                self._log(job, "完整文案已生成")
            else:
                self._update(job, status="running", stage="智能配音与剪辑", progress=8,
                             message="正在生成配音并剪辑成片")
                media = detect_materials(job.settings.material_folder)
                self._log(job, f"检测到视频：{Path(media.video_path).name}")
                self._log(job, f"原片 {media.duration / 60:.2f} 分钟，{media.width}×{media.height}")
                target_seconds = job.settings.video.target_minutes * 60
                usable = media.duration - job.settings.video.trim_head - job.settings.video.trim_tail
                if target_seconds > usable:
                    target_seconds = usable
                    self._log(job, f"目标时长超过可用原片，自动调整为 {usable / 60:.2f} 分钟", "warning")
                self._update(job, stage="智能文案与配音", progress=6,
                             message="正在生成来源锚定文案")
                if job.settings.video.separate_vocals_bgm:
                    self._log(job, "已去除原片人声和 BGM：只保留克隆旁白与新匹配的 BGM")
                self._run_process(job, self._command(job))
                if job.cancel_requested:
                    return
                output = Path(job.settings.material_folder) / "★ 成片.mp4"
                if not output.exists():
                    raise RuntimeError("流水线结束但未找到成片")
                self._update(job, stage="字幕、BGM与封面", progress=92,
                             message="正在生成发布包")
                self._log(job, "正在烧录字幕、混合 BGM" + (" 并生成多比例封面" if job.settings.cover.enabled else "，跳过封面生成"))
                saved = runtime_env(job.settings)
                result = run_postprocess(job.settings, Path(job.settings.material_folder),
                                         JOBS_DIR / job.info.id,
                                         saved.get("DEEPSEEK_API_KEY", ""),
                                         saved.get("SEEDREAM_API_KEY", ""))
                publication = result.get("publication", {})
                self._update(job, status="success", stage="成片完成", progress=100,
                             message="智能成片已完成", output_path=str(output),
                             title=publication.get("title", ""), tags=publication.get("tags", []),
                             description=publication.get("description", ""))
                self._log(job, f"成片完成：{output}", "success")
        except Exception as exc:
            if not job.cancel_requested:
                self._log(job, f"任务失败：{exc}", "error")
                self._update(job, status="failed", stage="处理失败", message=str(exc), error=str(exc))

    def generate_narration_only(self, settings: AppSettings) -> str:
        job_id = "_narration_" + uuid.uuid4().hex[:6]
        info = JobInfo(id=job_id, status="running", stage="生成文案", progress=0,
                       message="正在生成文案")
        job = JobRuntime(info=info, settings=settings)
        manifest_path = Path(settings.material_folder) / "_narration_manifest.json"
        manifest_path.unlink(missing_ok=True)
        self._run_process(job, self._command(job, script_only=True))
        script = (Path(settings.material_folder) / "配音稿.txt").read_text("utf-8").strip()
        if not script:
            raise RuntimeError("生成的文案为空")
        return script

    @staticmethod
    def _parse_progress(line: str) -> dict | None:
        match = re.search(r"文案分章\s*(\d+)/(\d+)", line)
        if match:
            current, total = map(int, match.groups())
            return {"stage": "分章生成文案", "progress": 6 + int(current / total * 2),
                    "message": f"正在生成文案 {current}/{total} 章"}
        match = re.search(r"Qwen TTS (\d+)/(\d+)", line)
        if match:
            current, total = map(int, match.groups())
            return {"stage": "克隆音色配音", "progress": 8 + int(current / total * 30),
                    "message": f"配音 {current}/{total}"}
        match = re.search(r"GPT-SoVITS (\d+)/(\d+)", line)
        if match:
            current, total = map(int, match.groups())
            return {"stage": "本地 GPT-SoVITS 克隆配音", "progress": 8 + int(current / total * 30),
                    "message": f"GPT-SoVITS 配音 {current}/{total}"}
        match = re.search(r"采访检测\s*(\d+)/(\d+)", line)
        if match:
            current, total = map(int, match.groups())
            return {"stage": "采访检测", "progress": 38 + int(current / total * 4),
                    "message": f"正在检测采访片段 {current}/{total}"}
        match = re.search(r"视频 (\d+)/(\d+)", line)
        if match:
            current, total = map(int, match.groups())
            return {"stage": "精准画面剪辑", "progress": 42 + int(current / total * 48),
                    "message": f"画面渲染 {current}/{total}"}
        if "文案" in line and "句" in line:
            return {"stage": "文案完成", "progress": 8, "message": line}
        if line.startswith("完成："):
            return {"stage": "最终质检", "progress": 96, "message": "正在检查输出"}
        return None


manager = JobManager()
