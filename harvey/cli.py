"""Harvey CLI — simple commands to install, setup, run, and manage Harvey."""

import argparse
import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path


MIN_PYTHON = (3, 11)


def _check_python_version():
    """Refuse to install on a Python that cannot run Harvey.

    `python3 -m venv .venv` on macOS builds the venv from /usr/bin/python3,
    which is 3.9 — old enough that Harvey's `X | Y` type syntax fails at
    import. The install itself appears to succeed and the failure surfaces
    later as an unrelated-looking ImportError, so check up front and name the
    fix.
    """
    if sys.version_info >= MIN_PYTHON:
        return

    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    want = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    print(f"\n  Harvey needs Python {want} or newer. This is Python {have}.")
    print(f"  ({sys.executable})\n")
    print("  On macOS, `python3` is usually the system 3.9, so a venv built")
    print("  with it is 3.9 too. Build the venv from a newer Python instead:\n")
    print("    brew install python@3.13")
    print("    rm -rf .venv")
    print("    $(brew --prefix)/bin/python3.13 -m venv .venv")
    print("    source .venv/bin/activate && pip install -e .\n")
    sys.exit(1)


def cmd_install(args):
    """Install all dependencies including Playwright browsers."""
    _check_python_version()
    print("\n  Installing Harvey dependencies...\n")

    # Install Python packages
    print("  [1/2] Installing Python packages...")
    requirements = Path(args._project_root) / "requirements.txt"
    if requirements.exists():
        pip_args = ["-r", "requirements.txt"]
    else:
        # Fall back to an editable install from pyproject.toml
        pip_args = ["-e", "."]
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *pip_args],
        cwd=args._project_root,
    )
    if result.returncode != 0:
        print("\n  Failed to install Python packages.")
        print("  Tip: make sure you're inside a virtualenv "
              "(python3 -m venv .venv && source .venv/bin/activate).")
        sys.exit(1)
    print("  ✓ Python packages installed.\n")

    # Install Playwright browsers
    print("  [2/2] Installing Playwright browsers...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=600,
        )
        playwright_ok = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        playwright_ok = False
    if not playwright_ok:
        print("\n  Playwright browser install failed (optional — needed for LinkedIn).")
    else:
        print("  ✓ Playwright browsers installed.\n")

    _ensure_importable(args._project_root)
    print("  Harvey is installed. Run 'harvey setup' next.\n")


def _ensure_importable(project_root: str):
    """Make sure `import harvey` works outside the repo directory.

    Python 3.13 silently skips .pth files carrying the macOS 'hidden'
    file flag, and some Macs propagate that flag to everything inside
    dot-directories like .venv — which breaks editable installs. When
    that happens, fall back to symlinking the package into site-packages
    (imports don't check the hidden flag; only .pth parsing does).
    """
    check = subprocess.run(
        [sys.executable, "-c", "import harvey"],
        cwd="/", capture_output=True,
    )
    if check.returncode == 0:
        return

    try:
        import site
        site_packages = Path(site.getsitepackages()[0])
        link = site_packages / "harvey"
        target = Path(project_root) / "harvey"
        if not link.exists() and target.is_dir():
            link.symlink_to(target)
            recheck = subprocess.run(
                [sys.executable, "-c", "import harvey"],
                cwd="/", capture_output=True,
            )
            if recheck.returncode == 0:
                print("  ✓ Fixed package visibility (editable .pth was being "
                      "ignored; linked the package directly).\n")
                return
    except OSError as e:
        print(f"  Could not apply import fix: {e}")

    print("\n  Warning: 'import harvey' fails outside the project directory.")
    print("  Run harvey commands from the project root, or reinstall with:")
    print("    pip install -e . --config-settings editable_mode=compat\n")


def cmd_setup(args):
    """Run the interactive setup wizard."""
    from harvey.setup import run_setup

    asyncio.run(run_setup())


def cmd_run(args):
    """Start Harvey's heartbeat loop."""
    from harvey.main import main

    main()


def cmd_train(args):
    """Train Harvey on a website."""
    from harvey.trainer import Trainer

    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
        print(f"  No scheme given — using {url}")

    if args.max_pages < 1:
        print("  max_pages must be at least 1.")
        sys.exit(2)

    from pydantic import ValidationError
    from harvey.config import BrainConfig, ConfigError, load_brain_config

    try:
        brain_config = load_brain_config()
        overrides = {
            k: v for k, v in (
                ("provider", args.provider),
                ("model", args.model),
                ("reasoning_effort", args.reasoning_effort),
            ) if v is not None
        }
        if overrides:
            if "provider" in overrides and overrides["provider"] != brain_config.provider:
                # A different provider never inherits the other one's model.
                brain_config = BrainConfig(**overrides)
            else:
                brain_config = BrainConfig(**{**brain_config.model_dump(), **overrides})
    except (ConfigError, ValidationError) as e:
        print(f"  {e}")
        sys.exit(2)

    trainer = Trainer(brain_config=brain_config)
    asyncio.run(trainer.train(url, max_pages=args.max_pages))


def cmd_dashboard(args):
    """Launch the local web dashboard."""
    from harvey.dashboard import DashboardSetupError, dashboard_url, start_dashboard
    from harvey.dashboard_auth import TokenError

    if not 1 <= args.port <= 65535:
        print(f"  Invalid port: {args.port}. Must be 1-65535.")
        sys.exit(2)

    try:
        allow_remote = getattr(args, "allow_remote", False)
        tunnel = getattr(args, "tunnel", False)
        send_wa = getattr(args, "send_wa", False)
        wa_phone = getattr(args, "phone", "087764021131")

        if getattr(args, "url", False):
            # The unlock link for an already-running dashboard.
            from harvey.paths import PROJECT_ROOT
            urls_file = PROJECT_ROOT / "data" / "dashboard-urls.json"
            if urls_file.exists() and "PYTEST_CURRENT_TEST" not in os.environ:
                try:
                    import json
                    d = json.loads(urls_file.read_text(encoding="utf-8"))
                    url_out = d.get("tunnel") or d.get("lan") or d.get("local")
                    if url_out:
                        print(url_out)
                        return
                except Exception:
                    pass
            print(dashboard_url(host=args.host, port=args.port, allow_remote=allow_remote))
            return
        start_dashboard(
            host=args.host,
            port=args.port,
            allow_remote=allow_remote,
            tunnel=tunnel,
            send_wa=send_wa,
            wa_phone=wa_phone,
            open_browser=not getattr(args, "no_open", False),
        )
    except (DashboardSetupError, TokenError) as e:
        print(f"\n  Dashboard not started: {e}\n")
        sys.exit(2)


def cmd_status(args):
    """Show current pipeline status."""
    from harvey.state import StateManager

    async def _status():
        state = StateManager()
        await state.init_db()
        summary = await state.get_state_summary()

        print("\n  Harvey Pipeline Status")
        print("  " + "=" * 40)
        print(f"  Prospects:            {summary['prospects']}")
        print(f"  Ready to write for:   {summary.get('writable_prospects', 0)} "
              "(new, with a verified or catch-all email)")
        write_failed = (summary.get("prospects") or {}).get("write_failed", 0)
        if write_failed:
            print(f"  Couldn't write for:   {write_failed} (the model's drafts kept coming back "
                  "unusable; `harvey retry-writes` tries again)")
        print(f"  Draft campaigns:      {summary['draft_campaigns']}")
        print(f"  Active campaigns:     {summary['active_campaigns']}")
        print(f"  Open conversations:   {summary['open_conversations']}")
        by_provider = summary.get("usage_today_by_provider", {})
        print(f"  Claude calls today:   {by_provider.get('claude', summary['usage_today'])}")
        print(f"  Codex calls today:    {by_provider.get('codex', 0)}")
        print(f"  Gemini calls today:   {by_provider.get('gemini', 0)}")
        from harvey.config import compliance_notice, load_config, sending_notice
        loaded = None
        try:
            loaded = load_config()
            notice = sending_notice(loaded)
            address_notice = compliance_notice(loaded)
        except Exception:
            # Status must still print when harvey.yaml is broken; that's
            # when people run it. The --check path reports config errors.
            notice = address_notice = ""
        from harvey.main import unusable_backlog_notice
        backlog = unusable_backlog_notice(summary)
        if backlog:
            print("  Prospecting:          PAUSED")
            print(textwrap.fill(backlog, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        if notice:
            print("  Sending:              HELD")
            print(textwrap.fill(notice, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        hold = await state.current_sends_held()
        if hold:
            from harvey.sending_mode import held_message
            waiting = len(await state.get_outbox(status="approved", limit=500))
            _, detail = held_message(hold, waiting=waiting)
            print("  Sending:              NOT SENDING (couldn't read your inbox)")
            print(textwrap.fill(detail, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        from harvey.integrations.precleared import current_hold, hold_message
        precleared_hold = await current_hold(state, loaded)
        if precleared_hold:
            message = hold_message(precleared_hold)
            print(f"  Precleared:           HOLDING COLD EMAIL since {precleared_hold.get('since', '?')}")
            print(textwrap.fill(f"{message['title']}. {message['detail']}", width=72,
                                initial_indent="                        ",
                                subsequent_indent="                        "))
        if address_notice:
            print("  Postal address:       MISSING")
            print(textwrap.fill(address_notice, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        from harvey.brain import BRAIN_STATUS_KEY, Brain, active_brain_status
        from harvey.config import ConfigError, load_brain_config
        try:
            brain_config = load_brain_config()
        except ConfigError as e:
            brain_config, config_error = None, e
        # Only the configured provider's health: an old Codex pause says
        # nothing once harvey.yaml has switched to Claude.
        brain_status = active_brain_status(
            await state.get_setting(BRAIN_STATUS_KEY),
            brain_config.provider if brain_config else "")
        healthy = True
        if getattr(args, "check", False):
            if brain_config is None:
                print(f"  Brain:                config error: {config_error}")
                healthy = False
            else:
                brain = Brain(state, brain_config)
                problem = await brain.preflight()
                label = brain.provider_label + (f" ({brain.model})" if brain.model else "")
                if problem:
                    healthy = False
                    print(f"  Brain:                {label} NOT READY")
                    print(f"                        {problem}")
                elif brain.provider == "codex":
                    print(f"  Brain:                {label} ready (CLI, isolation, login, "
                          "model and account checked; no prompt sent)")
                elif brain.provider == "gemini":
                    print(f"  Brain:                {label} ready (REST API accessible, "
                          "API key valid, model usable; no prompt sent)")
                else:
                    print(f"  Brain:                {label} CLI isolation flags present "
                          "(login not checked: Claude is checked by its first call)")
        elif brain_status.get("status") == "paused":
            label = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}.get(brain_status.get("provider"), "Brain")
            print(f"  Brain:                {label} PAUSED since {brain_status.get('since', '?')}")
            print(f"                        {brain_status.get('reason', '')}")
        print()
        return healthy

    if not asyncio.run(_status()):
        # `harvey status --check` failing is something scripts can act on.
        raise SystemExit(1)


def cmd_usage(args):
    """Show brain usage: active provider's quota, totals, breakdowns."""
    from harvey.state import StateManager
    from harvey.config import ConfigError, load_brain_config

    async def _usage():
        state = StateManager()
        await state.init_db()

        try:
            brain_config = load_brain_config()
        except ConfigError as e:
            print(f"  {e}")
            brain_config = None

        # Live quota for the ACTIVE provider only (best-effort). Each
        # subscription's gauge is read from its own source and labelled as
        # such; one is never shown as the other.
        windows = None
        provider = brain_config.provider if brain_config else None
        try:
            if provider == "codex":
                from harvey.codex import CodexClient, CodexQuotaClient
                windows = await CodexQuotaClient(
                    CodexClient(model=brain_config.model)).get_utilization()
            elif provider == "claude":
                from harvey.integrations.quota import QuotaClient
                windows = await QuotaClient().get_utilization()
        except Exception:
            pass

        label = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}.get(provider, "Brain")
        print(f"\n  {label} Usage")
        print("  " + "=" * 52)
        if windows:
            labels = {"five_hour": "5-hour window", "seven_day": "Weekly",
                      "limit_reached": "Usage limit"}
            for key, w in windows.items():
                resets = f"  (resets {w['resets_at']})" if w.get("resets_at") else ""
                print(f"  {labels.get(key, key):<16} {w['utilization']:5.1f}% used{resets}")
        elif provider == "codex":
            print("  Quota gauge unavailable (check 'codex login', the Codex CLI "
                  "version, and network).")
        elif provider == "claude":
            print("  Quota gauge unavailable (run 'claude login' or check network).")
        elif provider == "gemini":
            print("  Quota gauge not applicable for API key providers.")

        totals = await state.usage_totals()
        print()
        print(f"  {'Period':<10} {'Calls':>7} {'Input':>12} {'Output':>10} {'Cache read':>12}")
        for label, key in (("Today", "today"), ("7 days", "week"), ("30 days", "month")):
            t = totals.get(key) or {}
            print(
                f"  {label:<10} {t.get('calls', 0):>7} "
                f"{t.get('input_tokens', 0):>12,} {t.get('output_tokens', 0):>10,} "
                f"{t.get('cache_read_tokens', 0):>12,}"
            )

        by_provider = await state.usage_by_provider(days=args.days)
        if len(by_provider) > 1 or (by_provider and by_provider[0]["provider"] != "claude"):
            print(f"\n  By provider (last {args.days} days):")
            for row in by_provider:
                print(
                    f"    {row['provider']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )

        by_agent = await state.usage_by_agent(days=args.days)
        if by_agent:
            print(f"\n  By agent (last {args.days} days):")
            for row in by_agent:
                print(
                    f"    {row['agent']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )

        by_task = await state.usage_by_task(days=args.days)
        if by_task:
            print(f"\n  By task (last {args.days} days):")
            for row in by_task[:10]:
                print(
                    f"    {row['task']:<22} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )
        print(
            "\n  Subscription plans aren't billed per token — these are usage"
            "\n  counts, not costs.\n"
        )

    asyncio.run(_usage())


def cmd_export(args):
    """Export the prospect list as a sequencer-ready CSV."""
    from harvey.state import StateManager
    from harvey.export import export_prospects_csv

    async def _export():
        state = StateManager()
        await state.init_db()

        email_statuses = None
        if args.email_status:
            email_statuses = [s.strip() for s in args.email_status.split(",") if s.strip()]
        statuses = None
        if args.status:
            statuses = [s.strip() for s in args.status.split(",") if s.strip()]

        count, _ = await export_prospects_csv(
            state,
            out_path=args.out,
            email_statuses=email_statuses,
            min_score=args.min_score,
            statuses=statuses,
            include_all=args.all,
        )
        scope = "all prospects" if args.all else (
            f"email status {email_statuses or ['verified', 'risky']}"
            + (f", score >= {args.min_score}" if args.min_score else "")
        )
        print(f"\n  Exported {count} prospect(s) to {args.out}  ({scope})")
        if count == 0 and not args.all:
            print("  Tip: no deliverable emails yet? Add a REOON_API_KEY to .env so")
            print("  Harvey can verify addresses, or use --all for the raw list.\n")
        else:
            print("  The CSV imports directly into Instantly, Smartlead, or any sequencer.\n")

    asyncio.run(_export())


def cmd_gmail(args):
    """Gmail provider utilities (auth / test)."""
    from harvey.config import load_env

    env = load_env()
    if args.gmail_action == "auth":
        from harvey.integrations.gmail import run_auth_flow
        ok = run_auth_flow(env.gmail_client_id, env.gmail_client_secret)
        sys.exit(0 if ok else 1)

    if args.gmail_action == "test":
        from harvey.config import load_config
        from harvey.integrations.gmail import GmailProvider

        async def _test():
            provider = GmailProvider(load_config(), env)
            ok, detail = await provider.test_connection()
            print(f"\n  {'✓' if ok else '✗'} {detail}\n")
            sys.exit(0 if ok else 1)
        asyncio.run(_test())


def cmd_microsoft(args):
    """Microsoft 365 provider utilities (auth / test)."""
    from harvey.config import load_env

    env = load_env()
    if args.microsoft_action == "auth":
        from harvey.integrations.microsoft import run_auth_flow
        ok = run_auth_flow(env.m365_client_id, env.m365_tenant_id)
        sys.exit(0 if ok else 1)

    if args.microsoft_action == "test":
        from harvey.config import load_config
        from harvey.integrations.microsoft import MicrosoftProvider

        async def _test():
            provider = MicrosoftProvider(load_config(), env)
            ok, detail = await provider.test_connection()
            print(f"\n  {'✓' if ok else '✗'} {detail}\n")
            sys.exit(0 if ok else 1)
        asyncio.run(_test())


def cmd_outbox(args):
    """Review and approve queued outgoing emails."""
    from harvey.state import StateManager

    async def _outbox():
        state = StateManager()
        await state.init_db()

        if args.approve_all:
            n = await state.approve_outbox()
            print(f"\n  Approved {n} email(s). They'll send on schedule.")
            held = await state.count_held_for_review()
            if held:
                print(f"  {held} email(s) Harvey held for review were left out: read each one,")
                print("  then approve it with --approve <id>.")
            print()
            return
        if args.approve:
            n = await state.approve_outbox(args.approve)
            print(f"\n  {'Approved.' if n else 'No pending item with that id.'}\n")
            return
        if args.reject:
            n = await state.reject_outbox_item(args.reject)
            print(f"\n  {'Rejected.' if n else 'Nothing waiting to send with that id (already sending or sent?).'}\n")
            return

        paused = await state.get_setting("sending_paused")
        if paused:
            print(f"\n  ⚠ SENDING PAUSED: {paused}")
            print("  Resume with: harvey sending resume")

        pending = await state.get_outbox(status="pending_review", limit=50)
        approved = await state.get_outbox(status="approved", limit=10)
        print(f"\n  Outbox — {len(pending)} awaiting approval, "
              f"{len(approved)}+ approved/scheduled")
        print("  " + "=" * 60)
        for item in pending:
            print(f"\n  [{item['id']}] step {item['step']} ({item['kind']}) "
                  f"→ {item['to_email']}  (send {item['send_at'][:16]})")
            print(f"  Subject: {item['subject']}")
            body_preview = (item["body"][:200] + "...") if len(item["body"]) > 200 else item["body"]
            for line in body_preview.splitlines():
                print(f"    {line}")
        if pending:
            print("\n  Approve: harvey outbox --approve <id>   |   all: harvey outbox --approve-all")
            print("  Reject:  harvey outbox --reject <id>\n")
        else:
            print("  Nothing awaiting review.\n")

    asyncio.run(_outbox())


def cmd_profile(args):
    """Read what companies' own websites say about them. Free, no Claude calls."""
    from harvey.state import StateManager
    from harvey.pipeline import run_profile_stage

    async def _profile():
        state = StateManager()
        await state.init_db()

        pending = await state.count_companies_needing_profile(args.stale_days)
        if not pending:
            print("\n  Nothing to profile. Every company with a website has "
                  "been read recently.\n")
            return

        print(f"\n  {pending} companies need profiling. Reading up to "
              f"{args.limit}...\n")
        try:
            from harvey.config import load_config
            config = load_config()
        except Exception:
            config = None   # still profiles; no role is singled out as hiring intent
        companies, observations, _ = await run_profile_stage(
            state, limit=args.limit, stale_days=args.stale_days, config=config)
        print(f"  Read {companies} sites → {observations} observations (free)")
        print("\n  See what turned up: harvey dashboard → Signals\n")

    asyncio.run(_profile())


def cmd_discover(args):
    """Find businesses. The only stage that spends money, so it estimates first."""
    from harvey.state import StateManager
    from harvey.config import load_config
    from harvey.collectors.discover import (
        PROVIDERS, build_queries, preset_problem, provider_menu, run_discovery,
    )

    async def _discover():
        if args.providers:
            print("\n  Discovery providers")
            print("  " + "=" * 68)
            for m in provider_menu():
                mark = "ready" if m["configured"] else "needs setup"
                print(f"\n  {m['label']}  [{m['key']}]  ({mark})")
                print(f"    {m['blurb']}")
                print(f"    Cost: {m['cost_note']}")
                print(f"    Free: {m['free_tier']}")
                if m["env_keys"]:
                    print(f"    Needs: {', '.join(m['env_keys'])} in .env  —  {m['signup_url']}")
                if m["caveat"]:
                    print(f"    Note: {m['caveat']}")
            print("\n  Run one with: harvey discover --provider <key> --estimate\n")
            return

        if args.provider not in PROVIDERS:
            print(f"\n  Unknown provider {args.provider!r}. "
                  f"See: harvey discover --providers\n")
            return

        config = load_config()
        state = StateManager()
        await state.init_db()

        # Semicolons, not commas: "Denver, CO" is one city, not two.
        cities = [c.strip() for c in args.city.split(";") if c.strip()] or None
        preset = getattr(args, "preset", "") or ""
        problem = preset_problem(preset, args.provider, config=config)
        if problem:
            print(f"\n  {problem}\n")
            sys.exit(2)
        queries = build_queries(config, cities=cities,
                                depth=args.depth, limit=args.limit, preset=preset)

        report = await run_discovery(state, config, args.provider, queries,
                                     max_spend=args.max_spend, dry_run=True)
        print(f"\n  {len(queries)} queries via {PROVIDERS[args.provider].label}")
        for q in queries[:8]:
            print(f"    - {q.keyword()}" + ("" if q.coordinate else
                  "   (no coordinates — add icp.geo_coordinates for radius search)"))
        if len(queries) > 8:
            print(f"    ... and {len(queries) - 8} more")
        print(f"\n  Estimated cost: ${report.estimated_cost:.4f}"
              f"   (cap: ${args.max_spend:.2f})")

        if args.estimate:
            print("\n  Estimate only. Re-run without --estimate to collect.\n")
            return

        print("\n  Running...\n")
        from harvey.pipeline import run_prospecting

        result = await run_prospecting(
            state, config, args.provider, queries,
            max_spend=args.max_spend, profile=not args.no_profile,
        )
        r = result.discover or {}
        print(f"  DISCOVER — {r.get('found', 0)} results across "
              f"{r.get('queries', 0)} queries")
        print(f"    {r.get('new_companies', 0)} new companies, "
              f"{r.get('known_companies', 0)} already known")
        print(f"    {r.get('junk', 0)} filtered out as directories/aggregators")
        print(f"    {r.get('observations', 0)} observations recorded")
        print(f"    actual cost: ${r.get('actual_cost', 0):.4f}")
        if r.get("stopped"):
            print(f"    stopped early: {r['stopped']}")

        if args.no_profile:
            print("\n  PROFILE — skipped (--no-profile)")
        else:
            print(f"\n  PROFILE — {result.profiled_companies} sites read, "
                  f"{result.profile_observations} observations  (free)")

        for err in result.errors[:5]:
            print(f"    ! {err}")
        print("\n  Next: harvey dashboard → Signals → cohort builder\n")

    asyncio.run(_discover())


def cmd_signals(args):
    """Review the signal vocabulary — Harvey proposes, you confirm.

    Nothing is collected until a signal is confirmed, so a fresh install
    prospects against nothing until someone makes these decisions.
    """
    from harvey.state import StateManager
    from harvey.signals import seed_signal_catalog

    async def _signals():
        state = StateManager()
        await state.init_db()
        await seed_signal_catalog(state)

        codes = [a.strip().upper() for a in (args.confirm or args.reject or "").split(",")
                 if a.strip()]
        if codes:
            status = "confirmed" if args.confirm else "rejected"
            if codes == ["ALL"]:
                codes = [c["code"] for c in await state.get_signal_codes()]
            elif codes == ["FREE"]:
                # Only signals whose cost note STARTS with free/included. A
                # substring match would sweep in "1 credit (free tiers
                # available)", which is not free.
                codes = [c["code"] for c in await state.get_signal_codes()
                         if (c["cost_note"] or "").lower().startswith(("free", "included"))]
            changed = sum(
                [1 for c in codes if await state.set_signal_status(c, status)]
            )
            print(f"\n  {changed} signal(s) {status}.\n")
            return

        rows = await state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await state.signal_counts()}
        by_status = {"confirmed": 0, "proposed": 0, "rejected": 0}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1

        print(f"\n  Signals — {by_status['confirmed']} confirmed, "
              f"{by_status['proposed']} awaiting you, {by_status['rejected']} off")
        print("  " + "=" * 66)
        mark = {"confirmed": "[on] ", "proposed": "[ ? ]", "rejected": "[off]"}
        current = None
        for r in rows:
            if r["category"] != current:
                current = r["category"]
                print(f"\n  {current.upper()}")
            seen = counts.get(r["code"], {}).get("companies", 0)
            seen_txt = f"  ({seen} companies)" if seen else ""
            print(f"    {mark.get(r['status'], '     ')} {r['code']:<22} "
                  f"{r['label']}{seen_txt}")
            print(f"          {r['cost_note']}")

        if by_status["proposed"]:
            print("\n  Confirm with: harvey signals --confirm CODE[,CODE...]")
            print("  Everything free: harvey signals --confirm free")
            print("  Details and descriptions: harvey dashboard → Signals\n")
        else:
            print("")

    asyncio.run(_signals())


def cmd_retry_writes(args):
    """Put prospects the Writer gave up on back in line for another try."""
    from harvey.agents.writer import MAX_WRITE_ATTEMPTS
    from harvey.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        count = await state.retry_write_failed()
        if count:
            print(f"\n  {count} prospect(s) are back in line. The Writer will try each "
                  f"up to {MAX_WRITE_ATTEMPTS} more times.\n")
        else:
            print("\n  No prospects are waiting on a retry.\n")

    asyncio.run(_run())


def cmd_warmup(args):
    """Check whether the sending domain is ready for cold email."""
    import os

    from dotenv import load_dotenv

    from harvey import deliverability as d
    from harvey.agents.handler import BOUNCE_COUNT_KEY
    from harvey.config import ConfigError, load_config
    from harvey.paths import PROJECT_ROOT
    from harvey.state import StateManager

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        config = load_config()
    except ConfigError as e:
        print(f"\n  {e}\n")
        raise SystemExit(1)
    email = config.channels.email
    smtp_user = os.getenv("SMTP_USERNAME", "").strip() if email.provider == "smtp" else ""
    sender = smtp_user if "@" in smtp_user else config.persona.email
    domain = (args.domain or sender.rpartition("@")[2]).strip().lower().rstrip(".")
    if not d.DOMAIN_RE.match(domain):
        print("\n  No valid sending domain found. Pass one: harvey warmup yourdomain.com\n")
        raise SystemExit(1)

    async def _history():
        state = StateManager()
        await state.init_db()
        return (await state.count_outbox_sent(),
                int(await state.get_setting(BOUNCE_COUNT_KEY) or 0))

    smtp_host = os.getenv("SMTP_HOST", "").strip() if email.provider == "smtp" else ""
    print(f"\n  Deliverability check - {domain}")
    print("  " + "=" * 56)
    sections = [
        ("Authentication", [d.check_mx(domain), d.check_spf(domain),
                            d.check_dkim(domain, email.warmup.dkim_selector),
                            d.check_dmarc(domain)]),
        ("Reputation", [d.check_domain_age(domain), *d.check_domain_blocklists(domain)]
         + (d.check_ip_blocklists(smtp_host) if smtp_host else [])),
        ("Warmup & sending", [d.check_warmup(email.warmup, email.max_daily_sends,
                                             provider=email.provider),
                              d.check_send_history(*asyncio.run(_history()))]),
    ]
    icons = {d.OK: "OK  ", d.WARN: "WARN", d.FAIL: "FAIL", d.INFO: "info"}
    failed = warned = unknown = 0
    for title, checks in sections:
        print(f"\n  {title}")
        for c in checks:
            failed += c.status == d.FAIL
            warned += c.status == d.WARN
            unknown += c.unknown and title == "Authentication"
            print(f"    [{icons[c.status]}] {c.name:<26} {c.detail}")

    print("\n  " + "=" * 56)
    if failed:
        print(f"  NOT READY - fix the {failed} FAIL item(s) before any cold email.")
    elif unknown:
        print(f"  NOT CHECKED - {unknown} authentication lookup(s) failed; run again.")
    elif warned:
        print(f"  READY WITH CAVEATS - {warned} warning(s); keep volume low.")
    else:
        print("  READY - send within today's cap and watch bounces.")
    print("\n  What DNS can't tell you (the real reputation signal):")
    print("    - Google Postmaster Tools: https://postmaster.google.com - add the domain;")
    print("      it shows Gmail's spam rate and domain reputation once you send volume.")
    print("    - Inbox placement: send one email to a seed test (mail-tester.com is free)")
    print("      and aim for 9/10+. Your warmup service's dashboard shows its inbox rate;")
    print("      wait for ~95%+ inbox for a week before relying on cold sends.\n")
    if failed or unknown:
        raise SystemExit(1)


def cmd_sending(args):
    """Pause/resume the sending kill switch."""
    from harvey.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.sending_action == "pause":
            await state.set_setting("sending_paused", "paused manually")
            print("\n  Sending paused. Nothing will leave the outbox.\n")
        elif args.sending_action == "resume":
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
            print("\n  Sending resumed (bounce counter reset).\n")
        else:
            paused = await state.get_setting("sending_paused")
            print(f"\n  Sending: {'PAUSED — ' + paused if paused else 'active'}\n")

    asyncio.run(_run())



def cmd_source(args):
    """Source lab products by request via Tavily & Firecrawl."""
    from harvey.config import load_config, load_env
    from harvey.state import StateManager
    from harvey.brain import Brain
    from harvey.integrations.sourcing import SourcingEngine

    async def _run():
        config = load_config()
        env = load_env()
        state = StateManager()
        await state.init_db()
        brain = Brain(state, config.brain)

        engine = SourcingEngine(brain, env, config)
        print(f"\n[SOURCING] Mencari supplier & spesifikasi untuk: '{args.query}' (By Request)...")
        margin = args.margin / 100.0 if args.margin else None
        res = await engine.source_item(
            item_query=args.query,
            client_name=args.client or "Bapak/Ibu",
            institution=args.institution or "Laboratorium",
            margin_pct=margin,
        )

        pricing = res.get("pricing", {})
        print("\n" + "=" * 60)
        print(f"PRODUK         : {res.get('official_name', args.query)}")
        print(f"MEREK / SKU    : {res.get('brand', '-')} | Cat: {res.get('model_or_cat_no', '-')}")
        print(f"SPESIFIKASI    : {res.get('key_specs', '-')}")
        print(f"ESTIMASI HPP   : {pricing.get('formatted_hpp', '-')}")
        print(f"HARGA JUAL     : {pricing.get('formatted_quote_price', '-')} (Margin: {pricing.get('margin_pct')}%)")
        print(f"ESTIMASI INDEN : {res.get('lead_time_days', '-')}")
        print("=" * 60)

        print("\nDRAF PESAN KONSULTATIF AHNAF (WhatsApp / Email):")
        print("-" * 60)
        print(res.get("consultative_response", "-"))
        print("-" * 60)

        refs = res.get("supplier_references", [])
        if refs:
            print("\nSUMBER DISTRIBUTOR / VENDOR DITEMUKAN:")
            for s in refs:
                print(f"  * {s.get('name')}: {s.get('url')}")
        print()

    asyncio.run(_run())

def cmd_quote(args):
    """Manage SPH/Quotes and automated post-quotation follow-up chaser."""
    from harvey.integrations.quote_chaser import QuoteChaser
    from harvey.config import load_config, load_env
    from harvey.integrations.whatsapp import WhatsAppEngine
    from harvey.state import StateManager

    async def _run():
        config = load_config()
        env = load_env()
        state = StateManager()
        await state.init_db()
        wa_engine = WhatsAppEngine(state, config, env)
        chaser = QuoteChaser(wa_engine)

        action = args.quote_action
        if action == "add":
            if not args.id or not args.client:
                print("\n  Error: --id dan --client wajib diisi. Contoh:")
                print("  harvey quote add --id QUO-033 --client \"RSUD Tebo\" --name \"Ibu NurHikmah\" --phone \"0812...\" --items \"Lemari Asam 1.5m\"\n")
                return
            await chaser.add_quote(
                quote_id=args.id,
                company=args.client,
                contact_name=args.name,
                phone=args.phone,
                items_summary=args.items,
                total_amount=args.amount,
                sent_date_str=args.date,
            )
            print(f"\n  [OK] Penawaran {args.id} untuk {args.client} berhasil dicatat di QuoteChaser.\n")
        elif action == "chaser":
            print("\n  Memeriksa penawaran yang sudah jatuh tempo follow-up (H+2, H+6, H+12)...")
            count = await chaser.process_due_followups(dry_run=args.dry_run)
            print(f"  [SELESAI] {count} pesan follow-up penawaran diproses.\n")
        else:
            quotes = await chaser.list_quotes()
            print(f"\n  Daftar Penawaran (Quotes) Terdata: {len(quotes)}")
            print("  " + "=" * 70)
            if not quotes:
                print("  Belum ada penawaran tercatat. Tambahkan dengan: harvey quote add ...\n")
                return
            for q in quotes:
                stage_lbl = f"Stage {q['stage']}/3" if q['stage'] else "Baru Dikirim"
                print(f"  [{q['id']}] {q['company']} ({q['contact_name'] or '-'})")
                print(f"    Item   : {q['items_summary'] or '-'}")
                print(f"    Tgl    : {q['sent_date']} | Status: {q['status']} ({stage_lbl})")
                print(f"    Kontak : {q['phone'] or '-'} | Last Follow-up: {q['last_followup_at'] or 'Belum'}")
                print()

    asyncio.run(_run())


def cmd_whatsapp(args):
    """Manage WhatsApp Web login, status, run outreach, or send daily report."""
    from harvey.config import load_config, load_env
    from harvey.integrations.whatsapp import WhatsAppEngine
    from harvey.state import StateManager

    async def _run():
        config = load_config()
        env = load_env()
        state = StateManager()
        await state.init_db()
        engine = WhatsAppEngine(state, config, env)
        action = args.wa_action
        if action == "login":
            await engine.interactive_login()
        elif action == "run":
            config.channels.whatsapp.enabled = True
            sent = await engine.run()
            print(f"\n  WhatsApp outreach selesai: {sent} pesan terkirim.\n")
        elif action in ("report", "weekly-report"):
            target = getattr(args, "phone", "") or config.channels.whatsapp.report_phone or "087764021131"
            if action == "weekly-report" or getattr(args, "weekly", False):
                print(f"\n  Mengirimkan laporan MINGGUAN WhatsApp ke {target}...")
                ok = await engine.send_weekly_report(target_phone=target)
                rep_type = "mingguan"
            else:
                print(f"\n  Mengirimkan laporan harian WhatsApp ke {target}...")
                ok = await engine.send_daily_report(target_phone=target, force=True)
                rep_type = "harian"
            if ok:
                print(f"  [OK] Laporan {rep_type} berhasil dikirim ke {target}.\n")
            else:
                print(f"  [GAGAL] Gagal mengirimkan laporan {rep_type} ke {target}.\n")
        else:
            act = engine._load_activity()
            logged = engine.is_logged_in_profile_present() or bool(env.fonnte_api_key)
            print(f"\n  WhatsApp Status:")
            print(f"    Enabled       : {config.channels.whatsapp.enabled}")
            print(f"    Provider      : {'fonnte' if env.fonnte_api_key else config.channels.whatsapp.provider}")
            print(f"    Session Ready : {'YES' if logged else 'NO (run: harvey whatsapp login)'}")
            print(f"    Sent Today    : {act['sent']}/{config.channels.whatsapp.max_daily_messages}")
            print(f"    Work Hours    : {config.channels.whatsapp.work_hours_start} - {config.channels.whatsapp.work_hours_end} WIB")
            print(f"    Auto Report   : {'AKTIF' if config.channels.whatsapp.auto_report else 'NONAKTIF'} -> {config.channels.whatsapp.report_phone} (@ {config.channels.whatsapp.report_time} WIB)")
            print(f"    Last Report   : {act.get('last_report_date', 'Belum terkirim hari ini')}\n")

    asyncio.run(_run())


def cmd_inaproc(args):
    """Manage INAPROC E-Katalog v6 Agent & Mini Kompetisi Co-Pilot."""
    from harvey.config import load_config, load_env
    from harvey.integrations.inaproc_watcher import InaprocWatcher, format_inaproc_alert_message
    from harvey.agents.inaproc_agent import InaprocAgent
    from harvey.integrations.whatsapp import WhatsAppEngine
    from harvey.state import StateManager
    from harvey.brain import Brain

    async def _run():
        config = load_config()
        env = load_env()
        state = StateManager()
        await state.init_db()
        brain = Brain(state, config.brain)
        agent = InaprocAgent(state, config, env, brain)
        watcher = InaprocWatcher()

        action = args.inaproc_action
        if action == "login":
            await agent.interactive_login()
        elif action == "status":
            st = await agent.check_session_status()
            print("\n  " + "=" * 58)
            print("  STATUS SESI INAPROC E-KATALOG V6")
            print("  " + "=" * 58)
            status_symbol = "🟢 AKTIF" if st.get("logged_in") else "🔴 BELUM LOGIN"
            print(f"  Status        : {status_symbol}")
            print(f"  Portal Target : E-Katalog {st.get('portal', 'v6').upper()} (katalog.inaproc.id)")
            print(f"  Login Terakhir: {st.get('last_login') or 'Belum pernah'}")
            print(f"  Keterangan    : {st.get('status_text', '-')}")
            print("  " + "=" * 58 + "\n")
        elif action == "copilot":
            print("\n  ⚡ [CO-PILOT INAPROC] Menjalankan siklus otonom Mini Kompetisi...")
            print("  Margin Minimal : 20% di atas HPP")
            print("  Mode Eksekusi  : Auto-Submit")
            res = await agent.run_copilot_cycle(auto_submit=True)
            print(f"\n  Siklus selesai. {len(res)} penawaran diproses:")
            for b in res:
                print(f"  - [{b.get('status', 'draft')}] {b.get('package_name')} -> Rp {int(b.get('target_price', 0)):,} (Margin {b.get('margin_pct')}%)")
            print()
        elif action == "bids":
            bids = await state.get_inaproc_bids()
            print(f"\n  📋 [DAFTAR BID MINI KOMPETISI] Total {len(bids)} paket terekam:\n")
            if not bids:
                print("  Belum ada data bid. Jalankan: harvey inaproc copilot\n")
                return
            for b in bids:
                hpp_str = f"Rp {int(b.get('cost_hpp', 0)):,}".replace(",", ".")
                price_str = f"Rp {int(b.get('target_price', 0)):,}".replace(",", ".")
                print(f"  [{b.get('status', 'new').upper():<10}] {b.get('package_name', '')[:45]}")
                print(f"               Instansi : {b.get('institution', '-')}")
                print(f"               Produk   : {b.get('matched_product_name', '-')}")
                print(f"               HPP      : {hpp_str} | Tawaran: {price_str} (Margin {b.get('margin_pct')}%)")
                print(f"               Catatan  : {b.get('notes', '-')}\n")
        elif action == "search":
            kw = getattr(args, "keyword", "").strip()
            keywords = [kw] if kw else None
            pages = getattr(args, "pages", 1)
            notify = getattr(args, "notify", False)
            auto_sub = getattr(args, "auto_submit", False)

            kw_label = f"kata kunci '{kw}'" if kw else "kategori utama laboratorium"
            print(f"\n  🔍 [AUTONOMOUS SEARCH INAPROC] Mencari tender aktif untuk {kw_label}...")
            print("  Sesi Browser : Menggunakan profil autentikasi penyedia.inaproc.id")
            if notify:
                print("  Notifikasi   : WhatsApp Alert aktif -> 087764021131")

            found_pkgs = await agent.scan_and_alert_competitions(
                keywords=keywords,
                notify_wa=notify,
                auto_submit=auto_sub,
            )

            print(f"\n  Hasil Pencarian Otonom: {len(found_pkgs)} paket tender / mini kompetisi ditemukan:\n")
            if not found_pkgs:
                print("  Tidak ada paket yang cocok ditemukan saat ini.\n")
                return

            for idx, p in enumerate(found_pkgs, start=1):
                hps_str = f"Rp {int(p.get('hps_budget', 0)):,}".replace(",", ".") if p.get('hps_budget', 0) > 0 else "N/A"
                print("=" * 68)
                print(f"  [{idx}] {p.get('package_name')}")
                print(f"      ID Paket    : {p.get('id')} ({p.get('procurement_type', 'Mini Kompetisi')})")
                print(f"      Instansi    : {p.get('institution')} | {p.get('location')}")
                print(f"      Total HPS   : {hps_str}")
                print(f"      Batas Waktu : {p.get('deadline')} ({p.get('time_left', 'Segera')})")
                if p.get("spec_bank_match"):
                    print(f"      Produk Cocok: {p.get('spec_bank_match')}")
                if p.get("target_price", 0) > 0:
                    t_str = f"Rp {int(p['target_price']):,}".replace(",", ".")
                    print(f"      Estimasi    : Harga Penawaran {t_str} (Margin {p.get('margin_pct', 20)}%)")
                if p.get("products"):
                    print(f"      Rincian Item: {len(p['products'])} macam produk terdaftar")
                print(f"      Tautan Link : {p.get('source_url')}")
            print("=" * 68 + "\n")
        elif action == "scan":
            print("\n  🔍 [RADAR INAPROC] Memindai peluang Mini Kompetisi & LPSE live...")
            new_packages = await watcher.scan_live_procurement(env, brain)
            print(f"\n  Discovered {len(new_packages)} paket peluang baru:")
            for pkg in new_packages:
                budget_str = f"Rp {int(pkg.hps_budget):,}".replace(",", ".") if pkg.hps_budget > 0 else "Belum tertera"
                print("-" * 65)
                print(f"  🏢 Instansi : {pkg.institution}")
                print(f"  📦 Paket    : {pkg.package_name}")
                print(f"  🏷️  Metode   : {pkg.procurement_type} | Skor: {pkg.match_score}%")
                print(f"  💰 Pagu/HPS : {budget_str}")
                print(f"  📍 Lokasi   : {pkg.location} | Batas: {pkg.deadline}")
                print(f"  🔗 URL      : {pkg.source_url}")

                if getattr(args, "notify", False):
                    wa_engine = WhatsAppEngine(state, config, env)
                    alert_msg = format_inaproc_alert_message(pkg)
                    sent = await wa_engine.send_inaproc_alert(alert_msg)
                    if sent:
                        print("  📱 Alert WhatsApp terkirim ke Ahnaf Ibrahim!")
            print("-" * 65)
            print()
        else:
            # list
            pkgs = watcher.load_known_packages()
            print(f"\n  📦 [RADAR INAPROC] Total {len(pkgs)} paket tersimpan di database:\n")
            if not pkgs:
                print("  Belum ada paket tersimpan. Jalankan: harvey inaproc scan\n")
                return
            for pid, data in sorted(pkgs.items(), key=lambda x: x[1].get("match_score", 0), reverse=True):
                budget_str = f"Rp {int(data.get('hps_budget', 0)):,}".replace(",", ".") if data.get('hps_budget', 0) > 0 else "N/A"
                print(f"  [{data.get('match_score', 0)}%] {data.get('procurement_type', 'Tender'):<24} | {budget_str:<15} | {data.get('package_name', '')[:50]}")
                print(f"        Instansi: {data.get('institution', '-')} | URL: {data.get('source_url', '-')}\n")

    asyncio.run(_run())


def main():
    from harvey.textio import utf8_stdio
    utf8_stdio()
    from harvey.paths import PROJECT_ROOT
    project_root = str(PROJECT_ROOT)

    parser = argparse.ArgumentParser(
        prog="harvey",
        description="Harvey — Autonomous AI Sales Agent. Always Be Closing.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # harvey install
    sub = subparsers.add_parser("install", help="Install dependencies")
    sub.set_defaults(func=cmd_install)

    # harvey setup
    sub = subparsers.add_parser("setup", help="Run the interactive setup wizard")
    sub.set_defaults(func=cmd_setup)

    # harvey run
    sub = subparsers.add_parser("run", help="Start Harvey's heartbeat loop")
    sub.set_defaults(func=cmd_run)

    # harvey train <url>
    sub = subparsers.add_parser("train", help="Train Harvey on a website")
    sub.add_argument("url", help="Website URL to crawl and learn from")
    sub.add_argument(
        "max_pages",
        nargs="?",
        type=int,
        default=100,
        help="Max pages to crawl (default: 100)",
    )
    from harvey.config import BRAIN_PROVIDERS
    sub.add_argument(
        "--provider", choices=list(BRAIN_PROVIDERS), default=None,
        help="Subscription to think with (default: the one in your config, else claude)",
    )
    sub.add_argument("--model", default=None, help="Model for this provider (default: provider default)")
    sub.add_argument(
        "--reasoning-effort", default=None,
        help="Codex reasoning effort (minimal/low/medium/high/xhigh)",
    )
    sub.set_defaults(func=cmd_train)

    # harvey dashboard
    sub = subparsers.add_parser("dashboard", help="Open the web dashboard")
    sub.add_argument("--host", default="127.0.0.1",
                     help="Address to listen on (default: 127.0.0.1; use 0.0.0.0 with --allow-remote)")
    sub.add_argument("--port", type=int, default=5555, help="Port (default: 5555)")
    sub.add_argument("--url", action="store_true",
                     help="Print the unlock link (with its token) and exit")
    sub.add_argument("--allow-remote", action="store_true",
                     help="Allow access from other devices on the same Wi-Fi / local network")
    sub.add_argument("--tunnel", action="store_true",
                     help="Create an encrypted Cloudflare HTTPS tunnel for mobile remote access anywhere")
    sub.add_argument("--send-wa", action="store_true",
                     help="Send the mobile dashboard link directly to your WhatsApp")
    sub.add_argument("--phone", default="087764021131",
                     help="WhatsApp destination number for dashboard link")
    sub.add_argument("--no-open", action="store_true",
                     help="Do not automatically open the browser on startup")
    sub.set_defaults(func=cmd_dashboard)

    # harvey status
    sub = subparsers.add_parser("status", help="Show pipeline status")
    sub.add_argument(
        "--check", action="store_true",
        help="Also check the configured brain now (Codex: CLI version, isolation "
             "and login; Claude: CLI isolation flags; no model call)",
    )
    sub.set_defaults(func=cmd_status)

    # harvey export
    sub = subparsers.add_parser(
        "export", help="Export prospects as a sequencer-ready CSV"
    )
    sub.add_argument("--out", default="prospects.csv", help="Output file (default: prospects.csv)")
    sub.add_argument(
        "--email-status", default="",
        help="Comma-separated statuses to include (default: verified,risky)",
    )
    sub.add_argument("--min-score", type=int, default=0, help="Minimum ICP score")
    sub.add_argument("--status", default="", help="Comma-separated pipeline statuses (e.g. new,queued); opted-out contacts stay excluded")
    sub.add_argument("--all", action="store_true", help="Export the raw list without filters, including opted-out contacts (not for outreach)")
    sub.set_defaults(func=cmd_export)

    # harvey gmail auth|test
    sub = subparsers.add_parser("gmail", help="Gmail provider setup")
    sub.add_argument("gmail_action", choices=["auth", "test"],
                     help="auth: one-time OAuth; test: verify connection")
    sub.set_defaults(func=cmd_gmail)

    # harvey microsoft auth|test
    sub = subparsers.add_parser("microsoft", help="Microsoft 365 provider setup")
    sub.add_argument("microsoft_action", choices=["auth", "test"],
                     help="auth: one-time sign-in; test: verify connection")
    sub.set_defaults(func=cmd_microsoft)

    # harvey outbox
    sub = subparsers.add_parser("outbox", help="Review/approve queued emails")
    sub.add_argument("--approve", metavar="ID", default="", help="Approve one item")
    sub.add_argument("--approve-all", action="store_true", help="Approve all pending")
    sub.add_argument("--reject", metavar="ID", default="", help="Reject one item")
    sub.set_defaults(func=cmd_outbox)

    # harvey sending pause|resume|status
    sub = subparsers.add_parser("discover", help="Find businesses matching your ICP")
    sub.add_argument("--provider", default="osm",
                     help="Discovery provider (default: osm — free, no account)")
    sub.add_argument("--providers", action="store_true",
                     help="List every provider with cost and setup, then exit")
    sub.add_argument("--estimate", action="store_true",
                     help="Print projected spend and exit without calling anything")
    sub.add_argument("--city", default="",
                     help='Semicolon-separated cities, e.g. "Denver, CO;Dallas, TX" '
                          '(default: icp.geography)')
    sub.add_argument("--depth", type=int, default=30,
                     help="SERP depth (default: 30 — depth 100 costs 10x since Sept 2025)")
    sub.add_argument("--limit", type=int, default=100,
                     help="Max records per query (default: 100)")
    sub.add_argument("--max-spend", type=float, default=1.0,
                     help="Hard cap in dollars (default: 1.00)")
    sub.add_argument("--preset", default="", choices=["", "yc", "hiring", "accelerator", "funding"],
                     help="A built-in search instead of your ICP's industries "
                          "(each needs serper or dataforseo_serp). "
                          "yc: startups whose own site says they're YC-backed; "
                          "hiring: companies whose own careers pages list one of "
                          "your hiring_signals roles (else ICP titles); "
                          "accelerator: ones backed by Techstars, Antler, EF, SPC, 500 Global, "
                          "Neo, AI Grant, a16z speedrun or Plug and Play; "
                          "funding: companies announcing a raise on their own site in the last 18 months")
    sub.add_argument("--no-profile", action="store_true",
                     help="Don't profile what was found (profiling is free)")
    sub.set_defaults(func=cmd_discover)

    sub = subparsers.add_parser(
        "profile", help="Read discovered companies' websites (free, no Claude)")
    sub.add_argument("--limit", type=int, default=200,
                     help="Max sites to read (default: 200)")
    sub.add_argument("--stale-days", type=int, default=90,
                     help="Re-read a site after this many days (default: 90)")
    sub.set_defaults(func=cmd_profile)

    sub = subparsers.add_parser(
        "signals", help="Review/confirm which signals Harvey prospects against")
    sub.add_argument("--confirm", metavar="CODES", default="",
                     help="Comma-separated codes to confirm ('all' or 'free' accepted)")
    sub.add_argument("--reject", metavar="CODES", default="",
                     help="Comma-separated codes to turn off")
    sub.set_defaults(func=cmd_signals)

    sub = subparsers.add_parser(
        "retry-writes", help="Retry prospects the Writer couldn't write an email for")
    sub.set_defaults(func=cmd_retry_writes)

    sub = subparsers.add_parser("sending", help="Kill switch: pause/resume sending")
    sub.add_argument("sending_action", nargs="?", default="status",
                     choices=["pause", "resume", "status"])
    sub.set_defaults(func=cmd_sending)

    sub = subparsers.add_parser(
        "warmup", help="Check if your sending domain is set up and warmed up for cold email")
    sub.add_argument("domain", nargs="?", default="",
                     help="Domain to check (default: your sending mailbox's domain)")
    sub.set_defaults(func=cmd_warmup)

    # harvey usage
    sub = subparsers.add_parser("usage", help="Show brain usage and quota (Claude or Codex)")
    sub.add_argument("--days", type=int, default=30, help="Breakdown window (default: 30)")
    sub.set_defaults(func=cmd_usage)

    # harvey quote list|add|chaser
    sub = subparsers.add_parser("quote", help="Manage quotes & automated follow-up chaser")
    sub.add_argument("quote_action", nargs="?", default="list", choices=["list", "add", "chaser"])
    sub.add_argument("--id", default="", help="Nomor SPH/Quote (e.g. QUO-033)")
    sub.add_argument("--client", default="", help="Nama Perusahaan/Instansi (e.g. RSUD Tebo)")
    sub.add_argument("--name", default="", help="Nama PIC Klien (e.g. Ibu NurHikmah)")
    sub.add_argument("--phone", default="", help="Nomor WA Klien")
    sub.add_argument("--items", default="", help="Ringkasan item yang ditawarkan")
    sub.add_argument("--amount", type=float, default=0.0, help="Total nilai penawaran (Rp)")
    sub.add_argument("--date", default="", help="Tanggal kirim (YYYY-MM-DD)")
    sub.add_argument("--dry-run", action="store_true", help="Cek jadwal follow-up tanpa kirim beneran")
    sub.set_defaults(func=cmd_quote)

    # harvey whatsapp login|status|run|report|weekly-report
    sub = subparsers.add_parser("whatsapp", help="WhatsApp outreach, QR login & reporting")
    sub.add_argument("wa_action", nargs="?", default="status", choices=["login", "status", "run", "report", "weekly-report"])
    sub.add_argument("--phone", default="", help="Target phone number for report")
    sub.add_argument("--weekly", action="store_true", help="Send weekly report instead of daily")
    sub.set_defaults(func=cmd_whatsapp)

    # harvey source <query>
    sub = subparsers.add_parser("source", help="Source lab products by request via Tavily & Firecrawl")
    sub.add_argument("query", help="Nama alat, reagen, atau nomor katalog yang dicari")
    sub.add_argument("--client", default="", help="Nama klien / PIC")
    sub.add_argument("--institution", default="", help="Nama kampus / rumah sakit / instansi")
    sub.add_argument("--margin", type=float, default=20.0, help="Persentase margin penawaran (default: 20%%)")
    sub.set_defaults(func=cmd_source)

    # harvey inaproc login|status|copilot|scan|search|bids|list
    sub = subparsers.add_parser("inaproc", help="Dedicated Agent for INAPROC E-Katalog v6 & Mini Kompetisi")
    sub.add_argument("inaproc_action", nargs="?", default="status",
                     choices=["login", "status", "copilot", "scan", "search", "bids", "list"])
    sub.add_argument("--id", default="", help="ID Paket untuk submit manual")
    sub.add_argument("--keyword", default="", help="Kata kunci pencarian spesifik (misal: 'Laboratorium', 'Reagen')")
    sub.add_argument("--pages", type=int, default=1, help="Jumlah halaman pencarian (default: 1)")
    sub.add_argument("--notify", action="store_true", help="Kirimkan WhatsApp alert ke operator jika ada paket baru")
    sub.add_argument("--auto-submit", action="store_true", help="Otomatis ajukan penawaran jika margin >= 20%%")
    sub.set_defaults(func=cmd_inaproc)

    args = parser.parse_args()
    args._project_root = project_root

    if args.command is None:
        parser.print_help()
        print("\n  Quick start:")
        print("    harvey install   — Install dependencies")
        print("    harvey setup     — Configure Harvey (first time)")
        print("    harvey run       — Start closing deals")
        print("    harvey dashboard — Open the web dashboard")
        print()
        sys.exit(0)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n  Interrupted. Goodbye.")
        sys.exit(130)
    except Exception as e:
        # ConfigError and friends carry actionable messages — show them
        # cleanly instead of a raw traceback.
        from harvey.config import ConfigError

        if isinstance(e, ConfigError):
            print(f"\n  Configuration problem:\n  {e}\n")
        else:
            print(f"\n  Error running 'harvey {args.command}': {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
