"""Command line entry point (``python mt5_bot.py ...``)."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .advisor.redact import RedactingFormatter
from . import strategies as strategy_lib
from .backtest import compute_metrics, simulate
from .bot import (EXIT_ERROR, EXIT_OK, EXIT_SAFETY_STOP, EXIT_STARTUP, AccountPolicyError, Bot, StartupError, mask)
from .config import Config, apply_overrides
from .cycle import CycleLock, cost_model, resolved_symbols, run_cycle
from .engine import AdaptiveEngine, EngineSettings
from .optimizer import OptimizerSettings, optimize_symbol
from .persistence import account_state_dir
from .registry import Registry
from .reports import format_health
from .risk import SymbolSpec, request_resume

log = logging.getLogger("mt5_bot")

LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5
COUNTDOWN_SECONDS = 10
HEALTH_STALE_SECONDS = 300


def setup_logging(log_file: Optional[str]) -> None:
    """Rotating UTF-8 file log (optional) plus a console handler that never crashes on encoding.

    Every handler redacts secrets (API keys, tokens, passwords) from the final text,
    including exception tracebacks.
    """
    formatter = RedactingFormatter("%(asctime)s %(levelname)-8s %(message)s")
    log.handlers.clear()
    if log_file:
        path = Path(log_file)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES,
                                                            backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            # Legacy Windows consoles (cp850/cp1252) would raise on unencodable characters.
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    console = logging.StreamHandler(stream)
    console.setFormatter(formatter)
    log.addHandler(console)
    log.setLevel(logging.INFO)
    log.propagate = False


def install_signal_handlers(bot: Bot) -> None:
    install_signal_handlers_for(bot.request_stop)


def install_signal_handlers_for(stop: Any) -> None:
    def handler(signum: int, _frame: Any) -> None:
        try:
            stop(signum)
        except TypeError:
            stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK = Ctrl+Break / console close on Windows
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError, RuntimeError):
            log.debug("Could not install handler for %s", name)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MetaTrader 5 trading bot v3 (adaptive core, optional specialist "
                                                 "team and Claude advisor, supervised).")
    parser.add_argument("--config", default="config.json", help="path to config.json (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="run everything but never send orders")
    parser.add_argument("--confirm-real-money", action="store_true",
                        help="one-time explicit confirmation required to trade a REAL account")
    parser.add_argument("--no-countdown", action="store_true", help="skip the 10 s real-money countdown")
    parser.add_argument("--bootstrap-days", type=float, default=None,
                        help="minimum days of observation before the first promotion")
    parser.add_argument("--no-claude", action="store_true",
                        help="run without the optional Claude advisor (the bot is complete without it)")
    parser.add_argument("--team-mode", choices=("auto", "full", "baseline"), default=None,
                        help="override team.team_mode: auto (evidence decides), full (team) or baseline (v2 core)")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="print health.json and the champions")
    actions.add_argument("--history", action="store_true", help="print the decision log")
    actions.add_argument("--rollback", type=int, metavar="ID", help="restore the champion installed by decision ID")
    actions.add_argument("--resume", action="store_true", help="clear the drawdown circuit breaker")
    actions.add_argument("--backtest", action="store_true", help="walk-forward report (or one strategy with --strategy)")
    actions.add_argument("--optimize-once", action="store_true", help="run one optimisation cycle and exit")
    actions.add_argument("--set-claude-key", action="store_true",
                         help="store the Claude API key in the Windows Credential Manager (hidden prompt)")
    actions.add_argument("--simulate", action="store_true",
                         help="run the real bot and team on a synthetic market (no MT5, no broker, no API key)")
    actions.add_argument("--ablation-report", action="store_true",
                         help="print the team's value-added report (ablation) and the team-vs-baseline verdict")
    actions.add_argument("--reset-team-weights", action="store_true",
                         help="reset learned agent weights, calibration and performance benching (audited)")
    parser.add_argument("--sim-bars", type=int, default=None, help="with --simulate: stop after N bars")
    parser.add_argument("--sim-speed", type=float, default=2.0, help="with --simulate: bars per second (0 = max)")
    parser.add_argument("--sim-seed", type=int, default=7, help="with --simulate: random seed of the market")
    parser.add_argument("--sim-state", default="state_sim", help="with --simulate: state folder (default %(default)s)")
    parser.add_argument("--sim-fast-forward", type=int, default=0,
                        help="with --simulate: run the first N bars at full speed")
    parser.add_argument("--sim-advisor", choices=("simulated", "none", "claude"), default="simulated",
                        help="with --simulate: advisor to use (default: a rule-based simulated one, no API)")
    parser.add_argument("--offline", action="store_true", help="use only the cached data (no MT5 connection)")
    parser.add_argument("--state-dir", help="account state folder for offline commands")
    parser.add_argument("--account", help="state folder name (<server>_<login>) for offline commands")
    parser.add_argument("--trigger", default="manual", help=argparse.SUPPRESS)
    parser.add_argument("--workers", type=int, default=None, help="optimiser worker processes")
    parser.add_argument("--strategy", help="with --backtest: one strategy name")
    parser.add_argument("--params", help="with --backtest --strategy: JSON parameters (default: grid middle)")
    parser.add_argument("--limit", type=int, default=30, help="rows for --history")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


# ------------------------------------------------------------------ state lookup


def resolve_state_dir(cfg: Config, args: argparse.Namespace) -> Path:
    if args.state_dir:
        return Path(args.state_dir)
    root = Path(cfg.state_root)
    if args.account:
        return root / args.account
    health = Path(cfg.health_file)
    if health.exists():
        try:
            recorded = json.loads(health.read_text(encoding="utf-8")).get("state_dir")
            if recorded and Path(recorded).exists():
                return Path(recorded)
        except (OSError, ValueError):
            pass
    folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    if len(folders) == 1:
        return folders[0]
    if not folders:
        raise ValueError(f"No account state folder in {root}. Start the bot once, or pass --state-dir.")
    names = ", ".join(p.name for p in folders)
    raise ValueError(f"Several accounts have state in {root} ({names}). Choose one with --account <name>.")


def _ts(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ------------------------------------------------------------------- commands


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    health = Path(cfg.health_file)
    if not health.exists():
        print(f"{health} not found: the bot has not run yet in this folder (or health_file points elsewhere).")
        return EXIT_ERROR
    payload = json.loads(health.read_text(encoding="utf-8"))
    print(format_health(payload))
    age = time.time() - health.stat().st_mtime
    if age > HEALTH_STALE_SECONDS:
        print(f"\nWARNING: health file is {age / 60:.0f} minutes old; the bot may not be running.")
    return EXIT_OK


def cmd_history(cfg: Config, args: argparse.Namespace) -> int:
    registry = Registry(resolve_state_dir(cfg, args) / "registry.sqlite")
    try:
        rows = registry.promotions(args.limit)
        if not rows:
            print("No decisions recorded yet.")
            return EXIT_OK
        for row in reversed(rows):
            print(f"#{row['id']:<5} {_ts(row['ts'])}  {row['symbol']:<10} {row['action'].upper():<12} "
                  f"{row['old_desc']} -> {row['new_desc']}")
            print(f"       reason: {row['reason']}")
            metrics = json.loads(row["metrics_json"] or "{}")
            if metrics:
                print(f"       metrics: {json.dumps(metrics, sort_keys=True)[:600]}")
        return EXIT_OK
    finally:
        registry.close()


def cmd_rollback(cfg: Config, args: argparse.Namespace) -> int:
    registry = Registry(resolve_state_dir(cfg, args) / "registry.sqlite")
    try:
        engine = AdaptiveEngine(registry, EngineSettings.from_config(cfg))
        try:
            promo = engine.rollback(args.rollback)
        except KeyError as exc:
            print(str(exc))
            return EXIT_ERROR
        row = registry.promotion(promo)
        print(f"Rollback recorded as decision #{promo}: {row['symbol']} champion is now {row['new_desc']}.")
        print("A running bot picks this up on the next closed bar. Open positions keep their SL/TP.")
        return EXIT_OK
    finally:
        registry.close()


def cmd_resume(cfg: Config, args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(cfg, args)
    if request_resume(state_dir):
        print(f"Drawdown circuit breaker cleared for {state_dir.name}. The bot resumes on its next loop; "
              "the drawdown peak restarts from the equity at that moment.")
    else:
        print(f"No drawdown halt is active for {state_dir.name} (no RESUME_REQUIRED.txt).")
    return EXIT_OK


def _connect_for_data(cfg: Config, config_path: str) -> tuple[Bot, Registry]:
    bot = Bot(cfg, config_path=config_path)
    if not bot._initialize():
        raise StartupError(f"mt5.initialize() failed: {bot._last_error()}")
    acc = bot.mt5.account_info()
    if acc is None:
        raise StartupError("account_info() returned nothing; log in to an account")
    bot.is_real = not bot._is_demo(acc)
    bot.state_dir = account_state_dir(cfg.state_root, acc.server, acc.login)
    bot.registry = Registry(bot.state_dir / "registry.sqlite")
    from .bot import resolve_symbol

    for wanted in cfg.symbols:
        name = resolve_symbol(bot.mt5, wanted)
        if name is None:
            raise StartupError(f"Symbol {wanted!r} not found")
        bot.mt5.symbol_select(name, True)
        bot.symbols[wanted] = name
    bot.registry.set_kv("resolved_symbols", json.dumps(bot.symbols))
    bot.registry.set_kv("is_real", "1" if bot.is_real else "0")
    if not bot.refresh_market_data():
        log.warning("Some symbols returned no history")
    log.info("Data cached for account %s (%s)", mask(acc.login), "REAL" if bot.is_real else "DEMO")
    return bot, bot.registry


def _open_registry(cfg: Config, args: argparse.Namespace) -> tuple[Optional[Bot], Registry, Path]:
    if args.offline:
        state_dir = resolve_state_dir(cfg, args)
        return None, Registry(state_dir / "registry.sqlite"), state_dir
    bot, registry = _connect_for_data(cfg, args.config)
    return bot, registry, bot.state_dir  # type: ignore[return-value]


def cmd_optimize(cfg: Config, args: argparse.Namespace) -> int:
    bot, registry, state_dir = _open_registry(cfg, args)
    try:
        lock = CycleLock(state_dir)
        if not lock.acquire():
            print("Another optimisation cycle is already running for this account; exiting.")
            return EXIT_OK
        with lock:
            results = run_cycle(cfg, registry, args.trigger, workers=args.workers, lower_priority=args.offline)
        for symbol, run_id, result in results:
            if result is None:
                print(f"{symbol}: run {run_id} FAILED (see the log)")
                continue
            best = result.best
            print(f"{symbol}: run {run_id}, {result.trials} trials, {result.seconds:.0f}s -> "
                  f"{best.strategy if best else 'NoTrade (nothing passed the gates)'}")
            for cand in result.candidates:
                status = "PASS" if cand.passed else "fail"
                print(f"    {status} {cand.strategy:<24} score {cand.score:9.3f}  "
                      f"{'; '.join(cand.reasons[:3]) or 'all gates passed'}")
        return EXIT_OK
    finally:
        registry.close()
        if bot is not None:
            bot.shutdown()


def cmd_backtest(cfg: Config, args: argparse.Namespace) -> int:
    bot, registry, state_dir = _open_registry(cfg, args)
    try:
        is_real = registry.get_kv("is_real") == "1"
        out_dir = Path(cfg.reports_dir) / state_dir.name
        out_dir.mkdir(parents=True, exist_ok=True)
        for symbol in resolved_symbols(cfg, registry):
            spec_row = registry.load_spec(symbol)
            bars = registry.load_bars(symbol, cfg.timeframe)
            if spec_row is None or len(bars) < 500:
                print(f"{symbol}: not enough cached data ({len(bars)} bars); run without --offline first")
                continue
            spec = SymbolSpec.from_json(spec_row[0])
            costs = cost_model(cfg, spec_row[1])
            if args.strategy:
                strat = strategy_lib.get(args.strategy)
                params = strat.validate_params(json.loads(args.params)) if args.params else strat.default_params()
                trades = simulate(bars, strat.signals(bars, params), spec, costs, 10_000.0,
                                  cfg.risk_per_trade_pct(is_real))
                metrics = compute_metrics(trades, 10_000.0, bars["time"], 0, len(bars), cfg.bar_seconds)
                print(f"{symbol}: {strategy_lib.describe(strat, params)} over {len(bars)} bars (in-sample, "
                      "fixed parameters; NOT a validation)")
                for key, value in metrics.items():
                    print(f"    {key:<24} {value:.4f}")
                continue
            settings = OptimizerSettings.from_config(cfg, cfg.risk_per_trade_pct(is_real))
            result = optimize_symbol(symbol, bars, spec, costs, settings, cfg.strategy_names(),
                                     args.workers or cfg.optimizer_workers)
            lines = [f"{symbol}: walk-forward out-of-sample report ({result.trials} trials, {result.seconds:.0f}s)"]
            for cand in result.candidates:
                m = cand.metrics
                lines.append(
                    f"  {'PASS' if cand.passed else 'fail'} {cand.strategy:<24} trades {int(m.get('trades', 0)):>5} "
                    f"expR {m.get('expectancy_r', 0):+.3f} PF {m.get('profit_factor', 0):.2f} "
                    f"DD {m.get('max_drawdown_r', 0):.1f}R DSR {m.get('deflated_sharpe', 0):.2f} "
                    f"score {cand.score:.3f}")
                for reason in cand.reasons:
                    lines.append(f"       - {reason}")
            text = "\n".join(lines)
            print(text)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
            (out_dir / f"backtest_{symbol}_{stamp}.txt").write_text(text + "\n", encoding="utf-8")
        return EXIT_OK
    finally:
        registry.close()
        if bot is not None:
            bot.shutdown()


def _office(cfg: Config, args: argparse.Namespace) -> Any:
    from .officedb import OfficeStore, office_path

    path = office_path(resolve_state_dir(cfg, args))
    if not path.exists():
        raise ValueError(f"{path} not found: the v3 team has not run for this account yet")
    return OfficeStore(path)


def cmd_ablation_report(cfg: Config, args: argparse.Namespace) -> int:
    from .team.ablation import ablation_report
    from .team.ensemble import EnsembleSettings
    from .team.roster import GATES, VOTERS

    store = _office(cfg, args)
    try:
        report = ablation_report(store.resolved_dossiers(), EnsembleSettings.from_config(cfg.team),
                                 list(VOTERS) + list(GATES), cfg.team.ablation_confidence, cfg.team.ablation_min_samples)
    finally:
        store.close()
    lines = [f"Team ablation report: {report['n']} resolved decisions (minimum {report['min_samples']})",
             f"Verdict: {report['verdict']}", ""]
    for name in ("baseline", "team", "team_without_advisor"):
        m = report.get(name) or {}
        if m:
            lines.append(f"  {name:<22} n {m['n']:>5}  trades {m['trades']:>5}  total {m['total_r']:+9.2f}R  "
                         f"sharpe/proposal {m['sharpe']:+.4f}  max DD {m['max_drawdown_r']:.2f}R")
    if report.get("team_vs_baseline_ci"):
        lo, hi = report["team_vs_baseline_ci"]
        lines.append(f"  team - baseline Sharpe, {cfg.team.ablation_confidence:.0%} interval: [{lo:+.4f}, {hi:+.4f}]")
    lines.append("")
    lines.append("Value added per agent (Sharpe of the team with the agent minus without it):")
    for agent, entry in sorted((report.get("agents") or {}).items(), key=lambda kv: -kv[1]["value_added_sharpe"]):
        ci = entry.get("ci") or [None, None]
        ci_text = f"[{ci[0]:+.4f}, {ci[1]:+.4f}]" if ci[0] is not None else "n/a"
        lines.append(f"  {agent:<24} {entry['value_added_sharpe']:+.4f}  interval {ci_text}  n {entry['n']}")
    advisor = report.get("advisor") or {}
    if advisor:
        lines.append(f"  {'claude advisor':<24} {advisor.get('value_added_sharpe', 0):+.4f}  reviews "
                     f"{advisor.get('reviews', 0)}")
    lines.append("")
    lines.append("Past results only; no forecast and no guarantee of profit.")
    print("\n".join(lines))
    return EXIT_OK


def cmd_reset_team_weights(cfg: Config, args: argparse.Namespace) -> int:
    store = _office(cfg, args)
    try:
        store.reset_weights()
        store.set_kv("perf_benched", "[]")
        store.add_control_audit("reset_team_weights", "cli", True, "learned weights, calibration and benching reset")
    finally:
        store.close()
    print("Team weights, calibration and performance benching reset to uniform. A running bot keeps its in-memory "
          "weights until it restarts.")
    return EXIT_OK


def countdown(bot: Bot, seconds: int = COUNTDOWN_SECONDS) -> bool:
    """Real-money banner. Returns False if a stop was requested during the countdown."""
    for remaining in range(seconds, 0, -1):
        log.warning("REAL MONEY trading starts in %d s (Ctrl+C to abort; --no-countdown skips this)", remaining)
        bot._wait(1)
        if bot._stop.is_set():
            return False
    return True


def run_bot(cfg: Config, args: argparse.Namespace, overrides: dict[str, Any]) -> int:
    setup_logging(cfg.log_file)
    log.info("mt5_bot %s starting%s", __version__, " in DRY RUN mode" if cfg.dry_run else "")
    try:
        bot = Bot(cfg, config_path=args.config, confirm_real_money=args.confirm_real_money, cli_overrides=overrides,
                  no_claude=args.no_claude)
    except (StartupError, ValueError) as exc:
        log.error("Startup failed: %s", exc)
        return EXIT_STARTUP
    install_signal_handlers(bot)
    try:
        try:
            bot.startup()
        except AccountPolicyError as exc:
            log.critical("Account not allowed: %s", exc)
            return EXIT_SAFETY_STOP
        except (StartupError, ValueError) as exc:
            log.error("Startup failed: %s", exc)
            return EXIT_STARTUP
        if bot.is_real and not args.no_countdown and not countdown(bot):
            log.info("Countdown aborted; no trading happened.")
            return EXIT_OK
        try:
            return bot.run()
        except Exception:  # noqa: BLE001 - last-resort log before exit
            log.exception("Unexpected fatal error; open positions keep their server-side SL/TP.")
            bot.notifier.send("Bot crashed with an unexpected error; the supervisor will restart it.")
            return EXIT_ERROR
    finally:
        bot.shutdown()
        bot.close()
        log.info("MT5 connection closed.")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        cfg = Config.load(args.config)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_STARTUP
    if args.set_claude_key:
        from .advisor.keys import set_key_interactive

        return set_key_interactive()
    overrides: dict[str, Any] = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.bootstrap_days is not None:
        overrides["bootstrap_days"] = args.bootstrap_days
    if args.no_claude:
        overrides["claude.enabled"] = False
    if args.team_mode is not None:
        overrides["team.team_mode"] = args.team_mode
    if overrides:
        try:
            cfg = apply_overrides(cfg, overrides)
        except ValueError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return EXIT_STARTUP
    try:
        if args.status:
            return cmd_status(cfg, args)
        if args.history:
            return cmd_history(cfg, args)
        if args.rollback is not None:
            return cmd_rollback(cfg, args)
        if args.resume:
            return cmd_resume(cfg, args)
        if args.ablation_report:
            return cmd_ablation_report(cfg, args)
        if args.reset_team_weights:
            return cmd_reset_team_weights(cfg, args)
        if args.simulate:
            from .sim.runner import main_simulate

            return main_simulate(cfg, args)
        if args.optimize_once or args.backtest:
            # Offline runs are child processes of the live bot: console only, to avoid two writers on bot.log.
            setup_logging(None if args.offline else cfg.log_file)
            return cmd_optimize(cfg, args) if args.optimize_once else cmd_backtest(cfg, args)
    except (ValueError, StartupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_STARTUP
    return run_bot(cfg, args, overrides)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
