"""Typer-based CLI for the Zestimate agent.

Commands:
    zestimate lookup "123 Main St, Seattle, WA 98101"
    zestimate lookup --json "123 Main St, Seattle, WA 98101"
    zestimate version
"""

from __future__ import annotations

import asyncio
import json
import typing

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from zestimate_agent import __version__
from zestimate_agent.agent import ZestimateAgent
from zestimate_agent.cache import build_cache
from zestimate_agent.crosscheck import get_usage_counter
from zestimate_agent.eval import (
    DEFAULT_DATASET,
    EvalMode,
    EvalReport,
    EvalRunConfig,
    run_eval,
)
from zestimate_agent.logging import configure_logging
from zestimate_agent.models import ZestimateResult, ZestimateStatus

app = typer.Typer(
    name="zestimate",
    help="Fetch the current Zillow Zestimate for a US property address.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


@app.callback()
def _root(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    import logging
    import os

    if verbose:
        os.environ["LOG_LEVEL"] = "DEBUG"
    configure_logging()
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)


@app.command()
def version() -> None:
    """Print the package version."""
    console.print(f"zestimate-agent {__version__}")


@app.command()
def lookup(
    address: str = typer.Argument(..., help="US property address."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
    no_crosscheck: bool = typer.Option(
        False,
        "--no-crosscheck",
        help="Skip the Rentcast cross-check entirely.",
    ),
    force_crosscheck: bool = typer.Option(
        False,
        "--force-crosscheck",
        help="Run cross-check even if the monthly Rentcast cap is reached.",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass the local cache for read and write.",
    ),
) -> None:
    """Look up the current Zestimate for [ADDRESS]."""
    result = asyncio.run(
        _run_lookup(
            address,
            skip_crosscheck=no_crosscheck,
            force_crosscheck=force_crosscheck,
            use_cache=not no_cache,
        )
    )

    if json_out:
        _print_json(result)
    else:
        _print_pretty(result)

    # Exit code reflects outcome: 0 on ok, 2 on no-data, 1 on error
    if result.status == ZestimateStatus.OK:
        raise typer.Exit(0)
    if result.status in (ZestimateStatus.NO_ZESTIMATE, ZestimateStatus.NOT_FOUND, ZestimateStatus.AMBIGUOUS):
        raise typer.Exit(2)
    raise typer.Exit(1)


async def _run_lookup(
    address: str,
    *,
    skip_crosscheck: bool = False,
    force_crosscheck: bool = False,
    use_cache: bool = True,
) -> ZestimateResult:
    agent = ZestimateAgent.from_env()
    try:
        return await agent.aget(
            address,
            skip_crosscheck=skip_crosscheck,
            force_crosscheck=force_crosscheck,
            use_cache=use_cache,
        )
    finally:
        await agent.aclose()


@app.command("eval")
def eval_cmd(
    mode: str = typer.Option(
        "synthetic",
        "--mode",
        help="Eval mode: synthetic | fixture | live | all.",
    ),
    categories: str = typer.Option(
        "",
        "--categories",
        help="Comma-separated list of categories to include (default: all).",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Max number of cases to run (default: no limit).",
    ),
    concurrency: int = typer.Option(4, "--concurrency", help="Parallel cases."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON report to stdout."),
    csv_out: bool = typer.Option(False, "--csv", help="Emit CSV report to stdout."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow live mode to run without limit (costs credits!).",
    ),
    force_crosscheck: bool = typer.Option(
        False,
        "--force-crosscheck",
        help="Run Rentcast cross-check during live eval (default: skipped).",
    ),
) -> None:
    """Run the eval harness against the curated dataset.

    \b
    Modes:
      synthetic   zero-credit, inline-HTML cases (default, always runs)
      fixture     replays pre-recorded Zillow HTML files
      live        hits real Zillow + Rentcast; requires --force past --limit=3
      all         runs synthetic + fixture (live is never auto-included)
    """
    # ─── Mode ─────────────────────────────────────────────
    run_mode: EvalMode | None
    if mode == "all":
        run_mode = None  # mixed: synthetic + fixture (live filtered out below)
    else:
        try:
            run_mode = EvalMode(mode)
        except ValueError as e:
            err_console.print(f"[red]invalid --mode: {mode}[/red]")
            raise typer.Exit(2) from e

    # ─── Live-mode safety ─────────────────────────────────
    live_requested = run_mode == EvalMode.LIVE
    live_factory: typing.Callable[[], ZestimateAgent] | None = None
    if live_requested:
        if limit is None and not force:
            err_console.print(
                "[yellow]Refusing live eval without --limit (credits!).\n"
                "Pass --limit N or --force to override.[/yellow]"
            )
            raise typer.Exit(2)
        if limit is not None and limit > 3 and not force:
            err_console.print(
                f"[yellow]--limit {limit} would burn ~{limit * 25} ScraperAPI "
                "credits. Pass --force to proceed.[/yellow]"
            )
            raise typer.Exit(2)
        live_factory = ZestimateAgent.from_env

    # If mode="all", also strip live cases (no live_factory = auto-skipped).
    if run_mode is None:
        # Build a mixed dataset without live cases
        dataset = tuple(c for c in DEFAULT_DATASET if c.mode != EvalMode.LIVE)
    else:
        dataset = DEFAULT_DATASET

    cfg = EvalRunConfig(
        mode=run_mode,
        categories=tuple(c.strip() for c in categories.split(",") if c.strip()),
        limit=limit,
        concurrency=concurrency,
        skip_crosscheck=not force_crosscheck,
        force_crosscheck=force_crosscheck,
        live_agent_factory=live_factory,
    )

    outcomes = asyncio.run(run_eval(dataset, config=cfg))
    report = EvalReport.from_outcomes(outcomes)

    if json_out:
        console.print(report.to_json())
    elif csv_out:
        console.print(report.to_csv())
    else:
        _print_eval_pretty(report)

    # Exit 0 if we hit the 99% bar OR if there were zero cases (not a failure
    # — just nothing matched the filter). Exit 1 otherwise so CI can gate.
    if report.summary.total == 0:
        raise typer.Exit(0)
    raise typer.Exit(0 if report.summary.hit_target else 1)


def _print_eval_pretty(report: EvalReport) -> None:
    s = report.summary
    overall_color = "green" if s.hit_target else ("yellow" if s.accuracy >= 0.9 else "red")

    header = Table(show_header=False, box=None, pad_edge=False)
    header.add_column(style="bold", width=22)
    header.add_column()
    header.add_row("Cases", str(s.total))
    header.add_row(
        "Correct",
        f"[{overall_color}]{s.correct}/{s.total}  ({s.accuracy * 100:.1f}%)[/{overall_color}]",
    )
    header.add_row("≥99% target", "[green]HIT[/green]" if s.hit_target else "[red]MISS[/red]")
    header.add_row("Exact value match", f"{s.exact_value}/{s.total}")
    header.add_row("Within 1%", f"{s.within_1pct}/{s.total}")
    header.add_row("Within 5%", f"{s.within_5pct}/{s.total}")
    header.add_row("Status match", f"{s.status_match}/{s.total}")
    header.add_row("zpid match", f"{s.zpid_match}/{s.total}")
    header.add_row("Latency p50 / p95 / mean", f"{s.p50_ms}ms / {s.p95_ms}ms / {s.mean_ms}ms")
    console.print(Panel(header, title="[bold]Eval summary[/bold]", expand=False))

    if s.per_category:
        cat_table = Table(title="[bold]Per-category[/bold]", box=None)
        cat_table.add_column("Category", style="bold")
        cat_table.add_column("N", justify="right")
        cat_table.add_column("Correct", justify="right")
        cat_table.add_column("Accuracy", justify="right")
        cat_table.add_column("Exact", justify="right")
        cat_table.add_column("≤1%", justify="right")
        for cs in s.per_category:
            acc = cs.accuracy * 100
            color = "green" if acc >= 99 else ("yellow" if acc >= 90 else "red")
            cat_table.add_row(
                cs.category,
                str(cs.total),
                str(cs.correct),
                f"[{color}]{acc:.1f}%[/{color}]",
                str(cs.exact_value),
                str(cs.within_1pct),
            )
        console.print(cat_table)

    failures = report.failures()
    if failures:
        fail_table = Table(title="[bold red]Failures[/bold red]", box=None)
        fail_table.add_column("id", style="bold")
        fail_table.add_column("category")
        fail_table.add_column("expected")
        fail_table.add_column("actual")
        fail_table.add_column("error", overflow="fold")
        for o in failures:
            expected = (
                f"{o.case.expected_status.value}"
                + (f" ${o.case.expected_value:,}" if o.case.expected_value else "")
            )
            actual = (
                f"{o.result.status.value}"
                + (f" ${o.result.value:,}" if o.result.value else "")
            )
            fail_table.add_row(
                o.case.id,
                o.case.category.value,
                expected,
                actual,
                (o.result.error or o.exception or "")[:80],
            )
        console.print(fail_table)


@app.command("serve")
def serve(
    host: str = typer.Option(None, "--host", help="Bind host (default from settings)."),
    port: int = typer.Option(None, "--port", help="Bind port (default from settings)."),
    reload: bool = typer.Option(
        False,
        "--reload",
        help="Enable uvicorn auto-reload (dev only).",
    ),
    workers: int = typer.Option(
        1,
        "--workers",
        help="Number of uvicorn worker processes (>1 disables --reload).",
    ),
) -> None:
    """Run the FastAPI server via uvicorn.

    This imports FastAPI/uvicorn lazily so the CLI itself has no hard
    dependency on the `api` extra — install with `pip install zestimate-agent[api]`.
    """
    try:
        import uvicorn
    except ImportError as e:
        err_console.print(
            "[red]uvicorn is not installed. Run `pip install zestimate-agent[api]`.[/red]"
        )
        raise typer.Exit(1) from e

    from zestimate_agent.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "zestimate_agent.api:create_app",
        factory=True,
        host=host or settings.api_host,
        port=port or settings.api_port,
        reload=reload,
        workers=workers if not reload else 1,
        log_config=None,  # let our structlog config handle it
    )


@app.command("cache-stats")
def cache_stats(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
) -> None:
    """Show current cache backend, on-disk volume, and hit/miss counters."""
    from zestimate_agent.config import get_settings

    settings = get_settings()
    cache = build_cache()
    try:
        vol = cache.volume()
        st = cache.stats.as_dict()
    finally:
        cache.close()

    payload = {
        "backend": settings.cache_backend,
        "path": str(settings.cache_path) if settings.cache_backend == "sqlite" else None,
        "ttl_seconds": settings.cache_ttl_seconds,
        "volume_bytes": vol,
        **st,
    }
    if json_out:
        console.print_json(json.dumps(payload))
        return

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold", width=14)
    table.add_column()
    for k, v in payload.items():
        if v is None:
            continue
        table.add_row(k, str(v))
    console.print(Panel(table, title="[bold]Cache stats[/bold]", expand=False))


@app.command("cache-clear")
def cache_clear(
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't prompt."),
) -> None:
    """Delete every entry from the result cache."""
    if not yes:
        confirm = typer.confirm("Clear the entire result cache?")
        if not confirm:
            raise typer.Exit(1)
    cache = build_cache()
    try:
        n = cache.clear()
    finally:
        cache.close()
    console.print(f"[green]cleared[/green] {n} cache entries")


@app.command("rentcast-status")
def rentcast_status(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
) -> None:
    """Show the current Rentcast monthly usage against the configured cap."""
    counter = get_usage_counter()
    snap = counter.snapshot()
    if json_out:
        console.print_json(
            json.dumps(
                {
                    "month": snap.month,
                    "used": snap.used,
                    "cap": snap.cap,
                    "remaining": snap.remaining,
                    "exhausted": snap.exhausted,
                }
            )
        )
        return

    style = "red" if snap.exhausted else ("yellow" if snap.remaining <= 5 else "green")
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold", width=14)
    table.add_column()
    table.add_row("Month", snap.month)
    table.add_row("Used", f"[{style}]{snap.used}[/{style}] / {snap.cap}")
    table.add_row("Remaining", f"[{style}]{snap.remaining}[/{style}]")
    if snap.exhausted:
        table.add_row("Status", "[red]EXHAUSTED — cross-check skipped until next month[/red]")
    console.print(Panel(table, title="[bold]Rentcast usage[/bold]", expand=False))


# ─── Output formatters ──────────────────────────────────────────


def _print_json(result: ZestimateResult) -> None:
    # `mode="json"` ensures datetimes are ISO strings
    console.print_json(json.dumps(result.model_dump(mode="json")))


def _print_pretty(result: ZestimateResult) -> None:
    if result.status == ZestimateStatus.OK and result.value is not None:
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column(style="bold", width=14)
        table.add_column()
        table.add_row("Zestimate", f"[bold green]${result.value:,}[/bold green]")
        table.add_row("Address", result.matched_address or "?")
        table.add_row("zpid", result.zpid or "?")
        table.add_row("URL", result.zillow_url or "?")
        table.add_row("Confidence", f"{result.confidence:.2f}")
        table.add_row("Fetcher", result.fetcher or "?")
        if result.cached:
            table.add_row("Source", "[cyan]cache hit[/cyan]")
        if result.crosscheck is not None:
            cc = result.crosscheck
            if cc.skipped:
                table.add_row(
                    "Cross-check",
                    f"[dim]{cc.provider}: skipped ({cc.skipped_reason})[/dim]",
                )
            elif cc.estimate is not None and cc.delta_pct is not None:
                tone = "green" if cc.within_tolerance else "yellow"
                sign = "+" if cc.delta_pct >= 0 else ""
                table.add_row(
                    "Cross-check",
                    f"[{tone}]{cc.provider}: ${cc.estimate:,} ({sign}{cc.delta_pct:.1f}%)[/{tone}]",
                )
        if result.trace_id:
            table.add_row("Trace", result.trace_id)
        console.print(Panel(table, title="[bold]Zestimate result[/bold]", expand=False))
        return

    # Failure / empty / ambiguous
    style = {
        ZestimateStatus.NO_ZESTIMATE: "yellow",
        ZestimateStatus.NOT_FOUND: "yellow",
        ZestimateStatus.AMBIGUOUS: "yellow",
        ZestimateStatus.BLOCKED: "red",
        ZestimateStatus.ERROR: "red",
    }.get(result.status, "white")

    msg = f"[{style}]{result.status.value}[/{style}]"
    if result.error:
        msg += f"\n{result.error}"
    if result.matched_address:
        msg += f"\naddress: {result.matched_address}"
    console.print(Panel(msg, title="[bold]Zestimate result[/bold]", expand=False))


if __name__ == "__main__":
    app()
