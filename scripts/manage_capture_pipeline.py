from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
    return value if value > 0 else None


def _read_json(path: Path) -> dict[str, object] | list[object] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _tail_lines(path: Path, count: int = 5) -> list[str]:
    if not path.exists():
        return []
    lines = [line.rstrip("\n") for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    lines = [line for line in lines if line.strip()]
    return lines[-count:]


def _run_command(command: list[str], *, cwd: Path) -> int:
    print("执行命令:")
    print("  " + " ".join(shlex.quote(part) for part in command))
    completed = subprocess.run(command, cwd=str(cwd))
    return int(completed.returncode)


def _build_capture_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-u",
        str(ROOT / "scripts" / "capture_polymarket_ws_events.py"),
        "--output-dir",
        args.output_dir,
        "--slug-prefix",
        args.slug_prefix,
        "--max-cycles",
        str(args.max_cycles),
        "--status-every",
        str(args.status_every),
        "--poll-interval-seconds",
        str(args.poll_interval_seconds),
        "--ping-interval-seconds",
        str(args.ping_interval_seconds),
        "--receive-timeout-seconds",
        str(args.receive_timeout_seconds),
        "--cycle-grace-seconds",
        str(args.cycle_grace_seconds),
        "--start-buffer-seconds",
        str(args.start_buffer_seconds),
        "--env-file",
        args.env_file,
        "--account-index",
        str(args.account_index),
    ]
    if args.market_slugs:
        command.append("--market-slugs")
        command.extend(args.market_slugs)
    if args.market_slugs_file:
        command.extend(["--market-slugs-file", args.market_slugs_file])
    return command


def _build_batch_replay_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-u",
        str(ROOT / "scripts" / "batch_replay_recorded_trade_events.py"),
        "--input-dir",
        args.output_dir,
        "--config",
        args.config,
        "--starting-cash",
        str(args.starting_cash),
    ]
    if args.overrides:
        command.extend(["--overrides", args.overrides])
    if args.batch_output_dir:
        command.extend(["--output-dir", args.batch_output_dir])
    return command


def _build_pipeline_child_argv(raw_argv: list[str]) -> list[str]:
    child_args: list[str] = []
    skip_next = False
    value_flags = {"--log-file", "--pid-file"}
    for index, token in enumerate(raw_argv):
        if skip_next:
            skip_next = False
            continue
        if token == "--daemonize":
            continue
        if any(token.startswith(flag + "=") for flag in value_flags):
            continue
        if token in value_flags:
            if index + 1 < len(raw_argv):
                skip_next = True
            continue
        child_args.append(token)
    return [sys.executable, "-u", str(Path(__file__).resolve())] + child_args


def _launch_pipeline_in_background(args: argparse.Namespace, raw_argv: list[str]) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log_file) if args.log_file else output_dir / "pipeline.log"
    pid_path = Path(args.pid_file) if args.pid_file else output_dir / "pipeline.pid"
    meta_path = output_dir / "pipeline_background_meta.json"

    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    existing_pid = _read_pid(pid_path)
    if existing_pid and _is_process_running(existing_pid):
        raise RuntimeError(
            f"检测到已有后台流水线仍在运行，pid={existing_pid}。"
            f"如需停止，请执行 `python scripts/manage_capture_pipeline.py stop --output-dir {args.output_dir}`。"
        )
    if pid_path.exists():
        pid_path.unlink(missing_ok=True)

    command = _build_pipeline_child_argv(raw_argv)
    quoted_command = " ".join(shlex.quote(part) for part in command)
    with log_path.open("a", encoding="utf-8") as log_fh:
        log_fh.write(f"\n[{time.strftime('%Y-%m-%dT%H:%M:%S')}] launch: {quoted_command}\n")
        log_fh.flush()
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    pid_path.write_text(str(process.pid), encoding="utf-8")
    meta_path.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "log_file": str(log_path),
                "pid_file": str(pid_path),
                "output_dir": args.output_dir,
                "slug_prefix": args.slug_prefix,
                "max_cycles": args.max_cycles,
                "config": args.config,
                "overrides": args.overrides,
                "starting_cash": args.starting_cash,
                "command": command,
                "launched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("后台流水线已启动。")
    print(f"PID: {process.pid}")
    print(f"日志: {log_path}")
    print(f"PID 文件: {pid_path}")
    print(f"查看状态: python scripts/manage_capture_pipeline.py status --output-dir {args.output_dir}")
    print(f"停止任务: python scripts/manage_capture_pipeline.py stop --output-dir {args.output_dir}")
    return 0


def _load_manifest_rows(output_dir: Path) -> list[dict[str, object]]:
    payload = _read_json(output_dir / "capture_manifest.json")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _print_status(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    capture_pid_path = output_dir / "capture.pid"
    pipeline_pid_path = output_dir / "pipeline.pid"
    capture_meta = _read_json(output_dir / "capture_background_meta.json")
    pipeline_meta = _read_json(output_dir / "pipeline_background_meta.json")
    capture_pid = _read_pid(capture_pid_path)
    pipeline_pid = _read_pid(pipeline_pid_path)
    capture_running = bool(capture_pid and _is_process_running(capture_pid))
    pipeline_running = bool(pipeline_pid and _is_process_running(pipeline_pid))

    manifest_rows = _load_manifest_rows(output_dir)
    completed_cycles = len(manifest_rows)
    total_events = sum(int(row.get("event_count", 0) or 0) for row in manifest_rows)
    last_cycle = manifest_rows[-1] if manifest_rows else None

    print("## 任务状态")
    print(f"- 输出目录: {output_dir}")
    print(f"- 抓取进程: {'运行中' if capture_running else '未运行'}" + (f" (pid={capture_pid})" if capture_pid else ""))
    print(f"- 流水线进程: {'运行中' if pipeline_running else '未运行'}" + (f" (pid={pipeline_pid})" if pipeline_pid else ""))

    target_cycles = None
    if isinstance(pipeline_meta, dict):
        target_cycles = pipeline_meta.get("max_cycles")
    elif isinstance(capture_meta, dict):
        target_cycles = capture_meta.get("max_cycles")

    print("")
    print("## 抓取进度")
    print(f"- 已完成周期数: {completed_cycles}" + (f" / {target_cycles}" if target_cycles is not None else ""))
    print(f"- 已记录事件数: {total_events}")
    if last_cycle is not None:
        print(
            f"- 最近完成周期: {last_cycle.get('cycle_id')} | "
            f"events={last_cycle.get('event_count')} | "
            f"{last_cycle.get('first_timestamp')} -> {last_cycle.get('last_timestamp')}"
        )

    batch_dir = output_dir / "batch_replay_outputs"
    summary_csv = batch_dir / "batch_replay_summary.csv"
    report_md = batch_dir / "batch_replay_performance_report_zh.md"
    if summary_csv.exists():
        summary_df = pd.read_csv(summary_csv)
        print("")
        print("## 回放与报告")
        print(f"- 已回放周期数: {len(summary_df)}")
        if not summary_df.empty and "total_net_profit" in summary_df.columns:
            total_profit = float(pd.to_numeric(summary_df["total_net_profit"], errors="coerce").fillna(0.0).sum())
            print(f"- 批量回放总净利润: {total_profit:.6f}")
        print(f"- 总报告: {'已生成' if report_md.exists() else '未生成'}")
        print(f"- 回放目录: {batch_dir}")

    capture_log = output_dir / "capture.log"
    pipeline_log = output_dir / "pipeline.log"
    recent_lines = _tail_lines(pipeline_log if pipeline_log.exists() else capture_log, count=args.tail_lines)
    if recent_lines:
        print("")
        print("## 最近日志")
        for line in recent_lines:
            print(f"- {line}")
    return 0


def _terminate_pid(pid: int, *, timeout_seconds: float) -> bool:
    if not _is_process_running(pid):
        return True
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        if not _is_process_running(pid):
            return True
        time.sleep(0.2)
    return False


def _stop_jobs(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    stopped_any = False
    failures: list[str] = []

    targets = [
        ("流水线", output_dir / "pipeline.pid"),
        ("抓取", output_dir / "capture.pid"),
    ]

    for label, pid_path in targets:
        pid = _read_pid(pid_path)
        if not pid:
            continue
        if not _is_process_running(pid):
            pid_path.unlink(missing_ok=True)
            continue
        print(f"正在停止{label}进程 pid={pid} ...")
        stopped_any = True
        if _terminate_pid(pid, timeout_seconds=args.timeout_seconds):
            pid_path.unlink(missing_ok=True)
            print(f"- 已停止 {label}进程")
        else:
            failures.append(f"{label} pid={pid}")

    if failures:
        raise RuntimeError("以下进程在超时时间内未退出: " + ", ".join(failures))
    if not stopped_any:
        print("没有检测到正在运行的抓取或流水线进程。")
    return 0


def _run_full_pipeline(args: argparse.Namespace) -> int:
    capture_command = _build_capture_command(args)
    batch_command = _build_batch_replay_command(args)

    exit_code = _run_command(capture_command, cwd=ROOT)
    if exit_code != 0:
        return exit_code
    return _run_command(batch_command, cwd=ROOT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理 Polymarket websocket 抓取与离线回放流水线")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_capture_args(target: argparse.ArgumentParser) -> None:
        target.add_argument("--output-dir", required=True)
        target.add_argument("--slug-prefix", default="btc-updown-5m-")
        target.add_argument("--max-cycles", type=int, default=10)
        target.add_argument("--status-every", type=int, default=100)
        target.add_argument("--poll-interval-seconds", type=float, default=3.0)
        target.add_argument("--ping-interval-seconds", type=float, default=10.0)
        target.add_argument("--receive-timeout-seconds", type=float, default=1.0)
        target.add_argument("--cycle-grace-seconds", type=float, default=0.0)
        target.add_argument("--start-buffer-seconds", type=float, default=2.0)
        target.add_argument("--market-slugs", nargs="*", default=None)
        target.add_argument("--market-slugs-file", default=None)
        target.add_argument("--env-file", default=str(ROOT.parent / "APIs" / "ApiConfig.env"))
        target.add_argument("--account-index", type=int, default=2)

    start_parser = subparsers.add_parser("start", help="后台开始抓数据")
    add_capture_args(start_parser)

    status_parser = subparsers.add_parser("status", help="查看后台运行状态和进度")
    status_parser.add_argument("--output-dir", required=True)
    status_parser.add_argument("--tail-lines", type=int, default=5)

    stop_parser = subparsers.add_parser("stop", help="一键停止进程")
    stop_parser.add_argument("--output-dir", required=True)
    stop_parser.add_argument("--timeout-seconds", type=float, default=10.0)

    run_full_parser = subparsers.add_parser("run-full", help="一键抓取并直到生成业绩报告")
    add_capture_args(run_full_parser)
    run_full_parser.add_argument("--config", default="configs/strategy.yaml")
    run_full_parser.add_argument("--overrides", default=None)
    run_full_parser.add_argument("--starting-cash", type=float, default=100.0)
    run_full_parser.add_argument("--batch-output-dir", default=None)
    run_full_parser.add_argument("--daemonize", action="store_true", help="将整条流水线放到后台运行")
    run_full_parser.add_argument("--log-file", default=None, help="后台流水线日志文件，默认 <output-dir>/pipeline.log")
    run_full_parser.add_argument("--pid-file", default=None, help="后台流水线 PID 文件，默认 <output-dir>/pipeline.pid")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "start":
        command = _build_capture_command(args)
        command.insert(3, "--daemonize")
        if _run_command(command, cwd=ROOT) != 0:
            raise RuntimeError("后台抓取启动失败。")
        return 0
    if args.command == "status":
        return _print_status(args)
    if args.command == "stop":
        return _stop_jobs(args)
    if args.command == "run-full":
        if args.daemonize:
            return _launch_pipeline_in_background(args, sys.argv[1:])
        return _run_full_pipeline(args)

    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
