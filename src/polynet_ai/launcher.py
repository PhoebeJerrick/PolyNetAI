from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class LaunchField:
    name: str
    label: str
    kind: str
    default: Any
    description: str
    cli_flag: str
    min_value: float | int | None = None
    max_value: float | int | None = None
    step: float | int | None = None
    options: list[Any] = field(default_factory=list)
    required: bool = False
    example: str = ""
    detail: str = ""
    show_when: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "description": self.description,
            "cli_flag": self.cli_flag,
            "min": self.min_value,
            "max": self.max_value,
            "step": self.step,
            "options": list(self.options),
            "required": self.required,
            "example": self.example,
            "detail": self.detail,
            "show_when": dict(self.show_when),
        }


@dataclass(frozen=True, slots=True)
class LaunchProfile:
    name: str
    title: str
    description: str
    command: list[str]
    mode: str
    fields: list[LaunchField] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "mode": self.mode,
            "command": list(self.command),
            "command_text": format_command(self.command),
            "fields": [field.as_dict() for field in self.fields],
        }


def format_command(command: list[str]) -> str:
    return subprocess.list2cmdline([str(part) for part in command])


def _get_option(command: list[str], flag: str, default: Any = "") -> Any:
    try:
        index = command.index(flag)
    except ValueError:
        return default
    if index + 1 >= len(command):
        return default
    return command[index + 1]


def _set_option(command: list[str], flag: str, value: Any) -> list[str]:
    updated = list(command)
    string_value = str(value)
    try:
        index = updated.index(flag)
    except ValueError:
        updated.extend([flag, string_value])
        return updated
    if index + 1 >= len(updated):
        updated.append(string_value)
    else:
        updated[index + 1] = string_value
    return updated


def _set_checkbox_flag(command: list[str], flag: str, enabled: bool) -> list[str]:
    updated = [part for part in command if part != flag]
    if enabled:
        updated.append(flag)
    return updated


def _coerce_override(field: LaunchField, raw_value: Any) -> Any:
    if raw_value is None or raw_value == "":
        return field.default
    if field.kind == "checkbox":
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, (int, float)):
            return bool(raw_value)
        text = str(raw_value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{field.label} 必须是布尔值")
    if field.kind == "number":
        number = float(raw_value)
        if field.step is not None and float(field.step).is_integer():
            number = int(number)
        if field.min_value is not None and number < field.min_value:
            raise ValueError(f"{field.label} 不能小于 {field.min_value}")
        if field.max_value is not None and number > field.max_value:
            raise ValueError(f"{field.label} 不能大于 {field.max_value}")
        return number
    if field.kind == "select":
        text = str(raw_value)
        if field.options and text not in {str(option) for option in field.options}:
            raise ValueError(f"{field.label} 不在允许范围内")
        return text
    text = str(raw_value).strip()
    if field.required and not text:
        raise ValueError(f"{field.label} 不能为空")
    return text or field.default


def resolve_profile_values(profile: LaunchProfile, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    source = overrides or {}
    for launch_field in profile.fields:
        raw_value = source.get(launch_field.name, launch_field.default)
        resolved[launch_field.name] = _coerce_override(launch_field, raw_value)
    return resolved


def apply_profile_overrides(profile: LaunchProfile, overrides: dict[str, Any] | None = None) -> LaunchProfile:
    if not overrides:
        return profile
    updated_command = list(profile.command)
    resolved = resolve_profile_values(profile, overrides)
    for launch_field in profile.fields:
        if launch_field.name not in resolved:
            continue
        value = resolved[launch_field.name]
        if launch_field.kind == "checkbox":
            updated_command = _set_checkbox_flag(updated_command, launch_field.cli_flag, bool(value))
            continue
        if value is None:
            continue
        updated_command = _set_option(updated_command, launch_field.cli_flag, value)
    return LaunchProfile(
        name=profile.name,
        title=profile.title,
        description=profile.description,
        command=updated_command,
        mode=profile.mode,
        fields=profile.fields,
    )


def build_launch_profiles(
    *,
    root: Path,
    dashboard_dir: Path,
    python_executable: str | None = None,
) -> dict[str, LaunchProfile]:
    python_cmd = python_executable or sys.executable
    dashboard_output = str(dashboard_dir)
    return {
        "sim-paper": LaunchProfile(
            name="sim-paper",
            title="模拟下单测试",
            description="使用本地 record_job 下 btc-updown-5m-* 成交流回放做准实时 paper trading，并持续刷新当前 dashboard。",
            mode="paper_simulation",
            command=[
                python_cmd,
                "scripts/run_recorded_live_paper.py",
                "--input-dirs",
                "artifacts/live/record_job;artifacts/live/record_job/More_RawData",
                "--cycle-glob",
                "btc-updown-5m-*",
                "--max-cycles",
                "10",
                "--config",
                "configs/strategy.yaml",
                "--output-dir",
                dashboard_output,
                "--pace-factor",
                "1000000",
                "--status-every",
                "100",
                "--dashboard-refresh-seconds",
                "1",
                "--starting-cash",
                "100",
                "--capital-reset-mode",
                "fixed",
                "--per-cycle-cash",
                "100",
            ],
            fields=[
                LaunchField(
                    name="input_dirs",
                    label="数据源根目录（多路径）",
                    kind="text",
                    default="artifacts/live/record_job;artifacts/live/record_job/More_RawData",
                    description="逗号或分号分隔多个输入根目录；每个目录下会扫描匹配周期子目录并合并回放事件。",
                    cli_flag="--input-dirs",
                    required=False,
                    example="artifacts/live/record_job;artifacts/live/record_job_v2",
                    detail="用于“模拟下单回放测试”。多个根目录下可能出现同名 btc-updown-5m-* 子目录；流式报告里 cycle_slug 会用相对项目根的路径区分。",
                ),
                LaunchField(
                    name="cycle_glob",
                    label="周期目录匹配",
                    kind="text",
                    default="btc-updown-5m-*",
                    description="按目录名匹配要回放的周期集合。",
                    cli_flag="--cycle-glob",
                    required=True,
                    example="btc-updown-5m-*",
                    detail="默认扫描 input-dir 下所有匹配目录，并读取各目录内 ws_trade_events.ndjson 作为回放输入。",
                ),
                LaunchField(
                    name="starting_cash",
                    label="起始本金",
                    kind="number",
                    default=100,
                    description="资金曲线起始本金。",
                    cli_flag="--starting-cash",
                    min_value=10,
                    max_value=1000000,
                    step=10,
                    example="1000 表示资金曲线从 1000 开始",
                    detail="fixed 模式下，资金曲线从该值起步；每周期实际投注额由“每周期固定投注资金”决定。cumulative 模式下，账户从该值起步并跨周期滚动。",
                ),
                LaunchField(
                    name="capital_reset_mode",
                    label="周期资金处理模式",
                    kind="select",
                    default="fixed",
                    description="多周期测试中，是否每个周期重置资金为固定投注额。",
                    cli_flag="--capital-reset-mode",
                    options=["fixed", "cumulative"],
                    example="fixed — 每周期投注金额固定，盈亏累计到曲线",
                    detail="fixed: 每周期投注本金固定，周期盈亏累计到资金曲线；cumulative: 账户资金跨周期连续滚动。",
                ),
                LaunchField(
                    name="per_cycle_cash",
                    label="每周期固定投注资金",
                    kind="number",
                    default=100,
                    description="fixed 模式下每周期分配给策略的实际投注本金。",
                    cli_flag="--per-cycle-cash",
                    min_value=1,
                    max_value=1000000,
                    step=10,
                    example="100 — 起始本金=1000时，每周期投注100，曲线仍从1000起算",
                    detail="不填则默认等于起始本金。",
                    show_when={"capital_reset_mode": "fixed"},
                ),
                LaunchField(
                    name="max_cycles",
                    label="测试周期数",
                    kind="number",
                    default=10,
                    description="本次模拟最多回放多少个周期目录（按时间顺序取前 N 个）。",
                    cli_flag="--max-cycles",
                    min_value=1,
                    max_value=10000,
                    step=1,
                    example="10 表示只回放最早的 10 个 btc-updown-5m-* 周期目录",
                    detail="用于快速验证参数而不必跑完整历史；设大一些可做更长区间模拟。",
                ),
                LaunchField(
                    name="include_trade_process",
                    label="生成交易流水 Excel",
                    kind="checkbox",
                    default=False,
                    description="是否额外生成交易过程详细 Excel",
                    cli_flag="--include-trade-process",
                    example="勾选此项生成交易流水",
                    detail="不勾选只生成性能报告；勾选时会额外生成交易流水表，耗时较长。",
                ),
            ],
        ),
        "market-paper": LaunchProfile(
            name="market-paper",
            title="实盘行情验证（含自动对比）",
            description="连接 Polymarket 实时公开行情做 robot-mode paper 验证，完成后自动生成对比报告。不会真实发单。",
            mode="market_validation",
            command=[
                python_cmd,
                "-u",
                "scripts/run_polymarket_live_paper.py",
                "--robot-mode",
                "--slug-prefix",
                "btc-updown-5m-",
                "--max-cycles",
                "10",
                "--config",
                "configs/strategy.yaml",
                "--output-dir",
                dashboard_output,
                "--record-events-dir",
                "artifacts/live/record_job/record_job_market",
                "--dashboard-refresh-seconds",
                "1",
                "--starting-cash",
                "100",
                "--capital-reset-mode",
                "fixed",
                "--per-cycle-cash",
                "100",
            ],
            fields=[
                LaunchField(
                    name="record_events_dir",
                    label="实时事件落盘目录",
                    kind="text",
                    default="artifacts/live/record_job/record_job_market",
                    description="可选：把实时行情流按周期目录落盘，结构兼容 record_job，便于离线回放对比。",
                    cli_flag="--record-events-dir",
                    required=False,
                    example="artifacts/live/record_job/record_job_market",
                    detail="目录下会生成 btc-updown-5m-*/ws_trade_events.ndjson，可直接和模拟下单测试的数据源做同结构对比。",
                ),
                LaunchField(
                    name="slug_prefix",
                    label="市场前缀",
                    kind="text",
                    default="btc-updown-5m-",
                    description="机器人模式用它自动发现目标市场窗口，例如 btc-updown-5m-。",
                    cli_flag="--slug-prefix",
                    required=True,
                    example="btc-updown-5m- 会匹配 slug 以该串开头的当期合约",
                    detail="前缀错误会订阅不到目标市场；改品种或周期时须换成对应 slug 命名习惯（仍以官方接口为准）。",
                ),
                LaunchField(
                    name="max_cycles",
                    label="最大周期数",
                    kind="number",
                    default=10,
                    description="本次最多追踪多少个连续窗口。",
                    cli_flag="--max-cycles",
                    min_value=1,
                    max_value=500,
                    step=1,
                    example="10 表示连跑约 10 个 5 分钟窗后进程结束",
                    detail="用于控制单次验证时长与磁盘写入量；调大前注意网络与机器长期稳定性。",
                ),
                LaunchField(
                    name="starting_cash",
                    label="起始本金",
                    kind="number",
                    default=100,
                    description="paper 验证账户的初始资金。",
                    cli_flag="--starting-cash",
                    min_value=10,
                    max_value=1000000,
                    step=10,
                    example="100 — 与模拟下单、资金曲线展示一致",
                    detail="fixed 模式下，资金曲线从该值起步；每周期实际投注额由“每周期固定投注资金”决定。cumulative 模式下，账户从该值起步并跨周期滚动。",
                ),
                LaunchField(
                    name="capital_reset_mode",
                    label="周期资金处理模式",
                    kind="select",
                    default="fixed",
                    description="多周期行情验证中，是否每个周期重置资金。",
                    cli_flag="--capital-reset-mode",
                    options=["fixed", "cumulative"],
                    example="fixed — 每个周期用相同初始金额开始",
                    detail="fixed: 每周期投注本金固定为起始值，但周期盈亏会累计到资金曲线；cumulative: 账户资金跨周期连续滚动。",
                ),
                LaunchField(
                    name="per_cycle_cash",
                    label="每周期固定投注资金",
                    kind="number",
                    default=100,
                    description="fixed 模式下每周期分配给策略的实际投注本金。",
                    cli_flag="--per-cycle-cash",
                    min_value=1,
                    max_value=1000000,
                    step=10,
                    example="100 — 起始本金=1000时，每周期投注100，曲线仍从1000起算",
                    detail="不填则默认等于起始本金。",
                    show_when={"capital_reset_mode": "fixed"},
                ),
            ],
        ),
    }


def get_launch_profile(
    profile_name: str,
    *,
    root: Path,
    dashboard_dir: Path,
    python_executable: str | None = None,
) -> LaunchProfile:
    profiles = build_launch_profiles(
        root=root,
        dashboard_dir=dashboard_dir,
        python_executable=python_executable,
    )
    try:
        return profiles[profile_name]
    except KeyError as exc:
        available = ", ".join(sorted(profiles))
        raise KeyError(f"未知启动档案: {profile_name}。可选: {available}") from exc


def build_project_help_text(
    *,
    root: Path,
    dashboard_dir: Path,
    python_executable: str | None = None,
) -> str:
    python_cmd = python_executable or "python"
    launcher_cmd = f"{python_cmd} scripts/run_polynet.py"
    profiles = build_launch_profiles(
        root=root,
        dashboard_dir=dashboard_dir,
        python_executable=python_cmd,
    )
    lines = [
        "PolyNetAI 项目启动帮助",
        "",
        "推荐入口：",
        f"  {launcher_cmd} help",
        "",
        "常用命令：",
        f"  {launcher_cmd} profiles",
        f"  {launcher_cmd} start sim-paper",
        f"  {launcher_cmd} start market-paper",
        f"  {python_cmd} scripts/run_dashboard_console.py --dashboard-dir {dashboard_dir.as_posix()}",
        "",
        "启动档案说明：",
    ]
    for profile in profiles.values():
        lines.append(f"  - {profile.name}: {profile.title}")
        lines.append(f"    {profile.description}")
        lines.append(f"    {format_command(profile.command)}")
    lines.extend(
        [
            "",
            "说明：",
            "  - sim-paper: 使用本地 record_job 事件流做准实时模拟下单测试。",
            "  - market-paper: 接真实 Polymarket 行情做实盘验证，但仍是 paper 模式，不会真实下单。",
            "  - dashboard 控制台负责可视化改参数、查看状态，并可一键启动/停止上述运行档案。",
        ]
    )
    return "\n".join(lines) + "\n"
