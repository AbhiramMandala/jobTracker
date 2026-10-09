/* Test harness: runs the SHIPPED results-page filter IIFE against real
 * rendered HTML with a minimal fake DOM, then reports visible-card counts.
 *
 * Usage: node filter_harness.js <results-template> <rendered-html>
 * Prints JSON: { total, options: {selectId: [[value, labelCount]]},
 *                 single: {"selectId:value": visible} }
 */
const fs = require("fs");

const [templatePath, htmlPath] = process.argv.slice(2);
const template = fs.readFileSync(templatePath, "utf8");
const html = fs.readFileSync(htmlPath, "utf8");

const scripts = [...template.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const filterSrc = scripts[1]; // results toolbar filter block

function stripTags(s) {
  return s.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function parseSelects(src) {
  const selects = {};
  const re = /<select id="([^"]+)">([\s\S]*?)<\/select>/g;
  let m;
  while ((m = re.exec(src)) !== null) {
    const opts = [];
    const ore = /<option value="([^"]*)"[^>]*>([\s\S]*?)<\/option>/g;
    let o;
    while ((o = ore.exec(m[2])) !== null) opts.push({ value: o[1], label: stripTags(o[2]) });
    selects[m[1]] = opts;
  }
  return selects;
}

function parseCards(src) {
  const cards = [];
  const re = /<article class="card job-card"([^>]*)>([\s\S]*?)<\/article>/g;
  let m;
  while ((m = re.exec(src)) !== null) {
    const attrs = m[1];
    const body = m[2];
    const verify = (attrs.match(/data-verify="([^"]*)"/) || ["", ""])[1];
    const ctype = (attrs.match(/data-company-type="([^"]*)"/) || ["", ""])[1];
    const h2 = (body.match(/<h2>([\s\S]*?)<\/h2>/) || ["", ""])[1];
    const desc = (body.match(/<p class="desc">([\s\S]*?)<\/p>/) || ["", ""])[1];
    cards.push({ verify, ctype, h2: stripTags(h2), desc: stripTags(desc) });
  }
  return cards;
}

// --- fake DOM -------------------------------------------------------------
const selectDefs = parseSelects(html);
const cardDefs = parseCards(html);

function makeOption(def) {
  let text = def.label;
  const attrs = {};
  return {
    value: def.value,
    get textContent() { return text; },
    set textContent(v) { text = v; },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    setAttribute: (k, v) => { attrs[k] = v; },
  };
}
function makeSelect(id, defs, initial) {
  const listeners = [];
  return {
    id,
    value: initial,
    options: defs.map(makeOption),
    addEventListener: (ev, fn) => { if (ev === "change") listeners.push(fn); },
    fire: function () { listeners.forEach((fn) => fn()); },
  };
}
const initialJobType = (() => {
  const sel = selectDefs["filter-jobtype"] || [];
  const pre = html.match(/<select id="filter-jobtype">([\s\S]*?)<\/select>/);
  const selHtml = pre ? pre[1] : "";
  const msel = selHtml.match(/<option value="([^"]*)" selected>/);
  return msel ? msel[1] : "any";
})();
const selects = {
  "filter-verify": makeSelect("filter-verify", selectDefs["filter-verify"] || [], ""),
  "filter-company": makeSelect("filter-company", selectDefs["filter-company"] || [], ""),
  "filter-exp": makeSelect("filter-exp", selectDefs["filter-exp"] || [], ""),
  "filter-jobtype": makeSelect("filter-jobtype", selectDefs["filter-jobtype"] || [], initialJobType),
};
const cardObjs = cardDefs.map((c) => ({
  hidden: false,
  getAttribute: (k) => (k === "data-verify" ? c.verify : k === "data-company-type" ? c.ctype : null),
  querySelector: (sel) => ({ textContent: sel === "h2" ? c.h2 : c.desc }),
}));
const emptyEl = { hidden: true };
const mountEl = {};

global.document = {
  getElementById: (id) =>
    selects[id] || (id === "filter-empty" ? emptyEl : id === "results" ? mountEl : null),
  querySelectorAll: (sel) => (sel === ".job-card" ? cardObjs : []),
};
global.window = {}; // no MutationObserver -> recount hook skipped, like old browsers

// eslint-disable-next-line no-eval
eval(filterSrc);

function visible() {
  return cardObjs.filter((c) => !c.hidden).length;
}
function setOnly(id, value) {
  for (const [sid, sel] of Object.entries(selects)) {
    sel.value = sid === id ? value : sid === "filter-jobtype" ? "any" : "";
    sel.fire();
  }
  return visible();
}

const out = { total: cardObjs.length, options: {}, single: {} };
for (const [id, sel] of Object.entries(selects)) {
  out.options[id] = sel.options.map((o) => {
    const mm = o.textContent.match(/\((\d+)\)\s*$/);
    return [o.value, mm ? Number(mm[1]) : null];
  });
  for (const o of sel.options) out.single[`${id}:${o.value}`] = setOnly(id, o.value);
}
setOnly("none", ""); // restore neutral
out.neutral = visible();
console.log(JSON.stringify(out));
