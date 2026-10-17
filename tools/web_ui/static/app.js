"use strict";

const selector = document.getElementById("workflow");
const fields = document.getElementById("fields");
const artifacts = document.getElementById("artifacts");
const capabilities = document.getElementById("capabilities");
const values = new Map();
const reportedSelections = new Map();
let definition;
let csrf;
let previewId;
let operationRevision = 0;
let operationBusy = false;
let selectedRun;
let pollTimer;
let pollRevision = 0;
let runRequest = false;
let listingRuns = false;

function invalidatePreview() {
  operationRevision += 1;
  previewId = undefined;
  document.getElementById("start-operation").disabled = true;
  document.getElementById("command-preview").replaceChildren();
}

function paragraph(text, className = "") {
  const element = document.createElement("p");
  element.textContent = text;
  element.className = className;
  return element;
}

function sourceText(source) {
  return `${source.tool}; ${source.location}; contract ${source.contract}; ${source.kind}`;
}

function capabilityText(capability) {
  return `${capability.key}: ${JSON.stringify(capability.value)}; ` +
    `state: ${capability.state}; readiness: ${capability.readiness}; ` +
    `provenance: ${capability.provenance}; verification: ${capability.verification}; ` +
    `default: ${capability.default ? "yes" : "no"}; ` +
    `source: ${sourceText(capability.source)}` +
    (capability.origin === null ? "" : `; origin: ${capability.origin}`) +
    (capability.reason === null ? "" : `; reason: ${capability.reason}`) +
    `; evidence: ${JSON.stringify(capability.evidence)}`;
}

function control(field, value) {
  const input = document.createElement("input");
  input.type = field.selection === "existing-file" ? "file" :
    field.kind === "boolean" ? "checkbox" :
    field.kind === "number" ? "number" : "text";
  input.required = field.required && input.type !== "file";
  if (input.type === "file") {
    input.accept = field.accept;
  } else if (input.type === "checkbox") {
    input.checked = value === true;
  } else {
    input.value = value ?? "";
  }
  if (input.type === "number") {
    for (const attribute of ["minimum", "maximum", "step"]) {
      if (field[attribute] !== null && field[attribute] !== undefined) {
        input.setAttribute(
          attribute === "minimum" ? "min" : attribute === "maximum" ? "max" : "step",
          field[attribute],
        );
      }
    }
    if (field.exclusive_minimum !== null && field.exclusive_minimum !== undefined) {
      input.setAttribute("min", field.exclusive_minimum);
      const validate = () => input.setCustomValidity(
        input.value !== "" && input.valueAsNumber <= field.exclusive_minimum ?
          `Must be greater than ${field.exclusive_minimum}.` : "",
      );
      input.addEventListener("input", validate);
      validate();
    }
  }
  return input;
}

function dependentChoices(field, parentSelection) {
  if (!field.dependency) {
    return { choices: field.capabilities, indexes: field.capabilities.map((_, i) => i) };
  }
  const dependency = field.dependency;
  const binding = parentSelection === undefined ? undefined :
    dependency.bindings.find(item => item.parent_index === parentSelection);
  const indexes = binding ? binding.choices : field.capabilities.map((_, i) => i);
  return {
    choices: indexes.map(index => field.capabilities[index]), indexes,
    state: binding ? binding.state : dependency.state,
    reason: binding ? binding.reason : dependency.reason,
  };
}

function render(workflow) {
  invalidatePreview();
  document.getElementById("execution-actions").hidden = !workflow.operation;
  document.getElementById("preview-operation").disabled =
    !csrf || !definition?.execution_enabled || !workflow.operation;
  document.getElementById("operation-status").textContent = definition?.execution_enabled ?
    "Preview validates local paths, not MIDI/lyric compatibility or resource readiness." :
    "Execution unavailable: supply one explicit local Melisma source/executable at server startup. Unsaved editor changes do not apply.";
  document.getElementById("details").hidden = false;
  document.getElementById("workflow-title").textContent = workflow.label;
  const availability = document.getElementById("availability");
  availability.textContent =
    `${workflow.available ? "Form available" : "Unavailable"}: ${workflow.reason}`;
  availability.className = workflow.available ? "" : "unavailable";
  document.getElementById("readiness").textContent =
    `Workflow runtime readiness: ${workflow.readiness}. ${workflow.readiness_reason}`;
  capabilities.replaceChildren();
  if (!workflow.reports.some(report => report.capabilities.length)) {
    capabilities.append(paragraph(
      "No capability evidence currently supplied for this workflow; readiness is unknown.",
    ));
  }
  for (const report of workflow.reports) {
    const heading = document.createElement("h3");
    heading.textContent = `Supplied report: ${sourceText(report.source)}`;
    capabilities.append(heading);
    if (!report.capabilities.length) {
      capabilities.append(paragraph("Empty report; capability readiness is unknown."));
    }
    for (const capability of report.capabilities) {
      capabilities.append(paragraph(capabilityText(capability), "help"));
    }
  }
  fields.replaceChildren();
  artifacts.replaceChildren();
  fields.hidden = !workflow.available;
  artifacts.hidden = !workflow.available;

  const choiceControls = new Map();
  function updateDependencies() {
    for (const field of workflow.fields) {
      const controls = choiceControls.get(field.key);
      if (!controls) continue;
      const parent = field.dependency ?
        reportedSelections.get(`${workflow.key}:${field.dependency.parent}`) : undefined;
      const result = dependentChoices(field, parent);
      const previous = controls.select.value;
      controls.select.replaceChildren();
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Choose reported text to copy; no validation is performed";
      controls.select.append(placeholder);
      result.choices.forEach((capability, index) => {
        if (capability.value === null) return;
        const option = document.createElement("option");
        option.value = String(result.indexes[index]);
        option.textContent = capabilityText(capability);
        controls.select.append(option);
      });
      controls.select.value = [...controls.select.options].some(item => item.value === previous) ?
        previous : "";
      if (field.dependency) {
        controls.reason.textContent = `Dependency: ${result.state}; ${result.reason}`;
      }
    }
  }

  for (const field of workflow.fields) {
    const key = `${workflow.key}:${field.key}`;
    const executionPath = Boolean(workflow.operation) && field.selection === "existing-file";
    const valueKey = executionPath ? `${key}:host` : key;
    const label = document.createElement("label");
    label.htmlFor = field.key;
    const labelText = executionPath ?
      `${field.key === "midi" ? "MIDI input" : field.label}: absolute local path` : field.label;
    label.append(document.createTextNode(`${labelText} `));
    const indicator = document.createElement("span");
    indicator.className = "indicator";
    indicator.textContent = field.selection === "existing-file" && !executionPath ?
      "(optional filename metadata)" : field.required ? "(required)" : "(optional)";
    label.append(indicator);
    const input = control(
      executionPath ? { ...field, selection: null } : field,
      values.get(valueKey) ?? field.default,
    );
    input.id = field.key;
    input.name = field.key;
    input.addEventListener("input", () => {
      if (input.type === "file") return;
      values.set(valueKey, input.type === "checkbox" ? input.checked : input.value);
      invalidatePreview();
      reportedSelections.delete(key);
      updateDependencies();
    });
    const help = paragraph(executionPath ?
      "Enter an absolute path on the machine running the Melisma web server. " +
      "The browser does not read or upload the file. Preview validates this explicitly entered local path; " +
      "Start uses the same retained path through the trusted Operation. " +
      (field.key === "midi" ?
        "MIDI must be format 0, one track, positive PPQN, with monophonic non-overlapping notes." :
        "Use an existing Melisma lyric file, not free-form song lyrics.") : field.help, "help");
    help.id = `${field.key}-help`;
    input.setAttribute("aria-describedby", help.id);
    fields.append(label, input, help);
    if (field.path_limitation && !executionPath) {
      fields.append(paragraph(field.path_limitation, "help"));
    }
    if (input.type === "file") {
      const selected = paragraph(
        values.has(key) ? `Previously selected filename: ${values.get(key)}. Reselect to attach local selection metadata.` :
          "No local file selected.", "help",
      );
      input.addEventListener("change", () => {
        const file = input.files[0];
        if (file) {
          values.set(key, file.name);
          selected.textContent = `Selected filename: ${file.name}. Contents not read; host path unavailable.`;
        } else {
          values.delete(key);
          selected.textContent = "No local file selected.";
        }
      });
      fields.append(selected);
    }
    if (field.provenance.includes("configured")) {
      fields.append(paragraph(
        `Value provenance: ${field.provenance.join(" / ")}; ${field.verification}.`,
        "help",
      ));
      for (const selection of field.configured) {
        fields.append(paragraph(
          `Configured text: ${selection.value} (${selection.provenance}; ` +
          `unverified; source ${selection.source.location}).`,
          "help",
        ));
      }
    }
    const choices = field.capabilities.filter(capability => capability.value !== null);
    if (choices.length || field.dependency) {
      const choiceLabel = document.createElement("label");
      const choiceSelector = document.createElement("select");
      choiceSelector.id = `${field.key}-choices`;
      choiceLabel.htmlFor = choiceSelector.id;
      choiceLabel.textContent = "Reported values (optional; manual text entry remains above)";
      const reason = paragraph("", "help");
      choiceControls.set(field.key, { select: choiceSelector, reason });
      choiceSelector.addEventListener("change", () => {
        if (choiceSelector.value !== "") {
          const index = Number(choiceSelector.value);
          input.value = field.capabilities[index].value;
          values.set(key, input.value);
          reportedSelections.set(key, index);
          invalidatePreview();
        } else {
          reportedSelections.delete(key);
        }
        updateDependencies();
      });
      input.addEventListener("input", () => { choiceSelector.value = ""; });
      fields.append(choiceLabel, choiceSelector, reason);
    }
  }
  updateDependencies();

  for (const output of workflow.outputs) {
    const heading = document.createElement("h3");
    heading.textContent = `Inspectable ${output.kind.toUpperCase()} artifact`;
    artifacts.append(heading, paragraph(
      `Expected artifact destination: ${output.field} field. Preview does not write; current run inspection appears in the console.`,
    ));
    for (const view of output.views) {
      artifacts.append(paragraph(`${view.label} (${view.state}): ${view.reason}`));
    }
  }
}

function showDefinitions(updated) {
  const previous = selector.value || "generate-abc";
  definition = updated;
  selector.replaceChildren();
  for (const workflow of definition.workflows) {
    const option = document.createElement("option");
    option.value = workflow.key;
    option.textContent = `${workflow.label}${workflow.available ? "" : " (unavailable)"}`;
    selector.append(option);
  }
  selector.value = previous;
  selector.disabled = false;
  render(definition.workflows.find(workflow => workflow.key === selector.value));
}

async function post(path, value) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-UI-CSRF": csrf },
    body: JSON.stringify(value),
    cache: "no-store",
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.reason || result.error || `HTTP ${response.status}`);
  return result;
}

function button(text, action) {
  const element = document.createElement("button");
  element.type = "button";
  element.textContent = text;
  element.addEventListener("click", action);
  return element;
}

function initialValue(schema) {
  if ("default" in schema) return structuredClone(schema.default);
  if (schema.nullable) return null;
  if (schema.kind === "array") return schema.repeated ? [] : schema.items.map(initialValue);
  if (schema.kind === "object") {
    return Object.fromEntries(
      Object.entries(schema.properties).map(([key, child]) => [key, initialValue(child)]),
    );
  }
  return schema.kind === "boolean" ? false : "";
}

function configurationEditor(schema, value, changed, path = "configuration") {
  const container = document.createElement("div");
  const field = schema.field;
  const title = document.createElement("label");
  const id = `editor-${path}`;
  title.htmlFor = id;
  title.textContent = `${field.label} (${field.required ? "required" : "optional"}; configured/unverified)`;
  container.append(title);
  if (schema.nullable) {
    container.append(button(value === null ? "Set optional value" : "Unset optional value", () => {
      const active = { ...schema, nullable: false };
      delete active.default;
      const next = value === null ? initialValue(active) : null;
      changed(next);
      container.replaceWith(configurationEditor(schema, next, changed, path));
    }));
    if (value === null) return container;
  }
  if (schema.kind === "object") {
    container.className = "configuration-group";
    for (const [key, child] of Object.entries(schema.properties)) {
      container.append(configurationEditor(child, value[key], next => {
        value[key] = next;
        changed(value);
      }, `${path}.${key}`));
    }
  } else if (schema.kind === "array") {
    const redraw = () => container.replaceWith(configurationEditor(schema, value, changed, path));
    value.forEach((item, index) => {
      container.append(configurationEditor(
        schema.items[schema.repeated ? 0 : index], item,
        next => { value[index] = next; changed(value); }, `${path}.${index}`,
      ));
      if (schema.repeated) {
        container.append(button("Remove item", () => {
          value.splice(index, 1);
          changed(value);
          redraw();
        }));
      }
    });
    if (schema.repeated) {
      container.append(button("Add configured item", () => {
        value.push(initialValue(schema.items[0]));
        changed(value);
        redraw();
      }));
    }
  } else {
    const input = control(field, value);
    input.id = id;
    if (schema.choices) {
      const suggestions = document.createElement("datalist");
      suggestions.id = `${id}-suggestions`;
      schema.choices.forEach(choice => {
        const option = document.createElement("option");
        option.value = choice;
        suggestions.append(option);
      });
      input.setAttribute("list", suggestions.id);
      container.append(suggestions);
    }
    input.addEventListener("input", () => {
      const next = input.type === "checkbox" ? input.checked :
        input.type === "number" && input.value !== "" ? input.valueAsNumber : input.value;
      changed(next);
    });
    container.append(input, paragraph(field.help, "help"));
  }
  return container;
}

async function loadWorkflows() {
  const status = document.getElementById("status");
  try {
    const response = await fetch("/api/workflows", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Workflow definitions request failed: HTTP ${response.status}`);
    }
    showDefinitions(await response.json());
    status.textContent = "Definitions loaded. Only configured ABC/USTX operations can execute.";
  } catch (error) {
    status.textContent = `Unable to load workflow definitions: ${error.message}`;
    status.className = "unavailable";
    selector.disabled = true;
  }
}

async function loadUI() {
  const refreshButton = document.getElementById("refresh");
  const refreshStatus = document.getElementById("refresh-status");
  const configStatus = document.getElementById("configuration-status");
  try {
    const response = await fetch("/api/ui", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const ui = await response.json();
    csrf = ui.csrf;
    render(definition.workflows.find(workflow => workflow.key === selector.value));
    await refreshRuns();
    refreshStatus.textContent = `${ui.refresh.state}: ${ui.refresh.reason}`;
    refreshButton.disabled = false;
    refreshButton.addEventListener("click", async () => {
      refreshButton.disabled = true;
      refreshStatus.textContent = "Read-only refresh in progress...";
      try {
        const updated = await post("/api/capabilities/refresh", {});
        reportedSelections.clear();
        showDefinitions(updated);
        document.getElementById("status").textContent = "Definitions refreshed; readiness remains separate from execution support.";
        document.getElementById("status").className = "";
        refreshStatus.textContent = `${updated.refresh.state}: ${updated.refresh.reason}`;
      } catch (error) {
        refreshStatus.textContent = `Refresh failed; displayed evidence may be stale: ${error.message}`;
      } finally {
        refreshButton.disabled = false;
      }
    });
    let draft = ui.configuration.value;
    let revision = 0;
    const edited = () => {
      revision += 1;
      configStatus.textContent = "Edited, unsaved, not yet model-validated. Active sources unchanged; readiness unknown.";
    };
    configStatus.textContent = ui.configuration.reason;
    document.getElementById("configuration-fields").append(configurationEditor(
      ui.configuration.schema, draft, next => { draft = next; edited(); },
    ));
    const validate = document.getElementById("validate-configuration");
    validate.disabled = false;
    validate.addEventListener("click", async () => {
      validate.disabled = true;
      const validatingRevision = revision;
      try {
        const result = await post("/api/configuration-preview", draft);
        if (validatingRevision === revision) configStatus.textContent = result.reason;
      } catch (error) {
        if (validatingRevision === revision) configStatus.textContent = error.message;
      } finally {
        validate.disabled = false;
      }
    });
  } catch (error) {
    refreshStatus.textContent = `Capability refresh unavailable: ${error.message}`;
    configStatus.textContent = `Configuration preview unavailable: ${error.message}`;
  }
}

function copyButton(label, text) {
  return button(label, async event => {
    const target = event?.currentTarget;
    if (target) target.disabled = true;
    try {
      await navigator.clipboard.writeText(text);
      document.getElementById("run-status").textContent = `${label}: copied.`;
    } catch (error) {
      document.getElementById("run-status").textContent = `Copy failed: ${error.message}`;
    } finally {
      if (target) target.disabled = false;
    }
  });
}

function textSection(container, label, text, copy = false) {
  const heading = document.createElement("h3");
  heading.textContent = label;
  const output = document.createElement("pre");
  output.title = label;
  output.textContent = text;
  container.append(heading, output);
  if (copy) container.append(copyButton(`Copy ${label}`, text));
}

function renderCommand(container, record) {
  const command = record.command;
  textSection(container, "Executable", command.executable);
  const heading = document.createElement("h3");
  heading.textContent = "Arguments (ordered)";
  const list = document.createElement("ol");
  command.arguments.forEach(argument => {
    const item = document.createElement("li");
    item.textContent = JSON.stringify(argument);
    list.append(item);
  });
  container.append(heading, list);
  textSection(container, "Working directory", command.cwd);
  textSection(container, "Display-only shell command", command.display, true);
  textSection(container, "Expected artifacts", JSON.stringify(record.expected, null, 2));
  textSection(container, "Operation metadata", JSON.stringify(record.metadata, null, 2));
}

function operationPending(pending) {
  const workflow = definition?.workflows.find(item => item.key === selector.value);
  operationBusy = pending;
  fields.disabled = pending;
  selector.disabled = pending;
  document.getElementById("refresh").disabled = pending || !csrf;
  document.getElementById("preview-operation").disabled =
    pending || !csrf || !definition?.execution_enabled || !workflow?.operation;
  document.getElementById("start-operation").disabled = pending || !previewId;
}

async function previewOperation() {
  if (operationBusy) return;
  invalidatePreview();
  const revision = operationRevision;
  const workflow = definition.workflows.find(item => item.key === selector.value);
  const inputs = Object.fromEntries(workflow.fields.map(field => [
    field.key, values.get(`${workflow.key}:${field.key}${field.selection === "existing-file" ? ":host" : ""}`) ?? field.default,
  ]));
  operationPending(true);
  const status = document.getElementById("operation-status");
  status.textContent = "Validating preview...";
  try {
    const record = await post("/api/operations/preview", { workflow: workflow.key, inputs });
    if (revision !== operationRevision) return;
    previewId = record.preview_id;
    const container = document.getElementById("command-preview");
    container.replaceChildren();
    renderCommand(container, record);
    container.append(paragraph(record.publication));
    status.textContent = "Preview ready; nothing executed. Start uses this exact operation.";
  } catch (error) {
    status.textContent = `Preview failed: ${error.message}`;
  } finally {
    operationPending(false);
  }
}

async function startOperation() {
  if (operationBusy || !previewId) return;
  operationPending(true);
  try {
    const record = await post("/api/runs", { preview_id: previewId });
    previewId = undefined;
    document.getElementById("operation-status").textContent = `Submitted ${record.id}.`;
    selectRun(record.id);
    renderRun(record);
    schedulePoll(record);
    await refreshRuns();
  } catch (error) {
    // A lost response may still have started a run; never retry a submission automatically.
    invalidatePreview();
    document.getElementById("operation-status").textContent =
      `Start failed or response lost: ${error.message}. Refresh session runs before retrying.`;
  } finally {
    operationPending(false);
  }
}

function selectRun(id) {
  clearTimeout(pollTimer);
  pollRevision += 1;
  selectedRun = id;
  document.getElementById("stop-run").disabled = true;
}

function renderRun(record) {
  const container = document.getElementById("execution-console");
  const scroll = new Map([...container.children]
    .filter(item => item.tagName.toLowerCase() === "pre")
    .map(item => [item.title, item.scrollTop]));
  container.replaceChildren();
  const process = record.process;
  container.append(paragraph(
    `${record.tool} / ${record.operation}; run ${record.id}; phase: ${record.phase}; ` +
    `process: ${process.state}; ${record.complete ? (record.success ? "successful" : "not successful") : "in progress"}`,
  ));
  textSection(container, "Lifecycle", JSON.stringify({
    pid: process.pid, submitted: record.submitted_at, started: process.started_at,
    process_completed: process.completed_at, completed: record.completed_at,
    elapsed_seconds: record.duration, exit_status: process.exit_status,
    signal: process.signal, stop_requested: record.stop_requested,
    escalated: process.escalated,
  }, null, 2));
  renderCommand(container, record);
  for (const stream of ["stdout", "stderr"]) {
    textSection(container, stream, process[stream], true);
    container.append(paragraph(
      `${stream}: ${process[`${stream}_bytes`]} bytes received; ` +
      (process[`${stream}_truncated`] ? "TRUNCATED (console only): first 1 MiB retained." : "not truncated.") +
      ` Capture: ${process[`${stream}_complete`] ? "complete (EOF)" : "awaiting EOF or incomplete; inspect process diagnostic"}.`,
    ));
  }
  const diagnostics = {
    process_error: process.error, operation_validation_errors: record.errors,
    artifacts: record.artifacts, completion_metadata: record.completion,
    validation_processes: record.validation_processes,
    active_validation: record.active_validation,
  };
  textSection(container, "Process start/rejection/capture diagnostic", process.error ?? "None");
  textSection(container, "Operation validation errors", JSON.stringify(record.errors, null, 2));
  textSection(container, "Inspected artifacts", JSON.stringify(record.artifacts, null, 2));
  textSection(container, "Completion metadata", JSON.stringify(record.completion, null, 2));
  textSection(container, "Validation subprocesses", JSON.stringify(record.validation_processes, null, 2));
  if (record.active_validation) {
    textSection(container, "Active validation subprocess", JSON.stringify(record.active_validation, null, 2));
  }
  container.append(copyButton("Copy Diagnostics and artifacts", JSON.stringify(diagnostics, null, 2)));
  for (const item of container.children) {
    if (scroll.has(item.title)) item.scrollTop = scroll.get(item.title);
  }
  document.getElementById("stop-run").disabled = runRequest || !record.can_stop;
}

function schedulePoll(record) {
  clearTimeout(pollTimer);
  if (!record.complete && record.id === selectedRun) {
    pollTimer = setTimeout(() => pollRun(record.id), 500);
  }
}

async function pollRun(id) {
  if (!id || id !== selectedRun) return;
  clearTimeout(pollTimer);
  const revision = ++pollRevision;
  document.getElementById("stop-run").disabled = true;
  try {
    const response = await fetch(`/api/runs/${id}`, { cache: "no-store" });
    const record = await response.json();
    if (!response.ok) throw new Error(record.error || `HTTP ${response.status}`);
    if (id !== selectedRun || revision !== pollRevision) return;
    renderRun(record);
    document.getElementById("run-status").textContent = record.complete ?
      "Run complete; polling stopped." : "Receiving output (500 ms polling).";
    schedulePoll(record);
  } catch (error) {
    if (id !== selectedRun || revision !== pollRevision) return;
    document.getElementById("run-status").textContent =
      `Polling failed: ${error.message}. Refresh session runs to retry.`;
    document.getElementById("stop-run").disabled = true;
  }
}

async function refreshRuns() {
  if (listingRuns || runRequest) return;
  listingRuns = true;
  const control = document.getElementById("refresh-runs");
  const choices = document.getElementById("session-runs");
  control.disabled = true;
  choices.disabled = true;
  try {
    const response = await fetch("/api/runs", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    choices.replaceChildren();
    for (const run of result.runs) {
      const option = document.createElement("option");
      option.value = run.id;
      option.textContent = `${run.tool}/${run.operation} ${run.id} (${run.phase})`;
      choices.append(option);
    }
    if (!result.runs.some(run => run.id === selectedRun)) {
      selectRun(result.runs.at(-1)?.id);
      document.getElementById("execution-console").replaceChildren();
    }
    choices.value = selectedRun ?? "";
    if (selectedRun) await pollRun(selectedRun);
  } catch (error) {
    document.getElementById("run-status").textContent = `Run listing failed: ${error.message}`;
  } finally {
    control.disabled = false;
    choices.disabled = false;
    listingRuns = false;
  }
}

async function stopRun() {
  if (!selectedRun || runRequest || listingRuns) return;
  runRequest = true;
  pollRevision += 1;
  clearTimeout(pollTimer);
  const id = selectedRun;
  let record;
  document.getElementById("stop-run").disabled = true;
  document.getElementById("session-runs").disabled = true;
  document.getElementById("refresh-runs").disabled = true;
  try {
    record = await post(`/api/runs/${id}/stop`, {});
    if (id !== selectedRun) return;
    document.getElementById("run-status").textContent = "Stop requested for this owned run only.";
  } catch (error) {
    document.getElementById("run-status").textContent =
      `Stop failed or response lost: ${error.message}. Refresh session runs to check state.`;
  } finally {
    runRequest = false;
    document.getElementById("session-runs").disabled = false;
    document.getElementById("refresh-runs").disabled = false;
    if (record && id === selectedRun) {
      renderRun(record);
      schedulePoll(record);
    }
  }
}

document.getElementById("preview-operation").addEventListener("click", previewOperation);
document.getElementById("start-operation").addEventListener("click", startOperation);
document.getElementById("refresh-runs").addEventListener("click", refreshRuns);
document.getElementById("stop-run").addEventListener("click", stopRun);
document.getElementById("session-runs").addEventListener("change", () => {
  selectRun(document.getElementById("session-runs").value);
  pollRun(selectedRun);
});
selector.addEventListener("change", () => {
  render(definition.workflows.find(workflow => workflow.key === selector.value));
});
loadWorkflows().then(loadUI);
