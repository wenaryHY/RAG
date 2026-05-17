"""EPUB → OCR → TXT 预处理管线。

用法：
  python ocr_epub.py <epub_path>
  python ocr_epub.py "D:/RAG/RAGfiles/pharmacy/book.epub"

功能：
  1. 将 EPUB 作为 ZIP 打开，提取所有图片
  2. 用 Tesseract (chi_sim) OCR 每张图片
  3. 合并文本输出到同目录下的 .txt 文件
  4. FileSync 引擎会自动检测到 .txt 并上传到 RAGFlow
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import pytesseract
from PIL import Image


TESSERACT_EXE = Path("C:/Program Files/Tesseract-OCR/tesseract.exe")
TESSDATA_DIR = Path(__file__).resolve().parent / "tessdata"

if TESSERACT_EXE.exists():
    pytesseract.pytesseract.tesseract_cmd = str(TESSERACT_EXE)
os.environ.setdefault("TESSDATA_PREFIX", str(TESSDATA_DIR))


def extract_images(epub_path: Path, work_dir: Path) -> list[Path]:
    """从 EPUB 中提取所有图片到 work_dir，返回路径列表。"""
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp"}
    extracted: list[Path] = []
    with zipfile.ZipFile(epub_path) as zf:
        for name in sorted(zf.namelist()):
            if any(name.lower().endswith(ext) for ext in image_exts):
                # 跳过封面等小图（通常 <10KB）
                info = zf.getinfo(name)
                if info.file_size < 10000:
                    continue
                # 保持文件名避免冲突
                out_name = name.replace("/", "_").replace("\\", "_")
                out_path = work_dir / out_name
                zf.extract(name, work_dir)
                # 如果 extract 创建了子目录，移动文件到根
                actual = work_dir / name
                if actual.exists() and actual != out_path:
                    actual.rename(out_path)
                if out_path.exists():
                    extracted.append(out_path)
    return extracted


def ocr_image(img_path: Path, index: int, total: int) -> str:
    """对单张图片执行 OCR，返回提取的文字。"""
    try:
        img = Image.open(img_path)
        # 大图先缩小以提高 OCR 速度（宽度 > 2000 的缩到 2000）
        w, h = img.size
        if w > 2000:
            ratio = 2000 / w
            img = img.resize((2000, int(h * ratio)), Image.LANCZOS)
        text = pytesseract.image_to_string(img, lang="chi_sim", config="--psm 6")
        text = text.strip()
        if text:
            print(f"  [{index+1}/{total}] {img_path.name}: {len(text)} chars")
        else:
            print(f"  [{index+1}/{total}] {img_path.name}: empty")
        return text
    except Exception as e:
        print(f"  [{index+1}/{total}] {img_path.name}: ERROR {e}")
        return ""


def main():
    parser = argparse.ArgumentParser(description="EPUB OCR → TXT 预处理")
    parser.add_argument("epub", help="EPUB 文件路径")
    parser.add_argument("-o", "--output", default=None, help="输出 TXT 路径（默认同目录同名 .txt）")
    parser.add_argument("--no-cleanup", action="store_true", help="保留临时图片文件")
    args = parser.parse_args()

    epub_path = Path(args.epub)
    if not epub_path.exists():
        raise SystemExit(f"file not found: {epub_path}")
    if epub_path.suffix.lower() != ".epub":
        raise SystemExit(f"not an EPUB file: {epub_path}")

    out_path = Path(args.output) if args.output else epub_path.with_suffix(".txt")
    if out_path.exists():
        print(f"[skip] output already exists: {out_path}")
        return

    print(f"EPUB: {epub_path.name}")
    print(f"Output: {out_path}")

    with tempfile.TemporaryDirectory(prefix="epub_ocr_") as tmp:
        work_dir = Path(tmp)
        images = extract_images(epub_path, work_dir)
        n = len(images)
        print(f"Images to OCR: {n} (filtered >10KB)")

        if n == 0:
            print("no images found — EPUB may be text-based, nothing to OCR")
            return

        texts: list[str] = []
        for i, img in enumerate(images):
            page_text = ocr_image(img, i, n)
            if page_text:
                texts.append(f"--- Page {i+1} ---\n{page_text}")

        if not texts:
            print("no text extracted from any image")
            return

        combined = "\n\n".join(texts)
        out_path.write_text(combined, encoding="utf-8")
        print(f"\nDone: {len(texts)}/{n} pages with text → {out_path}")
        print(f"Total: {len(combined)} chars, ~{len(combined)//1500} estimated chunks")
        print("FileSync will auto-upload the .txt file to RAGFlow.")


if __name__ == "__main__":
    main()
