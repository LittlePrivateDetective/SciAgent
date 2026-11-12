"""页图加载失败时的表现：不能只留一个破图标。

真事：公网实例上打开某篇 PDF，阅读区只有浏览器默认的破图标，用户不知道
是网络断、文件缺失还是后端渲染崩了。这条用例就是把"失败要有交代"钉住：

1. 正常情况下不出现占位（别把好状态也盖住）；
2. 图片请求失败时出现占位：一句人话 + 「重新加载」，并且文本层被清空
   （页图都没出来，上面再叠一层透明文字只会更乱）；
3. 点「重新加载」后真的恢复了：URL 带上新 nonce、图片重新渲染出宽度。

做法是把页图的 src 临时指到一个不存在的 doc_id，制造一次真实的 error 事件
（不篡改数据库，也不依赖后端故障）。

用法：./.venv/Scripts/python.exe tests/ui_img_fail.py
证据：docs/qa/evidence/img_fail_{ok,state,retry}.png
"""

import base64
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

import websocket

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
OUT = Path("docs/qa/evidence")
PORT = 9877
profile = Path(tempfile.gettempdir()) / f"_p_imgfail_{int(time.time())}"
profile.mkdir(parents=True, exist_ok=True)
proc = subprocess.Popen(
    [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
     f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
     f"--user-data-dir={profile}", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ws = None
try:
    t = None
    for _ in range(60):
        try:
            with urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2) as r:
                t = json.load(r)
            if t:
                break
        except Exception:
            time.sleep(0.3)
    page = next(x for x in t if x["type"] == "page")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=90)
    seq = [0]

    def call(m, p=None):
        seq[0] += 1
        ws.send(json.dumps({"id": seq[0], "method": m, "params": p or {}}))
        while True:
            d = json.loads(ws.recv())
            if d.get("id") == seq[0]:
                return d.get("result", {})

    def js(e):
        r = call("Runtime.evaluate", {"expression": e, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            return {"__err": str(r["exceptionDetails"])[:300]}
        return (r.get("result") or {}).get("value")

    def shot(name):
        d = call("Page.captureScreenshot", {"format": "png"}).get("data")
        if d:
            (OUT / name).write_bytes(base64.b64decode(d))

    call("Page.enable")
    call("Runtime.enable")
    call("Emulation.setDeviceMetricsOverride",
         {"width": 1280, "height": 800, "deviceScaleFactor": 1, "mobile": False})
    call("Page.navigate", {"url": "http://127.0.0.1:8013/"})
    for _ in range(80):
        if js("!!(window.__sciagent && window.__sciagent.docs.length)"):
            break
        time.sleep(0.4)
    time.sleep(2.5)

    res = {}
    res["healthy"] = js("""(()=>{const v=window.__sciagent;
      const im=document.querySelector('.pdf-page');
      return {failed:v.imgFailed, nat:im?im.naturalWidth:0, place:!!document.querySelector('.img-fail')}})()""")
    shot("img_fail_ok.png")

    # 把 src 指到一个不存在的文档，制造一次真实的图片加载失败
    js("""(()=>{const im=document.querySelector('.pdf-page');
      im.src='/api/documents/nope/page/1.png'; return 1})()""")
    time.sleep(1.6)
    res["after_error"] = js("""(()=>{const v=window.__sciagent;
      const el=document.querySelector('.img-fail');
      return {failed:v.imgFailed, place:!!el,
              text:el?el.innerText.replace(/\\s+/g,' ').slice(0,90):'',
              btn:!!(el&&[...el.querySelectorAll('button')].find(b=>b.textContent.includes('重新加载'))),
              words:v.tl.words.length}})()""")
    shot("img_fail_state.png")

    js("""(()=>{const el=document.querySelector('.img-fail');
      [...el.querySelectorAll('button')].find(b=>b.textContent.includes('重新加载')).click(); return 1})()""")
    time.sleep(2.4)
    res["after_retry"] = js("""(()=>{const v=window.__sciagent;
      const im=document.querySelector('.pdf-page');
      return {failed:v.imgFailed, place:!!document.querySelector('.img-fail'),
              nat:im?im.naturalWidth:0, src:im?im.getAttribute('src'):''}})()""")
    shot("img_fail_retry.png")

    res["checks"] = {
        "healthy_no_placeholder": (res["healthy"]["failed"] is False
                                   and res["healthy"]["place"] is False
                                   and (res["healthy"]["nat"] or 0) > 0),
        "placeholder_on_error": (res["after_error"]["failed"] is True
                                 and res["after_error"]["place"] is True
                                 and res["after_error"]["btn"] is True
                                 and "没加载出来" in (res["after_error"]["text"] or "")),
        "text_layer_cleared": (res["after_error"]["words"] or 0) == 0,
        "retry_recovers": (res["after_retry"]["failed"] is False
                           and res["after_retry"]["place"] is False
                           and (res["after_retry"]["nat"] or 0) > 0
                           and "v=1" in (res["after_retry"]["src"] or "")),
    }
    res["ok"] = all(res["checks"].values())
    print(json.dumps(res, ensure_ascii=False, indent=1))
    raise SystemExit(0 if res["ok"] else 1)
finally:
    if ws:
        ws.close()
    proc.terminate()
    time.sleep(0.5)
    shutil.rmtree(profile, ignore_errors=True)
