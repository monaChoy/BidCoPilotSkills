#!/usr/bin/env python3
"""合并文档与图片紧凑摘要，不读取完整清单，不作风险判断。"""

import argparse
import json
from pathlib import Path

IMAGE_KEYS = (
    "registered_objects", "extracted_objects", "metadata_read", "metadata_errors",
    "contact_sheet_pages", "visual_reviewed", "visual_review_pending",
    "exact_duplicate_references", "pixel_duplicate_references", "similar_groups",
    "metadata_candidates", "status",
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-summary", required=True, type=Path)
    parser.add_argument("--image-summary", type=Path)
    parser.add_argument("--image-report", default="")
    parser.add_argument(
        "--image-status", choices=("completed", "partial", "not_applicable", "not_executed"),
        default="not_executed",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"输出文件已存在，拒绝覆盖：{args.output}")
    for path in (args.document_summary, args.image_summary):
        if path is not None and not path.is_file():
            parser.error(f"输入摘要不存在：{path}")
    document = read_json(args.document_summary)
    image_machine = read_json(args.image_summary) if args.image_summary else {}
    image = {
        "status": args.image_status,
        "report": args.image_report,
        "machine_preprocessing": {key: image_machine[key] for key in IMAGE_KEYS if key in image_machine},
        "boundary": "图片预处理计数不代替视觉初筛和深查；最终状态由实际图片复核结果提供。",
    }
    document_complete = document.get("status") == "complete"
    image_complete = args.image_status in {"completed", "not_applicable"}
    result = {
        "schema_version": 1,
        "status": "complete" if document_complete and image_complete else "incomplete",
        "branches": {"document": document, "image": image},
        "decision": "machine_aggregation_only",
    }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    print(json.dumps({
        "output": str(args.output), "status": result["status"],
        "document_status": document.get("status"), "image_status": args.image_status,
    }, ensure_ascii=False, indent=2))
    return int(result["status"] != "complete")


if __name__ == "__main__":
    raise SystemExit(main())
