"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         DyLoRISK — Dynamic Log Risk and Instability State-Space Framework   ║
║         Complete Implementation: All 11 Stages                              ║
║         Non-Deep-Learning • Interpretable • Production-Ready                ║
╚══════════════════════════════════════════════════════════════════════════════╝

Reference: "DyLoRISK: Dynamic Log Risk and Instability State-Space Knowledge
Framework for Continuous System Stability Modeling" — Vignesh T et al., 2025.

Pipeline:
  Stage 1  → Log Parsing          (regex template extraction)
  Stage 2  → Window Segmentation  (W = max(5, ⌊N/20⌋))
  Stage 3  → Log State Modeling   (LSD vectors)
  Stage 4  → Behavioral Drift     (cosine distance + cumulative risk)
  Stage 5  → Entropy Analysis     (Shannon entropy per window)
  Stage 6  → Latency & Escalation (gradient analysis)
  Stage 7  → Graph Analysis       (Log Structure Graph, centrality, density)
  Stage 8  → Health Score         (HS = max(0, 100 − 50·d̄ − 10·ḡ_norm))
  Stage 9  → Visualizations       (4 diagnostic plots)
  Stage 10 → PDF Report           (multi-page diagnostic report)
  Stage 11 → Results & Discussion (interpretations)
"""

# ─────────────────────────────────────────────────────────────────────────────
# Imports
# ─────────────────────────────────────────────────────────────────────────────
import re
import math
import random
import datetime
import warnings
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.backends.backend_pdf import PdfPages
import networkx as nx
from scipy.stats import entropy as scipy_entropy
from sklearn.metrics.pairwise import cosine_similarity

matplotlib.use("Agg")
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1 — LOG PARSING
# ─────────────────────────────────────────────────────────────────────────────

def generate_sample_logs(n_events: int = 200, seed: int = 42) -> list[str]:
    """
    Generate a realistic synthetic log stream that models:
      • Normal operation (windows 0–8)
      • Onset of instability  (windows 9–13)
      • Failure propagation   (windows 14–19)

    Each line: ISO-timestamp  LEVEL  EventTemplate <optional numeric tokens>
    """
    rng = random.Random(seed)

    templates_normal = [
        "User {id} logged in from IP {ip}",
        "Request {id} processed in {ms} ms",
        "Cache hit for key {key}",
        "Database query executed in {ms} ms",
        "Health check passed for service {svc}",
        "Config loaded successfully version {ver}",
        "Session {id} established",
        "Batch job {id} completed successfully",
    ]
    templates_warn = [
        "High memory usage detected: {pct} percent",
        "Slow query warning: {ms} ms exceeded threshold",
        "Retry attempt {n} for service {svc}",
        "Connection pool exhausted: {n} waiting",
        "CPU usage spike: {pct} percent",
    ]
    templates_error = [
        "Service {svc} unavailable — connection refused",
        "Database connection timeout after {ms} ms",
        "Disk I/O error on partition {part}",
        "Out-of-memory error in process {pid}",
        "Critical: replication lag {ms} ms",
        "Null pointer exception in module {mod}",
    ]

    def fill(template: str) -> str:
        return re.sub(
            r"\{[^}]+\}",
            lambda _: str(rng.randint(1, 999)),
            template,
        )

    logs = []
    t = datetime.datetime(2024, 1, 1, 8, 0, 0)

    # Phase 1 — normal (60 % of events)
    phase1 = int(n_events * 0.60)
    for _ in range(phase1):
        t += datetime.timedelta(seconds=rng.uniform(0.2, 1.5))
        tmpl = rng.choice(templates_normal)
        level = "INFO"
        logs.append(f"{t.strftime('%Y-%m-%d %H:%M:%S')} {level} {fill(tmpl)}")

    # Phase 2 — degradation onset (25 %)
    phase2 = int(n_events * 0.25)
    for _ in range(phase2):
        t += datetime.timedelta(seconds=rng.uniform(1.0, 5.0))
        pool = templates_warn + templates_normal[:3]
        tmpl = rng.choice(pool)
        level = "WARN" if tmpl in templates_warn else "INFO"
        logs.append(f"{t.strftime('%Y-%m-%d %H:%M:%S')} {level} {fill(tmpl)}")

    # Phase 3 — failure storm (15 %)
    phase3 = n_events - phase1 - phase2
    for _ in range(phase3):
        t += datetime.timedelta(seconds=rng.uniform(3.0, 12.0))
        pool = templates_error + templates_warn
        tmpl = rng.choice(pool)
        level = "ERROR" if tmpl in templates_error else "WARN"
        logs.append(f"{t.strftime('%Y-%m-%d %H:%M:%S')} {level} {fill(tmpl)}")

    return logs


def templatize(line: str) -> str:
    """Replace numeric tokens (integers, floats, IPs) with the placeholder <*>."""
    # Replace IP-like patterns first
    line = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<*>", line)
    # Replace standalone numbers (with optional leading/trailing punctuation)
    line = re.sub(r"\b\d+(\.\d+)?\b", "<*>", line)
    # Collapse multiple spaces
    line = re.sub(r" +", " ", line).strip()
    return line


def parse_logs(raw_lines: list[str]) -> pd.DataFrame:
    """
    Stage 1 — Parse raw log lines into a structured DataFrame.

    Returns
    -------
    DataFrame with columns:
        EventId        : E1, E2, … (assigned by first-seen template order)
        Timestamp      : Unix float (seconds)
        EventTemplate  : normalised template string
        RawLine        : original log line
    """
    def parse_timestamp(line: str) -> tuple[float, str] | None:
        line = line.strip()
        if not line:
            return None

        iso_match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)", line)
        if iso_match:
            ts_str, body = iso_match.groups()
            try:
                dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
            return dt.timestamp(), body.strip()

        hdfs_short_match = re.match(
            r"^(\d{2})(\d{2})(\d{2})\s+"  # yymmdd
            r"(\d{2})(\d{2})(\d{2})\s+"  # HHMMSS
            r"\d+\s+"                      # ms / counter
            r"(.*)$",
            line,
        )
        if hdfs_short_match:
            yy, mm, dd, hh, mi, ss, rest = hdfs_short_match.groups()
            try:
                date = datetime.datetime.strptime(f"{yy}{mm}{dd}", "%y%m%d").date()
                dt = datetime.datetime.combine(
                    date,
                    datetime.time(int(hh), int(mi), int(ss)),
                )
            except ValueError:
                return None
            return dt.timestamp(), rest.strip()

        return None

    template_to_id: dict[str, str] = {}
    counter = 0
    records = []

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        parsed = parse_timestamp(line)
        if parsed is None:
            continue
        ts_unix, body = parsed

        template = templatize(body)
        if template not in template_to_id:
            counter += 1
            template_to_id[template] = f"E{counter}"
        eid = template_to_id[template]
        records.append({
            "EventId": eid,
            "Timestamp": ts_unix,
            "EventTemplate": template,
            "RawLine": line,
        })

    df = pd.DataFrame(records)
    if df.empty:
        raise ValueError("No valid timestamped events were parsed from the log input.")
    df.sort_values("Timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 2 — WINDOW SEGMENTATION
# ─────────────────────────────────────────────────────────────────────────────

def compute_window_size(n_events: int) -> int:
    """W = max(5, ⌊N / 20⌋)"""
    return max(5, int(math.floor(n_events / 20)))


def segment_windows(df: pd.DataFrame, window_size: int) -> list[pd.DataFrame]:
    """
    Stage 2 — Partition the parsed log DataFrame into non-overlapping
    windows of `window_size` rows.  Windows with fewer than 2 events
    are discarded (as specified by the DyLoRISK paper).
    """
    windows = []
    for start in range(0, len(df), window_size):
        chunk = df.iloc[start: start + window_size].copy()
        if len(chunk) >= 2:
            windows.append(chunk)
    return windows


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 3 — LOG STATE MODELING (LSD)
# ─────────────────────────────────────────────────────────────────────────────

def build_lsd_vectors(
    windows: list[pd.DataFrame],
    all_event_ids: list[str],
) -> np.ndarray:
    """
    Stage 3 — Build the Log State Distribution (LSD) matrix.

    LSD(t) = [c_1(t), c_2(t), ..., c_K(t)]

    where c_k(t) is the raw count of event template e_k inside window W_t.

    Returns
    -------
    lsd_matrix : shape (T, K)  — T windows, K event types
    """
    k = len(all_event_ids)
    idx_map = {eid: i for i, eid in enumerate(all_event_ids)}
    lsd = np.zeros((len(windows), k), dtype=float)

    for t, window in enumerate(windows):
        counts = Counter(window["EventId"].tolist())
        for eid, cnt in counts.items():
            if eid in idx_map:
                lsd[t, idx_map[eid]] = cnt

    return lsd


def compute_window_latency(windows: list[pd.DataFrame]) -> np.ndarray:
    """
    Stage 3 (helper) — Compute per-window execution latency.

    Lat(t) = τ_last(t) − τ_first(t)  clamped to ≥ 0
    """
    latencies = []
    for window in windows:
        ts = window["Timestamp"].values
        lat = max(0.0, float(ts[-1] - ts[0]))
        latencies.append(lat)
    return np.array(latencies)


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 4 — BEHAVIORAL DRIFT  (cosine distance + cumulative risk)
# ─────────────────────────────────────────────────────────────────────────────

def compute_cosine_drift(lsd_matrix: np.ndarray) -> np.ndarray:
    """
    Stage 4 — Cosine-distance drift between consecutive LSD vectors.

        d(t) = 1 − ( LSD(t) · LSD(t-1) ) / ( ‖LSD(t)‖ · ‖LSD(t-1)‖ )

    Edge cases:
      • d(0) = 0.0  (no previous window)
      • Zero-norm vectors → d(t) = 0.0
    """
    T = lsd_matrix.shape[0]
    drift = np.zeros(T, dtype=float)

    for t in range(1, T):
        v_curr = lsd_matrix[t].reshape(1, -1)
        v_prev = lsd_matrix[t - 1].reshape(1, -1)

        norm_curr = np.linalg.norm(v_curr)
        norm_prev = np.linalg.norm(v_prev)

        if norm_curr == 0.0 or norm_prev == 0.0:
            drift[t] = 0.0
        else:
            sim = cosine_similarity(v_curr, v_prev)[0, 0]
            # Clamp numerical errors to [0, 1]
            drift[t] = max(0.0, min(1.0, 1.0 - float(sim)))

    return drift


def compute_cumulative_risk(drift: np.ndarray) -> np.ndarray:
    """
    Stage 4 — Cumulative risk trajectory.

        Risk(t) = Σ_{i=1}^{t} d(i)

    Monotonically non-decreasing; captures total behavioural deviation
    accumulated since monitoring began.
    """
    return np.cumsum(drift)


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 5 — SHANNON ENTROPY
# ─────────────────────────────────────────────────────────────────────────────

def compute_entropy(lsd_matrix: np.ndarray) -> np.ndarray:
    """
    Stage 5 — Per-window Shannon entropy of the event distribution.

        H(t) = − Σ_k  p_k(t) · log( p_k(t) )
        where p_k(t) = c_k(t) / Σ_j c_j(t)

    Uses natural logarithm base (scipy default); clip to 0 for degenerate
    windows.
    """
    T = lsd_matrix.shape[0]
    entropies = np.zeros(T, dtype=float)
    for t in range(T):
        row = lsd_matrix[t]
        total = row.sum()
        if total == 0.0:
            entropies[t] = 0.0
        else:
            pk = row / total
            # scipy_entropy handles zero probabilities safely (0·log0 → 0)
            entropies[t] = float(scipy_entropy(pk))
    return entropies


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 6 — LATENCY GRADIENT & ESCALATION
# ─────────────────────────────────────────────────────────────────────────────

def compute_latency_gradient(latencies: np.ndarray) -> np.ndarray:
    """
    Stage 6 — Central-difference numerical gradient of the latency sequence.

        g(t) = ( Lat(t+1) − Lat(t−1) ) / 2

    Boundary windows use forward / backward differences (numpy.gradient).
    A consistently positive g(t) indicates accelerating execution slowdown.
    """
    return np.gradient(latencies)


def compute_escalation_score(latencies: np.ndarray, gradients: np.ndarray) -> float:
    """
    Stage 6 — Normalised mean absolute latency gradient.

        ḡ_norm = mean( |g(t)| ) / mean( Lat )

    Quantifies relative latency growth independent of absolute scale.
    Returns 0.0 if mean latency is zero.
    """
    mean_lat = float(np.mean(latencies))
    if mean_lat == 0.0:
        return 0.0
    mean_abs_grad = float(np.mean(np.abs(gradients)))
    return mean_abs_grad / mean_lat


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 7 — LOG STRUCTURE GRAPH ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def build_log_structure_graph(df: pd.DataFrame) -> nx.DiGraph:
    """
    Stage 7 — Directed weighted Log Structure Graph G = (V, E).

    Nodes  : distinct event templates (EventId labels)
    Edges  : directed transition e_i → e_j for consecutive events;
             weight = co-occurrence count of that transition.
    """
    G = nx.DiGraph()
    events = df["EventId"].tolist()

    for i in range(len(events) - 1):
        src, dst = events[i], events[i + 1]
        if G.has_edge(src, dst):
            G[src][dst]["weight"] += 1
        else:
            G.add_edge(src, dst, weight=1)

    return G


def compute_graph_metrics(G: nx.DiGraph) -> dict:
    """
    Stage 7 — Compute structural graph metrics.

    Returns
    -------
    dict with:
      degree_centrality  : {node: CD(v)} for all nodes
      graph_density      : ρ = |E| / ( |V|·(|V|−1) )
      top_central_nodes  : top-3 nodes by degree centrality
      n_nodes            : |V|
      n_edges            : |E|
    """
    if G.number_of_nodes() == 0:
        return {
            "degree_centrality": {},
            "graph_density": 0.0,
            "top_central_nodes": [],
            "n_nodes": 0,
            "n_edges": 0,
        }

    deg_centrality = nx.degree_centrality(G)
    density = nx.density(G)
    top_nodes = sorted(deg_centrality, key=deg_centrality.get, reverse=True)[:3]

    return {
        "degree_centrality": deg_centrality,
        "graph_density": density,
        "top_central_nodes": top_nodes,
        "n_nodes": G.number_of_nodes(),
        "n_edges": G.number_of_edges(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 8 — HEALTH SCORE
# ─────────────────────────────────────────────────────────────────────────────

def compute_health_score(drift: np.ndarray, g_norm: float) -> dict:
    """
    Stage 8 — Composite system health score.

        HS = max( 0,  100 − 50·d̄ − 10·ḡ_norm )

    Classification:
        > 80  →  Healthy
        50–80 →  Warning
        < 50  →  Critical
    """
    d_avg = float(np.mean(drift))
    hs = max(0.0, 100.0 - 50.0 * d_avg - 10.0 * g_norm)
    hs = round(hs, 2)

    if hs > 80:
        status = "HEALTHY"
        color  = "#2ecc71"
    elif hs >= 50:
        status = "WARNING"
        color  = "#f39c12"
    else:
        status = "CRITICAL"
        color  = "#e74c3c"

    return {
        "health_score": hs,
        "status": status,
        "color": color,
        "d_avg": round(d_avg, 4),
        "g_norm": round(g_norm, 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 9 — VISUALIZATIONS
# ─────────────────────────────────────────────────────────────────────────────

COLORS = {
    "drift":    "#e74c3c",
    "risk":     "#8e44ad",
    "entropy":  "#2980b9",
    "latency":  "#27ae60",
    "gradient": "#f39c12",
    "bg":       "#fafafa",
    "grid":     "#e0e0e0",
}


def _style_axis(ax, title: str, xlabel: str, ylabel: str):
    ax.set_title(title, fontsize=13, fontweight="bold", pad=10)
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.set_facecolor(COLORS["bg"])
    ax.grid(True, color=COLORS["grid"], linewidth=0.7, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def plot_drift(drift: np.ndarray, ax=None):
    """Stage 9 — Plot 1: Per-window cosine drift."""
    if ax is None:
        _, ax = plt.subplots(figsize=(9, 3.5))
    windows_idx = np.arange(len(drift))
    ax.plot(windows_idx, drift, color=COLORS["drift"], linewidth=2, marker="o",
            markersize=4, label="Cosine Drift d(t)")
    ax.axhline(y=0.10, color="#e67e22", linewidth=1.2, linestyle="--",
               label="Normal Threshold (0.10)")
    ax.axhline(y=0.38, color=COLORS["drift"], linewidth=1.2, linestyle=":",
               label="Pre-anomaly Level (0.38)")
    ax.fill_between(windows_idx, 0, drift, alpha=0.12, color=COLORS["drift"])
    _style_axis(ax,
                "Behavioral Drift — Cosine Distance d(t)",
                "Window Index", "Cosine Distance")
    ax.legend(fontsize=8)
    return ax


def plot_risk_trajectory(risk: np.ndarray, ax=None):
    """Stage 9 — Plot 2: Cumulative risk trajectory."""
    if ax is None:
        _, ax = plt.subplots(figsize=(9, 3.5))
    windows_idx = np.arange(len(risk))
    ax.plot(windows_idx, risk, color=COLORS["risk"], linewidth=2.5,
            label="Cumulative Risk(t)")
    ax.fill_between(windows_idx, 0, risk, alpha=0.15, color=COLORS["risk"])
    # Mark "knee point" region — last 30 % of windows
    knee = int(0.70 * len(risk))
    ax.axvspan(knee, len(risk) - 1, alpha=0.08, color=COLORS["drift"],
               label="Accelerated Degradation Zone")
    _style_axis(ax,
                "Cumulative Risk Trajectory Risk(t) = Σ d(i)",
                "Window Index", "Cumulative Risk")
    ax.legend(fontsize=8)
    return ax


def plot_entropy(entropies: np.ndarray, ax=None):
    """Stage 9 — Plot 3: Shannon entropy per window."""
    if ax is None:
        _, ax = plt.subplots(figsize=(9, 3.5))
    windows_idx = np.arange(len(entropies))
    ax.bar(windows_idx, entropies, color=COLORS["entropy"], alpha=0.75,
           edgecolor="white", linewidth=0.5, label="H(t)")
    mean_h = np.mean(entropies)
    ax.axhline(y=mean_h, color="#c0392b", linewidth=1.5, linestyle="--",
               label=f"Mean H = {mean_h:.3f}")
    _style_axis(ax,
                "Shannon Entropy H(t) — Event Diversity per Window",
                "Window Index", "Entropy (nats)")
    ax.legend(fontsize=8)
    return ax


def plot_latency(latencies: np.ndarray, gradients: np.ndarray, ax=None):
    """Stage 9 — Plot 4: Window latency + escalation gradient."""
    if ax is None:
        _, ax = plt.subplots(figsize=(9, 3.5))
    windows_idx = np.arange(len(latencies))
    ax2 = ax.twinx()

    ax.plot(windows_idx, latencies, color=COLORS["latency"], linewidth=2,
            marker="s", markersize=3, label="Lat(t) [s]")
    ax.fill_between(windows_idx, 0, latencies, alpha=0.10, color=COLORS["latency"])

    ax2.bar(windows_idx, gradients, color=COLORS["gradient"], alpha=0.5,
            label="Gradient g(t)")
    ax2.axhline(y=0, color="#7f8c8d", linewidth=0.8, linestyle="-")

    _style_axis(ax,
                "Window Latency and Escalation Gradient",
                "Window Index", "Latency (seconds)")
    ax2.set_ylabel("Latency Gradient g(t)", fontsize=10)
    ax2.spines["top"].set_visible(False)

    lines1, labs1 = ax.get_legend_handles_labels()
    lines2, labs2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labs1 + labs2, fontsize=8)
    return ax


def generate_all_plots(
    drift: np.ndarray,
    risk: np.ndarray,
    entropies: np.ndarray,
    latencies: np.ndarray,
    gradients: np.ndarray,
    save_path: str = "dylorisk_plots.png",
) -> str:
    """
    Stage 9 — Combine all four diagnostic plots into one figure and save as PNG.
    """
    fig, axes = plt.subplots(4, 1, figsize=(11, 16))
    fig.suptitle(
        "DyLoRISK — Diagnostic Dashboard",
        fontsize=16, fontweight="bold", y=0.99,
    )
    plt.subplots_adjust(hspace=0.45)

    plot_drift(drift, ax=axes[0])
    plot_risk_trajectory(risk, ax=axes[1])
    plot_entropy(entropies, ax=axes[2])
    plot_latency(latencies, gradients, ax=axes[3])

    plt.savefig(save_path, dpi=150, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    return save_path


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 10 — PDF REPORT GENERATION
# ─────────────────────────────────────────────────────────────────────────────

def _add_text_page(pdf: PdfPages, title: str, body_lines: list[str],
                   title_color="#2c3e50"):
    """Helper: render a formatted text page into the PDF."""
    fig, ax = plt.subplots(figsize=(10, 7.5))
    ax.axis("off")
    fig.patch.set_facecolor("white")

    # Title box
    ax.text(0.5, 0.96, title,
            ha="center", va="top", fontsize=15,
            fontweight="bold", color=title_color,
            transform=ax.transAxes)
    ax.plot([0, 1], [0.93, 0.93], color=title_color, linewidth=1.5, transform=ax.transAxes, clip_on=False)

    y = 0.88
    for line in body_lines:
        if line.startswith("##"):
            ax.text(0.04, y, line[2:].strip(),
                    ha="left", va="top", fontsize=11,
                    fontweight="bold", color="#2980b9",
                    transform=ax.transAxes)
            y -= 0.03
        elif line.startswith("•"):
            ax.text(0.06, y, line,
                    ha="left", va="top", fontsize=9,
                    color="#2c3e50", transform=ax.transAxes)
            y -= 0.028
        elif line == "---":
            ax.plot([0.02, 0.98], [y, y], color="#bdc3c7", linewidth=0.8,
                    transform=ax.transAxes, clip_on=False)
            y -= 0.015
        elif line == "":
            y -= 0.015
        else:
            # Wrap long lines at ~110 chars
            words = line.split()
            row = ""
            for w in words:
                if len(row) + len(w) + 1 > 110:
                    ax.text(0.04, y, row,
                            ha="left", va="top", fontsize=9,
                            color="#2c3e50", transform=ax.transAxes)
                    y -= 0.028
                    row = w
                else:
                    row = (row + " " + w).strip()
            if row:
                ax.text(0.04, y, row,
                        ha="left", va="top", fontsize=9,
                        color="#2c3e50", transform=ax.transAxes)
                y -= 0.028
        if y < 0.04:
            break

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _score_gauge_page(pdf: PdfPages, hs_dict: dict, stats: dict):
    """Render the health-score gauge page."""
    fig, axes = plt.subplots(1, 2, figsize=(10, 5),
                             gridspec_kw={"width_ratios": [1, 1.4]})
    fig.patch.set_facecolor("white")

    # ── Left: semicircular gauge ──────────────────────────────────────────
    ax = axes[0]
    ax.set_aspect("equal")
    ax.axis("off")

    hs    = hs_dict["health_score"]
    color = hs_dict["color"]

    # Background arc (grey)
    theta = np.linspace(np.pi, 0, 200)
    r = 1.0
    ax.fill_between(r * np.cos(theta), r * np.sin(theta),
                    0.85 * r * np.cos(theta) * 0, alpha=0)
    ax.plot(r * np.cos(theta), r * np.sin(theta), color="#dfe6e9", linewidth=20,
            solid_capstyle="round")
    # Filled arc proportional to score
    frac  = hs / 100.0
    theta2 = np.linspace(np.pi, np.pi - frac * np.pi, 200)
    ax.plot(r * np.cos(theta2), r * np.sin(theta2),
            color=color, linewidth=20, solid_capstyle="round")

    ax.text(0, 0.05, f"{hs:.1f}", ha="center", va="center",
            fontsize=32, fontweight="bold", color=color)
    ax.text(0, -0.25, "Health Score", ha="center", va="center",
            fontsize=10, color="#7f8c8d")
    ax.text(0, -0.45, hs_dict["status"], ha="center", va="center",
            fontsize=14, fontweight="bold", color=color)
    ax.set_xlim(-1.3, 1.3)
    ax.set_ylim(-0.7, 1.2)

    # ── Right: metrics table ──────────────────────────────────────────────
    ax2 = axes[1]
    ax2.axis("off")
    rows = [
        ("Total Events",          str(stats["n_events"])),
        ("Total Windows",         str(stats["n_windows"])),
        ("Window Size (W)",       str(stats["window_size"])),
        ("Distinct Templates (K)",str(stats["n_templates"])),
        ("Mean Cosine Drift (d̄)", f"{hs_dict['d_avg']:.4f}"),
        ("Normalised Grad (ḡ)",   f"{hs_dict['g_norm']:.4f}"),
        ("Graph Density (ρ)",     f"{stats['graph_density']:.4f}"),
        ("Peak Cumul. Risk",      f"{stats['peak_risk']:.4f}"),
    ]
    col_labels = ["Metric", "Value"]
    table = ax2.table(
        cellText=rows,
        colLabels=col_labels,
        cellLoc="center",
        loc="center",
        bbox=[0, 0, 1, 1],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    for (r, c), cell in table.get_celld().items():
        if r == 0:
            cell.set_facecolor("#2c3e50")
            cell.set_text_props(color="white", fontweight="bold")
        elif r % 2 == 0:
            cell.set_facecolor("#ecf0f1")
        else:
            cell.set_facecolor("white")
        cell.set_edgecolor("#bdc3c7")

    fig.suptitle("DyLoRISK — System Health Assessment",
                 fontsize=14, fontweight="bold", color="#2c3e50")
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _plots_page(pdf: PdfPages, plot_png_path: str):
    """Embed the saved diagnostic PNG as a full page in the PDF."""
    img = plt.imread(plot_png_path)
    fig, ax = plt.subplots(figsize=(10, 13))
    ax.imshow(img)
    ax.axis("off")
    fig.patch.set_facecolor("white")
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def generate_pdf_report(
    df: pd.DataFrame,
    windows: list[pd.DataFrame],
    lsd_matrix: np.ndarray,
    drift: np.ndarray,
    risk: np.ndarray,
    entropies: np.ndarray,
    latencies: np.ndarray,
    gradients: np.ndarray,
    hs_dict: dict,
    graph_metrics: dict,
    all_event_ids: list[str],
    plot_png_path: str,
    output_path: str = "DyLoRISK_Report.pdf",
) -> str:
    """
    Stage 10 — Generate the multi-page PDF diagnostic report.

    Pages:
      1. Cover / Executive Summary
      2. Health Score Gauge + Metrics Table
      3. Diagnostic Plots (all 4 charts)
      4. Results — Drift & Risk Analysis
      5. Results — Entropy & Graph Analysis
      6. Results — Escalation & Health Score
      7. Discussion — Limitations & Future Work
    """
    stats = {
        "n_events":     len(df),
        "n_windows":    len(windows),
        "window_size":  compute_window_size(len(df)),
        "n_templates":  lsd_matrix.shape[1],
        "graph_density": graph_metrics["graph_density"],
        "peak_risk":    float(risk[-1]) if len(risk) > 0 else 0.0,
    }

    with PdfPages(output_path) as pdf:

        # ── Page 1: Cover ────────────────────────────────────────────────────
        cover_lines = [
            "## Framework Overview",
            "",
            "DyLoRISK is a mathematically grounded, non-deep-learning framework for",
            "continuous system stability modeling using operational log data.",
            "",
            "Unlike deep learning approaches (DeepLog, LogBERT, DualBERT) that produce",
            "binary anomaly labels, DyLoRISK models the full instability trajectory by:",
            "",
            "•  Representing system state as sliding-window event-frequency vectors (LSD)",
            "•  Quantifying behavioral drift via cosine distance between consecutive LSDs",
            "•  Tracking event diversity through per-window Shannon entropy",
            "•  Constructing a directed Log Structure Graph from event transitions",
            "•  Measuring escalation via the numerical latency gradient",
            "•  Synthesising a monotonically non-decreasing cumulative risk trajectory",
            "•  Composing a composite 0–100 Health Score for instant status assessment",
            "",
            "---",
            "## Key Results — This Session",
            "",
            f"•  Total Log Events Analysed  : {stats['n_events']}",
            f"•  Analysis Windows           : {stats['n_windows']} (W = {stats['window_size']} events each)",
            f"•  Distinct Event Templates   : {stats['n_templates']}",
            f"•  Mean Cosine Drift (d̄)      : {hs_dict['d_avg']:.4f}",
            f"•  Normalised Escalation (g)  : {hs_dict['g_norm']:.4f}",
            f"•  Cumulative Risk (final)     : {stats['peak_risk']:.4f}",
            f"•  Health Score               : {hs_dict['health_score']} / 100",
            f"•  System Status              : {hs_dict['status']}",
            "",
            "---",
            "Reference: Vignesh T, Sibi Kumar R, Santhosh Kumar A.",
            "DyLoRISK: Dynamic Log Risk and Instability State-Space Knowledge",
            "Framework for Continuous System Stability Modeling. 2025.",
        ]
        _add_text_page(pdf,
                       "DyLoRISK — Diagnostic Report",
                       cover_lines,
                       title_color="#2c3e50")

        # ── Page 2: Health Gauge ─────────────────────────────────────────────
        _score_gauge_page(pdf, hs_dict, stats)

        # ── Page 3: Diagnostic Plots ─────────────────────────────────────────
        _plots_page(pdf, plot_png_path)

        # ── Page 4: Drift & Risk Results ────────────────────────────────────
        peak_drift_idx = int(np.argmax(drift))
        drift_lines = [
            "## Behavioral Drift Analysis",
            "",
            "Cosine drift d(t) measures how much the event-frequency distribution",
            "has changed between two consecutive analysis windows.",
            "",
            f"•  d(t) < 0.10  → Normal stable operation",
            f"•  d(t) ≥ 0.10  → Detectable distributional shift",
            f"•  d(t) ≥ 0.38  → Pre-anomaly threshold (HDFS benchmark reference)",
            "",
            f"•  Mean drift d̄            : {hs_dict['d_avg']:.4f}",
            f"•  Maximum drift           : {float(np.max(drift)):.4f}  (window {peak_drift_idx})",
            f"•  Minimum drift           : {float(np.min(drift[1:])):.4f}  (first positive window)",
            f"•  Windows with d > 0.10  : {int(np.sum(drift > 0.10))} / {len(drift)}",
            f"•  Windows with d > 0.38  : {int(np.sum(drift > 0.38))} / {len(drift)}",
            "",
            "---",
            "## Cumulative Risk Trajectory",
            "",
            "Risk(t) = Σ d(i) is a monotonically non-decreasing signal.",
            "A knee-point in the trajectory indicates the onset of accelerated",
            "degradation — typically 14–21 windows before critical failure",
            "(per HDFS synthetic fault-injection experiments).",
            "",
            f"•  Initial risk (t=0)      : 0.0000",
            f"•  Final cumulative risk   : {float(risk[-1]):.4f}",
            f"•  Risk at 50% of windows : {float(risk[len(risk)//2]):.4f}",
            f"•  Risk at 75% of windows : {float(risk[int(len(risk)*0.75)]):.4f}",
            "",
            "The trajectory shows the total behavioural deviation accumulated since",
            "monitoring began — invisible to instantaneous binary anomaly detectors.",
        ]
        _add_text_page(pdf,
                       "Results: Drift Quantification & Risk Trajectory",
                       drift_lines,
                       title_color="#8e44ad")

        # ── Page 5: Entropy & Graph ──────────────────────────────────────────
        top_nodes_str = ", ".join(graph_metrics["top_central_nodes"]) or "N/A"
        entropy_lines = [
            "## Shannon Entropy Analysis",
            "",
            "H(t) quantifies event diversity within each analysis window.",
            "High H(t) → diverse execution patterns (normal distributed activity).",
            "Low H(t)  → narrow repetitive patterns (recovery / overload loops).",
            "",
            f"•  Mean entropy H̄          : {float(np.mean(entropies)):.4f} nats",
            f"•  Maximum entropy         : {float(np.max(entropies)):.4f}  (window {int(np.argmax(entropies))})",
            f"•  Minimum entropy         : {float(np.min(entropies)):.4f}  (window {int(np.argmin(entropies))})",
            f"•  Entropy std deviation   : {float(np.std(entropies)):.4f}",
            "",
            "Anomalous phases exhibit bimodal entropy behaviour:",
            "•  SPIKES → error bursts (many new event types simultaneously)",
            "•  DROPS  → recovery phase (dominated by a narrow set of repair routines)",
            "",
            "---",
            "## Log Structure Graph (LSG)",
            "",
            "The directed weighted graph G = (V, E) captures event-transition patterns.",
            "Each node is a distinct log template; each edge (e_i → e_j) records how",
            "frequently event e_i is immediately followed by event e_j.",
            "",
            f"•  Graph nodes |V|          : {graph_metrics['n_nodes']}",
            f"•  Graph edges |E|          : {graph_metrics['n_edges']}",
            f"•  Graph density ρ          : {graph_metrics['graph_density']:.4f}",
            f"     ρ ∈ [0.12, 0.35] is the HDFS normal range",
            f"•  Top-3 central nodes     : {top_nodes_str}",
            "",
            "Nodes with high degree centrality are critical execution hubs.",
            "Anomalous activation of these hubs frequently precedes cascading failures.",
        ]
        _add_text_page(pdf,
                       "Results: Entropy Analysis & Log Structure Graph",
                       entropy_lines,
                       title_color="#2980b9")

        # ── Page 6: Escalation + Health ──────────────────────────────────────
        esc_lines = [
            "## Latency Escalation Dynamics",
            "",
            "g(t) = (Lat(t+1) − Lat(t−1)) / 2   (central-difference numerical gradient)",
            "",
            f"•  Normalised escalation ḡ_norm : {hs_dict['g_norm']:.4f}",
            f"•  Mean window latency          : {float(np.mean(latencies)):.2f} s",
            f"•  Maximum window latency       : {float(np.max(latencies)):.2f} s",
            f"•  Peak positive gradient       : {float(np.max(gradients)):.4f}",
            f"•  Peak negative gradient       : {float(np.min(gradients)):.4f}",
            "",
            "A consistently positive g(t) indicates accelerating execution slowdown —",
            "a quantitative precursor to system overload, providing proactive lead time",
            "before a binary anomaly is confirmed.",
            "",
            "•  DyLoRISK detects escalating slowdowns in ~93.4% of synthetic overload",
            "   scenarios with only a 7.1% false-positive rate (HDFS reference).",
            "",
            "---",
            "## Health Score Composition",
            "",
            "HS = max( 0,  100 − 50·d̄ − 10·ḡ_norm )",
            "",
            f"   = max( 0,  100 − 50×{hs_dict['d_avg']:.4f} − 10×{hs_dict['g_norm']:.4f} )",
            f"   = max( 0,  100 − {50*hs_dict['d_avg']:.4f} − {10*hs_dict['g_norm']:.4f} )",
            f"   = {hs_dict['health_score']}",
            "",
            f"   Status: {hs_dict['status']}",
            "",
            "Interpretation:",
            "•  HS > 80  → HEALTHY   — system operating within normal parameters",
            "•  50 ≤ HS ≤ 80 → WARNING  — elevated drift; monitor closely",
            "•  HS < 50  → CRITICAL  — immediate investigation required",
            "",
            "The health score correlated with operator-assessed system condition at",
            "Pearson r = 0.85 in HDFS experimental validation.",
        ]
        _add_text_page(pdf,
                       "Results: Escalation Dynamics & Health Score",
                       esc_lines,
                       title_color="#27ae60")

        # ── Page 7: Discussion ───────────────────────────────────────────────
        disc_lines = [
            "## Advantages of DyLoRISK vs. Deep Learning Approaches",
            "",
            "•  Proactive monitoring: average 6–14 window lead time before binary anomaly",
            "•  No GPU required: 13.5× higher throughput vs. DualBERT on commodity CPU",
            "•  No training data: purely unsupervised, stateless, no periodic retraining",
            "•  Full mathematical interpretability: all outputs are closed-form formulae",
            "•  Complementary: risk/drift signals can feed downstream binary classifiers",
            "",
            "---",
            "## Computational Complexity",
            "",
            "For N events, K templates, T windows of size W:",
            "•  LSD vectorisation per window : O(W + K)",
            "•  Cosine distance per pair      : O(K)",
            "•  Shannon entropy per window   : O(K)",
            "•  Graph construction           : O(N)",
            "•  Degree centrality            : O(|V| + |E|)",
            "•  Cumulative risk accumulation : O(T)",
            "Total: O(N + T·K) — highly tractable; no GPU required.",
            "",
            "---",
            "## Limitations",
            "",
            "•  Fixed window size may miss multi-scale temporal patterns",
            "•  Does not perform per-window transition-matrix comparison",
            "•  Regex templating may conflate semantically distinct events sharing structure",
            "•  Graph is built from the entire stream; per-window graph evolution not tracked",
            "•  Does not incorporate semantic meaning of log content",
            "",
            "---",
            "## Future Enhancements",
            "",
            "•  Adaptive window sizing based on log arrival rate",
            "•  Streaming deployment via Apache Kafka / Flink for real-time monitoring",
            "•  Multi-system cross-correlation for microservice environments",
            "•  Per-window transition matrix Frobenius-norm drift component",
            "•  Integration with automated remediation pipelines at threshold triggers",
            "•  Cloud-native deployment (AWS/Azure/GCP) for enterprise-scale log volumes",
        ]
        _add_text_page(pdf,
                       "Discussion — Limitations & Future Work",
                       disc_lines,
                       title_color="#e67e22")

    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 11 — RESULT & DISCUSSION  (printed summary)
# ─────────────────────────────────────────────────────────────────────────────

def print_results(
    df: pd.DataFrame,
    windows: list[pd.DataFrame],
    lsd_matrix: np.ndarray,
    drift: np.ndarray,
    risk: np.ndarray,
    entropies: np.ndarray,
    latencies: np.ndarray,
    gradients: np.ndarray,
    hs_dict: dict,
    graph_metrics: dict,
):
    sep = "─" * 70
    print(f"\n{sep}")
    print("  DyLoRISK — ANALYSIS RESULTS & SCIENTIFIC DISCUSSION")
    print(sep)

    print(f"\n📋 STAGE 1 — LOG PARSING")
    print(f"   Total raw events parsed        : {len(df)}")
    print(f"   Distinct event templates (K)   : {lsd_matrix.shape[1]}")

    W = compute_window_size(len(df))
    print(f"\n🪟 STAGE 2 — WINDOW SEGMENTATION")
    print(f"   Window size W = max(5, ⌊{len(df)}/20⌋) = {W}")
    print(f"   Total analysis windows (T)     : {len(windows)}")

    print(f"\n📐 STAGE 3 — LOG STATE DISTRIBUTION")
    print(f"   LSD matrix shape               : {lsd_matrix.shape}  (T×K)")
    print(f"   Mean events per window         : {lsd_matrix.sum(axis=1).mean():.1f}")

    print(f"\n📊 STAGE 4 — BEHAVIORAL DRIFT & CUMULATIVE RISK")
    print(f"   Mean cosine drift d̄            : {hs_dict['d_avg']:.4f}")
    print(f"   Max drift                      : {float(np.max(drift)):.4f}  (window {int(np.argmax(drift))})")
    print(f"   Windows exceeding 0.10         : {int(np.sum(drift > 0.10))} / {len(drift)}")
    print(f"   Final cumulative risk          : {float(risk[-1]):.4f}")

    print(f"\n🔀 STAGE 5 — SHANNON ENTROPY")
    print(f"   Mean H(t)                      : {float(np.mean(entropies)):.4f} nats")
    print(f"   Std deviation                  : {float(np.std(entropies)):.4f}")
    print(f"   Min H(t) @ window              : {float(np.min(entropies)):.4f} (w={int(np.argmin(entropies))})")
    print(f"   Max H(t) @ window              : {float(np.max(entropies)):.4f} (w={int(np.argmax(entropies))})")

    print(f"\n⏱  STAGE 6 — LATENCY & ESCALATION")
    print(f"   Mean window latency            : {float(np.mean(latencies)):.2f} s")
    print(f"   Max window latency             : {float(np.max(latencies)):.2f} s")
    print(f"   Normalised escalation ḡ_norm   : {hs_dict['g_norm']:.4f}")

    print(f"\n🔗 STAGE 7 — LOG STRUCTURE GRAPH")
    print(f"   Graph nodes |V|                : {graph_metrics['n_nodes']}")
    print(f"   Graph edges |E|                : {graph_metrics['n_edges']}")
    print(f"   Graph density ρ                : {graph_metrics['graph_density']:.4f}")
    top_nodes = graph_metrics['top_central_nodes']
    for i, node in enumerate(top_nodes):
        cd = graph_metrics['degree_centrality'].get(node, 0.0)
        print(f"   High-centrality node #{i+1}        : {node}  (CD={cd:.4f})")

    print(f"\n❤  STAGE 8 — HEALTH SCORE")
    print(f"   HS = max(0, 100 − 50×{hs_dict['d_avg']:.4f} − 10×{hs_dict['g_norm']:.4f})")
    print(f"      = {hs_dict['health_score']}")
    hs_color_map = {"HEALTHY": "✅", "WARNING": "⚠️ ", "CRITICAL": "🔴"}
    icon = hs_color_map.get(hs_dict['status'], "")
    print(f"   Status: {icon}  {hs_dict['status']}")

    print(f"\n{sep}")
    print("  SCIENTIFIC INTERPRETATION")
    print(sep)

    # Drift interpretation
    d = hs_dict['d_avg']
    print(f"\n🔬 Drift:")
    if d < 0.10:
        print("   Mean cosine drift is below 0.10 — event-frequency distributions are")
        print("   stable across windows. System behaviour is consistent.")
    elif d < 0.38:
        print("   Mean cosine drift is elevated (0.10–0.38). The system is exhibiting")
        print("   non-trivial distributional shift. Monitor for further acceleration.")
    else:
        print("   Mean cosine drift exceeds 0.38 — the pre-anomaly threshold observed")
        print("   in HDFS experiments. High probability of impending failure event.")

    # Entropy interpretation
    h_mean = float(np.mean(entropies))
    h_std  = float(np.std(entropies))
    print(f"\n🔬 Entropy:")
    if h_std > 0.5 * h_mean:
        print("   High entropy variance — bimodal behaviour detected. Error bursts are")
        print("   causing entropy spikes, while recovery phases cause drops.")
    else:
        print(f"   Entropy is stable around {h_mean:.3f} nats — consistent event diversity.")

    # Escalation interpretation
    g = hs_dict['g_norm']
    print(f"\n🔬 Escalation:")
    if g > 0.2:
        print("   Normalised latency gradient is HIGH. Execution slowdown is accelerating.")
        print("   This is a quantitative precursor to system overload.")
    elif g > 0.05:
        print("   Moderate latency escalation detected. Execution time is growing.")
    else:
        print("   Latency gradient is low — no significant escalation detected.")

    # Graph interpretation
    rho = graph_metrics['graph_density']
    print(f"\n🔬 Graph Structure:")
    if rho > 0.35:
        print(f"   Graph density ρ={rho:.4f} is HIGH. Many event co-transitions suggest")
        print("   complex, potentially chaotic execution paths.")
    elif rho < 0.12:
        print(f"   Graph density ρ={rho:.4f} is LOW. Sparse, linear execution paths —")
        print("   typical of healthy sequential processing pipelines.")
    else:
        print(f"   Graph density ρ={rho:.4f} is within the normal HDFS reference range")
        print("   [0.12, 0.35] — indicating structured but non-trivial event flow.")

    print(f"\n{sep}\n")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

def run_dylorisk(
    raw_lines: list[str] | None = None,
    n_events: int = 200,
    report_path: str = "DyLoRISK_Report.pdf",
    plots_path: str  = "dylorisk_plots.png",
    seed: int = 42,
) -> dict:
    """
    Full DyLoRISK pipeline — Stages 1 through 11.

    Parameters
    ----------
    raw_lines   : list of raw log strings (None → auto-generate)
    n_events    : number of events to generate if raw_lines is None
    report_path : output PDF path
    plots_path  : output PNG dashboard path
    seed        : random seed for synthetic log generation

    Returns
    -------
    dict with all computed metrics and output file paths
    """
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║            DyLoRISK — Full Pipeline Execution                    ║")
    print("╚══════════════════════════════════════════════════════════════════╝\n")

    # ── Stage 1 — Parsing ─────────────────────────────────────────────────
    print("▶ Stage 1: Log Parsing …")
    if raw_lines is None:
        print(f"  (No input provided — generating {n_events} synthetic log events)")
        raw_lines = generate_sample_logs(n_events=n_events, seed=seed)
    df = parse_logs(raw_lines)
    df.to_csv("parsed_logs.csv", index=False)
    print(f"  Parsed {len(df)} events → {df['EventId'].nunique()} templates  [parsed_logs.csv]")

    # ── Stage 2 — Windowing ───────────────────────────────────────────────
    print("▶ Stage 2: Window Segmentation …")
    W = compute_window_size(len(df))
    windows = segment_windows(df, W)
    print(f"  W = {W}  →  {len(windows)} valid windows")

    all_event_ids = sorted(df["EventId"].unique(),
                           key=lambda x: int(x[1:]))

    # ── Stage 3 — LSD Modeling ────────────────────────────────────────────
    print("▶ Stage 3: Log State Distribution (LSD) …")
    lsd_matrix = build_lsd_vectors(windows, all_event_ids)
    latencies  = compute_window_latency(windows)
    print(f"  LSD matrix: {lsd_matrix.shape}  (T×K)")

    # ── Stage 4 — Drift ───────────────────────────────────────────────────
    print("▶ Stage 4: Behavioral Drift + Cumulative Risk …")
    drift = compute_cosine_drift(lsd_matrix)
    risk  = compute_cumulative_risk(drift)
    print(f"  Mean drift = {np.mean(drift):.4f}  |  Peak risk = {risk[-1]:.4f}")

    # ── Stage 5 — Entropy ─────────────────────────────────────────────────
    print("▶ Stage 5: Shannon Entropy …")
    entropies = compute_entropy(lsd_matrix)
    print(f"  Mean H(t) = {np.mean(entropies):.4f} nats")

    # ── Stage 6 — Latency ─────────────────────────────────────────────────
    print("▶ Stage 6: Latency Gradient & Escalation …")
    gradients = compute_latency_gradient(latencies)
    g_norm    = compute_escalation_score(latencies, gradients)
    print(f"  ḡ_norm = {g_norm:.4f}")

    # ── Stage 7 — Graph ───────────────────────────────────────────────────
    print("▶ Stage 7: Log Structure Graph …")
    G = build_log_structure_graph(df)
    graph_metrics = compute_graph_metrics(G)
    print(f"  |V|={graph_metrics['n_nodes']}  |E|={graph_metrics['n_edges']}  ρ={graph_metrics['graph_density']:.4f}")

    # ── Stage 8 — Health Score ────────────────────────────────────────────
    print("▶ Stage 8: Health Score …")
    hs_dict = compute_health_score(drift, g_norm)
    print(f"  HS = {hs_dict['health_score']}  →  {hs_dict['status']}")

    # ── Stage 9 — Visualizations ──────────────────────────────────────────
    print("▶ Stage 9: Generating Visualizations …")
    generate_all_plots(drift, risk, entropies, latencies, gradients,
                       save_path=plots_path)
    print(f"  Plots saved → {plots_path}")

    # ── Stage 10 — PDF Report ─────────────────────────────────────────────
    print("▶ Stage 10: Generating PDF Report …")
    generate_pdf_report(
        df, windows, lsd_matrix, drift, risk,
        entropies, latencies, gradients, hs_dict,
        graph_metrics, all_event_ids, plots_path,
        output_path=report_path,
    )
    print(f"  Report saved → {report_path}")

    # ── Stage 11 — Results & Discussion ──────────────────────────────────
    print("\n▶ Stage 11: Results & Discussion")
    print_results(df, windows, lsd_matrix, drift, risk, entropies,
                  latencies, gradients, hs_dict, graph_metrics)

    return {
        "parsed_df":      df,
        "windows":        windows,
        "lsd_matrix":     lsd_matrix,
        "drift":          drift,
        "risk":           risk,
        "entropies":      entropies,
        "latencies":      latencies,
        "gradients":      gradients,
        "g_norm":         g_norm,
        "graph":          G,
        "graph_metrics":  graph_metrics,
        "health_score":   hs_dict,
        "plots_path":     plots_path,
        "report_path":    report_path,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    results = run_dylorisk(n_events=200, seed=42)
    print(f"✅  Done.  PDF → {results['report_path']}")
    print(f"✅  Done.  PNG → {results['plots_path']}")
