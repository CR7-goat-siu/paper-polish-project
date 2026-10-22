"""Zotero Word 域交叉引用 Skill 入口。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.zotero_crossref import (  # noqa: E402
    audit_zotero_docx,
    extract_reference_manifest,
    probe_zotero_local,
    rebind_zotero_fields,
)
from utils.config_loader import PROJECT_ROOT, load_yaml_file  # noqa: E402


def _config() -> dict[str, Any]:
    path = PROJECT_ROOT / "config" / "zotero.yaml"
    return load_yaml_file(path) if path.exists() else {}


def _word_plugin_status() -> dict[str, Any]:
    config = _config()
    word_config = config.get("word") or {}
    appdata = Path.home() / "AppData" / "Roaming"
    startup = appdata / str(
        word_config.get("startup_relative_path") or "Microsoft/Word/STARTUP"
    )
    template = startup / str(word_config.get("template_name") or "Zotero.dotm")
    dll_candidates = []
    for raw in word_config.get("integration_dll_candidates") or []:
        path = Path(str(raw))
        dll_candidates.append(path if path.is_absolute() else appdata / path)
    dll = next((path for path in dll_candidates if path.exists()), None)
    return {
        "template": str(template),
        "template_exists": template.exists(),
        "integration_dll": str(dll) if dll else "",
        "integration_dll_exists": bool(dll),
        "installed": template.exists() and bool(dll),
    }


def _refresh_word(input_path: Path, output_path: Path) -> dict[str, Any]:
    config = _config()
    api_config = config.get("local_api") or {}
    word_config = config.get("word") or {}
    probe = probe_zotero_local(
        str(api_config.get("base_url") or "http://127.0.0.1:23119"),
        timeout=float(api_config.get("timeout_seconds") or 1.5),
    )
    if not probe.get("running"):
        raise RuntimeError("Zotero 本地连接器未运行；请先启动 Zotero 再执行刷新")
    script = Path(__file__).with_name("refresh_word.ps1")
    if not script.exists():
        raise FileNotFoundError(script)
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-InputPath",
        str(input_path.resolve()),
        "-OutputPath",
        str(output_path.resolve()),
        "-Macro",
        str(word_config.get("refresh_macro") or "Zotero.ZoteroRefresh"),
    ]
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"Word/Zotero 刷新失败: stdout={process.stdout[-1000:]!r}; "
            f"stderr={process.stderr[-2000:]!r}"
        )
    audit = audit_zotero_docx(output_path)
    return {
        "input": str(input_path.resolve()),
        "output": str(output_path.resolve()),
        "probe": probe,
        "stdout": process.stdout[-2000:],
        "stderr": process.stderr[-2000:],
        "audit": audit,
        "pass": bool(audit.get("pass")),
    }


def run(args: dict[str, Any]) -> dict[str, Any]:
    mode = str(args.get("mode") or "audit")
    if mode == "probe":
        config = _config()
        api_config = config.get("local_api") or {}
        return {
            "mode": mode,
            "zotero": probe_zotero_local(
                str(
                    args.get("base_url")
                    or api_config.get("base_url")
                    or "http://127.0.0.1:23119"
                ),
                timeout=float(api_config.get("timeout_seconds") or 1.5),
            ),
            "word_plugin": _word_plugin_status(),
        }
    if mode == "audit":
        path = args.get("input") or args.get("docx")
        if not path:
            raise ValueError("audit 模式需要 input 或 docx")
        return {"mode": mode, "audit": audit_zotero_docx(Path(str(path)))}
    if mode == "manifest":
        source = args.get("source") or args.get("input")
        if not source:
            raise ValueError("manifest 模式需要 source 或 input")
        output = args.get("out") or args.get("manifest")
        manifest = extract_reference_manifest(
            Path(str(source)),
            output=Path(str(output)) if output else None,
        )
        return {"mode": mode, "manifest": manifest}
    if mode == "rebind":
        source = args.get("source")
        target = args.get("target") or args.get("input")
        output = args.get("output") or args.get("out")
        if not source or not target or not output:
            raise ValueError("rebind 模式需要 source、target/input、output/out")
        return {
            "mode": mode,
            "report": rebind_zotero_fields(
                Path(str(source)),
                Path(str(target)),
                Path(str(output)),
                manifest=(
                    Path(str(args["manifest"])) if args.get("manifest") else None
                ),
                report_path=(
                    Path(str(args["report"])) if args.get("report") else None
                ),
            ),
        }
    if mode == "refresh":
        source = args.get("input") or args.get("docx")
        output = args.get("output") or args.get("out")
        if not source or not output:
            raise ValueError("refresh 模式需要 input/docx 与 output/out")
        return {
            "mode": mode,
            "report": _refresh_word(Path(str(source)), Path(str(output))),
        }
    raise ValueError(f"未知 mode: {mode}")
