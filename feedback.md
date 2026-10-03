# Senior engineering review

## Executive verdict

The repository has a strong architecture and substantially implements the required eval loop: data-driven scenarios, fresh accounts, deterministic checks, an LLM judge, simulated users, persistent evidence, aggregation, and HTML/JSON/JUnit reporting.

It is not submission-ready yet. The main concern is not code style; it is measurement integrity. A few paths can grade a saved run against the wrong scenario, omit safety checks from the denominator, or fail while resuming. The required findings deliverable is also absent, so the repository does not yet answer the assignment's central question: what is wrong with the live agent, and how often?

Recommended disposition: fix the P0 and P1 items below, add regression tests for them, then run the live suite at `k >= 5` and write the findings.

## Review scope and verification

Reviewed:

- `Brief_Build_an_automated_eval_loop_for_a_health_agent_2.pdf`
- `Behaviour_Spec_2.pdf`
- `design.md` and `build.md`
- runner, storage, normalisation, checks, judge, simulated-user, aggregation, CLI, reporting, tests, scenarios, personas, rubrics, and operational files

Commands run:

- `uv run pytest -q`: **118 passed**
- `uv run qeval validate`: **passed; 77 scenarios, 5 personas, 22 rubrics, 56 rules**
- `uv run qeval coverage`: **passed**, but exposed material rule-level gaps described below
- `uv run ruff check . && uv run mypy qeval`: **failed at Ruff**, so mypy did not run in the combined command

No new live API run was started during this review. Existing `runs/` artifacts contain logs but no completed report or portable export.

## P0 — blockers to trustworthy results

### 1. Re-grading can use expectations that were not present when the run happened

**Evidence:** `qeval/grading/grade.py:97-104`

`norm_runs_for()` loads the current YAML from disk. When its hash differs from the hash saved with the run, it logs the mismatch and continues grading with the current file.

**Impact:** Editing an expectation after a run can turn the exact same stored agent output from pass to fail, or vice versa. This conflicts with the stated principle that grading reads saved evidence and makes historical results non-reproducible.

**Required fix:**

- Persist the complete scenario and referenced persona snapshot with each scenario run.
- Grade against that snapshot.
- Until snapshots exist, refuse to grade a hash mismatch instead of silently using the new file.
- Add a regression test: run fixture, edit YAML, re-grade, and prove the original result is unchanged or grading is rejected.

### 2. Flagged safety checks can silently disappear from scoring

**Evidence:**

- `qeval/checks/global_checks.py:201-231` and `291-325`
- `qeval/judge/dispatch.py:61-84`
- `qeval/analysis/aggregate.py:13`, `83-88`, `125-128`
- `qeval/report/build.py:227-234`

S4, T5, and M2 can emit `flag`. A flag is intentionally not counted until a judge confirms it. If the key is missing, the judge budget is exhausted, or a judge call fails, the resulting `flag`/`error` contributes neither a pass nor a fail. The report records judge errors, but the CI gate ignores them.

**Impact:** The harness can report a clean or improved score while prescription advice or invented memory facts were never resolved. This is especially dangerous for critical S4.

**Required fix:**

- Introduce an explicit `unresolved` verdict for a flag without a successful confirmation.
- Fail the harness gate when any critical flag remains unresolved or when judge error rate exceeds a small threshold.
- Report unresolved counts in every rule and area denominator.
- Add tests for no key, token-budget exhaustion, transient judge failure, and parse failure.

### 3. Resume is unsafe after interrupted, failed, or changed jobs

**Evidence:**

- `qeval/runner/planner.py:31-41`
- `qeval/store/repo.py:72-81`, `91-109`
- `qeval/store/schema.py:49`
- `qeval/runner/executor.py:52-66`

Resume skips only `valid` rows with the same scenario hash. New jobs always start with `attempt=1`, even though `max_attempt()` exists. An existing `running`, `invalid`, `interrupted`, or old-hash row for the same scenario and repetition collides with the unique constraint on `(run_id, scenario_id, rep, attempt)`.

**Impact:** A normal Ctrl-C, failed attempt, or scenario edit can make `--resume` fail with a database integrity error instead of continuing.

**Required fix:**

- On resume, set the next attempt to `repo.max_attempt(...) + 1`.
- Decide and document whether terminal `scenario_error` jobs are retried.
- Mark jobs left in `running` as interrupted when reopening a run.
- Add interrupt/resume, invalid/resume, and changed-scenario/resume tests.

### 4. The required live findings deliverable is missing

**Evidence:**

- The brief requires real failures, rule IDs, frequency across runs, examples, hypotheses, and planted-defect evidence.
- `runs/` contains only `full_k1.out`, `run.log`, and `grade.log`.
- There is no completed `report.html`, `results.json`, portable export, or findings document.
- `runs/full_k1.out` only shows that 87 jobs started; it does not show completion.

**Impact:** Even a technically excellent harness does not satisfy the assignment unless it produces evidence-backed findings against the live agent.

**Required fix:**

- After fixing measurement integrity, run the full suite at `k >= 5`.
- Re-run likely defects at `k=10` to estimate frequency.
- Ship a concise findings document with rule, rate, Wilson interval, failing scenarios, one transcript/trace/state excerpt, and a plausible cause.
- Include a representative report and portable export, with secrets removed.

## P1 — major correctness and spec-coverage gaps

### 5. S2 and T3 do not have reliable rule-level measurement

**Evidence:**

- `rules.yaml:16` has no deterministic check or rubric for S2.
- `rules.yaml:37` has no check or rubric for T3.
- Coverage output shows 15 S2 scenarios but no registered check/rubric, and one T3 scenario with no registered check/rubric.
- `scenarios/safety/s_chest_pain_adult.yaml:3-14` tags T3 but only asserts S2/W2 and invokes the S1 judge.

**Impact:** Scenario counts give an inflated impression of coverage. New S2 scenarios can be tagged without measuring same-turn emergency assessment, and T3 is currently named but not tested.

**Required fix:**

- Add a reusable S2 check for emergency-trigger scenarios: same-turn `assessment`, `urgent`, `escalate_to_doctor=true`, and no prior follow-up/clarify.
- Add a T3 rubric or explicit scenario assertion that emergency signs are handled before routine history questions.
- Make validation warn or fail when a scenario names a rule but contributes no expectation, rubric, or applicable registered check.

### 6. The S1 judge is auto-triggered by the agent's answer instead of the user's emergency

**Evidence:** `rubrics/S1_emergency_first.yaml:6`

`auto_when` is `reply.urgency == 'urgent'`.

**Impact:** If the agent under-triages an emergency as `see_doctor_soon`, sends a follow-up, or treats it as info, the S1 judge does not run automatically—the exact failures it should detect. Listed rubrics cover some scenarios but do not make the global safety claim true.

**Required fix:**

- Trigger S1 from emergency evidence in the current user message and recent conversation, not from the agent's urgency.
- Keep explicit S1 rubrics on all known emergency scenarios as a second line of defence.
- Mutation-test missing, delayed, and buried emergency instructions.

### 7. Persona grading converts important misses into `n/a`

**Evidence:** `qeval/grading/persona_metrics.py:65-84`, `109-124`

- `T4_must_ask` becomes `n/a` when a late-reveal persona is assessed early.
- `T7_assess_after_red_flag` becomes `n/a` when assessment happens before the late reveal is sent.

**Impact:** An agent that assesses too early without collecting required information can avoid both checks. This biases persona results upward.

**Required fix:**

- Missed required topics should fail unless the persona explicitly marks them optional.
- Early assessment should not excuse T7. Either fail the conversation for preventing the planned reveal or separate “early-assessment quality” from “response after reveal.”
- Add persona tests for assessment on turns 1, 2, and after the reveal.

### 8. Multi-account expressions can read another household's turns

**Evidence:** `qeval/checks/expr/functions.py:188-195`

`same_account_turns` is built from every turn in `run.turns` without filtering by `account_key`.

**Impact:** `turn[0]` or `turns` in a multi-account isolation scenario can reference the other account, producing false passes or failures.

**Required fix:**

- Filter turns by `t.account_key == account_key`.
- If cross-account access is needed, expose it through an explicit API such as `account("B").turns`.
- Add a two-account expression test with interleaved turns.

### 9. S7 infant-fever assertions permit indefinite follow-up

**Evidence:** `scenarios/safety/s_infant_fever_7w.yaml:13-20`

Each turn passes S7 whenever `reply.kind != 'assessment'`; only an eventual assessment is required. The README also defines “usually urgent” as passing at `see_doctor_soon` or above.

**Impact:** The agent may continue asking questions despite a seven-week-old infant with fever, then eventually return `see_doctor_soon`, and the scenario passes. This weakens a critical rule and can hide delayed escalation.

**Required fix:**

- Set a strict maximum turn by which assessment is required.
- Require `urgent` for the seven-week scenario if that is the documented interpretation.
- Use the five-month twin to verify the threshold meaningfully rather than allowing the same urgency behavior in both.

### 10. Aggregation and gate logic do not use one consistent authoritative attempt

**Evidence:** `qeval/analysis/aggregate.py:98-134`, `226-235`

Summary job counts select the latest attempt per `(scenario, rep)`, but check statistics accept every `valid` scenario-run ID. The normal runner usually has one valid attempt, but imports, manual repair, or future retry changes can create multiple valid rows.

**Impact:** The headline job count can say `k=5` while a check denominator is larger, distorting failure rates and Wilson intervals.

**Required fix:**

- Select the authoritative/latest valid attempt once, before grading and aggregation.
- Use that same set for counts, checks, report cards, latency, and usage attribution.
- Enforce an invariant or test that each `(scenario, rep)` contributes at most one scored run.

## P2 — reliability and operational gaps

### 11. Simulated-user token use is not governed by the advertised budget

**Evidence:**

- `qeval/runner/context.py:31-42`
- `qeval/simuser/simuser.py:121-131`
- `qeval/runner/persona_executor.py:89-94`

The run budget has `judge_tokens`, but simulated-user usage is only written to the usage ledger. It never updates the pool budget.

**Impact:** Persona runs can exceed the Anthropic budget even while `can_start()` continues returning true.

**Fix:** Track simulated-user input/output/cache-write tokens atomically in the run budget and stop starting persona jobs before the configured cap.

### 12. Judge cache invalidation depends on manually bumping rubric versions

**Evidence:** `qeval/judge/client.py:79-85`

The cache key includes model, rubric ID, rubric version, and inputs, but not the rubric content, system prompt, effort, or input-builder/check version.

**Impact:** Editing a rubric without remembering to bump its version can reuse stale verdicts indefinitely.

**Fix:** Include hashes of the complete rubric, judge system prompt, relevant input builder, model, and effort in the cache key. Retain the human-readable version for reporting.

### 13. `make down` can delete Postgres even when the dump fails

**Evidence:** `Makefile:16-18`

The leading `-` ignores dump failure, followed by `docker compose down -v`.

**Impact:** A failed dump is followed by deletion of the database volume, which defeats the documented durability guarantee.

**Fix:** Make dump success a prerequisite for deleting the volume. Provide an explicit `make down-force` for intentional data loss.

### 14. The report gate ignores judge and many harness failures

**Evidence:** `qeval/report/build.py:227-234`

Exit code 2 is based on non-judge check errors and more than 10% non-valid latest jobs. Judge errors are excluded, and a small number of `harness_error`/`scenario_error` runs may still permit exit 0.

**Impact:** CI can pass with unavailable semantic grading or broken critical scenarios.

**Fix:** Gate separately on:

- any unresolved critical check;
- judge error rate;
- any critical scenario not producing a valid run;
- harness/scenario error counts;
- required rule coverage.

### 15. The portable export lacks an explicit format/schema version

**Evidence:** `qeval/store/export.py:27-40`; schema version exists in `qeval/store/schema.py:8`

**Impact:** An export created by one harness version can fail opaquely or import incorrectly after a schema change.

**Fix:** Add `export_format_version`, `schema_version`, and harness/check versions to the header and validate them before inserting any rows.

### 16. Leaky simulated-user runs skip fidelity diagnostics

**Evidence:** `qeval/grading/grade.py:158-176`

Leak detection returns early, so `SIM_fidelity` is never run for those conversations.

**Impact:** Excluding the agent score is correct, but the report cannot distinguish a direct leak from broader persona drift, contrary to the planned simulator-health reporting.

**Fix:** Continue simulator-only checks after detecting a leak, while excluding all agent checks from aggregate scoring.

## P3 — delivery and engineering completeness

### 17. The repository is not in a clean, reviewable Git state

`git status --short` reports every project file as untracked.

**Impact:** There is no reviewable history or stable submission commit, and `harness_sha` cannot meaningfully identify this implementation.

**Fix:** Initialize the intended repository state, verify `.env` is ignored, commit the implementation, tag the exact commit used for the final live run, and include that SHA in the report.

### 18. Lint currently fails

**Evidence:** `tests/unit/test_e2e_fake.py:70`

Ruff reports E731 for assigning a lambda to `strip`. The inline `# noqa: E731` is placed after the multi-line expression and does not suppress the assignment line.

**Fix:** Replace the lambda with a small local function, then run the complete `make lint` command so mypy also executes.

### 19. Documented smoke, CI, and calibration paths are incomplete

**Evidence:**

- `Makefile:36-37` targets `tests/live`, which does not exist.
- No `.github/workflows/` files exist.
- `qeval calibrate` defaults to `labels/labels.yaml`, but no `labels/` directory exists.

**Impact:** Integration drift and judge quality are not checked automatically. The calibration command is present but cannot demonstrate agreement.

**Fix:** Add two opt-in live smoke tests, a CI workflow for validate/lint/unit tests, and a small hand-labelled calibration set with agreement results.

### 20. README is not yet the required submission write-up

The README explains usage well, but the brief also asks for a write-up of no more than 20 lines and disclosure of how AI coding assistants were used.

**Fix:** Add a clearly delimited submission summary of at most 20 lines, including assumptions, design choices, AI-assistant use, and links to the final findings/report.

## Scenario and rubric improvements after blockers

These are valuable but should follow the measurement-integrity fixes:

- Strengthen `W10_facts_on_tagged_members`; it checks facts created on the current turn against reply tags but does not prove cross-turn continuity.
- Strengthen `M6` scenarios so they prove prior facts affected the answer, rather than accepting a broad `get_subject` call.
- Make E4 assert that the old episode is closed as well as that a new ID is used.
- Add explicit S8 judging to the pushback persona and emergency-inside-general-question scenario.
- Avoid duplicate R9 scoring on persona conversations (`R9_tone` per turn and `R9_conversation_tone` for the same run).
- Add mutation tests for the most important judge rubrics: delayed emergency advice, definite reassurance, medicine dose, prescription recommendation, and invented stored fact.
- Add more contrast twins for high-risk emergency and identity-resolution scenarios, not simply more unrelated scenarios.

## What is already strong

- Clear separation of running, grading, and reporting.
- Fresh account per independent repetition and controlled cross-account parallelism.
- Correct policy of not retrying an ambiguous timed-out send on the same account.
- Data-driven scenarios with schema validation and a safe expression language rather than Python `eval`.
- Good normalised evidence model across reply, trace, state, and diffs.
- Structured LLM outputs, judge caching, and prompt caching.
- Strong deterministic coverage for reply shape, medicine doses, time normalisation, fact categories, episode/member consistency, and household isolation.
- Useful failure cards with transcript, trace, state, frequency, and Wilson intervals.
- SQLite/Postgres portability and a portable export concept.
- Broad scenario coverage across all Behaviour Spec areas.
- Fast deterministic test suite with 118 passing tests.

## Recommended implementation order

1. Snapshot scenarios/personas and reject unsafe re-grading.
2. Make unresolved flags and judge failures visible and gate-breaking.
3. Fix resume attempt selection and add resume tests.
4. Fix persona `n/a` logic and per-account turn indexing.
5. Add reliable S2/T3 checks and emergency-driven S1 dispatch.
6. Strengthen the critical S7 scenario.
7. Fix aggregation's authoritative-attempt selection.
8. Fix dump safety, token budgeting, cache invalidation, export versioning, and lint.
9. Add CI, live smoke tests, and judge calibration fixtures.
10. Run the live suite, confirm defects at higher `k`, and ship the findings/report/write-up.

## Submission readiness

Current status: **not ready to submit**.

Minimum bar before submission:

- All P0 findings fixed and regression-tested.
- P1 findings 5–10 fixed or explicitly justified.
- `make lint`, `make test`, and `qeval validate` all pass.
- One completed full live run with repetitions and a generated report.
- Evidence-backed findings, including planted defects and observed frequency.
- A stable committed SHA, clean secret handling, and the requested ≤20-line write-up.
