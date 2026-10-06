/* Operator console (UI-001..003).
 *
 * Deliberately vanilla: no framework, no CDN, no build step. During an incident the console
 * must render from the same origin as the API, and it must be auditable by reading one file.
 *
 * The UI is a *view*: it never decides anything. Approvals are explicit buttons that send the
 * payload hash shown to the operator, so a stale tab cannot approve a changed payload.
 */
"use strict";

const API = "/api/v1";
const state = { incidents: [], selected: null, run: null, approvals: [] };

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function banner(message, kind) {
  const node = $("banner");
  node.textContent = message;
  node.className = `banner ${kind || ""}`;
  node.classList.remove("hidden");
  if (!kind) setTimeout(() => node.classList.add("hidden"), 4000);
}

async function api(path, options) {
  const response = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const correlation = response.headers.get("X-Request-Id");
  if (correlation) $("correlation").textContent = `request ${correlation.slice(0, 8)}`;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body?.error?.message || `HTTP ${response.status}`;
    throw new Error(`${message}${body?.error?.code ? ` (${body.error.code})` : ""}`);
  }
  return body;
}

function tag(text, kind) {
  return el("span", `tag ${kind || ""}`, text);
}

/* ------------------------------------------------------------------ health */

async function loadHealth() {
  try {
    const ready = await fetch("/ready").then((r) => r.json());
    const dot = $("health-dot");
    dot.className = `dot ${ready.status === "ok" ? "ok" : "degraded"}`;
    $("mode-badge").textContent = `mode: ${ready.simulated ? "sandbox (simulated)" : "live"}`;
    $("version-badge").textContent = `v${ready.version}`;
  } catch (error) {
    $("health-dot").className = "dot down";
    banner(`Readiness check failed: ${error.message}`, "error");
  }
}

/* --------------------------------------------------------------- incidents */

async function loadIncidents() {
  const data = await api("/incidents?limit=100");
  state.incidents = data.incidents;
  const counts = data.counts || {};
  $("incident-counts").textContent = Object.entries(counts)
    .map(([key, value]) => `${key.toLowerCase()} ${value}`)
    .join(" · ");

  const list = $("incident-list");
  list.replaceChildren();
  if (!data.incidents.length) {
    list.append(el("li", "muted", "No incidents yet. Trigger a scenario above."));
    return;
  }
  for (const incident of data.incidents) {
    const card = el("li", `card sev-${incident.severity}`);
    if (state.selected === incident.id) card.classList.add("active");
    card.append(el("span", "card-title", incident.title));
    const sub = el("span", "card-sub");
    sub.append(el("span", null, incident.severity));
    sub.append(el("span", null, incident.status));
    sub.append(el("span", null, `${incident.evidence_count} evidence`));
    if (incident.verification_outcome) {
      sub.append(tag(`verification ${incident.verification_outcome}`,
        incident.verification_outcome === "SUCCESS" ? "ok"
          : incident.verification_outcome === "FAILED" ? "bad" : "warn"));
    }
    if (incident.simulated) sub.append(tag("simulated", "warn"));
    card.append(sub);
    card.addEventListener("click", () => selectIncident(incident.id));
    list.append(card);
  }
}

async function selectIncident(incidentId) {
  state.selected = incidentId;
  await loadIncidents();
  await loadRun(incidentId);
}

/* --------------------------------------------------------------------- run */

function renderRun(snapshot) {
  const container = $("run-detail");
  container.classList.remove("empty");
  container.replaceChildren();
  if (!snapshot) {
    container.classList.add("empty");
    container.append(el("span", null, "No run has been started for this incident."));
    $("run-state").textContent = "";
    return;
  }
  const run = snapshot.run || snapshot;
  $("run-state").textContent = `${run.state}${run.version ? ` · v${run.version}` : ""}`;

  const head = el("div", "row");
  head.append(tag(run.state, run.state === "RESOLVED" ? "ok" : run.state === "ESCALATED" ? "bad" : "warn"));
  if (run.reasoner) head.append(tag(`reasoner: ${run.reasoner}`));
  if (run.degraded_reason) head.append(tag(`degraded: ${run.degraded_reason}`, "warn"));
  if (snapshot.requires_human) head.append(tag("human decision required", "warn"));
  container.append(head);

  if (snapshot.note) container.append(el("p", "muted", snapshot.note));

  const timeline = el("ul", "timeline");
  for (const item of snapshot.transitions || []) {
    timeline.append(
      el("li", null, `${item.from_state} → ${item.to_state} — ${item.reason} (${item.actor})`)
    );
  }
  if (timeline.children.length) {
    container.append(el("h3", "section-title", "State transitions"));
    container.append(timeline);
  }

  const diagnosis = snapshot.diagnosis;
  if (diagnosis) {
    container.append(el("h3", "section-title", "Diagnosis"));
    container.append(el("p", null, diagnosis.hypothesis));
    container.append(
      el("p", "muted",
        `confidence ${Number(diagnosis.confidence).toFixed(2)} · cites ${(diagnosis.evidence_ids || []).length} evidence item(s)`)
    );
    if ((diagnosis.uncertainties || []).length) {
      const ul = el("ul");
      for (const note of diagnosis.uncertainties) ul.append(el("li", "muted", note));
      container.append(el("h3", "section-title", "Stated uncertainties"));
      container.append(ul);
    }
    if ((diagnosis.injection_flags || []).length) {
      container.append(
        tag(`prompt-injection content flagged: ${diagnosis.injection_flags.join(", ")}`, "bad")
      );
    }
  }

  const actions = snapshot.actions || [];
  if (actions.length) {
    container.append(el("h3", "section-title", "Actions"));
    const table = el("table");
    const header = el("tr");
    for (const label of ["tool", "risk", "policy", "status", "approval"]) {
      header.append(el("th", null, label));
    }
    table.append(header);
    for (const action of actions) {
      const row = el("tr");
      row.append(el("td", null, action.tool_name));
      row.append(el("td", null, action.risk));
      row.append(el("td", null, action.policy_decision));
      row.append(el("td", null, action.status));
      row.append(el("td", null, action.requires_approval ? "required" : "not required"));
      table.append(row);
    }
    container.append(table);
  }

  if (snapshot.verification) {
    container.append(el("h3", "section-title", "Verification (independent of execution)"));
    container.append(
      tag(snapshot.verification.outcome, snapshot.verification.outcome === "SUCCESS" ? "ok"
        : snapshot.verification.outcome === "FAILED" ? "bad" : "warn")
    );
    container.append(el("p", null, snapshot.verification.reason));
    const table = el("table");
    for (const check of snapshot.verification.checks || []) {
      const row = el("tr");
      row.append(el("td", null, check.check));
      row.append(el("td", null, check.outcome));
      row.append(el("td", null, check.reason));
      table.append(row);
    }
    container.append(table);
  }
}

async function loadRun(incidentId) {
  const detail = await api(`/incidents/${incidentId}`);
  const incident = detail.incident;
  const container = $("run-detail");
  container.replaceChildren();

  if (!incident.agent_run_id) {
    $("run-state").textContent = "";
    container.classList.add("empty");
    container.append(el("span", null, "No run yet — start one to investigate this incident."));
    const button = el("button", "primary", "Run agent");
    button.addEventListener("click", () => startRun(incidentId));
    container.append(button);
    return;
  }

  try {
    const snapshot = await api(`/agents/runs/${incident.agent_run_id}`);
    state.run = snapshot;
    renderRun({
      run: snapshot.run,
      transitions: snapshot.transitions,
      diagnosis: snapshot.run.diagnosis,
      actions: (snapshot.tool_invocations || []).map((item) => ({
        tool_name: item.tool_name,
        risk: item.risk,
        policy_decision: item.outcome,
        status: item.outcome,
        requires_approval: false,
      })),
      verification: null,
      requires_human: snapshot.run.state === "WAITING_APPROVAL",
      note: snapshot.run.failure_reason || "",
    });
  } catch (error) {
    container.classList.add("empty");
    container.append(el("span", null, `Could not load run: ${error.message}`));
  }

  const evidence = await api(`/incidents/${incidentId}/evidence`).catch(() => ({ evidence: [] }));
  if (evidence.evidence.length) {
    container.append(
      el("h3", "section-title", "Evidence (sanitised, ranked, simulated labels preserved)")
    );
    const list = el("ul", "list");
    for (const item of evidence.evidence) {
      const card = el("li", "card");
      card.append(el("span", "card-title", `${item.source}/${item.kind} · ${item.id}`));
      card.append(el("span", "card-sub", item.summary));
      if ((item.injections || []).length) {
        card.append(tag(`injection: ${item.injections.join(", ")}`, "bad"));
      }
      list.append(card);
    }
    container.append(list);
  }
}

async function startRun(incidentId) {
  try {
    const snapshot = await api("/agents/runs", {
      method: "POST",
      body: JSON.stringify({ incident_id: incidentId }),
    });
    banner(`Run advanced to ${snapshot.run.state}.`, "ok");
    renderRun(snapshot);
    await loadApprovals();
    await loadIncidents();
  } catch (error) {
    banner(`Run failed: ${error.message}`, "error");
  }
}

/* --------------------------------------------------------------- approvals */

async function loadApprovals() {
  const data = await api("/approvals?pending_only=true");
  state.approvals = data.approvals;
  const list = $("approval-list");
  list.replaceChildren();
  if (!data.approvals.length) {
    list.append(el("li", "muted", "No approvals are waiting."));
    return;
  }
  for (const approval of data.approvals) {
    const card = el("li", "card");
    card.append(
      el("span", "card-title", `${approval.tool_name} · ${approval.risk} risk`)
    );
    card.append(
      el("span", "card-sub", `incident ${approval.incident_id} · hash ${approval.payload_hash.slice(0, 12)}… · expires ${approval.expires_at}`)
    );
    const row = el("div", "row");
    const approve = el("button", "primary", "Approve & execute");
    const reject = el("button", "danger", "Reject");
    const escalate = el("button", "secondary", "Escalate to a human");
    approve.addEventListener("click", () => decide(approval, "APPROVED"));
    reject.addEventListener("click", () => decide(approval, "REJECTED"));
    // Escalation is a first-class decision, not a failure to decide: it hands the incident to a
    // human with the payload hash intact.
    escalate.addEventListener("click", () => decide(approval, "ESCALATED"));
    row.append(approve, reject, escalate);
    card.append(row);
    list.append(card);
  }
}

async function decide(approval, decision) {
  try {
    await api(`/approvals/${approval.id}/decision`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        payload_hash: approval.payload_hash,
        reason:
          decision === "APPROVED"
            ? "approved from console"
            : decision === "ESCALATED"
              ? "escalated from console"
              : "rejected from console",
      }),
    });
    if (decision === "APPROVED") {
      await api(`/agents/runs/${state.run?.run?.id}/resume`, { method: "POST" }).catch(async () => {
        const incidentId = approval.incident_id;
        await api("/agents/runs", {
          method: "POST",
          body: JSON.stringify({ incident_id: incidentId }),
        });
      });
    }
    banner(`Approval ${decision.toLowerCase()}.`, "ok");
    await loadApprovals();
    await loadIncidents();
    if (state.selected) await loadRun(state.selected);
  } catch (error) {
    banner(`Decision failed: ${error.message}`, "error");
  }
}

/* --------------------------------------------------------------- scenarios */

async function runScenario(scenario) {
  if (
    !window.confirm(
      `Inject scenario ${scenario}?\n\nThis resets the operated-system SIMULATOR and takes no real ` +
        `action. Every payload stays labelled "simulated".`
    )
  ) {
    return;
  }
  try {
    const result = await api("/detection/simulate", {
      method: "POST",
      body: JSON.stringify({
        scenario,
        orchestrate: $("orchestrate").checked,
        reset: true,
      }),
    });
    banner(
      `Scenario ${scenario}: ${result.incidents_created.length} incident(s) created, ` +
        `${result.incidents_deduplicated.length} deduplicated.`,
      "ok"
    );
    await refreshAll();
  } catch (error) {
    banner(`Scenario failed: ${error.message}`, "error");
  }
}

async function refreshAll() {
  await loadHealth();
  await loadIncidents();
  await loadApprovals();
}

document.querySelectorAll("[data-scenario]").forEach((button) => {
  button.addEventListener("click", () => runScenario(button.dataset.scenario));
});
$("refresh").addEventListener("click", () =>
  refreshAll().catch((error) => banner(error.message, "error"))
);

refreshAll().catch((error) => banner(`Could not load console data: ${error.message}`, "error"));
