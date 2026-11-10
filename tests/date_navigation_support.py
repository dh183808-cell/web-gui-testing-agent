"""Hand-authored navigation example for tests, NOT an Agent3/site template.

Only test fixtures use these locators/heading strings. The application changes are
prompt constraints; this example proves they are executable in the existing subset.
"""
from calendar import month_name


def title(year, month):
    return f'{month_name[month]} {year}'


def target_heading_locator(year, month):
    return f'//*[@id="month" and normalize-space(.)="{title(year, month)}"]'


def navigation_code(target=(2026, 11), *, limit=36, timeout=.6):
    # Exact full headings, with a finite supported range. No model code arithmetic,
    # string parsing, functions, custom predicates or exceptions are required.
    states = []
    for index in range(2025 * 12 + 10, 2027 * 12 + 2):
        year, zero_month = divmod(index, 12)
        next_year, next_month = divmod(index + 1, 12)
        prev_year, prev_month = divmod(index - 1, 12)
        states.append([title(year, zero_month + 1), year, zero_month + 1,
                       title(next_year, next_month + 1), title(prev_year, prev_month + 1)])
    return f'''states = {states!r}
target = {target!r}
current_heading = driver.find_element(By.ID, "month").text.strip()
parsed_heading = [state for state in states if state[0] == current_heading]
current = parsed_heading[0]
for turn in {list(range(limit))!r}:
    if (current[1], current[2]) != target:
        if (current[1], current[2]) < target:
            expected_heading = current[3]
            WebDriverWait(driver, {timeout}).until(EC.element_to_be_clickable((By.ID, "next"))).click()
        else:
            expected_heading = current[4]
            WebDriverWait(driver, {timeout}).until(EC.element_to_be_clickable((By.ID, "prev"))).click()
        WebDriverWait(driver, {timeout}).until(EC.text_to_be_present_in_element((By.ID, "month"), expected_heading))
        observed_heading = driver.find_element(By.ID, "month").text.strip()
        confirmed_transition = [state for state in states if state[0] == observed_heading and state[0] == expected_heading]
        current = confirmed_transition[0]
WebDriverWait(driver, {timeout}).until(EC.visibility_of_element_located((By.XPATH, {target_heading_locator(*target)!r})))'''


def selection_code(target=(2026, 11), *, timeout=.6):
    return f'''WebDriverWait(driver, {timeout}).until(EC.visibility_of_element_located((By.XPATH, {target_heading_locator(*target)!r})))
WebDriverWait(driver, {timeout}).until(EC.element_to_be_clickable((By.ID, "day10"))).click()'''


def program(target=(2026, 11), *, limit=36, timeout=.6):
    return ('# webagent-step: S1\n'
        'driver.find_element(By.ID, "date").click()\n'
        'WebDriverWait(driver, 2).until(EC.visibility_of_element_located((By.ID, "calendar")))\n\n'
        '# webagent-step: S2\n' + navigation_code(target, limit=limit, timeout=timeout) + '\n\n'
        '# webagent-step: S3\n' + selection_code(target, timeout=timeout) + '\n\n'
        '# webagent-step: S4\ndriver.title\n')


def local_page(start, *, stuck=False):
    # Reuses the existing ARIA evidence path. This is an interactive local fixture;
    # it is not a simulation of Bootstrap-datepicker's instance structure.
    return '''<!doctype html><meta charset="utf-8"><title>Local cross-year calendar</title>
<label for="date">Calendar date</label><input id="date" aria-controls="calendar"
 onclick="document.querySelector('#calendar').hidden=false">
<div id="calendar" hidden><h2 id="month"></h2>
<button id="prev" onclick="move(-1)">Previous month</button>
<button id="next" onclick="move(1)">Next month</button>
<table role="grid" aria-labelledby="month"><tbody id="days"></tbody></table></div>
<div id="moves"></div><script>
let year=YEAR, month=MONTH, selected=null;
const names=['January','February','March','April','May','June','July','August','September','October','November','December'];
const heading=document.querySelector('#month'), days=document.querySelector('#days');
function render() {
 heading.textContent=names[month-1]+' '+year;
 days.replaceChildren(); const tr=document.createElement('tr'); days.append(tr);
 for(let day=1;day<=new Date(Date.UTC(year,month,0)).getUTCDate();day++) {
  const cell=document.createElement('td'); cell.id='day'+day; cell.textContent=day;
  cell.setAttribute('role','gridcell'); cell.setAttribute('aria-selected',String(day===selected));
  cell.onclick=()=>{selected=day; document.querySelector('#date').value=month+'/'+String(day).padStart(2,'0')+'/'+year; render();};
  tr.append(cell);
 }
}
function move(delta) {
 const row=document.createElement('span'); row.textContent=String(delta); document.querySelector('#moves').append(row);
 if(STUCK) return;
 setTimeout(()=>{const date=new Date(Date.UTC(year,month-1+delta,1));
  year=date.getUTCFullYear();month=date.getUTCMonth()+1;selected=null;render();},80);
}
render();</script>'''.replace('YEAR', str(start[0])).replace('MONTH', str(start[1])).replace('STUCK', str(stuck).lower())
