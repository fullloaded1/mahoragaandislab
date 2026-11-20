"""Harvey's main heartbeat loop. Always Be Closing."""

import asyncio
import logging
import signal
import sys
from datetime import datetime, time, timedelta

import pytz

from harvey.brain import Brain
from harvey.sending_mode import sync_sending_mode
from harvey.config import (
    ConfigError, load_config, load_env, HarveyConfig, legacy_sending_held, sending_notice,
    compliance_notice, send_window_of,
)
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.pipeline import run_profile_stage
from harvey.state import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("harvey")

# Backoff for consecutive failed cycles: 60s, 120s, 240s, ... capped at 15 min
ERROR_BACKOFF_BASE = 60
ERROR_BACKOFF_CAP = 900

# Hard ceiling on one cycle's parallel agents. return_exceptions=True on
# gather() only isolates a task that raises — a task that hangs (a socket or
# lock that never resolves) blocks gather() forever, taking the whole
# heartbeat loop down silently with it. This is the only thing standing
# between one stuck agent and Harvey freezing for hours with no agent
# knowing it's dead — found in production after exactly that happened.
CYCLE_TIMEOUT_SECONDS = 600

# Cap on a single quiet-hours sleep chunk. See the quiet-hours block in
# heartbeat() for why this exists — a system sleep/wake cycle during one
# multi-hour asyncio.wait_for left Harvey silently stalled well past when
# quiet hours actually ended.
QUIET_HOURS_POLL_SECONDS = 300

# Phase-1 tasks that read replies; sending waits for all of them to succeed.
REPLY_READERS = ("handle_replies", "read_replies")


def in_quiet_hours(config: HarveyConfig) -> bool:
    """Check if we're currently in quiet hours."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz).time()
    start = time.fromisoformat(qh.start)
    end = time.fromisoformat(qh.end)

    if start <= end:
        return start <= now <= end
    else:
        # Quiet hours cross midnight (e.g., 22:00 - 07:00)
        return now >= start or now <= end


def sends_through_quiet_hours(config: HarveyConfig) -> bool:
    """True when quiet hours pause Harvey's own work but not cold email
    that is due in its recipient's window.

    With a send window in each recipient's time zone, the operator's night
    can be a recipient's morning. Prospecting and writing still sleep, and
    replies wait for the operator's hours as before. A heartbeat wakes only
    when a cold email can go now (Sender.cold_due_in_window): it reads and
    handles replies first (so stop-on-reply has run; reading a reply is what
    consumes it, so it can't be left for the morning), then runs the sender,
    which stages any draft campaign into the outbox (no model call, nothing
    sent) and sends only cold email. Otherwise it does nothing. Native mail
    providers only.
    """
    email = config.channels.email
    window = send_window_of(config)
    return bool(window is not None and window.recipient_timezone
                and email.enabled and email.provider in NATIVE_PROVIDERS)


def seconds_until_quiet_hours_end(config: HarveyConfig) -> int:
    """Calculate seconds until quiet hours end."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz)
    end = time.fromisoformat(qh.end)
    end_today = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)

    if end_today <= now:
        # End time is tomorrow
        end_today = end_today + timedelta(days=1)

    delta = end_today - now
    return max(int(delta.total_seconds()), 60)


# When nothing is writable, prospecting pauses for the rest of the day once
# this many unusable prospects (a guessed, invalid or missing email) were
# found in the last 24 hours. Without a working verifier every address is a
# guess, and prospecting every cycle would spend search and verifier credits
# on people Harvey can never email. Nothing re-checks a guess, so the window
# is a day, never all time: an all-time count would stop Scout for good.
PROSPECT_BACKLOG = 20


def _new_counts(summary: dict) -> tuple[int, int]:
    """(all 'new' prospects, the ones the Writer can draft for)."""
    prospects = summary.get("prospects") or {}
    new = (prospects.get("new", 0) if isinstance(prospects, dict) else 0) or 0
    # A summary built without the writable count keeps the old rule.
    writable = summary.get("writable_prospects", new) or 0
    return new, writable


def unusable_backlog_notice(summary: dict) -> str:
    """Why Harvey paused prospecting for the day, or ""."""
    _, writable = _new_counts(summary)
    recent = summary.get("recent_unusable_prospects", 0) or 0
    if writable or recent < PROSPECT_BACKLOG:
        return ""
    return (
        f"Harvey found {recent} prospects in the last day, but none has a verified "
        "or catch-all email, so it paused prospecting for about a day to save search "
        "and verifier credits. Add an email verifier key (REOON_API_KEY, "
        "ZEROBOUNCE_API_KEY or HUNTER_API_KEY in .env) or check it still has credits. "
        "`harvey export --all` exports the list as it is."
    )


async def decide_next_action(
    brain: Brain,
    state: StateManager,
    config: HarveyConfig,
    summary: dict | None = None,
) -> str:
    """Decide what Harvey should do next based on current state.

    Uses deterministic priority rules (handle_replies > send_campaign >
    write_campaign > prospect > idle) instead of burning a Claude call on a
    decision that is fully derivable from pipeline counts. This saves budget
    every cycle and removes a fragile LLM string-parsing step.
    """
    if summary is None:
        summary = await state.get_state_summary()

    # Only prospects the Writer can draft for trigger writing: one with a
    # guessed email stays 'new' for good, and counting it starved Scout.
    new_prospects, writable = _new_counts(summary)
    draft_campaigns = summary.get("draft_campaigns", 0) or 0
    open_conversations = summary.get("open_conversations", 0) or 0

    # Held Instantly drafts can't deploy; choosing send_campaign for them
    # every cycle would starve the writer and scout forever.
    deployable = 0 if legacy_sending_held(config) else draft_campaigns

    if open_conversations > 0:
        action, reason = "handle_replies", f"{open_conversations} open conversation(s) waiting"
    elif deployable > 0:
        action, reason = "send_campaign", f"{draft_campaigns} draft campaign(s) ready to deploy"
    elif writable > 0:
        action, reason = "write_campaign", f"{writable} new prospect(s) with no drafts ready"
    elif unusable_backlog_notice(summary):
        wa_on = bool(getattr(getattr(config, "channels", None), "whatsapp", None) and config.channels.whatsapp.enabled)
        if wa_on:
            action, reason = "prospect", "email backlog paused, continuing prospecting for active WhatsApp channel"
        else:
            action, reason = "idle", "a day's worth of prospects with no usable email"
            logger.warning(unusable_backlog_notice(summary))
    else:
        action, reason = "prospect", f"{new_prospects} new prospect(s), none ready to write for"

    logger.info(f"Decision: {action} ({reason})")
    return action


async def _run_agents(tasks: list) -> dict:
    """Run (name, coroutine) pairs in parallel; return {name: outcome}.

    Errors are caught per task so one bad agent never takes down the cycle,
    and the whole batch is bounded so one HUNG agent (no exception, just
    never returns) can't either. A task's outcome is what it returned, or
    the exception it raised; a task cut off by the bound has no outcome.
    """
    if not tasks:
        return {}
    if len(tasks) > 1:
        logger.info(f"Running {len(tasks)} agents in parallel: {[t[0] for t in tasks]}")
    # pyrefly: ignore[parse-error]
    task_objs= [(name, asyncio.ensure_future(coro)) for name, coro in tasks]
    done, pending = await asyncio.wait(
        [t for _, t in task_objs], timeout=CYCLE_TIMEOUT_SECONDS
    )
    if pending:
        still_running = [name for name, t in task_objs if t in pending]
        logger.error(
            f"Cycle timed out after {CYCLE_TIMEOUT_SECONDS}s with "
            f"{still_running} still running — cancelled, "
            "moving on to the next heartbeat instead of hanging forever."
        )
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    outcomes = {}
    for name, t in task_objs:
        if t in pending:
            continue
        try:
            result = t.result()
        except asyncio.CancelledError:
            raise
        except Exception as result_exc:
            logger.error(f"Agent {name} failed: {result_exc}", exc_info=result_exc)
            outcomes[name] = result_exc
        else:
            outcomes[name] = result
    return outcomes


async def _interruptible_sleep(seconds: float, stop_event: asyncio.Event) -> bool:
    """Sleep up to `seconds`, waking immediately on shutdown.

    Returns True if a shutdown was requested during the sleep.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


def send_hold_reason(unread: list, paused: str, read_problem: str) -> str | None:
    """What to record about this cycle's send phase: why sending is held by
    unread replies, "" when it ran (clear the note), or None to leave the
    note alone (a paused brain has its own Today card)."""
    if paused:
        return None
    if unread:
        return read_problem or "replies couldn't all be read and handled."
    return ""


async def record_send_phase(state, held_reason: str):
    """Note why sending was held this cycle (Today and `harvey status` show
    it), or clear the note when sending ran. Never stops the loop."""
    try:
        if held_reason:
            await state.note_sends_held(held_reason)
        else:
            await state.clear_sends_held()
    except Exception as e:
        logger.warning(f"Couldn't record the send hold: {e}")


async def heartbeat(stop_event: asyncio.Event | None = None):
    """Harvey's main loop. Wakes up, decides, acts, sleeps. Repeat."""
    if stop_event is None:
        stop_event = asyncio.Event()

    logger.info("=" * 60)
    logger.info("Harvey is online. Always Be Closing.")
    logger.info("=" * 60)

    try:
        config = load_config()
    except (ConfigError, Exception) as e:
        if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        logger.error(f"Cannot start — configuration error:\n{e}")
        return
    env = load_env()
    state = StateManager()
    brain = Brain(state, config.brain)
    logger.info(
        f"Thinking with {brain.provider_label}"
        + (f" ({brain.model})" if brain.model else "")
        + ". Change brain.provider in harvey.yaml to switch."
    )
    for notice in (sending_notice(config), compliance_notice(config)):
        if notice:
            logger.warning("=" * 60)
            logger.warning(notice)
            logger.warning("=" * 60)

    await state.init_db()
    logger.info("Database initialized.")

    # Import agents here to avoid circular imports
    from harvey.agents.scout import Scout
    from harvey.agents.writer import Writer
    from harvey.agents.sender import Sender
    from harvey.agents.handler import Handler
    from harvey.agents.analyst import Analyst
    from harvey.integrations.whatsapp import WhatsAppEngine
    from harvey.integrations.inaproc_watcher import InaprocWatcher, format_inaproc_alert_message
    from harvey.agents.inaproc_agent import InaprocAgent

    scout = Scout(brain, state, config, env)
    writer = Writer(brain, state, config, env)
    sender = Sender(brain, state, config, env)
    handler = Handler(brain, state, config, env)
    analyst = Analyst(state)
    whatsapp = WhatsAppEngine(state, config, env)
    inaproc_watcher = InaprocWatcher()
    inaproc_agent = InaprocAgent(state, config, env, brain, min_margin=0.20)

    interval = config.usage.heartbeat_interval_minutes * 60
    max_calls = max(int(200 * (config.usage.max_daily_claude_percent / 100)), 1)
    consecutive_errors = 0

    async def _daily_report_ticker():
        """Ensure daily WhatsApp report is dispatched at exact report_time (e.g. 17:00 WIB) once per day."""
        import pytz
        from datetime import datetime
        last_sent_date = None
        try:
            act = whatsapp._load_activity()
            last_sent_date = act.get("last_report_date")
        except Exception:
            pass

        while not stop_event.is_set():
            try:
                wa_cfg = getattr(config.channels, "whatsapp", None)
                if wa_cfg and wa_cfg.enabled and wa_cfg.auto_report and wa_cfg.report_phone:
                    now_jkt = datetime.now(pytz.timezone("Asia/Jakarta"))
                    curr_hhmm = now_jkt.strftime("%H:%M")
                    target_time = wa_cfg.report_time or "17:00"
                    today_str = now_jkt.date().isoformat()
                    act = whatsapp._load_activity()
                    file_last_report = act.get("last_report_date")

                    if curr_hhmm >= target_time and last_sent_date != today_str and file_last_report != today_str:
                        # Lock out immediately in memory and file to prevent duplicate triggers
                        last_sent_date = today_str
                        act["last_report_date"] = today_str
                        whatsapp._save_activity(act)
                        logger.info(f"WhatsApp: Jam {curr_hhmm} WIB tercapai (jadwal: {target_time}). Mengirimkan auto-report harian ke {wa_cfg.report_phone}...")
                        await whatsapp.send_daily_report(force=True)
            except Exception as ex:
                logger.warning(f"WhatsApp: Error in daily report ticker: {ex}")
            if await _interruptible_sleep(30, stop_event):
                break

    report_ticker_task = asyncio.create_task(_daily_report_ticker())

    async def _procurement_scan_ticker():
        """Monitor INAPROC / LPSE and Mini Kompetisi daily at 08:30 WIB (morning procurement release)."""
        import pytz
        from datetime import datetime
        last_scan_date = None
        try:
            act = inaproc_watcher.load_activity()
            last_scan_date = act.get("last_scan_date")
        except Exception:
            pass

        last_patrol_time = None
        while not stop_event.is_set():
            try:
                now_jkt = datetime.now(pytz.timezone("Asia/Jakarta"))
                curr_hhmm = now_jkt.strftime("%H:%M")
                
                # Active patrol window: 08:00 to 17:30 WIB, every 60 minutes
                is_working_hours = "08:00" <= curr_hhmm <= "17:30"
                should_patrol = False

                if is_working_hours:
                    if last_patrol_time is None:
                        should_patrol = True
                    else:
                        elapsed = (now_jkt - last_patrol_time).total_seconds()
                        if elapsed >= 3600:  # 60 minutes
                            should_patrol = True

                if should_patrol:
                    last_patrol_time = now_jkt
                    logger.info("Radar INAPROC: Memulai patroli otonom portal Mini Kompetisi E-Katalog v6...")
                    found_competitions = await inaproc_agent.scan_and_alert_competitions(
                        notify_wa=True,
                        auto_submit=False,
                    )
                    logger.info(f"Radar INAPROC: Patroli selesai. {len(found_competitions)} paket tender terdeteksi/diperbarui.")
            except Exception as ex:
                logger.warning(f"Radar INAPROC: Error in procurement patrol ticker: {ex}")

            if await _interruptible_sleep(60, stop_event):
                break

    procurement_ticker_task = asyncio.create_task(_procurement_scan_ticker())

    async def _wa_live_sync_ticker():
        """Fast-path realtime WhatsApp chat sync and instant auto-reply ticker (every 45s)."""
        while not stop_event.is_set():
            try:
                wa_cfg = getattr(config.channels, "whatsapp", None)
                if wa_cfg and wa_cfg.enabled and whatsapp.in_reply_hours():
                    await whatsapp.sync_live_chats_from_openwa(limit=50)
            except Exception as ex:
                logger.debug(f"WhatsApp: Error in fast sync ticker: {ex}")
            if await _interruptible_sleep(45, stop_event):
                break

    wa_sync_task = asyncio.create_task(_wa_live_sync_ticker())

    async def _bigdata_maintenance_ticker():
        """Background Big Data sync: rebuilds LanceDB every 6h and harvests 4 segments every 24h."""
        from datetime import datetime, timezone
        # Wait 90s after startup so initial heartbeat/WA sync settles first
        if await _interruptible_sleep(90, stop_event):
            return
        while not stop_event.is_set():
            try:
                now_ts = datetime.now(timezone.utc).timestamp()

                last_idx_raw = await state.get_setting("bigdata_last_index_ts")
                last_idx = float(last_idx_raw) if last_idx_raw else 0.0
                if (now_ts - last_idx) >= 6 * 3600:
                    from harvey.integrations.big_data import MahoragaBigData
                    res = await asyncio.to_thread(MahoragaBigData().build_all_indexes)
                    await state.set_setting("bigdata_last_index_ts", str(now_ts))
                    logger.info(
                        f"BigData: Auto-sinkron LanceDB selesai ({res.get('total_indexed', 0)} rekaman, {res.get('elapsed_ms', 0)} ms)."
                    )

                last_harv_raw = await state.get_setting("bigdata_last_harvest_ts")
                last_harv = float(last_harv_raw) if last_harv_raw else 0.0
                if (now_ts - last_harv) >= 24 * 3600:
                    from harvey.integrations.universe_harvester import UniverseHarvester
                    hres = await UniverseHarvester().harvest_balanced_universe(max_per_segment=3)
                    await state.set_setting("bigdata_last_harvest_ts", str(now_ts))
                    logger.info(
                        f"BigData: Auto-panen 4 segmen selesai (+{hres.get('total_inserted', 0)} prospek baru)."
                    )
            except Exception as ex:
                logger.debug(f"BigData: Error in maintenance ticker: {ex}")
            if await _interruptible_sleep(1800, stop_event):
                break

    bigdata_task = asyncio.create_task(_bigdata_maintenance_ticker())

    while not stop_event.is_set():
        try:
            # 0. Approval or autopilot, as the config file says now: the
            # dashboard can switch it while Harvey runs.
            await sync_sending_mode(config, state)

            # 1. Check quiet hours. With send windows in recipients' own time
            # zones, quiet hours only stop prospecting and writing: a cycle
            # still reads replies and sends what's due (sends_through_quiet_hours).
            quiet = in_quiet_hours(config)
            send_only = quiet and sender.is_native and sends_through_quiet_hours(config)
            if quiet and not send_only:
                sleep_for = seconds_until_quiet_hours_end(config)
                logger.info(f"Quiet hours. Sleeping for {sleep_for // 60} minutes.")
                # Sleep in short polled chunks rather than one multi-hour
                # wait: found in production that a system sleep/wake cycle
                # during a long asyncio.wait_for left the process silently
                # stalled for the rest of the quiet-hours window (11+ hours
                # in one case) instead of resuming once the Mac woke up.
                # Re-checking wall-clock quiet hours every few minutes
                # bounds the worst case to QUIET_HOURS_POLL_SECONDS instead
                # of however long the missed timer would have run.
                shutting_down = False
                while in_quiet_hours(config):
                    if await _interruptible_sleep(QUIET_HOURS_POLL_SECONDS, stop_event):
                        shutting_down = True
                        break
                if shutting_down:
                    break
                continue
            # Quiet hours wake only for cold email that can go now: due,
            # inside its recipient's window, not held by a cap or a pause.
            # Replies wait for the operator's hours, as they always have.
            sender.cold_only = send_only
            if send_only and not await sender.cold_due_in_window():
                logger.info(
                    f"Quiet hours. No cold email can go now; checking again in "
                    f"{config.usage.heartbeat_interval_minutes} minutes."
                )
                if await _interruptible_sleep(interval, stop_event):
                    break
                continue

            # 2. Is the brain usable at all? (Codex: CLI version, isolation,
            # login, account and model checked without a prompt.) If not, do nothing
            # this cycle — no replies consumed, nothing written or sent —
            # and say why, rather than running agents that would each fail.
            problem = await brain.preflight()
            if problem:
                logger.error(
                    f"Harvey is paused: {problem} Nothing was sent or changed. "
                    f"Re-checking in {config.usage.heartbeat_interval_minutes} minutes."
                )
                if await _interruptible_sleep(interval, stop_event):
                    break
                continue

            # 3. Check usage budget (real subscription quota when readable,
            # else Harvey's own call counter)
            if not await brain.is_within_budget(
                max_calls, max_percent=config.usage.max_daily_claude_percent
            ):
                logger.info(
                    f"{brain.provider_label} usage limit reached "
                    f"({config.usage.max_daily_claude_percent}% of quota or "
                    f"{max_calls} calls). Sleeping 1h, then re-checking."
                )
                if await _interruptible_sleep(3600, stop_event):
                    break
                continue

            # 4. Decide what to do (in quiet hours: only read replies and send)
            if send_only:
                logger.info("Quiet hours: reading replies, then sending what's due "
                            "in recipients' own time zones.")
                summary = {}
                action = "quiet_hours_send"
            else:
                logger.info("Checking pipeline state...")
                summary = await state.get_state_summary()
                action = await decide_next_action(brain, state, config, summary=summary)

            # 5. Execute — run independent agents in parallel where possible
            # Handler is always safe to run alongside other agents
            tasks = []
            has_open_convos = summary.get("open_conversations", 0) > 0

            if action == "handle_replies":
                tasks.append(("handle_replies", handler.run()))
                # Never starve Writer when verified email prospects are waiting
                _, writable_now = _new_counts(summary)
                if writable_now > 0:
                    tasks.append(("write_campaign", writer.run()))
            elif action == "prospect":
                tasks.append(("prospect", scout.run()))
                # Also handle replies in parallel if needed
                if has_open_convos:
                    tasks.append(("handle_replies", handler.run()))
                # Analyst is cheap (no Claude calls) — keep analytics fresh
                tasks.append(("analyze", analyst.run()))
            elif action == "write_campaign":
                tasks.append(("write_campaign", writer.run()))
                if has_open_convos:
                    tasks.append(("handle_replies", handler.run()))
            elif action == "idle":
                tasks.append(("analyze", analyst.run()))

            # Sending happens in a second phase, after every agent above has
            # finished: the handler's stop-on-reply must cancel follow-ups to
            # people who just replied before anything is sent. Native mail
            # providers drain the outbox every cycle, so their mailbox is
            # read every cycle too (handle_replies above only runs when a
            # conversation is already open).
            send_tasks = []
            if action == "send_campaign":
                send_tasks.append(("send_campaign", sender.run))
            elif sender.is_native:
                send_tasks.append(("send_outbox", sender.run))
            if getattr(getattr(config.channels, "whatsapp", None), "enabled", False):
                send_tasks.append(("send_whatsapp", whatsapp.run))
            if sender.is_native and not any(n == "handle_replies" for n, _ in tasks):
                tasks.append(("read_replies", handler.run()))

            # Profiling rides along every cycle. It is three HTTP requests per
            # business with no model call, so it costs nothing against the
            # Claude budget the rest of this loop is rationing — and it is what
            # turns a name and a domain into something worth writing about.
            if summary.get("unprofiled", 0):
                tasks.append(("profile", run_profile_stage(state, limit=25, config=config)))

            outcomes = await _run_agents(tasks)

            # Phase 2: send — only after every reply-reading task this cycle
            # finished reading and handling replies (it returns True; a
            # failure, exception or timeout doesn't), and only if the brain
            # didn't stop being usable meanwhile. Otherwise a reply may be
            # unread, so its stop-on-reply hasn't run: queued emails wait.
            if send_tasks:
                unread = [n for n, _ in tasks
                          if n in REPLY_READERS and outcomes.get(n) is not True]
                paused = brain.paused_reason
                if paused or unread:
                    reason = paused or "replies couldn't all be read and handled."
                    logger.error(
                        f"Not sending this cycle: {reason} Queued emails wait "
                        "until replies can be read again."
                    )
                    held = send_hold_reason(unread, paused, getattr(handler, "last_read_problem", ""))
                    if held is not None:
                        await record_send_phase(state, held)
                else:
                    await record_send_phase(state, send_hold_reason([], "", ""))
                    await _run_agents([(name, run()) for name, run in send_tasks])

            # 6. Log the action (best-effort; never kills the loop)
            try:
                await state.log_action(action_type=action, agent="main")
            except Exception as e:
                logger.warning(f"Failed to log action '{action}': {e}")

            consecutive_errors = 0

            # 7. Sleep until next heartbeat
            logger.info(
                f"Cycle complete. Sleeping for {config.usage.heartbeat_interval_minutes} minutes."
            )
            if await _interruptible_sleep(interval, stop_event):
                break

        except (KeyboardInterrupt, asyncio.CancelledError):
            break
        except Exception as e:
            consecutive_errors += 1
            backoff = min(
                ERROR_BACKOFF_BASE * (2 ** (consecutive_errors - 1)),
                ERROR_BACKOFF_CAP,
            )
            logger.error(
                f"Error in heartbeat (failure #{consecutive_errors}): {e}",
                exc_info=True,
            )
            logger.info(f"Recovering... sleeping {backoff}s before retry.")
            if await _interruptible_sleep(backoff, stop_event):
                break

    report_ticker_task.cancel()
    procurement_ticker_task.cancel()
    wa_sync_task.cancel()
    bigdata_task.cancel()
    logger.info("Harvey shutting down. Deals don't close themselves, but I need a break.")


def _needs_setup() -> bool:
    """Check if Harvey needs first-time setup."""
    from harvey.paths import PROJECT_ROOT
    from harvey.config import _find_config_file, ConfigFileNotFoundError

    env_file = PROJECT_ROOT / ".env"

    # If .env doesn't exist, definitely needs setup
    if not env_file.exists():
        return True

    # harvey.local.yaml wins over the tracked harvey.yaml template, same
    # resolution order the real config loader uses (see config.py).
    try:
        config_file = _find_config_file()
    except ConfigFileNotFoundError:
        return True

    try:
        import yaml
        from harvey.textio import read_user_text
        config = yaml.safe_load(read_user_text(config_file))
        if not isinstance(config, dict):
            return True
        company = (config.get("persona") or {}).get("company", "")
        if company in ("Your Company", ""):
            return True
    except Exception:
        return True

    return False


async def _run_with_signals():
    """Run the heartbeat with SIGINT/SIGTERM wired to a graceful shutdown."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig_name: str):
        if stop_event.is_set():
            logger.info("Second shutdown signal — exiting immediately.")
            sys.exit(1)
        logger.info(f"Received {sig_name}. Finishing current work, then shutting down...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread fallback
            signal.signal(sig, lambda s, f: _request_shutdown(signal.Signals(s).name))

    await heartbeat(stop_event)


def main():
    """Entry point."""
    # Check for first-time setup
    if _needs_setup():
        print("\n  First time running Harvey? Let's get you set up.\n")
        from harvey.setup import run_setup
        asyncio.run(run_setup())
        return

    try:
        asyncio.run(_run_with_signals())
    except KeyboardInterrupt:
        logger.info("Goodbye.")


if __name__ == "__main__":
    from harvey.textio import utf8_stdio
    utf8_stdio()
    main()
