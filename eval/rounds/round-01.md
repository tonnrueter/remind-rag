# Round 01 — baseline (REMIND RAG v0.1.0)

The "before" measurement for step 1 of the next-steps plan: ask the 20 reviewed questions, grade them against the
code, record verdicts and failure classes. Round 02 repeats the same questions after the default-awareness fixes.

## Setup

| | |
|---|---|
| Started | 2026-09-24 |
| Questions | `../../../geminis_questions_reviewed.md`, Part 1 (Q1–Q20), asked verbatim, one fresh session each |
| Answer key | same file, Part 2 |
| RAG | `rag` v0.1.0, index `data/remind-bge.db` (bge-small v1, 4,706 chunks, gms 0.35.0 export), MCP server `remind-rag` |
| Static context | **loaded**: `remind/CLAUDE.md` → `@remind-context/AGENT.md` (fixed 2026-09-24). This round measures "context + RAG". |
| Answerer | Claude Code in `remind/`, interactive; model as reported per question |
| Grader | Claude (Opus) in the rag session, against the REMIND source. **Not independent:** same party built the RAG. |
| Rubric | severities from `magpie/magpie-agent/audit/flywheel_rubric.md` §1 (Critical / Major / Minor) |

## Results

| Q | Kind | Model | Critical | Major | Minor | Main failure class | Details |
|---|---|---|---|---|---|---|---|
| Q1 | keep | Sonnet | 0 | 4 | 2 | premise accepted; purpose guessed from name | [below](#q1) |
| Q2 | keep | | | | | | pending |
| Q3 | keep | | | | | | pending |
| Q4 | keep | | | | | | pending |
| Q5 | trap | | | | | | pending |
| Q6 | trap | | | | | | pending |
| Q7 | trap | | | | | | pending |
| Q8 | trap | | | | | | pending |
| Q9 | trap | | | | | | pending |
| Q10 | rewrite | | | | | | pending |
| Q11 | rewrite | | | | | | pending |
| Q12 | rewrite | | | | | | pending |
| Q13 | rewrite | | | | | | pending |
| Q14 | rewrite | | | | | | pending |
| Q15 | replacement | | | | | | pending |
| Q16 | replacement | | | | | | pending |
| Q17 | replacement | | | | | | pending |
| Q18 | replacement | (not stated) | 0 | 0 | 2 | overgeneralization (minor) | [below](#q18) |
| Q19 | replacement | | | | | | pending |
| Q20 | replacement | | | | | | pending |

## Failure classes (running tally, incl. the two earlier cases)

| Class | Description | Seen in |
|---|---|---|
| **default not checked** | mechanism described without checking which switch/realization is active by default | carbon case (Critical), damages case #2, Q1 #6 |
| **purpose guessed from a name** | meaning inferred from an identifier or label instead of its declaration/doc | carbon case ("portfolio" → run type), Q1 #7 ("exp" → expansion) |
| **switch meaning guessed** | value of a switch explained from intuition instead of its documented meaning | damages case #6 (`cm_emiscen = 9`) |
| **premise accepted** | the question's framing is taken as true and results are forced into it | Q1 (7 "infeasibility valves" with one shared purpose) |
| **quote misattributed** | a real comment attached to the wrong construct | Q1 #2 ("prevents zero-demand infeasibility") |
| **retrieval miss** | the key chunk isn't found by `search` | carbon case (`q21_taxrevGHG` not in top 8) |
| **overreach from a name** | a module's name taken as its full semantics | damages case #7 ("internalize" ≠ cost-benefit optimum) |

Earlier cases (before this round, same setup except without static context):
[`../cases/2026-09-24-carbon-penalty-macro-core.md`](../cases/2026-09-24-carbon-penalty-macro-core.md) (2 Critical),
[`../cases/2026-09-24-damages-standalone.md`](../cases/2026-09-24-damages-standalone.md) (1 Major).

---

## Q1

**Question (verbatim):** Find all instances in `equations.gms` across the entire codebase where we use an
artificial penalty variable to prevent infeasibility in our optimization loop.

### Answer (verbatim, Sonnet)

> Grepped every equations.gms in core/ and modules/ for penalty/slack/valve/dummy patterns. Found 7 distinct
> artificial-feasibility mechanisms, all sharing the same purpose: give the solver a "relief valve" so a constraint
> that could otherwise be infeasible (zero demand, mismatched capacities, sudden switch-point discontinuities) stays
> solvable, usually at a small cost penalty in the objective.
>
> | # | Variable(s) | Location | What it prevents |
> |---|---|---|---|
> | 1 | v_changeProdStartyearSlack | core/equations.gms:947 | Forces the production-change constraint at cm_startyear to always allow some deviation — "always allow some change (depending on .up/.lo of the slack variable)" |
> | 2 | v_penSeFeSectorShare / q_penSeFeSectorShareDev* / vm_penSeFeSectorShareDevCost | core/equations.gms:1107, 1207-1281 (gated by cm_seFeSectorShareDevMethod) | SE/FE sector-share convergence penalty — comment explicitly says "prevents zero-demand infeasibility" when a fuel share in a sector goes to zero |
> | 3 | v_co2capturevalve | core/equations.gms:594, 888 | "Valve from CO2 capture step" — absorbs the mismatch when capture capacity and CCS/CCU offtake capacity have different lifetimes, so the capture/storage balance stays feasible |
> | 4 | v21_emiAllco2neg_slack | modules/21_tax/on/equations.gms:351-356 | Keeps vm_emiAllco2neg =e= -vm_emiAll + slack feasible when gross emissions go negative; slack absorbs the excess so the equality never forces infeasibility |
> | 5 | v21_emiAllco2neg_acrossIterations_slack | modules/21_tax/on/equations.gms:360-365 | Same pattern across Nash iterations, comparing CDR emissions to a reference gross-emissions level |
> | 6 | v31_fuSlack | modules/31_fossil/MOFEX/equations.gms:89-115, modules/31_fossil/timeDepGrades/equations.gms:142-168 | Absorbs leftover extraction capacity when early retirement of oil wells is switched on, so the extraction-limit equation doesn't bind infeasibly |
> | 7 | v36_expSlack | modules/36_buildings/simple/equations.gms:43, 63 | Slack on the buildings-expansion balance, penalized at 1e-8 in the objective — negligible cost, just keeps the equation solvable |
>
> Related but distinct (worth knowing, not the same mechanism): vm_costAdjNash in
> modules/80_optimization/nash/equations.gms:29-40 is a quadratic trade-adjustment cost that stabilizes
> Nash-iteration convergence by penalizing large jumps in trade patterns between iterations — it's a convergence
> stabilizer, not an infeasibility escape valve.
>
> Not actually penalties (flagged by my grep but false positives): q31_mc_dummy / q31_totfuex_dummy in
> modules/31_fossil/{grades2poly,timeDepGrades}/equations.gms are "dummy" only in the sense of auxiliary
> bound-determination equations (regional uranium extraction bounds) — no slack/penalty involved.

### Judgment

| # | Claim | Verdict | Severity | Source |
|---|---|---|---|---|
| 1 | `v_changeProdStartyearSlack` keeps the start-year change feasible | quote and line correct; but it is a *cost-free tolerance* ("slack variable to allow a minimum cost-free change with respect to the reference run"), not a feasibility valve | Minor | `core/declarations.gms:369`, `core/equations.gms:947` |
| 2 | Sector-share penalty "prevents zero-demand infeasibility" | ❌ it is an *allocation incentive* ("sectors are incentivized to apply similar shares of bio-fuels, synfuels, and fossil"; default `sqSectorAvrgShare`); the quote belongs to the solids exclusion in `q_shSeFeSector` | Major | `main.gms:2116-2123`, `core/equations.gms:1104-1108` |
| 3 | `v_co2capturevalve` for lifetime mismatch | ✅; line 594 → 596 | Minor (citation off by 2) | `core/declarations.gms:159`, `core/equations.gms:593-596, 888` |
| 4–5 | `v21_emiAllco2neg[_acrossIterations]_slack` keep the equality feasible | ❌ auxiliary variables extracting net-negative emissions (`max(0,−x)` for taxing CDR): "dummy variable to extract net-negative CO2 emissions from emiAll" | Major | `modules/21_tax/on/declarations.gms:148-150`, `equations.gms:349-364` |
| 6 | `v31_fuSlack` absorbs extraction under early retirement | ✅ mechanism roughly right ("oil that is not extracted but put aside never to be used again"); ❌ no default caveat: only in `timeDepGrades` and `MOFEX`, default is `grades2poly` | Major (missing default caveat) | `modules/31_fossil/timeDepGrades/declarations.gms:95`, `equations.gms:155-168`, `main.gms:327` |
| 7 | `v36_expSlack` = "buildings-expansion balance", 1e-8 "in the objective" | ❌ overflow guard: "slack variable to avoid overflow on too high logistic function exponent" in the H2 phase-in cost; the 1e-8 term sits in `q36_costAddH2PhaseIn` | Major | `modules/36_buildings/simple/declarations.gms:26`, `equations.gms:38-43, 58-63` |
| + | `vm_costAdjNash` is a stabilizer, not a valve | ✅ | — | `modules/80_optimization/nash/equations.gms:29-40` |
| + | `q31_mc_dummy` / `q31_totfuex_dummy` are false positives | ✅ "only for determining the regional uranium bounds" | — | `modules/31_fossil/grades2poly/equations.gms:69-79` |

**Totals:** 0 Critical, 4 Major (#2, #4–5, #6, #7), 2 Minor (#1, #3).

**Completeness check:** all declared variables with slack/valve/dummy/penalty/infeasib in name or description
(symbol table query) — no further candidate; the remaining hits are welfare inconvenience penalties
(`v02_inconvPen*`), sector-share penalties and the uranium dummy objective. REMIND has almost no classic
infeasibility escape variables: genuine valves are `v_co2capturevalve` and (non-default) `v31_fuSlack`;
`v36_expSlack` is a numerical guard, `v_changeProdStartyearSlack` a tolerance.

**What went well:** grepped instead of relying on top-k search (right tool for an exhaustive question); no
invented identifiers (even avoided the stale name `v_fuelslack`, which only exists inside a description string);
separated the Nash stabilizer and discarded the dummy equations.

**What went wrong:** premise accepted (7 items forced into "one shared purpose"); purposes guessed from names
("exp" → expansion) instead of read from declarations; default caveat missing (#6); quote attached to the wrong
construct (#2). A good answer would have started with "fewer than you think".

---

## Q18

**Question (verbatim):** What happens to the direct air capture equations and variables when `cm_33DAC` is set to 0?

### Answer (verbatim; model not stated)

> Set membership: DAC is removed from the model entirely
>
> modules/33_carbonRemoval/portfolio/sets.gms:79-86 only populates "dac" into the module's technology sets when
> cm_33DAC eq 1:
>
> ```
> if(cm_33DAC eq 1,
>       te_used33("dac") = YES;
>       teNoTransform33("dac") = YES;
>       teNoTransform2rlf33("dac", "1") = YES;
>       teAdj33("dac") = YES;
>       teLearn33("dac") = YES;
>       te_ccs33("dac") = YES;
> );
> ```
>
> With cm_33DAC = 0, none of this fires, so "dac" never enters te_used33. Then line 109, te(te_used33) = YES,
> propagates the used CDR techs into the global technology set te — so "dac" also never becomes a member of te,
> teNoTransform, teAdj, or teLearn anywhere in the model, not just module 33.
>
> Consequences for equations and variables
>
> - Equations that sum/loop over te_used33 or the global te (capacity constraints, cost equations, emission
>   balances in both module 33 and core) simply skip "dac" — GAMS dynamic-set domain control means no equation
>   instances are generated for it at all. This isn't a bound of zero, it's non-existence in the equation domain.
> - Variables are still declared (they're declared over all_te, which is the static superset), but every
>   DAC-indexed instance gets hard-fixed to zero via .fx in bounds.gms:23-26:
>   ```
>   vm_emiCdrTeDetail.fx(t,regi,te_all33)$(not te_used33(te_all33)) = 0;
>   v33_FEdemand.fx(t,regi,entyFe,entyFe2,te_all33)$(not te_used33(te_all33) and fe2cdr(entyFe,entyFe2,te_all33)) = 0;
>   vm_co2emi_cdrFE_beforeCapture.fx(t,regi,te_all33)$(not te_ccs33(te_all33)) = 0;
>   ```
> - The DAC-specific bounds block never executes: bounds.gms:41-45 (minimum DAC capacity, feedstock exclusions for
>   low-temp DAC) is itself gated by if(te_used33("dac"), ...), so it's simply skipped.
> - Capacity variable vm_cap(t,regi,"dac",rlf): since "dac" isn't in the active te/teNoTransform2rlf sets, it's
>   excluded from all capacity-balance/investment equations globally (in core/equations.gms too) — effectively
>   inert, not just zero-bounded.
>
> Net effect
>
> DAC becomes a technology that literally cannot be built or contribute to CDR: it's excluded from the CDR
> portfolio's capacity/cost/emissions equations at the set level (not merely bounded to zero), and every
> DAC-specific variable instance is additionally .fx'd to 0 as a belt-and-suspenders safeguard. The model then
> solves the carbon-removal portfolio over whatever other technologies remain active (weathering via cm_33EW,
> oae_ng/oae_el via cm_33OAE, biochar, etc.).

### Judgment

| Claim | Verdict | Severity | Source |
|---|---|---|---|
| `dac` only enters the module sets when `cm_33DAC eq 1` | ✅ | — | `modules/33_carbonRemoval/portfolio/sets.gms:79-86` |
| `te(te_used33) = YES` (line 109) → with 0, `dac` is in no global `te` subset | ✅ `dac` is only in the static superset `all_te` (`core/sets.gms:135`, member line 248), not in `te`'s static list (`core/sets.gms:1073ff.`) | — | `sets.gms:109`, `core/sets.gms` |
| Variables declared over `all_te`, fixed via `.fx` (three lines quoted) | ✅ verbatim | — | `bounds.gms:23-26` |
| DAC bounds block gated by `if(te_used33("dac"), …)` | ✅ | — | `bounds.gms:41-46` |
| "no equation instances are generated for it at all … non-existence" | ⚠️ overgeneralized: true for equations over `te_used33`/`te`, but `q33_DAC_FEdemand` takes its domain from the static `fe2cdr` (which always lists `dac`) and is still generated, then trivially 0 = 0 | Minor | `equations.gms:105`, `sets.gms:32ff.` |
| Remaining portfolio incl. "biochar" | ⚠️ biochar is not a `te_used33` technology; module 33 only computes biochar revenue | Minor | `sets.gms:80-98` |

**Totals:** 0 Critical, 0 Major, 2 Minor. **Better than the answer key**: it found the global `te` propagation,
which the key lacked (key extended accordingly).

**Why it went well:** a concrete mechanics question confined to a few files; the answer read the code (sets,
bounds) instead of inferring from names, and quoted it verbatim.
