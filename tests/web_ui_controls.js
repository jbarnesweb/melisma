// A small DOM/event harness; uses an existing GJS runtime, never installs one.
"use strict";
function assert(condition, message) {
  if (!condition) throw new Error(message);
}
class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.listeners = new Map();
    this.attributes = {};
    this._value = "";
    this.textContent = "";
  }
  append(...children) {
    children.forEach(child => {
      child.parent = this;
      this.children.push(child);
    });
  }
  replaceChildren(...children) {
    this.children = [];
    this._value = "";
    this.append(...children);
  }
  replaceWith(next) {
    if (this.parent) {
      const index = this.parent.children.indexOf(this);
      this.parent.children[index] = next;
      next.parent = this.parent;
    }
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  setCustomValidity(message) { this.validationMessage = message; }
  get valueAsNumber() { return Number(this.value); }
  addEventListener(event, handler) {
    if (!this.listeners.has(event)) this.listeners.set(event, []);
    this.listeners.get(event).push(handler);
  }
  trigger(event) {
    return Promise.all((this.listeners.get(event) || [])
      .map(handler => {
        const dispatched = { currentTarget: this };
        try {
          return handler(dispatched);
        } finally {
          dispatched.currentTarget = null;
        }
      }));
  }
  get options() { return this.children.filter(child => child.tagName === "option"); }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
}
const elements = new Map();
function walk(element, predicate) {
  if (predicate(element)) return element;
  for (const child of element.children || []) {
    const result = walk(child, predicate);
    if (result) return result;
  }
  return undefined;
}
const document = {
  getElementById(id) {
    for (const element of elements.values()) {
      const result = walk(element, item => item.id === id);
      if (result) return result;
    }
    const element = new Element(id === "workflow" ? "select" : "div");
    element.id = id;
    elements.set(id, element);
    return element;
  },
  createElement(tag) { return new Element(tag); },
  createTextNode(textContent) { return { textContent }; },
};
const requests = [];
const timers = new Map();
let timerId = 0;
function setTimeout(action, delay) {
  assert(delay === 500, "Polling interval is bounded at 500 ms");
  timers.set(++timerId, action);
  return timerId;
}
function clearTimeout(id) { timers.delete(id); }
const copied = [];
const navigator = { clipboard: { async writeText(text) { copied.push(text); } } };
function fetch(path, options) {
  requests.push({ path, options });
  return new Promise(() => {});
}
function structuredClone(value) { return JSON.parse(JSON.stringify(value)); }

// APPLICATION_INSERTION_POINT

const ustx = fixture.document.workflows.find(item => item.key === "generate-ustx");
const metadataWorkflow = {
  ...structuredClone(ustx), key: "metadata-only", operation: false,
  fields: ustx.fields.filter(field => field.selection === "existing-file"), outputs: [],
};
render(metadataWorkflow);
assert(document.getElementById("midi").type === "file", "MIDI must use file picker");
assert(document.getElementById("midi").accept === ".mid,.midi", "MIDI extension hints");
assert(document.getElementById("lyrics").type === "file", "Lyrics must use file picker");
assert(document.getElementById("lyrics").accept === ".txt", "Lyric extension hints");
assert(!walk(fields, item => item.id === "midi-host-path"),
  "Non-executable filename metadata has no execution-path control");
const midiInput = document.getElementById("midi");
const requestsBeforeFileSelection = requests.length;
const selectedFile = { name: "compatible.mid" };
for (const method of ["text", "arrayBuffer", "stream"]) {
  selectedFile[method] = () => { throw new Error("file contents must never be read"); };
}
midiInput.files = [selectedFile];
Object.defineProperty(midiInput, "value", {
  get() { throw new Error("browser fake path must never be read"); },
});
midiInput.trigger("input");
midiInput.trigger("change");
assert(values.get("metadata-only:midi") === "compatible.mid", "Only filename metadata retained");
assert(requests.length === requestsBeforeFileSelection, "File selection must not initiate network requests");
values.set("generate-ustx:midi", "compatible.mid");
values.set("generate-ustx:lyrics", "gloria.txt");
render(ustx);
assertExecutionInputs(ustx);
assert(document.getElementById("midi").value === "" && document.getElementById("lyrics").value === "",
  "Filename-only values cannot populate execution paths");
assert(document.getElementById("ustx_output").type === "text", "Output remains an explicit path");
const singer = document.getElementById("singer");
const singerChoices = document.getElementById("singer-choices");
const rendererChoices = document.getElementById("renderer-choices");
const phonemizerChoices = document.getElementById("phonemizer-choices");
assert(rendererChoices.options.length === 3, "Initially all reported renderer choices retained");
singerChoices.value = "0";
singerChoices.trigger("change");
assert(singer.value === "alpha", "Reported text copied into editable singer");
assert(rendererChoices.options.length === 2, "Explicit singer type filters renderers");
assert(rendererChoices.options[1].value === "0", "Classic evidence chooses only first renderer");
assert(phonemizerChoices.options[1].value === "0", "Explicit singer/file evidence filters phonemizers");
const renderer = document.getElementById("renderer");
renderer.value = "manually-entered-renderer";
renderer.trigger("input");
singerChoices.value = "1";
singerChoices.trigger("change");
assert(rendererChoices.options[1].value === "1", "Parent change refreshes renderer evidence choices");
assert(phonemizerChoices.options[1].value === "1", "Parent change refreshes phonemizer evidence choices");
assert(renderer.value === "manually-entered-renderer", "Parent changes must preserve manual metadata");
singer.value = "operator-entered";
singer.trigger("input");
assert(rendererChoices.options.length === 3, "Manual parent has unknown identity, no fabricated filtering");
assert(phonemizerChoices.options.length === 3, "Manual parent preserves unknown phonemizer choices");
assert(ustx.fields[5].capabilities.every(item => item.readiness === "unknown"), "Selection cannot promote readiness");
const unknownField = structuredClone(ustx.fields[5]);
unknownField.dependency.bindings[0].choices = [0, 1];
unknownField.dependency.bindings[0].state = "unknown";
const unknown = dependentChoices(unknownField, 0);
assert(unknown.state === "unknown" && unknown.choices.length === 2, "Insufficient evidence remains explicitly unknown");
const allManual = dependentChoices(ustx.fields[5], undefined);
assert(allManual.state === "unknown", "Unknown dependency state is explicit");

const configSchema = fixture.configuration.schema;
const targetSchema = configSchema.properties.yue2_targets.items[0].properties;
const port = control(targetSchema.administration.properties.port.field, "");
assert(port.type === "number", "Numeric fields use constrained numeric inputs");
assert(port.attributes.min === "1" && port.attributes.max === "65535", "Known port bounds preserved");
assert(port.attributes.step === "1", "Known integer step preserved");
const generationSchema = targetSchema.contracts.items[0].properties.defaults.properties;
const seed = control(generationSchema.seed.field, "");
assert(seed.type === "number" && seed.attributes.step === "1", "Integer type, no invented value");
assert(!("min" in seed.attributes) && !("max" in seed.attributes), "Unknown numeric bounds must not exist");
const topP = control(generationSchema.abc_sampling.properties.top_p.field, "");
assert(topP.attributes.min === "0" && topP.attributes.max === "1", "Known sampling range");
assert(topP.attributes.step === "any", "No invented fractional increment");
const positive = control(generationSchema.budget.field, "");
positive.value = "0";
positive.trigger("input");
assert(positive.validationMessage !== "", "Exclusive positive bound rejects zero without inventing epsilon");
positive.value = "0.001";
positive.trigger("input");
assert(positive.validationMessage === "", "Positive values are not rounded to an invented step");
const mixSchema = configSchema.properties.presets.items[0].properties.mix.properties;
for (const key of ["instrumental_gain", "vocal_gain", "master_gain"]) {
  const gain = control(mixSchema[key].field, mixSchema[key].default);
  assert(gain.attributes.min === "0" && !("max" in gain.attributes), "Gain upper bound is not established");
  assert(gain.attributes.step === "any", "No invented gain increment");
  gain.value = "0";
  gain.trigger("input");
  assert(gain.validationMessage !== "", "Existing adapter/configuration contract explicitly disallows zero gain");
  gain.value = "0.001";
  gain.trigger("input");
  assert(gain.validationMessage === "", "Existing contract accepts positive fractional gains");
}
const root = configurationEditor(configSchema, fixture.configuration.value, () => {});
const recursive = walk(root, element => element.id === "editor-configuration.sources.0.singer_roots.0.recursive");
assert(recursive && recursive.type === "checkbox", "Real traversal-policy field renders checkbox");
assert(recursive.checked === false, "Actual model default retained");
let optionalValue = null;
const holder = new Element("div");
holder.append(configurationEditor(
  generationSchema.offline, null, value => { optionalValue = value; }, "offline",
));
walk(holder, item => item.tagName === "button").trigger("click");
const offline = walk(holder, item => item.id === "editor-offline");
assert(offline && offline.type === "checkbox", "Optional real boolean can be explicitly set");
offline.checked = true;
offline.trigger("input");
assert(optionalValue === true, "Boolean editing stores a boolean, not text");
showDefinitions(fixture.document);
selector.value = "generate-abc";
selector.trigger("change");
assert(!walk(fields, item => item.id === "singer"), "Workflow switch removes irrelevant USTX fields");
assertExecutionInputs(fixture.document.workflows.find(item => item.key === "generate-abc"));
assert(fixture.document.workflows.length === 8, "All workflows remain visible");
print("Browser control and dependency assertions passed");

function collect(element, predicate, found = []) {
  if (predicate(element)) found.push(element);
  for (const child of element.children || []) collect(child, predicate, found);
  return found;
}

function assertExecutionInputs(workflow) {
  assert(!collect(fields, item => item.type === "file").length,
    `${workflow.key}: executable inputs have no browser pickers`);
  for (const key of ["midi", "lyrics"]) {
    const inputs = collect(fields, item => item.tagName === "input" && item.id === key);
    assert(inputs.length === 1 && inputs[0].type === "text" && inputs[0].required,
      `${workflow.key}: one required absolute-path execution control for ${key}`);
    assert(!walk(fields, item => item.id === `${key}-host-path`),
      `${workflow.key}: no redundant second path control`);
    const label = walk(fields, item => item.tagName === "label" && item.htmlFor === key);
    const expected = key === "midi" ? "MIDI input" : "Melisma lyric input";
    assert(label.children[0].textContent === `${expected}: absolute local path `,
      `${workflow.key}: unambiguous ${key} label`);
    const help = document.getElementById(`${key}-help`).textContent;
    for (const phrase of ["machine running the Melisma web server", "does not read or upload",
      "Preview validates", "same retained path"]) {
      assert(help.includes(phrase), `${workflow.key}: ${key} explains ${phrase}`);
    }
  }
  for (const stale of ["optional filename metadata", "Selected filename", "host path unavailable"]) {
    assert(!collect(fields, item => item.textContent?.includes(stale)).length,
      `${workflow.key}: no stale picker explanation`);
  }
}

async function executionAssertions() {
  const markup = '<script>alert("not HTML")</script> & stdout\n  indentation';
  const record = {
    id: "a".repeat(32), tool: "fixture", operation: "abc", phase: "running",
    complete: false, success: false, can_stop: true,
    submitted_at: "2026-10-16T00:00:00+00:00", completed_at: null, duration: 1,
    stop_requested: false,
    command: {
      executable: "/fixture/tool", arguments: ["space here", "", "<b>literal</b>"],
      cwd: "/fixture", display: "'/fixture/tool' 'space here' '' '<b>literal</b>'",
    },
    expected: [{ path: "/fixture/new.abc", kind: "abc" }], metadata: {},
    process: {
      state: "running", pid: 123, started_at: "2026-10-16T00:00:00+00:00",
      completed_at: null, exit_status: null, signal: null, escalated: false,
      stdout: markup, stderr: "separate stderr\n", error: null,
      stdout_bytes: 100, stderr_bytes: 16, stdout_truncated: true, stderr_truncated: false,
      stdout_complete: false, stderr_complete: false,
    },
    errors: ["validation detail"], artifacts: [], completion: null,
    validation_processes: [], active_validation: null,
  };
  const container = document.getElementById("execution-console");
  Object.defineProperty(Element.prototype, "innerHTML", {
    set() { throw new Error("HTML output interpretation is forbidden"); },
  });
  selectRun(record.id);
  renderRun(record);
  const outputs = collect(container, item => item.tagName === "pre");
  assert(outputs.some(item => item.textContent === markup), "stdout preserved exactly as text");
  assert(outputs.some(item => item.textContent === "separate stderr\n"), "stderr has its own pre");
  assert(!outputs.some(item => item.textContent.includes(markup) &&
    item.textContent.includes("separate stderr")), "Streams never flattened together");
  assert(collect(container, item => item.textContent?.includes("TRUNCATED")).length,
    "Truncation visibly reported");
  assert(collect(container, item => item.textContent?.includes("console only")).length,
    "Console truncation does not imply an incomplete artifact");
  assert(collect(container, item => item.textContent?.includes("awaiting EOF or incomplete")).length,
    "Pending or incomplete capture visibly distinguished from EOF");
  assert(!document.getElementById("stop-run").disabled, "Stop enabled for live child");
  for (const label of ["Copy stdout", "Copy stderr", "Copy Display-only shell command",
    "Copy Diagnostics and artifacts"]) {
    const copy = walk(container, item => item.textContent === label);
    await copy.trigger("click");
    assert(!copy.disabled, "Copy pending state is cleared");
  }
  assert(copied[0] === markup && copied[1] === "separate stderr\n", "Copy is faithful");
  assert(copied[2] === record.command.display, "Display command is copyable only");
  assert(copied[3].includes("validation detail"), "Useful diagnostics copied");

  const replies = [];
  fetch = async (path, options) => {
    requests.push({ path, options });
    if (path === "/api/artifacts") {
      return { ok: true, async json() {
        return { artifacts: [], consumers: [], history_path: "/fixture/history.json" };
      } };
    }
    const reply = replies.shift();
    if (reply instanceof Error) throw reply;
    assert(reply, `Unexpected fetch ${path}`);
    return { ok: reply.status < 400, status: reply.status, async json() { return reply.body; } };
  };
  replies.push({ status: 200, body: record });
  await pollRun(record.id);
  assert(timers.size === 1, "Live run schedules one bounded poll");
  const complete = structuredClone(record);
  complete.complete = true;
  complete.phase = "complete";
  complete.can_stop = false;
  complete.process.state = "exited";
  complete.process.exit_status = 7;
  complete.process.stdout_complete = true;
  complete.process.stderr_complete = true;
  replies.push({ status: 200, body: complete });
  await pollRun(record.id);
  assert(timers.size === 0, "Terminal run stops polling");
  assert(document.getElementById("stop-run").disabled, "Completed Stop is disabled");
  assert(collect(container, item => item.textContent?.includes("complete (EOF)")).length,
    "Actual capture EOF visibly reported");
  replies.push(new Error("transport unavailable"));
  await pollRun(record.id);
  assert(document.getElementById("run-status").textContent.includes("Polling failed"),
    "Polling errors visible, not success-shaped");
  assert(timers.size === 0, "Failed poll does not retry indefinitely");

  definition.execution_enabled = true;
  csrf = "fixture-token";
  for (const workflow of definition.workflows) {
    selector.value = workflow.key;
    render(workflow);
    if (workflow.operation) assertExecutionInputs(workflow);
    assert(document.getElementById("execution-actions").hidden === !workflow.operation,
      `${workflow.key}: execution actions hidden for unsupported workflows`);
    operationPending(true);
    assert(document.getElementById("preview-operation").disabled,
      `${workflow.key}: pending state disables Preview`);
    operationPending(false);
    assert(document.getElementById("preview-operation").disabled === !workflow.operation,
      `${workflow.key}: released Preview matches selected-workflow executability`);
  }
  for (const mode of ["abc", "ustx"]) {
    selector.value = `generate-${mode}`;
    await selector.trigger("change");
    const paths = { midi: `/fixture/${mode}-input.mid`, lyrics: `/fixture/${mode}-lyrics.txt` };
    for (const [key, path] of Object.entries(paths)) {
      const input = document.getElementById(key);
      input.value = path;
      await input.trigger("input");
    }
    const output = document.getElementById(`${mode}_output`);
    output.value = `/fixture/new.${mode}`;
    await output.trigger("input");
    for (const key of ["midi", "lyrics"]) {
      replies.push({ status: 200, body: {
        ...record, preview_id: "preview-token", publication: "Exclusive publication",
      } });
      await previewOperation();
      assert(previewId === "preview-token", `${mode}: validated preview retained`);
      const draft = JSON.parse(requests.at(-1).options.body);
      assert(draft.workflow === `generate-${mode}` &&
        draft.inputs.midi === paths.midi && draft.inputs.lyrics === paths.lyrics,
      `${mode}: Preview submits only explicitly entered execution paths`);
      assert(!("argv" in draft) && !("executable" in draft), "Browser sends no raw command");
      assert(!document.getElementById("start-operation").disabled, "Start enabled after preview");
      const input = document.getElementById(key);
      paths[key] += ".changed";
      input.value = paths[key];
      await input.trigger("input");
      assert(previewId === undefined && document.getElementById("start-operation").disabled,
        `${mode}: changing ${key} invalidates Preview`);
      assert(!document.getElementById("command-preview").children.length, "Stale preview cleared");
    }
  }
  selector.value = "generate-abc";
  await selector.trigger("change");
  const abcPath = document.getElementById("midi").value;
  replies.push({ status: 200, body: {
    ...record, preview_id: "preview-token", publication: "Exclusive publication",
  } });
  await previewOperation();
  assert(previewId === "preview-token", "Validated preview retained");
  selector.value = "generate-ustx";
  await selector.trigger("change");
  assert(previewId === undefined && document.getElementById("start-operation").disabled,
    "Switching workflows invalidates Preview");
  assert(document.getElementById("midi").value === values.get("generate-ustx:midi:host") &&
    document.getElementById("midi").value !== abcPath, "Workflow path values remain separate");
  selector.value = "generate-abc";
  await selector.trigger("change");
  assert(document.getElementById("midi").value === abcPath, "Explicit paths survive safe workflow switching");
  assert(!document.getElementById("command-preview").children.length, "Stale preview cleared");
  replies.push({ status: 422, body: { error: "input rejected" } });
  await previewOperation();
  assert(document.getElementById("operation-status").textContent.includes("Preview failed"),
    "Preview failures visibly rejected");
  assert(!operationBusy && !fields.disabled, "Controls re-enabled after preview failure");

  previewId = "start-token";
  replies.push({ status: 202, body: record }, { status: 200, body: { runs: [record] } },
    { status: 200, body: complete });
  await startOperation();
  const submission = requests.find(item =>
    item.path === "/api/runs" && item.options?.method === "POST");
  assert(JSON.stringify(JSON.parse(submission.options.body)) ===
    '{"preview_id":"start-token"}', "Start submits only exact preview identity");
  assert(previewId === undefined && !operationBusy, "Start consumes preview");
  replies.push({ status: 200, body: complete });
  await stopRun();
  const stop = requests.find(item => item.path.endsWith("/stop"));
  assert(JSON.parse(stop.options.body) && !stop.options.body.includes("pid"),
    "Stop targets selected run ID, never arbitrary PID");
  assert(!runRequest, "Stop pending state released");
  assert(timers.size === 0, "Stop completion leaves no polling timer");

  previewId = "lost-response";
  replies.push(new Error("response lost"));
  await startOperation();
  assert(document.getElementById("operation-status").textContent.includes("Refresh session runs"),
    "Lost start response never automatically resubmits");
  const before = copied.length;
  navigator.clipboard.writeText = async () => { throw new Error("denied"); };
  await copyButton("Copy stdout", markup).trigger("click");
  assert(copied.length === before &&
    document.getElementById("run-status").textContent.includes("Copy failed"), "Copy failures visible");
  print("Execution console, trust-boundary, copy and polling assertions passed");
  await artifactAssertions();
}

async function artifactAssertions() {
  const entry = {
    id: "b".repeat(32), path: "/fixture/lyrics.txt", type: "lyrics",
    revision: "original-revision", exists: true, valid: true, production_status: "imported",
  };
  const history = {
    artifacts: [entry], consumers: [{
      id: "consumer", stage: "melisma.abc", success: true, stale: true, changed_inputs: [entry.id],
    }],
    import_types: ["midi", "lyrics", "text"], history_path: "/fixture/artifacts.json",
  };
  const markup = "<script>literal lyric text</script>";
  let view = { artifact: entry, text: markup, editable: true, truncated: false, limit: 65536 };
  fetch = async (path, options) => {
    requests.push({ path, options });
    let body;
    if (path === "/api/artifacts") body = history;
    else if (path === "/api/artifacts/compatible") body = { artifacts: [entry] };
    else if (path.endsWith("/text")) body = view;
    else if (path.endsWith("/save")) body = entry;
    else throw new Error(`Unexpected artifact fetch: ${path}`);
    return { ok: true, async json() { return body; } };
  };
  await refreshArtifacts();
  assert(document.getElementById("recent-artifacts").options.length === 2,
    "Recent history offers an explicit choice, not an automatically selected artifact");
  assert(collect(document.getElementById("artifact-consumers"),
    item => item.textContent?.includes("Historical success: true")).length,
  "Stale input warning preserves historical execution success");
  selector.value = "generate-abc";
  render(definition.workflows.find(item => item.key === selector.value));
  const find = collect(fields, item => item.textContent === "Find recent compatible artifacts")[1];
  await find.trigger("click");
  const lookup = JSON.parse(requests.at(-1).options.body);
  assert(lookup.workflow === "generate-abc" && lookup.field === "lyrics",
    "Compatibility choices are requested from the server for the actual field");
  const recent = document.getElementById("lyrics-recent-artifact");
  recent.value = entry.path;
  previewId = "old-preview";
  await recent.trigger("change");
  assert(document.getElementById("lyrics").value === entry.path &&
    values.get("generate-abc:lyrics:host") === entry.path,
  "Recent artifact selection retains the existing explicit execution-path boundary");
  assert(previewId === undefined, "Artifact selection invalidates Preview");
  document.getElementById("lyrics").value = "/manual/lyrics.txt";
  await document.getElementById("lyrics").trigger("input");
  assert(values.get("generate-abc:lyrics:host") === "/manual/lyrics.txt",
    "Manual absolute-path entry remains available");
  document.getElementById("recent-artifacts").value = entry.id;
  const beforeInspection = requests.length;
  await inspectArtifact();
  assert(requests.length === beforeInspection + 1 && !requests.at(-1).options.method,
    "Inspection performs only a read request");
  assert(document.getElementById("artifact-text").textContent === markup,
    "Text artifacts render as literal text, never HTML");
  assert(document.getElementById("artifact-editor").hidden, "Viewing is not editing");
  const beforeEdit = requests.length;
  beginArtifactEdit();
  assert(requests.length === beforeEdit, "Opening the explicit editor never writes");
  assert(!document.getElementById("artifact-replace").checked &&
    document.getElementById("artifact-save-path").value === "",
  "Save As to a new destination is the default");
  document.getElementById("artifact-edit-text").value = "edited lyrics";
  document.getElementById("artifact-save-path").value = "/fixture/derived-lyrics.txt";
  await saveArtifact();
  const saveRequest = requests.findLast(item => item.path.endsWith("/save"));
  const saved = JSON.parse(saveRequest.options.body);
  assert(saved.replace === false && saved.revision === entry.revision &&
    saved.destination === "/fixture/derived-lyrics.txt" && saved.text === "edited lyrics",
  "Save sends an explicit new path, original revision, complete draft and no replacement");
  view = { ...view, editable: false, truncated: true };
  await inspectArtifact();
  assert(document.getElementById("edit-artifact").disabled,
    "Truncated inspection disables editing");
  const beforeTruncatedSave = requests.length;
  beginArtifactEdit();
  await saveArtifact();
  assert(requests.length === beforeTruncatedSave, "Truncated buffers are never submitted for saving");
  assert(document.getElementById("artifact-text-status").textContent.includes("TRUNCATED"),
    "Truncation is explicit");
  const registration = document.getElementById("register-artifact");
  const registrationType = document.getElementById("artifact-import-type");
  document.getElementById("artifact-import-path").value = entry.path;
  registrationType.value = "lyrics";
  let selectedType = "lyrics";
  for (const outcome of [
    "success", "success", "validation failure", "success", "text success",
    "transport failure", "history failure",
  ]) {
    if (outcome === "text success") {
      selectedType = "text";
      registrationType.value = selectedType;
    }
    document.getElementById("artifact-import-path").value =
      outcome === "validation failure" ? "/fixture/missing-lyrics.txt" : entry.path;
    let finish;
    let fail;
    fetch = (path, options) => {
      requests.push({ path, options });
      if (path === "/api/artifacts/register") {
        assert(JSON.parse(options.body).type === selectedType,
          `${outcome}: registration submits the user's selected type`);
        return new Promise((resolve, reject) => { finish = resolve; fail = reject; });
      }
      assert(path === "/api/artifacts", "Registration refresh reads only artifact history");
      if (outcome === "history failure") return Promise.reject(new Error("history unavailable"));
      return Promise.resolve({ ok: true, async json() { return history; } });
    };
    const pending = registration.trigger("click");
    assert(registration.disabled, `${outcome}: registration stays disabled while awaiting network`);
    // trigger() already cleared currentTarget, just as DOM dispatch does.
    if (outcome === "transport failure") {
      fail(new Error("network unavailable"));
    } else {
      finish({
        ok: outcome !== "validation failure", status: outcome === "validation failure" ? 422 : 200,
        async json() { return outcome === "validation failure" ? { error: "input rejected" } : entry; },
      });
    }
    await pending;
    assert(!registration.disabled, `${outcome}: registration button recovers after dispatch has ended`);
    assert(registrationType.value === selectedType,
      `${outcome}: registration preserves the user's ${selectedType} selection`);
    if (outcome !== "success" && outcome !== "text success") {
      assert(document.getElementById("artifact-status").textContent.includes("failed"),
        `${outcome}: failure remains visible after controls recover`);
    }
  }
  print("Artifact compatibility, inspection, explicit editing and stale-warning assertions passed");
}
const loop = new imports.gi.GLib.MainLoop(null, false);
let executionFailure;
executionAssertions().then(
  () => loop.quit(),
  error => { executionFailure = error; loop.quit(); },
);
loop.run();
if (executionFailure) throw executionFailure;
