"""Small deterministic calendar-date rules shared by provenance and verification.

No locale guessing, rollover correction, model-provided formats or browser access.
Keeping these rules here avoids coupling schema validation to an Agent/Runtime.
"""
from datetime import date, datetime, timezone
import re


ISO = r'[0-9]{4}-[0-9]{2}-[0-9]{2}'
CHINESE = r'[0-9]{4}\s*年\s*[0-9]{1,2}\s*月\s*[0-9]{1,2}\s*日'
SOURCE_DATES = re.compile(r'(?<![0-9])(?:' + CHINESE + '|' + ISO
                          + r'|[0-9]{1,4}/[0-9]{1,2}/[0-9]{1,4})(?![0-9])')
FORMATS = {'YYYY-MM-DD', 'MM/DD/YYYY', 'DD/MM/YYYY'}


def normalize_date(value):
    """Accept only self-describing ISO or Chinese calendar dates."""
    if not isinstance(value, str):
        raise ValueError('date must be a string')
    if re.fullmatch(ISO, value):
        parts = value.split('-')
    elif re.fullmatch(CHINESE, value):
        parts = re.findall(r'[0-9]+', value)
    else:
        raise ValueError('date requires unambiguous YYYY-MM-DD or Chinese year/month/day')
    try:
        return date(*map(int, parts)).isoformat()
    except ValueError as exc:
        raise ValueError('invalid calendar date') from exc


def expected_date(value):
    if not isinstance(value, str) or not re.fullmatch(ISO, value):
        raise ValueError('date_equals expected must be canonical YYYY-MM-DD')
    return normalize_date(value)


def validate_date_source(expected, quote):
    normalized = expected_date(expected)
    matches = list(SOURCE_DATES.finditer(quote))
    if len(matches) != 1:
        raise ValueError('date source_quote must identify exactly one unambiguous calendar date; narrow the quote')
    if normalize_date(matches[0].group()) != normalized:
        raise ValueError('date expected differs from the date in source_quote')


def calendar_date(measurement, formal_context):
    """Validate linked same-capture Formal DOM facts, without seeing expected."""
    snapshot = measurement.get('calendar_evidence')
    if not isinstance(snapshot, dict) or snapshot.get('version') != 1:
        raise ValueError('no verifiable calendar snapshot')
    if snapshot.get('status') != 'observed' or snapshot.get('reasons'):
        raise ValueError('calendar selection/association incomplete: ' + ', '.join(snapshot.get('reasons', [])))
    provenance = snapshot.get('provenance', {})
    if not formal_context or formal_context.get('purpose') != 'formal':
        raise ValueError('independent Formal calendar context required')
    for key in ('session_id', 'browser_session_id'):
        if not formal_context.get(key) or provenance.get(key) != formal_context[key]:
            raise ValueError('calendar session differs from Formal input evidence')
    for key in ('step_id', 'observation_ref', 'screenshot_ref', 'evidence_id'):
        if not measurement.get(key) or provenance.get(key) != measurement[key]:
            raise ValueError('calendar capture does not match input measurement: ' + key)
    if provenance.get('purpose') != 'formal' or not snapshot.get('captured_at'):
        raise ValueError('calendar capture provenance incomplete')
    if measurement.get('read') != 'attribute' or measurement.get('attribute') != 'value':
        raise ValueError('calendar must accompany the input DOM value')
    input_ref = measurement.get('date_context', {}).get('input_ref')
    association, root = snapshot.get('association') or {}, snapshot.get('root') or {}
    if (not input_ref or input_ref != snapshot.get('input_ref') or input_ref != association.get('input_ref')
            or snapshot.get('input_value') != measurement.get('value')
            or association.get('root_ref') != root.get('ref') or not root.get('ref')
            or association.get('owner_count') != 1 or association.get('exact_input') is not True
            or association.get('exact_picker') is not True):
        raise ValueError('calendar is not uniquely linked to the measured input')
    if (root.get('visible') is not True or root.get('grid_visible') is not True
            or root.get('candidate_count') != 1 or not root.get('grid_ref')):
        raise ValueError('one visible calendar day grid required')
    heading = snapshot.get('heading') or {}
    text = heading.get('text', '')
    if not heading.get('ref') or not isinstance(text, str):
        raise ValueError('missing calendar month heading')
    months = ('January','February','March','April','May','June','July','August',
              'September','October','November','December')
    match = re.fullmatch(r'(' + '|'.join(months) + r')\s+([0-9]{4})', text, re.I)
    chinese = re.fullmatch(r'([0-9]{4})年\s*([0-9]{1,2})月', text)
    if match:
        month, year = next(i for i,m in enumerate(months,1) if m.lower()==match[1].lower()), int(match[2])
    elif chinese:
        year, month = int(chinese[1]), int(chinese[2])
    else:
        raise ValueError('unsupported calendar month heading')
    selected = snapshot.get('selected', [])
    if snapshot.get('selected_count') != 1 or len(selected) != 1:
        raise ValueError('exactly one selected date required')
    cell = selected[0]
    if (cell.get('visible') is not True or cell.get('disabled') is not False
            or cell.get('adjacent') is not False or not cell.get('ref')
            or not re.fullmatch(r'[0-9]{1,2}', cell.get('text', ''))):
        raise ValueError('selected cell must be a visible enabled day in the displayed month')
    actual = date(year, month, int(cell['text']))
    method = association.get('method')
    if method == 'aria-controls':
        if (association.get('controlled_id') != root.get('id') or not root.get('id')
                or cell.get('state_basis') != 'aria-selected' or cell.get('aria_selected') != 'true'
                or cell.get('role') != 'gridcell'):
            raise ValueError('ARIA association/selected state not established')
        if cell.get('data_date') and normalize_date(cell['data_date']) != actual.isoformat():
            raise ValueError('selected cell date conflicts with displayed calendar month/day')
    elif method == 'datepicker-instance':
        dates = snapshot.get('component_dates', [])
        epoch = int(datetime(year, month, actual.day, tzinfo=timezone.utc).timestamp() * 1000)
        if (cell.get('state_basis') != 'component-dates-and-active-day'
                or not {'day','active'} <= set(cell.get('class', '').split())
                or len(dates) != 1 or type(dates[0]) not in {int,float}
                or dates[0] != epoch or str(epoch) != cell.get('data_date')):
            raise ValueError('component Date state does not confirm the active day cell')
    else:
        raise ValueError('unsupported calendar/input association')
    return actual.isoformat()


def slash_date_candidates(raw):
    """Both permitted interpretations, independent of the frozen expected value."""
    if not isinstance(raw, str) or not re.fullmatch(r'[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}', raw):
        return set()
    first, second, year = map(int,raw.split('/'))
    values = set()
    for month, day in ((first,second),(second,first)):
        try:
            values.add(date(year,month,day).isoformat())
        except ValueError:
            pass
    return values


def compare_date(expected, measurement, *, formal_context=None):
    """Return tri-state equality and auditable interpretation of a raw measurement.

    The caller must first establish independent Formal provenance and completeness.
    date_context is added by the trusted collector, never accepted in bindings.
    """
    raw = measurement.get('value')
    context = measurement.get('date_context', {})
    details = dict(expected=expected, raw_actual=raw, normalized_actual=None,
                   format=None, basis=None, dom_context=context)
    if 'calendar_evidence' in measurement:
        details['calendar_evidence'] = measurement['calendar_evidence']
    try:
        canonical = expected_date(expected)
        if not isinstance(raw, str):
            raise ValueError('missing or non-string date measurement')
        native = (context.get('tag') == 'input' and context.get('type') == 'date'
                  and measurement.get('read') == 'attribute' and measurement.get('attribute') == 'value')
        formats = set()
        if native:
            formats.add('YYYY-MM-DD')
            details['basis'] = 'input[type=date] DOM value (not localized display)'
        else:
            attrs = context.get('format_attributes', {})
            for name in ('data-date-format', 'data-format', 'placeholder'):
                hint = attrs.get(name)
                if isinstance(hint, str) and hint.strip().upper() in FORMATS:
                    formats.add(hint.strip().upper())
            if len(formats) > 1:
                raise ValueError('conflicting DOM date formats')
            if formats:
                details['basis'] = 'explicit DOM format attributes'
        if formats:
            fmt = next(iter(formats))
            details['format'] = fmt
            pattern = ISO if fmt == 'YYYY-MM-DD' else r'[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}'
            if not re.fullmatch(pattern, raw):
                raise ValueError('raw date does not match the observed format')
            if fmt == 'YYYY-MM-DD':
                actual = normalize_date(raw)
            else:
                first, second, year = map(int, raw.split('/'))
                month, day = (first, second) if fmt == 'MM/DD/YYYY' else (second, first)
                actual = date(year, month, day).isoformat()
        else:
            try:
                actual = normalize_date(raw)
                details.update(format='YYYY-MM-DD' if re.fullmatch(ISO, raw) else 'Chinese year/month/day',
                               basis='self-describing calendar date')
            except ValueError:
                actual = calendar_date(measurement, formal_context)
                details['calendar_actual'] = actual
                candidates = slash_date_candidates(raw)
                details['input_date_candidates'] = sorted(candidates)
                if actual not in candidates:
                    raise ValueError('selected calendar date conflicts with input value')
                details.update(basis='linked Formal calendar selected state',format='calendar-disambiguated')
        # Known format remains primary. A second reliably established source
        # disagreeing with it is a conflict, never an opportunity to choose PASS.
        if details['basis'] != 'linked Formal calendar selected state' and 'calendar_evidence' in measurement:
            try:
                selected_date = calendar_date(measurement, formal_context)
            except (ValueError,TypeError,KeyError,AttributeError):
                selected_date = None
            if selected_date is not None and selected_date != actual:
                details['calendar_actual'] = selected_date
                raise ValueError('calendar selected date conflicts with explicitly interpreted input date')
        details['normalized_actual'] = actual
        return actual == canonical, details
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        details['reason'] = str(exc)
        return None, details
