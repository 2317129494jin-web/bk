#!/usr/bin/env python3
from __future__ import annotations

import json
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import fresh_bidking_bot as bot
from manual_bidking_advisor import evaluate, render_report


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
PRICE_CONFIG_PATH = ROOT / "price_config.json"

MAP_KEYS = ("1", "2", "3", "4", "5", "6", "7")
RISK_OPTIONS = {
    "保守": "floor_price",
    "均衡": "avg_price",
    "激进": "avg_price_plus_25",
    "自定义": "custom_factor",
}
RISK_LABEL_BY_VALUE = {value: label for label, value in RISK_OPTIONS.items()}
MODE_OPTIONS = {
    "标准模式": "normal",
    "快递跑刀": "express",
}
MODE_LABEL_BY_VALUE = {value: label for label, value in MODE_OPTIONS.items()}
ROLE_OPTIONS = {
    "艾哈迈德": "ahmad",
    "拉文": "lavin",
    "维克托": "victor",
    "玛丽亚": "maria",
}
ROLE_LABEL_BY_VALUE = {value: label for label, value in ROLE_OPTIONS.items()}


class GuiLogger:
    def __init__(self, write_line):
        self.write_line = write_line

    def __call__(self, message: str) -> None:
        self.write_line(message)


class BidKingApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("竞拍之王助手")
        self.root.geometry("1360x940")
        self.root.minsize(1200, 860)

        self.worker: threading.Thread | None = None
        self.stop_requested = False
        self.original_log = bot.log
        bot.log = GuiLogger(self.append_log)

        self.config = self.load_json(CONFIG_PATH)
        self.price_config = self.load_json(PRICE_CONFIG_PATH)

        self.map_var = tk.StringVar()
        self.runs_var = tk.StringVar()
        self.mode_var = tk.StringVar()
        self.risk_var = tk.StringVar()
        self.role_var = tk.StringVar()
        self.tool_round_vars: dict[int, tk.BooleanVar] = {}
        self.custom_risk_var = tk.StringVar()
        self.fallback_price_var = tk.StringVar()
        self.express_multiplier_var = tk.StringVar()
        self.safe_guard_enabled_var = tk.BooleanVar()
        self.safe_guard_ratio_var = tk.StringVar()
        self.bid_cap_price_var = tk.StringVar()
        self.sticky_increment_var = tk.StringVar()
        self.maria_round_bid_scheme_enabled_var = tk.BooleanVar()
        self.ahmad_gold_grid_bonus_enabled_var = tk.BooleanVar()
        self.ahmad_gold_grid_scaled_scheme_enabled_var = tk.BooleanVar()
        self.calc_vars: dict[str, tk.StringVar] = {}
        self.weight_summary_var = tk.StringVar()

        self.build_ui()
        self.load_into_form()
        self.mode_var.trace_add("write", lambda *_: self.on_mode_changed())
        self.on_mode_changed()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def save_json(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def build_ui(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)
        main = ttk.Frame(notebook, padding=12)
        calc_page = ttk.Frame(notebook, padding=12)
        notebook.add(main, text="自动化")
        notebook.add(calc_page, text="手动计算器")

        top = ttk.Frame(main)
        top.pack(fill="x")

        weight_box = ttk.LabelFrame(top, text="1. 品类权重设置", padding=10)
        weight_box.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ttk.Label(weight_box, text="新版逻辑按品类权重计算场景价格").pack(anchor="w")
        ttk.Label(weight_box, textvariable=self.weight_summary_var, wraplength=420, justify="left").pack(anchor="w", pady=(6, 8))
        ttk.Button(weight_box, text="设置权重", command=self.open_weight_editor).pack(anchor="w")

        settings_box = ttk.LabelFrame(top, text="2. 选图与重复轮数", padding=10)
        settings_box.pack(side="left", fill="both", expand=True)

        ttk.Label(settings_box, text="地图").grid(row=0, column=0, sticky="w", pady=4)
        self.map_combo = ttk.Combobox(settings_box, textvariable=self.map_var, state="readonly", width=20)
        self.map_combo["values"] = [f"{k}. {self.config['automation']['maps'][k]['name']}" for k in MAP_KEYS]
        self.map_combo.grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(settings_box, text="重复次数").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(settings_box, textvariable=self.runs_var, width=10).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(settings_box, text="模式").grid(row=2, column=0, sticky="w", pady=4)
        self.mode_combo = ttk.Combobox(settings_box, textvariable=self.mode_var, state="readonly", width=20)
        self.mode_combo["values"] = list(MODE_OPTIONS.keys())
        self.mode_combo.grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(settings_box, text="角色").grid(row=3, column=0, sticky="w", pady=4)
        self.role_combo = ttk.Combobox(settings_box, textvariable=self.role_var, state="readonly", width=20)
        self.role_combo["values"] = list(ROLE_OPTIONS.keys())
        self.role_combo.grid(row=3, column=1, sticky="w", pady=4)

        button_box = ttk.LabelFrame(main, text="3. 控制", padding=10)
        button_box.pack(fill="x", pady=(10, 0))
        self.start_btn = ttk.Button(button_box, text="开启", command=self.start_bot)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(button_box, text="停止", command=self.stop_bot)
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.stop_btn.state(["disabled"])

        tool_rounds_box = ttk.LabelFrame(main, text="5. 道具使用回合", padding=10)
        tool_rounds_box.pack(fill="x", pady=(10, 0))
        ttk.Label(tool_rounds_box, text="勾选后，该回合会自动使用最左边道具。快递跑刀默认不使用道具。").pack(side="left", padx=(0, 12))
        for round_no in range(1, 6):
            var = tk.BooleanVar(value=round_no in (1, 2))
            self.tool_round_vars[round_no] = var
            ttk.Checkbutton(tool_rounds_box, text=f"第{round_no}回合", variable=var).pack(side="left", padx=(0, 8))

        risk_box = ttk.LabelFrame(main, text="6. 拍卖激进度", padding=10)
        risk_box.pack(fill="x", pady=(10, 0))
        ttk.Label(risk_box, text="模式").pack(side="left")
        risk_combo = ttk.Combobox(risk_box, textvariable=self.risk_var, state="readonly", width=12)
        risk_combo["values"] = list(RISK_OPTIONS.keys())
        risk_combo.pack(side="left", padx=(8, 12))
        ttk.Label(risk_box, text="自定义倍率").pack(side="left")
        ttk.Entry(risk_box, textvariable=self.custom_risk_var, width=10).pack(side="left", padx=(8, 12))
        ttk.Label(risk_box, text="例如 -0.2=平均价80%，0.8=平均价180%").pack(side="left")

        extra_box = ttk.LabelFrame(main, text="4. 模式与安全", padding=10)
        extra_box.pack(fill="x", pady=(10, 0))
        row1 = ttk.Frame(extra_box)
        row1.pack(fill="x")
        ttk.Label(row1, text="快递跑刀单件价格").pack(side="left")
        ttk.Entry(row1, textvariable=self.express_multiplier_var, width=10).pack(side="left", padx=(8, 12))
        ttk.Label(row1, text="快递跑刀会自动锁定地图为快递盲盒堆。出价=总物品数*单件价格，直接按个位价格出价").pack(side="left")

        row2 = ttk.Frame(extra_box)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(row2, text="安全开关", variable=self.safe_guard_enabled_var).pack(side="left")
        ttk.Label(row2, text="单回合最大加价比例").pack(side="left", padx=(12, 0))
        ttk.Entry(row2, textvariable=self.safe_guard_ratio_var, width=10).pack(side="left", padx=(8, 12))
        ttk.Label(row2, text="例如 0.5 代表超过上回合 50% 就自动取消").pack(side="left")

        row3 = ttk.Frame(extra_box)
        row3.pack(fill="x", pady=(8, 0))
        ttk.Label(row3, text="出价硬顶").pack(side="left")
        ttk.Entry(row3, textvariable=self.bid_cap_price_var, width=10).pack(side="left", padx=(8, 12))
        ttk.Label(row3, text="达到硬顶后停止继续加价，0 代表无硬顶").pack(side="left")

        row4 = ttk.Frame(extra_box)
        row4.pack(fill="x", pady=(8, 0))
        ttk.Label(row4, text="防黏递增比例").pack(side="left")
        ttk.Entry(row4, textvariable=self.sticky_increment_var, width=10).pack(side="left", padx=(8, 12))
        ttk.Label(row4, text="按首个出价生成固定步长，后续每回合线性递增且不自动降价；艾哈迈德方案不应用").pack(side="left")

        row5 = ttk.Frame(extra_box)
        row5.pack(fill="x", pady=(8, 0))
        ttk.Label(row5, text="Fallback 出价").pack(side="left")
        ttk.Entry(row5, textvariable=self.fallback_price_var, width=10).pack(side="left", padx=(8, 12))
        ttk.Label(row5, text="识别失败或被安全开关拦截时使用").pack(side="left")

        row6 = ttk.Frame(extra_box)
        row6.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(row6, text="玛丽亚专用出价方案", variable=self.maria_round_bid_scheme_enabled_var).pack(side="left")
        ttk.Label(row6, text="仅角色为玛丽亚时生效：1/2回合85%，3回合x1.33，4回合x1.1x1.1，5回合x1.03").pack(side="left", padx=(12, 0))

        row7 = ttk.Frame(extra_box)
        row7.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(row7, text="艾哈迈德金/紫均加价", variable=self.ahmad_gold_grid_bonus_enabled_var).pack(side="left")
        ttk.Label(row7, text="仅角色为艾哈迈德时生效：第2轮识别橙/金均格，第3轮识别紫均格；仓库金按表估值，紫按每格2000").pack(side="left", padx=(12, 0))

        row8 = ttk.Frame(extra_box)
        row8.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(row8, text="艾哈迈德金均递增方案", variable=self.ahmad_gold_grid_scaled_scheme_enabled_var).pack(side="left")
        ttk.Label(row8, text="开启后：第3轮加80%(金+紫)，第4轮=第3轮最终价x1.1x1.1，第5轮=第4轮最终价x1.03；关闭则按旧方案直接加100%").pack(side="left", padx=(12, 0))

        tip_box = ttk.LabelFrame(main, text="7. 道具提示", padding=10)
        tip_box.pack(fill="x", pady=(10, 0))
        ttk.Label(tip_box, text="维克托：优先带紫色数量、橙色均格；有钱可以带橙色扫描").pack(anchor="w")
        ttk.Label(tip_box, text="石油佬：优先带蓝色数量、绿白均格、绿白总格").pack(anchor="w", pady=(6, 0))

        log_box = ttk.LabelFrame(main, text="运行日志 / Debug", padding=10)
        log_box.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(log_box, height=20, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.build_calculator_ui(calc_page)

    def build_calculator_ui(self, parent: ttk.Frame) -> None:
        input_box = ttk.LabelFrame(parent, text="手动计算器本体", padding=10)
        input_box.pack(fill="x")
        fields = [
            ("round", "回合", "1"),
            ("role", "角色", ""),
            ("maria_start_price", "玛丽亚起拍价", ""),
            ("total_all", "总藏品数", ""),
            ("avg_grid_all", "总平均格子", ""),
            ("total_grid_all", "全部总格数", ""),
            ("wg_total", "白+绿总数", ""),
            ("avg_wg", "白+绿均格", ""),
            ("grid_wg", "白+绿总格", ""),
            ("count_white", "白件数", ""),
            ("count_green", "绿件数", ""),
            ("count_blue", "蓝件数", ""),
            ("grid_blue", "蓝总格", ""),
            ("avg_blue", "蓝均格", ""),
            ("count_purple", "紫件数", ""),
            ("grid_purple", "紫总格", ""),
            ("avg_purple", "紫均格", ""),
            ("count_gold", "橙件数", ""),
            ("grid_gold", "橙总格", ""),
            ("avg_gold", "橙均格", ""),
            ("count_red", "红件数", ""),
            ("grid_red", "红总格", ""),
            ("avg_red", "红均格", ""),
            ("avg_price_wg", "白绿均价原数", ""),
            ("total_price_wg", "白绿总价原数", ""),
            ("avg_price_white", "白均价原数", ""),
            ("total_price_white", "白总价原数", ""),
            ("avg_price_green", "绿均价原数", ""),
            ("total_price_green", "绿总价原数", ""),
            ("avg_price_blue", "蓝均价原数", ""),
            ("total_price_blue", "蓝总价原数", ""),
            ("avg_price_purple", "紫均价原数", ""),
            ("total_price_purple", "紫总价原数", ""),
            ("avg_price_gold", "橙均价原数", ""),
            ("total_price_gold", "橙总价原数", ""),
            ("avg_price_red", "红均价原数", ""),
            ("total_price_red", "红总价原数", ""),
        ]
        for idx, (key, label, default) in enumerate(fields):
            row = idx // 4
            col = (idx % 4) * 2
            ttk.Label(input_box, text=label).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=3)
            var = tk.StringVar(value=default)
            self.calc_vars[key] = var
            if key == "role":
                combo = ttk.Combobox(input_box, textvariable=var, state="readonly", width=12)
                combo["values"] = list(ROLE_OPTIONS.keys())
                combo.grid(row=row, column=col + 1, sticky="w", pady=3)
            else:
                ttk.Entry(input_box, textvariable=var, width=12).grid(row=row, column=col + 1, sticky="w", pady=3)

        button_row = ttk.Frame(parent)
        button_row.pack(fill="x", pady=(8, 0))
        ttk.Button(button_row, text="计算", command=self.run_manual_calculator).pack(side="left")
        ttk.Button(button_row, text="清空", command=self.clear_manual_calculator).pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="同步自动化设置", command=self.sync_calculator_from_config).pack(side="left", padx=(8, 0))

        result_box = ttk.LabelFrame(parent, text="计算结果", padding=10)
        result_box.pack(fill="both", expand=True, pady=(10, 0))
        self.calc_result_text = tk.Text(result_box, height=24, wrap="word")
        self.calc_result_text.pack(fill="both", expand=True)

    def load_into_form(self) -> None:
        default_map = str(self.config.get("automation", {}).get("default_map", "4"))
        self.map_var.set(f"{default_map}. {self.config['automation']['maps'][default_map]['name']}")
        self.runs_var.set(str(self.config.get("automation", {}).get("default_runs", 1)))
        selected_mode = self.config.get("automation", {}).get("selected_mode", "normal")
        mode_label = MODE_LABEL_BY_VALUE.get(selected_mode, "标准模式")
        self.mode_var.set(mode_label)
        self.risk_var.set("均衡")
        role = self.config.get("advisor", {}).get("role", "ahmad")
        self.role_var.set(ROLE_LABEL_BY_VALUE.get(role, "艾哈迈德"))
        selected_risk = self.config.get("automation", {}).get("selected_risk", "avg_price")
        self.risk_var.set(RISK_LABEL_BY_VALUE.get(selected_risk, "均衡"))
        self.custom_risk_var.set(str(self.config.get("automation", {}).get("custom_risk_factor", 0.0)))
        self.fallback_price_var.set(str(self.config.get("pricing", {}).get("fallback_bid_price", 30000)))
        self.express_multiplier_var.set(str(self.config.get("automation", {}).get("express_total_multiplier", 1000)))
        self.safe_guard_enabled_var.set(bool(self.config.get("automation", {}).get("safe_guard_enabled", False)))
        self.safe_guard_ratio_var.set(str(self.config.get("automation", {}).get("safe_guard_max_increase_ratio", 0.5)))
        self.bid_cap_price_var.set(str(self.config.get("automation", {}).get("bid_cap_price", 0)))
        self.sticky_increment_var.set(str(self.config.get("automation", {}).get("sticky_increment_ratio", 0.03)))
        self.maria_round_bid_scheme_enabled_var.set(bool(self.config.get("automation", {}).get("maria_round_bid_scheme_enabled", True)))
        self.ahmad_gold_grid_bonus_enabled_var.set(bool(self.config.get("automation", {}).get("ahmad_gold_grid_bonus_enabled", True)))
        self.ahmad_gold_grid_scaled_scheme_enabled_var.set(bool(self.config.get("automation", {}).get("ahmad_gold_grid_scaled_scheme_enabled", True)))
        tool_rounds = {int(item) for item in self.config.get("automation", {}).get("tool_rounds", [1, 2])}
        for round_no, var in self.tool_round_vars.items():
            var.set(round_no in tool_rounds)
        self.refresh_weight_summary()
        self.sync_calculator_from_config()

    def append_log(self, message: str) -> None:
        def _write():
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
        self.root.after(0, _write)

    def selected_map_key(self) -> str:
        text = self.map_var.get().strip()
        return text.split(".", 1)[0].strip() if "." in text else text

    def apply_form_to_config(self) -> None:
        runs_value = int(self.runs_var.get()) if self.runs_var.get().isdigit() and int(self.runs_var.get()) > 0 else 1
        selected_mode = MODE_OPTIONS.get(self.mode_var.get().strip(), "normal")
        selected_map = self.selected_map_key() or "4"
        selected_risk = RISK_OPTIONS.get(self.risk_var.get().strip(), "avg_price")
        selected_role = ROLE_OPTIONS.get(self.role_var.get().strip(), "ahmad")
        selected_tool_rounds = [round_no for round_no, var in self.tool_round_vars.items() if var.get()]
        if selected_mode == "express":
            selected_tool_rounds = []
        custom_risk_factor = float(self.custom_risk_var.get().strip() or "0")
        fallback_bid_price = int(float(self.fallback_price_var.get().strip() or "30000"))
        express_total_multiplier = float(self.express_multiplier_var.get().strip() or "1000")
        safe_guard_ratio = float(self.safe_guard_ratio_var.get().strip() or "0")
        bid_cap_price = int(float(self.bid_cap_price_var.get().strip() or "0"))
        sticky_increment_ratio = float(self.sticky_increment_var.get().strip() or "0")

        self.config.setdefault("automation", {})
        self.config.setdefault("pricing", {})
        self.config["automation"]["selected_map"] = selected_map
        self.config["automation"]["selected_runs"] = runs_value
        self.config["automation"]["selected_risk"] = selected_risk
        self.config["automation"]["selected_mode"] = selected_mode
        self.config["automation"]["custom_risk_factor"] = custom_risk_factor
        self.config["automation"]["express_total_multiplier"] = express_total_multiplier
        self.config["automation"]["safe_guard_enabled"] = bool(self.safe_guard_enabled_var.get())
        self.config["automation"]["safe_guard_max_increase_ratio"] = safe_guard_ratio
        self.config["automation"]["bid_cap_price"] = bid_cap_price
        self.config["automation"]["sticky_increment_ratio"] = sticky_increment_ratio
        self.config["automation"]["maria_round_bid_scheme_enabled"] = bool(self.maria_round_bid_scheme_enabled_var.get())
        self.config["automation"]["ahmad_gold_grid_bonus_enabled"] = bool(self.ahmad_gold_grid_bonus_enabled_var.get())
        self.config["automation"]["ahmad_gold_grid_scaled_scheme_enabled"] = bool(self.ahmad_gold_grid_scaled_scheme_enabled_var.get())
        self.config["automation"]["tool_rounds"] = selected_tool_rounds
        self.config["pricing"]["fallback_bid_price"] = fallback_bid_price
        self.config.setdefault("advisor", {})["role"] = selected_role

        self.save_json(CONFIG_PATH, self.config)
        self.save_json(PRICE_CONFIG_PATH, self.price_config)

    def refresh_weight_summary(self) -> None:
        weights = self.price_config.get("category_weights", {})
        non_default = [f"cat{i}={weights.get(f'cat{i}', 1)}" for i in range(1, 11) if int(weights.get(f"cat{i}", 1)) != 1]
        if non_default:
            self.weight_summary_var.set("当前已修改: " + "，".join(non_default))
        else:
            self.weight_summary_var.set("当前全部为默认权重 1")

    def on_mode_changed(self) -> None:
        mode = MODE_OPTIONS.get(self.mode_var.get().strip(), "normal")
        if mode == "express":
            self.map_combo.state(["!disabled"])
            for var in self.tool_round_vars.values():
                var.set(False)
        else:
            self.map_combo.state(["!disabled"])

    def open_weight_editor(self) -> None:
        top = tk.Toplevel(self.root)
        top.title("品类权重设置")
        top.geometry("520x420")
        top.transient(self.root)
        top.grab_set()

        labels = [
            ("cat1", "家具日用"),
            ("cat2", "医疗用品"),
            ("cat3", "时尚潮流"),
            ("cat4", "武器装备"),
            ("cat5", "矿物珠宝"),
            ("cat6", "文玩古董"),
            ("cat7", "数码电子"),
            ("cat8", "能源交通"),
            ("cat9", "饮食烹饪"),
            ("cat10", "书籍绘画"),
        ]
        vars_map: dict[str, tk.StringVar] = {}
        weights = self.price_config.setdefault("category_weights", {})
        wrapper = ttk.Frame(top, padding=12)
        wrapper.pack(fill="both", expand=True)
        header = ttk.Frame(wrapper)
        header.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
        ttk.Label(header, text="只支持 0 / 1 / 2。0=排除，1=默认，2=强化").pack(anchor="w")
        for idx, (key, label) in enumerate(labels):
            row = idx // 2 + 1
            col = (idx % 2) * 2
            ttk.Label(wrapper, text=label).grid(row=row, column=col, sticky="w", padx=(0, 8), pady=6)
            var = tk.StringVar(value=str(weights.get(key, 1)))
            vars_map[key] = var
            combo = ttk.Combobox(wrapper, textvariable=var, state="readonly", width=8)
            combo["values"] = ("0", "1", "2")
            combo.grid(row=row, column=col + 1, sticky="w", pady=6)

        button_row = ttk.Frame(wrapper)
        button_row.grid(row=6 + 1, column=0, columnspan=4, sticky="w", pady=(16, 0))

        def reset_weights() -> None:
            for key in vars_map:
                vars_map[key].set("1")

        def save_weights() -> None:
            for key, var in vars_map.items():
                self.price_config.setdefault("category_weights", {})[key] = int(var.get())
            self.save_json(PRICE_CONFIG_PATH, self.price_config)
            self.refresh_weight_summary()
            top.destroy()

        ttk.Button(button_row, text="恢复默认", command=reset_weights).pack(side="left")
        ttk.Button(button_row, text="保存", command=save_weights).pack(side="left", padx=(8, 0))

    def _float_or_none(self, value: str):
        value = value.strip()
        return None if value == "" else float(value)

    def _int_or_none(self, value: str):
        value = value.strip()
        return None if value == "" else int(float(value))

    def manual_payload(self) -> dict:
        role = ROLE_OPTIONS.get(self.calc_vars["role"].get().strip(), "ahmad")
        payload = bot.default_advisor_input()
        payload = bot.apply_price_config(payload, self.price_config)
        payload["my_role"] = role
        payload["round"] = self._int_or_none(self.calc_vars["round"].get()) or 1
        payload["maria_start_price"] = self._float_or_none(self.calc_vars["maria_start_price"].get())
        for key in ("total_all", "avg_grid_all", "total_grid_all", "wg_total", "count_white", "count_green"):
            payload[key] = self._float_or_none(self.calc_vars[key].get()) if key == "avg_grid_all" else self._int_or_none(self.calc_vars[key].get())
        payload["constraints"] = {
            "wg": {"count": self._int_or_none(self.calc_vars["wg_total"].get()), "grid": self._int_or_none(self.calc_vars["grid_wg"].get()), "avg": self._float_or_none(self.calc_vars["avg_wg"].get())},
            "blue": {"count": self._int_or_none(self.calc_vars["count_blue"].get()), "grid": self._int_or_none(self.calc_vars["grid_blue"].get()), "avg": self._float_or_none(self.calc_vars["avg_blue"].get())},
            "purple": {"count": self._int_or_none(self.calc_vars["count_purple"].get()), "grid": self._int_or_none(self.calc_vars["grid_purple"].get()), "avg": self._float_or_none(self.calc_vars["avg_purple"].get())},
            "gold": {"count": self._int_or_none(self.calc_vars["count_gold"].get()), "grid": self._int_or_none(self.calc_vars["grid_gold"].get()), "avg": self._float_or_none(self.calc_vars["avg_gold"].get())},
            "red": {"count": self._int_or_none(self.calc_vars["count_red"].get()), "grid": self._int_or_none(self.calc_vars["grid_red"].get()), "avg": self._float_or_none(self.calc_vars["avg_red"].get())},
        }
        for color in ("wg", "white", "green", "blue", "purple", "gold", "red"):
            for prefix in ("avg", "total"):
                key = f"{prefix}_price_{color}"
                payload[key] = self._float_or_none(self.calc_vars[key].get())
        return payload

    def run_manual_calculator(self) -> None:
        try:
            payload = self.manual_payload()
            result = evaluate(payload)
            report = render_report(payload, result)
        except Exception:
            report = traceback.format_exc()
        self.calc_result_text.delete("1.0", "end")
        self.calc_result_text.insert("end", report)

    def clear_manual_calculator(self) -> None:
        for key, var in self.calc_vars.items():
            var.set("1" if key == "round" else "")
        self.calc_vars["role"].set(self.role_var.get() or "艾哈迈德")

    def sync_calculator_from_config(self) -> None:
        advisor = self.config.get("advisor", {})
        self.calc_vars["role"].set(self.role_var.get() or ROLE_LABEL_BY_VALUE.get(advisor.get("role", "ahmad"), "艾哈迈德"))
        self.calc_vars["round"].set(str(advisor.get("round", 1)))
        green_count = advisor.get("green_count")
        white_count = advisor.get("white_count")
        self.calc_vars["count_green"].set("" if green_count in (None, "") else str(green_count))
        self.calc_vars["count_white"].set("" if white_count in (None, "") else str(white_count))
        self.calc_vars["wg_total"].set("")
        self.calc_vars["avg_wg"].set("")
        self.calc_vars["grid_wg"].set("")
        avg_grid_all = advisor.get("avg_grid_all")
        self.calc_vars["avg_grid_all"].set("" if avg_grid_all is None else str(avg_grid_all))

    def start_bot(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "脚本已经在运行中")
            return
        try:
            self.apply_form_to_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return

        self.stop_requested = False
        bot.reset_stop()
        self.start_btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])
        self.append_log("GUI start: bot thread launching")

        def runner():
            try:
                bot.run_loop(CONFIG_PATH)
            except bot.StopRequested:
                self.append_log("GUI stop: stopped")
            except Exception:
                self.append_log(traceback.format_exc())
            finally:
                self.root.after(0, self.on_worker_done)

        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()

    def stop_bot(self) -> None:
        bot.request_stop()
        self.stop_btn.state(["disabled"])
        self.append_log("GUI stop: requested")

    def on_worker_done(self) -> None:
        self.start_btn.state(["!disabled"])
        self.stop_btn.state(["disabled"])

    def on_close(self) -> None:
        bot.request_stop()
        bot.log = self.original_log
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    app = BidKingApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
