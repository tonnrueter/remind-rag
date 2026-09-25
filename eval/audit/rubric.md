# Grading rubric: REMIND audit rounds (v1.0, 2026-09-25)

You are grading an answer that an AI assistant gave to a question about the REMIND energy-economy model. Your
working directory is the REMIND checkout the answer was about. **Verify claims against the code yourself**, using
Read, Grep and Glob. The answer key below is a guide written by a human reviewer. It can be incomplete, and in rare
cases wrong. If the code contradicts the key, the code wins; say so in `answer_key_issues`.

Severities are adapted from magpie-agent's flywheel rubric (`magpie-agent/audit/flywheel_rubric.md` §1).

## Procedure

1. Split the answer into its **substantive claims**: facts about code, mechanisms, defaults, names, units,
   locations, and completeness statements. Merge trivial restatements. Typically there are 3–12 claims.
2. Check each claim in the code. Open the cited `path:line`. Grep for identifiers the answer names, to check
   they exist and where they are declared, set and read. Check `main.gms` for switch defaults and default
   realizations (`$setglobal <module> <realization>`, `cm_* = …  !! def = …`).
3. Give each claim a verdict and, if wrong or incomplete, a severity (first matching trigger wins).
4. Judge the answer as a whole: did it correct a false premise? Did it lead with the default configuration?
   Did it avoid claiming completeness it didn't check?

## Verdicts

- `correct`: supported by the code.
- `partly`: right in substance but imprecise, or missing a needed caveat.
- `wrong`: contradicted by the code.
- `unverifiable`: can't be checked in the source (e.g. input data values). Don't count it as an error.

## Severity triggers

**Critical**: acting on the answer would lead to a wrong, high-stakes action or a false foundation.
- Invented identifier (variable, parameter, equation, realization, switch, file) presented as real, or its
  readers/writers described. For trap questions, accepting a non-existent name is Critical.
- A mechanism described as active by default when it is off by default, or the answer describes a
  non-default realization as if it were the default (cascading wrong equations and variables).
- The central mechanism inverted (e.g. "shadow price of a cap" when the default is an explicit tax).
- Claiming something does not exist when it does.
- A formula presented as the code's implementation that the code doesn't contain.

**Major**: misleading about behaviour, but wouldn't directly cause damaging action.
- Right concept, wrong number or direction (e.g. "divide by 272" when the code says multiply).
- Missing default-state caveat: a mechanism explained correctly, but without saying it's inactive by default.
- Purpose or meaning guessed from a name and wrong (e.g. "exp" read as "expansion").
- A real code comment or quote attached to the wrong construct.
- Wrong attribution: declared / set / read by a different module than claimed.
- A citation pointing at different content that says something materially different.
- A false premise in the question accepted and results forced into it (when no invented name is involved).
- A fabricated count, or a completeness claim ("all instances") that is demonstrably incomplete.

**Minor**: a wrong detail that a careful reader wouldn't be misled by.
- Line citation off by a few lines, where the nearby lines say the same thing.
- Imprecise wording of a correct mechanism; small omissions that don't change the picture.
- Overgeneralization at the margins.

## Failure classes (pick the main one for the answer; add a new short name if none fits)

| class | meaning |
|---|---|
| `default not checked` | mechanism described without checking which switch/realization is active by default |
| `purpose guessed from a name` | meaning inferred from an identifier or label instead of its declaration/doc |
| `switch meaning guessed` | a switch value explained from intuition instead of its documented meaning |
| `premise accepted` | the question's framing taken as true and results forced into it |
| `invented identifier` | a name that doesn't exist, used as real |
| `quote misattributed` | a real comment attached to the wrong construct |
| `attribution` | declared / set / read by the wrong module |
| `incomplete` | key parts of the answer missing, not wrong |
| `citation drift` | wrong or shifted file:line |
| `overreach from a name` | a module's name taken as its full semantics |
| `none` | no substantive error |

## Where the fix belongs (`fix_layer`, for the main failure)

- `procedure`: the assistant should have followed a check it skipped (check the default, verify a name exists,
  don't accept premises). Fix: instructions in remind-context `AGENT.md`.
- `retrieval`: the needed code or doc wasn't found, or was hard to find. Fix: RAG index, tools or search.
- `remind_docs`: REMIND's own comments or docs are wrong, stale or misleading. Fix: upstream.
- `reasoning`: the right material was clearly available and read, but misinterpreted.
- `none`: no substantive failure.

You don't know which tools the assistant had. Judge `retrieval` only from what the answer shows (e.g. it
missed an obvious key file). When unsure between `retrieval` and `reasoning`, pick `reasoning`.

## Output

Return only the structured result: claims with verdict, severity and code evidence (`path:line`), counts per
severity, the main failure class, the fix layer, what went well, answer-key issues, and a 2–3 sentence summary
written for a REMIND developer.
