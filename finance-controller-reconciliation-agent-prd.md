# PRD: Reconciliation Agent — Finance Controller

**Status:** Draft v1
**Owner:** Nitin Nautiyal
**Module:** Finance Controller → Reconciliation
**Backend:** Built per Engineering Spec v1 (14/14 tests passing)

---

## 1. Context

The reconciliation engine already exists. It matches CDMS invoices and credit notes against Razorpay webhooks and settlements, classifies discrepancies, and generates LLM remarks using stored memory context (tax rules, discount policies, dispute records). This PRD scopes the **product surface** on top of that engine — the screen where a finance user triggers runs, reads results, and resolves exceptions — and defines how it sits inside the existing Finance Controller.

Finance Controller today has five modules in the left nav: Home, Payments, Payouts, Banking, Reconciliation, Reports, Developers, Settings. Reconciliation becomes the sixth. This PRD covers only the Reconciliation module.

---

## 2. Problem Statement

Finance teams reconcile CDMS invoices against Razorpay settlements manually today — pulling exports, matching line items in spreadsheets, and chasing discrepancies by email. This is weekly, repetitive, and error-prone at volume (2,100+ records, ₹40M+ per cycle in the reference data). The Reconciliation Agent automates the matching and drafts an explanation for every mismatch. What's missing is a product surface that lets a finance user trigger a run, trust the output, and act on exceptions without leaving the platform.

---

## 3. Goals

| Goal | Metric |
|---|---|
| Replace manual matching with agent-run reconciliation | 100% of weekly cycles run through the agent, zero spreadsheet exports |
| Cut time to close a reconciliation cycle | From multi-day manual process to under 1 hour end-to-end |
| Make every exception actionable without external tools | 0 exceptions require CDMS/Razorpay console access to diagnose |
| Build trust in agent output | Human escalation rate stays under 5% of total exceptions |
| Give finance ops visibility into recurring problems | Pattern/insight view surfaces repeat offenders (same customer, same error type) every cycle |

---

## 4. Non-Goals

| Non-goal | Why |
|---|---|
| Writing corrections back into CDMS automatically | Source-of-truth risk — v1 is read-only against CDMS, resolution stays a human action |
| General ledger / full P&L statement generation | Reconciliation surfaces net discrepancy, not a complete financial statement — a full P&L is a separate module |
| Multi-currency or multi-entity reconciliation | Current schema and demo data are single-currency (INR), single-entity |
| Customer-facing dispute resolution | Disputes are logged for context (memory_context) but resolving them with the customer happens outside this module |
| Real-time (sub-minute) reconciliation | Runs are cycle-based (weekly), not a live ledger |

---

## 5. Users

| Persona | Role | What they need from this screen |
|---|---|---|
| Finance Ops Analyst | Runs cycles, works exceptions daily | Fast triage: what's broken, why, what to do |
| Finance Controller / Manager | Owns the close, reviews escalations | Cycle-level confidence: match rate, net discrepancy, trend |
| Finance Ops Admin | Configures the agent | Tolerance, tax rate, and memory-rule (policy/discount/dispute) management |

---

## 6. User Flow

The agent is triggered one of two ways, and both land on the same Reconciliation screen.

**Path A — No input needed (automatic)**
The CRM/CDMS sends invoice and credit note data via webhook on its own schedule. Razorpay payment and settlement events arrive via the existing `/webhooks/razorpay` listener. The user opens Reconciliation and reads the latest cycle's insights directly — no action required to see fresh data.

**Path B — Input needed (manual)**
The user uploads Collection/Credit Note data directly into the platform (for CDMS sources not yet webhook-connected), then clicks **Run Reconciliation** to trigger the agent on demand. This is also the path for re-running a cycle after fixing an upstream data issue.

Both paths write into the same `reconciliation_runs` and `exceptions` tables, so the screen behaves identically regardless of trigger source. The one difference: manually-triggered runs show "Run Reconciliation" as an active CTA state (spinner → complete), while webhook-triggered runs update the Cycle History dropdown silently and the user is notified via the "Ready to run" / cycle timestamp badge.

---

## 7. Functional Requirements

### 7.1 Entry & Trigger Layer

| Priority | Requirement | Acceptance Criteria |
|---|---|---|
| P0 | Manual upload of Collection/Credit Note data | Given a user on the Reconciliation screen, when they select "Upload Data," then they can attach a CSV/XLSX and the system validates column headers against the CDMS schema before ingest |
| P0 | Run Reconciliation CTA | Given valid seeded/uploaded data exists, when the user clicks "Run Reconciliation," then a new row is created in `reconciliation_runs` and the Agent Stats header updates on completion |
| P0 | Webhook auto-trigger, no user action | Given CDMS or Razorpay sends new data via webhook, when a scheduled cycle boundary is reached, then a run executes automatically and appears in Cycle History without the user clicking anything |
| P1 | Run status feedback | Given a run is in progress, when the user is on the screen, then the "Ready to run" badge changes to "Running…" and the CTA shows a spinner until the run completes or fails |
| P1 | Failed run handling | Given a run fails (bad data, webhook signature mismatch, timeout), when this happens, then the badge shows "Run Failed" with a reason and a retry action |

### 7.2 Agent Stats Header

| Priority | Requirement | Acceptance Criteria |
|---|---|---|
| P0 | Show Evals, Runs, Processed, Tokens for the latest cycle | All four stat cards populate from the most recent `reconciliation_runs` row; "Last run" timestamp matches that row |
| P0 | Human Escalations count | Given exceptions exist that were manually escalated (not agent-resolved), when the header renders, then the "0 Human Escalations" value reflects the current cycle's actual count |
| P1 | Sparkline trend on Processed and Tokens | Sparklines show the last 6 cycles, not just the current one, so a controller can spot volume/cost trend at a glance |

### 7.3 Reconciliation Analysis Cards

| Priority | Requirement | Acceptance Criteria |
|---|---|---|
| P0 | Match Rate donut with numeric readout | Match Rate % = (matched records / total records) for the selected cycle; donut colors distinguish matched vs. exception segments |
| P0 | Total Records (count + ₹ volume) | Pulls directly from `reconciliation_runs` totals for the selected cycle |
| P0 | Open Cases (Resolved vs. Manual/Unassigned) | "Res" count = exceptions closed by agent remark acceptance; "M.U." count = exceptions requiring human action |
| P0 | Net Discrepancy with "Variance Requiring Action" badge | Sum of all unresolved `delta` values across open exceptions; badge only shows when net discrepancy > 0 |
| P1 | Cycle History dropdown drives all four cards | Selecting a past cycle from "Cycle History" re-renders all four cards and the exception table for that cycle, read-only |

### 7.4 Tabs and Data Views

| Priority | Requirement | Acceptance Criteria |
|---|---|---|
| P0 | Recon Report tab (default) | Shows the unified exception table across all sources |
| P1 | CRM Collection tab | Shows raw CDMS orders/credit notes ingested for the cycle, independent of match status |
| P1 | Razorpay tab | Shows raw settlement/webhook events ingested for the cycle, independent of match status |
| P2 | Cross-tab linking | Clicking a record in the Recon Report tab jumps to its source row in CRM Collection or Razorpay tab |

### 7.5 Exception Table and Filters

| Priority | Requirement | Acceptance Criteria |
|---|---|---|
| P0 | Table columns: Sno., Customer Name, Invoice/Pay ID, Error type, Status, Delta (₹), Remarks, Action | Matches the five classified exception types from the engine: Underpayment, Overpayment, Tax Mismatch, Duplicate CN, Orphan Payment |
| P0 | Filter chips by error type | Selecting "Underpayment" (etc.) filters the table client-side without a new query; "All" clears the filter |
| P0 | Search by invoice, customer, or ID | Matches the global search bar behavior already in the header |
| P0 | Show: Open Only / All toggle | "Open Only" hides resolved exceptions; default state is "Open Only" |
| P0 | Empty state | When a cycle has zero data, show "No reconciliation data found" with a CTA to Seed Demo Data or Upload Data |
| P1 | Row-level Action menu | Actions: Accept agent remark (marks resolved), Escalate to human, Add note, View full audit trail |
| P1 | Disputed Invoice as a sixth exception type | Demo output includes a "Disputed Invoice" remark tied to `memory_context` dispute records — this needs a formal sixth classification, not just a remark string, so it can be filtered like the other five |

### 7.6 Insights and Reporting

| Priority | Requirement | Acceptance Criteria |
|---|---|---|
| P0 | LLM remark shown per exception row | Remark pulled from `memory_insights`, generated in the batched LLM pass, referencing the specific memory_context rule it matched (discount policy, dispute, tax change) |
| P1 | Pattern/insight summary panel | Surfaces repeat offenders across cycles — e.g., same customer with 3+ Duplicate CN exceptions in the last 4 cycles — pulled from `memory_insights` aggregated over time |
| P1 | P&L-adjacent export | A "Net Discrepancy" export (CSV/PDF) that finance can attach to the cycle close packet — not a full P&L, just the reconciliation variance summary |
| P2 | Trend view of Net Discrepancy over cycles | Line chart of Net Discrepancy across the last N cycles, to show whether the underlying process is improving |

### 7.7 Agent Configuration

| Priority | Requirement | Acceptance Criteria |
|---|---|---|
| P0 | Settings panel for TOLERANCE and STANDARD_TAX_RATE | Admin can view and edit these two config values; changes apply to the next run, not retroactively |
| P1 | Memory context editor | Admin can add/edit/remove entries in `memory_context` (discount policies, dispute records, tax changes) that drive remark generation |
| P1 | Webhook connection status | Settings shows whether the CDMS webhook and Razorpay webhook are live, with last-received timestamp for each |
| P2 | Configurable escalation rules | Admin defines a delta threshold above which an exception auto-escalates to human review instead of waiting for manual triage |

---

## 8. Success Metrics

| Type | Metric | Target |
|---|---|---|
| Leading | % of cycles triggered via webhook (no manual click) | 70%+ within 4 weeks of launch |
| Leading | Time from "Run Reconciliation" click to results rendered | Under 3 minutes for a 2,000+ record cycle |
| Leading | % of exceptions resolved via Accept/Escalate action in-app | 90%+ (vs. resolved outside the platform) |
| Lagging | Human escalation rate | Under 5% of total exceptions per cycle |
| Lagging | Reduction in manual reconciliation hours per cycle | 80%+ reduction vs. pre-agent baseline |
| Lagging | Net Discrepancy trend | Declining quarter over quarter as pattern insights get acted on |

---

## 9. Open Questions

| Question | Owner |
|---|---|
| Is "Settlements" the correct nav placement, or does this stay under "Reconciliation" as shown in the current build? | Design |
| What's the retention window for `reconciliation_runs` history — is Cycle History unlimited or capped? | Engineering |
| Should "Disputed Invoice" be a formal sixth exception type in the schema, or remain a remark-level distinction under an existing type? | Engineering |
| Who has permission to edit `memory_context` (tax/discount/dispute rules) — is this role-gated? | Engineering / Security |
| Does the manual upload path need CDMS-side validation before ingest, or does the agent handle malformed rows gracefully? | Engineering |

---

## 10. Timeline / Phasing

| Phase | Scope |
|---|---|
| V1 (current) | Backend engine, webhook ingestion, batched LLM remarks, Review UI as shown in the reference screen — ship as-is with the P0 items above closed |
| V1.1 | Manual upload flow, Agent Configuration settings panel, sixth exception type for Disputed Invoice |
| V1.2 | Pattern/insight summary panel, Net Discrepancy export, cross-tab linking |
| V2 | Trend view over cycles, configurable escalation rules, multi-entity support |

---

## Appendix: Exception Taxonomy (from Engineering Spec v1)

| Error Type | Trigger Condition |
|---|---|
| Underpayment / Pending Collection | `order.total_amount - cn_sum - stl_sum > 0`, beyond tolerance |
| Overpayment | Settlement total exceeds invoice net of credit notes |
| Tax Mismatch | Applied tax rate diverges from `STANDARD_TAX_RATE` (0.18) |
| Duplicate CN | More than one credit note applied against the same invoice |
| Orphan Payment | Razorpay settlement received with no matching invoice tag |
| Disputed Invoice (proposed) | Delta traces to an active `memory_context` dispute record |
