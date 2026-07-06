"""
dylorisk_gui_v2.py
════════════════════════════════════════════════════════════════════════════════
DyLoRISK GUI v2 — Idempotent Health Scoring + Root-Cause Reporting

NEW in v2 (over v1 dylorisk_gui.py)
────────────────────────────────────
  File list:
    • Shows per-file health score, last-seen warnings (up to 3), analysis time
    • "Clear History" button in FILE LIST card header
    • Column added: "Time (s)" for analysis duration
    • Cached results shown with a ⚡ prefix in Status column

  Health summary (right panel):
    • Aggregate / combined score across all analyzed files (weighted mean)
    • "Contributing Factors" sub-panel listing weighted root causes

  Analysis timing:
    • Each analysis records wall-clock duration, shown in tree + detail pane

  Root-cause panel:
    • After every analysis the detail pane appends a ROOT CAUSES section
    • Each cause: rank, factor name, metric, threshold, weight, evidence lines

  GUI layout: IDENTICAL to v1 — same geometry, colours, fonts, dark theme.
  All additions are additive; no existing widget is removed or repositioned.

DEPENDENCIES
────────────
  dylorisk_score_engine.py  (new deterministic engine)
  dylorisk_cache.py         (SHA-256 content-addressed cache)
  dylorisk.py               (original 11-stage pipeline for PDF/PNG generation)
  run_my_log.py             (Hadoop normaliser)
"""

import os
import sys
import json
import time
import threading
import traceback
import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

# ── Resolve package location ──────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from dylorisk import run_dylorisk
    from run_my_log import normalize_hadoop_line
    from dylorisk_score_engine import (
        score_log_file, compute_aggregate_score, score_result_to_dict,
        SCORE_FORMULA_STR,
        generate_human_summary,
    )
    from dylorisk_cache import ScoreCache, analyze_with_cache
except ImportError as _e:
    import tkinter as _tk
    _r = _tk.Tk(); _r.withdraw()
    _tk.messagebox.showerror(
        "Import Error",
        f"Missing module.\n\nEnsure dylorisk_score_engine.py, dylorisk_cache.py, "
        f"dylorisk.py and run_my_log.py are all in the same folder.\n\n{_e}"
    )
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────────────────
APP_DIR     = Path(_HERE)
REPORTS_DIR = APP_DIR / "reports" / "pdf"
PLOTS_DIR   = APP_DIR / "reports" / "plots"
INT_LOG     = APP_DIR / "dylorisk_internal.log"

REPORTS_DIR.mkdir(parents=True, exist_ok=True)
PLOTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Colours / fonts (unchanged dark industrial theme) ─────────────────────────
BG       = "#0f1117"
SURFACE  = "#1a1d27"
SURFACE2 = "#252836"
ACCENT   = "#4f8ef7"
ACCENT2  = "#7c6af7"
HEALTHY  = "#2ecc71"
WARNING  = "#f39c12"
CRITICAL = "#e74c3c"
TEXT     = "#e8eaf0"
MUTED    = "#6c7283"
BORDER   = "#2e3347"

FONT_MONO  = ("Courier New", 10)
FONT_BODY  = ("Segoe UI", 10)  if sys.platform == "win32" else ("Helvetica Neue", 10)
FONT_TITLE = ("Segoe UI", 13, "bold") if sys.platform == "win32" else ("Helvetica Neue", 13, "bold")
FONT_SMALL = ("Segoe UI", 9)   if sys.platform == "win32" else ("Helvetica Neue", 9)

LOG_EXTENSIONS = {".log", ".txt", ".out", ".gz", ".bz2"}

# ── Internal logger ────────────────────────────────────────────────────────────

def _ilog(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(INT_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

# ── Load / normalise helper ────────────────────────────────────────────────────

def _load_and_normalize(log_path: str) -> list:
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        raw = [l.rstrip("\n") for l in f]
    return [normalize_hadoop_line(l) for l in raw]


# ═════════════════════════════════════════════════════════════════════════════
# Main Application
# ═════════════════════════════════════════════════════════════════════════════

class DyLoRISKApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DyLoRISK — Log Risk Analyzer v2")
        self.configure(bg=BG)
        self.geometry("1150x760")
        self.minsize(960, 620)

        self._cache = ScoreCache(str(APP_DIR / ".dylorisk_cache_v2.json"))
        # In-memory store of full ScoreResult dicts keyed by content_hash
        self._results: dict = {}
        self._workers: list = []
        self._last_folder_summary = []

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_aggregate_panel()

    # ─────────────────────────────────────────────────────────────────────────
    # UI Construction (identical layout to v1 + additions)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._configure_styles()
        self._build_header()
        content = tk.Frame(self, bg=BG)
        content.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)
        self._build_left_panel(content)
        self._build_right_panel(content)
        self._build_status_bar()

    def _configure_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Dark.TFrame",        background=BG)
        s.configure("Surface.TFrame",     background=SURFACE)
        s.configure("Surface2.TFrame",    background=SURFACE2)
        s.configure("Dark.TLabel",        background=BG,       foreground=TEXT,  font=FONT_BODY)
        s.configure("Title.TLabel",       background=BG,       foreground=TEXT,  font=FONT_TITLE)
        s.configure("Muted.TLabel",       background=SURFACE,  foreground=MUTED, font=FONT_SMALL)
        s.configure("Surface.TLabel",     background=SURFACE,  foreground=TEXT,  font=FONT_BODY)
        s.configure("Surface2.TLabel",    background=SURFACE2, foreground=TEXT,  font=FONT_BODY)
        s.configure("Accent.TButton",
                    background=ACCENT,  foreground=TEXT,
                    font=("Segoe UI", 10, "bold") if sys.platform == "win32" else ("Helvetica Neue", 10, "bold"),
                    borderwidth=0, focuscolor="none", relief="flat")
        s.map("Accent.TButton",
              background=[("active", ACCENT2), ("pressed", ACCENT2)])
        s.configure("Secondary.TButton",
                    background=SURFACE2, foreground=TEXT,
                    font=FONT_BODY, borderwidth=0, focuscolor="none", relief="flat")
        s.map("Secondary.TButton",
              background=[("active", BORDER), ("pressed", BORDER)])
        s.configure("Treeview",
                    background=SURFACE, foreground=TEXT,
                    fieldbackground=SURFACE, borderwidth=0,
                    rowheight=26, font=FONT_SMALL)
        s.configure("Treeview.Heading",
                    background=SURFACE2, foreground=MUTED,
                    font=FONT_SMALL, borderwidth=0, relief="flat")
        s.map("Treeview", background=[("selected", ACCENT2)])

    def _build_header(self):
        hdr = tk.Frame(self, bg=SURFACE, height=56)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="⬡ DyLoRISK", bg=SURFACE, fg=ACCENT,
                 font=("Courier New", 15, "bold")).pack(side="left", padx=20, pady=10)
        tk.Label(hdr, text="Dynamic Log Risk & Instability State-Space Analyzer v2",
                 bg=SURFACE, fg=MUTED, font=FONT_SMALL).pack(side="left", padx=0, pady=10)
        tk.Button(hdr, text="📁 Reports", bg=SURFACE2, fg=TEXT,
                  font=FONT_SMALL, bd=0, padx=10, pady=5,
                  cursor="hand2", command=self._open_reports_folder
                  ).pack(side="right", padx=20, pady=10)

    def _build_left_panel(self, parent):
        left = tk.Frame(parent, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=0)
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)

        # ── Folder picker ─────────────────────────────────────────────────────
        folder_card = self._card(left, "AUTO ANALYSIS — Folder")
        folder_card.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self._folder_var = tk.StringVar()
        folder_row = tk.Frame(folder_card, bg=SURFACE)
        folder_row.pack(fill="x", padx=12, pady=(0, 10))

        entry = tk.Entry(folder_row, textvariable=self._folder_var,
                         bg=SURFACE2, fg=TEXT, insertbackground=TEXT,
                         bd=0, highlightthickness=1, highlightbackground=BORDER,
                         font=FONT_SMALL, relief="flat")
        entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 6))
        tk.Button(folder_row, text="Browse", bg=ACCENT, fg=TEXT,
                  font=FONT_SMALL, bd=0, padx=10, pady=4,
                  cursor="hand2", command=self._browse_folder).pack(side="right")

        self._new_only_var = tk.BooleanVar(value=True)
        tk.Checkbutton(folder_card, text="Process new files only",
                       variable=self._new_only_var,
                       bg=SURFACE, fg=TEXT, selectcolor=SURFACE2,
                       activebackground=SURFACE, activeforeground=TEXT,
                       font=FONT_SMALL, bd=0, cursor="hand2"
                       ).pack(anchor="w", padx=12, pady=(0, 4))

        tk.Button(folder_card, text="▶  Analyze Folder",
                  bg=ACCENT, fg=TEXT, font=FONT_BODY,
                  bd=0, padx=16, pady=7, cursor="hand2",
                  command=self._start_auto_analysis
                  ).pack(padx=12, pady=(4, 12), anchor="w")

        # ── Manual single-file ────────────────────────────────────────────────
        manual_card = self._card(left, "MANUAL ANALYSIS — Single File")
        manual_card.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        tk.Button(manual_card, text="⊕  Select Log File & Analyze",
                  bg=SURFACE2, fg=TEXT, font=FONT_BODY,
                  bd=0, padx=16, pady=7, cursor="hand2",
                  command=self._start_manual_analysis
                  ).pack(padx=12, pady=12, anchor="w")

        # ── File list ─────────────────────────────────────────────────────────
        list_card = self._card_with_action(left, "FILE LIST", "⌫ Clear History",
                                           self._clear_history)
        list_card.grid(row=2, column=0, sticky="nsew")
        list_card.pack_propagate(True)

        # Extended columns: file, status, score, analysis time
        cols = ("file", "status", "score", "secs")
        self._tree = ttk.Treeview(list_card, columns=cols, show="headings",
                                  selectmode="browse")
        self._tree.heading("file",   text="File")
        self._tree.heading("status", text="Status")
        self._tree.heading("score",  text="HS")
        self._tree.heading("secs",   text="Time(s)")
        self._tree.column("file",   width=150, stretch=True)
        self._tree.column("status", width=80,  stretch=False)
        self._tree.column("score",  width=42,  stretch=False, anchor="center")
        self._tree.column("secs",   width=56,  stretch=False, anchor="center")
        self._tree.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        vsb = ttk.Scrollbar(list_card, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._populate_tree_from_cache()

    def _build_right_panel(self, parent):
        right = tk.Frame(parent, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)

        # ── Health gauge row (unchanged from v1) ──────────────────────────────
        gauge_card = self._card(right, "HEALTH SCORE")
        gauge_card.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        gauge_inner = tk.Frame(gauge_card, bg=SURFACE)
        gauge_inner.pack(fill="x", padx=16, pady=(0, 14))

        self._hs_score = tk.Label(gauge_inner, text="--", bg=SURFACE,
                                  fg=MUTED, font=("Courier New", 42, "bold"))
        self._hs_score.pack(side="left")

        hs_right = tk.Frame(gauge_inner, bg=SURFACE)
        hs_right.pack(side="left", padx=16)
        self._hs_status = tk.Label(hs_right, text="No analysis yet",
                                   bg=SURFACE, fg=MUTED,
                                   font=("Courier New", 14, "bold"))
        self._hs_status.pack(anchor="w")
        self._hs_file = tk.Label(hs_right, text="",
                                 bg=SURFACE, fg=MUTED, font=FONT_SMALL)
        self._hs_file.pack(anchor="w")

        bar_frame = tk.Frame(gauge_inner, bg=SURFACE)
        bar_frame.pack(side="right", fill="x", expand=True)
        self._bar_labels = {}
        for k in ("Drift", "Escalation", "Density"):
            row = tk.Frame(bar_frame, bg=SURFACE)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{k:12s}", bg=SURFACE, fg=MUTED,
                     font=FONT_SMALL).pack(side="left")
            lbl = tk.Label(row, text="--", bg=SURFACE, fg=TEXT, font=FONT_SMALL)
            lbl.pack(side="left", padx=4)
            self._bar_labels[k] = lbl

        # ── NEW: Aggregate summary panel ──────────────────────────────────────
        agg_card = self._card(right, "AGGREGATE HEALTH SUMMARY")
        agg_card.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        agg_inner = tk.Frame(agg_card, bg=SURFACE)
        agg_inner.pack(fill="x", padx=16, pady=(0, 10))

        agg_top = tk.Frame(agg_inner, bg=SURFACE)
        agg_top.pack(fill="x")
        self._agg_score_lbl = tk.Label(agg_top, text="--", bg=SURFACE,
                                       fg=MUTED, font=("Courier New", 28, "bold"))
        self._agg_score_lbl.pack(side="left")
        agg_sub = tk.Frame(agg_top, bg=SURFACE)
        agg_sub.pack(side="left", padx=12)
        self._agg_status_lbl = tk.Label(agg_sub, text="No files analyzed",
                                        bg=SURFACE, fg=MUTED,
                                        font=("Courier New", 11, "bold"))
        self._agg_status_lbl.pack(anchor="w")
        self._agg_meta_lbl = tk.Label(agg_sub, text="",
                                      bg=SURFACE, fg=MUTED, font=FONT_SMALL)
        self._agg_meta_lbl.pack(anchor="w")

        # Contributing factors sub-frame
        cf_frame = tk.Frame(agg_inner, bg=SURFACE)
        cf_frame.pack(fill="x", pady=(6, 0))
        tk.Label(cf_frame, text="TOP CONTRIBUTING FACTORS", bg=SURFACE, fg=MUTED,
                 font=("Courier New", 7, "bold")).pack(anchor="w")
        self._cf_text = tk.Text(cf_frame, bg=SURFACE2, fg=TEXT,
                                font=FONT_SMALL, bd=0, height=4,
                                state="disabled", wrap="none",#no word wrap for better alignment
                                highlightthickness=0)
        self._cf_text.pack(fill="x")

        # ── Detail pane ───────────────────────────────────────────────────────
        detail_card = self._card(right, "ANALYSIS DETAIL")
        detail_card.grid(row=2, column=0, sticky="nsew")
        detail_card.pack_propagate(True)

        btn_row = tk.Frame(detail_card, bg=SURFACE)
        btn_row.pack(fill="x", padx=6, pady=(0, 4))

        tk.Button(
            btn_row,
            text="Folder Summary",
            bg=SURFACE2,
            fg=TEXT,
            font=FONT_SMALL,
            bd=0,
            padx=8,
            pady=3,
            cursor="hand2",
            command=self._restore_folder_summary
        ).pack(side="right")

        self._detail_text = tk.Text(
            detail_card, bg=SURFACE, fg=TEXT, bd=0,
            font=FONT_MONO, state="disabled", wrap="word",
            highlightthickness=0, insertbackground=TEXT,
        )
        self._detail_text.pack(fill="both", expand=True, padx=4, pady=4)
        vsb2 = ttk.Scrollbar(detail_card, orient="vertical",
                              command=self._detail_text.yview)
        self._detail_text.configure(yscrollcommand=vsb2.set)
        vsb2.pack(side="right", fill="y")
        
        hsb = ttk.Scrollbar(
            detail_card,
            orient="horizontal",
            command=self._detail_text.xview
        )
        self._detail_text.configure(
            xscrollcommand=hsb.set
        )

        hsb.pack(side="bottom", fill="x")
 
        

        # Text tags
        for tag, fg in [
            ("header",   ACCENT),  ("value",   TEXT),    ("muted",    MUTED),
            ("healthy",  HEALTHY), ("warning", WARNING), ("critical", CRITICAL),
        ]:
            self._detail_text.tag_configure(tag, foreground=fg)

    def _build_status_bar(self):
        bar = tk.Frame(self, bg=SURFACE2, height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self._status_var = tk.StringVar(value="Ready.")
        tk.Label(bar, textvariable=self._status_var,
                 bg=SURFACE2, fg=MUTED, font=FONT_SMALL,
                 anchor="w").pack(side="left", padx=12)
        self._progress = ttk.Progressbar(bar, mode="indeterminate", length=120)
        self._progress.pack(side="right", padx=12, pady=3)

    # ─────────────────────────────────────────────────────────────────────────
    # Widget helpers (unchanged from v1 except _card_with_action)
    # ─────────────────────────────────────────────────────────────────────────

    def _card(self, parent, title: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=SURFACE, bd=0, relief="flat",
                         highlightbackground=BORDER, highlightthickness=1)
        tk.Label(outer, text=title, bg=SURFACE, fg=MUTED,
                 font=("Courier New", 8, "bold"), anchor="w").pack(
            fill="x", padx=12, pady=(8, 4))
        return outer

    def _card_with_action(self, parent, title: str, action_label: str,
                          action_cmd) -> tk.Frame:
        """Card header with an inline action button on the right."""
        outer = tk.Frame(parent, bg=SURFACE, bd=0, relief="flat",
                         highlightbackground=BORDER, highlightthickness=1)
        hdr_row = tk.Frame(outer, bg=SURFACE)
        hdr_row.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(hdr_row, text=title, bg=SURFACE, fg=MUTED,
                 font=("Courier New", 8, "bold"), anchor="w").pack(side="left")
        tk.Button(hdr_row, text=action_label, bg=SURFACE2, fg=CRITICAL,
                  font=("Courier New", 7), bd=0, padx=6, pady=1,
                  cursor="hand2", command=action_cmd).pack(side="right")
        return outer

    # ─────────────────────────────────────────────────────────────────────────
    # Detail text helpers (unchanged)
    # ─────────────────────────────────────────────────────────────────────────

    def _write_detail(self, text: str, tag: str = ""):
        self._detail_text.configure(state="normal")
        self._detail_text.delete("1.0", "end")
        if tag:
            self._detail_text.insert("end", text, tag)
        else:
            self._detail_text.insert("end", text)
        self._detail_text.configure(state="disabled")

    def _append_detail(self, text: str, tag: str = ""):
        self._detail_text.configure(state="normal")
        if tag:
            self._detail_text.insert("end", text, tag)
        else:
            self._detail_text.insert("end", text)
        self._detail_text.see("end")
        self._detail_text.configure(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # Tree population
    # ─────────────────────────────────────────────────────────────────────────

    def _populate_tree_from_cache(self):
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        entries = sorted(
            [(k, v) for k, v in self._cache._data.items()
             if not k.startswith("_")],
            key=lambda x: x[1].get("_cached_at", 0), reverse=True
        )
        for chash, info in entries:
            if not isinstance(info, dict) or "health_score" not in info:
                continue
            fname  = info.get("file_name", chash[:12])
            status = info.get("status", "?")
            score  = info.get("health_score", "--")
            secs   = info.get("analysis_secs", "")
            secs_s = f"{secs:.1f}" if isinstance(secs, float) else str(secs)
            color_tag = self._status_tag(status)
            cached_status = f"⚡{status}"
            self._tree.insert("", "end", iid=chash,
                              values=(fname, cached_status, score, secs_s),
                              tags=(color_tag,))
        self._tree.tag_configure("healthy", foreground=HEALTHY)
        self._tree.tag_configure("warning", foreground=WARNING)
        self._tree.tag_configure("critical", foreground=CRITICAL)
        self._tree.tag_configure("unknown",  foreground=MUTED)

    def _add_tree_item(self, chash: str, fname: str, status: str,
                       score, secs: float, cached: bool = False):
        color_tag = self._status_tag(status)
        disp_status = ("⚡" if cached else "") + status
        secs_s = f"{secs:.1f}" if isinstance(secs, (int, float)) else "--"
        if self._tree.exists(chash):
            self._tree.item(chash,
                            values=(fname, disp_status, score, secs_s),
                            tags=(color_tag,))
        else:
            self._tree.insert("", 0, iid=chash,
                              values=(fname, disp_status, score, secs_s),
                              tags=(color_tag,))

    def _status_tag(self, status: str) -> str:
        s = str(status).upper().lstrip("⚡")
        if "HEALTHY"  in s: return "healthy"
        if "WARNING"  in s: return "warning"
        if "CRITICAL" in s: return "critical"
        return "unknown"

    # ─────────────────────────────────────────────────────────────────────────
    # Aggregate panel refresh
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_aggregate_panel(self):
        """Recompute and display the aggregate score from all cached results."""
        entries = [v for k, v in self._cache._data.items()
                   if isinstance(v, dict) and "health_score" in v
                   and not k.startswith("_")]

        if not entries:
            self._agg_score_lbl.configure(text="--", fg=MUTED)
            self._agg_status_lbl.configure(text="No files analyzed", fg=MUTED)
            self._agg_meta_lbl.configure(text="")
            self._cf_text.configure(state="normal")
            self._cf_text.delete("1.0", "end")
            self._cf_text.configure(state="disabled")
            return

        # Weighted mean
        total_ev = sum(e.get("total_events", 1) for e in entries)
        if total_ev == 0:
            total_ev = len(entries)
        agg = round(sum(
            e.get("health_score", 0) * e.get("total_events", 1)
            for e in entries
        ) / total_ev, 1)

        if agg > 80:   s, c = "HEALTHY",  HEALTHY
        elif agg >= 50: s, c = "WARNING",  WARNING
        else:           s, c = "CRITICAL", CRITICAL

        n_crit = sum(1 for e in entries if e.get("status") == "CRITICAL")
        n_warn = sum(1 for e in entries if e.get("status") == "WARNING")
        n_ok   = sum(1 for e in entries if e.get("status") == "HEALTHY")
        total_t = sum(e.get("analysis_secs", 0) for e in entries)

        self._agg_score_lbl.configure(text=str(agg), fg=c)
        self._agg_status_lbl.configure(text=s, fg=c)
        self._agg_meta_lbl.configure(
            text=f"{len(entries)} files  ·  ✅{n_ok}  ⚠️{n_warn}  🔴{n_crit}"
                 f"  ·  Total: {total_t:.1f}s"
        )

        # Contributing factors — aggregate root causes across all files
        factor_totals: dict = {}
        for e in entries:
            for rc in e.get("root_causes", []):
                key = rc.get("factor", "")
                w   = rc.get("weight", 0.0)
                factor_totals[key] = factor_totals.get(key, 0.0) + w

        self._cf_text.configure(state="normal")
        self._cf_text.delete("1.0", "end")
        if factor_totals:
            sorted_factors = sorted(factor_totals.items(),
                                    key=lambda x: x[1], reverse=True)
            for factor, total_w in sorted_factors[:5]:
                line = f"  • {factor:25s}  total penalty pts: {total_w:.1f}\n"
                self._cf_text.insert("end", line)
        else:
            self._cf_text.insert("end", "  No contributing factors above threshold.\n")
        self._cf_text.configure(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # Clear history
    # ─────────────────────────────────────────────────────────────────────────

    def _clear_history(self):
        if not messagebox.askyesno(
            "Clear History",
            "Remove all cached analysis results from the file list?\n\n"
            "This will not delete any PDF/PNG reports from disk."
        ):
            return
        n = self._cache.clear()
        self._results.clear()
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        self._refresh_aggregate_panel()
        self._set_status(f"Cleared {n} cached result(s).")
        self._write_detail(f"History cleared ({n} entries removed).\n", "muted")

    # ─────────────────────────────────────────────────────────────────────────
    # Folder / reports browsing
    # ─────────────────────────────────────────────────────────────────────────

    def _browse_folder(self):
        d = filedialog.askdirectory(title="Select Log Folder")
        if d:
            self._folder_var.set(d)

    def _open_reports_folder(self):
        import subprocess
        folder = str(REPORTS_DIR.parent)
        if sys.platform == "win32":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])

    # ─────────────────────────────────────────────────────────────────────────
    # Auto analysis
    # ─────────────────────────────────────────────────────────────────────────

    def _discover_logs(self, folder: str) -> list:
        results = []
        for root, _, files in os.walk(folder):
            for fn in sorted(files):
                if any(fn.lower().endswith(ext) for ext in LOG_EXTENSIONS):
                    results.append(os.path.join(root, fn))
        return results

    def _start_auto_analysis(self):
        folder = self._folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("DyLoRISK", "Please select a valid folder first.")
            return
        all_files = self._discover_logs(folder)
        if not all_files:
            messagebox.showinfo("DyLoRISK", "No log files found in the selected folder.")
            return
        new_only = self._new_only_var.get()
        if new_only:
            files = [f for f in all_files
                     if ScoreCache.hash_file(f) not in self._cache._data]
        else:
            files = all_files
        if not files:
            messagebox.showinfo("DyLoRISK",
                "All files already processed.\n\nUncheck 'Process new files only' to re-analyze.")
            return
        self._run_in_thread(self._auto_worker, files)

    def _auto_worker(self, files: list):
        total = len(files)
        self._set_status(f"Analyzing {total} file(s)…")
        self._start_spinner()
        summary_rows = []

        for i, log_path in enumerate(files, 1):
            fname = os.path.basename(log_path)
            stem  = Path(log_path).stem
            rpt   = str(REPORTS_DIR / f"{stem}_DyLoRISK_Report.pdf")
            png   = str(PLOTS_DIR   / f"{stem}_DyLoRISK_plots.png")
            self._set_status(f"[{i}/{total}] {fname}")
            try:
                r, was_cached = self._analyze_file(log_path, rpt, png)
                hs   = r.get("health_score")
                secs = r.get("analysis_secs", 0)
                self.after(0, self._add_tree_item,
                           r["content_hash"], fname,
                           r.get("status", "?"), hs, secs, was_cached)
                summary_rows.append((fname, r, was_cached))
            except Exception as exc:
                _ilog(f"ERROR {log_path}: {traceback.format_exc()}")
                summary_rows.append((fname, None, False))

        self._stop_spinner()
        self._set_status(f"Done — {total} file(s) processed.")
        self.after(0, self._show_auto_summary, summary_rows)
        self.after(0, self._refresh_aggregate_panel)

    def _analyze_file(self, log_path: str, rpt: str, png: str) -> tuple:
        """Run analysis (or return from cache). Returns (result_dict, was_cached)."""
        lines = _load_and_normalize(log_path)
        chash = ScoreCache.hash_lines(lines)

        cached = self._cache.get(chash)
        if cached is not None:
            return cached, True

        # Run PDF/PNG generation through original pipeline
        t0 = time.time()
        try:
            run_dylorisk(raw_lines=lines, report_path=rpt, plots_path=png)
        except Exception:
            pass  # score engine runs independently

        result = score_log_file(
            file_path=log_path,
            raw_lines=lines,
            report_pdf=rpt,
            plots_png=png,
            _t_start=t0,
        )
        from dylorisk_score_engine import score_result_to_dict
        rd = score_result_to_dict(result)
        self._cache.put(chash, rd, force=False)
        return rd, False


    
    def _show_auto_summary(self, rows):

        self._detail_text.configure(state="normal")
        self._detail_text.delete("1.0", "end")

        summary_buffer = []

        def add_line(text="", tag=None):

            # Always append newline automatically
            if not text.endswith("\n"):
                text += "\n"

            summary_buffer.append((text, tag))

            if tag:
                self._detail_text.insert("end", text, tag)
            else:
                self._detail_text.insert("end", text)

        # ─────────────────────────────────────────────
        # HEADER
        # ─────────────────────────────────────────────

        add_line("═" * 100, "header")
        add_line("                 FOLDER ANALYSIS SUMMARY", "header")
        add_line("═" * 100, "header")
        add_line()

        # ─────────────────────────────────────────────
        # FILES
        # ─────────────────────────────────────────────

        for fname, r, cached in rows:
            if r is None:
                add_line(f"✗ {fname}", "critical")
                add_line()
                continue

            score = r.get("health_score", 0)
            status = r.get("status", "UNKNOWN")

            tag = self._status_tag(status)

            # File separator
            add_line("─" * 100, "muted")

            # File header
            header_line = (
                f"{fname:<65}"
                f"Score: {score:>6}/100   "
                f"[{status}]"
            )

            add_line(header_line, tag)

            add_line("─" * 100, "muted")

            events = r.get("drift_events", [])

            # No drift
            if not events:

                add_line(
                    "  • No major drift events detected",
                    "healthy"
                )

                add_line()
                continue

            # Drift windows
            for block in events:

                drift_header = (
                    f"  • Drift Window {block['window']:>2}    "
                    f"Drift Score = {block['drift']:<8}"
                )

                add_line(drift_header, "critical")

                for ev in block["events"]:
                    color = "warning"

                    if (
                        "ERROR" in ev
                        or "WARN" in ev
                        or "CRITICAL" in ev
                        or "FATAL" in ev
                    ):
                        color = "critical"

                    clean_ev = ev.strip()

                    # Prevent ugly overflow
                    if len(clean_ev) > 140:
                        clean_ev = clean_ev[:140] + "..."

                    add_line(
                        f"      → {clean_ev}",
                        color
                    )

            add_line()

        # Extra spacing between files
        add_line()

        try:
            self._detail_text.configure(state="disabled")
            self._last_folder_summary = summary_buffer
        except NameError:
            # running in a context without `self` (e.g., during import/tests)
            pass

    def _restore_folder_summary(self):

        if not self._last_folder_summary:

            self._write_detail(
                "No folder summary available yet.",
                "muted"
            )

            return

        self._detail_text.configure(state="normal")
        self._detail_text.delete("1.0", "end")

        for text, tag in self._last_folder_summary:

            if tag:
                self._detail_text.insert("end", text, tag)
            else:
                self._detail_text.insert("end", text)

        self._detail_text.configure(state="disabled")


    # ─────────────────────────────────────────────────────────────────────────
    # Manual analysis
    # ─────────────────────────────────────────────────────────────────────────

    def _start_manual_analysis(self):
        log_path = filedialog.askopenfilename(
            title="Select Log File",
            filetypes=[("Log files", "*.log *.txt *.out"), ("All files", "*.*")],
        )
        if not log_path:
            return
        self._run_in_thread(self._manual_worker, log_path)

    def _manual_worker(self, log_path: str):
        fname = os.path.basename(log_path)
        stem  = Path(log_path).stem
        rpt   = str(REPORTS_DIR / f"{stem}_DyLoRISK_Report.pdf")
        png   = str(PLOTS_DIR   / f"{stem}_DyLoRISK_plots.png")

        self._set_status(f"Analyzing {fname}…")
        self._start_spinner()
        self._write_detail(f"Analyzing  {fname} …\n", "muted")

        try:
            r, was_cached = self._analyze_file(log_path, rpt, png)
            hs    = r.get("health_score", 0)
            secs  = r.get("analysis_secs", 0)
            self.after(0, self._add_tree_item,
                       r["content_hash"], fname,
                       r.get("status","?"), hs, secs, was_cached)
            self.after(0, self._update_gauge, r, fname)
            self.after(0, self._show_manual_detail, fname, r, was_cached)
            self.after(0, self._refresh_aggregate_panel)
        except Exception as exc:
            _ilog(f"ERROR {log_path}: {traceback.format_exc()}")
            self._write_detail(f"Analysis failed:\n{exc}\n\nCheck {INT_LOG}.", "critical")
        finally:
            self._stop_spinner()
            self._set_status("Ready.")

    def _update_gauge(self, r: dict, fname: str):
        score  = r.get("health_score", "--")
        status = r.get("status", "?")
        color  = {"HEALTHY": HEALTHY, "WARNING": WARNING, "CRITICAL": CRITICAL}.get(
            status.upper(), TEXT)
        self._hs_score.configure(text=str(score), fg=color)
        self._hs_status.configure(text=status, fg=color)
        self._hs_file.configure(text=fname)
        self._bar_labels["Drift"].configure(text=f"{r.get('d_avg',0):.4f}")
        self._bar_labels["Escalation"].configure(text=f"{r.get('g_norm',0):.4f}")
        self._bar_labels["Density"].configure(text=f"{r.get('graph_density',0):.4f}")

    def _show_manual_detail(self, fname: str, r: dict, was_cached: bool):
        status = r.get("status", "?")
        tag    = self._status_tag(status)
        secs   = r.get("analysis_secs", 0)
        cached_note = " (loaded from cache)" if was_cached else ""

        self._write_detail("")
        sep = "─" * 54 + "\n"
        self._append_detail("╔" + "═" * 54 + "╗\n", "header")
        self._append_detail(f"║  ANALYSIS REPORT{cached_note:<37s}║\n", "header")
        self._append_detail("╚" + "═" * 54 + "╝\n\n", "header")

        self._append_detail("FILE\n", "header")
        self._append_detail(f"  {fname}\n", "value")
        self._append_detail(f"  Hash: {r.get('content_hash','')[:16]}…\n\n", "muted")

        self._append_detail("HEALTH SCORE\n", "header")
        self._append_detail(f"  {r.get('health_score','--')} / 100", "value")
        self._append_detail(f"  ← {status}\n", tag)
        self._append_detail(f"  Formula: {r.get('score_formula','')}\n\n", "muted")

        self._append_detail("ANALYSIS TIMING\n", "header")
        self._append_detail(f"  Wall-clock time : {secs:.3f}s{cached_note}\n\n", "value")

        self._append_detail("KEY METRICS\n", "header")
        self._append_detail(f"  Total events      : {r.get('total_events',0):>8,}\n", "value")
        self._append_detail(f"  ERROR / FATAL     : {r.get('error_count',0):>8,}\n", "value")
        self._append_detail(f"  WARNING           : {r.get('warn_count',0):>8,}\n", "value")
        self._append_detail(f"  INFO              : {r.get('info_count',0):>8,}\n", "value")
        self._append_detail(f"  Mean drift (d̄)   : {r.get('d_avg',0):.4f}\n", "value")
        self._append_detail(f"  Escalation ḡ_norm : {r.get('g_norm',0):.4f}\n", "value")
        self._append_detail(f"  Graph nodes|edges : "
                            f"{r.get('graph_nodes',0)} | {r.get('graph_edges',0)}\n", "value")
        self._append_detail(f"  Graph density ρ   : {r.get('graph_density',0):.4f}\n\n", "value")

        # Top error templates
        top5 = r.get("top5_error_templates", {})
        if top5:
            self._append_detail("TOP ERROR TEMPLATES\n", "header")
            for i, (tmpl, cnt) in enumerate(list(top5.items())[:5], 1):
                short = (tmpl[:48] + "…") if len(tmpl) > 50 else tmpl
                self._append_detail(f"  {i}. [{cnt:>3}]  {short}\n", "critical")
            self._append_detail("\n")

        # ── ROOT CAUSES ────────────────────────────────────────────────────
        root_causes = r.get("root_causes", [])
        self._append_detail("ROOT CAUSES\n", "header")
        if not root_causes:
            self._append_detail("  No significant contributing factors above threshold.\n\n",
                                "healthy")
        else:
            for rc in root_causes:
                rank   = rc.get("rank", 0)
                factor = rc.get("factor", "")
                metric = rc.get("metric_name", "")
                val    = rc.get("metric_value", 0)
                thr    = rc.get("threshold", 0)
                wt     = rc.get("weight", 0)
                desc   = rc.get("description", "")
                ev     = rc.get("evidence", [])

                rc_tag = "critical" if wt >= 10 else "warning" if wt >= 3 else "value"
                self._append_detail(f"\n  #{rank}  {factor}\n", rc_tag)
                self._append_detail(f"      {metric}={val}  threshold={thr}"
                                    f"  score penalty={wt:.1f}pts\n", "muted")
                self._append_detail(f"      {desc}\n", "value")
                if ev:
                    self._append_detail("      Evidence:\n", "muted")
                    for e_line in ev[:3]:
                        self._append_detail(f"        → {e_line}\n", "muted")

        # INTERPRETATION (unchanged from v1)
        self._append_detail("\nINTERPRETATION\n", "header")
        d = r.get("d_avg", 0)
        if d < 0.10:
            self._append_detail("  Drift: Stable — event distributions consistent.\n", "healthy")
        elif d < 0.38:
            self._append_detail("  Drift: Elevated — non-trivial distributional shift.\n", "warning")
        else:
            self._append_detail("  Drift: CRITICAL — exceeds pre-anomaly threshold (0.38).\n", "critical")

        g = r.get("g_norm", 0)
        if g > 0.2:
            self._append_detail("  Latency: HIGH gradient — accelerating slowdown.\n", "critical")
        elif g > 0.05:
            self._append_detail("  Latency: Moderate escalation — growing execution time.\n", "warning")
        else:
            self._append_detail("  Latency: Low gradient — no significant escalation.\n", "healthy")

        self._append_detail(f"\n  PDF  : {r.get('report_pdf','')}\n", "muted")
        self._append_detail(f"  PNG  : {r.get('plots_png','')}\n",  "muted")

    # ─────────────────────────────────────────────────────────────────────────
    # Tree selection
    # ─────────────────────────────────────────────────────────────────────────

    def _on_tree_select(self, event):
        sel = self._tree.selection()
        if not sel:
            return
        chash = sel[0]
        info  = self._cache.get(chash)
        if not info:
            return
        fname  = info.get("file_name", "")
        status = info.get("status", "?")
        score  = info.get("health_score", "--")
        color  = {"HEALTHY": HEALTHY, "WARNING": WARNING, "CRITICAL": CRITICAL}.get(
            str(status).upper(), MUTED)
        self._hs_score.configure(text=str(score), fg=color)
        self._hs_status.configure(text=str(status), fg=color)
        self._hs_file.configure(text=fname)
        # Show stored detail
        self._show_manual_detail(fname, info, was_cached=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Threading helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _run_in_thread(self, fn, *args):
        t = threading.Thread(target=fn, args=args, daemon=True)
        self._workers.append(t)
        t.start()

    def _set_status(self, msg: str):
        self.after(0, lambda: self._status_var.set(msg))

    def _start_spinner(self):
        self.after(0, self._progress.start, 12)

    def _stop_spinner(self):
        self.after(0, self._progress.stop)

    def _on_close(self):
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = DyLoRISKApp()
    app.mainloop()
