"""FileSync OCR 预处理模块。

对纯图片和扫描版 EPUB/PDF 执行 OCR，产出 .txt 供 FileSync 上传。
运行于 sync worker 线程内，不阻塞 watchdog。

依赖:
  - tesseract (winget install UB-Mannheim.TesseractOCR)
  - pytesseract, pillow (pip install pytesseract pillow)
  - pdf2image + poppler (winget install poppler, pip install pdf2image)
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

logger = logging.getLogger("rag-core.sync.ocr")

# ---- image extensions that can be OCR'd directly ----
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"})

# ---- EPUB extensions that indicate images inside ----
EPUB_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"})


def _tesseract_available() -> bool:
    """Check tesseract is installed and Chinese language data exists."""
    if not shutil.which("tesseract"):
        return False
    # check chi_sim traineddata
    for search in (
        Path(os.environ.get("TESSDATA_PREFIX", ".")),
        Path(os.environ.get("LOCALAPPDATA", "."), "Tesseract-OCR", "tessdata"),
        Path("C:/Program Files/Tesseract-OCR/tessdata"),
    ):
        if (search / "chi_sim.traineddata").exists():
            return True
    return False


def _ocr_image(img_path: Path) -> str:
    """OCR single image, return extracted text."""
    import pytesseract

    try:
        img = Image.open(img_path)
        w, h = img.size
        if w > 2000:
            ratio = 2000 / w
            img = img.resize((2000, int(h * ratio)), Image.LANCZOS)
        text = pytesseract.image_to_string(img, lang="chi_sim", config="--psm 6")
        return text.strip()
    except Exception as e:
        logger.warning("ocr_image(%s) failed: %s", img_path.name, e)
        return ""


def _extract_epub_images(epub_path: Path, work_dir: Path) -> list[Path]:
    """Extract all non-trivial images from an EPUB."""
    extracted: list[Path] = []
    with zipfile.ZipFile(epub_path) as zf:
        for name in sorted(zf.namelist()):
            if any(name.lower().endswith(ext) for ext in EPUB_IMAGE_EXTS):
                info = zf.getinfo(name)
                if info.file_size < 10_000:
                    continue  # skip tiny images (covers, icons)
                out_name = name.replace("/", "_").replace("\\", "_")
                out_path = work_dir / out_name
                zf.extract(name, work_dir)
                actual = work_dir / name
                if actual.exists() and actual != out_path:
                    actual.rename(out_path)
                if out_path.exists():
                    extracted.append(out_path)
    return extracted


def needs_ocr(path: Path) -> bool:
    """判断文件是否必须经 OCR 预处理才能提取文字。

    条件:
      - 后缀是纯图片格式 → True
      - EPUB 且内部主要为图片 → True (RAGFlow naive parser 0 chunk)
      - PDF/DOCX/PPTX 等 → False (交给 RAGFlow 原生解析, parse_poll_loop 兜底)
    """
    suff = path.suffix.lower()
    if suff in IMAGE_EXTS:
        return True
    if suff == ".epub" and _tesseract_available():
        # 快速检测 EPUB 是否主要为图片
        try:
            img_bytes = 0
            text_bytes = 0
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    info = zf.getinfo(name)
                    nlow = name.lower()
                    if any(nlow.endswith(e) for e in EPUB_IMAGE_EXTS):
                        img_bytes += info.file_size
                    elif nlow.endswith((".html", ".xhtml", ".htm")):
                        text_bytes += info.file_size
            if img_bytes > 0 and img_bytes > text_bytes * 2:
                return True
        except Exception:
            pass
    return False


def ocr_file(path: Path, out_dir: Path | None = None) -> Path | None:
    """对文件执行 OCR, 产出一个 .txt 到同目录。

    返回 .txt 路径; 若 tesseract 不可用或 OCR 无输出则返回 None。
    """
    if not _tesseract_available():
        logger.warning("ocr_file: tesseract not available, skip %s", path.name)
        return None

    out_dir = out_dir or path.parent
    out_path = out_dir / (path.stem + ".txt")

    if out_path.exists():
        logger.info("ocr_file: output already exists %s", out_path.name)
        return out_path

    suff = path.suffix.lower()

    if suff in IMAGE_EXTS:
        # ----- direct image OCR -----
        logger.info("ocr_file: direct image %s", path.name)
        text = _ocr_image(path)
        if text:
            out_path.write_text(text, encoding="utf-8")
            logger.info("ocr_file: %s → %s (%d chars)", path.name, out_path.name, len(text))
            return out_path
        else:
            logger.warning("ocr_file: no text from %s", path.name)
            return None

    if suff == ".epub":
        # ----- EPUB image extraction + OCR -----
        logger.info("ocr_file: EPUB %s", path.name)
        with tempfile.TemporaryDirectory(prefix="ocr_epub_") as tmp:
            work_dir = Path(tmp)
            images = _extract_epub_images(path, work_dir)
            if not images:
                logger.warning("ocr_file: EPUB has no images %s", path.name)
                return None

            texts: list[str] = []
            for i, img in enumerate(images):
                page_text = _ocr_image(img)
                if page_text:
                    texts.append(f"--- Page {i+1} ---\n{page_text}")

            if not texts:
                logger.warning("ocr_file: no OCR text from EPUB %s", path.name)
                return None

            combined = "\n\n".join(texts)
            out_path.write_text(combined, encoding="utf-8")
            logger.info(
                "ocr_file: EPUB %s → %s (%d pages, %d chars)",
                path.name, out_path.name, len(texts), len(combined),
            )
        return out_path

    return None
