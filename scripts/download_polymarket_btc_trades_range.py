"""
下载 Polymarket BTC 5 分钟市场（btc-updown-5m-<unix_ts>）在指定日期区间内的公开成交，
并导出为可被 `polynet_ai.adapters.excel_loader.load_excel_events` 读取的 xlsx/csv。

加速要点（默认开启）：
1. 先枚举时间窗内全部 `btc-updown-5m-<ts>` slug，再用 Gamma `/markets?slug=...&slug=...` **批量拉元数据**（约 8640→~200 次 HTTP，而非逐条 slug）。
2. data-api `/trades` 单次 `limit` 默认 **10000**（官方上限），显著减少分页。
3. 线程池并发拉成交（`--workers` 默认 12）；遇连接被重置会自动退避重试。

`--discover slug`：逐条 `markets/slug/<x>`（最慢，仅排查）。`--workers 1`：串行拉成交。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

EXCEL_MAX_ROWS = 1_048_576

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.polymarket_live import (
    GAMMA_API_BASE,
    PolymarketMarketSpec,
    apply_proxy_env_from_dict,
    fetch_market_spec,
    load_api_env,
)
from polynet_ai.reporting.excel_export import get_version_tag


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="下载 BTC 5m Polymarket 历史成交区间")
    p.add_argument("--days", type=float, default=30.0, help="回溯天数（默认 30）")
    p.add_argument("--slug-prefix", default="btc-updown-5m-", help="市场 slug 前缀")
    p.add_argument(
        "--output-prefix",
        default="polymarket_btc_5m_range",
        help="输出文件名前缀（不含扩展名），放在 data/raw/",
    )
    p.add_argument(
        "--discover",
        choices=("batch", "slug", "auto", "gamma"),
        default="batch",
        help="batch：枚举 slug 后 Gamma 多 slug 批量查元数据（推荐）；slug：逐条 slug；auto/gamma 同 batch（兼容旧参数）",
    )
    p.add_argument(
        "--slug-batch-size",
        type=int,
        default=40,
        help="每个 Gamma /markets 请求携带多少个 slug（过大若 400 可改为 20）",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=12,
        help="并发线程数（1=完全串行）。默认 12；过大易触发远端断开(如 WinError 10054)，可降到 6～8。",
    )
    p.add_argument(
        "--market-retries",
        type=int,
        default=6,
        help="单个市场整段拉取失败（连接被重置等）时的重试次数，带指数退避",
    )
    p.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.0,
        help="串行模式下每个市场后的休眠；并发模式默认 0。",
    )
    p.add_argument(
        "--page-limit",
        type=int,
        default=10000,
        help="data-api /trades 每页条数，官方最大 10000（默认用满以少分页）",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=12,
        help="单市场最多分页次数（limit=10000 时多数市场 1～2 页即可）",
    )
    p.add_argument("--resume", action="store_true", help="从上次进度继续")
    p.add_argument(
        "--quiet",
        action="store_true",
        help="少打印日志",
    )
    return p.parse_args()


def _exception_chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = [exc]
    cur: BaseException | None = exc
    while cur.__cause__ is not None:
        cur = cur.__cause__
        out.append(cur)
    return out


def _is_transient_http_error(exc: BaseException) -> bool:
    """连接被远端/本机软件中止、超时、429/503 等可重试（含 __cause__ 链）。"""
    for e in _exception_chain(exc):
        if isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError)):
            return True
        if isinstance(e, requests.exceptions.ConnectionError):
            return True
        if isinstance(e, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return True
        if isinstance(e, OSError):
            we = getattr(e, "winerror", None)
            # 10053 本机软件中止连接；10054 远程强迫关闭（常见于代理/防火墙/限流）
            if we in (10053, 10054):
                return True
            if getattr(e, "errno", None) in (104,):  # ECONNRESET
                return True
        if isinstance(e, requests.exceptions.HTTPError):
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code in {429, 502, 503, 504}:
                return True
    return False


def _session(*, pool_maxsize: int = 16) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "PolyNetAI/0.1", "Accept": "application/json"})
    # 与 urllib3 协同：连接错误 / 部分状态码自动重试
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=0.5,
        status_forcelist=(429, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=min(pool_maxsize, 32), pool_maxsize=pool_maxsize)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    if os.environ.get("POLYNET_DOWNLOAD_CLOSE", "").strip().lower() in {"1", "true", "yes"}:
        s.headers["Connection"] = "close"
    return s


def _http_get_json(url: str, *, params: object = None, max_tries: int = 12) -> object:
    """带退避的 GET JSON（Gamma / data-api 通用）。"""
    session = _session(pool_maxsize=12)
    for attempt in range(max_tries):
        if attempt:
            time.sleep(min(90.0, 0.45 * (2 ** (attempt - 1)) + random.uniform(0, 0.35)))
        try:
            resp = session.get(url, params=params, timeout=(25, 120))
            resp.raise_for_status()
            return resp.json()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if attempt >= max_tries - 1 or not _is_transient_http_error(exc):
                raise
    raise RuntimeError("_http_get_json: unreachable")


def fetch_market_spec_with_retry(slug: str, *, max_tries: int = 12) -> PolymarketMarketSpec:
    """包装 polymarket_live.fetch_market_spec，对 10053/连接错误退避重试。"""
    last: BaseException | None = None
    for attempt in range(max_tries):
        if attempt:
            time.sleep(min(90.0, 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.4)))
        try:
            return fetch_market_spec(slug)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            last = exc
            if not _is_transient_http_error(exc) or attempt >= max_tries - 1:
                raise
    assert last is not None
    raise last


def fetch_specs_via_slug_batches(slugs: list[str], *, batch_size: int) -> list[PolymarketMarketSpec]:
    """
    Gamma `GET /markets` 支持同一查询参数多个 `slug`，一次返回多个市场 JSON，
    将元数据请求从 len(slugs) 降到约 len(slugs)/batch_size。
    """
    by_slug: dict[str, PolymarketMarketSpec] = {}
    n = len(slugs)
    for i in range(0, n, batch_size):
        batch = slugs[i : i + batch_size]
        params: list[tuple[str, str]] = [("slug", s) for s in batch]
        params.append(("limit", str(max(len(batch), 1))))
        time.sleep(0.12)  # 略降速，减轻代理/防火墙对 burst 的断开（10053）
        try:
            data = _http_get_json(f"{GAMMA_API_BASE}/markets", params=params)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            print(
                f"[discover/batch] 批量 {batch_size} 失败 ({exc!s})，改单条重试本批…",
                flush=True,
            )
            for slug in batch:
                try:
                    spec = fetch_market_spec_with_retry(slug)
                    by_slug[spec.slug] = spec
                except BaseException as e2:
                    if isinstance(e2, (KeyboardInterrupt, SystemExit)):
                        raise
                    continue
            print(f"[discover/batch] {min(i + batch_size, n)}/{n}", flush=True)
            continue

        markets = data if isinstance(data, list) else []
        if not markets and len(batch) > 1:
            print(
                f"[discover/batch] 本批无返回，改单条重试 {len(batch)} 个 slug…",
                flush=True,
            )
            for slug in batch:
                try:
                    spec = fetch_market_spec_with_retry(slug)
                    by_slug[spec.slug] = spec
                except BaseException as e2:
                    if isinstance(e2, (KeyboardInterrupt, SystemExit)):
                        raise
                    continue
            print(f"[discover/batch] {min(i + batch_size, n)}/{n} slugs 已请求", flush=True)
            continue
        for market in markets:
            if not isinstance(market, dict):
                continue
            slug = str(market.get("slug") or "")
            if not slug:
                continue
            try:
                spec = PolymarketMarketSpec.from_market_json(market)
            except ValueError:
                continue
            by_slug[slug] = spec
        print(f"[discover/batch] {min(i + batch_size, n)}/{n} slugs 已请求", flush=True)

    return sorted(by_slug.values(), key=lambda s: s.slug)


def enumerate_expected_slugs(slug_prefix: str, start_ts_floor: int, end_ts_ceil: int) -> list[str]:
    slugs: list[str] = []
    ts = start_ts_floor
    while ts < end_ts_ceil:
        slugs.append(f"{slug_prefix}{ts}")
        ts += 300
    return slugs


def discover_specs_via_slug_list(slugs: list[str]) -> list[PolymarketMarketSpec]:
    out: list[PolymarketMarketSpec] = []
    for i, slug in enumerate(slugs):
        if i % 500 == 0:
            print(f"[discover/slug] {i + 1}/{len(slugs)} {slug}", flush=True)
        try:
            out.append(fetch_market_spec_with_retry(slug))
        except Exception:
            continue
    return out


def collect_specs(
    args: argparse.Namespace,
    start_ts_floor: int,
    end_ts_ceil: int,
) -> list[PolymarketMarketSpec]:
    slugs_full = enumerate_expected_slugs(args.slug_prefix, start_ts_floor, end_ts_ceil)
    mode = args.discover
    if mode in ("auto", "gamma"):
        mode = "batch"

    if mode == "slug":
        return discover_specs_via_slug_list(slugs_full)

    specs = fetch_specs_via_slug_batches(slugs_full, batch_size=max(1, args.slug_batch_size))
    expected_n = len(slugs_full)
    print(
        f"[discover] 批量元数据命中 {len(specs)}/{expected_n} 个 slug（其余多为历史窗口无市场或已下架）",
        flush=True,
    )
    return specs


def fetch_trades_pages(
    session: requests.Session,
    condition_id: str,
    *,
    page_limit: int,
    max_pages: int,
    start_ts: int,
    end_ts: int,
    log_slug: str | None = None,
) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    page_count = 0
    while page_count < max_pages:
        url = "https://data-api.polymarket.com/trades?" + urlencode(
            {"market": condition_id, "limit": page_limit, "offset": offset}
        )
        payload: list | None = None
        last_exc: Exception | None = None
        for attempt in range(12):
            if attempt:
                time.sleep(min(45.0, 0.35 * (2 ** (attempt - 1)) + random.uniform(0, 0.25)))
            try:
                resp = session.get(url, timeout=(15, 90))
                if resp.status_code == 400:
                    payload = None
                    break
                resp.raise_for_status()
                data = resp.json()
                payload = data if isinstance(data, list) else None
                break
            except Exception as exc:
                last_exc = exc
                if not _is_transient_http_error(exc) or attempt == 11:
                    raise
        else:
            if last_exc is not None:
                raise last_exc

        if payload is None:
            break
        if not payload:
            break

        n_before = len(rows)
        page_count += 1
        for row in payload:
            try:
                ts = int(row.get("timestamp"))
            except Exception:
                continue
            if not (start_ts <= ts < end_ts):
                continue
            rows.append(row)

        if log_slug:
            matched = len(rows) - n_before
            print(
                f"  [trades] {log_slug} 第{page_count}/{max_pages}页 offset={offset} "
                f"本页{len(payload)}条 窗口内+{matched} 累计{len(rows)}",
                flush=True,
            )

        if len(payload) < page_limit:
            break
        offset += len(payload)
        # data-api offset 上限常为 10000；再大可能 400
        if offset > 100000:
            break
        time.sleep(0.02)

    return rows


def spec_time_window(spec: PolymarketMarketSpec, slug_fallback: str) -> tuple[datetime, datetime]:
    cycle_start = spec.start_time
    cycle_end = spec.end_time
    if cycle_start is None or cycle_end is None:
        base_ts = int(slug_fallback.rsplit("-", 1)[-1])
        cycle_start = datetime.fromtimestamp(base_ts, tz=timezone.utc).replace(tzinfo=None)
        cycle_end = cycle_start + timedelta(minutes=5)
    return cycle_start, cycle_end


def process_one_spec(
    spec: PolymarketMarketSpec,
    *,
    page_limit: int,
    max_pages: int,
    quiet: bool,
    market_retries: int,
    pool_maxsize: int,
) -> tuple[str, list[dict]]:
    """返回 (slug, 行记录列表)。对连接重置等错误做整段重试。"""
    slug = spec.slug
    cycle_start, cycle_end = spec_time_window(spec, slug)
    s_ts = int(cycle_start.replace(tzinfo=timezone.utc).timestamp()) if cycle_start.tzinfo is None else int(
        cycle_start.astimezone(timezone.utc).timestamp()
    )
    e_ts = int(cycle_end.replace(tzinfo=timezone.utc).timestamp()) if cycle_end.tzinfo is None else int(
        cycle_end.astimezone(timezone.utc).timestamp()
    )

    last_exc: BaseException | None = None
    for attempt in range(max(1, market_retries)):
        if attempt:
            time.sleep(min(90.0, 1.2 * (2 ** (attempt - 1)) + random.uniform(0, 0.5)))
        try:
            session = _session(pool_maxsize=pool_maxsize)
            raw_rows = fetch_trades_pages(
                session,
                spec.condition_id,
                page_limit=page_limit,
                max_pages=max_pages,
                start_ts=s_ts,
                end_ts=e_ts,
                log_slug=None if quiet else slug,
            )

            records: list[dict] = []
            for row in raw_rows:
                try:
                    tsec = int(row.get("timestamp"))
                except Exception:
                    continue
                ts_dt = datetime.fromtimestamp(tsec, tz=timezone.utc).replace(tzinfo=None)
                outcome_text = str(row.get("outcome") or "").strip().lower()
                if outcome_text in {"yes", "up"}:
                    outcome = "up"
                elif outcome_text in {"no", "down"}:
                    outcome = "down"
                else:
                    asset = str(row.get("asset") or "")
                    if asset == spec.yes_token_id:
                        outcome = "up"
                    elif asset == spec.no_token_id:
                        outcome = "down"
                    else:
                        continue
                records.append(
                    {
                        "price": float(row.get("price") or 0.0),
                        "shares": abs(float(row.get("size") or 0.0)),
                        "outcome": outcome,
                        "action": str(row.get("side") or "BUY").strip().lower(),
                        "symbol": "BTC",
                        "event_start_time": cycle_start,
                        "timestamp": ts_dt,
                        "market_slug": spec.slug,
                        "condition_id": spec.condition_id,
                        "transaction_hash": str(row.get("transactionHash") or ""),
                        "asset": str(row.get("asset") or ""),
                    }
                )
            return slug, records
        except Exception as exc:
            last_exc = exc
            if not _is_transient_http_error(exc) or attempt >= market_retries - 1:
                raise
    assert last_exc is not None
    raise last_exc


def load_resume_state(progress_path: Path, csv_path: Path, resume: bool) -> tuple[set[str], list[dict]]:
    if not resume:
        return set(), []
    done: set[str] = set()
    records: list[dict] = []
    if progress_path.exists():
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        done = set(str(x) for x in data.get("done_slugs", []))
    if csv_path.exists():
        try:
            df = pd.read_csv(csv_path)
            records = df.to_dict(orient="records")
            if not done and "market_slug" in df.columns:
                done = set(df["market_slug"].astype(str).unique())
        except Exception:
            pass
    return done, records


def main() -> int:
    args = parse_args()
    api_env = ROOT.parent / "APIs" / "ApiConfig.env"
    if api_env.exists():
        apply_proxy_env_from_dict(load_api_env(api_env))

    now = datetime.now(timezone.utc)
    window_end = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, tz=timezone.utc)
    window_start = window_end - timedelta(days=args.days)

    start_ts_floor = int(window_start.timestamp()) // 300 * 300
    end_ts_ceil = int(window_end.timestamp())

    out_dir = ROOT / "data" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = window_end.strftime("%Y%m%dT%H%M%SZ")
    days_tag = f"{args.days:g}".replace(".", "p")
    base_name = f"{args.output_prefix}_{days_tag}d_{stamp}"
    progress_path = out_dir / f"{base_name}.progress.json"
    csv_path = out_dir / f"{base_name}.csv"
    xlsx_path = out_dir / f"{base_name}_{get_version_tag()}.xlsx"

    specs = collect_specs(args, start_ts_floor, end_ts_ceil)
    if not specs:
        print("未发现任何市场，退出。", flush=True)
        return 1

    done_slugs, records = load_resume_state(progress_path, csv_path, args.resume)
    if args.resume and (done_slugs or records):
        print(
            f"[resume] 已载入 CSV 行 {len(records)}，完成 slug {len(done_slugs)} 个",
            flush=True,
        )

    pending = [s for s in specs if s.slug not in done_slugs]
    total = len(specs)
    pool_maxsize = max(8, min(48, args.workers * 2))
    print(
        f"[run] 待处理 {len(pending)}/{total} 个市场，workers={args.workers} "
        f"market_retries={args.market_retries} pool_maxsize={pool_maxsize}",
        flush=True,
    )

    lock = threading.Lock()

    def write_progress() -> None:
        payload = {
            "mode": "parallel" if args.workers > 1 else "sequential",
            "done_slugs": sorted(done_slugs),
            "rows": len(records),
            "total_slugs": total,
        }
        progress_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def flush_csv() -> None:
        df = pd.DataFrame.from_records(records)
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    completed = 0

    if args.workers <= 1:
        for spec in pending:
            completed += 1
            if not args.quiet or completed % 50 == 1:
                print(f"[download] {completed}/{len(pending)} {spec.slug}", flush=True)
            try:
                slug, rows = process_one_spec(
                    spec,
                    page_limit=args.page_limit,
                    max_pages=args.max_pages,
                    quiet=args.quiet,
                    market_retries=args.market_retries,
                    pool_maxsize=pool_maxsize,
                )
            except Exception as exc:
                print(f"  [err] {spec.slug}: {exc}", flush=True)
                continue
            records.extend(rows)
            done_slugs.add(slug)
            write_progress()
            if completed % 50 == 0:
                flush_csv()
            if args.sleep_seconds:
                time.sleep(args.sleep_seconds)
    else:
        # 避免一次性挂上万 future 占满内存：分块提交
        chunk_size = max(args.workers * 8, 64)
        for chunk_lo in range(0, len(pending), chunk_size):
            chunk = pending[chunk_lo : chunk_lo + chunk_size]
            if chunk_lo > 0:
                time.sleep(0.3)  # 减轻连续大块请求对远端的瞬时压力
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futs = {
                    pool.submit(
                        process_one_spec,
                        spec,
                        page_limit=args.page_limit,
                        max_pages=args.max_pages,
                        quiet=True,
                        market_retries=args.market_retries,
                        pool_maxsize=pool_maxsize,
                    ): spec
                    for spec in chunk
                }
                for fut in as_completed(futs):
                    spec = futs[fut]
                    try:
                        slug, rows = fut.result()
                    except Exception as exc:
                        print(f"[err] {spec.slug}: {exc}", flush=True)
                        continue
                    with lock:
                        records.extend(rows)
                        done_slugs.add(slug)
                        completed += 1
                        if not args.quiet and (completed % 20 == 0 or completed == len(pending)):
                            print(
                                f"[download] {completed}/{len(pending)} 完成 最近 {slug} +{len(rows)} 行 累计 {len(records)}",
                                flush=True,
                            )
                        write_progress()
                        if completed % 100 == 0:
                            flush_csv()
        if not args.quiet:
            print(f"[download] 并发任务结束，累计行 {len(records)}", flush=True)

    df = pd.DataFrame.from_records(records)
    df.sort_values(
        by=["event_start_time", "timestamp", "transaction_hash", "asset", "price", "shares"],
        inplace=True,
    )
    df = df.drop_duplicates(
        subset=["market_slug", "timestamp", "transaction_hash", "asset", "price", "shares", "action"]
    ).reset_index(drop=True)

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    xlsx_written = False
    xlsx_skip_reason = ""
    if len(df) <= EXCEL_MAX_ROWS:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="BTC", index=False)
        xlsx_written = True
    else:
        xlsx_path.unlink(missing_ok=True)
        xlsx_skip_reason = (
            f"rows={len(df)} exceeds_excel_limit={EXCEL_MAX_ROWS}; "
            "csv remains the canonical artifact"
        )
        if not args.quiet:
            print(f"[download] 跳过 xlsx：{xlsx_skip_reason}", flush=True)

    summary = {
        "window_start_utc": window_start.isoformat(),
        "window_end_utc": window_end.isoformat(),
        "slug_count": total,
        "trade_rows": len(df),
        "csv": str(csv_path),
        "xlsx": str(xlsx_path) if xlsx_written else "",
        "xlsx_written": xlsx_written,
        "xlsx_skip_reason": xlsx_skip_reason,
        "workers": args.workers,
        "discover": args.discover,
        "page_limit": args.page_limit,
    }
    (out_dir / f"{base_name}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    progress_path.write_text(
        json.dumps(
            {"done_slugs": sorted(done_slugs), "rows": len(df), "finished": True, **summary},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

