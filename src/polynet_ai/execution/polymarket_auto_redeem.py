"""
结算后自动 redeem：Data API 发现可赎回仓位 + Polymarket Relayer 执行 `redeemPositions`（gasless）。

需安装可选依赖：`pip install -e ".[redeem]"`（web3、py-builder-relayer-client、py-builder-signing-sdk）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from polynet_ai.adapters.polymarket_live import get_account_env_value
from polynet_ai.adapters.polymarket_redeem_api import fetch_redeemable_positions_aggregated

# Polygon 主网 Conditional Tokens（Polymarket 标准）
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PARENT_COLLECTION_ID_BYTES = bytes(32)

CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

CTF_VIEW_ABI = [
    {
        "inputs": [
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSet", "type": "uint256"},
        ],
        "name": "getCollectionId",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "getOutcomeSlotCount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "collectionId", "type": "bytes32"},
        ],
        "name": "getPositionId",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

DEFAULT_RELAYER_URL = "https://relayer-v2.polymarket.com"
DEFAULT_RPC_URL = "https://polygon-rpc.com"
FAIL_COOLDOWN_SEC = 3600.0

_fail_cooldown: dict[str, float] = {}
_log_once_relayer_import = False


def _normalize_condition_hex(condition_id: str) -> str:
    s = str(condition_id).strip()
    if s.startswith("0x"):
        return s
    return "0x" + s


def _to_bytes32(hex_str: str) -> bytes:
    s = str(hex_str).strip()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 64:
        raise ValueError(f"conditionId 应为 32 字节 hex，实际长度 {len(s)}")
    return bytes.fromhex(s)


def on_chain_redeemable_balance(
    condition_id: str,
    owner_address: str,
    *,
    rpc_url: str = DEFAULT_RPC_URL,
) -> int:
    """
    链上 outcome token 余额总和；>0 可尝试 redeem。
    返回 -1 表示 RPC/解码失败（调用方勿当作已赎回）。
    """
    try:
        from web3 import Web3
    except ImportError:
        return -1
    try:
        cond_bytes = _to_bytes32(_normalize_condition_hex(condition_id))
    except ValueError:
        return -1
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 12}))
        if not w3.is_connected():
            return -1
        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_ABI + CTF_VIEW_ABI,
        )
        owner = Web3.to_checksum_address(owner_address)
        try:
            outcome_n = int(ctf.functions.getOutcomeSlotCount(cond_bytes).call())
        except Exception:  # noqa: BLE001
            outcome_n = 2
        if outcome_n <= 0 or outcome_n > 256:
            outcome_n = 2
        total = 0
        for i in range(outcome_n):
            index_set = 1 << i
            coll_id = ctf.functions.getCollectionId(PARENT_COLLECTION_ID_BYTES, cond_bytes, index_set).call()
            pos_id = ctf.functions.getPositionId(Web3.to_checksum_address(USDC_E_ADDRESS), coll_id).call()
            total += ctf.functions.balanceOf(owner, pos_id).call()
        return int(total)
    except Exception:  # noqa: BLE001
        return -1


def _encode_redeem_calldata(condition_id: str) -> bytes:
    from web3 import Web3

    cond_bytes = _to_bytes32(_normalize_condition_hex(condition_id))
    w3 = Web3()
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    args = [
        Web3.to_checksum_address(USDC_E_ADDRESS),
        PARENT_COLLECTION_ID_BYTES,
        cond_bytes,
        [1, 2],
    ]
    # web3.py v7: encode_abi 从 ContractFunction 移至 Contract 对象
    hex_data: str = ctf.encode_abi("redeemPositions", args=args)
    return bytes.fromhex(hex_data.removeprefix("0x"))


def redeem_condition_via_relayer(
    *,
    private_key: str,
    builder_api_key: str,
    builder_secret: str,
    builder_passphrase: str,
    condition_id: str,
    slug: str = "",
    relayer_url: str = DEFAULT_RELAYER_URL,
    chain_id: int = 137,
) -> bool:
    """单 condition 调用 Relayer 执行 redeemPositions。"""
    global _log_once_relayer_import
    pk = (private_key or "").strip()
    if not pk.startswith("0x"):
        pk = "0x" + pk
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import OperationType, SafeTransaction
        try:
            from py_builder_signing_sdk import BuilderApiKeyCreds, BuilderConfig
        except ImportError:
            from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig
    except ImportError:
        if not _log_once_relayer_import:
            _log_once_relayer_import = True
            print(
                "[redeem] 未安装 redeem 可选依赖，跳过 Relayer。"
                " 请执行: pip install -e \".[redeem]\"",
                flush=True,
            )
        return False

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=builder_api_key,
            secret=builder_secret,
            passphrase=builder_passphrase,
        )
    )
    client = RelayClient(relayer_url, chain_id, pk, builder_config)
    redeem_data = _encode_redeem_calldata(condition_id)
    label = (slug or condition_id)[:48]
    redeem_tx = SafeTransaction(
        to=CTF_ADDRESS,
        operation=OperationType.Call,
        data=redeem_data,
        value="0",
    )
    response = client.execute([redeem_tx], f"Redeem positions {label}")
    result = response.wait()
    if result and str(result.get("state", "")).upper() in ("STATE_CONFIRMED", "STATE_MINED"):
        return True
    return False


@dataclass(slots=True)
class RedeemScanReport:
    """单次 `run_auto_redeem_scan` 的结果，用于报表/Excel 审计。"""

    success_count: int
    data_api_group_count: int
    condition_rows: list[dict[str, Any]]


@dataclass(slots=True)
class AutoRedeemSettings:
    relayer_url: str
    rpc_url: str
    chain_id: int
    private_key: str
    purse_address: str
    builder_api_key: str
    builder_secret: str
    builder_passphrase: str


def load_auto_redeem_settings(values: dict[str, str], *, account_index: int) -> AutoRedeemSettings | None:
    pk = get_account_env_value(values, "PURSE_PRIVATE_KEY", account_index=account_index) or ""
    purse = get_account_env_value(values, "PURSE_ADDRESS", account_index=account_index) or ""
    b_key = get_account_env_value(values, "POLY_BUILDER_API_KEY", account_index=account_index) or ""
    b_secret = (
        get_account_env_value(values, "POLY_BUILDER_API_SECRET", account_index=account_index)
        or get_account_env_value(values, "POLY_BUILDER_SECRET", account_index=account_index)
        or ""
    )
    b_pass = (
        get_account_env_value(values, "POLY_BUILDER_API_PASSPHRASE", account_index=account_index)
        or get_account_env_value(values, "POLY_BUILDER_PASSPHRASE", account_index=account_index)
        or ""
    )
    if not (pk and purse.startswith("0x") and b_key and b_secret and b_pass):
        return None
    relayer = (get_account_env_value(values, "POLY_RELAYER_URL", account_index=account_index) or "").strip()
    rpc = (get_account_env_value(values, "POLYGON_RPC_URL", account_index=account_index) or "").strip()
    chain_raw = get_account_env_value(values, "POLY_CHAIN_ID", account_index=account_index) or "137"
    try:
        chain_id = int(str(chain_raw).strip())
    except ValueError:
        chain_id = 137
    return AutoRedeemSettings(
        relayer_url=relayer or DEFAULT_RELAYER_URL,
        rpc_url=rpc or DEFAULT_RPC_URL,
        chain_id=chain_id,
        private_key=pk.strip(),
        purse_address=purse.strip(),
        builder_api_key=b_key.strip(),
        builder_secret=b_secret.strip(),
        builder_passphrase=b_pass.strip(),
    )


def run_auto_redeem_scan(
    settings: AutoRedeemSettings,
    *,
    priority_condition_ids: list[str] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> RedeemScanReport:
    """
    扫描 Data API 可赎回项并逐个尝试 Relayer redeem。
    `priority_condition_ids` 非空时优先尝试这些 condition（例如刚结束的周期）。
    返回 `RedeemScanReport`（含每条 condition 处理结果，便于写入 Excel）。
    """
    log = log_fn or (lambda m: print(m, flush=True))
    audit: list[dict[str, Any]] = []
    try:
        items = fetch_redeemable_positions_aggregated(settings.purse_address)
    except Exception as exc:  # noqa: BLE001 — 网络抖动不应拖垮整段 live paper 主流程
        log(f"[redeem] Data API 拉取可赎回仓位失败，已跳过本次扫描: {exc}")
        return RedeemScanReport(
            success_count=0,
            data_api_group_count=0,
            condition_rows=[
                {
                    "condition_id": "",
                    "slug": "",
                    "outcome": "data_api_request_failed",
                    "detail": str(exc)[:500],
                }
            ],
        )
    if not items:
        log("[redeem] Data API 无可赎回仓位 (redeemable=0)")
        return RedeemScanReport(
            success_count=0,
            data_api_group_count=0,
            condition_rows=[
                {
                    "condition_id": "",
                    "slug": "",
                    "outcome": "data_api_empty",
                    "detail": "Data API 返回无可赎回仓位",
                }
            ],
        )

    prio = {_normalize_condition_hex(c) for c in (priority_condition_ids or []) if c}

    def _sort_key(entry: dict[str, Any]) -> tuple[int, float]:
        cid = str(entry.get("condition_id") or "")
        cid_n = _normalize_condition_hex(cid) if cid else ""
        in_prio = 0 if cid_n in prio else 1
        payout = float(entry.get("expected_payout") or 0.0)
        return (in_prio, -payout)

    items.sort(key=_sort_key)
    n_groups = len(items)
    now = time.time()
    success = 0
    for entry in items:
        cid = str(entry.get("condition_id") or "")
        slug = str(entry.get("slug") or "")
        if not cid:
            continue
        cid_n = _normalize_condition_hex(cid)
        exp = float(entry.get("expected_payout") or 0.0)
        if exp <= 0:
            audit.append(
                {
                    "condition_id": cid_n,
                    "slug": slug,
                    "outcome": "skip_non_positive_expected",
                    "detail": f"expected_payout={exp:g}",
                }
            )
            continue
        last_fail = _fail_cooldown.get(cid_n, 0.0)
        if now - last_fail < FAIL_COOLDOWN_SEC:
            audit.append(
                {
                    "condition_id": cid_n,
                    "slug": slug,
                    "outcome": "skip_fail_cooldown",
                    "detail": f"cooldown<{FAIL_COOLDOWN_SEC:g}s",
                }
            )
            continue
        bal = on_chain_redeemable_balance(cid_n, settings.purse_address, rpc_url=settings.rpc_url)
        if bal == 0:
            audit.append(
                {
                    "condition_id": cid_n,
                    "slug": slug,
                    "outcome": "skip_zero_onchain_balance",
                    "detail": "",
                }
            )
            continue
        if bal < 0:
            log(f"[redeem] 链上余额未知，仍尝试 Relayer: {slug or cid_n[:18]}…")

        try:
            ok = redeem_condition_via_relayer(
                private_key=settings.private_key,
                builder_api_key=settings.builder_api_key,
                builder_secret=settings.builder_secret,
                builder_passphrase=settings.builder_passphrase,
                condition_id=cid_n,
                slug=slug,
                relayer_url=settings.relayer_url,
                chain_id=settings.chain_id,
            )
        except Exception as exc:  # noqa: BLE001
            ok = False
            log(f"[redeem] Relayer 异常 condition={cid_n[:16]}…: {exc}")
            audit.append(
                {
                    "condition_id": cid_n,
                    "slug": slug,
                    "outcome": "relayer_error",
                    "detail": str(exc)[:500],
                }
            )
            _fail_cooldown[cid_n] = now
            continue
        if ok:
            success += 1
            log(f"[redeem] 已赎回 condition={cid_n[:20]}… slug={slug or '—'}")
            audit.append(
                {
                    "condition_id": cid_n,
                    "slug": slug,
                    "outcome": "success",
                    "detail": "",
                }
            )
        else:
            _fail_cooldown[cid_n] = now
            log(f"[redeem] 赎回未确认或失败: {slug or cid_n[:18]}…")
            audit.append(
                {
                    "condition_id": cid_n,
                    "slug": slug,
                    "outcome": "relayer_not_confirmed",
                    "detail": "",
                }
            )

    if success:
        log(f"[redeem] 本轮成功赎回 {success} 个 condition")
    return RedeemScanReport(
        success_count=success,
        data_api_group_count=n_groups,
        condition_rows=audit,
    )


def redeem_report_to_audit_rows(
    report: RedeemScanReport,
    *,
    utc_start: datetime,
    utc_end: datetime,
    trigger: str,
    finalized_cycle_slug: str = "",
    priority_condition_id: str = "",
) -> list[dict[str, Any]]:
    """将单次扫描结果展开为多行，便于写入 CSV/Excel（每 condition 一行，共享同一时间窗与 trigger）。"""
    dur = (utc_end - utc_start).total_seconds()
    out: list[dict[str, Any]] = []
    rows = report.condition_rows
    if not rows:
        rows = [
            {
                "condition_id": "",
                "slug": "",
                "outcome": "no_detail_rows",
                "detail": "",
            }
        ]
    for cr in rows:
        out.append(
            {
                "utc_time_start": utc_start.isoformat(),
                "utc_time_end": utc_end.isoformat(),
                "duration_seconds": round(dur, 4),
                "trigger": trigger,
                "finalized_cycle_slug": finalized_cycle_slug,
                "priority_condition_id": priority_condition_id,
                "scan_success_count": report.success_count,
                "data_api_group_count": report.data_api_group_count,
                "condition_id": str(cr.get("condition_id") or ""),
                "redeem_market_slug": str(cr.get("slug") or ""),
                "outcome": str(cr.get("outcome") or ""),
                "detail": str(cr.get("detail") or ""),
            }
        )
    return out
