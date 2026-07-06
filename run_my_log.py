"""
run_my_log.py  —  DyLoRISK launcher for Hadoop/YARN log files.

Hadoop log format:
    2015-10-18 18:01:47,978 INFO [main] org.apache.hadoop.xxx.ClassName: message text

Usage (Windows PowerShell):
    python run_my_log.py
    python run_my_log.py Hadoop_2k.log
    python run_my_log.py Hadoop_2k.log MyReport.pdf
    python run_my_log.py logs
"""

import re
import sys
import os
import datetime

from dylorisk import run_dylorisk

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Resolve the DyLoRISK package location robustly.
FILES_DIR = SCRIPT_DIR
if not os.path.exists(os.path.join(FILES_DIR, "dylorisk.py")):
    candidate = os.path.join(SCRIPT_DIR, "files")
    if os.path.exists(os.path.join(candidate, "dylorisk.py")):
        FILES_DIR = candidate
    else:
        parent = os.path.dirname(SCRIPT_DIR)
        if os.path.exists(os.path.join(parent, "dylorisk.py")):
            FILES_DIR = parent
        elif os.path.exists(os.path.join(parent, "files", "dylorisk.py")):
            FILES_DIR = os.path.join(parent, "files")

LOGS_DIR = os.path.join(FILES_DIR, "logs")
REPORTS_DIR = os.path.join(FILES_DIR, "reports")

if os.path.exists(os.path.join(FILES_DIR, "dylorisk.py")):
    sys.path.insert(0, FILES_DIR)
else:
    raise FileNotFoundError(f"Could not locate dylorisk.py in {SCRIPT_DIR} or adjacent folders")


# ── Hadoop timestamp patterns ─────────────────────────────────────────────────
# Captures common Hadoop formats such as:
#   2015-10-18 18:01:47,978 INFO [main] org.apache.hadoop.xxx.ClassName: message
#   081109 203615 148 INFO dfs.DataNode$PacketResponder: message
HADOOP_PAT = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}),\d+\s+"   # date + time,ms
    r"(\w+)\s+"                                             # LEVEL
    r"(?:\[([^\]]*)\]\s+)?"                               # optional [thread]
    r"[\w\.$]+:\s*(.*)"                                     # Logger: message
)
HADOOP_SHORT_PAT = re.compile(
    r"^(\d{2})(\d{2})(\d{2})\s+"                          # yymmdd
    r"(\d{2})(\d{2})(\d{2})\s+"                            # HHMMSS
    r"\d+\s+"                                                # ms / counter
    r"(\w+)\s+"                                              # LEVEL
    r"(?:\[([^\]]*)\]\s+)?"                               # optional [thread]
    r"([\w\.$]+):\s*(.*)"""
)


def normalize_hadoop_line(line: str) -> str:
    """
    Convert many Hadoop log formats to the DyLoRISK canonical form:
        YYYY-MM-DD HH:MM:SS  LEVEL  message_body

    Lines that don't match are returned unchanged.
    """
    text = line.strip()
    m = HADOOP_PAT.match(text)
    if m:
        date, time_part, level, _, body = m.groups()
        clean_body = body.strip()
        return f"{date} {time_part} {level} {clean_body}"

    m = HADOOP_SHORT_PAT.match(text)
    if m:
        yy, mm, dd, hh, mi, ss, level, _, _, body = m.groups()
        try:
            normalized_date = datetime.datetime.strptime(f"{yy}{mm}{dd}", "%y%m%d").date()
        except ValueError:
            return line
        clean_body = body.strip()
        return f"{normalized_date} {hh}:{mi}:{ss} {level} {clean_body}"

    return line


def load_and_normalize(log_path: str) -> list[str]:
    """Load a log file and normalize lines to canonical DyLoRISK format."""
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        raw_lines = [line.rstrip("\n") for line in f]

    print(f"  Loaded {len(raw_lines):,} raw lines from '{os.path.basename(log_path)}'")

    normalized = [normalize_hadoop_line(l) for l in raw_lines]

    print("  Sample normalized lines:")
    for line in normalized[:3]:
        print(f"    {line}")
    return normalized


def discover_log_files(arg: str | None = None) -> list[str]:
    """Return a list of log files to process.

    If `arg` is omitted, discover all files in the default logs folder.
    If `arg` is a directory, use its contents. If it is a file path,
    process that individual file.
    """
    if arg is None:
        source_dir = LOGS_DIR
        if not os.path.isdir(source_dir):
            raise FileNotFoundError(f"Logs folder not found: {source_dir}")
        return [os.path.join(source_dir, fn)
                for fn in sorted(os.listdir(source_dir))
                if os.path.isfile(os.path.join(source_dir, fn))]

    if os.path.isdir(arg):
        return [os.path.join(arg, fn)
                for fn in sorted(os.listdir(arg))
                if os.path.isfile(os.path.join(arg, fn))]

    if os.path.isfile(arg):
        return [arg]

    candidate = os.path.join(LOGS_DIR, arg)
    if os.path.isfile(candidate):
        return [candidate]

    raise FileNotFoundError(f"Log file or folder not found: {arg}")


def ensure_reports_folder() -> None:
    """Create the reports directory if it doesn't exist."""
    os.makedirs(REPORTS_DIR, exist_ok=True)


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log_arg = sys.argv[1] if len(sys.argv) > 1 else None
    custom_report = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        log_files = discover_log_files(log_arg)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        print("Usage:  python run_my_log.py [log_file|log_folder] [output.pdf]")
        sys.exit(1)

    if not log_files:
        print(f"ERROR: no log files found in '{LOGS_DIR}'")
        sys.exit(1)

    ensure_reports_folder()

    for log_file in log_files:
        base_name = os.path.splitext(os.path.basename(log_file))[0]
        if custom_report and len(log_files) == 1:
            report_pdf = custom_report
            plots_png = report_pdf.replace(".pdf", "_plots.png")
        else:
            report_pdf = os.path.join(REPORTS_DIR, f"{base_name}_DyLoRISK_Report.pdf")
            plots_png = os.path.join(REPORTS_DIR, f"{base_name}_DyLoRISK_plots.png")

        print("\nDyLoRISK — Log Analyzer")
        print(f"Input  : {log_file}")
        print(f"Report : {report_pdf}\n")

        normalized = load_and_normalize(log_file)

        results = run_dylorisk(
            raw_lines=normalized,
            report_path=report_pdf,
            plots_path=plots_png,
        )

        hs = results["health_score"]
        print(f"\n{'='*55}")
        print(f"  Final Health Score : {hs['health_score']} / 100")
        print(f"  System Status      : {hs['status']}")
        print(f"  PDF Report         : {report_pdf}")
        print(f"  Plots PNG          : {plots_png}")
        print(f"{'='*55}\n")
