from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_dashboard_console.py"
SPEC = importlib.util.spec_from_file_location("run_dashboard_console_module", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
DashboardRunManager = MODULE.DashboardRunManager


def test_dashboard_run_manager_persists_saved_defaults(tmp_path: Path) -> None:
    manager = DashboardRunManager(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )

    saved = manager.save_defaults(
        "market-paper",
        {"slug_prefix": "eth-updown-5m-", "max_cycles": 12, "starting_cash": 2000},
    )
    payload = manager.profiles_payload()
    market_profile = next(profile for profile in payload["profiles"] if profile["name"] == "market-paper")
    fields = {field["name"]: field for field in market_profile["fields"]}

    assert saved["defaults"]["slug_prefix"] == "eth-updown-5m-"
    assert fields["slug_prefix"]["saved_default"] == "eth-updown-5m-"
    assert fields["slug_prefix"]["value"] == "eth-updown-5m-"
    assert Path(payload["preferences_path"]).exists()


def test_dashboard_run_manager_records_last_used_values(tmp_path: Path, monkeypatch) -> None:
    manager = DashboardRunManager(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )

    class FakeProcess:
        pid = 12345
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(MODULE.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    status = manager.start("sim-paper", {"sheet": "BTC", "starting_cash": 4321})
    payload = manager.profiles_payload()
    sim_profile = next(profile for profile in payload["profiles"] if profile["name"] == "sim-paper")
    fields = {field["name"]: field for field in sim_profile["fields"]}

    assert status["running"] is True
    assert fields["starting_cash"]["last_used"] == 4321
    assert fields["starting_cash"]["value"] == 4321
