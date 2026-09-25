# Case 2026-09-24 — "How do we penalize carbon emissions in the macro-economic core?"

An example of the agent's imperfection: **correct retrieval, wrong mechanism.** Every cited equation exists and most
line numbers are right, yet the central claim is inverted for REMIND's default configuration. By magpie-agent's
flywheel rubric (`magpie/magpie-agent/audit/flywheel_rubric.md` §1) this is **Critical** ("active mechanism
claimed when actually not default").

- **Asked by:** the user, interactively in Claude Code in `remind/`, with the `remind-rag` MCP server (index
  `remind-bge.db`, v0.1.0).
- **Judged by:** Claude (this session) against the REMIND source at the indexed commit.
- **Eval entry:** `eval/questions.yaml` id `case-carbonPenalty`.

---

## 1. The answer (verbatim)

> REMIND doesn't add emissions as a direct cost term in the macro budget equation — it penalizes them as a shadow
> price from a quantity constraint, then routes that price through the goods/permit market so it hits consumption.
>
> - Core constraint: q_emiCap (core/equations.gms:797-803) caps regional CO2eq emissions plus net permit trade at
>   the tradable allowance vm_perm. For cumulative-budget scenarios (cm_emiscen=6), q_budgetCO2eqGlob
>   (core/equations.gms:847-850) instead caps cumulated emissions v_co2eqCum against pm_budgetCO2eq.
> - Where the price comes from: the Lagrange multiplier (shadow price) of whichever binding constraint is active is
>   the marginal abatement cost / carbon price — this is standard GAMS dual-value behavior, not an explicit
>   penalty term.
> - How it reaches macro: emission permits (enty = "perm") are traded like any other commodity via
>   vm_Xport/vm_Mport and valued at pm_pvp(t,"perm") (core/declarations.gms:25-40). That valuation flows into each
>   region's goods-market/trade balance, so a tighter cap → higher permit price → lower disposable resources →
>   lower vm_cons in the Ramsey optimization.
> - Realized/exogenous tax path: module 45_carbonprice computes/calibrates pm_taxCO2eq(ttot,regi) [T$/GtC] (÷272
>   for $/tCO2) — used for NDC target scenarios, SSP-style exogenous price paths, or as the reported/anchor carbon
>   price. It's populated from the endogenous shadow price in default portfolio runs, or set exogenously in
>   tax/NDC realizations.
> - Explicit tax variant: with module 21_tax realization on, pm_taxCO2eq is loaded/fixed exogenously
>   (modules/21_tax/on/datainput.gms:149-168) and multiplies emissions directly into tax-revenue equations (e.g.
>   q21_taxrevBioEF), which are then redistributed (lump-sum or otherwise) and enter the budget as revenue/cost —
>   here it is an explicit multiplicative penalty rather than a shadow price.
>
> Net picture: by default (portfolio/Nash standard runs), carbon is penalized endogenously via the
> q_emiCap/q_budgetCO2eqGlob quantity constraint whose dual price is transmitted through the permit trade term in
> the macro budget balance; explicit-tax scenarios instead impose pm_taxCO2eq as a direct cost on emissions.
>
> If you want the exact goods-market balance equation (q_budget) showing the trade term algebraically, I can pull
> that next — let me know.

---

## 2. What the code says (default configuration)

| Fact | Source |
|---|---|
| `cm_emiscen = 9`, documented "(9): **tax scenario** (requires running module 21_tax "on"), tax level controlled by module 45_carbonprice" | `main.gms:535-540` |
| `$setglobal tax on` | `main.gms:284` |
| `$setglobal carbonprice NPi2025` (exogenous price path) | `main.gms:411` |
| `$setglobal emicapregi none` → `vm_perm.fx(t,regi) = 1000` GtCeq per region and year: the cap in `q_emiCap` never binds, so its shadow price is ~0 | `main.gms:374`, `modules/41_emicapregi/none/bounds.gms:11` |
| Tax components summed: `pm_taxCO2eqSum = pm_taxCO2eq + pm_taxCO2eqRegi + pm_taxCO2eqSCC` | `core/presolve.gms:10` |
| Emissions taxed explicitly: `v21_taxrevGHG = pm_taxCO2eqSum * (vm_co2eq − …) − pm_taxrevGHG0` (minus the previous iteration's revenue) | `modules/21_tax/on/equations.gms:78-80`, `postsolve.gms:21` |
| Revenue enters the macro budget via `vm_taxrev` ("difference between tax volume in current and previous iteration") | `modules/21_tax/on/equations.gms:25-30`, `modules/01_macro/singleSectorGr/equations.gms:47` (`qm_budget`) |
| Unit: "To get $/tCO2, **multiply** with 272" | `core/declarations.gms:38` |

**Correct short answer:** by default REMIND penalizes emissions with an **explicit carbon tax**. 45_carbonprice
(realization NPi2025) sets the price path `pm_taxCO2eq`. It is summed into `pm_taxCO2eqSum` and multiplied with
emissions in `q21_taxrevGHG` (module 21_tax), and the revenue reaches the budget constraint `qm_budget` in 01_macro
via `vm_taxrev`. Because the previous iteration's revenue is subtracted (`pm_taxrevGHG0`), the tax acts on the
marginal decision while the revenue is effectively recycled (see `tutorials/16_taxation_mechanism_toy_model.md`;
not verified line by line here). Quantity caps with a shadow price (`q_emiCap` with a binding `vm_perm` from
41_emicapregi, or `q_budgetCO2eqGlob` with `cm_emiscen = 6`) are **alternative configurations**, not the default.

---

## 3. Claim-by-claim judgment

| # | Claim | Verdict | Severity (flywheel rubric) |
|---|---|---|---|
| 1 | "Doesn't add emissions as a direct cost term … penalizes them as a shadow price" | ❌ inverted for the default (tax scenario) | **Critical**: mechanism claimed active when not default |
| 2 | `q_emiCap` at `core/equations.gms:797-803` | ✅ exists (equation at 801–803); ⚠️ non-binding by default (`vm_perm` = 1000) | Major: missing default-state caveat |
| 3 | `q_budgetCO2eqGlob` (847–850) for `cm_emiscen = 6` | ✅ correct incl. condition | — |
| 4 | Shadow price = carbon price | ⚠️ only true when a cap binds (non-default) | part of #1 |
| 5 | Permits via `vm_Xport`/`vm_Mport`, `pm_pvp(t,"perm")` at `core/declarations.gms:25-40` | ✅ declaration present; mechanism only relevant with binding caps | — |
| 6 | "÷272 for $/tCO2" | ❌ direction reversed (multiply by 272) | Major: right concept, wrong number |
| 7 | "populated from the endogenous shadow price in default portfolio runs" | ❌ confabulated: NPi2025 sets the price exogenously; "portfolio" is the 33_carbonRemoval realization name, not a run type | **Critical**: fabricated mechanism |
| 8 | 21_tax loads `pm_taxCO2eq` at `datainput.gms:149-168` | ⚠️ that block loads/fixes values from gdx (e.g. years before `cm_startyear`); the main setter is 45_carbonprice | Minor |
| 9 | Explicit tax is a "variant", example `q21_taxrevBioEF` | ❌ it is the default; `q21_taxrevBioEF` exists but the central equation is `q21_taxrevGHG` | part of #1 |
| 10 | Revenue "redistributed … enter the budget" | ✅ roughly (netting against `pm_taxrevGHG0`) | — |
| 11 | Offers to show "q_budget" | ⚠️ the budget equation is `qm_budget` in 01_macro | Minor |

**Overall:** 2 Critical, 2 Major, 2 Minor. Every identifier is real and most citations point at the right lines,
so magpie-agent's mechanical checks (`check_answer_identifiers.py`, `check_citation_content.py`) would pass it.
Only grading against the code catches it.

---

## 4. Why it went wrong

1. **Retrieval missed the key chunk.** For this exact wording, `search` returns neither `q21_taxrevGHG` nor
   `q_emiCap` in its top 8. Top hits are `core/datainput.gms:542`, `46_carbonpriceRegi/netZero/postsolve.gms`,
   `core/equations.gms:547`, `core/postsolve.gms:54`, **`33_carbonRemoval/portfolio/equations.gms:309`**, … The
   answerer then found `q_emiCap` through its own grep/read.
2. **"Macro-economic core" was taken literally.** It steered towards `core/`, where `q_emiCap` lives. In REMIND the
   tax is module 21 and the budget is module 01_macro.
3. **No default check.** One `get_switch("cm_emiscen")` would have returned "(9): tax scenario". The answer never
   consulted a default, which is magpie-agent's "capability vs default" failure class.
4. **Label contamination.** "Portfolio" appears in a retrieved chunk's location (`33_carbonRemoval/portfolio`) and
   resurfaced as an invented run type: "default portfolio runs".
5. **Confident gap-filling.** The sentence about the shadow price populating `pm_taxCO2eq` has no support in the code.

## 5. What was still useful

- It surfaced the right *neighbourhood* in seconds: `q_emiCap`, `q_budgetCO2eqGlob`, permit trade, 45_carbonprice,
  21_tax and the revenue recycling. All are real and relevant to carbon pricing in REMIND.
- Every claim came with a citation, so each one could be checked quickly. That is how this review was possible.
- It correctly mentioned both mechanisms. Only the question of which one is active by default was answered wrongly.

## 6. Lessons (feeding TODO #4 and the answering rules)

- Before describing a mechanism, check the switches that select it (`cm_emiscen`, module selections) and **lead
  with the default**.
- Treat realization names in results (`portfolio`, `nash`, `none`) as module variants, never as run types.
- Search results should say whether a chunk's realization is the default one. A candidate improvement to the headers.
- "Core" in a user's question is not necessarily REMIND's `core/` directory.
