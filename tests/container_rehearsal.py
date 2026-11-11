"""Docker 布局预演：在没有 Docker 的本机，尽可能真实地验证「镜像能起来」。

本机未安装 Docker，不能冒充 `docker build` 成功。但可以验证构建所需的
**全部可验证前提**：

1. 静态：Dockerfile 每条 COPY 的源都存在；compose YAML 可解析；
   requirements 的每个包在本机实测环境中都有对应版本；.env.example
   覆盖 config.Settings 的所有字段（大小写不敏感）；CMD 与实测启动一致。
2. 动态：按 Dockerfile 的 COPY 清单搭出等价的 `/app` 目录（只放镜像里
   真正会有的文件，不复制 .env、不复制 data/），用零密钥 mock 模式
   真实启动 uvicorn，探测 /health、/api/system/info、/、/static/app.js。

这是「从零复现」在本机可达到的最强证据；真实 docker build 仍标【待实测】。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
# 直接以 `python tests/container_rehearsal.py` 运行时 sys.path[0] 是 tests/，
# 需要显式把项目根加进来才能 import app.*
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def static_check() -> dict:
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    copies = re.findall(r"^COPY\s+(.+?)\s+(\S+)\s*$", df, re.M)
    copy_ok = {}
    for srcs, _dst in copies:
        for src in srcs.split():
            copy_ok[src] = (ROOT / src).exists()
    cmd = re.search(r"^CMD\s+(.+)$", df, re.M)
    cmd_txt = cmd.group(1) if cmd else ""

    # 判据修正（QA 第三轮）：不是"requirements 每个包本机都装了"——OCR/本地向量
    # 这类可选兜底包本机故意不装；真正的可复现性要求是
    # 「代码里 import 的每个第三方包都在 requirements 里有声明」，
    # 否则容器里会静默降级（langgraph 缺失就是这么漏掉的）。
    import ast
    import importlib.metadata as md

    stdlib = set(sys.stdlib_module_names) | {"app", "tests", "__future__"}
    imported: set[str] = set()
    for f in (ROOT / "app").rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
    third_party = sorted(imported - stdlib)
    pypi_name = {"pil": "Pillow", "pymupdf": "PyMuPDF", "yaml": "PyYAML",
                 "rank_bm25": "rank-bm25", "sklearn": "scikit-learn",
                 "pydantic_settings": "pydantic-settings",
                 "sentence_transformers": "sentence-transformers"}
    # 包名归一化：PyPI 允许 - _ . 混用（pydantic-settings 导入名是 pydantic_settings）
    norm_name = lambda s: re.sub(r"[-_.]", "", s.lower())  # noqa: E731
    req_names = set()
    for line in req.splitlines():
        line = line.split("#")[0].strip()
        if line:
            req_names.add(norm_name(re.split(r"[><=\[\]]", line)[0].strip()))

    # 显式选择不安装的可选档：requirements.txt 里有注释说明理由，
    # 代码路径带兜底（仅 EMB_PROVIDER=local 才触发）。不算"漏声明"。
    OPTIONAL_NOT_BUNDLED = {"sentence-transformers"}

    pkgs, undeclared, optional_skipped = [], [], []
    for mod in third_party:
        name = pypi_name.get(mod.lower(), mod)
        try:
            ver = md.version(name)
        except md.PackageNotFoundError:
            ver = ""  # 可选兜底包本机可以没装
        declared = norm_name(name) in req_names
        pkgs.append({"import": mod, "package": name, "declared": declared,
                     "installed": ver or "(可选，本机未装)"})
        if not declared:
            (optional_skipped if name in OPTIONAL_NOT_BUNDLED else undeclared).append(name)

    # 从 Settings 字段反查 .env.example 覆盖率
    from app.core.config import Settings
    env_keys = set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=", line.strip())
        if m:
            env_keys.add(m.group(1).lower())
    settings_fields = set(Settings.model_fields.keys())
    uncovered = sorted(settings_fields - env_keys)

    return {
        "copy_sources": copy_ok,
        "copy_all_exist": all(copy_ok.values()),
        "compose_services": list((compose or {}).get("services", {}).keys()),
        "compose_port": (((compose or {}).get("services", {}).get("sciagent", {})
                          .get("ports") or ["?"])[0]),
        "compose_data_volume": bool((compose or {}).get("services", {})
                                    .get("sciagent", {}).get("volumes")),
        "cmd": cmd_txt,
        "cmd_matches_local": "uvicorn" in cmd_txt and "app.main:app" in cmd_txt,
        "third_party_imports": third_party,
        "packages": pkgs,
        "undeclared_packages": undeclared,
        "optional_not_bundled": optional_skipped,
        "env_fields_total": len(settings_fields),
        "env_uncovered": uncovered,
        "static_ok": (all(copy_ok.values()) and bool(compose) and not undeclared
                      and not uncovered and "uvicorn" in cmd_txt),
    }


def layout_and_boot() -> dict:
    """按 COPY 清单搭等价 /app，零密钥 mock 启动，返回真实接口响应。

    用时间戳独立目录、不删除旧目录：预演产物本身就是 QA 证据，
    也避免 rmtree 触发批量删除保护。
    """
    tmp = ROOT / "data" / f"_qa_container_{time.strftime('%H%M%S')}_{os.getpid()}"
    app_root = tmp / "app"
    app_root.mkdir(parents=True, exist_ok=False)
    # 与 .dockerignore 语义一致：镜像不会收 QA 证据/浏览器 profile，
    # 预演也必须排除，否则 docs/qa/evidence 会被递归复制放大到数百 MB。
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "evidence",
                                    "_profile_*", "_cdp_*", "_p_*", "_qa_*")
    for src in ("app", "web", "docs"):
        shutil.copytree(ROOT / src, app_root / src, ignore=ignore)
    shutil.copy2(ROOT / ".env.example", app_root / ".env.example")
    shutil.copy2(ROOT / "requirements.txt", app_root / "requirements.txt")
    for sub in ("pdf", "uploads", "pages", "exports", "logs", "chroma"):
        (app_root / "data" / sub).mkdir(parents=True, exist_ok=True)

    # 故意不放 .env：容器靠 env_file / environment 注入，不该依赖仓库里的密钥文件
    env = {
        "PATH": str(Path(sys.executable).parent) + ";" + str(Path(sys.executable).parent / "Scripts"),
        "PYTHONPATH": str(app_root),
        "DATA_DIR": str(app_root / "data"),
        "VECTOR_BACKEND": "sqlite",
        "EMB_PROVIDER": "hash",
        "RERANK_PROVIDER": "none",
        "LLM_MOCK": "1",
        "LLM_ROUTE": "mock",
        "PORT": "0",
        "LOG_LEVEL": "WARNING",
        "PYTHONIOENCODING": "utf-8",
    }
    for k in ("SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(app_root), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="ignore",
    )
    out = {"layout_files": sorted(str(p.relative_to(app_root)) for p in app_root.iterdir()),
           "has_dotenv": (app_root / ".env").exists(), "port": port}
    try:
        c = httpx.Client(timeout=5)
        alive = False
        for _ in range(45):
            try:
                if c.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                    alive = True
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.6)
        out["booted"] = alive
        if alive:
            probes = {}
            for path in ("/health", "/api/system/info", "/", "/static/app.js", "/docs"):
                try:
                    r = c.get(f"http://127.0.0.1:{port}{path}")
                    probes[path] = {"status": r.status_code, "bytes": len(r.content)}
                except Exception as exc:
                    probes[path] = {"status": "ERR", "error": str(exc)[:100]}
            out["probes"] = probes
            out["info"] = c.get(f"http://127.0.0.1:{port}/api/system/info").json()
            out["all_200"] = all(p["status"] == 200 for p in probes.values())
        else:
            try:
                out["boot_log"] = proc.communicate(timeout=3)[0][-1500:]
            except Exception:
                out["boot_log"] = ""
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
    return out


if __name__ == "__main__":
    res = {"static": static_check()}
    if res["static"]["static_ok"]:
        res["boot"] = layout_and_boot()
    print(json.dumps(res, ensure_ascii=False, indent=2))
