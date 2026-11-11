"""扫描件 OCR 兜底：优先视觉大模型，其次本地 tesseract，最后明确返回空并标记。

为什么不默认上 PaddleOCR：paddle 依赖重、安装易失败，8 天工期赌不起；
视觉大模型按页调用成本极低（扫描件用例只有 15 页），且中文/公式识别明显更好。
"""
from __future__ import annotations

import base64
import io

import httpx

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)


def _render_png(path: str, page_no: int, dpi: int = 200) -> bytes:
    import pymupdf

    doc = pymupdf.open(path)
    try:
        page = doc[page_no - 1]
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _ocr_vlm(png: bytes) -> str:
    """调用 OpenAI 兼容的视觉模型接口。"""
    if not (settings.vlm_base_url and settings.vlm_api_key and settings.vlm_model):
        return ""
    b64 = base64.b64encode(png).decode()
    payload = {
        "model": settings.vlm_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "请把这页学术论文图片完整转写为纯文本。要求："
                            "1) 保留段落顺序与换行；2) 公式用 LaTeX 行内写法；"
                            "3) 表格用 Markdown 表格；4) 不要总结、不要翻译、不要添加任何说明；"
                            "5) 章节标题必须用 Markdown 标题语法标注层级："
                            "一级标题用 #、二级用 ##、三级用 ###（这是后续程序切分章节的唯一依据）；"
                            "6) 图注以 Figure N: 开头、表注以 Table N: 开头，各占一行。"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": 4000,
    }
    with httpx.Client(timeout=180) as client:
        resp = client.post(
            f"{settings.vlm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.vlm_api_key}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def _ocr_tesseract(png: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        img = Image.open(io.BytesIO(png))
        # 中英混合；没装中文包时退回 eng 不报错
        try:
            return pytesseract.image_to_string(img, lang="chi_sim+eng")
        except Exception:  # noqa: BLE001
            return pytesseract.image_to_string(img, lang="eng")
    except Exception as exc:  # noqa: BLE001
        log.warning("tesseract 不可用：%s", exc)
        return ""


def ocr_page(path: str, page_no: int) -> str:
    """返回该页 OCR 文本；三者都不可用时返回空串（上层据此标记"需人工核对"）。"""
    png = _render_png(path, page_no)

    text = _ocr_vlm(png)
    if text.strip():
        log.info("第 %d 页使用视觉模型 OCR", page_no)
        return text.strip()

    text = _ocr_tesseract(png)
    if text.strip():
        log.info("第 %d 页使用 tesseract OCR", page_no)
        return text.strip()

    log.warning("第 %d 页 OCR 不可用（未配置 VLM 且未安装 tesseract）", page_no)
    return ""


def ocr_available() -> dict:
    """给前端一个能力自检，避免评委点了没反应却不知道为什么。"""
    import importlib.util

    has_tess_bin = False
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        has_tess_bin = True
    except Exception:  # noqa: BLE001
        has_tess_bin = False

    return {
        "vlm_configured": bool(settings.vlm_base_url and settings.vlm_api_key),
        "tesseract_python_pkg": importlib.util.find_spec("pytesseract") is not None,
        "tesseract_binary": has_tess_bin,
    }
