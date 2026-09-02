# Handoff Notes — Alpaca Options Trading Agent

Written for whoever picks this up next. Covers what's built, why it's built that way, whether
that was the right call, what's actually wrong with it right now, and what to do about it.
Repo: https://github.com/Sairishwanth89/alpaca_software

## 1. What this is

An autonomous options-trading agent for Alpaca's paper environment, built for lablab.ai's
"Alpaca AI Trading Agents Hackathon." Core hackathon rules: must be an autonomous agent using
Alpaca's Trading API, must use Alpaca's MCP server or CLI, must trade options, competition
account must start at exactly $100,000 (fixed, not adjustable), and a fresh dedicated paper
account is required for judging — **the account currently wired up is not fresh, it has test
trades on it. Create a new one before final submission.**

Judging is 5 weighted criteria: P&L Performance, Technology Implementation, Creativity &
Originality, Presentation & Execution, Social engagement — not pure profit.

## 2. What's built, and why, module by module

### 2a. The core idea: don't trust a strategy until it's proven

The single organizing principle of this codebase: **nothing gets traded because an LLM thinks
it's a good idea.** Every strategy has to survive a statistical gate first
(`agent/backtest/metrics.py`, `agent/backtest/engine.py`), and only what survives is ever
presented to the live agent as tradeable. This is the right call for a hackathon judged partly
on "Technology Implementation" — most competing submissions will be an LLM with tool access and
a prompt; a real validation layer is a genuine differentiator, and it's also just correct
practice for anything that touches real capital, paper or not.

The gate (`validate_strategy_result` in `metrics.py`):
- At least 30 simulated trades (guards against small-sample noise).
- A bootstrap confidence interval whose **lower bound** excludes zero for **both** mean return
  and Sharpe — not just "doesn't straddle zero" (a strategy with a CI entirely below zero is a
  confirmed loser, not a pass, even though it also "excludes zero").
- Anything that passes gets automatically retested on a much longer lookback window
  (`EXTENDED_LOOKBACK_DAYS`), and demoted if it doesn't hold up — this caught a real case: a
  commodity-style calendar spread strategy that looked good on a short window reversed to a
  clear fail on extended history, which is exactly the overfitting pattern this exists to catch.
- Later strengthened further with a **sub-period stability** check
  (`validate_sub_period_stability`): the extended-history trades get split chronologically in
  half, and *both* halves have to individually pass, not just the combined sample. This is what
  demoted the original headline strategy (see §4).

Every validated (or rejected) result gets written to `docs/strategy_graveyard.md` with real
numbers — pass or fail — so a falsified idea is documented, not silently deleted, and doesn't
get re-tested from scratch by mistake later.

**Is this the right way to do it?** Yes, I'd defend this as the correct default for a trading
system, hackathon or not. The mistake most people make with a "let the AI decide" trading demo
is skipping exactly this step. Where I'd push back on my own design: see §4, because the
backtest engine itself has real, newly-discovered bugs that undermine some of what this gate
was supposed to guarantee.

### 2b. Two ways to reason about a trade: single-agent and Proposer/Critic

`agent/live_agent.py` (Anthropic) and `agent/live_agent_openai.py` (OpenAI, structurally
identical, different SDK) give an LLM Alpaca's real MCP tools and let it research + decide +
execute in one tool-use loop. `agent/multi_agent.py` splits this into two roles: a **Proposer**
whose tool list has every order-placing tool *removed* (it is structurally incapable of trading,
only of calling a local `propose_trade` tool), and an independent, adversarially-prompted
**Critic** that can veto the proposal before anything reaches the deterministic risk gate.

**Why built this way:** the single-agent version is the obvious, first-thing-anyone-builds
approach — necessary but not differentiated. The Proposer/Critic split is a genuine second
opinion with actual privilege separation (not just "ask the same model twice"), and it directly
answers a real self-criticism: the core trading *reasoning* here is fairly conventional
(read data, pick from an enumerated strategy list) — the two-agent structure is one of the few
places the LLM does something a rules engine couldn't cheaply replicate.

**Is this the right way?** The Critic reviews only what the Proposer *reports* — it doesn't
independently re-fetch live data to check the Proposer isn't misrepresenting what it saw. That's
a real, disclosed gap, not an oversight: giving the Critic its own tool loop would double the
research cost for a check that's really about internal-consistency and evidence-citation, not
data re-verification. Reasonable tradeoff, worth knowing about.

### 2c. Zero-cost deterministic execution — for testing, not for the actual entry

`agent/deterministic_agent.py` mechanically trades only symbol/strategy combinations that
already cleared the statistical gate — no LLM call anywhere. Built specifically so the trading
*mechanics* (real-chain strike matching, risk gates, order placement) could be tested for free
before spending anything on the LLM paths. It caught several real bugs this way (see §5) at
zero cost, which is exactly what it was for.

**Is this the right way?** Yes for testing. It should **not** be the primary way this system
presents itself for judging, though — the hackathon explicitly wants an *AI* trading agent, and
a purely rule-following executor doesn't showcase that, even though the statistical machinery
behind it is a real technology asset either way.

### 2d. Order & position management, kill switch, guardrails

Every trading path only ever *opens* positions. `agent/order_manager.py` is what closes the
loop: cancels stale unfilled orders, force-closes anything within `FORCE_CLOSE_DTE` days of
expiration (pin-risk protection), and applies a universal stop-loss/profit-take off Alpaca's own
reported unrealized P&L. Runs first, automatically, in every cycle.

`agent/kill_switch.py` — a manual, independent-of-everything-else lever
(`python kill_switch.py on/off/status`) that blocks all new orders and optionally cancels
everything open, checked both inside the risk gate and at the top of every agent's cycle (so a
killed session doesn't even spend money researching).

`agent/risk/gates.py` (`RiskGate`) is the actual enforcement point every order-placing call goes
through: position-count limit, per-trade and portfolio-wide capital caps (percentage *and*
optional absolute-dollar, whichever is stricter), DTE bounds parsed from the real OCC symbol
(never trusting the LLM's arithmetic), naked-call protection, a daily-loss circuit breaker
correctly anchored to Alpaca's own `last_equity` field (not local state that could reset), and —
added after a live OpenAI-driven test trade proved it was necessary — a hard block on trading
any symbol with no backtest-cleared strategy. That last one matters: "prefer validated
strategies" was originally only prompt text, and a cheaper model traded an unvalidated symbol
(NVDA) on its very first real run. It's a code-level gate now, not a suggestion.

**Is this the right way?** Yes, and this is probably the second-strongest part of the codebase
after the validation gate. But §5 has real, serious bugs found in this exact layer — the design
intent is right, the implementation has gaps.

### 2e. Self-learning loop — deliberately not fine-tuning

`agent/reflection.py` logs a structured post-mortem on every closed position, checking two
things, neither of which is "did this trade make money": (1) process integrity — did entry
respect what the risk gate is supposed to guarantee; (2) once enough live history exists,
realized-vs-backtested drift. The summary feeds into the next cycle's prompt as plain context —
never a weight update.

**Why not fine-tune:** two reasons, both real. Not enough data (a handful of real trades total),
and more importantly, a strategy with a genuine edge still loses a predictable fraction of the
time (the account's one validated strategy at one point had a 70% backtest win rate — 30% of
*correctly executed* trades were still expected to lose). A naive "avoid what led to a loss"
loop would spend its whole learning budget unlearning real edge based on ordinary variance. This
is the right call — don't undo it in the name of "making the learning loop do more."

### 2f. Everything else worth knowing about

- **Prompt caching + real cost tracking** (`agent/llm_cost.py`, `agent/openai_cost.py`): every
  API call's actual dollar cost is computed from real token usage, not estimated, and `--loop`
  enforces a hard `--max-spend` cap that stops the session once *measured* spend hits it.
- **`agent/skew_strategy.py`**: IV put/call skew observation, deliberately live-only and never
  wired into the validated strategy set — skew can't be backtested with what this project has
  (no historical options chains), so it only accumulates real observations and never trades.
- **`setup.py`**: one-command onboarding — installs deps, checks for `uv`/`uvx`, collects keys
  interactively, verifies connectivity, runs the backtest. This is the only manual step a new
  user should need.
- **MCP-only execution**: every trading/data operation goes through Alpaca's official MCP server
  (`agent/mcp/client.py`, spawned via `uvx alpaca-mcp-server`), satisfying the hackathon's
  MCP/CLI requirement. `alpaca-py` (the direct SDK) is used only for offline backtest data pulls
  and a market-clock check — never for a trading action.

## 3. Honest overall assessment

**Technology Implementation:** genuinely strong — the validation methodology, the multi-agent
architecture, the guardrail layer, and (see §5) the fact that testing this against a real
account kept finding and fixing real bugs rather than assuming things worked, are all real
substance for the write-up.

**Creativity:** honestly middling. The actual trading *logic* an LLM reasons over is
conventional — pick from a fixed strategy list based on data it read. The creativity in this
project is almost entirely in the governance layer around the AI (validation gate, adversarial
critic, zero-cost testing mode), not in a novel signal or a new kind of reasoning. Said this to
the user directly earlier and it's still true.

**P&L Performance risk:** thin, and it's a real tension, not a false modesty thing. The
validation gate is strict on purpose, which means the account trades rarely — good statistics,
unexciting demo. As of this writing only one strategy/symbol combination has ever cleared the
full gate, and see §4 for why even that's now in question.

## 4. The current strategy situation — read this carefully

Over the course of this build:
- **AMD `long_directional`** was the first strategy to clear the full gate (Sharpe 2.39, 6-year
  extended history). Stress-testing it (parameter sensitivity, cost sensitivity, chronological
  sub-period split) found the edge was **concentrated in the second half of its 6-year window**
  — the first half alone didn't clear the bar (Sharpe 0.99). That's exactly the instability
  pattern that shouldn't be trusted, so the validation gate was strengthened
  (`validate_sub_period_stability`) to require both halves to individually pass, and AMD was
  correctly demoted under the new stricter rule.
- Widening the watchlist from 8 to 18 symbols under the new stricter gate found
  **GOOGL `cash_secured_put`** — Sharpe 3.47, 70% win rate, both sub-period halves pass, much
  smaller max drawdown than AMD ever had (~4% of account vs ~18%). This became the new (and, as
  of the last full backtest run, only) validated strategy.
- A review of the backtest engine found that `covered_call` was being simulated as a naked short
  call, not a real covered position (no stock-leg modeling in the simulator at all — verified
  empirically: over a synthetic -17% decline, true covered-call P&L was -$1,150 while the old
  simulator scored it +$1,915 with a passing Sharpe of 2.33). **This is now fixed** — see §5,
  it's no longer in the open bug list.
- The same round of review found the bootstrap validation gate resamples overlapping trade
  windows as if they were independent, empirically measuring a ~14% false-positive rate against
  a ~2.5% nominal rate. **This has been partially fixed**: a moving-block bootstrap replaced the
  plain i.i.d. resample, and two independent adversarial reviewers each measured their own
  empirical false-positive rate on the fixed code — 7.0% and 6.0% respectively, down from ~14%
  but still 2-3x the 2.5% nominal target. Both reviewers' own control tests suggest part of the
  remaining gap is inherent small-sample percentile-bootstrap bias, not purely a block-length
  problem — see §5 item 2 for the full detail. **Bottom line: GOOGL `cash_secured_put`'s original
  PASS was produced under the old, more permissive bootstrap** (the ~14%-false-positive version).
  It has not yet been re-run against the corrected (still-partial) bootstrap. Re-run
  `python run_backtest.py GOOGL` before continuing to trust that result as the account's one live
  strategy — it may well still hold (it's a much more conservative, higher-win-rate strategy than
  AMD ever was, with real margin above the bar on the original run), but that hasn't been
  re-confirmed against the corrected engine yet.

## 5. Known bugs and limitations, prioritized — this is the real "what to fix" list

Five independent fresh-eyes reviews have now run against this codebase across two rounds (dead
code, general cleanup/reuse, the risk-management path, the Alpaca MCP data-retrieval layer, and
the backtest statistical engine), with fixes applied and personally re-verified (diffs read,
regression tests run, not just trusting agent reports) after each round. **This section reflects
disk state as of commit `f39016a`** — check `git log`/`git status` if you're reading this later,
in case more has landed since.

### Fixed and verified (closed, not open items anymore)

- `covered_call` scored as a naked short call in the backtest (no stock-leg modeling) — fixed:
  the simulator now understands a `option_type == "stock"` leg, marked directly at the underlying
  price rather than through Black-Scholes, excluded from options transaction-cost friction.
- `enabled_for_paper` was set *before* the extended-retest/sub-period checks completed, walked
  back only by a `demote()` call, with no rollback on a mid-retest exception — fixed: the flag is
  now flipped exactly once, at the end of the per-strategy block, from the fully combined final
  outcome; verified with a mock test that a mid-retest exception leaves it `False`, not stranded.
- ATR-derived stop-loss was computed once from the initial window and reused stale for the
  extended retest — fixed: the extended retest now derives its own `risk_params` from the
  extended window's own ATR bars.
- Plain `iron_condor` used an underlying-distance stop tuned for directional/single-leg
  structures (forcing ~93% unwarranted stop-outs per an earlier finding) instead of the
  credit-multiple stop its own `iron_condor_vrp_45_21` sibling correctly uses — fixed: both now
  use the same credit-multiple stop mechanism.
- `place_option_order`/`cancel_order_by_id` results were never checked for success — fixed:
  `parse_order_error` (`agent/mcp_parsers.py`) is wired into every order-placing/closing/canceling
  call site (`order_manager.py`, `live_agent.py`, `live_agent_openai.py`, `multi_agent.py`,
  `deterministic_agent.py`) — a rejection is now logged/alerted as a rejection, not a success.
- Portfolio-wide capital cap wasn't cumulative across cycles — fixed: `RiskGate.update_positions`
  seeds `committed_this_cycle` from existing positions' estimated capital-at-risk once per
  process, instead of resetting to zero every cycle.
- `qty` was never validated as positive in `RiskGate.check()` — fixed: zero/negative qty is now
  rejected outright rather than silently coerced.
- Position-count and naked-call-cover checks used a stale, mis-keyed snapshot — fixed: the
  position-count gate now unions `held_option_roots | open_positions.keys() |
  symbols_committed_this_cycle`, keyed by underlying root, not full OCC symbol.
- `committed_this_cycle` had no rollback on an abandoned multi-leg batch — fixed: `release_commitment()`.
- Dead code removed: `strategy_drift_report`, `price_iron_condor_real_quotes` +
  `match_strike_by_delta`, `ContractQuote`, `STRATEGY_FUNCS`, `StrategyRegistry.all()`/
  `.enabled_for_live()`, plus unused `bs_delta`/`Optional`/`field`/`statistics` imports.

### Still open — safety/correctness-relevant

1. **Overlapping trade windows resampled as i.i.d. in the bootstrap — partially fixed, not
   closed.** A moving-block bootstrap (block length = `HOLD_DAYS // STEP_DAYS` = 3) replaced the
   plain i.i.d. resample in `agent/backtest/metrics.py`. Two independent adversarial reviewers
   each ran their own empirical false-positive-rate test (zero-true-edge random walks, engine's
   own HOLD_DAYS=21/STEP_DAYS=7 timing, n_boot=2000, ci=0.95) and both verdicted
   **PARTIALLY_FIXED**: measured rates of 7.0% and 6.0%, down substantially from the original
   ~14% but still 2-3x the 2.5% nominal target. Both reviewers separately found that even a
   *non-overlapping* control sample doesn't hit exactly 2.5% at this sample size (one measured
   5.5% on independent trades), suggesting part of the remaining gap is inherent small-sample
   percentile-bootstrap bias rather than something block-length tuning alone can fully close. One
   reviewer's own sweep found block_length=5 tested slightly better (7.5% vs 9.5% in their run)
   than the current default of 3, but the fixing agent deliberately kept the principled
   `HOLD_DAYS // STEP_DAYS` derivation rather than hand-tuning to one synthetic test — reasonable,
   but means there may be room for a more rigorous block-length selection method (e.g. a
   stationary/random-length bootstrap, or an automatic block-length selection procedure) as a
   next step. **Practical implication: every strategy this engine has ever passed, including
   GOOGL `cash_secured_put`, was validated under either the old (worse) or new (still imperfect)
   bootstrap — re-run `python run_backtest.py GOOGL` and treat a PASS as good evidence, not
   statistical certainty, until this gets tighter.**
2. **`iron_condor`/`covered_call` structural naked-call blocker — fixed for the batch-aware
   paths, still open for the free-form LLM path.** Two separate causes were found:
   - `iron_condor`'s short-call leg: already correctly hedged via `covered_by_paired_long` (a
     `_call_leg_is_hedged` check verified against a real `price_iron_condor()` plan — confirmed
     `hedged=True` for the short call) in `deterministic_agent.py` and `multi_agent.py`. This was
     actually already working before this round; the earlier "silently rejected" finding predates
     the `covered_by_paired_long` fix landing.
   - `covered_call`'s short call: `place_stock_order` was unconditionally rejected regardless of
     context, so there was no legitimate way to ever own the 100 covering shares. **Fixed**:
     `RiskGate.check()` gained `covered_call_stock_leg`, settable only by a caller that has
     already built a covered_call plan (never derivable from `tool_input`, so an LLM can't
     self-declare an arbitrary stock buy as "just cover") — still buy-only, still
     symbol-validated, still capital-capped, requires a limit price. `deterministic_agent.py` now
     buys the cover shares before the short call and locally reflects the fill so the same-cycle
     naked-call check sees it; `multi_agent.py`'s Proposer can now include the cover-share leg as
     a plain equity ticker in its proposal, routed through `place_stock_order` the same way.
     Verified with a 7-case standalone regression script (unauthorized buy rejected, authorized
     buy approved, sell-side still blocked even with the flag, naked call rejected pre-cover,
     approved post-cover, unpriced market buy rejected, oversized buy capped).
   - **Still genuinely open**: the free-form single-agent LLM paths (`live_agent.py`,
     `live_agent_openai.py`) call tools one at a time with no batch structure, so there's no way
     to verify a short call is actually hedged rather than the LLM just asserting it is — neither
     `iron_condor` nor `covered_call` can safely execute through those two paths yet. If you want
     them tradeable there too, the LLM would need to submit legs in an order the gate can check
     against real, already-filled positions (not a same-cycle claim) — not attempted here, flagged
     as the next real piece of work on this specific bug.

### Real but lower-severity

3. `CONFIG.watchlist` has no hard code-level enforcement — only prompt text plus the separately
   togglable backtest-validation gate.
4. `profit_target_pct` is fully implemented in the simulator but never actually passed by
   either backtest driver — every validated strategy's numbers reflect hold-to-stop-or-expiration
   only, not early profit-taking, even for strategies where that's standard practice.
5. `covered_call`'s `max_loss_per_contract` doesn't net out the premium collected — actually also
   fixed in the same pass that added the stock leg (nets out premium now), noted here only in
   case it regresses.
6. A meaningful amount of duplicated logic remains: the Alpaca response-envelope unwrap is still
   hand-copied in places instead of using `agent/mcp_parsers.py` consistently; per-strategy
   dispatch is hand-rolled in three separate places while `StrategyRegistry` sits there for
   exactly this; no use of `asyncio.gather` anywhere despite several places awaiting independent
   MCP calls sequentially.

### Explicitly disclosed design limitations (not bugs, known tradeoffs)

- The synthetic backtest path prices off historical *underlying* prices with Black-Scholes, not
  real historical bid/ask option chains — disclosed from the start, since full historical chains
  aren't reliably available for a broad watchlist.
- Multi-leg strategies submit legs sequentially, not as an atomic order — a fill-timing gap is
  possible even with the buy-before-sell ordering fix (see bug #4 above for why that fix isn't
  sufficient on its own).
- The order manager's exit rules are universal/strategy-agnostic, not each strategy's own
  specific backtested exit (e.g. the VRP iron condor's 21-DTE managed exit) — noted as a next
  step from the start, not attempted.
- The bootstrap CI fundamentally cannot represent tail risk that didn't occur in the sampled
  history — a methodological ceiling on the whole validation approach for short-premium
  strategies, not fixable by more bootstrap iterations.

## 6. What to actually do next, in order

1. **Read `git log`/`git status`/`git diff` right now** to confirm what's fixed vs. open in §5 —
   this doc was last updated against commit `f39016a`; verify nothing's drifted since.
2. **Re-run `python run_backtest.py GOOGL`** (or the full watchlist) against the now-corrected
   engine and bootstrap, and treat the result as the current source of truth — the previous
   GOOGL `cash_secured_put` PASS was produced under a worse (or, now, still-imperfect) bootstrap
   and hasn't been re-confirmed.
3. **If you want the statistical gate genuinely tight rather than "much better than before,"**
   revisit the moving-block-bootstrap's block-length selection (§5 item 1) — a data-driven
   block-length choice or a stationary bootstrap would likely close more of the remaining
   6-7%-vs-2.5% gap than the current fixed `HOLD_DAYS // STEP_DAYS` heuristic. Not required to
   ship, but the honest next step if P&L credibility needs to be airtight.
4. `iron_condor`/`covered_call` now execute correctly through `deterministic_agent.py` and
   `multi_agent.py` (§5 item 2, fixed) — they still can't safely fire through `live_agent.py`/
   `live_agent_openai.py`. Either extend those two paths to verify real hedged positions before
   allowing a short call, or keep steering the single-agent LLM demo toward `cash_secured_put`
   (the strategy that's actually cleared validation there) for now.
5. **Get a genuinely fresh Alpaca paper account** before final submission — the current one has
   test trades on it and won't be eligible for judging per the rules.
6. **Run the Claude-driven or OpenAI-driven agent live at least once**, budgeted — as of the
   last check, real autonomous LLM-driven trading activity in the account was still minimal;
   the demo/write-up needs to be able to show the agent actually deciding something, not just
   the deterministic path.
7. **Submission materials** (video, 1-page write-up, slides) — as of this doc, entirely undone,
   and required for judging regardless of how much further engineering happens.
8. If there's time after the above: the remaining lower-severity items in §5 (items 3-6).

## 7. Setup, quick reference

```bash
pip install -r requirements.txt
python setup.py                        # one-command interactive setup + connectivity check + backtest
python main.py --deterministic         # zero-LLM-cost test path
python main.py --once                  # single Claude-driven cycle
python main.py --once --provider openai
python main.py --multi-agent --once    # Proposer/Critic pipeline (Anthropic only)
python kill_switch.py status           # manual kill switch
python main.py --manage-only           # position/order housekeeping only
python run_backtest.py                 # re-run the statistical validation gate
```
