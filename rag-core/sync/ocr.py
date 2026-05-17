"""FileSync OCR 预处理模块 (PaddleOCR 后端)。

对纯图片和扫描版 EPUB/PDF 执行 OCR，产出 .md 供 FileSync 上传。
运行于 sync worker 线程内，不阻塞 watchdog。

依赖: pip install paddlepaddle paddleocr pillow
"""
from __future__ import annotations

import logging
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

logger = logging.getLogger("rag-core.sync.ocr")

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"})
EPUB_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"})

_ocr_engine = None


def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from paddleocr import PaddleOCR
            _ocr_engine = PaddleOCR(
                lang="ch",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
            logger.info("PaddleOCR engine initialised (CPU, Chinese)")
        except ImportError:
            logger.warning("PaddleOCR not installed")
            return None
    return _ocr_engine


def _ocr_image_text(img_path: Path) -> str:
    engine = _get_ocr()
    if engine is None:
        return ""
    try:
        result = engine.predict(str(img_path))
        if not result:
            return ""
        lines: list[str] = []
        for page in result:
            for text in page.get("rec_texts", []):
                if text.strip():
                    lines.append(text.strip())
        return "\n".join(lines)
    except Exception as e:
        logger.warning("ocr_image(%s) failed: %s", img_path.name, e)
        return ""


def _extract_epub_images(epub_path: Path, work_dir: Path) -> list[Path]:
    extracted: list[Path] = []
    with zipfile.ZipFile(epub_path) as zf:
        for name in sorted(zf.namelist()):
            if any(name.lower().endswith(ext) for ext in EPUB_IMAGE_EXTS):
                info = zf.getinfo(name)
                if info.file_size < 10_000:
                    continue
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
    suff = path.suffix.lower()
    if suff in IMAGE_EXTS:
        return True
    if suff == ".epub":
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
    if _get_ocr() is None:
        logger.warning("ocr_file: PaddleOCR unavailable, skip %s", path.name)
        return None

    out_dir = out_dir or path.parent
    out_path = out_dir / (path.stem + ".md")

    if out_path.exists():
        logger.info("ocr_file: output already exists %s", out_path.name)
        return out_path

    suff = path.suffix.lower()

    if suff in IMAGE_EXTS:
        logger.info("ocr_file: image %s", path.name)
        text = _ocr_image_text(path)
        if text:
            out_path.write_text(text, encoding="utf-8")
            logger.info("ocr_file: %s → %s (%d chars)", path.name, out_path.name, len(text))
            return out_path
        logger.warning("ocr_file: no text from image %s", path.name)
        return None

    if suff == ".epub":
        logger.info("ocr_file: EPUB %s", path.name)
        with tempfile.TemporaryDirectory(prefix="ocr_epub_") as tmp:
            work_dir = Path(tmp)
            images = _extract_epub_images(path, work_dir)
            if not images:
                return None
            texts: list[str] = []
            for i, img in enumerate(images):
                page_text = _ocr_image_text(img)
                if page_text:
                    texts.append(f"## Page {i+1}\n\n{page_text}")
            if not texts:
                return None
            combined = "\n\n".join(texts)
            out_path.write_text(combined, encoding="utf-8")
            logger.info("ocr_file: EPUB %s → %s (%d pages, %d chars)",
                        path.name, out_path.name, len(texts), len(combined))
        return out_path

    if suff == ".pdf":
        logger.info("ocr_file: PDF fallback %s", path.name)
        try:
            from pdf2image import convert_from_path
            images = convert_from_path(path, dpi=200, first_page=1, last_page=50)
        except Exception as e:
            logger.warning("ocr_file: pdf2image failed for %s: %s", path.name, e)
            return None
        if not images:
            return None
        texts: list[str] = []
        for i, img in enumerate(images):
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                img.save(tmp.name)
                page_text = _ocr_image_text(Path(tmp.name))
                Path(tmp.name).unlink(missing_ok=True)
            if page_text:
                texts.append(f"## Page {i+1}\n\n{page_text}")
        if not texts:
            return None
        combined = "\n\n".join(texts)
        out_path.write_text(combined, encoding="utf-8")
        logger.info("ocr_file: PDF %s → %s (%d pages, %d chars)",
                     path.name, out_path.name, len(texts), len(combined))
        return out_path

    return None
