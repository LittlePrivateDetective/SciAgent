"""UI 布局测量：用 CDP 打开真实页面，读回关键元素的几何信息。

为什么需要它：前端对齐问题肉眼看截图容易漏掉 1px 级错位，
而且改完不好证明"确实修好了"。这里直接取 getBoundingClientRect，
输出每个控件的 x/width/height/中心线，修复前后各跑一次即可对照。

用法：
  ./.venv/Scripts/python.exe tests/ui_layout_check.py            # 打印 JSON
  ./.venv/Scripts/python.exe tests/ui_layout_check.py out.json   # 存文件
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import websocket

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
BASE = "http://127.0.0.1:8013"

# 需要测量的元素：右栏工具栏是本次重点，同时覆盖中栏/左栏头部做回归
JS = r"""
(() => {
  const px = (v) => Math.round(v * 100) / 100;
  const rect = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {x: px(r.x), y: px(r.y), w: px(r.width), h: px(r.height),
            cy: px(r.y + r.height / 2), right: px(r.right)};
  };
  const pick = (sel) => rect(document.querySelector(sel));
  const head = document.querySelector('.panel.right .panel-head');
  const tools = document.querySelector('.chat-tools');
  const out = {
    viewport: {w: window.innerWidth, h: window.innerHeight},
    rightHead: rect(head),
    tools: rect(tools),
    title: rect(head && head.querySelector('span')),
    select: pick('.chat-tools .el-select'),
    selectInner: pick('.chat-tools .el-select__wrapper'),
    radioGroup: pick('.chat-tools .el-radio-group'),
    radioBtns: [...document.querySelectorAll('.chat-tools .el-radio-button')].map(rect),
    radioInners: [...document.querySelectorAll('.chat-tools .el-radio-button__inner')].map(rect),
    sw: pick('.chat-tools .el-switch'),
    swLabel: pick('.chat-tools .el-switch__label'),
    ask: pick('.ask-bar'),
    sendBtn: pick('.ask-bar .el-button'),
    input: pick('.ask-bar .el-input'),
  };
  // 对齐判定：同一行内的控件中心线极差
  const items = [out.select, out.radioGroup, out.sw].filter(Boolean);
  out.rowCenterSpread = items.length
    ? px(Math.max(...items.map(i => i.cy)) - Math.min(...items.map(i => i.cy)))
    : null;
  // 溢出判定：工具栏右边界是否超出头部右边界
  out.overflowPx = (out.tools && out.rightHead)
    ? px(out.tools.right - (out.rightHead.right - 10)) : null;
  // 高度是否统一
  out.heights = {select: out.select && out.select.h,
                 radioGroup: out.radioGroup && out.radioGroup.h,
                 sw: out.sw && out.sw.h};
  return JSON.stringify(out);
})()
"""


def measure(url: str = BASE + "/", win: str = "1680,1000") -> dict:
    port = 9444 + (int(time.time()) % 200)
    profile = ROOT / "docs" / "qa" / "evidence" / f"_p_layout_{int(time.time())}"
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}",
         f"--window-size={win}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ws = None
    try:
        targets = None
        for _ in range(60):
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                    targets = json.load(r)
                if targets:
                    break
            except Exception:
                time.sleep(0.4)
        if not targets:
            return {"error": "CDP 端口未就绪"}
        page = next((t for t in targets if t.get("type") == "page"), targets[0])
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15)
        seq = [0]

        def call(method, params=None):
            seq[0] += 1
            mid = seq[0]
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    return msg.get("result", {})

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": url})
        # 轮询等待 Vue 挂载完成，而不是固定 sleep：
        # CDN 冷启动时 4.5s 不够，测出来全是 null 会被误判成"布局崩了"。
        probe = ("document.querySelector('.chat-tools .el-switch') ? 1 : 0")
        for _ in range(40):
            time.sleep(0.5)
            res = call("Runtime.evaluate", {"expression": probe, "returnByValue": True})
            if (res.get("result") or {}).get("value") == 1:
                break
        time.sleep(0.6)  # 让 Element Plus 完成一轮布局
        res = call("Runtime.evaluate", {"expression": JS, "returnByValue": True})
        val = (res.get("result") or {}).get("value")
        return json.loads(val) if val else {"error": "evaluate 返回空"}
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    win = sys.argv[2] if len(sys.argv) > 2 else "1680,1000"
    data = measure(win=win)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))
