"""
dylorisk_score_engine.py
════════════════════════════════════════════════════════════════════════════════
DyLoRISK Deterministic Score Engine  — v2.0
Implements idempotent health scoring with root-cause attribution.

DESIGN CONTRACT
───────────────
• Pure functions — no global mutable state, no I/O.
• All float arithmetic uses float64 with explicit rounding at output boundary.
• EventId assignment is alphabetically-sorted (not insertion-order), breaking
  the non-determinism present in the v1 template_to_id dict.
• SHA-256 content hash is the cache key; mtime/size are NOT used.
• Aggregate score over N files uses weighted mean (by event count).

Data Models (see bottom of file for JSON schema examples)
────────────────────────────────────────────────────────
  LogEntry      — one parsed log line
  WindowMetrics — computed metrics for one time-window
  ScoreResult   — full result for one file
  RootCause     — one identified contributing factor
  AggregateResult — cross-file summary
"""

from __future__ import annotations

import re
import math
import hashlib
import datetime
from dataclasses import dataclass, field, asdict
from collections import Counter
from typing import List, Dict, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import entropy as scipy_entropy
from sklearn.metrics.pairwise import cosine_similarity
import networkx as nx


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LogEntry:
    line_number:    int          # 1-based original line index
    raw_line:       str
    timestamp:      float        # Unix epoch, deterministic parse
    level:          str          # INFO / WARN / ERROR / FATAL / CRITICAL / UNKNOWN
    event_template: str          # templatized (numbers → <*>)
    event_id:       str          # E1, E2, … assigned by sorted template order


@dataclass
class WindowMetrics:
    window_index: int
    event_count:  int
    latency_s:    float          # last_ts − first_ts
    entropy:      float          # Shannon H(t)
    drift:        float          # cosine distance from previous window
    error_count:  int
    warn_count:   int
    first_ts:     float
    last_ts:      float


@dataclass
class RootCause:
    rank:         int            # 1 = highest impact
    factor:       str            # human-readable name  e.g. "High Cosine Drift"
    metric_name:  str            # internal key         e.g. "d_avg"
    metric_value: float
    threshold:    float          # the boundary that was crossed
    weight:       float          # contribution to score penalty (raw points lost)
    evidence:     List[str]      # pointers: "file:line" or "template" strings
    description:  str


@dataclass
class ScoreResult:
    # identity
    file_path:      str
    file_name:      str
    content_hash:   str          # SHA-256 of file bytes — cache key

    # timing
    analysis_start: float        # time.time() at start
    analysis_end:   float        # time.time() at end
    analysis_secs:  float        # end − start

    # score
    health_score:   float        # 0 – 100, rounded to 2dp
    status:         str          # HEALTHY / WARNING / CRITICAL
    score_formula:  str          # human-readable formula used

    # raw inputs to formula
    d_avg:          float        # mean cosine drift
    g_norm:         float        # normalised escalation

    # counts
    total_events:   int
    error_count:    int
    warn_count:     int
    info_count:     int

    # structural
    graph_density:  float
    graph_nodes:    int
    graph_edges:    int

    # details
    window_metrics: List[WindowMetrics]
    top5_error_templates: Dict[str, int]
    root_causes:    List[RootCause]
    drift_events:   List[dict]

    # output paths
    report_pdf:     str = ""
    plots_png:      str = ""


@dataclass
class AggregateResult:
    file_count:         int
    aggregate_score:    float        # weighted mean by event count
    aggregate_status:   str
    critical_files:     List[str]
    warning_files:      List[str]
    healthy_files:      List[str]
    common_patterns:    List[str]    # error templates appearing in ≥50% of files
    per_file:           List[ScoreResult]
    analysis_secs_total: float


# ─────────────────────────────────────────────────────────────────────────────
# CONTENT HASHING  (deterministic cache key)
# ─────────────────────────────────────────────────────────────────────────────

def compute_content_hash(file_path: str) -> str:
    """SHA-256 of raw file bytes — file rename / mtime change do NOT affect this."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_lines_hash(lines: List[str]) -> str:
    """SHA-256 of normalised line list (used when content is already loaded)."""
    h = hashlib.sha256()
    for line in lines:
        h.update((line + "\n").encode("utf-8", errors="replace"))
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1 — DETERMINISTIC LOG PARSING
# ─────────────────────────────────────────────────────────────────────────────

_LEVEL_RE = re.compile(
    r"\b(ERROR|FATAL|CRITICAL|WARN(?:ING)?|INFO|DEBUG)\b", re.I
)
_TS_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)")
_TS_HDFS_SHORT = re.compile(
    r"^(\d{2})(\d{2})(\d{2})\s+(\d{2})(\d{2})(\d{2})\s+\d+\s+(.*)"
)


def _parse_timestamp(line: str) -> Optional[Tuple[float, str]]:
    m = _TS_ISO.match(line)
    if m:
        ts_str, body = m.groups()
        try:
            dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            return dt.timestamp(), body.strip()
        except ValueError:
            return None
    m = _TS_HDFS_SHORT.match(line)
    if m:
        yy, mm, dd, hh, mi, ss, rest = m.groups()
        try:
            d = datetime.datetime.strptime(f"{yy}{mm}{dd}", "%y%m%d").date()
            dt = datetime.datetime.combine(d, datetime.time(int(hh), int(mi), int(ss)))
            return dt.timestamp(), rest.strip()
        except ValueError:
            return None
    return None


def _infer_level(body: str) -> str:
    m = _LEVEL_RE.search(body)
    if not m:
        return "UNKNOWN"
    lv = m.group(1).upper()
    return "WARN" if lv == "WARNING" else lv


def _templatize(text: str) -> str:
    text = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<*>", text)
    text = re.sub(r"\b\d+(\.\d+)?\b", "<*>", text)
    return re.sub(r" +", " ", text).strip()


def parse_logs_deterministic(raw_lines: List[str]) -> List[LogEntry]:
    """
    Parse raw lines into LogEntry objects.

    DETERMINISM GUARANTEE:
    • EventId is assigned by SORTED order of first-seen templates, not
      insertion order. This means the same set of templates always maps to
      the same E-numbers regardless of line order in the file.
    • Timestamps use datetime.strptime (no locale-dependent parsing).
    """
    # First pass: collect all templates, timestamps
    raw_records: List[Tuple[int, float, str, str]] = []  # (lineno, ts, body, template)
    for lineno, line in enumerate(raw_lines, 1):
        line = line.strip()
        if not line:
            continue
        parsed = _parse_timestamp(line)
        if parsed is None:
            continue
        ts, body = parsed
        template = _templatize(body)
        raw_records.append((lineno, ts, body, template))

    # Assign EventIds by SORTED unique templates (deterministic)
    all_templates = sorted(set(r[3] for r in raw_records))
    template_to_id = {t: f"E{i+1}" for i, t in enumerate(all_templates)}

    entries: List[LogEntry] = []
    for (lineno, ts, body, template) in raw_records:
        level = _infer_level(body)
        entries.append(LogEntry(
            line_number=lineno,
            raw_line=raw_lines[lineno - 1],
            timestamp=ts,
            level=level,
            event_template=template,
            event_id=template_to_id[template],
        ))

    # Sort by timestamp (secondary sort by line_number for ties — deterministic)
    entries.sort(key=lambda e: (e.timestamp, e.line_number))
    return entries


# ─────────────────────────────────────────────────────────────────────────────
# STAGES 2-7 — PURE METRIC COMPUTATION (stateless, deterministic)
# ─────────────────────────────────────────────────────────────────────────────

def _window_size(n: int) -> int:
    return max(5, int(math.floor(n / 20)))


def _segment(entries: List[LogEntry], W: int) -> List[List[LogEntry]]:
    windows = []
    for start in range(0, len(entries), W):
        chunk = entries[start:start + W]
        if len(chunk) >= 2:
            windows.append(chunk)
    return windows


def _build_lsd(windows: List[List[LogEntry]], all_eids: List[str]) -> np.ndarray:
    """LSD matrix (T × K) — counts, no randomness."""
    idx = {eid: i for i, eid in enumerate(all_eids)}
    lsd = np.zeros((len(windows), len(all_eids)), dtype=np.float64)
    for t, win in enumerate(windows):
        for e in win:
            if e.event_id in idx:
                lsd[t, idx[e.event_id]] += 1.0
    return lsd


def _cosine_drift(lsd: np.ndarray) -> np.ndarray:
    T = lsd.shape[0]
    d = np.zeros(T, dtype=np.float64)
    for t in range(1, T):
        v, u = lsd[t:t+1], lsd[t-1:t]
        nv, nu = np.linalg.norm(v), np.linalg.norm(u)
        if nv == 0.0 or nu == 0.0:
            d[t] = 0.0
        else:
            sim = float(cosine_similarity(v, u)[0, 0])
            d[t] = max(0.0, min(1.0, 1.0 - sim))
    return d


def _entropy(lsd: np.ndarray) -> np.ndarray:
    T = lsd.shape[0]
    H = np.zeros(T, dtype=np.float64)
    for t in range(T):
        row = lsd[t]
        total = row.sum()
        if total > 0:
            H[t] = float(scipy_entropy(row / total))
    return H


def _latencies(windows: List[List[LogEntry]]) -> np.ndarray:
    return np.array([
        max(0.0, w[-1].timestamp - w[0].timestamp)
        for w in windows
    ], dtype=np.float64)


def _escalation(lats: np.ndarray) -> Tuple[np.ndarray, float]:
    if len(lats) < 2:
        return np.zeros_like(lats), 0.0
    grads = np.gradient(lats)
    mean_lat = float(np.mean(lats))
    g_norm = 0.0 if mean_lat == 0.0 else float(np.mean(np.abs(grads))) / mean_lat
    return grads, g_norm


def _build_graph(entries: List[LogEntry]) -> nx.DiGraph:
    G = nx.DiGraph()
    eids = [e.event_id for e in entries]
    for i in range(len(eids) - 1):
        src, dst = eids[i], eids[i+1]
        if G.has_edge(src, dst):
            G[src][dst]["weight"] += 1
        else:
            G.add_edge(src, dst, weight=1)
    return G


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 8 — DETERMINISTIC HEALTH SCORE
# ─────────────────────────────────────────────────────────────────────────────

# Score formula coefficients — centralised so changes propagate everywhere
_COEFF_DRIFT      = 50.0   # penalty weight for mean cosine drift
_COEFF_ESCALATION = 10.0   # penalty weight for normalised escalation
_MAX_SCORE        = 100.0

SCORE_FORMULA_STR = (
    f"HS = max(0, {_MAX_SCORE} "
    f"− {_COEFF_DRIFT}·d̄ "
    f"− {_COEFF_ESCALATION}·ḡ_norm)"
)

# Thresholds
_HEALTHY_THRESHOLD  = 80.0
_WARNING_THRESHOLD  = 50.0

# Drift interpretation bands (from HDFS reference)
_DRIFT_STABLE        = 0.10
_DRIFT_ELEVATED      = 0.38

# Escalation bands
_ESC_HIGH            = 0.20
_ESC_MODERATE        = 0.05


def _classify(score: float) -> str:
    if score > _HEALTHY_THRESHOLD:
        return "HEALTHY"
    if score >= _WARNING_THRESHOLD:
        return "WARNING"
    return "CRITICAL"


def compute_health_score_deterministic(d_avg: float, g_norm: float) -> Tuple[float, str]:
    """
    Pure function: (d_avg, g_norm) → (score, status).
    Identical inputs ALWAYS produce identical outputs.
    """
    raw = _MAX_SCORE - _COEFF_DRIFT * d_avg - _COEFF_ESCALATION * g_norm
    score = round(max(0.0, raw), 2)
    return score, _classify(score)


# ─────────────────────────────────────────────────────────────────────────────
# ROOT-CAUSE EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_root_causes(
    entries:   List[LogEntry],
    windows:   List[List[LogEntry]],
    drift:     np.ndarray,
    g_norm:    float,
    d_avg:     float,
    lsd:       np.ndarray,
    H:         np.ndarray,
    grads:     np.ndarray,
    graph:     nx.DiGraph,
) -> List[RootCause]:
    """
    Identify the top contributing factors to a non-HEALTHY score.
    Each RootCause carries:
      • the metric value and threshold crossed
      • concrete evidence pointers (file line numbers, timestamp ranges,
        template strings)
      • the raw score penalty it contributed
    """
    causes: List[RootCause] = []

    # ── 1. Drift ──────────────────────────────────────────────────────────────
    drift_penalty = round(_COEFF_DRIFT * d_avg, 2)
    if d_avg >= _DRIFT_STABLE:
        # Find windows with highest drift
        top_drift_windows = np.argsort(drift)[::-1][:3]
        evidence = []
        for wi in top_drift_windows:
            if wi < len(windows) and drift[wi] > 0:
                w = windows[wi]
                ts_start = datetime.datetime.fromtimestamp(w[0].timestamp).strftime("%Y-%m-%d %H:%M:%S")
                ts_end   = datetime.datetime.fromtimestamp(w[-1].timestamp).strftime("%Y-%m-%d %H:%M:%S")
                evidence.append(
                    f"Window {wi}: lines {w[0].line_number}–{w[-1].line_number}"
                    f"  [{ts_start} → {ts_end}]  drift={drift[wi]:.4f}"
                )
        if d_avg >= _DRIFT_ELEVATED:
            desc = (f"Mean cosine drift {d_avg:.4f} exceeds pre-anomaly threshold "
                    f"{_DRIFT_ELEVATED}. High probability of impending failure.")
        else:
            desc = (f"Mean cosine drift {d_avg:.4f} is elevated (>{_DRIFT_STABLE}). "
                    "Non-trivial distributional shift in event frequencies detected.")
        causes.append(RootCause(
            rank=0, factor="High Cosine Drift",
            metric_name="d_avg", metric_value=round(d_avg, 4),
            threshold=_DRIFT_STABLE,
            weight=drift_penalty,
            evidence=evidence, description=desc,
        ))

    # ── 2. Escalation ─────────────────────────────────────────────────────────
    esc_penalty = round(_COEFF_ESCALATION * g_norm, 2)
    if g_norm >= _ESC_MODERATE:
        # Find windows with highest absolute gradient
        abs_grads = np.abs(grads)
        top_esc_windows = np.argsort(abs_grads)[::-1][:3]
        evidence = []
        for wi in top_esc_windows:
            if wi < len(windows):
                w = windows[wi]
                evidence.append(
                    f"Window {wi}: lines {w[0].line_number}–{w[-1].line_number}"
                    f"  gradient={grads[wi]:.2f}s"
                )
        desc = (f"Normalised latency gradient {g_norm:.4f} "
                f"{'(HIGH)' if g_norm >= _ESC_HIGH else '(MODERATE)'}. "
                "Execution time is growing between analysis windows.")
        causes.append(RootCause(
            rank=0, factor="Latency Escalation",
            metric_name="g_norm", metric_value=round(g_norm, 4),
            threshold=_ESC_MODERATE,
            weight=esc_penalty,
            evidence=evidence, description=desc,
        ))

    # ── 3. Error burst ────────────────────────────────────────────────────────
    error_entries = [e for e in entries if e.level in ("ERROR", "FATAL", "CRITICAL")]
    error_rate = len(error_entries) / max(1, len(entries))
    if error_rate > 0.10:
        top_templates = Counter(e.event_template for e in error_entries).most_common(3)
        evidence = []
        for t, cnt in top_templates:
            sample = next((e for e in error_entries if e.event_template == t), None)
            if sample:
                evidence.append(
                    f"'{t[:60]}' × {cnt} occurrences "
                    f"(first at line {sample.line_number})"
                )
        causes.append(RootCause(
            rank=0, factor="Error Burst",
            metric_name="error_rate", metric_value=round(error_rate, 4),
            threshold=0.10,
            weight=0.0,   # informational; not directly in formula
            evidence=evidence,
            description=(
                f"{len(error_entries)} ERROR/FATAL/CRITICAL events "
                f"({error_rate*100:.1f}% of total). Top templates listed in evidence."
            ),
        ))

    # ── 4. Entropy spike ──────────────────────────────────────────────────────
    if len(H) > 1:
        h_mean, h_std = float(np.mean(H)), float(np.std(H))
        if h_std > 0.5 * h_mean:
            spike_windows = np.where(H > h_mean + 2 * h_std)[0]
            evidence = []
            for wi in spike_windows[:3]:
                if wi < len(windows):
                    w = windows[wi]
                    evidence.append(
                        f"Window {wi}: H={H[wi]:.3f}  "
                        f"lines {w[0].line_number}–{w[-1].line_number}"
                    )
            causes.append(RootCause(
                rank=0, factor="Entropy Spike",
                metric_name="h_std_ratio", metric_value=round(h_std / h_mean, 4),
                threshold=0.5,
                weight=0.0,
                evidence=evidence,
                description=(
                    f"Entropy std/mean ratio {h_std/h_mean:.3f} > 0.5. "
                    "Bimodal behaviour detected — error bursts followed by recovery."
                ),
            ))

    # Sort by weight descending (most impactful first), then assign ranks
    causes.sort(key=lambda c: c.weight, reverse=True)
    for i, c in enumerate(causes):
        c.rank = i + 1
    return causes



def extract_drift_events(
    entries,
    windows,
    drift,
    top_k=3,
):
    """Extract actual drift-causing log lines."""

    if len(windows) < 2:
        return []

    results = []
    top_windows = np.argsort(drift)[::-1][:top_k]

    for wi in top_windows:

        if wi == 0 or wi >= len(windows):
            continue

        curr = windows[wi]
        prev = windows[wi - 1]

        prev_templates = Counter(e.event_template for e in prev)
        curr_templates = Counter(e.event_template for e in curr)

        changes = []

        for tmpl in set(prev_templates) | set(curr_templates):

            before = prev_templates.get(tmpl, 0)
            after = curr_templates.get(tmpl, 0)

            diff = abs(after - before)

            if diff > 0:
                changes.append((diff, tmpl))

        changes.sort(reverse=True)

        evidence = []

        for _, tmpl in changes[:5]:

            sample = next(
                (e.raw_line for e in curr if e.event_template == tmpl),
                None
            )

            if sample:
                evidence.append(sample)

        results.append({
            "window": int(wi),
            "drift": round(float(drift[wi]), 4),
            "events": evidence,
        })

    return results


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATE SCORING (multi-file, deterministic)
# ─────────────────────────────────────────────────────────────────────────────

def compute_aggregate_score(results: List[ScoreResult]) -> AggregateResult:
    """
    Weighted mean health score (weight = event count per file).
    Order-independent: result is identical regardless of results list order.
    """
    total_events = sum(r.total_events for r in results)
    if total_events == 0:
        weighted_score = 0.0
    else:
        weighted_score = sum(
            r.health_score * r.total_events for r in results
        ) / total_events

    agg_score  = round(weighted_score, 2)
    agg_status = _classify(agg_score)

    critical = sorted(r.file_name for r in results if r.status == "CRITICAL")
    warning  = sorted(r.file_name for r in results if r.status == "WARNING")
    healthy  = sorted(r.file_name for r in results if r.status == "HEALTHY")

    # Common patterns: error templates in ≥50% of files
    n_files = len(results)
    template_file_count: Counter = Counter()
    for r in results:
        for tmpl in r.top5_error_templates:
            template_file_count[tmpl] += 1
    common = sorted(
        t for t, c in template_file_count.items()
        if c >= max(2, n_files * 0.5)
    )

    total_secs = sum(r.analysis_secs for r in results)

    return AggregateResult(
        file_count=n_files,
        aggregate_score=agg_score,
        aggregate_status=agg_status,
        critical_files=critical,
        warning_files=warning,
        healthy_files=healthy,
        common_patterns=common,
        per_file=results,
        analysis_secs_total=round(total_secs, 3),
    )


# ─────────────────────────────────────────────────────────────────────────────
# FULL PIPELINE ORCHESTRATOR (stateless)
# ─────────────────────────────────────────────────────────────────────────────

def score_log_file(
    file_path: str,
    raw_lines: List[str],
    report_pdf: str = "",
    plots_png:  str = "",
    _t_start:   float = 0.0,
) -> ScoreResult:
    """
    Run all 8 scoring stages on `raw_lines` and return a ScoreResult.

    This function is PURE with respect to scoring:
      • No file I/O (caller provides raw_lines).
      • No global mutable state.
      • SHA-256 hash of raw_lines content is the cache key.
      • Identical inputs → identical ScoreResult (except analysis_start/end timing).
    """
    import time
    t0 = _t_start or time.time()

    content_hash = compute_lines_hash(raw_lines)
    fname = raw_lines[0] if not file_path else __import__("os").path.basename(file_path)

    # Stage 1 — Parse
    entries = parse_logs_deterministic(raw_lines)
    if not entries:
        raise ValueError(f"No valid timestamped log events parsed from {file_path!r}")

    # Stage 2 — Window
    W = _window_size(len(entries))
    windows = _segment(entries, W)
    all_eids = sorted(set(e.event_id for e in entries))  # sorted = deterministic

    # Stage 3 — LSD + latencies
    lsd = _build_lsd(windows, all_eids)
    lats = _latencies(windows)

    # Stage 4 — Drift
    drift = _cosine_drift(lsd)
    d_avg = float(np.mean(drift))

    # Stage 5 — Entropy
    H = _entropy(lsd)

    # Stage 6 — Escalation
    grads, g_norm = _escalation(lats)

    # Stage 7 — Graph
    G = _build_graph(entries)
    rho = nx.density(G) if G.number_of_nodes() > 0 else 0.0

    # Stage 8 — Health Score
    health_score, status = compute_health_score_deterministic(d_avg, g_norm)

    # Event counts
    level_counts = Counter(e.level for e in entries)
    error_count = level_counts.get("ERROR", 0) + level_counts.get("FATAL", 0) + level_counts.get("CRITICAL", 0)
    warn_count  = level_counts.get("WARN", 0)
    info_count  = len(entries) - error_count - warn_count

    # Top-5 error templates (sorted for determinism)
    error_templates = Counter(
        e.event_template for e in entries
        if e.level in ("ERROR", "FATAL", "CRITICAL")
    )
    top5 = dict(error_templates.most_common(5))

    # Window metrics
    wm_list: List[WindowMetrics] = []
    for i, win in enumerate(windows):
        ec = sum(1 for e in win if e.level in ("ERROR","FATAL","CRITICAL"))
        wc = sum(1 for e in win if e.level == "WARN")
        wm_list.append(WindowMetrics(
            window_index=i,
            event_count=len(win),
            latency_s=float(lats[i]),
            entropy=float(H[i]),
            drift=float(drift[i]),
            error_count=ec,
            warn_count=wc,
            first_ts=win[0].timestamp,
            last_ts=win[-1].timestamp,
        ))

    # Root causes
    root_causes = extract_root_causes(
        entries, windows, drift, g_norm, d_avg, lsd, H, grads, G
    )

    drift_events = extract_drift_events(
        entries,
        windows,
        drift
    )

    t1 = time.time()
    return ScoreResult(
        file_path=file_path,
        file_name=__import__("os").path.basename(file_path),
        content_hash=content_hash,
        analysis_start=t0,
        analysis_end=t1,
        analysis_secs=round(t1 - t0, 3),
        health_score=health_score,
        status=status,
        score_formula=SCORE_FORMULA_STR,
        d_avg=round(d_avg, 4),
        g_norm=round(g_norm, 4),
        total_events=len(entries),
        error_count=error_count,
        warn_count=warn_count,
        info_count=info_count,
        graph_density=round(rho, 4),
        graph_nodes=G.number_of_nodes(),
        graph_edges=G.number_of_edges(),
        window_metrics=wm_list,
        top5_error_templates=top5,
        root_causes=root_causes,
        drift_events=drift_events,
        report_pdf=report_pdf,
        plots_png=plots_png,
    )


# ─────────────────────────────────────────────────────────────────────────────
# JSON SERIALISATION
# ─────────────────────────────────────────────────────────────────────────────

def score_result_to_dict(r: ScoreResult) -> dict:
    d = asdict(r)
    # Convert numpy types to native Python for JSON serialisation
    def _clean(obj):
        if isinstance(obj, (np.integer,)):     return int(obj)
        if isinstance(obj, (np.floating,)):    return float(obj)
        if isinstance(obj, np.ndarray):        return obj.tolist()
        if isinstance(obj, dict):              return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, list):              return [_clean(v) for v in obj]
        return obj
    return _clean(d)


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA EXAMPLES (for documentation)
# ─────────────────────────────────────────────────────────────────────────────
"""
SAMPLE LogEntry JSON:
{
  "line_number": 42,
  "raw_line": "2024-01-01 08:00:05 ERROR Database connection timeout after 5000 ms",
  "timestamp": 1704096005.0,
  "level": "ERROR",
  "event_template": "ERROR Database connection timeout after <*> ms",
  "event_id": "E3"
}

SAMPLE ScoreResult JSON (abbreviated):
{
  "file_path": "/logs/app.log",
  "file_name": "app.log",
  "content_hash": "a3f5c7...",
  "analysis_secs": 1.24,
  "health_score": 63.5,
  "status": "WARNING",
  "score_formula": "HS = max(0, 100 − 50·d̄ − 10·ḡ_norm)",
  "d_avg": 0.48,
  "g_norm": 0.12,
  "total_events": 1200,
  "error_count": 87,
  "warn_count": 143,
  "root_causes": [
    {
      "rank": 1,
      "factor": "High Cosine Drift",
      "metric_name": "d_avg",
      "metric_value": 0.48,
      "threshold": 0.10,
      "weight": 24.0,
      "evidence": [
        "Window 14: lines 701-750 [2024-01-01 08:23:11 → 08:23:59]  drift=0.89"
      ],
      "description": "Mean cosine drift 0.48 exceeds pre-anomaly threshold 0.38."
    }
  ]
}

SAMPLE AggregateResult JSON (abbreviated):
{
  "file_count": 3,
  "aggregate_score": 71.2,
  "aggregate_status": "WARNING",
  "critical_files": [],
  "warning_files": ["app.log", "worker.log"],
  "healthy_files": ["scheduler.log"],
  "common_patterns": ["ERROR Database connection timeout after <*> ms"],
  "analysis_secs_total": 4.71
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# HUMAN SUMMARY GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def generate_human_summary(result: dict) -> list:
    """
    Generate short human-readable summary bullets for GUI folder analysis.
    """

    bullets = []

    score = result.get("health_score", 0)
    status = result.get("status", "UNKNOWN")

    d_avg = result.get("d_avg", 0)
    g_norm = result.get("g_norm", 0)

    error_count = result.get("error_count", 0)
    warn_count = result.get("warn_count", 0)

    density = result.get("graph_density", 0)

    root_causes = result.get("root_causes", [])

    if status == "HEALTHY":

        bullets.append("Stable execution flow detected")
        bullets.append("No major behavioral drift observed")

        if density < 0.12:
            bullets.append("Sequential processing appears healthy")

        if g_norm < 0.05:
            bullets.append("No significant latency escalation")

        if error_count == 0:
            bullets.append("No ERROR or FATAL events detected")

        return bullets[:5]

    if d_avg >= 0.38:
        bullets.append(
            "Critical behavioral drift detected across analysis windows"
        )

    elif d_avg >= 0.10:
        bullets.append(
            "Non-trivial distributional shift detected in log patterns"
        )

    if g_norm >= 0.20:
        bullets.append(
            "Severe latency escalation detected"
        )

    elif g_norm >= 0.05:
        bullets.append(
            "Moderate execution slowdown observed"
        )

    if error_count > 0:

        if error_count >= 100:
            bullets.append(
                f"Heavy ERROR/FATAL activity ({error_count} events)"
            )

        else:
            bullets.append(
                f"ERROR activity detected ({error_count} events)"
            )

    if warn_count > 50:
        bullets.append(
            f"High WARN frequency detected ({warn_count} warnings)"
        )

    if density > 0.35:
        bullets.append(
            "Chaotic event-transition structure observed"
        )

    elif density < 0.12:
        bullets.append(
            "Sparse linear execution path detected"
        )

    for rc in root_causes[:3]:

        factor = rc.get("factor", "")

        if factor == "High Cosine Drift":
            bullets.append(
                "System behavior deviating from normal execution baseline"
            )

        elif factor == "Latency Escalation":
            bullets.append(
                "Execution time increasing between windows"
            )

        elif factor == "Error Burst":
            bullets.append(
                "Frequent repeated failures detected"
            )

        elif factor == "Entropy Spike":
            bullets.append(
                "Irregular event diversity spike observed"
            )

    final = []

    for b in bullets:
        if b not in final:
            final.append(b)

    return final[:5]

