#!/usr/bin/env python3
"""Fresh BidKing automation loop.

This script intentionally ignores the old auto-loop logic.  It follows the
user-provided flow exactly:
- Wait until central OCR sees a round number.
- Wait a fixed delay, use the leftmost tool, wait for animation.
- OCR central info, calculate a bid, input it, confirm.
- If OCR sees "对局结束", run the fixed post-round transition clicks.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyautogui
from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from bidking_maa_test.central_info_parser import merge_patch, parse_central_info  # noqa: E402
from bidking_maa_test.window_backend import capture_window_frame, find_window, scale_point  # noqa: E402
from manual_bidking_advisor import evaluate  # noqa: E402

try:
    import ctypes
    import ctypes.wintypes as wt

    USER32 = ctypes.windll.user32
except Exception:  # pragma: no cover - only used on Windows desktops.
    USER32 = None
    wt = None

_FAST_OCR = None
_STOP_EVENT = threading.Event()

HWND_TOP = 0
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
MONITOR_DEFAULTTONEAREST = 2
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class StopRequested(RuntimeError):
    pass


def request_stop() -> None:
    _STOP_EVENT.set()


def reset_stop() -> None:
    _STOP_EVENT.clear()


def stop_requested() -> bool:
    return _STOP_EVENT.is_set()


def ensure_not_stopped() -> None:
    if stop_requested():
        raise StopRequested()


def sleep_interruptible(seconds: float, step: float = 0.05) -> None:
    end = time.monotonic() + max(0.0, float(seconds))
    while True:
        ensure_not_stopped()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(float(step), remaining))


CHINESE_ROUND_NUMBERS = {
    "一": 1,
    "壹": 1,
    "二": 2,
    "两": 2,
    "贰": 2,
    "三": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "I": 1,
    "Ⅰ": 1,
    "l": 1,
    "丨": 1,
    "II": 2,
    "Ⅱ": 2,
    "III": 3,
    "Ⅲ": 3,
    "IV": 4,
    "Ⅳ": 4,
    "V": 5,
    "Ⅴ": 5,
}


@dataclass
class CaptureResult:
    text: str
    image_path: Path | None
    parsed: dict[str, Any]


@dataclass
class Observation:
    capture: CaptureResult
    end_text: str
    round_no: int | None
    end_prompt: bool
    reward_continue: bool
    auction_lobby: bool
    home_bid_button: bool
    has_any_signal: bool


class EndPromptDetected(RuntimeError):
    def __init__(self, source: str):
        super().__init__(source)
        self.source = source


def now_text() -> str:
    return time.strftime("%H:%M:%S")


def log(message: str) -> None:
    print(f"[{now_text()}] {message}", flush=True)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_path(config_path: Path, raw_path: str | None, default_name: str) -> Path:
    if not raw_path:
        return config_path.parent / default_name
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return config_path.parent / path


def default_advisor_input() -> dict[str, Any]:
    return {
        "round": 1,
        "my_role": "ahmad",
        "maria_start_price": None,
        "total_all": None,
        "avg_grid_all": None,
        "count_green": None,
        "count_white": None,
        "min_count_green": 0,
        "min_count_white": 0,
        "max_count": 60,
        "max_show": 20,
        "avg_tolerance": 0.05,
        "grid_price_green": 0.0,
        "grid_price_white": 0.0,
        "grid_price_blue": 0.0,
        "grid_price_purple": 0.28,
        "grid_price_gold": 1.13,
        "grid_price_red": 4.77,
        "total_grid_rounding": "round",
        "constraints": {
            "blue": {"avg": None, "count": None, "grid": None, "min_count": None},
            "purple": {"avg": None, "count": None, "grid": None, "min_count": None},
            "gold": {"avg": None, "count": None, "grid": None, "min_count": None},
            "red": {"avg": None, "count": None, "grid": None, "min_count": None},
        },
        "category_weights": {f"cat{index}": 1 for index in range(1, 11)},
        "rank_signal": {
            "my_rank": 2,
            "players": 4,
            "pressure": 0.55,
            "suspected_bluff": 0.35,
        },
        "style": {
            "risk_bias": "balanced",
            "need_comeback": False,
        },
    }


def apply_price_config(data: dict[str, Any], price_config: dict[str, Any]) -> dict[str, Any]:
    grid_prices = price_config.get("grid_prices", {})
    for color in ("green", "white", "blue", "purple", "gold", "red"):
        if color in grid_prices:
            data[f"grid_price_{color}"] = float(grid_prices[color])
    if "avg_tolerance" in price_config:
        data["avg_tolerance"] = float(price_config["avg_tolerance"])
    if "category_weights" in price_config:
        data["category_weights"] = dict(price_config["category_weights"])
    if "burst_limit" in price_config:
        data["burst_limit"] = float(price_config["burst_limit"])
    if "round_rules" in price_config:
        data["round_rules"] = dict(price_config["round_rules"])
    return data


def build_advisor_input(config: dict[str, Any], text: str, round_no: int, price_config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    advisor = config.get("advisor", {})
    parsed = parse_central_info(text)
    data = default_advisor_input()
    data = apply_price_config(data, price_config)
    data["round"] = int(round_no)
    data["my_role"] = advisor.get("role", "ahmad")
    data["avg_grid_all"] = advisor.get("avg_grid_all")
    data["total_grid_rounding"] = advisor.get("total_grid_rounding", "round")
    green_count = advisor.get("green_count")
    white_count = advisor.get("white_count")
    data["count_green"] = None if green_count in (None, "") else int(green_count)
    data["count_white"] = None if white_count in (None, "") else int(white_count)
    merged = merge_patch(data, parsed)
    merged["round"] = int(round_no)
    return merged, parsed


def merge_parsed_memory(current: dict[str, Any] | None, new_patch: dict[str, Any]) -> dict[str, Any]:
    if not current:
        return json.loads(json.dumps(new_patch, ensure_ascii=False))

    merged = json.loads(json.dumps(current, ensure_ascii=False))
    current_round = current.get("round")
    new_round = new_patch.get("round")
    same_round = (
        current_round is not None and new_round is not None and int(current_round) == int(new_round)
    )
    sticky_scalar_fields = {
        "total_all",
        "victor_total_all",
        "total_grid_all",
        "wg_total",
        "count_green",
        "count_white",
        "avg_grid_all",
        "maria_start_price",
    }
    sticky_constraint_fields = {"count", "grid", "avg"}
    if not same_round:
        for key in list(merged.keys()):
            if key in ("constraints", "parsed_facts", "unparsed_lines", "round"):
                continue
            if key.startswith("avg_price_") or key.startswith("total_price_"):
                merged.pop(key, None)
                continue
            if key in {
                "observed_low_price",
                "mixed_type_count",
                "mixed_type_avg_grid_price",
            }:
                merged.pop(key, None)
    for key, value in new_patch.items():
        if key in ("parsed_facts", "unparsed_lines"):
            continue
        if key == "constraints":
            merged.setdefault("constraints", {})
            for color, fields in value.items():
                merged["constraints"].setdefault(color, {})
                for field, field_value in fields.items():
                    if field_value is not None and (same_round or field in sticky_constraint_fields):
                        merged["constraints"][color][field] = field_value
        else:
            if value is not None and (same_round or key in sticky_scalar_fields):
                merged[key] = value

    merged_facts = list(current.get("parsed_facts") or [])
    merged_facts.extend(new_patch.get("parsed_facts") or [])
    merged["parsed_facts"] = merged_facts

    merged_unparsed = list(current.get("unparsed_lines") or [])
    merged_unparsed.extend(new_patch.get("unparsed_lines") or [])
    merged["unparsed_lines"] = merged_unparsed
    return merged


def sanitize_parsed_patch_for_memory(parsed_patch: dict[str, Any], round_no: int | None) -> dict[str, Any]:
    patch = json.loads(json.dumps(parsed_patch or {}, ensure_ascii=False))
    if patch.get("round") is not None and round_no is not None and int(patch.get("round")) != int(round_no):
        return {"parsed_facts": [], "unparsed_lines": []}

    current_round = int(round_no) if round_no is not None else None
    if current_round is not None:
        patch["round"] = current_round
    return patch


def build_advisor_input_from_patch(config: dict[str, Any], parsed_patch: dict[str, Any], round_no: int, price_config: dict[str, Any]) -> dict[str, Any]:
    advisor = config.get("advisor", {})
    data = default_advisor_input()
    data = apply_price_config(data, price_config)
    data["round"] = int(round_no)
    data["my_role"] = advisor.get("role", "ahmad")
    data["avg_grid_all"] = advisor.get("avg_grid_all")
    data["total_grid_rounding"] = advisor.get("total_grid_rounding", "round")
    green_count = advisor.get("green_count")
    white_count = advisor.get("white_count")
    data["count_green"] = None if green_count in (None, "") else int(green_count)
    data["count_white"] = None if white_count in (None, "") else int(white_count)
    merged = merge_patch(data, parsed_patch)
    merged["round"] = int(round_no)
    return merged


def normalize_text(text: str) -> str:
    table = str.maketrans(
        {
            "０": "0",
            "１": "1",
            "２": "2",
            "３": "3",
            "４": "4",
            "５": "5",
            "６": "6",
            "７": "7",
            "８": "8",
            "９": "9",
            "Ⅰ": "I",
            "Ⅱ": "II",
            "Ⅲ": "III",
            "Ⅳ": "IV",
            "Ⅴ": "V",
        }
    )
    return (text or "").translate(table)


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", normalize_text(text))


def round_token_to_int(token: str) -> int | None:
    token = normalize_text(token).strip()
    if token.isdigit():
        value = int(token)
        return value if 1 <= value <= 5 else None
    value = CHINESE_ROUND_NUMBERS.get(token)
    if value is not None and 1 <= value <= 5:
        return value
    return None


def parse_round_number(text: str) -> int | None:
    raw = normalize_text(text)
    patterns = [
        r"第\s*([1-5一二两三四五壹贰叁肆伍IⅤVⅡⅢⅣ]+)\s*(?:轮|回合)",
        r"(?:当前|现在)?(?:轮次|回合)\s*[:：]?\s*第?\s*([1-5一二两三四五壹贰叁肆伍IⅤVⅡⅢⅣ]+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, raw, flags=re.IGNORECASE):
            value = round_token_to_int(match.group(1).upper())
            if value is not None:
                return value

    tight = compact_text(raw)
    for pattern in (
        r"第([1-5一二两三四五壹贰叁肆伍IⅤVⅡⅢⅣ]+)(?:轮|回合)",
        r"(?:轮次|回合)[:：]?第?([1-5一二两三四五壹贰叁肆伍IⅤVⅡⅢⅣ]+)",
    ):
        match = re.search(pattern, tight, flags=re.IGNORECASE)
        if match:
            value = round_token_to_int(match.group(1).upper())
            if value is not None:
                return value
    return None


def has_end_prompt(text: str) -> bool:
    tight = compact_text(text)
    if "对局结束" in tight:
        return True
    return "对局" in tight and "结束" in tight


def has_auction_lobby(text: str) -> bool:
    tight = compact_text(text)
    if "竞拍大厅" in tight:
        return True
    return "竞拍" in tight and "大厅" in tight


def has_home_bid_button(text: str) -> bool:
    tight = compact_text(text)
    return "竞拍" in tight


def has_reward_continue(text: str) -> bool:
    tight = compact_text(text)
    return "EXP" in tight.upper() and "\u7ee7\u7eed" in tight


def ensure_output_dir(config: dict[str, Any], config_path: Path) -> Path:
    debug = config.get("debug", {})
    raw = debug.get("runs_dir", "runs")
    path = Path(raw)
    if not path.is_absolute():
        path = config_path.parent / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def rapidocr_once(image: Image.Image) -> str:
    global _FAST_OCR
    if _FAST_OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _FAST_OCR = RapidOCR()
    result, _ = _FAST_OCR(image)
    if not result:
        return ""
    rows = sorted(result, key=lambda item: (min(point[1] for point in item[0]), min(point[0] for point in item[0])))
    return "\n".join(str(item[1]) for item in rows)


def scaled_region_box(region: dict[str, Any], config: dict[str, Any], image_width: int, image_height: int) -> tuple[int, int, int, int]:
    reference = config.get("window", {}).get("reference_client_size", {})
    ref_width = max(1, int(reference.get("width") or image_width))
    ref_height = max(1, int(reference.get("height") or image_height))
    left = round(float(region["left"]) * image_width / ref_width)
    top = round(float(region["top"]) * image_height / ref_height)
    width = round(float(region["width"]) * image_width / ref_width)
    height = round(float(region["height"]) * image_height / ref_height)
    right = min(image_width, max(0, left + width))
    bottom = min(image_height, max(0, top + height))
    left = min(max(0, left), right)
    top = min(max(0, top), bottom)
    return int(left), int(top), int(right), int(bottom)


def has_purple_outline_signal(text: str) -> bool:
    tight = compact_text(text)
    if "紫" not in tight:
        return False
    if not any(token in tight for token in ("轮廓", "轮廊", "廓")):
        return False
    return "显示" in tight or "所有" in tight or "藏品" in tight


def is_purple_outline_pixel(pixel: tuple[int, int, int]) -> bool:
    r, g, b = pixel[:3]
    return b >= 120 and r >= 85 and g <= 145 and (b - g) >= 45 and (r - g) >= 20


def connected_component_boxes(mask: bytearray, width: int, height: int) -> list[tuple[int, int, int, int, int]]:
    boxes: list[tuple[int, int, int, int, int]] = []
    for start in range(width * height):
        if not mask[start]:
            continue
        stack = [start]
        mask[start] = 0
        min_x = max_x = start % width
        min_y = max_y = start // width
        area = 0
        while stack:
            index = stack.pop()
            area += 1
            x = index % width
            y = index // width
            if x < min_x:
                min_x = x
            elif x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            elif y > max_y:
                max_y = y
            for neighbor in (index - 1, index + 1, index - width, index + width):
                if neighbor < 0 or neighbor >= width * height or not mask[neighbor]:
                    continue
                if neighbor == index - 1 and x == 0:
                    continue
                if neighbor == index + 1 and x == width - 1:
                    continue
                mask[neighbor] = 0
                stack.append(neighbor)
        boxes.append((min_x, min_y, max_x + 1, max_y + 1, area))
    return boxes


def detect_purple_outline_count(config: dict[str, Any], frame: Image.Image) -> tuple[int | None, list[tuple[int, int, int, int]]]:
    visual = config.get("visual_detection", {})
    if not bool(visual.get("purple_outline_count_enabled", True)):
        return None, []
    region = visual.get("loot_panel_region")
    if not region:
        return None, []
    left, top, right, bottom = scaled_region_box(region, config, frame.width, frame.height)
    if right <= left or bottom <= top:
        return None, []
    crop = frame.crop((left, top, right, bottom)).convert("RGB")
    width, height = crop.size
    pixels = crop.load()
    mask = bytearray(width * height)
    for y in range(height):
        row = y * width
        for x in range(width):
            if is_purple_outline_pixel(pixels[x, y]):
                mask[row + x] = 1
    min_width = max(1, parse_int_config(visual.get("purple_outline_min_box_width"), 18))
    min_height = max(1, parse_int_config(visual.get("purple_outline_min_box_height"), 18))
    min_pixels = max(1, parse_int_config(visual.get("purple_outline_min_pixels"), 24))
    boxes: list[tuple[int, int, int, int]] = []
    for x1, y1, x2, y2, area in connected_component_boxes(mask, width, height):
        box_width = x2 - x1
        box_height = y2 - y1
        if area < min_pixels or box_width < min_width or box_height < min_height:
            continue
        boxes.append((left + x1, top + y1, left + x2, top + y2))
    return len(boxes), boxes


def add_visual_count_fact(
    parsed: dict[str, Any],
    *,
    color: str,
    count: int,
    source: str,
    boxes: list[tuple[int, int, int, int]] | None = None,
) -> dict[str, Any]:
    parsed.setdefault("constraints", {})
    parsed["constraints"].setdefault(color, {"avg": None, "count": None, "grid": None, "min_count": None})
    constraint = parsed["constraints"][color]
    if int(count) > 0:
        for field in ("avg", "grid"):
            if constraint.get(field) == 0:
                constraint[field] = None
    constraint["count"] = int(count)
    parsed.setdefault("parsed_facts", []).append(
        {
            "field": f"constraints.{color}.count",
            "value": int(count),
            "line": source,
        }
    )
    parsed.setdefault("visual_detections", {})[f"{color}_outline_count"] = {
        "count": int(count),
        "boxes": [list(box) for box in boxes or []],
    }
    return parsed


def observe_state_fast(config: dict[str, Any], config_path: Path, label: str) -> Observation:
    if bool(config.get("safety", {}).get("bring_window_to_front_on_observe", False)):
        bring_window_to_front(config)
    frame, _info = capture_window_frame(config)
    runs_dir = ensure_output_dir(config, config_path)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    image_path: Path | None = None
    if bool(config.get("debug", {}).get("save_crops", True)):
        image_path = runs_dir / f"{timestamp}_{label}_full_window.png"
        frame.save(image_path)
    full_window_text = rapidocr_once(ImageOps.grayscale(frame).convert("RGB"))
    if bool(config.get("debug", {}).get("save_ocr_text", True)):
        (runs_dir / f"{timestamp}_{label}_full_window.txt").write_text(full_window_text, encoding="utf-8")

    central_region = config.get("capture", {}).get("central_info_region")
    if central_region:
        central_box = scaled_region_box(central_region, config, frame.width, frame.height)
        central_crop = frame.crop(central_box)
        central_text = rapidocr_once(ImageOps.grayscale(central_crop).convert("RGB"))
        if bool(config.get("debug", {}).get("save_crops", True)):
            central_path = runs_dir / f"{timestamp}_{label}_central_info.png"
            central_crop.save(central_path)
        if bool(config.get("debug", {}).get("save_ocr_text", True)):
            (runs_dir / f"{timestamp}_{label}_central_info.txt").write_text(central_text, encoding="utf-8")
    else:
        central_text = full_window_text

    home_bid_text = ""
    home_region = config.get("capture", {}).get("home_bid_button_region")
    if home_region:
        box = scaled_region_box(home_region, config, frame.width, frame.height)
        home_crop = frame.crop(box)
        home_bid_text = rapidocr_once(ImageOps.grayscale(home_crop).convert("RGB"))

    round_no = parse_round_number(central_text) or parse_round_number(full_window_text)
    parsed = parse_central_info(central_text)
    visual = config.get("visual_detection", {})
    purple_min_round = max(1, parse_int_config(visual.get("purple_outline_min_round"), 3))
    if round_no is not None and int(round_no) >= purple_min_round and has_purple_outline_signal(central_text + "\n" + full_window_text):
        purple_count, purple_boxes = detect_purple_outline_count(config, frame)
        if purple_count is not None and purple_count > 0:
            parsed = add_visual_count_fact(
                parsed,
                color="purple",
                count=purple_count,
                source=f"visual purple outline count={purple_count}",
                boxes=purple_boxes,
            )
    capture = CaptureResult(text=central_text, image_path=image_path, parsed=parsed)
    parsed_facts = capture.parsed.get("parsed_facts") or []
    any_signal = bool(
        parsed_facts
        or round_no is not None
        or has_end_prompt(full_window_text)
        or has_reward_continue(full_window_text)
        or has_auction_lobby(full_window_text)
        or has_home_bid_button(home_bid_text)
    )
    return Observation(
        capture=capture,
        end_text=full_window_text,
        round_no=round_no,
        end_prompt=has_end_prompt(full_window_text),
        reward_continue=has_reward_continue(full_window_text),
        auction_lobby=has_auction_lobby(full_window_text),
        home_bid_button=has_home_bid_button(home_bid_text),
        has_any_signal=any_signal,
    )


def observe_state(config: dict[str, Any], config_path: Path, label: str) -> Observation:
    return observe_state_fast(config, config_path, label)


def apply_observation_memory(observation: Observation, knowledge_patch: dict[str, Any] | None) -> dict[str, Any] | None:
    parsed = sanitize_parsed_patch_for_memory(observation.capture.parsed or {}, observation.round_no)
    facts = parsed.get("parsed_facts") or []
    if not facts:
        return knowledge_patch
    return merge_parsed_memory(knowledge_patch, parsed)


def save_round_debug_bundle(
    config: dict[str, Any],
    config_path: Path,
    *,
    round_no: int,
    raw_text: str,
    knowledge_patch: dict[str, Any] | None,
    advisor_input: dict[str, Any],
    details: dict[str, Any],
    final_price: int,
) -> None:
    debug = config.get("debug", {})
    if not bool(debug.get("save_round_debug", True)):
        return
    runs_dir = ensure_output_dir(config, config_path)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = runs_dir / f"{stamp}_round{round_no}"
    (prefix.with_suffix(".ocr.txt")).write_text(raw_text or "", encoding="utf-8")
    (prefix.with_suffix(".knowledge.json")).write_text(
        json.dumps(knowledge_patch or {}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (prefix.with_suffix(".advisor_input.json")).write_text(
        json.dumps(advisor_input, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    payload = {
        "final_price": final_price,
        "details": details,
    }
    (prefix.with_suffix(".result.json")).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def persist_last_submitted_price(
    config_path: Path,
    price: int | None,
    runtime_config: dict[str, Any] | None = None,
    round_no: int | None = None,
) -> None:
    normalized_price = None if price is None else int(price)
    normalized_round = None if normalized_price is None or round_no is None else int(round_no)
    if runtime_config is not None:
        runtime_config.setdefault("pricing", {})
        runtime_config["pricing"]["last_submitted_price"] = normalized_price
        if normalized_price is None:
            runtime_config["pricing"]["last_submitted_round"] = None
        elif normalized_round is not None:
            runtime_config["pricing"]["last_submitted_round"] = normalized_round
        if normalized_price is None:
            runtime_config["pricing"]["sticky_increment_step"] = None
        else:
            existing_step = runtime_config["pricing"].get("sticky_increment_step")
            if existing_step in (None, "", 0, "0"):
                increment_ratio = max(
                    0.0,
                    parse_float_config(runtime_config.get("automation", {}).get("sticky_increment_ratio"), 0.0),
                )
                if increment_ratio > 0:
                    rounding = str(runtime_config["pricing"].get("rounding", "floor_int"))
                    step = choose_rounding(float(normalized_price) * increment_ratio, rounding)
                    runtime_config["pricing"]["sticky_increment_step"] = max(1, int(step))
    try:
        config = load_json(config_path)
    except Exception:
        return
    config.setdefault("pricing", {})
    config["pricing"]["last_submitted_price"] = normalized_price
    if normalized_price is None:
        config["pricing"]["last_submitted_round"] = None
    elif normalized_round is not None:
        config["pricing"]["last_submitted_round"] = normalized_round
    if normalized_price is None:
        config["pricing"]["sticky_increment_step"] = None
    else:
        existing_step = config["pricing"].get("sticky_increment_step")
        if existing_step in (None, "", 0, "0"):
            increment_ratio = max(
                0.0,
                parse_float_config(config.get("automation", {}).get("sticky_increment_ratio"), 0.0),
            )
            if increment_ratio > 0:
                rounding = str(config["pricing"].get("rounding", "floor_int"))
                step = choose_rounding(float(normalized_price) * increment_ratio, rounding)
                config["pricing"]["sticky_increment_step"] = max(1, int(step))
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def virtual_screen_rect() -> tuple[int, int, int, int]:
    if USER32 is None:
        return 0, 0, 1920, 1080
    left = int(USER32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    top = int(USER32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    width = int(USER32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    height = int(USER32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    return left, top, left + max(1, width), top + max(1, height)


def get_window_outer_rect(hwnd: int) -> tuple[int, int, int, int]:
    if USER32 is None or wt is None:
        return 0, 0, 1920, 1080
    rect = wt.RECT()
    if not USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0, 0, 1920, 1080
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def screen_center_position(width: int, height: int) -> tuple[int, int]:
    left, top, right, bottom = virtual_screen_rect()
    screen_width = max(1, right - left)
    screen_height = max(1, bottom - top)
    x = left + max(0, (screen_width - width) // 2)
    y = top + max(0, (screen_height - height) // 2)
    return int(x), int(y)


def prepare_target_window(config: dict[str, Any], *, center: bool) -> None:
    ensure_not_stopped()
    if USER32 is None:
        return
    window_options = config.get("window", {})
    if not bool(config.get("safety", {}).get("bring_window_to_front", True)):
        return
    try:
        info = find_window(window_options)
        hwnd = int(info.hwnd)
        USER32.ShowWindow(hwnd, SW_RESTORE)
        sleep_interruptible(0.05)

        left, top, right, bottom = get_window_outer_rect(hwnd)
        width = max(1, right - left)
        height = max(1, bottom - top)
        if center and bool(window_options.get("center_on_start", True)):
            x, y = screen_center_position(width, height)
            USER32.SetWindowPos(hwnd, HWND_TOP, int(x), int(y), width, height, SWP_SHOWWINDOW)
            sleep_interruptible(0.08)
            log(f"window centered: hwnd={hwnd} pos={x},{y} size={width}x{height}")

        if bool(window_options.get("force_topmost_bump", True)):
            USER32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            sleep_interruptible(0.03)
            USER32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            sleep_interruptible(0.03)

        USER32.SetForegroundWindow(hwnd)
        sleep_interruptible(float(config.get("timing", {}).get("click_pause_seconds", 0.12)))
    except Exception as exc:
        log(f"warn: failed to prepare target window: {exc}")


def bring_window_to_front(config: dict[str, Any]) -> None:
    prepare_target_window(config, center=False)


def client_to_screen(config: dict[str, Any], point: dict[str, Any]) -> tuple[int, int]:
    info = find_window(config.get("window", {}))
    x, y = scale_click_point_to_client(config, point, info.width, info.height)
    origin_x, origin_y = info.client_origin
    return origin_x + x, origin_y + y


def scale_click_point_to_client(config: dict[str, Any], point: dict[str, Any], actual_width: int, actual_height: int) -> tuple[int, int]:
    reference = config.get("window", {}).get("reference_client_size", {})
    ref_height = int(reference.get("height") or actual_height or 1080)
    raw_point = dict(point)
    if str(raw_point.get("origin", "left_top")).strip().lower() in {"left_bottom", "bottom_left"}:
        raw_point["y"] = ref_height - int(raw_point["y"])
    return scale_point(raw_point, reference, actual_width, actual_height)


def bid_confirm_target_looks_like_abandon(config: dict[str, Any]) -> bool:
    safety = config.get("safety", {})
    if not bool(safety.get("bid_confirm_abandon_guard", True)):
        return False
    point = config.get("clicks", {}).get("bid_confirm")
    if not point:
        return False
    try:
        frame, _info = capture_window_frame(config)
    except Exception as exc:
        log(f"warn: abandon guard capture failed: {exc}")
        return False
    x, y = scale_click_point_to_client(config, point, frame.width, frame.height)
    radius = max(8, parse_int_config(safety.get("abandon_guard_radius_pixels"), 36))
    left = max(0, int(x) - radius)
    top = max(0, int(y) - radius)
    right = min(frame.width, int(x) + radius + 1)
    bottom = min(frame.height, int(y) + radius + 1)
    if right <= left or bottom <= top:
        return False
    crop = frame.crop((left, top, right, bottom)).convert("RGB")
    pixels = crop.getdata()
    total = 0
    danger = 0
    for r, g, b in pixels:
        total += 1
        if r >= 125 and g <= 95 and b >= 75 and (r - g) >= 45 and (b - g) >= 20:
            danger += 1
    ratio = float(danger) / float(total or 1)
    threshold = max(0.0, parse_float_config(safety.get("abandon_guard_red_ratio"), 0.08))
    if ratio >= threshold:
        log(f"bid confirm blocked: target looks like abandon button red_ratio={ratio:.3f} threshold={threshold:.3f}")
        return True
    return False


def jitter_click_point(config: dict[str, Any], x: int, y: int, point: dict[str, Any] | None = None) -> tuple[int, int, int, int]:
    safety = config.get("safety", {})
    raw_radius = (point or {}).get("jitter_pixels", safety.get("click_jitter_pixels", 0))
    try:
        radius = max(0, int(float(raw_radius or 0)))
    except Exception:
        radius = 0
    if radius <= 0:
        return int(x), int(y), 0, 0

    dx = random.randint(-radius, radius)
    dy = random.randint(-radius, radius)
    left, top, right, bottom = virtual_screen_rect()
    jittered_x = min(max(left, int(x) + dx), right - 1)
    jittered_y = min(max(top, int(y) + dy), bottom - 1)
    return int(jittered_x), int(jittered_y), int(jittered_x) - int(x), int(jittered_y) - int(y)


def click_point(config: dict[str, Any], name: str, repeat: int = 1, pause: float | None = None) -> None:
    bring_window_to_front(config)
    point = config["clicks"][name]
    timing = config.get("timing", {})
    pause_value = float(timing.get("click_pause_seconds", 0.12) if pause is None else pause)
    dry_run = bool(config.get("safety", {}).get("dry_run", False))
    base_x, base_y = client_to_screen(config, point)
    for index in range(repeat):
        ensure_not_stopped()
        x, y, dx, dy = jitter_click_point(config, base_x, base_y, point)
        if dx or dy:
            log(f"click {name} #{index + 1}: screen={x},{y} jitter={dx},{dy}")
        else:
            log(f"click {name} #{index + 1}: screen={x},{y}")
        if not dry_run:
            pyautogui.click(x, y)
        sleep_interruptible(pause_value)


def press_escape(config: dict[str, Any]) -> None:
    ensure_not_stopped()
    bring_window_to_front(config)
    dry_run = bool(config.get("safety", {}).get("dry_run", False))
    log("press key: esc")
    if not dry_run:
        pyautogui.press("esc")
    sleep_interruptible(float(config.get("timing", {}).get("click_pause_seconds", 0.12)))


def type_price(config: dict[str, Any], price: int) -> None:
    ensure_not_stopped()
    bring_window_to_front(config)
    timing = config.get("timing", {})
    pause = float(timing.get("click_pause_seconds", 0.12))
    dry_run = bool(config.get("safety", {}).get("dry_run", False))
    log(f"type price: {price}")
    if dry_run:
        return
    pyautogui.hotkey("ctrl", "a")
    sleep_interruptible(pause)
    ensure_not_stopped()
    pyautogui.write(str(price), interval=0.02)
    sleep_interruptible(pause)


def run_tool_sequence(config: dict[str, Any]) -> None:
    log("tool sequence: open/select/confirm")
    click_point(config, "tool_button")
    click_point(config, "leftmost_tool")
    click_point(config, "tool_confirm")


def input_bid(config: dict[str, Any], price: int) -> bool:
    log("bid sequence: open/input/confirm")
    safety = config.get("safety", {})
    retry_count = max(1, parse_int_config(safety.get("bid_open_retry_count"), 2))
    open_wait = max(0.0, parse_float_config(safety.get("bid_open_wait_seconds"), 0.35))
    for attempt in range(1, retry_count + 1):
        if retry_count > 1:
            log(f"bid attempt {attempt}/{retry_count}")
        click_point(config, "bid_button")
        sleep_interruptible(open_wait)
        click_point(config, "bid_input_box")
        type_price(config, price)
        if bool(safety.get("confirm_after_type", True)):
            if bid_confirm_target_looks_like_abandon(config):
                if attempt < retry_count:
                    log("bid confirm skipped for this attempt; retry open/input")
                    continue
                log("bid submit skipped: confirm target still looks like abandon button")
                return False
            click_point(config, "bid_confirm")
            click_point(config, "tool_confirm")
        sleep_interruptible(float(config.get("timing", {}).get("after_bid_confirm_wait_seconds", 1.0)))
        return True
    return False


def run_post_round_transition(config: dict[str, Any]) -> float:
    log("post-round transition: fixed click chain")
    click_point(config, "end_reward_click", repeat=2)
    sleep_interruptible(1.0)
    click_point(config, "end_close_click", repeat=2)
    sleep_interruptible(1.0)
    click_point(config, "continue_button", repeat=3)
    log("post-round transition complete; waiting for auction lobby OCR")


def run_auction_lobby_transition(config: dict[str, Any]) -> None:
    log("auction lobby detected: enter selected room")
    sleep_interruptible(1.0)
    click_point(config, "post_continue_action")
    sleep_interruptible(2.0)
    click_point(config, "post_continue_confirm")
    confirm_at = time.monotonic()
    log("auction lobby transition complete; waiting for round OCR")
    return confirm_at


def run_home_bid_button_transition(config: dict[str, Any]) -> None:
    log("home bid button detected: click auction entry")
    click_point(config, "home_bid_button")
    log("home bid button transition complete; waiting for next OCR")


def run_reward_continue_transition(config: dict[str, Any]) -> None:
    log("reward continue detected: click continue")
    click_point(config, "reward_continue_button")
    log("reward continue click complete; waiting for next OCR")


def current_map_point(config: dict[str, Any], selected_map: str) -> dict[str, Any] | None:
    maps = config.get("automation", {}).get("maps", {})
    item = maps.get(str(selected_map), {})
    point = item.get("point")
    return point if isinstance(point, dict) else None


def run_map_selection_transition(config: dict[str, Any], selected_map: str) -> float | None:
    maps = config.get("automation", {}).get("maps", {})
    automation = config.get("automation", {})
    item = maps.get(str(selected_map), {})
    name = str(item.get("name") or selected_map)
    point = current_map_point(config, selected_map)
    if not point:
        log(f"map selection skipped: no point configured for {selected_map}.{name}")
        return None
    log(f"auction lobby detected: select map {selected_map}.{name}")
    bring_window_to_front(config)
    sleep_interruptible(1.0)
    for click_name in item.get("pre_clicks") or []:
        log(f"map pre-click: {click_name}")
        click_point(config, str(click_name))
        sleep_interruptible(1.0)
    sx, sy = client_to_screen(config, point)
    log(f"click map point: screen={sx},{sy}")
    if not bool(config.get("safety", {}).get("dry_run", False)):
        pyautogui.click(sx, sy)
    sleep_interruptible(float(config.get("timing", {}).get("click_pause_seconds", 0.12)))
    confirm_at = time.monotonic()
    if bool(automation.get("confirm_after_map_select", True)):
        sleep_interruptible(2.0)
        click_point(config, "post_continue_confirm")
        confirm_at = time.monotonic()
    log("map selection transition complete; waiting for round OCR")
    return confirm_at


def choose_rounding(value: float, rounding: str) -> int:
    if rounding == "ceil_int":
        return int(math.ceil(value))
    if rounding == "round_int":
        return int(round(value))
    return int(math.floor(value))


def parse_float_config(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def parse_int_config(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def current_time_random_extra(max_seconds: Any) -> float:
    max_extra = max(0.0, parse_float_config(max_seconds, 0.0))
    if max_extra <= 0:
        return 0.0
    return random.Random(time.time_ns()).random() * max_extra


def apply_observed_low_price_floor(result: dict[str, Any], price: int, rounding: str) -> tuple[int, str | None]:
    summary = (result or {}).get("summary") or {}
    observed_low_price = summary.get("observed_low_price")
    if observed_low_price is None:
        return int(price), None
    try:
        observed_low_price = float(observed_low_price)
    except Exception:
        return int(price), None
    if observed_low_price <= 0:
        return int(price), None
    if observed_low_price > float(price):
        raised = choose_rounding(observed_low_price * 1.25, rounding)
        return int(max(price, raised)), f"observed_low_price={observed_low_price:.0f} -> raised={raised}"
    return int(price), None


def choose_bid_value_by_mode(config: dict[str, Any], result: dict[str, Any]) -> tuple[float | None, str]:
    selected_risk = str(config.get("automation", {}).get("selected_risk", "均衡")).strip()
    summary = (result or {}).get("summary") or {}
    custom_factor = parse_float_config(config.get("automation", {}).get("custom_risk_factor"), 0.0)
    if selected_risk in ("保守", "conservative", "floor_price"):
        return summary.get("floor_price"), "保守=floor_price"
    if selected_risk in ("激进", "aggressive", "avg_price_plus_25"):
        avg_price = summary.get("avg_price")
        return (float(avg_price) * 1.25 if avg_price is not None else None), "激进=avg_price*1.25"
    if selected_risk in ("自定义", "custom", "custom_factor"):
        avg_price = summary.get("avg_price")
        return (float(avg_price) * (1.0 + custom_factor) if avg_price is not None else None), f"自定义=avg_price*(1+{custom_factor:.4f})"
    return summary.get("avg_price"), "均衡=avg_price"


def normalized_role(config: dict[str, Any]) -> str:
    return str(config.get("advisor", {}).get("role", "")).strip().lower()


def is_ahmad_role(config: dict[str, Any]) -> bool:
    return normalized_role(config) in {"ahmad", "ahmed", "role2", "鑹惧搱杩堝痉"}


def is_maria_role(config: dict[str, Any]) -> bool:
    role = str(config.get("advisor", {}).get("role", "")).strip().lower()
    return role in {"maria", "mary", "mariya", "maliya", "玛丽亚"}


def maria_round_bid_scheme_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("automation", {}).get("maria_round_bid_scheme_enabled", False))


def maria_round_bid_value(base_price: float, round_no: int) -> tuple[float, str]:
    round_index = max(1, min(5, int(round_no)))
    multipliers = {
        1: 0.85,
        2: 0.85,
        3: 0.85 * 1.33,
        4: 0.85 * 1.33 * 1.1 * 1.1,
        5: 0.85 * 1.33 * 1.1 * 1.1 * 1.03,
    }
    labels = {
        1: "第1回合=基础价*0.85",
        2: "第2回合=基础价*0.85",
        3: "第3回合=基础价*0.85*1.33",
        4: "第4回合=基础价*0.85*1.33*1.1*1.1",
        5: "第5回合=基础价*0.85*1.33*1.1*1.1*1.03",
    }
    return float(base_price) * multipliers[round_index], labels[round_index]


def choose_maria_bid_value(parsed_patch: dict[str, Any]) -> tuple[float | None, str]:
    raw_value = (parsed_patch or {}).get("maria_start_price")
    try:
        value = float(raw_value) if raw_value not in (None, "") else None
    except Exception:
        value = None
    if value is None or value <= 0:
        return None, "玛丽亚缺少白绿蓝总价值"
    return value, f"玛丽亚=白绿蓝总价值({value:.0f})"


def choose_express_bid_value(config: dict[str, Any], parsed_patch: dict[str, Any]) -> tuple[float | None, str]:
    automation = config.get("automation", {})
    total_all = parsed_patch.get("total_all")
    if total_all is None:
        total_all = parsed_patch.get("victor_total_all")
    try:
        total_all = int(total_all) if total_all is not None else None
    except Exception:
        total_all = None
    if total_all is None or total_all <= 0:
        return None, "快递跑刀缺少 total_all"
    express_factor = parse_float_config(automation.get("express_total_multiplier"), 0.0)
    final_price = choose_rounding(float(total_all) * float(express_factor), "floor_int")
    return float(final_price), f"快递跑刀=total_all({total_all})*单件价({express_factor:.4f})"


def has_ahmad_color_grid_signal(advisor_input: dict[str, Any], color: str) -> bool:
    color_info = ((advisor_input or {}).get("constraints") or {}).get(color) or {}
    return color_info.get("avg") not in (None, "") or color_info.get("grid") not in (None, "")


def direct_color_grid_from_advisor_input(advisor_input: dict[str, Any], color: str) -> float | None:
    color_info = ((advisor_input or {}).get("constraints") or {}).get(color) or {}
    try:
        grid = float(color_info.get("grid"))
    except Exception:
        return None
    return grid if grid > 0 else None


def min_color_grid_from_result(result: dict[str, Any], color: str) -> float | None:
    candidates = color_grid_candidates_from_result(result, color)
    return min(candidates) if candidates else None


def color_grid_candidates_from_result(result: dict[str, Any], color: str) -> list[float]:
    summary = (result or {}).get("summary") or {}
    summary_candidates = summary.get(f"{color}_grid_candidates")
    if isinstance(summary_candidates, list):
        candidates: list[float] = []
        for grid in summary_candidates:
            try:
                grid_value = float(grid)
            except Exception:
                continue
            if grid_value > 0:
                candidates.append(grid_value)
        if candidates:
            return sorted(set(candidates))

    candidates: list[float] = []
    solved_color = ((result or {}).get("solved") or {}).get(color) or {}
    pair_map = solved_color.get("pair_map") or {}
    for grids in pair_map.values():
        if not isinstance(grids, list):
            continue
        for grid in grids:
            try:
                grid_value = float(grid)
            except Exception:
                continue
            if grid_value > 0:
                candidates.append(grid_value)
    for combo in (result or {}).get("combos_preview") or []:
        color_range = ((combo or {}).get("ranges") or {}).get(color)
        if not color_range:
            continue
        try:
            grid_value = float(color_range[0])
        except Exception:
            continue
        if grid_value > 0:
            candidates.append(grid_value)
    return sorted(set(candidates))


def is_ahmad_warehouse_gold_table_enabled(config: dict[str, Any]) -> bool:
    automation = config.get("automation", {})
    if not bool(automation.get("ahmad_warehouse_gold_value_table_enabled", False)):
        return False
    return is_ahmad_warehouse_map(config)


def is_ahmad_warehouse_map(config: dict[str, Any]) -> bool:
    automation = config.get("automation", {})
    selected_map = str(automation.get("selected_map") or automation.get("default_map") or "")
    enabled_maps = {str(item) for item in automation.get("ahmad_warehouse_map_ids", ["2"])}
    return selected_map in enabled_maps


def is_ahmad_warehouse_round_schedule_enabled(config: dict[str, Any]) -> bool:
    automation = config.get("automation", {})
    return (
        is_ahmad_role(config)
        and is_ahmad_warehouse_map(config)
        and bool(automation.get("ahmad_gold_grid_bonus_enabled", False))
        and bool(automation.get("ahmad_gold_grid_scaled_scheme_enabled", False))
    )


def ahmad_gold_value_table(config: dict[str, Any]) -> dict[int, int]:
    table = config.get("automation", {}).get("ahmad_gold_grid_value_table") or {}
    result: dict[int, int] = {}
    for key, value in table.items():
        try:
            grid = int(float(key))
            price = int(float(value))
        except Exception:
            continue
        if grid > 0 and price > 0:
            result[grid] = price
    return result


def ahmad_color_grid_bonus_value(
    config: dict[str, Any],
    color: str,
    result: dict[str, Any],
    min_grid: float,
    unit_price: int,
) -> tuple[int, str]:
    if color != "gold" or not is_ahmad_warehouse_gold_table_enabled(config):
        return choose_rounding(float(min_grid) * float(unit_price), "floor_int"), "unit"

    table = ahmad_gold_value_table(config)
    table_candidates: list[tuple[int, int]] = []
    for grid_value in color_grid_candidates_from_result(result, color):
        grid = int(round(float(grid_value)))
        if abs(float(grid_value) - grid) > 1e-9:
            continue
        if grid in table:
            table_candidates.append((grid, int(table[grid])))
    if table_candidates:
        grid, price = min(table_candidates, key=lambda item: item[1])
        return int(price), f"warehouse_table_grid={grid}"
    direct_grid = int(round(float(min_grid)))
    if abs(float(min_grid) - direct_grid) <= 1e-9 and direct_grid in table:
        return int(table[direct_grid]), f"warehouse_table_grid={direct_grid}"
    return choose_rounding(float(min_grid) * float(unit_price), "floor_int"), "unit_fallback"


def cache_ahmad_color_grid_bonus(
    config: dict[str, Any],
    color: str,
    min_grid: float,
    unit_price: int,
    result: dict[str, Any],
) -> int:
    pricing = config.setdefault("pricing", {})
    bonus, value_source = ahmad_color_grid_bonus_value(config, color, result, min_grid, unit_price)
    if bonus <= 0:
        return 0
    pricing[f"ahmad_{color}_grid_bonus_price"] = int(bonus)
    pricing[f"ahmad_{color}_grid_bonus_min_grid"] = float(min_grid)
    pricing[f"ahmad_{color}_grid_bonus_unit_price"] = int(unit_price)
    pricing[f"ahmad_{color}_grid_bonus_value_source"] = str(value_source)
    return int(bonus)


def cached_ahmad_color_grid_bonus(config: dict[str, Any], color: str) -> tuple[int, float | None, str | None]:
    pricing = config.setdefault("pricing", {})
    try:
        bonus = int(pricing.get(f"ahmad_{color}_grid_bonus_price") or 0)
    except Exception:
        bonus = 0
    try:
        min_grid = float(pricing.get(f"ahmad_{color}_grid_bonus_min_grid"))
    except Exception:
        min_grid = None
    source = pricing.get(f"ahmad_{color}_grid_bonus_value_source")
    return max(0, bonus), min_grid, (str(source) if source not in (None, "") else None)


def ahmad_gold_grid_bonus_multiplier(config: dict[str, Any], round_no: int, start_round: int) -> tuple[float, str]:
    automation = config.get("automation", {})
    if not bool(automation.get("ahmad_gold_grid_scaled_scheme_enabled", False)):
        return 1.0, "fixed_100pct"
    relative_round = max(0, int(round_no) - int(start_round))
    if is_ahmad_warehouse_round_schedule_enabled(config):
        if relative_round <= 0:
            return 0.8, "scaled_80pct"
        return 0.8, "scaled_80pct_final_schedule"
    if relative_round <= 0:
        return 0.8, "scaled_80pct"
    if relative_round == 1:
        return 0.8 * 1.1, "scaled_80pct_x1.1"
    return 0.8 * 1.1 * 1.1, "scaled_80pct_x1.1_x1.1"


def compute_ahmad_gold_grid_bonus(
    config: dict[str, Any],
    round_no: int,
    advisor_input: dict[str, Any],
    result: dict[str, Any],
) -> tuple[int, str | None, bool]:
    automation = config.get("automation", {})
    if not bool(automation.get("ahmad_gold_grid_bonus_enabled", False)):
        return 0, None, False
    if not is_ahmad_role(config):
        return 0, None, False
    gold_unit_price = max(0, parse_int_config(automation.get("ahmad_gold_grid_bonus_unit_price"), 10000))
    purple_unit_price = max(0, parse_int_config(automation.get("ahmad_purple_grid_bonus_unit_price"), 2000))
    if gold_unit_price <= 0 and purple_unit_price <= 0:
        return 0, None, False

    color_parts: dict[str, dict[str, Any]] = {}
    base_bonus = 0
    for color in ("gold", "purple"):
        unit_price = gold_unit_price if color == "gold" else purple_unit_price
        if unit_price <= 0:
            continue
        source = "cache"
        value_source = None
        min_grid: float | None = None
        bonus = 0
        if has_ahmad_color_grid_signal(advisor_input, color):
            min_grid = min_color_grid_from_result(result, color)
            if min_grid is None:
                min_grid = direct_color_grid_from_advisor_input(advisor_input, color)
            if min_grid is not None and min_grid > 0:
                bonus = cache_ahmad_color_grid_bonus(config, color, min_grid, unit_price, result)
                _, _, value_source = cached_ahmad_color_grid_bonus(config, color)
                if bonus > 0:
                    source = "ocr"
        if bonus <= 0:
            bonus, min_grid, value_source = cached_ahmad_color_grid_bonus(config, color)
        if bonus > 0:
            color_parts[color] = {
                "source": source,
                "value_source": value_source,
                "min_grid": min_grid,
                "unit_price": unit_price,
                "bonus": int(bonus),
            }
            base_bonus += int(bonus)

    if base_bonus <= 0:
        return 0, None, False

    start_round = max(1, parse_int_config(automation.get("ahmad_gold_grid_bonus_start_round"), 3))
    applied = int(round_no) >= start_round
    multiplier, scheme_label = ahmad_gold_grid_bonus_multiplier(config, round_no, start_round)
    final_bonus = choose_rounding(float(base_bonus) * multiplier, "floor_int")
    if final_bonus <= 0:
        return 0, None, False
    part_labels = []
    for color in ("gold", "purple"):
        part = color_parts.get(color)
        if not part:
            continue
        grid = part.get("min_grid")
        grid_label = f"{grid:g}" if isinstance(grid, (int, float)) else "unknown"
        value_source = part.get("value_source") or "unknown"
        part_labels.append(
            f"{color}[source={part.get('source')} value={value_source} min_grid={grid_label} unit={part.get('unit_price')} bonus={part.get('bonus')}]"
        )
    reason = (
        f"ahmad_gold_purple_grid_bonus {'; '.join(part_labels)} "
        f"base=+{base_bonus} scheme={scheme_label} "
        f"multiplier={multiplier:.3f} -> +{final_bonus}"
    )
    if not applied:
        reason += f" pending_until_round={start_round}"
    return int(final_bonus), reason, applied


def ahmad_warehouse_round_schedule_multiplier(round_no: int) -> tuple[float | None, str | None]:
    round_index = int(round_no)
    if round_index == 4:
        return 1.1 * 1.1, "round4=round3_final*1.1*1.1"
    if round_index == 5:
        return 1.03, "round5=round4_final*1.03"
    return None, None


def apply_ahmad_warehouse_round_schedule(
    config: dict[str, Any],
    round_no: int,
    final_price: int,
    rounding: str,
) -> tuple[int, str | None]:
    if not is_ahmad_warehouse_round_schedule_enabled(config):
        return int(final_price), None

    multiplier, label = ahmad_warehouse_round_schedule_multiplier(round_no)
    if multiplier is None or label is None:
        return int(final_price), None

    pricing = config.get("pricing", {})
    previous_price_raw = pricing.get("last_submitted_price")
    try:
        previous_price = int(previous_price_raw) if previous_price_raw not in (None, "") else None
    except Exception:
        previous_price = None
    if previous_price is None or previous_price <= 0:
        return int(final_price), f"ahmad_warehouse_round_schedule {label} skipped: missing previous final price"

    previous_round_raw = pricing.get("last_submitted_round")
    try:
        previous_round = int(previous_round_raw) if previous_round_raw not in (None, "") else None
    except Exception:
        previous_round = None
    expected_previous_round = int(round_no) - 1
    if previous_round is not None and previous_round != expected_previous_round:
        return (
            int(final_price),
            f"ahmad_warehouse_round_schedule {label} skipped: previous_round={previous_round} expected={expected_previous_round}",
        )

    scheduled_price = choose_rounding(float(previous_price) * float(multiplier), rounding)
    if scheduled_price <= 0:
        return int(final_price), f"ahmad_warehouse_round_schedule {label} skipped: scheduled_price={scheduled_price}"

    previous_label = f"round{previous_round}" if previous_round is not None else "previous"
    return (
        int(scheduled_price),
        (
            f"ahmad_warehouse_round_schedule {label} "
            f"{previous_label}_price={previous_price} multiplier={multiplier:.3f} "
            f"computed_input={int(final_price)} -> {scheduled_price}"
        ),
    )


def reset_runtime_auction_state(config: dict[str, Any]) -> None:
    pricing = config.setdefault("pricing", {})
    for key in (
        "ahmad_gold_grid_bonus_price",
        "ahmad_gold_grid_bonus_min_grid",
        "ahmad_gold_grid_bonus_unit_price",
        "ahmad_gold_grid_bonus_value_source",
        "ahmad_purple_grid_bonus_price",
        "ahmad_purple_grid_bonus_min_grid",
        "ahmad_purple_grid_bonus_unit_price",
        "ahmad_purple_grid_bonus_value_source",
    ):
        pricing.pop(key, None)


def apply_bid_cap(config: dict[str, Any], final_price: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    automation = config.get("automation", {})
    bid_cap = max(0, parse_int_config(automation.get("bid_cap_price"), 0))
    if bid_cap <= 0:
        payload["bid_cap"] = {"enabled": False, "cap_price": 0, "applied": False}
        return int(final_price), payload
    capped = min(int(final_price), bid_cap)
    payload["bid_cap"] = {
        "enabled": True,
        "cap_price": bid_cap,
        "applied": capped != int(final_price),
        "original_price": int(final_price),
    }
    return int(capped), payload


def apply_safe_guard(config: dict[str, Any], final_price: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    automation = config.get("automation", {})
    safe_enabled = bool(automation.get("safe_guard_enabled", False))
    safe_limit = max(0.0, parse_float_config(automation.get("safe_guard_max_increase_ratio"), 0.0))
    previous_price = config.get("pricing", {}).get("last_submitted_price")
    if not safe_enabled:
        payload["safe_guard"] = {"enabled": False, "triggered": False}
        return int(final_price), payload
    try:
        previous = int(previous_price) if previous_price not in (None, "") else None
    except Exception:
        previous = None
    if previous is None or previous <= 0:
        payload["safe_guard"] = {"enabled": True, "triggered": False, "previous_price": previous}
        return int(final_price), payload
    limit_price = int(math.floor(previous * (1.0 + safe_limit)))
    triggered = final_price > limit_price
    payload["safe_guard"] = {
        "enabled": True,
        "triggered": triggered,
        "previous_price": previous,
        "limit_price": limit_price,
        "safe_limit_ratio": safe_limit,
    }
    if triggered:
        payload["skip_submit"] = True
        payload["reason"] = (
            f"safe_guard blocked: {final_price} > {limit_price} "
            f"(previous={previous}, ratio={safe_limit:.4f})"
        )
        return int(final_price), payload
    return int(final_price), payload


def apply_sticky_increment(config: dict[str, Any], final_price: int) -> tuple[int, str | None]:
    pricing = config.get("pricing", {})
    automation = config.get("automation", {})
    increment_ratio = max(0.0, parse_float_config(automation.get("sticky_increment_ratio"), 0.0))
    if increment_ratio <= 0:
        return int(final_price), None
    previous_price = pricing.get("last_submitted_price")
    try:
        previous = int(previous_price) if previous_price not in (None, "") else None
    except Exception:
        previous = None
    if previous is None or previous <= 0:
        return int(final_price), None
    step_value = pricing.get("sticky_increment_step")
    try:
        step = int(step_value) if step_value not in (None, "") else None
    except Exception:
        step = None
    if step is None or step <= 0:
        step = choose_rounding(float(previous) * increment_ratio, str(pricing.get("rounding", "floor_int")))
        step = max(1, int(step))
    minimum_price = int(previous) + int(step)
    if int(final_price) >= minimum_price:
        return int(final_price), None
    return int(minimum_price), f"sticky_increment linear previous={previous} step={step} -> {minimum_price}"


def should_apply_sticky_increment(config: dict[str, Any]) -> bool:
    if is_ahmad_role(config):
        return False
    return True


def compute_bid_price(
    config: dict[str, Any],
    parsed_patch: dict[str, Any],
    round_no: int,
    price_config: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    pricing = config.get("pricing", {})
    fallback = parse_int_config(pricing.get("fallback_bid_price"), 22223)
    min_facts = int(pricing.get("min_useful_facts", 1))
    multiplier = int(pricing.get("computed_price_multiplier", 10000))
    rounding = str(pricing.get("rounding", "floor_int"))
    mode = str(config.get("automation", {}).get("selected_mode", "normal")).strip().lower()

    parsed = parsed_patch
    advisor_input = build_advisor_input_from_patch(config, parsed_patch, round_no, price_config)
    facts = parsed.get("parsed_facts") or []
    payload: dict[str, Any] = {
        "fallback": False,
        "reason": "",
        "facts": len(facts),
        "parsed": parsed,
        "advisor_input": advisor_input,
        "result": {},
        "source_value": None,
    }
    if is_maria_role(config):
        value, source_reason = choose_maria_bid_value(parsed_patch)
        if value is None:
            payload["fallback"] = True
            payload["reason"] = f"missing bid value: {source_reason}"
            return fallback, payload
        payload["source_value"] = value
        sticky_reason = None
        if maria_round_bid_scheme_enabled(config):
            adjusted_value, schedule_reason = maria_round_bid_value(float(value), round_no)
            price = choose_rounding(adjusted_value, rounding)
            source_reason = f"{source_reason}; 玛丽亚专用方案 {schedule_reason}"
        else:
            price = choose_rounding(float(value), rounding)
        if price <= 0:
            payload["fallback"] = True
            payload["reason"] = f"non-positive final price: {price}"
            return fallback, payload
        final_price = price
        if not maria_round_bid_scheme_enabled(config):
            final_price, sticky_reason = apply_sticky_increment(config, price)
        final_price, payload = apply_bid_cap(config, final_price, payload)
        final_price, payload = apply_safe_guard(config, final_price, payload)
        if not payload.get("fallback") and not payload.get("skip_submit"):
            payload["reason"] = f"{source_reason} -> input={final_price}"
            if sticky_reason:
                payload["reason"] += f"; {sticky_reason}"
            bid_cap_info = payload.get("bid_cap") or {}
            if bid_cap_info.get("applied"):
                payload["reason"] += f"; bid_cap={bid_cap_info.get('cap_price')}"
        return int(final_price), payload

    if len(facts) < min_facts:
        payload["fallback"] = True
        payload["reason"] = f"not enough parsed facts: {len(facts)}"
        return fallback, payload

    result = evaluate(advisor_input)
    payload["result"] = result
    errors = result.get("errors") or []
    if errors and mode != "express":
        payload["fallback"] = True
        payload["reason"] = "; ".join(str(item) for item in errors)
        return fallback, payload

    if mode == "express":
        value, source_reason = choose_express_bid_value(config, parsed_patch)
    else:
        value, source_reason = choose_bid_value_by_mode(config, result)
    if value is None:
        payload["fallback"] = True
        payload["reason"] = f"missing bid value: {source_reason}"
        return fallback, payload

    value = float(value)
    payload["source_value"] = value
    if value <= 0:
        payload["fallback"] = True
        payload["reason"] = f"non-positive source value: {value}"
        return fallback, payload

    if mode == "express":
        price = choose_rounding(value, rounding)
    else:
        price = choose_rounding(value * multiplier, rounding)
    if price <= 0:
        payload["fallback"] = True
        payload["reason"] = f"non-positive final price: {price}"
        return fallback, payload
    bonus_price, bonus_reason, bonus_applied = compute_ahmad_gold_grid_bonus(
        config,
        round_no,
        advisor_input,
        result,
    )
    if bonus_applied:
        price += bonus_price
    final_price, low_price_reason = apply_observed_low_price_floor(result, price, rounding)
    final_price, round_schedule_reason = apply_ahmad_warehouse_round_schedule(config, round_no, final_price, rounding)
    sticky_reason = None
    if should_apply_sticky_increment(config):
        final_price, sticky_reason = apply_sticky_increment(config, final_price)
    final_price, payload = apply_bid_cap(config, final_price, payload)
    final_price, payload = apply_safe_guard(config, final_price, payload)
    if payload.get("fallback"):
        return int(final_price), payload
    if mode == "express":
        payload["reason"] = f"{source_reason} -> input={price}"
    else:
        payload["reason"] = f"{source_reason}: {value:.4f}w * {multiplier} -> input={price}"
    if low_price_reason:
        payload["reason"] += f"; {low_price_reason}"
    if round_schedule_reason:
        payload["reason"] += f"; {round_schedule_reason}"
    if final_price != price or low_price_reason or round_schedule_reason:
        payload["reason"] += f"; final={final_price}"
    if bonus_reason:
        payload["reason"] += f"; {bonus_reason}"
    if sticky_reason:
        payload["reason"] += f"; {sticky_reason}"
    bid_cap_info = payload.get("bid_cap") or {}
    if bid_cap_info.get("applied"):
        payload["reason"] += f"; bid_cap={bid_cap_info.get('cap_price')}"
    return int(final_price), payload


def wait_with_observation(
    config: dict[str, Any],
    config_path: Path,
    seconds: float,
    message: str,
    *,
    observe: bool = True,
) -> None:
    ensure_not_stopped()
    seconds = max(0.0, float(seconds))
    if seconds <= 0:
        return
    log(f"{message}: wait {seconds:g}s")
    if not observe:
        sleep_interruptible(seconds)
        return
    end = time.monotonic() + seconds
    poll_seconds = max(0.2, float(config.get("timing", {}).get("poll_seconds", 2.0)))
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        sleep_interruptible(min(poll_seconds, remaining))
        observation = observe_state(config, config_path, f"{message.replace(' ', '_')}_wait")
        if observation.end_prompt:
            raise EndPromptDetected(message)


def handle_round(
    config: dict[str, Any],
    config_path: Path,
    price_config: dict[str, Any],
    round_no: int,
    knowledge_patch: dict[str, Any] | None,
) -> dict[str, Any] | None:
    ensure_not_stopped()
    timing = config.get("timing", {})
    if int(round_no) == 1:
        round_wait = float(timing.get("round_detect_wait_seconds", 15.0))
        round_wait += float(timing.get("round1_extra_wait_seconds", 0.0))
    else:
        round_wait = float(timing.get("round2_5_detect_wait_seconds", 2.0))
    random_extra = current_time_random_extra(timing.get("round_detect_random_extra_max_seconds", 5.0))
    if random_extra > 0:
        round_wait += random_extra
        log(f"round {round_no}: random bid wait extra={random_extra:.2f}s")
    wait_with_observation(
        config,
        config_path,
        round_wait,
        f"round {round_no} detected",
        observe=bool(timing.get("observe_during_round_wait", False)),
    )
    tool_rounds = {int(item) for item in config.get("automation", {}).get("tool_rounds", [1, 2])}
    if int(round_no) in tool_rounds:
        run_tool_sequence(config)
        wait_with_observation(
            config,
            config_path,
            float(config.get("timing", {}).get("tool_after_wait_seconds", 5.0)),
            "after tool",
        )
    else:
        log(f"round {round_no}: tool skipped by config")
    observation = observe_state(config, config_path, f"round{round_no}_after_tool")
    knowledge_patch = apply_observation_memory(observation, knowledge_patch)
    if observation.end_prompt:
        raise EndPromptDetected(f"round {round_no} after tool")
    effective_patch = knowledge_patch or observation.capture.parsed
    price, details = compute_bid_price(config, effective_patch, round_no, price_config)
    summary = (details.get("result") or {}).get("summary") or {}
    advisor_input = details.get("advisor_input") or build_advisor_input_from_patch(config, effective_patch, round_no, price_config)
    if details.get("fallback"):
        log(f"price fallback: {price}; reason={details.get('reason')}")
    else:
        log(
            "price computed: "
            f"{price}; {details.get('reason')}; "
            f"facts={details.get('facts')} combo={summary.get('combo_count')}"
        )
    if bool(config.get("debug", {}).get("print_ocr_snippet", False)):
        log("ocr snippet: " + compact_text(observation.capture.text)[:160])
    if bool(config.get("debug", {}).get("print_round_debug", False)):
        log(f"debug raw ocr: {repr(observation.capture.text[:300])}")
        log(f"debug advisor input keys: {sorted(advisor_input.keys())}")
        log(f"debug parsed facts: {len((effective_patch or {}).get('parsed_facts') or [])}")
    save_round_debug_bundle(
        config,
        config_path,
        round_no=round_no,
        raw_text=observation.capture.text,
        knowledge_patch=effective_patch,
        advisor_input=advisor_input,
        details=details,
        final_price=price,
    )
    if details.get("skip_submit"):
        log(f"bid skipped: {details.get('reason')}")
        return knowledge_patch
    if not input_bid(config, price):
        log("bid not submitted: guarded against abandon click")
        return knowledge_patch
    persist_last_submitted_price(config_path, price, config, round_no=round_no)
    return knowledge_patch


def load_price_config(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    price_path = resolve_path(config_path, config.get("advisor", {}).get("price_config_path"), "price_config.json")
    if not price_path.exists():
        log(f"warn: price config not found, using defaults: {price_path}")
        return {}
    return load_json(price_path)


def handle_end_transition(
    config: dict[str, Any],
    handled_rounds: set[int],
    last_end_at: float,
    transition_debounce: float,
    source: str,
) -> tuple[float, float]:
    if time.monotonic() - last_end_at < transition_debounce:
        log(f"{source}: end prompt ignored by debounce")
        return last_end_at, 0.0
    log(f"{source}: end prompt detected")
    confirm_at = run_post_round_transition(config)
    handled_rounds.clear()
    reset_runtime_auction_state(config)
    return time.monotonic(), confirm_at


def run_loop(config_path: Path) -> None:
    config = load_json(config_path)
    price_config = load_price_config(config, config_path)
    persist_last_submitted_price(config_path, None, config)
    reset_runtime_auction_state(config)
    selected_map = str(config.get("automation", {}).get("selected_map") or config.get("automation", {}).get("default_map", "4"))
    max_runs = int(config.get("automation", {}).get("selected_runs") or config.get("automation", {}).get("default_runs", 1))
    pyautogui.FAILSAFE = bool(config.get("safety", {}).get("failsafe", True))
    pyautogui.PAUSE = float(config.get("safety", {}).get("move_pause_seconds", 0.08))
    prepare_target_window(config, center=True)

    log("fresh bot started")
    log("mode: full-window OCR -> lobby/end/round handling")

    handled_rounds: set[int] = set()
    knowledge_patch: dict[str, Any] | None = None
    completed_runs = 0
    last_end_at = 0.0
    last_lobby_at = 0.0
    last_home_bid_at = 0.0
    last_reward_continue_at = 0.0
    last_unknown_escape_at = 0.0
    last_post_continue_confirm_at = 0.0
    poll_seconds = float(config.get("timing", {}).get("poll_seconds", 1.0))
    transition_debounce = float(config.get("timing", {}).get("transition_debounce_seconds", 8.0))
    reward_continue_debounce = float(config.get("timing", {}).get("reward_continue_debounce_seconds", 1.0))
    unknown_escape_cooldown = float(config.get("automation", {}).get("unknown_escape_cooldown_seconds", 2.0))
    post_confirm_escape_block_seconds = float(config.get("automation", {}).get("post_confirm_escape_block_seconds", 30.0))
    loop_index = 0

    while True:
        loop_index += 1
        try:
            ensure_not_stopped()
            observation = observe_state(config, config_path, "poll")
            knowledge_patch = apply_observation_memory(observation, knowledge_patch)
            round_no = observation.round_no
            log(
                f"loop {loop_index}: observed round={round_no} "
                f"end={observation.end_prompt} lobby={observation.auction_lobby} "
                f"reward_continue={observation.reward_continue} "
                f"home_bid={observation.home_bid_button} any={observation.has_any_signal}"
            )

            if not observation.has_any_signal:
                since_post_confirm = time.monotonic() - last_post_continue_confirm_at
                if since_post_confirm < post_confirm_escape_block_seconds:
                    log(
                        f"loop {loop_index}: no signal, esc blocked after post_continue_confirm "
                        f"({since_post_confirm:.1f}/{post_confirm_escape_block_seconds:.1f}s)"
                    )
                elif time.monotonic() - last_unknown_escape_at >= unknown_escape_cooldown:
                    press_escape(config)
                    last_unknown_escape_at = time.monotonic()
                else:
                    log(f"loop {loop_index}: no signal, esc on cooldown")
                sleep_interruptible(poll_seconds)
                continue

            if observation.end_prompt:
                last_end_at, confirm_at = handle_end_transition(
                    config,
                    handled_rounds,
                    last_end_at,
                    transition_debounce,
                    f"loop {loop_index}",
                )
                if confirm_at:
                    last_post_continue_confirm_at = confirm_at
                completed_runs += 1
                knowledge_patch = None
                persist_last_submitted_price(config_path, None, config)
                log(f"completed runs: {completed_runs}/{max_runs}")
                if completed_runs >= max_runs:
                    log("target runs reached; exit")
                    return
                sleep_interruptible(poll_seconds)
                continue

            if observation.reward_continue:
                if time.monotonic() - last_reward_continue_at >= reward_continue_debounce:
                    run_reward_continue_transition(config)
                    knowledge_patch = None
                    last_reward_continue_at = time.monotonic()
                else:
                    log(f"loop {loop_index}: reward continue ignored by debounce")
                sleep_interruptible(poll_seconds)
                continue

            if observation.auction_lobby:
                if time.monotonic() - last_lobby_at >= transition_debounce:
                    confirm_at = run_map_selection_transition(config, selected_map)
                    if confirm_at:
                        last_post_continue_confirm_at = confirm_at
                    handled_rounds.clear()
                    knowledge_patch = None
                    persist_last_submitted_price(config_path, None, config)
                    reset_runtime_auction_state(config)
                    last_lobby_at = time.monotonic()
                else:
                    log(f"loop {loop_index}: auction lobby ignored by debounce")
                sleep_interruptible(poll_seconds)
                continue

            if observation.home_bid_button:
                if time.monotonic() - last_home_bid_at >= transition_debounce:
                    run_home_bid_button_transition(config)
                    knowledge_patch = None
                    persist_last_submitted_price(config_path, None, config)
                    reset_runtime_auction_state(config)
                    last_home_bid_at = time.monotonic()
                else:
                    log(f"loop {loop_index}: home bid button ignored by debounce")
                sleep_interruptible(poll_seconds)
                continue

            if round_no is None:
                log(f"loop {loop_index}: no round detected; waiting")
                sleep_interruptible(poll_seconds)
                continue

            if round_no == 1 and any(value > 1 for value in handled_rounds):
                log("new auction inferred from round 1; reset handled rounds")
                handled_rounds.clear()
                knowledge_patch = apply_observation_memory(observation, None)
                persist_last_submitted_price(config_path, None, config)
                reset_runtime_auction_state(config)

            if round_no in handled_rounds:
                log(f"loop {loop_index}: round {round_no} already handled; waiting")
                sleep_interruptible(poll_seconds)
                continue

            log(f"loop {loop_index}: round {round_no} detected")
            knowledge_patch = handle_round(config, config_path, price_config, round_no, knowledge_patch)
            handled_rounds.add(round_no)

            if round_no >= 5:
                log("round 5 handled; waiting for end prompt or a new OCR state")

            sleep_interruptible(poll_seconds)
        except KeyboardInterrupt:
            log("stopped by Ctrl+C")
            return
        except StopRequested:
            log("stopped by GUI")
            return
        except EndPromptDetected as exc:
            last_end_at, confirm_at = handle_end_transition(
                config,
                handled_rounds,
                last_end_at,
                transition_debounce,
                f"active handling ({exc.source})",
            )
            if confirm_at:
                last_post_continue_confirm_at = confirm_at
            completed_runs += 1
            knowledge_patch = None
            persist_last_submitted_price(config_path, None, config)
            log(f"completed runs: {completed_runs}/{max_runs}")
            if completed_runs >= max_runs:
                log("target runs reached; exit")
                return
            sleep_interruptible(poll_seconds)
        except Exception as exc:
            log(f"error: {type(exc).__name__}: {exc}")
            sleep_interruptible(max(1.0, poll_seconds))


def print_click_positions(config_path: Path) -> None:
    config = load_json(config_path)
    info = find_window(config.get("window", {}))
    log(f"window hwnd={info.hwnd} client_origin={info.client_origin} client_size={info.width}x{info.height}")
    for name in (
        "map_page_next",
        "tool_button",
        "leftmost_tool",
        "tool_confirm",
        "bid_button",
        "bid_input_box",
        "bid_confirm",
        "end_reward_click",
        "end_close_click",
        "continue_button",
        "post_continue_action",
        "post_continue_confirm",
        "reward_continue_button",
    ):
        point = config.get("clicks", {}).get(name)
        if not point:
            continue
        sx, sy = client_to_screen(config, point)
        origin = point.get("origin", "left_top")
        log(f"{name}: config=({point['x']},{point['y']}) origin={origin} -> screen=({sx},{sy})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fresh BidKing bot loop.")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--print-clicks", action="store_true", help="Print converted screen click positions and exit.")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    if args.print_clicks:
        print_click_positions(config_path)
    else:
        config = load_json(config_path)
        maps = config.get("automation", {}).get("maps", {})
        default_map = str(config.get("automation", {}).get("default_map", "4"))
        default_runs = int(config.get("automation", {}).get("default_runs", 1))
        print("请选择地图：")
        for key in ("1", "2", "3", "4", "5", "6", "7"):
            item = maps.get(key, {})
            print(f"{key}. {item.get('name', key)}")
        map_input = input(f"地图编号 [默认 {default_map}]: ").strip() or default_map
        runs_input = input(f"刷取次数 [默认 {default_runs}]: ").strip() or str(default_runs)
        selected_runs = int(runs_input) if runs_input.isdigit() and int(runs_input) > 0 else default_runs
        config.setdefault("automation", {})
        config["automation"]["selected_map"] = map_input
        config["automation"]["selected_runs"] = selected_runs
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        reset_stop()
        run_loop(config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
