// Trusted, read-only date-value collector. No expected date or model script input.
// `displayed` is the same Selenium visibility atom used by ordinary evidence reads.
const el = arguments[0];
const value = el.value === undefined ? el.getAttribute('value') : String(el.value);
const attr = (node, key) => (node.getAttribute(key) || '').slice(0, 160);
function ref(node) {
    const path = [];
    for (let n = node; n && n.nodeType === 1 && path.length < 40; n = n.parentElement) {
        let index = 1;
        for (let s = n.previousElementSibling; s; s = s.previousElementSibling)
            if (s.tagName === n.tagName) index++;
        path.unshift(n.tagName.toLowerCase() + '[' + index + ']');
    }
    return '/' + path.join('/');
}
const inputRef = ref(el);
const row = {value, tag:el.tagName.toLowerCase(), type:attr(el, 'type').toLowerCase(),
    input_ref:inputRef,
    format_attributes:Object.fromEntries(['data-date-format','data-format','placeholder']
        .map(k => [k,attr(el,k).slice(0,80)]))};
const calendar = {version:1, status:'unknown', reasons:[], input_ref:inputRef,
    input_value:value, captured_at:new Date().toISOString(),
    association:null, root:null, heading:null, selected:[], component_dates:[]};
row.calendar = calendar;
const unknown = reason => { calendar.reasons.push(reason); return row; };
// Explicit formats retain their existing semantics; calendar data is supplementary.
// Bound all scans. Unsupported contexts cannot produce a calendar conclusion.
const all = document.querySelectorAll('*');
if (all.length > 30000) return unknown('dom_limit');
if (document.querySelector('iframe,frame') || Array.from(all).some(n => n.shadowRoot))
    return unknown('unsupported_frame_or_shadow_context');
if (el.tagName !== 'INPUT' || !displayed(el)) return unknown('visible_input_required');
const roots = Array.from(document.querySelectorAll('[role="grid"],.datepicker'));
if (roots.length > 12) return unknown('calendar_limit');
// A grid inside a datepicker belongs to that same calendar, not a second popup.
const visibleRoots = roots.filter(n => displayed(n) && !roots.some(p => p !== n && p.contains(n)));
if (visibleRoots.length > 1) return unknown('multiple_visible_calendars');
let root, grid, method, instance;
const controls = attr(el, 'aria-controls').trim().split(/\s+/).filter(Boolean);
if (controls.length) {
    if (controls.length !== 1) return unknown('multiple_controlled_targets');
    const matches = Array.from(document.querySelectorAll('[id]')).filter(n => n.id === controls[0]);
    if (matches.length !== 1) return unknown('controlled_id_not_unique');
    root = matches[0];
    const grids = root.matches('[role="grid"]') ? [root] : Array.from(root.querySelectorAll('[role="grid"]'));
    if (grids.length !== 1) return unknown('one_grid_required');
    grid = grids[0];
    const owners = Array.from(document.querySelectorAll('input[aria-controls]')).filter(n =>
        attr(n,'aria-controls').split(/\s+/).includes(root.id));
    if (owners.length !== 1 || owners[0] !== el) return unknown('multiple_input_owners');
    method = 'aria-controls';
} else {
    // Bounded adapter for the Bootstrap-datepicker instance shape. An active class
    // alone is NEVER sufficient: require exact input/picker identity and Date state.
    const jq = window.jQuery;
    if (!jq || typeof jq.data !== 'function') return unknown('no_supported_association');
    instance = jq.data(el, 'datepicker'); // Read existing data; never initialize a widget.
    if (!instance || !instance.element || instance.element.length !== 1 || instance.element[0] !== el
        || !instance.inputField || instance.inputField.length !== 1 || instance.inputField[0] !== el
        || !instance.picker || instance.picker.length !== 1 || !Array.isArray(instance.dates))
        return unknown('unsupported_component_shape');
    root = instance.picker[0];
    if (!root || !root.matches('.datepicker') || !root.isConnected) return unknown('component_picker_mismatch');
    const owners = Array.from(document.querySelectorAll('input')).filter(n => {
        const data = jq.data(n, 'datepicker');
        return data && data.picker && data.picker[0] === root;
    });
    if (owners.length !== 1 || owners[0] !== el) return unknown('multiple_input_owners');
    const grids = Array.from(root.querySelectorAll('.datepicker-days'));
    if (grids.length !== 1 || grids[0].querySelectorAll('table').length !== 1)
        return unknown('component_day_view_structure');
    grid = grids[0];
    if (instance.dates.length > 12) return unknown('multiple_component_dates');
    try {
        calendar.component_dates = Array.from(instance.dates, d => Date.prototype.getTime.call(d));
    } catch (_) { return unknown('invalid_component_date_state'); }
    method = 'datepicker-instance';
}
calendar.association = {method, input_ref:inputRef, root_ref:ref(root),
    controlled_id:method === 'aria-controls' ? root.id : null,
    owner_count:1, exact_input:true, exact_picker:true};
calendar.root = {ref:ref(root), id:attr(root,'id'), visible:displayed(root),
    grid_ref:ref(grid), grid_visible:displayed(grid), candidate_count:1};
if (!displayed(root) || !displayed(grid)) return unknown('calendar_closed_or_day_view_hidden');
let heading;
if (method === 'aria-controls') {
    const labels = attr(grid,'aria-labelledby').trim().split(/\s+/).filter(Boolean);
    if (labels.length !== 1) return unknown('one_month_heading_required');
    const labelsFound = Array.from(document.querySelectorAll('[id]')).filter(n => n.id === labels[0]);
    if (labelsFound.length !== 1 || !root.contains(labelsFound[0]) || !displayed(labelsFound[0]))
        return unknown('month_heading_not_visible_unique');
    heading = labelsFound[0];
} else {
    const headings = grid.querySelectorAll('thead .datepicker-switch');
    if (headings.length !== 1 || !displayed(headings[0])) return unknown('one_month_heading_required');
    heading = headings[0];
}
calendar.heading = {text:(heading.innerText || '').trim().slice(0,100), ref:ref(heading)};
const candidates = method === 'aria-controls'
    ? Array.from(grid.querySelectorAll('[role="gridcell"][aria-selected="true"]'))
    : Array.from(grid.querySelectorAll('tbody td.day.active'));
calendar.selected_count = candidates.length;
if (candidates.length > 12) return unknown('selection_limit');
calendar.selected = candidates.map(n => ({ref:ref(n), text:(n.innerText || '').trim().slice(0,40),
    visible:displayed(n), aria_selected:attr(n,'aria-selected'), role:attr(n,'role'),
    disabled:n.matches(':disabled,[disabled],[aria-disabled="true"],.disabled')
        || !!n.closest('[aria-disabled="true"],[disabled]'),
    adjacent:n.matches('.old,.new,.outside,[data-outside-month="true"]'),
    class:attr(n,'class'), data_date:attr(n,'data-date'),
    aria_current:attr(n,'aria-current'),
    state_basis:method === 'aria-controls' ? 'aria-selected' : 'component-dates-and-active-day'}));
if (candidates.length !== 1) return unknown('one_selected_day_required');
calendar.status = 'observed';
return row;
