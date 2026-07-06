# DyLoRISK v2 — Idempotent Health Scoring Architecture Plan

> **Idempotent, deterministic, root-cause-aware multi-file log analysis.**  
> Same input files → identical scores regardless of run order or repetition.

---

## 1. Root-Cause Analysis: Why v1 Is Non-Deterministic

The existing codebase has three non-determinism sources. Each is explained below with the exact fix applied in v2.

### 1.1 Non-Deterministic EventId Assignment

**Where:**  `dylorisk.py › parse_logs()` lines 186–203

```python
# v1 — insertion-order dict (BROKEN)
template_to_id: dict[str, str] = {}
counter = 0
for line in raw_lines:
    ...
    if template not in template_to_id:
        counter += 1
        template_to_id[template] = f"E{counter}"   # E1, E2, … by first-seen order
```

**Problem:** If two log files contain the same templates but in different line orders (e.g. file A has `ERROR timeout` on line 1, file B has it on line 50), the template maps to `E1` in file A but `E17` in file B. The LSD matrix columns differ, making drift vectors incomparable across files.

**Fix (v2):**
```python
# v2 — sort all templates first, then assign ids
all_templates = sorted(set(record[3] for record in raw_records))
template_to_id = {t: f"E{i+1}" for i, t in enumerate(all_templates)}
```
Same set of templates → same E-numbers, regardless of line order or file order.

---

### 1.2 Content Hash Using mtime + Size

**Where:** `dylorisk_gui.py › _file_hash()` line 93

```python
# v1 — mtime-based (BROKEN for idempotence)
raw = f"{path}|{st.st_mtime}|{st.st_size}"
return hashlib.md5(raw.encode()).hexdigest()
```

**Problem:** Copying a file or `touch`-ing it changes `st_mtime`. The cache treats it as a new file and re-analyzes it, potentially with a different result if any upstream dependency changed.

**Fix (v2):** SHA-256 of raw file bytes.
```python
def compute_content_hash(file_path: str) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
```
The hash is content-addressed. Rename, copy, touch → same hash. Only an actual byte-level change invalidates the cache.

---

### 1.3 Missing Aggregate Score

**Where:** v1 has no combined score across files.

**Problem:** Running folder analysis on 5 files gives 5 independent scores but no single "system health" number. The aggregate varies depending on which subset of files is shown.

**Fix (v2):** Weighted mean by event count.
```
AggScore = Σ(HS_i × N_i) / Σ(N_i)
```
Order-independent, monotone under addition of new files, and weighted so large log files have proportional influence.

---

## 2. Precise Health Score Model

### 2.1 Formula (unchanged from v1, now formally specified)

```
HS = max(0,  100 − 50 × d̄ − 10 × ḡ_norm)
```

| Symbol | Meaning | Range |
|--------|---------|-------|
| d̄ | Mean cosine drift between consecutive LSD window vectors | [0, 1] |
| ḡ_norm | Mean absolute latency gradient ÷ mean window latency | [0, ∞) |

Both inputs are pure functions of the sorted, templatized event stream. Same bytes → same d̄ and ḡ_norm → same HS.

### 2.2 Classification Thresholds

| Score | Status | Color |
|-------|--------|-------|
| > 80 | HEALTHY | `#2ecc71` |
| 50 – 80 | WARNING | `#f39c12` |
| < 50 | CRITICAL | `#e74c3c` |

### 2.3 Aggregate Score (new)

```
AggHS = Σ(HS_i × events_i) / Σ(events_i)
```

Classified using the same thresholds. Shown in the new "AGGREGATE HEALTH SUMMARY" panel.

---

## 3. Architecture

### 3.1 Module Map

```
┌─────────────────────────────────────────────────────────────────────┐
│                        dylorisk_gui_v2.py                           │
│  ┌─────────────────┐   ┌────────────────────────┐                  │
│  │  Tkinter UI     │   │  App Orchestrator       │                  │
│  │  (dark theme,   │──▶│  _analyze_file()        │                  │
│  │  unchanged      │   │  _auto_worker()         │                  │
│  │  layout)        │   │  _manual_worker()       │                  │
│  │                 │   │  _refresh_aggregate()   │                  │
│  └─────────────────┘   └───────────┬────────────┘                  │
└───────────────────────────────────┬┼─────────────────────────────  ┘
                                    ││
               ┌────────────────────┘└───────────────────┐
               │                                         │
   ┌───────────▼────────────┐             ┌──────────────▼──────────┐
   │  dylorisk_score_engine │             │    dylorisk_cache.py    │
   │  (NEW — pure functions)│             │  (SHA-256 content cache)│
   │                        │             │                         │
   │  parse_logs_           │             │  ScoreCache.get()       │
   │    deterministic()     │             │  ScoreCache.put()       │
   │  compute_health_score_ │             │  ScoreCache.clear()     │
   │    deterministic()     │             │  analyze_with_cache()   │
   │  extract_root_causes() │             └─────────────────────────┘
   │  compute_aggregate_    │
   │    score()             │
   │  score_log_file()      │
   └───────────┬────────────┘
               │
   ┌───────────▼────────────┐    ┌────────────────────────────────┐
   │     dylorisk.py        │    │       run_my_log.py            │
   │  (original pipeline,   │    │  normalize_hadoop_line()       │
   │  PDF + PNG generation) │    │  (unchanged)                   │
   └────────────────────────┘    └────────────────────────────────┘
               │
         ┌─────┴─────┐
         ▼           ▼
   reports/pdf/  reports/plots/
   *.pdf         *.png
```

### 3.2 Data Flow

```
Raw log file (bytes)
       │
       ▼
SHA-256 hash ──▶ cache lookup ──▶ HIT: return cached ScoreResult
       │                              (analysis_secs ≈ 0)
       │ MISS
       ▼
normalize_hadoop_line()          ← run_my_log.py
       │
       ▼
parse_logs_deterministic()       ← score_engine Stage 1
  • Templates sorted before EventId assignment
  • Timestamps via datetime.strptime (no locale)
  • Sort by (timestamp, line_number) — deterministic tie-breaking
       │
       ▼
segment_windows()   W = max(5, ⌊N/20⌋)
       │
       ├──▶ build_lsd_vectors()    Stage 3 — T×K matrix
       │         │
       │         ├──▶ compute_cosine_drift()    Stage 4 → d̄
       │         └──▶ compute_entropy()         Stage 5 → H(t)
       │
       ├──▶ compute_window_latency()  Stage 3 → Lat(t)
       │         └──▶ compute_escalation_score() Stage 6 → ḡ_norm
       │
       ├──▶ build_log_structure_graph()  Stage 7 → ρ
       │
       ├──▶ compute_health_score_deterministic(d̄, ḡ_norm)  Stage 8
       │         → HS, status
       │
       └──▶ extract_root_causes()  → List[RootCause]
                 │
                 ├── drift contribution + window evidence
                 ├── escalation contribution + window evidence
                 ├── error burst rate + template evidence
                 └── entropy spike windows
       │
       ▼
ScoreResult (dataclass)
       │
       ├──▶ cache.put(sha256_hash, score_dict)
       │
       └──▶ GUI display:
               • File list row (score, status, time)
               • Gauge (HS number + color)
               • Detail pane (full metrics + root causes)
               • Aggregate panel (refreshed)
```

---

## 4. Data Models

### 4.1 LogEntry
```json
{
  "line_number":    42,
  "raw_line":       "2024-01-01 08:00:05 ERROR DB timeout after 5000 ms",
  "timestamp":      1704096005.0,
  "level":          "ERROR",
  "event_template": "ERROR DB timeout after <*> ms",
  "event_id":       "E3"
}
```

### 4.2 WindowMetrics
```json
{
  "window_index": 14,
  "event_count":  10,
  "latency_s":    47.3,
  "entropy":      1.84,
  "drift":        0.82,
  "error_count":  3,
  "warn_count":   2,
  "first_ts":     1704097391.0,
  "last_ts":      1704097438.3
}
```

### 4.3 RootCause
```json
{
  "rank":         1,
  "factor":       "High Cosine Drift",
  "metric_name":  "d_avg",
  "metric_value": 0.4821,
  "threshold":    0.10,
  "weight":       24.1,
  "evidence": [
    "Window 14: lines 701-750 [2024-01-01 08:23:11 → 08:23:59]  drift=0.89",
    "Window 15: lines 751-800 [2024-01-01 08:24:02 → 08:25:11]  drift=0.71"
  ],
  "description": "Mean cosine drift 0.48 exceeds pre-anomaly threshold 0.38..."
}
```

### 4.4 ScoreResult (abbreviated)
```json
{
  "file_path":      "/logs/hadoop.log",
  "file_name":      "hadoop.log",
  "content_hash":   "a3f5c7d2e8...",
  "analysis_secs":  1.24,
  "health_score":   63.5,
  "status":         "WARNING",
  "score_formula":  "HS = max(0, 100 − 50·d̄ − 10·ḡ_norm)",
  "d_avg":          0.4821,
  "g_norm":         0.1200,
  "total_events":   1200,
  "error_count":    87,
  "warn_count":     143,
  "graph_density":  0.18,
  "top5_error_templates": {
    "ERROR DB timeout after <*> ms": 43,
    "ERROR service <*> unavailable": 22
  },
  "root_causes": [...]
}
```

### 4.5 AggregateResult
```json
{
  "file_count":       3,
  "aggregate_score":  71.2,
  "aggregate_status": "WARNING",
  "critical_files":   [],
  "warning_files":    ["hadoop.log", "worker.log"],
  "healthy_files":    ["scheduler.log"],
  "common_patterns":  ["ERROR DB timeout after <*> ms"],
  "analysis_secs_total": 4.71
}
```

### 4.6 Cache Entry (on disk in `.dylorisk_cache_v2.json`)
```json
{
  "a3f5c7d2e8...": {
    "_schema_version": 2,
    "_cached_at":      1720000000.0,
    "file_name":       "hadoop.log",
    "health_score":    63.5,
    "status":          "WARNING",
    "d_avg":           0.4821,
    "g_norm":          0.1200,
    "content_hash":    "a3f5c7d2e8...",
    "analysis_secs":   1.24,
    "root_causes":     [...],
    ...
  }
}
```

---

## 5. Determinism Guarantees (Proof Sketch)

| Guarantee | Mechanism |
|-----------|-----------|
| Same file content → same score | SHA-256 content hash; all arithmetic is float64 with fixed rounding |
| Order-independent analysis | Aggregate uses weighted mean; per-file scoring has no cross-file state |
| Re-run idempotence | Cache hit returns stored dict without re-computation |
| Template stability | EventIds assigned by lexicographically sorted template strings |
| Timestamp stability | `datetime.strptime` with explicit format string; no `dateutil` or locale parsing |
| Window stability | W = max(5, ⌊N/20⌋) is deterministic from N |
| Numpy float stability | All intermediate arrays are float64; `round(x, 2)` only at output boundary |

---

## 6. Pseudocode — Core Scoring Logic

```python
def score_deterministic(raw_lines: list[str]) -> ScoreResult:

    # Stage 1 — Parse (deterministic EventId assignment)
    records = [(lineno, ts, body, templatize(body))
               for lineno, line in enumerate(raw_lines, 1)
               if (parsed := parse_ts(line))]

    all_templates = sorted({r[3] for r in records})        # SORT = determinism
    template_to_id = {t: f"E{i+1}" for i, t in enumerate(all_templates)}
    entries = [LogEntry(..., event_id=template_to_id[r[3]]) for r in records]
    entries.sort(key=lambda e: (e.timestamp, e.line_number))  # deterministic

    # Stages 2-7 — pure numpy computation
    W        = max(5, len(entries) // 20)
    windows  = [entries[i:i+W] for i in range(0, len(entries), W) if len(entries[i:i+W]) >= 2]
    all_eids = sorted({e.event_id for e in entries})       # SORT = determinism
    lsd      = build_lsd(windows, all_eids)
    drift    = cosine_drift(lsd)
    H        = shannon_entropy(lsd)
    lats     = window_latencies(windows)
    grads, g_norm = escalation(lats)
    d_avg    = float(np.mean(drift))

    # Stage 8 — Health score (pure formula)
    hs  = round(max(0.0, 100.0 - 50.0 * d_avg - 10.0 * g_norm), 2)
    status = "HEALTHY" if hs > 80 else "WARNING" if hs >= 50 else "CRITICAL"

    # Root-cause extraction
    causes = extract_root_causes(entries, windows, drift, g_norm, d_avg, lsd, H, grads)

    return ScoreResult(health_score=hs, status=status, root_causes=causes, ...)
```

---

## 7. Pseudocode — Cache Mechanism

```python
def analyze_with_cache(file_path, raw_lines, cache, force=False):
    key = sha256_of_lines(raw_lines)        # content-addressed

    if not force:
        cached = cache.get(key)
        if cached is not None:
            return cached, was_cached=True  # idempotent fast-path

    result = score_deterministic(raw_lines)
    cache.put(key, result, force=force)     # write-once unless force
    return result, was_cached=False

class ScoreCache:
    def get(self, key):
        entry = self._data.get(key)
        if entry is None:                   return None
        if entry["_schema_version"] != V:  # schema changed
            del self._data[key];           return None
        if entry["content_hash"] != key:   # integrity fail
            del self._data[key];           return None
        return entry

    def put(self, key, entry, force=False):
        if key in self._data and not force: return   # already stored
        self._data[key] = {**entry, "_schema_version": V, "_cached_at": now()}
        self._save_atomic()   # tmp file → replace (atomic on POSIX)
```

---

## 8. Root-Cause Extraction Pseudocode

```python
def extract_root_causes(entries, windows, drift, g_norm, d_avg, lsd, H, grads):
    causes = []

    # Factor 1: Drift
    drift_penalty = 50.0 * d_avg
    if d_avg >= DRIFT_STABLE_THRESHOLD:    # 0.10
        top_windows = argsort(drift, descending=True)[:3]
        evidence = [f"Window {w}: lines {start}–{end} [{ts_start}→{ts_end}] drift={d:.4f}"
                    for w in top_windows]
        causes.append(RootCause("High Cosine Drift", d_avg, 0.10, drift_penalty, evidence))

    # Factor 2: Escalation
    esc_penalty = 10.0 * g_norm
    if g_norm >= ESC_MODERATE_THRESHOLD:   # 0.05
        top_windows = argsort(abs(grads), descending=True)[:3]
        evidence = [f"Window {w}: gradient={grads[w]:.2f}s" for w in top_windows]
        causes.append(RootCause("Latency Escalation", g_norm, 0.05, esc_penalty, evidence))

    # Factor 3: Error burst (informational, weight=0)
    error_rate = count(entries where level in ERROR/FATAL/CRITICAL) / len(entries)
    if error_rate > 0.10:
        top_templates = Counter(template for ERROR entries).most_common(3)
        evidence = [f"'{tmpl}' × {cnt} (first line {first_lineno})"
                    for tmpl, cnt in top_templates]
        causes.append(RootCause("Error Burst", error_rate, 0.10, 0.0, evidence))

    # Factor 4: Entropy spike (informational, weight=0)
    if std(H) > 0.5 * mean(H):
        spike_windows = [w for w if H[w] > mean(H) + 2*std(H)]
        evidence = [f"Window {w}: H={H[w]:.3f} lines {start}–{end}"
                    for w in spike_windows[:3]]
        causes.append(RootCause("Entropy Spike", std(H)/mean(H), 0.5, 0.0, evidence))

    causes.sort(key=lambda c: c.weight, descending=True)
    for i, c in enumerate(causes): c.rank = i + 1
    return causes
```

---

## 9. End-to-End Minimal Runnable Example

```python
# ── install once ──
# pip install numpy pandas scipy scikit-learn networkx matplotlib

from dylorisk_score_engine import score_log_file, compute_aggregate_score
from dylorisk_cache import ScoreCache, analyze_with_cache

SAMPLE_LOG = [
    "2024-01-01 08:00:01 INFO  User 123 logged in from IP 10.0.0.1",
    "2024-01-01 08:00:05 ERROR Database connection timeout after 5000 ms",
    "2024-01-01 08:00:09 WARN  Retry attempt 1 for service auth",
    "2024-01-01 08:00:14 ERROR Database connection timeout after 5001 ms",
    "2024-01-01 08:00:20 INFO  Request 456 processed in 210 ms",
    "2024-01-01 08:00:31 ERROR Service auth unavailable — connection refused",
]

cache = ScoreCache("/tmp/demo_cache.json")

# ── First run: computes from scratch ──────────────────────────────────────────
result, was_cached = analyze_with_cache(
    file_path="demo.log",
    raw_lines=SAMPLE_LOG,
    cache=cache,
)
print(f"Score: {result['health_score']}  Status: {result['status']}")
print(f"Cached: {was_cached}")  # → False

# ── Second run: identical result from cache ───────────────────────────────────
result2, was_cached2 = analyze_with_cache(
    file_path="demo.log",
    raw_lines=SAMPLE_LOG,
    cache=cache,
)
print(f"Score: {result2['health_score']}  Status: {result2['status']}")
print(f"Cached: {was_cached2}")  # → True
assert result['health_score'] == result2['health_score']  # IDEMPOTENT ✓

# ── Root causes ───────────────────────────────────────────────────────────────
for rc in result.get("root_causes", []):
    print(f"\n#{rc['rank']} {rc['factor']}")
    print(f"    {rc['metric_name']}={rc['metric_value']}  penalty={rc['weight']:.1f}pts")
    for ev in rc['evidence'][:2]:
        print(f"    → {ev}")

# ── Aggregate across multiple files ──────────────────────────────────────────
from dylorisk_score_engine import ScoreResult, compute_aggregate_score
# (in practice, collect ScoreResult objects from multiple analyze_with_cache calls)
```

**Expected output (approximate):**
```
Score: 72.5  Status: WARNING
Cached: False
Score: 72.5  Status: WARNING
Cached: True

#1 High Cosine Drift
    d_avg=0.3812  penalty=19.1pts
    → Window 0: lines 1-6 [2024-01-01 08:00:01 → 08:00:31]  drift=0.38

#2 Error Burst
    error_rate=0.5000  penalty=0.0pts
    → 'ERROR Database connection timeout after <*> ms' × 2 (first line 2)
```

---

## 10. New GUI Features Summary

All additions are **additive** — the existing layout, dark theme, colours, and fonts are unchanged.

| Location | Addition | Purpose |
|----------|----------|---------|
| File list header | ⌫ Clear History button | Remove all cached results with one click |
| File list columns | New "Time (s)" column | Shows wall-clock analysis duration per file |
| File list "Status" | `⚡WARNING` prefix | Indicates result came from cache (near-instant) |
| Right panel (new row) | AGGREGATE HEALTH SUMMARY card | Weighted-mean score + per-status counts + contributing factor list |
| Detail pane | ROOT CAUSES section | Ranked list with metric values, thresholds, score penalty, and evidence pointers (file lines, timestamp ranges) |
| Detail pane | ANALYSIS TIMING row | Shows exact seconds taken |
| Detail pane | SHA-256 hash (first 16 chars) | Allows manual cache debugging |

---

## 11. Migration from v1

| Step | Action |
|------|--------|
| 1 | Copy `dylorisk_score_engine.py` and `dylorisk_cache.py` to your app folder |
| 2 | Replace `dylorisk_gui.py` with `dylorisk_gui_v2.py` |
| 3 | Keep `dylorisk.py` and `run_my_log.py` unchanged (still used for PDF/PNG) |
| 4 | Delete `.dylorisk_state.json` (old mtime-based cache, no longer used) |
| 5 | New cache file: `.dylorisk_cache_v2.json` (created automatically) |
| 6 | `pip install numpy pandas scipy scikit-learn networkx matplotlib` (same deps) |

---

## 12. File Deliverables

| File | Role |
|------|------|
| `dylorisk_score_engine.py` | Deterministic scoring — data models, all 8 stages, root-cause extraction, aggregate score |
| `dylorisk_cache.py` | SHA-256 content-addressed JSON cache with atomic writes |
| `dylorisk_gui_v2.py` | Enhanced GUI (same layout + aggregate panel, root causes, timing, clear history) |
| `ARCHITECTURE_PLAN.md` | This document |

> `dylorisk.py` and `run_my_log.py` are **unchanged** — they continue to generate PDFs and PNGs.
