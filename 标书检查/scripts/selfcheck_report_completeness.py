#!/usr/bin/env python3
"""使用临时虚构报告验证范围闭合、状态、哈希和结构化交接闸门。"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def run(script, report, handoff=None, require_complete=False):
    command = [sys.executable, "-B", str(script), "--report", str(report)]
    if handoff:
        command.extend(["--handoff", str(handoff)])
    if require_complete:
        command.append("--require-complete")
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(command, capture_output=True, text=True, env=environment)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError((result.returncode, result.stdout, result.stderr))
    return result.returncode, payload


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_field(hashes):
    return "、".join(f"`{value}`" for value in hashes) if hashes else "无"


def write_trace(root, sources, status="已完成", machine_status=None):
    document = root / "文档痕迹"
    document.mkdir(parents=True, exist_ok=True)
    (root / "文件痕迹报告.md").write_text(
        f"# 合成文件痕迹报告\n- 检查执行状态：{status}\n", encoding="utf-8"
    )
    (document / "summary.json").write_text(json.dumps({
        "status": machine_status or ("complete" if status == "已完成" else "incomplete"),
        "files": [{"path": str(path), "sha256": digest(path)} for path in sources],
    }, ensure_ascii=False), encoding="utf-8")


def write_main(path, registered, expected, overall="已完成", branch="已完成", include_section=True,
               include_path=True, include_scope=True):
    inputs = "\n".join(f"- 输入 SHA-256 `{value}`" for value in registered)
    trace = ""
    if include_section:
        trace = (
            "\n## 文件痕迹检查（固定分支）\n"
            f"- 分支状态：{branch}\n"
            f"- 配套报告及证据目录：{'`文件痕迹检查-synthetic/`' if include_path else '无'}\n"
        )
        if include_scope:
            trace += f"- 痕迹应检输入SHA-256：{hash_field(expected)}\n"
    path.write_text(
        f"# 合成标书检查报告\n- 状态：{overall}\n\n## 输入版本、范围与盲区\n{inputs}\n{trace}",
        encoding="utf-8",
    )


def write_handoff(path, status="已完成", reference="文件痕迹检查-synthetic/文件痕迹报告.md",
                  hashes=None, narrative="", include_fields=True):
    if not include_fields:
        path.write_text(narrative or "旧式交接，无结构字段。", encoding="utf-8")
        return
    path.write_text(
        f"- 文件痕迹分支状态：{status}\n"
        f"- 文件痕迹报告：{reference}\n"
        f"- 文件痕迹输入SHA-256：{hash_field(hashes or [])}\n"
        f"{narrative}\n",
        encoding="utf-8",
    )


def main():
    script = Path(__file__).with_name("check_report_completeness.py")
    with tempfile.TemporaryDirectory(prefix="report-completeness-selfcheck-") as name:
        root = Path(name)
        first = root / "first.pdf"
        second = root / "second.pdf"
        outside = root / "tender.pdf"
        first.write_bytes(b"first synthetic input")
        second.write_bytes(b"second synthetic input")
        outside.write_bytes(b"outside synthetic input")
        first_hash, second_hash, outside_hash = map(digest, (first, second, outside))
        expected = [first_hash, second_hash]
        trace_root = root / "文件痕迹检查-synthetic"
        main_report = root / "标书检查报告.md"
        handoff = root / "handoff.md"

        def valid():
            write_trace(trace_root, [first, second])
            write_main(main_report, [*expected, outside_hash], expected)
            write_handoff(handoff, hashes=expected)

        valid()
        code, payload = run(script, main_report, handoff)
        assert code == 0 and payload["trace"]["evidence_valid"], payload

        write_trace(trace_root, [first])
        code, payload = run(script, main_report)
        assert code == 1 and any("输入集合" in issue for issue in payload["issues"]), payload

        write_trace(trace_root, [first, second, outside])
        code, payload = run(script, main_report)
        assert code == 1 and any("输入集合" in issue for issue in payload["issues"]), payload

        unknown_hash = "0" * 64
        write_trace(trace_root, [first, second])
        write_main(main_report, expected, [*expected, unknown_hash])
        code, payload = run(script, main_report)
        assert code == 1 and any("未登记" in issue for issue in payload["issues"]), payload

        write_main(main_report, [*expected, outside_hash], expected, include_scope=False)
        code, payload = run(script, main_report)
        assert code == 1 and any("非空" in issue for issue in payload["issues"]), payload

        valid()
        write_trace(trace_root, [first, second], status="部分完成")
        code, payload = run(script, main_report)
        assert code == 1 and any("状态" in issue for issue in payload["issues"]), payload

        valid()
        write_main(main_report, [*expected, outside_hash], expected, include_section=False)
        code, payload = run(script, main_report)
        assert code == 1 and any("缺少" in issue for issue in payload["issues"]), payload

        valid()
        write_handoff(handoff, include_fields=False)
        code, payload = run(script, main_report, handoff)
        assert code == 1 and any("结构化" in issue or "状态字段" in issue for issue in payload["issues"]), payload

        write_handoff(handoff, hashes=[first_hash])
        code, payload = run(script, main_report, handoff)
        assert code == 1 and any("完整SHA-256集合" in issue for issue in payload["issues"]), payload

        write_handoff(handoff, reference="文件痕迹检查-other/文件痕迹报告.md", hashes=expected)
        code, payload = run(script, main_report, handoff)
        assert code == 1 and any("路径" in issue for issue in payload["issues"]), payload

        write_main(main_report, [*expected, outside_hash], expected, overall="部分完成", branch="未完成", include_path=False)
        write_handoff(handoff, status="未完成", reference="无", hashes=[],
                      narrative="文件痕迹旧报告未完成，新分支已完成。")
        code, payload = run(script, main_report, handoff)
        assert code == 1 and any("矛盾" in issue for issue in payload["issues"]), payload

        write_handoff(handoff, status="未完成", reference="无", narrative="痕迹分支已完成。")
        code, payload = run(script, main_report, handoff)
        assert code == 1 and any("矛盾" in issue for issue in payload["issues"]), payload

        write_handoff(handoff, status="未完成", reference="无",
                      narrative="没有证据时不得写文件痕迹已完成。\n图片分支已完成。")
        code, payload = run(script, main_report, handoff)
        assert code == 0, payload
        code, payload = run(script, main_report, handoff, require_complete=True)
        assert code == 1 and any("要求完整结案" in issue for issue in payload["issues"]), payload

        write_trace(trace_root, [first, second], status="部分完成")
        write_main(main_report, [*expected, outside_hash], expected, overall="部分完成", branch="部分完成")
        write_handoff(handoff, status="部分完成", hashes=expected)
        code, payload = run(script, main_report, handoff)
        assert code == 0, payload
        code, payload = run(script, main_report, handoff, require_complete=True)
        assert code == 1, payload

        valid()
        first.write_bytes(b"changed after review")
        code, payload = run(script, main_report)
        assert code == 1 and any("当前哈希" in issue for issue in payload["issues"]), payload
        print("通过：范围双向闭合、范围外输入、状态一致、结构化交接、多哈希、混合句/别名、规则误报排除、合法部分完成及输入变更。")


if __name__ == "__main__":
    main()
