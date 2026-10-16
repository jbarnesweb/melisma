"use strict";

const selector = document.getElementById("workflow");
const fields = document.getElementById("fields");
const artifacts = document.getElementById("artifacts");
const capabilities = document.getElementById("capabilities");
const values = new Map();
const reportedSelections = new Map();
let definition;
let csrf;

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
  input.required = field.required;
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
    const label = document.createElement("label");
    label.htmlFor = field.key;
    label.append(document.createTextNode(`${field.label} `));
    const indicator = document.createElement("span");
    indicator.className = "indicator";
    indicator.textContent = field.required ? "(required)" : "(optional)";
    label.append(indicator);
    const input = control(field, values.get(key) ?? field.default);
    input.id = field.key;
    input.name = field.key;
    input.addEventListener("input", () => {
      if (input.type === "file") return;
      values.set(key, input.type === "checkbox" ? input.checked : input.value);
      reportedSelections.delete(key);
      updateDependencies();
    });
    const help = paragraph(field.help, "help");
    help.id = `${field.key}-help`;
    input.setAttribute("aria-describedby", help.id);
    fields.append(label, input, help);
    if (field.path_limitation) {
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
      `Future artifact path: ${output.field} field. No artifact is created or read here.`,
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
    status.textContent = "Definitions loaded. All execution is disabled.";
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
    refreshStatus.textContent = `${ui.refresh.state}: ${ui.refresh.reason}`;
    refreshButton.disabled = false;
    refreshButton.addEventListener("click", async () => {
      refreshButton.disabled = true;
      refreshStatus.textContent = "Read-only refresh in progress...";
      try {
        const updated = await post("/api/capabilities/refresh", {});
        reportedSelections.clear();
        showDefinitions(updated);
        document.getElementById("status").textContent = "Definitions refreshed. All execution is disabled.";
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

selector.addEventListener("change", () => {
  render(definition.workflows.find(workflow => workflow.key === selector.value));
});
loadWorkflows().then(loadUI);
