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
  trigger(event) { (this.listeners.get(event) || []).forEach(handler => handler()); }
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
function fetch(path, options) {
  requests.push({ path, options });
  return new Promise(() => {});
}
function structuredClone(value) { return JSON.parse(JSON.stringify(value)); }

// APPLICATION_INSERTION_POINT

const ustx = fixture.document.workflows.find(item => item.key === "generate-ustx");
render(ustx);
assert(document.getElementById("midi").type === "file", "MIDI must use file picker");
assert(document.getElementById("midi").accept === ".mid,.midi", "MIDI extension hints");
assert(document.getElementById("lyrics").type === "file", "Lyrics must use file picker");
assert(document.getElementById("lyrics").accept === ".txt", "Lyric extension hints");
assert(document.getElementById("ustx_output").type === "text", "Output is metadata, not an upload");
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
assert(values.get("generate-ustx:midi") === "compatible.mid", "Only filename metadata retained");
assert(requests.length === requestsBeforeFileSelection, "File selection must not initiate network requests");
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
assert(document.getElementById("midi").type === "file", "Workflow switch preserves MIDI file semantics");
assert(fixture.document.workflows.length === 8, "All workflows remain visible");
print("Browser control and dependency assertions passed");
