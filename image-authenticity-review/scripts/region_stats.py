#!/usr/bin/env python3
"""在原始像素坐标内测量灰阶和颜色分布；依赖 Pillow，不判定拼接或 AI。"""

import argparse
import hashlib
import json
import math
from pathlib import Path

from PIL import Image, ImageChops


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--box", nargs=4, type=int, action="append",
                        metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    parser.add_argument("--dark-threshold", type=int, default=100)
    parser.add_argument("--mid-upper", type=int, default=205)
    parser.add_argument("--background-low", type=int, default=200)
    parser.add_argument("--background-high", type=int, default=255)
    parser.add_argument("--saturation-threshold", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.dark_threshold <= args.mid_upper <= 255:
        parser.error("需要 0 <= dark-threshold <= mid-upper <= 255")
    if not 0 <= args.background_low < args.background_high <= 255:
        parser.error("需要 0 <= background-low < background-high <= 255")
    if not 0 <= args.saturation_threshold <= 255:
        parser.error("saturation-threshold 必须为 0 到 255")
    with args.image.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    results = []
    with Image.open(args.image) as image:
        if getattr(image, "n_frames", 1) != 1:
            parser.error("仅支持单帧图片")
        if image.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "CMYK"):
            parser.error("仅支持可转换为 8 位灰阶的常见模式，避免静默截断高位深数据")
        rgb = image.convert("RGBA")
        if rgb.getchannel("A").getextrema() != (255, 255):
            parser.error("含透明像素，需要先明确显示背景后再测量")
        rgb = rgb.convert("RGB")
        boxes = args.box or [(0, 0, image.width, image.height)]
        for left, top, right, bottom in boxes:
            if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
                parser.error("box 必须为原图范围内非空区域，右边和下边不包含在区域内")
            crop = rgb.crop((left, top, right, bottom))
            histogram = crop.convert("L").histogram()
            count = crop.width * crop.height
            dark = sum(histogram[:args.dark_threshold])
            mid = sum(histogram[args.dark_threshold:args.mid_upper + 1])
            background = sum(histogram[args.background_low + 1:args.background_high])
            mean = sum(i * n for i, n in enumerate(histogram)) / count
            variance = sum((i - mean) ** 2 * n for i, n in enumerate(histogram)) / count
            red, green, blue = crop.split()
            maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
            minimum = ImageChops.darker(ImageChops.darker(red, green), blue)
            span = ImageChops.subtract(maximum, minimum).histogram()
            results.append({"box": [left, top, right, bottom], "pixels": count,
                            "gray_histogram": histogram,
                            "gray_unique_values": [i for i, n in enumerate(histogram) if n],
                            "gray_mean": mean, "gray_stddev": math.sqrt(variance),
                            "dark_pixels": dark, "mid_pixels": mid,
                            "mid_dark_ratio": mid / dark if dark else None,
                            "ratio_status": "measured" if dark else "no_dark_pixels",
                            "background_pixels": background,
                            "background_fraction": background / count,
                            "rgb_span_histogram": span,
                            "colored_pixels": sum(span[args.saturation_threshold + 1:])})
        size = list(image.size)
        orientation = image.getexif().get(274)
    print(json.dumps({"path": str(args.image), "sha256": digest, "size": size,
                      "coordinates": "stored_pixels_unrotated_right_bottom_exclusive",
                      "exif_orientation": orientation, "resampled": False,
                      "gray_conversion": "Pillow_RGB_to_L_without_ICC_transform",
                      "thresholds": {"dark_lt": args.dark_threshold,
                                     "mid_lte": args.mid_upper,
                                     "background_gt": args.background_low,
                                     "background_lt": args.background_high,
                                     "rgb_span_gt": args.saturation_threshold},
                      "regions": results, "authenticity_decision": "not_performed"},
                     ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
