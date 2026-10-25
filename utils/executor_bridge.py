"""免密钥执行桥：本地模型服务 / 用户已登录 CLI 的响应自动回填（R7）。

宪章边界（唯一红线 = 零 API Key）：
- 本模块绝不读取任何 Key / Token / Secret，不发 Authorization 头；
- CLI 通道只执行 config/executor-bridge.yaml 里声明的命令模板（argv 列表、
  无 shell），凭据由该 CLI 自身的登录态持有，项目代码与配置不接触凭据；
- HTTP 通道只允许回环地址（本地推理服务），经 utils.web_fetch.post_text_
  loopback（请求头免密钥检查、不落缓存）；
- 命令模板只能来自配置文件，绝不接受作业数据注入的命令；
- 每次调用写穿审计日志（bridge-log.jsonl）：作业、通道、耗时、状态、错误；
- 失败 / 超时按作业逐条记录并保持 pending（fail-closed），绝不伪造响应。

两种通道：
- type: cli   —— stdin 喂 prompt，stdout 即响应（claude -p / codex exec 等）；
- type: http  —— 回环 POST（api: ollama-chat 解析 message.content；raw 原样返回）。

纯标准库；不发外部网络请求。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.config_loader import PROJECT_ROOT, load_yaml_file
from utils.logger import get_logger
from utils import web_fetch

logger = get_logger("executor-bridge")

BRIDGE_CONFIG_PATH = PROJECT_ROOT / "config" / "executor-bridge.yaml"
LOG_FILENAME = "bridge-log.jsonl"

CHANNEL_TYPES = ("cli", "http")


class BridgeError(RuntimeError):
    """执行桥调用失败（非零退出 / 超时 / 响应不可解析）。"""


def load_bridge_config(path: str | Path | dict | None = None) -> dict[str, Any]:
    """加载执行桥配置；传入 dict 原样返回（测试 / 编程注入）。"""
    if isinstance(path, dict):
        return path
    target = Path(path) if path else BRIDGE_CONFIG_PATH
    if not target.exists():
        return {"enabled": False, "channels": {}, "routing": {}}
    return load_yaml_file(target)


def _resolve_channel(config: dict[str, Any], kind: str) -> tuple[str, dict[str, Any]]:
    routing = config.get("routing") or {}
    name = str(routing.get(kind) or "").strip()
    if not name:
        raise BridgeError(f"执行桥 routing 未配置作业类别 {kind!r}")
    channels = config.get("channels") or {}
    channel = channels.get(name)
    if not isinstance(channel, dict):
        raise BridgeError(f"执行桥通道 {name!r} 未定义")
    ctype = str(channel.get("type") or "")
    if ctype not in CHANNEL_TYPES:
        raise BridgeError(f"执行桥通道 {name!r} 类型非法: {ctype!r}")
    if ctype == "cli" and (not isinstance(channel.get("command"), list) or not channel.get("command")):
        # 配置期校验：字符串命令会被逐字符展开成 argv，绝不允许（无 shell）
        raise BridgeError(f"执行桥通道 {name!r} 的 command 必须是非空 argv 列表")
    return name, channel


def _execute_cli(prompt_text: str, channel: dict[str, Any], timeout: float) -> str:
    command = channel.get("command")
    if not isinstance(command, list) or not command:
        raise BridgeError("cli 通道 command 必须是非空 argv 列表（禁止字符串 shell 命令）")
    argv = [str(part) for part in command]
    completed = subprocess.run(  # noqa: S603 - 命令只来自配置文件，argv 列表无 shell
        argv,
        input=prompt_text.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise BridgeError(f"CLI 通道退出码 {completed.returncode}: {stderr[:300]}")
    return completed.stdout.decode("utf-8", errors="replace")


def _execute_http(prompt_text: str, channel: dict[str, Any], timeout: float) -> str:
    endpoint = str(channel.get("endpoint") or "").strip()
    if not endpoint:
        raise BridgeError("http 通道缺少 endpoint")
    api = str(channel.get("api") or "ollama-chat").strip().lower()
    if api == "ollama-chat":
        payload: dict[str, Any] = {
            "model": str(channel.get("model") or ""),
            "messages": [{"role": "user", "content": prompt_text}],
            "stream": False,
        }
    elif api == "raw":
        payload = {"prompt": prompt_text}
    else:
        raise BridgeError(f"http 通道 api 非法: {api!r}（可选 ollama-chat / raw）")
    body = web_fetch.post_text_loopback(endpoint, payload, timeout=timeout)
    if api != "ollama-chat":
        return body
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"ollama-chat 响应不是合法 JSON: {exc}") from exc
    message = data.get("message") or {}
    content = message.get("content")
    if not isinstance(content, str):
        raise BridgeError("ollama-chat 响应缺少 message.content")
    return content


def _job_prompt(job: dict[str, Any]) -> str:
    """作业 prompt 来源：润色作业带 prompt 字段；评审作业带 prompt_file。"""
    text = str(job.get("prompt") or "")
    if text.strip():
        return text
    prompt_file = str(job.get("prompt_file") or "")
    if prompt_file and Path(prompt_file).exists():
        return Path(prompt_file).read_text(encoding="utf-8-sig")
    raise BridgeError("作业缺少 prompt / prompt_file，无法构造执行桥请求")


def backfill_jobs(
    jobs: list[dict[str, Any]],
    responses_dir: str | Path,
    *,
    kind: str,
    config: dict[str, Any] | None = None,
    log_file: str | Path | None = None,
) -> dict[str, Any]:
    """对待回填作业逐个执行桥通道，响应写 responses/<job_id>.txt。

    返回 {channel, attempted, filled, skipped, failed}；失败作业不留响应文件
    （保持 pending，fail-closed）。每次调用（含失败）追加审计日志。
    """
    config = config or load_bridge_config()
    channel_name, channel = _resolve_channel(config, kind)
    defaults = config.get("defaults") or {}
    timeout = float(channel.get("timeout_seconds") or defaults.get("timeout_seconds") or 600)
    responses = Path(responses_dir)
    responses.mkdir(parents=True, exist_ok=True)
    log_path = Path(log_file) if log_file else None

    report: dict[str, Any] = {
        "channel": channel_name,
        "kind": kind,
        "attempted": 0,
        "filled": 0,
        "skipped": 0,
        "failed": [],
    }
    entries: list[dict[str, Any]] = []
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        if not job_id or str(job.get("response") or "").strip():
            report["skipped"] += 1
            continue
        target = responses / f"{job_id}.txt"
        if target.exists():
            report["skipped"] += 1
            continue
        report["attempted"] += 1
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "channel": channel_name,
            "job_id": job_id,
            "status": "failed",
        }
        started = time.monotonic()
        try:
            prompt_text = _job_prompt(job)
            entry["prompt_chars"] = len(prompt_text)
            response = (
                _execute_cli(prompt_text, channel, timeout)
                if channel.get("type") == "cli"
                else _execute_http(prompt_text, channel, timeout)
            )
            if not response.strip():
                raise BridgeError("通道返回空响应")
            target.write_text(response, encoding="utf-8")
            entry["status"] = "filled"
            entry["response_chars"] = len(response)
            report["filled"] += 1
        except (BridgeError, OSError, subprocess.TimeoutExpired) as exc:
            message = str(exc)
            entry["error"] = message[:400]
            report["failed"].append({"job_id": job_id, "error": message[:300]})
            logger.warning("执行桥作业 %s 失败: %s", job_id, message)
        entry["duration_ms"] = int((time.monotonic() - started) * 1000)
        entries.append(entry)

    if log_path is not None and entries:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            for item in entries:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return report


def self_test(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """自检：逐通道发一条固定探针，验证通道可用（不接触任何作业数据）。"""
    config = config or load_bridge_config()
    results: list[dict[str, Any]] = []
    probe = "bridge self-test: reply with the single word OK"
    for name, channel in (config.get("channels") or {}).items():
        entry: dict[str, Any] = {"channel": name, "type": channel.get("type")}
        try:
            timeout = float(
                channel.get("timeout_seconds") or (config.get("defaults") or {}).get("timeout_seconds") or 60
            )
            response = (
                _execute_cli(probe, channel, timeout)
                if channel.get("type") == "cli"
                else _execute_http(probe, channel, timeout)
            )
            entry["ok"] = bool(response.strip())
            entry["response_chars"] = len(response)
        except (BridgeError, OSError, subprocess.TimeoutExpired) as exc:
            entry["ok"] = False
            entry["error"] = str(exc)[:300]
        results.append(entry)
    return {"channels": results, "python": sys.version.split()[0]}
