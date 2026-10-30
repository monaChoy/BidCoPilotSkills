#!/usr/bin/env python3
"""用 Poppler 与 Pillow 批量登记 PDF 图片并生成接触表，不作真伪判断。"""

import argparse
import csv
import hashlib
import json
import math
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from image_metadata import inspect

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".ppm", ".pgm", ".pbm", ".ccitt"}
SOFTWARE_MARKERS = ("photoshop", "gimp", "paint.net", "fireworks", "acdsee", "picasa")


def parse_listing(text):
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 15 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append({
            "page": int(parts[0]),
            "num": int(parts[1]),
            "type": parts[2],
            "listed_width": int(parts[3]),
            "listed_height": int(parts[4]),
            "color": parts[5],
            "bpc": parts[7],
            "encoding": parts[8],
            "object_id": f"{parts[10]} {parts[11]}",
            "x_ppi": parts[12],
            "y_ppi": parts[13],
        })
    if not rows:
        raise RuntimeError("pdfimages -list 未返回图片对象")
    numbers = [row["num"] for row in rows]
    if len(set(numbers)) != len(numbers):
        raise RuntimeError("pdfimages 对象序号不唯一，无法建立提取映射")
    return rows


def extracted_files(directory, prefix):
    result = defaultdict(list)
    for path in directory.iterdir():
        if path.suffix.lower() not in IMAGE_SUFFIXES or not path.name.startswith(f"{prefix}-"):
            continue
        try:
            number = int(path.stem.rsplit("-", 1)[1])
        except (ValueError, IndexError):
            continue
        result[number].append(path)
    priority = {suffix: index for index, suffix in enumerate(
        [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".ppm", ".pgm", ".pbm", ".ccitt"]
    )}
    return {
        number: sorted(paths, key=lambda path: priority.get(path.suffix.lower(), 99))[0]
        for number, paths in result.items()
    }


def image_measurements(path):
    with Image.open(path) as image:
        gray = image.convert("L")
        gray.thumbnail((256, 256))
        histogram = gray.histogram()
        pixels = max(1, sum(histogram))
        nonwhite_ratio = sum(histogram[:250]) / pixels
        digest_image = gray.resize((9, 8), Image.Resampling.LANCZOS)
        values = digest_image.tobytes()
        bits = 0
        for row in range(8):
            for column in range(8):
                bits = (bits << 1) | int(values[row * 9 + column] > values[row * 9 + column + 1])
        with image.convert("RGB") as rgb:
            pixel_hash = hashlib.sha256(
                f"{image.width}x{image.height}:RGB".encode("ascii") + rgb.tobytes()
            ).hexdigest()
        return {
            "near_blank": nonwhite_ratio < 0.001,
            "nonwhite_ratio": round(nonwhite_ratio, 8),
            "dhash64": f"{bits:016x}",
            "pixel_sha256": pixel_hash,
        }


def metadata_fields(metadata):
    exif = metadata.get("exif", {})
    software = str(exif.get("305:Software", ""))
    header = metadata.get("jpeg_header") or {}
    kinds = [segment["kind"] for segment in header.get("segments", [])]
    raw = Path(metadata["path"]).read_bytes() if metadata.get("format") == "JPEG" else b""
    raw_resource = b"Photoshop 3.0\x00" in raw
    raw_software = b"Adobe Photoshop" in raw
    flags = []
    if software:
        flags.append(f"Software={software}")
    if "photoshop_resources" in kinds:
        flags.append("APP13=Photoshop 3.0")
    elif raw_resource:
        flags.append("原始字节候选=Photoshop 3.0（段位置未解析）")
    if raw_software and not software:
        flags.append("原始字节候选=Adobe Photoshop（字段未解析）")
    if "xmp" in kinds:
        flags.append("APP1=XMP")
    if "extended_xmp" in kinds:
        flags.append("APP1=Extended XMP")
    candidate = (
        any(marker in software.lower() for marker in SOFTWARE_MARKERS)
        or "photoshop_resources" in kinds
        or raw_resource
        or raw_software
    )
    return {
        "software": software,
        "datetime": str(exif.get("306:DateTime", "")),
        "metadata_flags": " | ".join(flags),
        "metadata_candidate": candidate,
    }


def base_row(listed, path):
    row = dict(listed)
    row.update({
        "extracted_path": str(path) if path else "",
        "sha256": "",
        "pixel_sha256": "",
        "format": "",
        "width": "",
        "height": "",
        "mode": "",
        "software": "",
        "datetime": "",
        "metadata_flags": "",
        "metadata_candidate": False,
        "metadata_status": "未读取",
        "error": "",
        "near_blank": False,
        "nonwhite_ratio": "",
        "dhash64": "",
        "exact_duplicate_of": "",
        "pixel_duplicate_of": "",
        "similar_group": "",
        "visual_review_status": "未初筛",
        "deep_review_status": "未深查",
        "evidence_path": "",
        "notes": "脚本仅完成机器预处理，视觉初筛须查看接触表后回写",
    })
    return row


def enrich_row(row, path):
    if path is None:
        row["metadata_status"] = "提取缺失"
        row["error"] = "未找到与 pdfimages 序号对应的提取文件"
        return
    try:
        metadata = inspect(path)
        if metadata.get("status") != "read":
            raise RuntimeError(metadata.get("error", "元数据读取失败"))
        row.update(metadata_fields(metadata))
        row.update({
            "sha256": metadata["sha256"],
            "format": metadata["format"],
            "width": metadata["size"][0],
            "height": metadata["size"][1],
            "mode": metadata["mode"],
            "metadata_status": "已读取",
        })
        row.update(image_measurements(path))
    except (OSError, RuntimeError, ValueError, UnidentifiedImageError) as error:
        row["metadata_status"] = "读取失败"
        row["error"] = f"{type(error).__name__}: {error}"


def mark_duplicates(rows):
    byte_groups = defaultdict(list)
    pixel_groups = defaultdict(list)
    hash_groups = defaultdict(list)
    for index, row in enumerate(rows):
        if row["sha256"]:
            byte_groups[row["sha256"]].append(index)
        if row["pixel_sha256"]:
            pixel_groups[row["pixel_sha256"]].append(index)
        if row["dhash64"] and not row["near_blank"]:
            hash_groups[row["dhash64"]].append(index)
    for group in byte_groups.values():
        if len(group) > 1:
            first = rows[group[0]]
            label = f"P{first['page']} / N{first['num']}"
            for index in group[1:]:
                rows[index]["exact_duplicate_of"] = label
    for group in pixel_groups.values():
        if len(group) > 1:
            first = rows[group[0]]
            label = f"P{first['page']} / N{first['num']}"
            for index in group[1:]:
                rows[index]["pixel_duplicate_of"] = label
    similar_number = 0
    for group in hash_groups.values():
        if len(group) > 1:
            similar_number += 1
            for index in group:
                rows[index]["similar_group"] = f"dhash-{similar_number:03d}"
    return {
        "exact_duplicate_references": sum(len(group) - 1 for group in byte_groups.values()),
        "pixel_duplicate_references": sum(len(group) - 1 for group in pixel_groups.values()),
        "similar_groups": similar_number,
    }


def contact_label(row):
    flags = []
    if row["metadata_candidate"]:
        flags.append("PS")
    if row["exact_duplicate_of"]:
        flags.append("DUP")
    if row["metadata_status"] != "已读取":
        flags.append("ERR")
    suffix = f" {'/'.join(flags)}" if flags else ""
    return f"P{row['page']} N{row['num']}{suffix}"


def make_contact_sheets(
    rows, output, columns, rows_per_sheet, tile_width, tile_height, prefix="接触表"
):
    per_sheet = columns * rows_per_sheet
    font = ImageFont.load_default()
    paths = []
    for sheet_index in range(math.ceil(len(rows) / per_sheet)):
        batch = rows[sheet_index * per_sheet:(sheet_index + 1) * per_sheet]
        sheet = Image.new("RGB", (columns * tile_width, rows_per_sheet * tile_height), "white")
        draw = ImageDraw.Draw(sheet)
        for index, row in enumerate(batch):
            x = (index % columns) * tile_width
            y = (index // columns) * tile_height
            outline = "#c00000" if row["metadata_candidate"] else "#999999"
            draw.rectangle((x, y, x + tile_width - 1, y + tile_height - 1), outline=outline, width=2)
            path = Path(row["extracted_path"]) if row["extracted_path"] else None
            try:
                if path is None:
                    raise OSError("提取文件缺失")
                with Image.open(path) as image:
                    image = image.convert("RGB")
                    image.thumbnail((tile_width - 20, tile_height - 55))
                    px = x + (tile_width - image.width) // 2
                    py = y + 8 + (tile_height - 55 - image.height) // 2
                    sheet.paste(image, (px, py))
            except (OSError, ValueError, UnidentifiedImageError):
                draw.text((x + 12, y + 55), "IMAGE READ ERROR", fill="#c00000", font=font)
            draw.text((x + 8, y + tile_height - 38), contact_label(row), fill="black", font=font)
            draw.text((x + 8, y + tile_height - 23), "PENDING REVIEW", fill="black", font=font)
        path = output / f"{prefix}-{sheet_index + 1:03d}.png"
        sheet.save(path)
        paths.append(path)
    return paths


def write_csv(rows, path):
    fields = [
        "page", "num", "object_id", "type", "listed_width", "listed_height", "color", "bpc",
        "encoding", "x_ppi", "y_ppi", "extracted_path", "sha256", "pixel_sha256", "format",
        "width", "height", "mode", "software", "datetime", "metadata_flags", "metadata_candidate",
        "metadata_status", "error", "near_blank", "nonwhite_ratio", "dhash64", "exact_duplicate_of",
        "pixel_duplicate_of", "similar_group", "visual_review_status", "deep_review_status",
        "evidence_path", "notes",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pdfimages", default="pdfimages")
    parser.add_argument("--prefix", default="img")
    parser.add_argument("--columns", type=int, default=5)
    parser.add_argument("--rows-per-sheet", type=int, default=6)
    parser.add_argument("--tile-width", type=int, default=300)
    parser.add_argument("--tile-height", type=int, default=250)
    parser.add_argument(
        "--candidate-sheets",
        action="store_true",
        help="额外生成元数据候选接触表，仅供深查；完整接触表仍覆盖全部对象",
    )
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("需要 Python 3.11 或以上")
    if not args.source.is_file():
        parser.error(f"输入不存在：{args.source}")
    if args.output.exists():
        parser.error(f"输出目录已存在，拒绝覆盖：{args.output}")
    if shutil.which(args.pdfimages) is None:
        parser.error(f"找不到 Poppler 工具：{args.pdfimages}")
    if min(args.columns, args.rows_per_sheet, args.tile_width, args.tile_height) <= 0:
        parser.error("接触表参数必须为正数")

    listing_result = subprocess.run(
        [args.pdfimages, "-list", str(args.source)], capture_output=True, text=True, check=True
    )
    listed = parse_listing(listing_result.stdout)
    args.output.mkdir(parents=True)
    extraction_result = subprocess.run(
        [args.pdfimages, "-all", str(args.source), str(args.output / args.prefix)],
        capture_output=True, text=True
    )
    if extraction_result.returncode != 0:
        raise RuntimeError(f"pdfimages 提取失败：{extraction_result.stderr.strip()}")
    extracted = extracted_files(args.output, args.prefix)
    rows = []
    for item in listed:
        row = base_row(item, extracted.get(item["num"]))
        enrich_row(row, extracted.get(item["num"]))
        rows.append(row)
    duplicate_stats = mark_duplicates(rows)
    ordered_rows = sorted(
        rows, key=lambda row: (not row["metadata_candidate"], row["page"], row["num"])
    )
    sheets = make_contact_sheets(
        ordered_rows,
        args.output,
        args.columns,
        args.rows_per_sheet,
        args.tile_width,
        args.tile_height,
    )
    candidate_rows = [row for row in ordered_rows if row["metadata_candidate"]]
    candidate_sheets = []
    if args.candidate_sheets:
        candidate_sheets = make_contact_sheets(
            candidate_rows,
            args.output,
            args.columns,
            args.rows_per_sheet,
            args.tile_width,
            args.tile_height,
            prefix="候选接触表",
        )
    write_csv(rows, args.output / "图片筛查表.csv")
    candidates = [
        {"page": row["page"], "num": row["num"], "evidence": row["metadata_flags"]}
        for row in candidate_rows
    ]
    errors = [
        {"page": row["page"], "num": row["num"], "error": row["error"]}
        for row in rows if row["metadata_status"] != "已读取"
    ]
    version = subprocess.run([args.pdfimages, "-v"], capture_output=True, text=True).stderr.strip()
    summary = {
        "source": str(args.source),
        "source_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
        "registered_objects": len(rows),
        "extracted_objects": len(extracted),
        "metadata_read": sum(row["metadata_status"] == "已读取" for row in rows),
        "metadata_errors": errors,
        "contact_sheet_pages": len(sheets),
        "contact_sheet_images": len(ordered_rows),
        "contact_sheets": [str(path) for path in sheets],
        "review_order_rule": "完整接触表按元数据候选优先、其余随后排列；仍须查看全部完整接触表",
        "review_order": [
            {
                "page": row["page"],
                "num": row["num"],
                "metadata_candidate": row["metadata_candidate"],
            }
            for row in ordered_rows
        ],
        "candidate_contact_sheet_pages": len(candidate_sheets),
        "candidate_contact_sheet_images": len(candidate_rows) if candidate_sheets else 0,
        "candidate_contact_sheets": [str(path) for path in candidate_sheets],
        "candidate_sheets_only_for_deep_review": True,
        "visual_reviewed": 0,
        "visual_review_pending": len(rows),
        "near_blank": sum(row["near_blank"] for row in rows),
        "metadata_candidates": candidates,
        **duplicate_stats,
        "status": "机器预处理完成；全部对象待视觉初筛",
        "parameters": {
            "near_blank": "256px灰阶缩略图中灰阶<250的像素占比<0.001；仅用于分组，不用于真实性判断",
            "dhash": "9x8灰阶、64位；同哈希仅作相似候选，不自动去重",
            "contact_sheet": {
                "columns": args.columns,
                "rows": args.rows_per_sheet,
                "tile_width": args.tile_width,
                "tile_height": args.tile_height,
                "image_width": args.columns * args.tile_width,
                "image_height": args.rows_per_sheet * args.tile_height,
            },
        },
        "tools": {"pdfimages": version, "pillow": Image.__version__},
        "warnings": (listing_result.stderr + extraction_result.stderr).strip(),
    }
    (args.output / "筛查汇总.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "registered": len(rows),
        "metadata_read": summary["metadata_read"],
        "contact_sheet_images": len(ordered_rows),
        "contact_sheet_pages": len(sheets),
        "candidate_contact_sheet_pages": len(candidate_sheets),
        "visual_reviewed": 0,
        "metadata_candidates": len(candidates),
        "errors": len(errors),
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return int(bool(errors) or len(extracted) != len(rows))


if __name__ == "__main__":
    raise SystemExit(main())
