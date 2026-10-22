// chromecli gate heuristics (spec 6.7): which actions need Kevin's in-browser "Allow once".
// Pure ES module (node --test). Input: the op and an element descriptor built in the page by page/actions.js
// (tag, type, role, name, value, title, id, nameAttr, label, autocomplete, inForm, defaultSubmit, isField).
// Output: reasons (empty = not gated). Later layers (Jev, P4) may add reasons; nothing here is ever removed.

export const SUBMIT_WORDS = /\b(buy|purchase|order|pay|checkout|send|post|publish|tweet|reply|submit|confirm|delete|remove|destroy|transfer|withdraw|subscribe|unsubscribe|accept|agree|approve|sign ?out|log ?out|merge|deploy)\b/i;
export const SENSITIVE_NAME = /card|cvv|cvc|iban|routing|ssn/i;
export const CREDENTIAL_AUTOCOMPLETE = /(?:^|\s)(current-password|new-password|one-time-code)(?:\s|$)/i;
export const PAYMENT_AUTOCOMPLETE = /(?:^|\s)cc-[a-z-]+(?:\s|$)/i;

const lc = (s) => String(s ?? "").toLowerCase();

// A click on this element submits / commits something (spec: always gated).
export function submitReasons(el) {
  if (!el) return [];
  const out = [];
  const tag = lc(el.tag);
  const type = lc(el.type);
  if (tag === "button" && el.inForm && (el.defaultSubmit || type === "" || type === "submit")) out.push("submit_button");
  if (tag === "input" && (type === "submit" || type === "image")) out.push("submit_input");
  for (const text of [el.name, el.value, el.title]) {
    const m = SUBMIT_WORDS.exec(String(text ?? ""));
    if (m) { out.push(`submit_word:${lc(m[1]).replace(/\s+/g, "")}`); break; }
  }
  return out;
}

// Typing into this field enters a credential or payment data (spec: always gated; its value is masked).
export function sensitiveReasons(el) {
  if (!el) return [];
  const out = [];
  const ac = lc(el.autocomplete);
  if (lc(el.tag) === "input" && lc(el.type) === "password") out.push("password_field");
  if (PAYMENT_AUTOCOMPLETE.test(ac)) out.push("payment_field");
  if (CREDENTIAL_AUTOCOMPLETE.test(ac)) out.push("credential_field");
  if ([el.nameAttr, el.id, el.label, el.name].some((s) => SENSITIVE_NAME.test(String(s ?? "")))) out.push("sensitive_name");
  return out;
}

export function isSensitiveField(el) {
  return sensitiveReasons(el).length > 0;
}

// Is the element a text-entry field (where Enter submits the form)?
function isField(el) {
  const tag = lc(el?.tag);
  if (el?.isField) return true;
  if (tag === "textarea" || tag === "select") return true;
  if (tag !== "input") return false;
  return !["button", "submit", "image", "reset", "checkbox", "radio", "file", "hidden", "range", "color"].includes(lc(el.type));
}

// op: click | type | fill | select | press | eval | navigate | open.
// ctx: { key } for press; { allowlisted } for navigate / open.
export function gateReasons(op, el, ctx = {}) {
  const reasons = [];
  switch (op) {
    case "eval": reasons.push("eval"); break;
    case "click": reasons.push(...submitReasons(el)); break;
    case "type": case "fill": case "select": reasons.push(...sensitiveReasons(el)); break;
    case "press": {
      const key = lc(ctx.key);
      if (key === "enter" && el && el.inForm && isField(el)) reasons.push("enter_in_form");
      // Enter / Space on a button or link activates it like a click.
      if ((key === "enter" || key === " " || key === "space") && el && !isField(el)) reasons.push(...submitReasons(el));
      break;
    }
    case "navigate": case "open": if (!ctx.allowlisted) reasons.push("origin_not_allowlisted"); break;
    default: reasons.push("unknown_op");
  }
  return [...new Set(reasons)];
}

// The value as Kevin sees it in the confirm window: masked for credential / payment fields.
export function displayValue(value, el) {
  if (value == null) return null;
  const s = String(value);
  if (isSensitiveField(el)) return { masked: true, text: "•".repeat(Math.min(Math.max(s.length, 4), 12)), chars: s.length };
  return { masked: false, text: s.length > 200 ? s.slice(0, 200) + "…" : s, chars: s.length };
}

// Readable labels for the confirm window's "Gated because" list.
const LABELS = {
  eval: "Runs JavaScript in the page",
  submit_button: "Submit button inside a form",
  submit_input: "Form submit input",
  password_field: "Password field",
  payment_field: "Payment card field (autocomplete cc-*)",
  credential_field: "Sign-in / one-time-code field",
  sensitive_name: "Field looks like card / CVV / IBAN / routing / SSN",
  enter_in_form: "Enter in a form field submits the form",
  origin_not_allowlisted: "Site isn't in read_allow or act_allow",
};
export function reasonLabel(r) {
  const s = String(r || "");
  if (LABELS[s]) return LABELS[s];
  if (s.startsWith("submit_word:")) return `Label says “${s.slice(12)}” (commits, sends, or deletes)`;
  if (s.startsWith("jev:")) return `Jev classified it as ${s.slice(4).replace(/_/g, " ")}`;
  return s.replace(/_/g, " ");
}
