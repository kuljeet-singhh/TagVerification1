/* Syntax highlighting for the code samples on /docs.
 *
 * Hand-rolled, and staying that way: this repo has no build step and no
 * package.json, and pulling Prism or highlight.js off a CDN would also add a
 * third-party origin to a page that currently has none and break offline dev.
 * The samples here are four languages of very ordinary shape, so a shallow
 * scanner is enough. This is NOT a general lexer and should not grow into one.
 *
 * Two properties the implementation has to keep:
 *
 *   1. Copy fidelity. app.js copies `source.textContent`, and every character
 *      of the input is emitted exactly once — escaped, inside a span or not.
 *      So textContent round-trips byte for byte. Never emit generated content,
 *      line numbers or a gutter here; those leak into a drag-select copy.
 *   2. Additive only. If this file fails to load, or a grammar matches nothing,
 *      the block renders exactly as it did before — plain --code-fg monospace.
 *      Nothing about the page depends on the colour.
 */

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;" };
const esc = (s) => s.replace(/[&<>]/g, (c) => ESCAPES[c]);

/* True when only whitespace separates `i` from the start of its line — used to
 * tell a shell command word from an argument that merely looks like one. */
function atLineStart(text, i) {
  for (let j = i - 1; j >= 0; j -= 1) {
    if (text[j] === "\n") return true;
    if (text[j] !== " " && text[j] !== "\t") return false;
  }
  return true;
}

/* Rules are tried in order at each position, so the ones that swallow whole
 * regions — comments, then strings — must come first, and the catch-all word
 * rule must come last. That word rule is not cosmetic: without it the scanner
 * falls through one character at a time and a later rule can fire in the middle
 * of an identifier (the `-app` of `your-app` read as a `-a` flag, say). */
const WORD = { cls: null, re: /[A-Za-z_$][\w$]*/y };
const NUMBER = { cls: "atom", re: /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y };

const GRAMMARS = {
  json: [
    { cls: "comment", re: /\/\/[^\n]*/y },
    // A key is a string with a colon after it. Checked before the plain string
    // rule, which is otherwise identical and would win.
    { cls: "key", re: /"(?:[^"\\]|\\.)*"(?=\s*:)/y },
    { cls: "str", re: /"(?:[^"\\]|\\.)*"/y },
    { cls: "atom", re: /\b(?:true|false|null)\b/y },
    NUMBER,
    { cls: "punct", re: /[{}[\],:]/y },
    WORD,
  ],

  shell: [
    { cls: "comment", re: /#[^\n]*/y },
    { cls: "str", re: /"(?:[^"\\]|\\.)*"/y },
    { cls: "str", re: /'(?:[^'\\]|\\.)*'/y },
    { cls: "key", re: /[A-Za-z][\w.-]*/y, at: atLineStart },
    { cls: "atom", re: /--?[A-Za-z][\w-]*/y },
    { cls: "punct", re: /[\\|;&]/y },
    { cls: null, re: /[A-Za-z_][\w-]*/y },
  ],

  python: [
    { cls: "comment", re: /#[^\n]*/y },
    { cls: "str", re: /[frbFRB]{0,2}"""[\s\S]*?"""/y },
    { cls: "str", re: /[frbFRB]{0,2}'''[\s\S]*?'''/y },
    { cls: "str", re: /[frbFRB]{0,2}"(?:[^"\\\n]|\\.)*"/y },
    { cls: "str", re: /[frbFRB]{0,2}'(?:[^'\\\n]|\\.)*'/y },
    { cls: "atom", re: /\b(?:None|True|False)\b/y },
    {
      cls: "key",
      re: /\b(?:import|from|as|with|for|in|if|elif|else|def|class|return|and|or|not|is|while|try|except|finally|raise|lambda|async|await|pass|yield|global|nonlocal|del|assert|break|continue)\b/y,
    },
    NUMBER,
    { cls: "punct", re: /[{}[\](),:]/y },
    WORD,
  ],

  javascript: [
    { cls: "comment", re: /\/\/[^\n]*/y },
    { cls: "comment", re: /\/\*[\s\S]*?\*\//y },
    { cls: "str", re: /"(?:[^"\\\n]|\\.)*"/y },
    { cls: "str", re: /'(?:[^'\\\n]|\\.)*'/y },
    { cls: "str", re: /`(?:[^`\\]|\\.)*`/y },
    { cls: "atom", re: /\b(?:null|undefined|true|false|NaN)\b/y },
    {
      cls: "key",
      re: /\b(?:const|let|var|function|return|await|async|for|of|in|if|else|new|class|extends|import|export|from|default|typeof|instanceof|throw|try|catch|finally|delete|void|do|while|switch|case|break|continue|yield|this)\b/y,
    },
    NUMBER,
    { cls: "punct", re: /[{}[\](),;]/y },
    WORD,
  ],
};

function tokenise(text, rules) {
  let out = "";
  let i = 0;

  while (i < text.length) {
    let hit = null;

    for (const rule of rules) {
      if (rule.at && !rule.at(text, i)) continue;
      rule.re.lastIndex = i;
      const match = rule.re.exec(text);
      // Sticky (`y`) anchors the match at lastIndex, so a match is by
      // definition at `i` — no index check needed.
      if (match && match[0].length > 0) {
        hit = { cls: rule.cls, text: match[0] };
        break;
      }
    }

    if (hit) {
      out += hit.cls ? `<span class="tok-${hit.cls}">${esc(hit.text)}</span>` : esc(hit.text);
      i += hit.text.length;
    } else {
      // Whitespace, punctuation no grammar claimed, and anything unexpected.
      out += esc(text[i]);
      i += 1;
    }
  }

  return out;
}

export function highlightAll(root = document) {
  for (const block of root.querySelectorAll(".code-block[data-code-lang]")) {
    if (block.dataset.highlighted === "true") continue;

    const rules = GRAMMARS[block.dataset.codeLang];
    const code = block.querySelector("pre > code");
    if (!rules || !code) continue;

    const source = code.textContent;
    const marked = tokenise(source, rules);

    // Belt and braces on the one property that must not regress. If the
    // round-trip is not exact, leave the block plain rather than hand a
    // reader a Copy button that lies.
    const probe = document.createElement("code");
    probe.innerHTML = marked;
    if (probe.textContent !== source) continue;

    code.innerHTML = marked;
    block.dataset.highlighted = "true";
  }
}

highlightAll();
