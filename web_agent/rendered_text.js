// Trusted, read-only light-DOM text extraction. One traversal, cached layout/style
// reads; no WebDriver getElementText recursion and no page mutations.
const [roots, limit] = arguments;
const styles = new WeakMap(), rects = new WeakMap();
const style = el => {
    if (!styles.has(el)) styles.set(el, getComputedStyle(el));
    return styles.get(el);
};
const rect = el => {
    if (!rects.has(el)) rects.set(el, el.getBoundingClientRect());
    return rects.get(el);
};
const trim = s => s.replace(/^[^\S\u00a0]+|[^\S\u00a0]+$/g, "");
return roots.map(root => {
    let lines = [""], size = 0, visited = 0, problem = null;
    const newline = () => { if (trim(lines[lines.length - 1]) !== "") lines.push(""); };
    const clipped = (el, clips) => {
        if (!clips.length) return false;
        const r = rect(el);
        return clips.some(c => c.x && (r.right <= c.r.left || r.left >= c.r.right) ||
                               c.y && (r.bottom <= c.r.top || r.top >= c.r.bottom));
    };
    const addText = (raw, css) => {
        size += raw.length;
        if (size > limit) { problem = "text_limit_exceeded"; return; }
        let text = raw.replace(/[\u200b\u200e\u200f]/g, "").replace(/\r\n|\r/g, "\n");
        if (css.whiteSpace === "normal" || css.whiteSpace === "nowrap") text = text.replace(/\n/g, " ");
        if (["pre", "pre-wrap", "break-spaces"].includes(css.whiteSpace)) {
            text = text.replace(/[ \f\t\v\u2028\u2029]/g, "\u00a0");
        } else {
            text = text.replace(/[ \f\t\v\u2028\u2029]+/g, " ");
        }
        if (css.textTransform === "uppercase") text = text.toUpperCase();
        if (css.textTransform === "lowercase") text = text.toLowerCase();
        if (css.textTransform === "capitalize") text = text.replace(/(^|\s)(\S)/g, (_, a, b) => a + b.toUpperCase());
        const last = lines.length - 1;
        if (lines[last].endsWith(" ") && text.startsWith(" ")) text = text.slice(1);
        lines[last] += text;
    };
    const walk = (el, clips, depth) => {
        if (problem) return;
        if (++visited > 100000 || depth > 500) { problem = "text_structure_limit"; return; }
        if (el.shadowRoot || el.tagName === "SLOT") { problem = "unsupported_shadow_text"; return; }
        const css = style(el);
        if (css.display === "none" || css.contentVisibility === "hidden" || Number(css.opacity) === 0 ||
            ["SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT", "INPUT"].includes(el.tagName)) return;
        const identity = ["type", "name", "id", "autocomplete"].map(k => el.getAttribute(k) || "").join(" ");
        if (/password|passwd|secret|token|api.?key|credit.?card/i.test(identity)) {
            problem = "sensitive_text"; return;
        }
        const visible = css.visibility !== "hidden" && css.visibility !== "collapse" && !clipped(el, clips);
        if (el.tagName === "BR") { if (visible) lines.push(""); return; }
        const cell = el.tagName === "TD" || css.display === "table-cell";
        const block = !cell && !["inline", "inline-block", "inline-table", "contents", "none"].includes(css.display);
        if (block) newline();
        const x = ["hidden", "clip"].includes(css.overflowX), y = ["hidden", "clip"].includes(css.overflowY);
        const nextClips = x || y ? [...clips, {r:rect(el), x, y}] : clips;
        for (const child of el.childNodes) {
            if (problem) break;
            if (child.nodeType === Node.TEXT_NODE && visible && !(el.tagName === "DETAILS" && !el.open)) {
                addText(child.nodeValue || "", css);
            }
            else if (child.nodeType === Node.ELEMENT_NODE) {
                // Children can override visibility:hidden; display/opacity cannot.
                if (el.tagName === "DETAILS" && !el.open && child.tagName !== "SUMMARY") continue;
                walk(child, nextClips, depth + 1);
            }
        }
        if (cell && lines[lines.length - 1] && !lines[lines.length - 1].endsWith(" ")) lines[lines.length - 1] += " ";
        if (block) newline();
    };
    let clips = [], excluded = false;
    for (let p = root.parentElement; p; p = p.parentElement) {
        const css = style(p);
        if (css.display === "none" || css.contentVisibility === "hidden" || Number(css.opacity) === 0) excluded = true;
        const x = ["hidden", "clip"].includes(css.overflowX), y = ["hidden", "clip"].includes(css.overflowY);
        if (x || y) clips.push({r:rect(p), x, y});
    }
    if (!excluded) walk(root, clips, 0);
    const text = problem ? null : trim(lines.map(trim).join("\n")).replace(/\u00a0/g, " ");
    return {text, status:problem || "observed", text_length:problem ? size : text.length,
            text_method:"rendered_dom", text_complete:!problem};
});
