#!/usr/bin/env python3
"""使用临时产物验证状态索引的登记、复用、失效和增量页集合。"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(script, action, state, entry=None, **options):
    command = [sys.executable, "-B", str(script), action, "--state", str(state)]
    if entry:
        command.extend(["--entry", str(entry)])
    for key, value in options.items():
        if value is not None:
            command.extend(["--" + key.replace("_", "-"), value])
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(command, capture_output=True, text=True, env=environment)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError((result.returncode, result.stdout, result.stderr))
    return result.returncode, payload


def main():
    script = Path(__file__).with_name("review_state.py")
    with tempfile.TemporaryDirectory(prefix="review-state-selfcheck-") as name:
        root = Path(name)
        source = root / "input.pdf"
        artifact = root / "full.txt"
        state = root / "检查状态.json"
        entry_path = root / "entry.json"
        source.write_bytes(b"synthetic state input")
        artifact.write_text("synthetic extracted text", encoding="utf-8")
        text_entry = {
            "kind": "text", "input_path": str(source), "input_sha256": digest(source),
            "scope": {"pages": "all", "page_mapping_verified": True},
            "tool_contract": "pdftotext-layout-test-v1", "artifact_path": str(artifact),
            "status": "已完成",
        }
        entry_path.write_text(json.dumps(text_entry, ensure_ascii=False), encoding="utf-8")
        code, payload = run(script, "register", state, entry_path)
        assert code == 0 and payload["registered"], payload
        code, payload = run(script, "show", state, input_sha256=digest(source), kind="text",
                            tool_contract="pdftotext-layout-test-v1", scope=json.dumps(text_entry["scope"]))
        assert code == 0 and payload["entry_count"] == 1 and payload["entries"][0]["reusable"], payload
        code, payload = run(script, "show", state, kind="text", tool_contract="different-tool")
        assert payload["entry_count"] == 0

        artifact.write_text("changed artifact", encoding="utf-8")
        code, payload = run(script, "verify", state, kind="text")
        assert code == 1 and "产物哈希已变化" in payload["entries"][0]["reasons"], payload
        artifact.write_text("synthetic extracted text", encoding="utf-8")
        run(script, "register", state, entry_path)

        evidence = root / "evidence"
        evidence.mkdir()
        page_report = root / "pages.md"
        page_report.write_text("synthetic page review", encoding="utf-8")
        page_entry = {
            "kind": "page-review", "input_path": str(source), "input_sha256": digest(source),
            "scope": {"required_pages": [1, 2, 3]}, "tool_contract": "visual-v1",
            "artifact_path": str(page_report), "report_path": str(page_report),
            "status": "部分完成", "pages": [1, 2], "evidence_path": str(evidence),
        }
        entry_path.write_text(json.dumps(page_entry, ensure_ascii=False), encoding="utf-8")
        code, payload = run(script, "register", state, entry_path)
        assert code == 0 and payload["details"]["missing_pages"] == [3], payload

        missing_evidence = dict(page_entry)
        missing_evidence.pop("evidence_path")
        entry_path.write_text(json.dumps(missing_evidence, ensure_ascii=False), encoding="utf-8")
        code, payload = run(script, "register", state, entry_path)
        assert code == 1 and any("证据目录不可读" in reason for reason in payload["reasons"]), payload

        image_csv = root / "images.csv"
        sheet = root / "contact-001.png"
        sheet.write_bytes(b"synthetic contact sheet")
        image_csv.write_text(
            "visual_review_status\n已初筛-接触表001-第1块\n已初筛-接触表001-第2块\n",
            encoding="utf-8",
        )
        image_entry = {
            "kind": "image", "input_path": str(source), "input_sha256": digest(source),
            "scope": {"object_count": 2, "contact_sheets": [str(sheet)]},
            "tool_contract": "image-screen-v1", "artifact_path": str(image_csv),
            "status": "已完成",
        }
        entry_path.write_text(json.dumps(image_entry, ensure_ascii=False), encoding="utf-8")
        code, payload = run(script, "register", state, entry_path)
        assert code == 0 and payload["details"]["visual_reviewed"] == 2, payload

        image_csv.write_text("visual_review_status\n已初筛\n未初筛\n", encoding="utf-8")
        code, payload = run(script, "register", state, entry_path)
        assert code == 1 and any("仍有未初筛对象" in reason for reason in payload["reasons"]), payload

        entry_path.write_text(json.dumps(text_entry, ensure_ascii=False), encoding="utf-8")
        source.write_bytes(b"changed source")
        code, payload = run(script, "verify", state, kind="text")
        assert code == 1 and "输入哈希已变化" in payload["entries"][0]["reasons"], payload

        source.write_bytes(b"synthetic state input")
        valid_before = state.read_bytes()
        state.write_text("{broken", encoding="utf-8")
        code, payload = run(script, "register", state, entry_path)
        assert code == 1 and "拒绝覆盖" in payload["error"], payload
        assert state.read_text(encoding="utf-8") == "{broken"
        assert valid_before != state.read_bytes()
        print("通过：有效登记、契约/范围筛选、产物与输入失效、逐页差集、证据目录、图片 CSV 实算及损坏索引保护。")


if __name__ == "__main__":
    main()
