"""xstx 中转站连通性诊断脚本。

测三件事：
  1. 短请求稳定性  — N 个轻量请求，统计成功率 / TTFB / 失败模式
  2. 长流式压测    — 一个 stream 请求输出 ~2000 token，记录逐 chunk 时间线
  3. 并发探测      — M 个请求同时发，看是否触发限流或连接池耗尽

用法:
  python probe_xstx.py                         # 用 keys.ini 里 [xstx] 的 key
  python probe_xstx.py --key sk-xxx            # 用备用 key 覆盖
  python probe_xstx.py -n 50 -c 10 --stream-tokens 4000   # 自定义参数

输出: reports/xstx-probe-<ts>.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

@dataclass
class XstxConfig:
    key: str
    base_url: str

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "X-API-Key": self.key,
        }


def load_xstx_from_config(key_override: Optional[str] = None) -> XstxConfig:
    """从项目的 config.py / keys.ini 读取 xstx 配置。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rag-core"))
    from config import load_config
    cfg = load_config()
    if "xstx" not in cfg.keys:
        raise SystemExit("keys.ini 中缺少 [xstx] 节")
    pk = cfg.keys["xstx"]
    return XstxConfig(key=key_override or pk.key, base_url=pk.base_url)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ShortResult:
    seq: int
    ok: bool
    status_code: Optional[int] = None
    ttfb_ms: Optional[float] = None       # time to first byte
    total_ms: Optional[float] = None
    content_len: int = 0
    error: str = ""


@dataclass
class StreamChunk:
    seq: int
    elapsed_ms: float
    content_len: int = 0


@dataclass
class StreamResult:
    ok: bool
    target_tokens: int = 0
    actual_chunks: int = 0
    first_chunk_ms: Optional[float] = None
    last_chunk_ms: Optional[float] = None
    total_bytes: int = 0
    error: str = ""
    chunks: list[StreamChunk] = field(default_factory=list)


@dataclass
class ProbeReport:
    ts: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d-%H%M%S"))
    base_url: str = ""
    key_prefix: str = ""          # 只记 key 前 7 位，不泄露
    model: str = ""

    short_total: int = 0
    short_ok: int = 0
    short_fail: int = 0
    short_results: list[ShortResult] = field(default_factory=list)

    stream: Optional[StreamResult] = None

    concurrent_total: int = 0
    concurrent_ok: int = 0
    concurrent_fail: int = 0
    concurrent_results: list[ShortResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 短请求测试
# ---------------------------------------------------------------------------

SHORT_PAYLOAD = {
    "model": "__placeholder__",
    "messages": [{"role": "user", "content": "Say exactly: ok"}],
    "max_tokens": 16,
    "temperature": 0.0,
}


async def _one_short(seq: int, cfg: XstxConfig, model: str, timeout: float) -> ShortResult:
    payload = {**SHORT_PAYLOAD, "model": model}
    t0 = time.monotonic()
    result = ShortResult(seq=seq, ok=False)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", cfg.chat_url, headers=cfg.headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = ""
                    try:
                        body = await resp.aread()
                    except Exception:
                        pass
                    result.status_code = resp.status_code
                    result.error = str(body)[:200]
                    result.total_ms = (time.monotonic() - t0) * 1000
                    return result
                # 收首字节
                first = True
                total_bytes = 0
                async for line in resp.aiter_lines():
                    if first:
                        result.ttfb_ms = (time.monotonic() - t0) * 1000
                        first = False
                    total_bytes += len(line)
                result.ok = True
                result.total_ms = (time.monotonic() - t0) * 1000
                result.content_len = total_bytes
    except httpx.ReadTimeout:
        result.error = "ReadTimeout"
        result.total_ms = (time.monotonic() - t0) * 1000
    except httpx.ConnectTimeout:
        result.error = "ConnectTimeout"
        result.total_ms = (time.monotonic() - t0) * 1000
    except httpx.RemoteProtocolError as e:
        result.error = f"RemoteProtocolError: {e}"[:200]
        result.total_ms = (time.monotonic() - t0) * 1000
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"[:200]
        result.total_ms = (time.monotonic() - t0) * 1000
    return result


# ---------------------------------------------------------------------------
# 长流式测试
# ---------------------------------------------------------------------------

async def _stream_long(cfg: XstxConfig, model: str, target_tokens: int, timeout: float) -> StreamResult:
    result = StreamResult(ok=False, target_tokens=target_tokens)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": f"Write exactly {target_tokens} words about apples. Output only words, no markdown, no numbers, no lists, no code. Just plain words."}],
        "max_tokens": target_tokens + 500,
        "temperature": 0.3,
        "stream": True,
    }
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", cfg.chat_url, headers=cfg.headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = ""
                    try:
                        body = await resp.aread()
                    except Exception:
                        pass
                    result.error = f"HTTP {resp.status_code}: {body!r}"[:300]
                    return result
                seq = 0
                async for line in resp.aiter_lines():
                    elapsed = (time.monotonic() - t0) * 1000
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if data == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data)
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or {}
                            content = delta.get("content") or delta.get("reasoning_content") or ""
                            if content:
                                seq += 1
                                result.chunks.append(StreamChunk(seq=seq, elapsed_ms=elapsed, content_len=len(content)))
                                result.total_bytes += len(content)
                        except json.JSONDecodeError:
                            continue
                result.ok = True
                result.actual_chunks = seq
                if result.chunks:
                    result.first_chunk_ms = result.chunks[0].elapsed_ms
                    result.last_chunk_ms = result.chunks[-1].elapsed_ms
    except httpx.ReadTimeout:
        result.error = "ReadTimeout (流中断)"
        elapsed = (time.monotonic() - t0) * 1000
        if result.chunks:
            result.first_chunk_ms = result.chunks[0].elapsed_ms
            result.last_chunk_ms = result.chunks[-1].elapsed_ms
    except httpx.RemoteProtocolError as e:
        result.error = f"RemoteProtocolError: {e}"[:200]
        elapsed = (time.monotonic() - t0) * 1000
        if result.chunks:
            result.first_chunk_ms = result.chunks[0].elapsed_ms
            result.last_chunk_ms = result.chunks[-1].elapsed_ms
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"[:200]
    return result


# ---------------------------------------------------------------------------
# 并发测试
# ---------------------------------------------------------------------------

async def _concurrent(cfg: XstxConfig, model: str, workers: int, timeout: float) -> list[ShortResult]:
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(_one_short(i, cfg, model, timeout)) for i in range(workers)]
    return [t.result() for t in tasks]


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------

def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * p / 100)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def render_markdown(report: ProbeReport) -> str:
    lines: list[str] = []
    L = lines.append

    L(f"# xstx 连通性诊断报告")
    L(f"")
    L(f"**时间**: {report.ts}")
    L(f"**目标 URL**: `{report.base_url}`")
    L(f"**Key 前缀**: `{report.key_prefix}***`")
    L(f"**Model**: `{report.model}`")
    L(f"")

    # --- 1. 短请求 ---
    L("## 1. 短请求稳定性采样")
    L("")
    ok = report.short_ok
    total = report.short_total
    rate = f"{ok / total * 100:.1f}%" if total else "N/A"
    L(f"| 指标 | 值 |")
    L(f"|------|----|")
    L(f"| 总数 | {total} |")
    L(f"| 成功 | {ok} |")
    L(f"| 失败 | {report.short_fail} |")
    L(f"| 成功率 | {rate} |")

    if report.short_results:
        ttfb_vals = [r.ttfb_ms for r in report.short_results if r.ok and r.ttfb_ms is not None]
        total_vals = [r.total_ms for r in report.short_results if r.ok and r.total_ms is not None]
        if ttfb_vals:
            L(f"| TTFB P50 | {_pct(ttfb_vals, 50):.0f} ms |")
            L(f"| TTFB P95 | {_pct(ttfb_vals, 95):.0f} ms |")
            L(f"| TTFB P99 | {_pct(ttfb_vals, 99):.0f} ms |")
        if total_vals:
            L(f"| 总耗时 P50 | {_pct(total_vals, 50):.0f} ms |")
            L(f"| 总耗时 P95 | {_pct(total_vals, 95):.0f} ms |")
            L(f"| 总耗时 P99 | {_pct(total_vals, 99):.0f} ms |")

    # 失败分类
    errors: dict[str, int] = {}
    for r in report.short_results:
        if not r.ok:
            label = r.error[:80] if r.error else f"HTTP {r.status_code}"
            errors[label] = errors.get(label, 0) + 1
    if errors:
        L("")
        L("### 失败明细")
        L("")
        L("| 错误 | 次数 |")
        L("|------|------|")
        for msg, cnt in sorted(errors.items(), key=lambda x: -x[1]):
            L(f"| {msg} | {cnt} |")

    # --- 2. 长流式 ---
    L("")
    L("## 2. 长流式压测")
    L("")
    s = report.stream
    if s is None:
        L("(未执行)")
    elif not s.ok:
        L(f"**结果**: 失败")
        L(f"**错误**: `{s.error}`")
    else:
        L(f"| 指标 | 值 |")
        L(f"|------|----|")
        L(f"| 目标 token | {s.target_tokens} |")
        L(f"| 收到 chunk 数 | {s.actual_chunks} |")
        L(f"| 总字节 | {s.total_bytes} |")
        L(f"| 首 chunk (TTFB) | {s.first_chunk_ms:.0f} ms" if s.first_chunk_ms else "| 首 chunk | N/A |")
        L(f"| 末 chunk | {s.last_chunk_ms:.0f} ms" if s.last_chunk_ms else "| 末 chunk | N/A |")

        if s.chunks:
            L("")
            L("### chunk 时间线 (前 10 + 后 5)")
            L("")
            L("| seq | elapsed_ms | content_len |")
            L("|-----|------------|-------------|")
            for c in s.chunks[:10]:
                L(f"| {c.seq} | {c.elapsed_ms:.0f} | {c.content_len} |")
            if len(s.chunks) > 15:
                L("| ... | ... | ... |")
            for c in s.chunks[-5:]:
                L(f"| {c.seq} | {c.elapsed_ms:.0f} | {c.content_len} |")

            # 检查异常间隔
            gaps: list[tuple[int, float]] = []
            prev_chunk = s.chunks[0]
            for c in s.chunks[1:]:
                gap = c.elapsed_ms - prev_chunk.elapsed_ms
                if gap > 5000:    # 5s 以上无 chunk 视为异常
                    gaps.append((c.seq, gap))
                prev_chunk = c
            if gaps:
                L("")
                L("### 异常间隔 (>5s 无响应)")
                L("")
                L("| 起始 chunk | 间隔 |")
                L("|-----------|------|")
                for seq, gap in gaps[:10]:
                    L(f"| {seq} | {gap:.0f} ms |")

    # --- 3. 并发 ---
    L("")
    L("## 3. 并发探测")
    L("")
    c_ok = report.concurrent_ok
    c_total = report.concurrent_total
    c_rate = f"{c_ok / c_total * 100:.1f}%" if c_total else "N/A"
    L(f"| 指标 | 值 |")
    L(f"|------|----|")
    L(f"| 并发数 | {c_total} |")
    L(f"| 成功 | {c_ok} |")
    L(f"| 失败 | {report.concurrent_fail} |")
    L(f"| 成功率 | {c_rate} |")

    if report.concurrent_results:
        ttfb_vals = [r.ttfb_ms for r in report.concurrent_results if r.ok and r.ttfb_ms is not None]
        if ttfb_vals:
            L(f"| TTFB min | {min(ttfb_vals):.0f} ms |")
            L(f"| TTFB max | {max(ttfb_vals):.0f} ms |")
            L(f"| TTFB P95 | {_pct(ttfb_vals, 95):.0f} ms |")

    c_errors: dict[str, int] = {}
    for r in report.concurrent_results:
        if not r.ok:
            label = r.error[:80] if r.error else f"HTTP {r.status_code}"
            c_errors[label] = c_errors.get(label, 0) + 1
    if c_errors:
        L("")
        L("### 并发失败明细")
        L("")
        L("| 错误 | 次数 |")
        L("|------|------|")
        for msg, cnt in sorted(c_errors.items(), key=lambda x: -x[1]):
            L(f"| {msg} | {cnt} |")

    # --- 4. 诊断结论 ---
    L("")
    L("## 4. 诊断结论")
    L("")

    short_ok_rate = ok / total if total else 0
    findings: list[str] = []

    if short_ok_rate >= 0.95:
        findings.append("- 短请求稳定性正常 (成功率 >= 95%)")
        L("- 短请求稳定性 **正常** (成功率 >= 95%)")
    elif short_ok_rate >= 0.7:
        findings.append("- 短请求存在波动 (成功率 70%-94%)，中转站偶发故障")
        L("- 短请求存在 **波动** (成功率 70%-94%)，中转站偶发故障")
    else:
        findings.append("- 短请求大面积失败 (成功率 < 70%)，中转站严重不稳定")
        L("- 短请求 **大面积失败** (成功率 < 70%)，中转站严重不稳定")

    # 分析 TTFB 抖动
    short_ttfb = [r.ttfb_ms for r in report.short_results if r.ok and r.ttfb_ms is not None]
    if len(short_ttfb) >= 10:
        p50 = _pct(short_ttfb, 50)
        p95 = _pct(short_ttfb, 95)
        if p50 > 5000:
            L(f"- TTFB P50={p50:.0f}ms > 5s，**延迟极高**，可能网络链路有问题")
        elif p95 > p50 * 5:
            L(f"- TTFB P95={p95:.0f}ms >> P50={p50:.0f}ms，**长尾抖动严重**")

    # 流式分析
    if s:
        if s.ok:
            L(f"- 长流式: 收到 {s.actual_chunks} chunks / {s.total_bytes} bytes")
            if s.actual_chunks < s.target_tokens * 0.5:
                L(f"- **流式被提前截断** (实际 chunk 数远小于目标)")
            else:
                L("- 流式: **完整收完**")
        else:
            L(f"- 长流式: **失败** ({s.error[:80]})")
            if "ReadTimeout" in s.error:
                L("  → 可能空闲超时被掐，检查中转站 idle timeout 配置")
            elif "RemoteProtocolError" in s.error:
                L("  → 可能传输层中断或代理异常")

    # 并发分析
    c_rate_ok = c_ok / c_total if c_total else 0
    if c_rate_ok < 1.0:
        L(f"- 并发: {c_ok}/{c_total} 成功，**存在并发失败**")
        if any("429" in (r.error or "") or (r.status_code == 429) for r in report.concurrent_results):
            L("  → 出现 429 (限流)，中转站有并发限制")
        else:
            L("  → 非 429 错误，可能是连接池耗尽或中转站过载")
    else:
        L(f"- 并发: 全部成功")

    L("")
    L("---")
    L(f"*报告生成时间: {datetime.now().isoformat()}*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def run_probe(
    cfg: XstxConfig,
    model: str = "claude-opus-4-7",
    short_n: int = 30,
    stream_tokens: int = 2000,
    concurrent_n: int = 5,
    timeout: float = 120.0,
) -> ProbeReport:
    """执行全套诊断并返回 ProbeReport。"""
    report = ProbeReport(
        base_url=cfg.base_url,
        key_prefix=cfg.key[:7],
        model=model,
    )

    print(f"[probe] 短请求采样 x{short_n} ...")
    coros = [_one_short(i, cfg, model, timeout) for i in range(short_n)]
    short_results = await asyncio.gather(*coros)
    report.short_total = short_n
    report.short_ok = sum(1 for r in short_results if r.ok)
    report.short_fail = short_n - report.short_ok
    report.short_results = list(short_results)
    print(f"  ok={report.short_ok} fail={report.short_fail}")

    if stream_tokens > 0:
        print(f"[probe] 长流式压测 (目标 {stream_tokens} tokens) ...")
        report.stream = await _stream_long(cfg, model, stream_tokens, timeout)
        if report.stream.ok:
            print(f"  chunks={report.stream.actual_chunks} bytes={report.stream.total_bytes}")
        else:
            print(f"  FAIL: {report.stream.error[:80]}")

    if concurrent_n > 0:
        print(f"[probe] 并发探测 x{concurrent_n} ...")
        report.concurrent_total = concurrent_n
        report.concurrent_results = await _concurrent(cfg, model, concurrent_n, timeout)
        report.concurrent_ok = sum(1 for r in report.concurrent_results if r.ok)
        report.concurrent_fail = concurrent_n - report.concurrent_ok
        print(f"  ok={report.concurrent_ok} fail={report.concurrent_fail}")

    return report


def main():
    parser = argparse.ArgumentParser(description="xstx 中转站连通性诊断")
    parser.add_argument("--key", default=None, help="覆盖 keys.ini 中的 xstx key (有备用时使用)")
    parser.add_argument("--base-url", default=None, help="覆盖 keys.ini 中的 base_url")
    parser.add_argument("--model", default="claude-opus-4-7", help="模型名 (默认 claude-opus-4-7)")
    parser.add_argument("-n", "--short-count", type=int, default=30, help="短请求采样数 (默认 30)")
    parser.add_argument("--stream-tokens", type=int, default=2000, help="流式目标 token 数 (默认 2000, 0=跳过)")
    parser.add_argument("-c", "--concurrent", type=int, default=5, help="并发数 (默认 5, 0=跳过)")
    parser.add_argument("--timeout", type=float, default=120.0, help="单请求超时秒 (默认 120)")
    parser.add_argument("-o", "--output", default=None, help="报告输出路径 (默认 reports/ 下自动命名)")
    args = parser.parse_args()

    cfg = load_xstx_from_config(key_override=args.key)
    if args.base_url:
        cfg.base_url = args.base_url

    print(f"xstx probe:")
    print(f"  url : {cfg.chat_url}")
    print(f"  key : {cfg.key[:7]}***")
    print(f"  model: {args.model}")
    print()

    report = asyncio.run(run_probe(
        cfg,
        model=args.model,
        short_n=args.short_count,
        stream_tokens=args.stream_tokens,
        concurrent_n=args.concurrent,
        timeout=args.timeout,
    ))

    md = render_markdown(report)

    # 确定输出路径
    if args.output:
        out_path = Path(args.output)
    else:
        # 从 config 拿 report_dir，但不依赖 config 必须存在
        try:
            from config import load_config
            c = load_config()
            out_dir = c.report_dir
        except Exception:
            out_dir = Path(__file__).resolve().parent / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"xstx-probe-{report.ts}.md"

    out_path.write_text(md, encoding="utf-8")
    print(f"\n报告已写入: {out_path}")
    print(md)


if __name__ == "__main__":
    main()
