"""Command line entry point (``python mt5_bot.py ...``): the single entry point of the system."""

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
from . import strategies as strategy_lib
from .advisor.redact import RedactingFormatter
from .backtest import compute_metrics, simulate
from .bot import (EXIT_ERROR, EXIT_OK, EXIT_SAFETY_STOP, EXIT_STARTUP, AccountPolicyError, Bot, StartupError, mask)
from .config import Config, apply_overrides
from .db import DB_NAME, LEGACY_REGISTRY, db_path
from .persistence import account_state_dir
from .registry import Registry
from .reports import format_health
from .research import ResearchLock, cost_model, research_settings, run_research, universe_from_registry
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
    parser = argparse.ArgumentParser(description="MetaTrader 5 trading bot v3.1: one integrated system (arms with "
                                                 "evidence tiers, specialist team, optional Claude advisor).")
    parser.add_argument("--config", default="config.json", help="path to config.json (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="run everything but never send orders")
    parser.add_argument("--confirm-real-money", action="store_true",
                        help="one-time explicit confirmation required to trade a REAL account")
    parser.add_argument("--no-countdown", action="store_true", help="skip the 10 s real-money countdown")
    parser.add_argument("--bootstrap-days", type=float, default=None, help=argparse.SUPPRESS)  # deprecated
    parser.add_argument("--no-claude", action="store_true",
                        help="run without the optional Claude advisor (the bot is complete without it)")
    parser.add_argument("--team-mode", choices=("auto", "full", "baseline"), default=None,
                        help="override team.team_mode: auto (evidence decides), full (team reductions apply) or "
                             "baseline (simple rules; the team only records opinions)")
    parser.add_argument("--risk-profile", choices=("conservative", "balanced", "aggressive"), default=None,
                        help="override risk_profile for this run")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="print health.json: equity, tiers, budgets, status")
    actions.add_argument("--diagnose", action="store_true",
                         help="why is (or isn't) the bot trading? Funnel and top blockers (PT-PT)")
    actions.add_argument("--arms", action="store_true", help="list the arms with their tier and evidence")
    actions.add_argument("--history", action="store_true", help="print the audit log (tier changes, research...)")
    actions.add_argument("--rollback", type=int, metavar="ID",
                         help="undo the tier change with audit ID (restores the previous tier, pinned 24 h)")
    actions.add_argument("--resume", action="store_true", help="clear the drawdown circuit breaker")
    actions.add_argument("--rebase-floor", action="store_true",
                         help="start a new risk epoch: the profit floor restarts from current equity (audited)")
    actions.add_argument("--risk-report", action="store_true",
                         help="Monte Carlo of the risk profile: ruin probability, drawdowns, recovery (estimates)")
    actions.add_argument("--migrate", action="store_true",
                         help="back up and migrate an older install to v3.1 (also runs on first start)")
    actions.add_argument("--rollback-upgrade", action="store_true",
                         help="restore the state and config.json saved by the last upgrade backup")
    actions.add_argument("--backtest", action="store_true", help="walk-forward report (or one strategy with --strategy)")
    actions.add_argument("--research-once", action="store_true", help="run one research cycle and exit")
    actions.add_argument("--optimize-once", action="store_true", help=argparse.SUPPRESS)  # alias kept for 3.0 tasks
    actions.add_argument("--set-claude-key", action="store_true",
                         help="store the Claude API key in the Windows Credential Manager (hidden prompt)")
    actions.add_argument("--simulate", action="store_true",
                         help="run the real bot and team on a synthetic market (no MT5, no broker, no API key)")
    actions.add_argument("--ablation-report", action="store_true",
                         help="print the team's value-added report (ablation) and the team-vs-baseline verdict")
    actions.add_argument("--reset-team-weights", action="store_true",
                         help="reset learned agent weights, calibration and performance benching (audited)")
    parser.add_argument("--hours", type=float, default=24.0, help="with --diagnose: window in hours")
    parser.add_argument("--days", type=int, default=250, help="with --risk-report: trading days simulated")
    parser.add_argument("--paths", type=int, default=2000, help="with --risk-report: Monte Carlo paths")
    parser.add_argument("--backup", default=None, help="with --rollback-upgrade: backup folder name")
    parser.add_argument("--force", action="store_true", help="with --rollback-upgrade: skip the running-bot check")
    parser.add_argument("--sim-bars", type=int, default=None, help="with --simulate: stop after N bars")
    parser.add_argument("--sim-speed", type=float, default=2.0, help="with --simulate: bars per second (0 = max)")
    parser.add_argument("--sim-seed", type=int, default=7, help="with --simulate: random seed of the market")
    parser.add_argument("--sim-state", default="state_sim", help="with --simulate: state folder (default %(default)s)")
    parser.add_argument("--sim-fast-forward", type=int, default=0,
                        help="with --simulate: run the first N bars at full speed")
    parser.add_argument("--sim-advisor", choices=("simulated", "none", "claude"), default="simulated",
                        help="with --simulate: advisor to use (default: a rule-based simulated one, no API)")
    parser.add_argument("--sim-edge", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--offline", action="store_true", help="use only the cached data (no MT5 connection)")
    parser.add_argument("--state-dir", help="account state folder for offline commands")
    parser.add_argument("--account", help="state folder name (<server>_<login>) for offline commands")
    parser.add_argument("--trigger", default="manual", help=argparse.SUPPRESS)
    parser.add_argument("--research-mode", choices=("startup", "full"), default="full", help=argparse.SUPPRESS)
    parser.add_argument("--workers", type=int, default=None, help="research worker processes")
    parser.add_argument("--strategy", help="with --backtest: one strategy name")
    parser.add_argument("--params", help="with --backtest --strategy: JSON parameters (default: grid middle)")
    parser.add_argument("--limit", type=int, default=30, help="rows for --history and --arms")
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
    folders = sorted(p for p in root.iterdir() if p.is_dir() and ((p / DB_NAME).exists() or
                                                               (p / LEGACY_REGISTRY).exists())) if root.exists() else []
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


def _registry(cfg: Config, args: argparse.Namespace) -> Registry:
    """Open the account database (migrating an older layout first, after a backup)."""
    from . import upgrade

    state_dir = resolve_state_dir(cfg, args)
    if upgrade.needs_migration(state_dir):
        project = Path(args.config).resolve().parent
        upgrade.migrate_installation(project, Path(args.config), Path(cfg.state_root), Path(cfg.health_file),
                                     cfg.timeframe)
    return Registry(db_path(state_dir))


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


def cmd_diagnose(cfg: Config, args: argparse.Namespace) -> int:
    from .diagnose import diagnose

    state_dir = resolve_state_dir(cfg, args)
    log_path = Path(cfg.log_file)
    logs = [log_path] + [log_path.with_name(f"{log_path.name}.{i}") for i in range(1, 6)]
    print(diagnose(state_dir, float(args.hours), [p for p in logs if p.exists()]))
    return EXIT_OK


def cmd_arms(cfg: Config, args: argparse.Namespace) -> int:
    from .arms import ArmEngine

    registry = _registry(cfg, args)
    try:
        engine = ArmEngine(registry, cfg.tiers, cfg.research)
        engine.refresh(force=True)
        counts = engine.counts()
        print(f"Arms: {sum(counts.values())} active ({', '.join(f'{k} {v}' for k, v in counts.items())})")
        print(f"{'id':>5} {'tier':<4} {'P(E>0)':>7} {'E[R]':>8} {'90% interval':>18} {'trades':>6} {'prior':>5}  arm")
        for arm in engine.ranked(args.limit):
            p = arm.posterior
            if p is None:
                continue
            print(f"{arm.id:>5} {arm.tier:<4} {p.p_positive:>7.2f} {p.mean:>+8.3f} "
                  f"[{p.lo:>+7.3f},{p.hi:>+7.3f}] {p.n_obs:>6} {arm.prior_n:>5}  {arm.describe()}")
        print("\nEstimates from past data (walk-forward prior, shadow and live trades). No forecast, no guarantee.")
        return EXIT_OK
    finally:
        registry.close()


def cmd_history(cfg: Config, args: argparse.Namespace) -> int:
    registry = _registry(cfg, args)
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
    from .arms import ArmEngine

    registry = _registry(cfg, args)
    try:
        row = registry.promotion(args.rollback)
        if row is None or not str(row["action"]).startswith(("tier_", "manual")):
            print(f"Decision #{args.rollback} is not a tier change; see --history")
            return EXIT_ERROR
        metrics = json.loads(row["metrics_json"] or "{}")
        arm_id = metrics.get("arm_id")
        old_tier = str(row["old_desc"]).split(" ", 1)[0]
        if arm_id is None:
            print(f"Decision #{args.rollback} does not name its arm; nothing to roll back")
            return EXIT_ERROR
        engine = ArmEngine(registry, cfg.tiers, cfg.research)
        audit = engine.pin(int(arm_id), old_tier, 24.0, f"rollback of decision #{args.rollback}")
        print(f"Rollback recorded as decision #{audit}: arm #{arm_id} is back at {old_tier}, pinned against "
              "promotions for 24 h (demotions on evidence still apply). A running bot reloads it on the next refresh.")
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


def cmd_rebase_floor(cfg: Config, args: argparse.Namespace) -> int:
    from .capital import CapitalState

    registry = _registry(cfg, args)
    try:
        state = CapitalState.from_json(registry.get_kv("capital_state"))
        equity = float(registry.get_kv("equity") or 0.0)
        if equity <= 0:
            print("No equity recorded yet; start the bot once first.")
            return EXIT_ERROR
        before = state.c0
        state.rebase(equity, time.time())
        registry.set_kv("capital_state", state.to_json())
        registry.log_event("*", "rebase_floor", f"manual: risk epoch restarted at equity {equity:.2f} "
                           f"(previous starting capital {before})", {"equity": equity, "previous_c0": before})
        print(f"New risk epoch from equity {equity:.2f}: the profit floor restarts below it. A running bot picks this "
              "up when it restarts (stop it first so it does not overwrite the state).")
        return EXIT_OK
    finally:
        registry.close()


def cmd_risk_report(cfg: Config, args: argparse.Namespace) -> int:
    from .profiles import effective_limits
    from .riskreport import DEFAULT_MIX, estimate_inputs, format_report_pt, run_report

    is_real, trades_per_day, mix = cfg.account_mode == "real_only", 3.0, dict(DEFAULT_MIX)
    try:
        registry = _registry(cfg, args)
        try:
            is_real = registry.get_kv("is_real") == "1"
            trades_per_day, mix = estimate_inputs(registry)
        finally:
            registry.close()
    except ValueError:
        pass  # no account yet: generic estimate
    lim = effective_limits(cfg, is_real)
    results = run_report(lim, args.days, args.paths, trades_per_day, mix)
    print(format_report_pt(lim, results, args.days, args.paths, trades_per_day, mix))
    return EXIT_OK


def cmd_migrate(cfg: Config, args: argparse.Namespace, migrated: dict, notices: list[str]) -> int:
    from . import upgrade
    from .config_migration import write_migrated

    project = Path(args.config).resolve().parent
    report = upgrade.migrate_installation(project, Path(args.config), Path(cfg.state_root), Path(cfg.health_file),
                                          cfg.timeframe, force_backup=True)
    print(f"Backup: {report['backup']}")
    for acc in report["accounts"]:
        print(f"  {Path(acc['state_dir']).name}: {acc['action']}"
              + (f", arms {acc.get('arms')}" if acc.get("arms") else "")
              + (f", schema v{acc.get('version')}" if acc.get("version") else ""))
    if not report["accounts"]:
        print("  No account state yet: nothing to migrate (the bot creates mt5bot.sqlite on first start).")
    if notices:
        target = write_migrated(Path(args.config), migrated)
        print(f"\nConfig keys were mapped ({len(notices)} notice(s)); migrated copy written to {target}:")
        for n in notices:
            print(f"  - {n}")
        print("config.json itself was NOT changed. Review the migrated file, then replace config.json with it.")
    else:
        print("config.json is already in the v3.1 format.")
    print("Rollback, if ever needed: python mt5_bot.py --rollback-upgrade")
    return EXIT_OK


def cmd_rollback_upgrade(cfg: Config, args: argparse.Namespace) -> int:
    from . import upgrade

    project = Path(args.config).resolve().parent
    try:
        report = upgrade.rollback_upgrade(project, Path(args.config), Path(cfg.state_root), args.backup,
                                          Path(cfg.health_file), force=args.force)
    except ValueError as exc:
        print(f"Rollback refused: {exc}")
        return EXIT_ERROR
    print(f"Restored {report['restored_from']} ({report['state_files']} state files, backup made by version "
          f"{report['backup_code_version']} at {report['backup_created_utc']}).")
    print(f"The replaced state and config are kept in {report['moved_aside_to']}.")
    print("Now reinstall the previous program version (see docs/UPGRADE.md, 'Voltar atrás') before starting the bot.")
    return EXIT_OK


def _connect_for_data(cfg: Config, config_path: str) -> tuple[Bot, Registry]:
    from .bot import resolve_symbol

    bot = Bot(cfg, config_path=config_path)
    if not bot._initialize():
        raise StartupError(f"mt5.initialize() failed: {bot._last_error()}")
    acc = bot.mt5.account_info()
    if acc is None:
        raise StartupError("account_info() returned nothing; log in to an account")
    bot.is_real = not bot._is_demo(acc)
    bot.state_dir = account_state_dir(cfg.state_root, acc.server, acc.login)
    bot._migrate_if_needed()
    bot.registry = Registry(db_path(bot.state_dir))
    raw = bot.registry.get_kv("universe")
    wanted = (json.loads(raw).get("symbols") if raw else None) or list(cfg.symbols)
    for name in wanted:
        resolved = resolve_symbol(bot.mt5, name)
        if resolved is None:
            log.warning("Symbol %s not found; skipped", name)
            continue
        bot.mt5.symbol_select(resolved, True)
        bot.symbols[name] = resolved
    bot.universe = list(bot.symbols.values())
    bot.registry.set_kv("is_real", "1" if bot.is_real else "0")
    if not bot.refresh_market_data("full"):
        log.warning("Some symbols returned no history")
    log.info("Data cached for account %s (%s)", mask(acc.login), "REAL" if bot.is_real else "DEMO")
    return bot, bot.registry


def _open_registry(cfg: Config, args: argparse.Namespace) -> tuple[Optional[Bot], Registry, Path]:
    if args.offline:
        state_dir = resolve_state_dir(cfg, args)
        return None, Registry(db_path(state_dir)), state_dir
    bot, registry = _connect_for_data(cfg, args.config)
    return bot, registry, bot.state_dir  # type: ignore[return-value]


def cmd_research(cfg: Config, args: argparse.Namespace) -> int:
    bot, registry, state_dir = _open_registry(cfg, args)
    try:
        lock = ResearchLock(state_dir)
        if not lock.acquire():
            print("Another research cycle is already running for this account; exiting.")
            return EXIT_OK
        with lock:
            results = run_research(cfg, registry, args.trigger, args.research_mode, workers=args.workers,
                                   lower_priority=args.offline)
        for item in results:
            if item["status"] == "done":
                print(f"{item['symbol']} {item['timeframe']}: run {item['run_id']}, {item['families']} families, "
                      f"{item['arms_with_prior']} arm priors, {item['seconds']}s")
            else:
                print(f"{item['symbol']} {item['timeframe']}: run {item['run_id']} FAILED: {item['error']}")
        return EXIT_OK
    finally:
        registry.close()
        if bot is not None:
            bot.shutdown()


def cmd_backtest(cfg: Config, args: argparse.Namespace) -> int:
    from .optimizer import optimize_symbol

    bot, registry, state_dir = _open_registry(cfg, args)
    try:
        out_dir = Path(cfg.reports_dir) / state_dir.name
        out_dir.mkdir(parents=True, exist_ok=True)
        symbols, _tfs = universe_from_registry(cfg, registry)
        tf = cfg.timeframe
        for symbol in symbols:
            spec_row = registry.load_spec(symbol)
            bars = registry.load_bars(symbol, tf)
            if spec_row is None or len(bars) < 500:
                print(f"{symbol}: not enough cached {tf} data ({len(bars)} bars); run without --offline first")
                continue
            spec = SymbolSpec.from_json(spec_row[0])
            costs = cost_model(cfg, spec_row[1], symbol)
            ref_equity, ref_risk = cfg.research.reference_equity, cfg.research.reference_risk_pct
            if args.strategy:
                strat = strategy_lib.get(args.strategy)
                params = strat.validate_params(json.loads(args.params)) if args.params else strat.default_params()
                trades = simulate(bars, strat.signals(bars, params), spec, costs, ref_equity, ref_risk)
                metrics = compute_metrics(trades, ref_equity, bars["time"], 0, len(bars), cfg.bar_seconds)
                print(f"{symbol} {tf}: {strategy_lib.describe(strat, params)} over {len(bars)} bars (in-sample, "
                      "fixed parameters; NOT a validation)")
                for key, value in metrics.items():
                    print(f"    {key:<24} {value:.4f}")
                continue
            result = optimize_symbol(symbol, bars, spec, costs, research_settings(cfg, tf, "full"),
                                     cfg.strategy_names(), args.workers or cfg.optimizer_workers)
            lines = [f"{symbol} {tf}: walk-forward out-of-sample report ({result.trials} trials, {result.seconds:.0f}s)."
                     " Flags are quality information; evidence tiers decide what trades."]
            for cand in result.candidates:
                m = cand.metrics
                lines.append(
                    f"  {'clean' if cand.passed else 'flags'} {cand.strategy:<24} trades {int(m.get('trades', 0)):>5} "
                    f"expR {m.get('expectancy_r', 0):+.3f} PF {m.get('profit_factor', 0):.2f} "
                    f"DD {m.get('max_drawdown_r', 0):.1f}R DSR {m.get('deflated_sharpe', 0):.2f} "
                    f"score {cand.score:.3f}")
                for reason in cand.reasons:
                    lines.append(f"       - {reason}")
            text = "\n".join(lines)
            print(text)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
            (out_dir / f"backtest_{symbol}_{tf}_{stamp}.txt").write_text(text + "\n", encoding="utf-8")
        return EXIT_OK
    finally:
        registry.close()
        if bot is not None:
            bot.shutdown()


def _office(cfg: Config, args: argparse.Namespace) -> Any:
    from .officedb import OfficeStore

    path = db_path(resolve_state_dir(cfg, args))
    if not path.exists():
        raise ValueError(f"{path} not found: the bot has not run for this account yet (or run --migrate)")
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


def run_bot(cfg: Config, args: argparse.Namespace, overrides: dict[str, Any], notices: list[str],
            migrated: dict) -> int:
    setup_logging(cfg.log_file)
    log.info("mt5_bot %s starting%s", __version__, " in DRY RUN mode" if cfg.dry_run else "")
    if notices:
        from .config_migration import write_migrated

        for notice in notices:
            log.warning("Config migration: %s", notice)
        try:
            target = write_migrated(Path(args.config), migrated)
            log.warning("config.json uses older keys; they were mapped in memory and a migrated copy was written to "
                        "%s (config.json itself is unchanged)", target)
        except OSError as exc:
            log.warning("Could not write the migrated config copy: %s", exc)
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
        cfg, notices, migrated = Config.load_with_notices(args.config)
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
        notices.append("--bootstrap-days belonged to the v3.0 champion promotion and is ignored: evidence tiers "
                       "decide when an arm trades")
    if args.no_claude:
        overrides["claude.enabled"] = False
    if args.team_mode is not None:
        overrides["team.team_mode"] = args.team_mode
    if args.risk_profile is not None:
        overrides["risk_profile"] = args.risk_profile
    if overrides:
        try:
            cfg = apply_overrides(cfg, overrides)
        except ValueError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return EXIT_STARTUP
    try:
        if args.status:
            return cmd_status(cfg, args)
        if args.diagnose:
            return cmd_diagnose(cfg, args)
        if args.arms:
            return cmd_arms(cfg, args)
        if args.history:
            return cmd_history(cfg, args)
        if args.rollback is not None:
            return cmd_rollback(cfg, args)
        if args.resume:
            return cmd_resume(cfg, args)
        if args.rebase_floor:
            return cmd_rebase_floor(cfg, args)
        if args.risk_report:
            return cmd_risk_report(cfg, args)
        if args.migrate:
            setup_logging(None)
            return cmd_migrate(cfg, args, migrated, notices)
        if args.rollback_upgrade:
            setup_logging(None)
            return cmd_rollback_upgrade(cfg, args)
        if args.ablation_report:
            return cmd_ablation_report(cfg, args)
        if args.reset_team_weights:
            return cmd_reset_team_weights(cfg, args)
        if args.simulate:
            from .sim.runner import main_simulate

            return main_simulate(cfg, args)
        if args.research_once or args.optimize_once or args.backtest:
            # Offline runs are child processes of the live bot: console only, to avoid two writers on bot.log.
            setup_logging(None if args.offline else cfg.log_file)
            return cmd_backtest(cfg, args) if args.backtest else cmd_research(cfg, args)
    except (ValueError, StartupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_STARTUP
    return run_bot(cfg, args, overrides, notices, migrated)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
