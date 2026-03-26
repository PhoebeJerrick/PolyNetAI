from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.config_io import load_yaml_mapping, write_yaml_mapping  # noqa: E402
from polynet_ai.launcher import apply_profile_overrides, build_launch_profiles, build_project_help_text, format_command, resolve_profile_values  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地 dashboard 参数控制台")
    parser.add_argument("--dashboard-dir", default="artifacts/live/polymarket_btc_10cycles")
    parser.add_argument("--strategy-config", default="configs/strategy.yaml")
    parser.add_argument("--sweep-config", default="configs/sweep.yaml")
    parser.add_argument("--optimize-config", default="configs/optimize.yaml")
    parser.add_argument("--python-executable", default=sys.executable)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--pid-file", default=None, help="将本进程 PID 写入此文件，便于外部管理")
    return parser.parse_args()


class DashboardRunManager:
    def __init__(self, *, root: Path, dashboard_dir: Path, python_executable: str) -> None:
        self._root = root
        self._dashboard_dir = dashboard_dir
        self._python_executable = python_executable
        self._prefs_path = dashboard_dir / "launcher_prefs.json"
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._profile_name: str | None = None
        self._profile_title: str | None = None
        self._command: list[str] | None = None
        self._log_path: Path | None = None
        self._log_handle = None
        self._last_exit_code: int | None = None

    def profiles_payload(self) -> dict[str, Any]:
        profiles = self._profiles_with_preferences()
        preferences = self._load_preferences()
        return {
            "help_command": format_command(
                [self._python_executable, "scripts/run_polynet.py", "--dashboard-dir", str(self._dashboard_dir), "help"]
            ),
            "help_text": build_project_help_text(
                root=self._root,
                dashboard_dir=self._dashboard_dir,
                python_executable=self._python_executable,
            ),
            "profiles": profiles,
            "preferences": preferences,
            "preferences_path": str(self._prefs_path),
            "status": self.status_payload(),
        }

    def _base_profiles(self):
        return build_launch_profiles(
            root=self._root,
            dashboard_dir=self._dashboard_dir,
            python_executable=self._python_executable,
        )

    def _load_preferences(self) -> dict[str, Any]:
        if not self._prefs_path.exists():
            return {"defaults": {}, "last_used": {}}
        try:
            payload = json.loads(self._prefs_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {"defaults": {}, "last_used": {}}
        if not isinstance(payload, dict):
            return {"defaults": {}, "last_used": {}}
        defaults = payload.get("defaults")
        last_used = payload.get("last_used")
        return {
            "defaults": defaults if isinstance(defaults, dict) else {},
            "last_used": last_used if isinstance(last_used, dict) else {},
        }

    def _write_preferences(self, preferences: dict[str, Any]) -> None:
        self._prefs_path.parent.mkdir(parents=True, exist_ok=True)
        self._prefs_path.write_text(
            json.dumps(preferences, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _profile_value_map(self, profile_name: str, profile, preferences: dict[str, Any]) -> dict[str, Any]:
        defaults = preferences.get("defaults", {}).get(profile_name, {})
        last_used = preferences.get("last_used", {}).get(profile_name, {})
        merged = {}
        if isinstance(defaults, dict):
            merged.update(defaults)
        if isinstance(last_used, dict):
            merged.update(last_used)
        return resolve_profile_values(profile, merged)

    def _profiles_with_preferences(self) -> list[dict[str, Any]]:
        preferences = self._load_preferences()
        profiles = self._base_profiles()
        payload: list[dict[str, Any]] = []
        for profile_name, profile in profiles.items():
            profile_dict = profile.as_dict()
            effective_values = self._profile_value_map(profile_name, profile, preferences)
            default_values = preferences.get("defaults", {}).get(profile_name, {})
            last_used_values = preferences.get("last_used", {}).get(profile_name, {})
            fields = []
            for field in profile.fields:
                field_dict = field.as_dict()
                field_dict["value"] = effective_values.get(field.name, field.default)
                field_dict["saved_default"] = default_values.get(field.name)
                field_dict["last_used"] = last_used_values.get(field.name)
                fields.append(field_dict)
            profile_dict["fields"] = fields
            payload.append(profile_dict)
        return payload

    def save_defaults(self, profile_name: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        profiles = self._base_profiles()
        profile = profiles.get(profile_name)
        if profile is None:
            raise ValueError(f"未知启动档案: {profile_name}")
        resolved = resolve_profile_values(profile, overrides or {})
        with self._lock:
            preferences = self._load_preferences()
            defaults = preferences.setdefault("defaults", {})
            defaults[profile_name] = resolved
            self._write_preferences(preferences)
        return {"profile": profile_name, "defaults": resolved, "preferences_path": str(self._prefs_path)}

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            return self._status_locked()

    def start(self, profile_name: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        profiles = self._base_profiles()
        profile = profiles.get(profile_name)
        if profile is None:
            raise ValueError(f"未知启动档案: {profile_name}")
        with self._lock:
            preferences = self._load_preferences()
            defaults = preferences.get("defaults", {}).get(profile_name, {})
            merged_overrides = {}
            if isinstance(defaults, dict):
                merged_overrides.update(defaults)
            if overrides:
                merged_overrides.update(overrides)
            resolved = resolve_profile_values(profile, merged_overrides)
            preferences.setdefault("last_used", {})[profile_name] = resolved
            self._write_preferences(preferences)
        profile = apply_profile_overrides(profile, resolved)
        with self._lock:
            self._refresh_locked()
            if self._process is not None:
                raise RuntimeError("已有任务正在运行，请先停止当前任务。")
            logs_dir = self._dashboard_dir / "runtime_logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / f"{profile.name}.log"
            log_handle = log_path.open("a", encoding="utf-8")
            log_handle.write(f"\n=== START {profile.title} ===\n")
            log_handle.write(format_command(profile.command) + "\n")
            log_handle.flush()
            self._process = subprocess.Popen(
                profile.command,
                cwd=self._root,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._profile_name = profile.name
            self._profile_title = profile.title
            self._command = list(profile.command)
            self._log_path = log_path
            self._log_handle = log_handle
            self._last_exit_code = None
            return self._status_locked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if self._process is None:
                return self._status_locked()
            process = self._process
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            self._last_exit_code = process.returncode
            self._close_log_locked()
            self._process = None
            return self._status_locked()

    def shutdown(self) -> None:
        with self._lock:
            self._refresh_locked()
            if self._process is not None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
                self._last_exit_code = self._process.returncode
                self._process = None
            self._close_log_locked()

    def _refresh_locked(self) -> None:
        if self._process is None:
            return
        exit_code = self._process.poll()
        if exit_code is None:
            return
        self._last_exit_code = exit_code
        self._process = None
        self._close_log_locked()

    def _close_log_locked(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.flush()
                self._log_handle.close()
            finally:
                self._log_handle = None

    def _status_locked(self) -> dict[str, Any]:
        running = self._process is not None
        return {
            "running": running,
            "profile_name": self._profile_name or "",
            "profile_title": self._profile_title or "",
            "pid": self._process.pid if self._process is not None else None,
            "command_text": format_command(self._command or []) if self._command else "",
            "log_path": str(self._log_path) if self._log_path else "",
            "last_exit_code": self._last_exit_code,
            "dashboard_dir": str(self._dashboard_dir),
        }


class DashboardConsoleHandler(SimpleHTTPRequestHandler):
    server_version = "PolynetDashboardConsole/0.1"

    def __init__(
        self,
        *args,
        directory: str,
        config_paths: dict[str, Path],
        run_manager: DashboardRunManager,
        **kwargs,
    ) -> None:
        self._config_paths = config_paths
        self._run_manager = run_manager
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body_json(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(size) if size > 0 else b"{}"
        payload = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON body 必须是对象")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return
        if parsed.path == "/api/configs":
            payload = {
                "configs": [
                    {
                        "name": name,
                        "path": str(path),
                        "exists": path.exists(),
                    }
                    for name, path in self._config_paths.items()
                ]
            }
            self._send_json(payload)
            return
        if parsed.path == "/api/launcher":
            self._send_json(self._run_manager.profiles_payload())
            return
        if parsed.path == "/api/launcher/status":
            self._send_json(self._run_manager.status_payload())
            return
        if parsed.path.startswith("/api/config/"):
            name = parsed.path.rsplit("/", 1)[-1]
            path = self._config_paths.get(name)
            if path is None:
                self._send_json({"error": f"未知配置: {name}"}, status=HTTPStatus.NOT_FOUND)
                return
            if not path.exists():
                self._send_json({"error": f"配置文件不存在: {path}"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(
                {
                    "name": name,
                    "path": str(path),
                    "data": load_yaml_mapping(path),
                    "last_modified_ms": int(path.stat().st_mtime * 1000),
                }
            )
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/launcher/defaults":
            try:
                payload = self._read_body_json()
                profile = str(payload.get("profile") or "").strip()
                if not profile:
                    raise ValueError("profile 字段不能为空")
                overrides = payload.get("overrides") or {}
                if not isinstance(overrides, dict):
                    raise ValueError("overrides 字段必须是对象")
                self._send_json({"ok": True, "saved": self._run_manager.save_defaults(profile, overrides)})
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/launcher/start":
            try:
                payload = self._read_body_json()
                profile = str(payload.get("profile") or "").strip()
                if not profile:
                    raise ValueError("profile 字段不能为空")
                overrides = payload.get("overrides") or {}
                if not isinstance(overrides, dict):
                    raise ValueError("overrides 字段必须是对象")
                self._send_json({"ok": True, "status": self._run_manager.start(profile, overrides)})
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        if parsed.path == "/api/launcher/stop":
            self._send_json({"ok": True, "status": self._run_manager.stop()})
            return
        if not parsed.path.startswith("/api/config/"):
            self._send_json({"error": "不支持的接口"}, status=HTTPStatus.NOT_FOUND)
            return
        name = parsed.path.rsplit("/", 1)[-1]
        path = self._config_paths.get(name)
        if path is None:
            self._send_json({"error": f"未知配置: {name}"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_body_json()
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("data 字段必须是对象")
            write_yaml_mapping(path, data)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {
                "ok": True,
                "name": name,
                "path": str(path),
                "last_modified_ms": int(path.stat().st_mtime * 1000),
            }
        )


def main() -> int:
    args = parse_args()
    dashboard_dir = (ROOT / args.dashboard_dir).resolve()
    config_paths = {
        "strategy": (ROOT / args.strategy_config).resolve(),
        "sweep": (ROOT / args.sweep_config).resolve(),
        "optimize": (ROOT / args.optimize_config).resolve(),
    }
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    run_manager = DashboardRunManager(
        root=ROOT,
        dashboard_dir=dashboard_dir,
        python_executable=args.python_executable,
    )

    def _handler(*handler_args, **handler_kwargs):
        return DashboardConsoleHandler(
            *handler_args,
            directory=str(dashboard_dir),
            config_paths=config_paths,
            run_manager=run_manager,
            **handler_kwargs,
        )

    server = ThreadingHTTPServer((args.host, args.port), _handler)

    # 写入 PID 文件（使用 os.getpid() 保证是操作系统原生 PID）
    pid_file_path = Path(args.pid_file) if args.pid_file else None
    if pid_file_path is not None:
        pid_file_path.parent.mkdir(parents=True, exist_ok=True)
        pid_file_path.write_text(str(os.getpid()), encoding="utf-8")

    print(f"Dashboard 控制台已启动: http://{args.host}:{args.port}/dashboard.html")
    print(f"服务目录: {dashboard_dir}")
    for name, path in config_paths.items():
        print(f"- {name}: {path}")
    print(f"- help: {args.python_executable} scripts/run_polynet.py --dashboard-dir {dashboard_dir} help")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard 控制台已停止。")
    finally:
        run_manager.shutdown()
        server.server_close()
        # 仅在 PID 文件内容仍记录的是自己时才删除，避免新进程的 PID 文件被旧进程 finally 误删
        if pid_file_path is not None:
            try:
                stored = pid_file_path.read_text(encoding="utf-8").strip()
                if stored == str(os.getpid()):
                    pid_file_path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
