# Case 2026-09-24 — "Why is there another damage model on its own and in which scenarios is that relevant?"

A contrast to [`2026-09-24-carbon-penalty-macro-core.md`](2026-09-24-carbon-penalty-macro-core.md): **mostly
correct.** Structure, defaults and scenarios are right and well sourced; the errors are in *interpreting* a switch
value and in how far "internalize" goes. By magpie-agent's flywheel rubric (`magpie/magpie-agent/audit/flywheel_rubric.md`
§1): **0 Critical, 1 Major, 2 Minor.**

- **Asked by:** the user, interactively in Claude Code in `remind/`, with the `remind-rag` MCP server (index
  `remind-bge.db`, v0.1.0). The answer's first line refers to earlier turns of that conversation.
- **Judged by:** Claude (this session) against the REMIND source at the indexed commit.
- **Eval entry:** `eval/questions.yaml` id `case-damagesStandalone`.

---

## 1. The answer (verbatim)

> Good — confirms the same three MAgPIE-coupled counterparts exist for the land-use side.
>
> **Why there are two separate switches (damages vs internalizeDamages)**
>
> They answer two different questions, and REMIND deliberately decouples them:
>
> - damages (module 50_damages) computes a climate-damage factor (vm_damageFactor, vm_damageProdFactor) from
>   temperature and directly multiplies it into GDP/production in 01_macro's budget equation and 02_welfare — it's
>   a real economic effect on the modeled economy, not just a reporting artefact. But the emissions/mitigation
>   trajectory itself is still whatever the policy scenario dictates (e.g. NPi, a fixed carbon-budget path via
>   cm_emiscen) — the optimizer doesn't "see" the damages when deciding how much to abate.
> - internalizeDamages (module 51_internalizeDamages) closes that loop: it computes the marginal damage of an extra
>   ton of CO2 (the social cost of carbon, p51_scc/pm_taxCO2eqSCC) and feeds it back into the carbon price, so the
>   model actually re-optimizes emissions against total cost (mitigation + damages). This is why it hard-requires
>   cm_emiscen = 9 — you need the "optimal policy" emission-scenario mode rather than an exogenously fixed
>   budget/NPi path, since the point is to let the model choose the trajectory.
>
> So 50_damages on its own = damages-as-consequence ("given this policy pathway, how much does climate change cost
> economically?"), useful for scenario comparison / cost-benefit reporting without changing the emissions story.
> 51_internalizeDamages = damages-as-driver ("let the model pick emissions to minimize mitigation+damage cost").
>
> **Where damages is used standalone (internalizeDamages = off)**
>
> Same RIKEN scenario family, one scenario:
>
> | Scenario | damages | internalizeDamages |
> |---|---|---|
> | M-SSP2-NPi-KLW | KotzWenz | off |
>
> This is the NPi (current-policy) baseline with the Kotz et al. (2024) damage function applied on top — used as
> the damages-under-fixed-policy counterfactual, presumably to compare against VLLO-SSP2-KLW/VLLO-SSP1-KLW (same
> damage function, but internalized/optimized) in a cost-benefit study. The MAgPIE-coupled counterpart
> (config/scenario_config_magpie_RIKEN.csv:5) mirrors it for the land-use side (SSP2|NPI|rcp4p5).
>
> Everywhere else in the repo (all other 19 scenario config files), damages is unset → defaults to off in
> main.gms → module 50 sits in its off realization, fixing vm_damageFactor = 1 (no GDP effect at all).

---

## 2. What the code says

| Fact | Source |
|---|---|
| Defaults: `damages off`, `internalizeDamages off`, `welfare utilitarian` | `main.gms:438`, `:453`, `:252` |
| GDP damage enters the budget: `qm_budget … vm_cesIO(ttot,regi,"inco") * vm_damageFactor(ttot,regi)`; production-factor damages at line 103 | `modules/01_macro/singleSectorGr/equations.gms:20-21`, `:103` |
| In KotzWenz the damage is **fixed per iteration**: `vm_damageFactor.fx(ttot,regi)$(ttot.val ge cm_startyear) = pm_damage(ttot,regi)`; `vm_damageProdFactor.fx = 1` | `modules/50_damages/KotzWenz/bounds.gms:9-10` |
| The code's own limitation note: "Unless the realization "KotzWenzItr" is used for module 51_internalizeDamages, the damages are not actually part of the optimization, but just enter as a fixed variable reducing output, updated in between iterations." | `modules/50_damages/KotzWenz/realization.gms:11` |
| `off` fixes both factors to 1 | `modules/50_damages/off/bounds.gms:9-10` |
| Welfare: damage variables used in `ineqLognormal`; the default `utilitarian` lists them in `not_used.txt` | `modules/02_welfare/ineqLognormal/equations.gms`, `modules/02_welfare/utilitarian/not_used.txt` |
| 51 realizations: "Requires cm_emiscen set to 9 (for now)" (documentation; no enforcing check found) | `main.gms:441-452` |
| `cm_emiscen = 9` means "**tax scenario** (requires 21_tax on), tax level controlled by 45_carbonprice"; it is the default | `main.gms:535-540` |
| SCC enters the carbon price as one summand: `pm_taxCO2eqSum = pm_taxCO2eq + pm_taxCO2eqRegi + pm_taxCO2eqSCC` | `core/presolve.gms:10` |
| Only `scenario_config_RIKEN.csv` sets damages: row 5 `M-SSP2-NPi-KLW` (KotzWenz / off, copies `M-SSP2-NPi`), rows 6–7 `VLLO-SSP2-KLW`, `VLLO-SSP1-KLW` (KotzWenz / KotzWenzItr, copy `VLLO-SSP*-PkBudg650`) | `config/scenario_config_RIKEN.csv:5-7` |
| MAgPIE counterpart row 5: `M-SSP2-NPi-KLW;…;SSP2\|NPI\|AR-natveg\|cc\|rcp4p5;…` | `config/scenario_config_magpie_RIKEN.csv:5` |
| 21 scenario config files in total | `config/scenario_config*.csv` |

---

## 3. Claim-by-claim judgment

| # | Claim | Verdict | Severity |
|---|---|---|---|
| 1 | 50_damages computes damage factors from temperature and multiplies GDP/production in 01_macro's budget | ✅ | — |
| 2 | … "and 02_welfare" | ⚠️ only in the non-default `ineqLognormal` realization | Minor: missing default caveat |
| 3 | The optimizer doesn't see damages when deciding abatement | ✅ confirmed by `.fx = pm_damage` and the `@limitations` note | — |
| 4 | 51 computes the SCC (`p51_scc` → `pm_taxCO2eqSCC`) and feeds it into the carbon price | ✅ | — |
| 5 | 51 "hard-requires cm_emiscen = 9" | ✅ documented requirement ("for now"); "hard" overstates it, no enforcement found | — |
| 6 | …because 9 is the "optimal policy" mode rather than an exogenous NPi/budget path | ❌ 9 is the *tax scenario* and the default, NPi runs use it too; the real reason is that the SCC is added as a **tax component**, which only acts in tax mode | **Major**: wrong meaning of a switch value |
| 7 | 51 = "let the model pick emissions to minimize mitigation + damage cost" | ⚠️ overstated: the SCC is added *on top of* the existing price path (summand in `pm_taxCO2eqSum`); in `VLLO-*-KLW` that path is a peak-budget one (`PkBudg650`), so not a pure cost-benefit optimum | Minor |
| 8 | Standalone use only in `M-SSP2-NPi-KLW` (KotzWenz / off), NPi baseline | ✅ | — |
| 9 | Presumed comparison to `VLLO-SSP2-KLW` / `VLLO-SSP1-KLW` | ✅ rows exist (hedged interpretation, fine) | — |
| 10 | MAgPIE counterpart `scenario_config_magpie_RIKEN.csv:5`, SSP2\|NPI\|rcp4p5 | ✅ | — |
| 11 | All other 19 config files leave damages unset → off → `vm_damageFactor = 1` | ✅ (21 files minus the RIKEN pair) | — |
| 12 | Kotz et al. (2024) | ✅ | — |

---

## 4. Why this went better than the carbon-penalty case

1. **The question is structural:** which module, which switch, which scenarios, which default. `get_module`,
   `get_scenario`, the scenario chunks and the `main.gms` switch docs answer that directly. The carbon-penalty
   question needed a mechanism traced across modules.
2. **Defaults were checked** ("unset → defaults to off in main.gms"), the step missing in the other case.
3. **The code states the key point itself** in the KotzWenz `@limitations` note.

## 5. What still went wrong, and why

- **#6: the meaning of a switch value.** The answer explained *why* 9 is required from general intuition
  ("optimal policy mode") instead of reading the documented meaning of `cm_emiscen = 9` ("tax scenario").
  `get_switch("cm_emiscen")` would have shown it.
- **#7: overreach from a name.** "Internalize damages" suggests a full cost-benefit optimum; the code adds the SCC
  as one component of the price, on top of whatever else sets it.
- **#2: capability vs default in miniature.** Correct that welfare *can* use damages; not in the default realization.

## 6. Side finding: goxygen misses `@limitations:`

The KotzWenz note is written `*' @limitations: …` (with a colon). goxygen's block pattern
(`^\*' @[a-z]*( |(\{.+\})|$)` in `goxygen/R/extractDocumentation.R`) needs a space or end of line after the tag, so
this block is not extracted, and our `limitations` chunks don't include it (the text is still in the
realization chunk). Affected: all 7 in `50_damages` — `DiceLike`, `KotzWenz`, `KWLike`, `KWTCint`, `KW_SE`,
`Labor`, `TC` (`realization.gms`). Tracked in `TODO.md`.
