#!/usr/bin/env python3
"""Convert 《彻底搞定 C 指针》 PDF into well-formatted Markdown.

Pipeline: pdftohtml -xml (font/position aware XML) -> line grouping
(baseline overlap) -> font-based classification (H1/H2/body/code/diagram)
-> block assembly -> Markdown with inline code/bold spans, fenced code
blocks, pixel-accurate ASCII-diagram re-projection and an auto TOC.

Usage: python3 pdf2md.py [input.pdf] [output.md]

Requires poppler's pdftohtml. The heuristics (font sizes, indent
thresholds, title-page skip, hanging-indent notes section) were tuned
for this specific book; adapt them for other PDFs.

The intermediate XML from pdftohtml encodes every text fragment with
font id, size, color and pixel position. Layout rules used here:

- color #c0c0c0 fonts are Word "shadow" duplicates and are dropped
- SimHei / large sizes            -> headings (# / ##)
- Courier dominant                -> code blocks (indent from pixel x)
- inline Courier runs             -> `code` spans
- SimHei / bold fonts inline      -> **bold** spans
- size-16 lines with box-drawing  -> ASCII diagrams, re-projected onto
  a monospace character grid (the book mixes proportional Times with
  Courier, so verbatim joining would misalign; word spaces are
  re-enforced semantically after char placement)
- CJK comments that wrap across PDF lines are absorbed back into their
  code block via a /* ... */ state machine
- hard-wrapped paragraphs are joined across page breaks using indent,
  blank-line and sentence-final-punctuation heuristics
"""
import re, html, unicodedata, statistics, subprocess, sys, tempfile, os
from collections import Counter

# ---------------- parsing ----------------

def parse(xml_path):
    data = open(xml_path, encoding='utf-8').read()
    fonts = {int(m[0]): (int(m[1]), m[2], m[3]) for m in re.findall(
        r'<fontspec id="(\d+)" size="(\d+)" family="([^"]*)" color="([^"]*)"/>', data)}
    pages = []
    for pm in re.finditer(r'<page number="(\d+)"[^>]*>(.*?)</page>', data, re.S):
        num, body = int(pm.group(1)), pm.group(2)
        els = []
        for tm in re.finditer(r'<text top="(\d+)" left="(\d+)" width="(\d+)" height="(\d+)" font="(\d+)">(.*?)</text>', body, re.S):
            t, l, w, h, f, c = tm.groups()
            els.append({'top': int(t), 'left': int(l), 'width': int(w),
                        'height': int(h), 'font': int(f), 'raw': c})
        pages.append({'num': num, 'els': els})
    return fonts, pages

TAG = re.compile(r'<(/?)([bi])>')

def element_runs(el):
    """Split element content into (text, bold, italic) runs with interpolated x."""
    c = re.sub(r'<a[^>]*>|</a>', '', el['raw'])
    pos, bold, ital, parts = 0, False, False, []
    for m in TAG.finditer(c):
        if m.start() > pos:
            parts.append((html.unescape(c[pos:m.start()]), bold, ital))
        if m.group(2) == 'b':
            bold = (m.group(1) != '/')
        else:
            ital = (m.group(1) != '/')
        pos = m.end()
    if pos < len(c):
        parts.append((html.unescape(c[pos:]), bold, ital))

    def w(t): return sum(2 if is_wide(ch) else 1 for ch in t)
    total = sum(w(p[0]) for p in parts) or 1
    x = el['left']
    out = []
    for t, b, i in parts:
        ww = w(t)
        rw = el['width'] * ww / total
        out.append({'text': t, 'bold': b, 'ital': i, 'x': x, 'w': rw, 'font': el['font']})
        x += rw
    return out

def is_wide(ch):
    if unicodedata.east_asian_width(ch) in 'WF':
        return True
    return ch in '，。；：！？、（）【】《》「」『』＝｜'

# ---------------- line grouping ----------------

def group_lines(els):
    lines = []
    for e in sorted(els, key=lambda x: (x['top'], x['left'])):
        placed = False
        for ln in lines:
            tol = max(6, int(min(ln['h'], e['height']) * 0.35))
            if abs(ln['top'] - e['top']) <= tol:
                ln['els'].append(e); placed = True
                ln['h'] = max(ln['h'], e['height'])
                break
        if not placed:
            lines.append({'top': e['top'], 'h': e['height'], 'els': [e]})
    for ln in lines:
        ln['els'].sort(key=lambda x: x['left'])
    lines.sort(key=lambda x: x['top'])
    return lines

# ---------------- classification ----------------

def line_info(ln, fonts):
    runs = []
    for e in ln['els']:
        runs.extend(element_runs(e))
    vis = ''.join(r['text'] for r in runs)
    stripped = re.sub(r'\s+', '', vis)
    cnt = Counter()
    for r in runs:
        sz, fam, col = fonts[r['font']]
        cnt[(sz, fam, col)] += sum(2 if is_wide(c) else 1 for c in r['text'])
    if not cnt or not stripped:
        return None
    (dsize, dfam, dcol), _ = cnt.most_common(1)[0]
    x0 = None
    for r in runs:
        t = r['text'].lstrip()
        if t:
            lead = len(r['text']) - len(t)
            cw = r['w'] / (sum(2 if is_wide(c) else 1 for c in r['text']) or 1)
            x0 = r['x'] + lead * cw
            break
    return {'runs': runs, 'text': vis, 'stripped': stripped, 'x0': x0,
            'dsize': dsize, 'dfam': dfam, 'dcol': dcol, 'top': ln['top']}

def classify(info, fonts):
    fam, sz, txt = info['dfam'], info['dsize'], info['stripped']
    if fam == 'TimesNewRomanPSMT' and sz == 14 and re.fullmatch(r'\d{1,3}', txt):
        return 'footer'          # page numbers
    is_courier = 'Courier' in fam
    bold_cjk = fam.endswith('SimHei')
    if sz >= 30:
        return 'h1'              # 篇 titles, front-matter headings
    if bold_cjk and sz == 27:
        return 'end'             # /* -- 完 -- */
    if sz in (21, 24) and (bold_cjk or 'Arial' in fam or 'Times' in fam or is_courier):
        return 'h2'              # numbered sections
    if is_courier and sz == 18:
        # a line dominated by Courier but starting with CJK is body text
        # (e.g. "情况二：const int *pi指针指向…") — absorption handles real comments
        if re.match(r'[\u3000-\u9fff\uff00-\uffef]', info['text'].lstrip()):
            return 'body'
        return 'code'
    if sz == 16:
        if re.search(r'[|┌┐└┘├┤→←↑↓]{2,}|-{6,}|={3,}|···|··', info['text']):
            return 'diagram'
        return 'body16'          # small-font notes section (hanging indent)
    return 'body'

# ---------------- helpers ----------------

def charw(run):
    return run['w'] / (sum(2 if is_wide(c) else 1 for c in run['text']) or 1)

def boundary_join(a, b):
    if not a: return b
    if not b: return a
    x, y = a[-1], b[0]
    if is_wide(x) or is_wide(y) or x in '…·' or y in '…·':
        return a + b
    return a + ' ' + b

SENT_END = '。！？…”』」）】'

def fmt_span(text, code, bold):
    t = text.strip(' ')
    if not t:
        return text
    lead = text[:len(text) - len(text.lstrip(' '))]
    trail = text[len(text.rstrip(' ')):]
    s = t.replace('`', "'")
    if code:
        s = f'`{s}`'
    if bold:
        s = f'**{s}**'
    return lead + s + trail

def run_style(r, fonts):
    fam = fonts[r['font']][1]
    code = 'Courier' in fam
    bold = r['bold'] or fam.endswith('SimHei') or fam in ('TimesNewRomanPS', 'CourierNewPS', 'Arial')
    return code, bold

def format_runs(runs, fonts, size):
    """Format a run list (one paragraph) into markdown text."""
    unit = size * 0.55
    # merge adjacent same-style runs
    merged = []
    for r in runs:
        if not r['text'].strip():
            continue
        code, bold = run_style(r, fonts)
        if merged and merged[-1][1] == code and merged[-1][2] == bold:
            gap = r['x'] - (merged[-1][0]['x'] + merged[-1][0]['w'])
            n = round(gap / unit) if gap > 0 else 0
            a = merged[-1][0]['text'][-1]
            b = r['text'][0]
            cjk = is_wide(a) or is_wide(b)
            sep = ' ' * n if (n >= 1 and not (cjk and n < 2) and a != ' ' and b != ' ') else ''
            if a == ' ' and b != ' ' and n >= 2:
                sep = ' ' * (n - 1)
            merged[-1][0] = {'text': merged[-1][0]['text'] + sep + r['text'],
                             'x': merged[-1][0]['x'], 'bold': r['bold'], 'ital': r['ital'],
                             'font': r['font'],
                             'w': r['x'] + r['w'] - merged[-1][0]['x']}
        else:
            merged.append([dict(r), code, bold])
    out = ''
    prev = None
    for r, code, bold in merged:
        if prev is not None:
            gap = r['x'] - (prev['x'] + prev['w'])
            n = round(gap / unit) if gap > 0 else 0
            a = out[-1] if out else ''
            b = r['text'][0]
            cjk = is_wide(a) or is_wide(b)
            if n >= 1 and not (cjk and n < 2):
                out += ' '
        out += fmt_span(r['text'], code, bold)
        prev = r
    out = out.replace('\u3000', '')
    out = re.sub(r' +([。，；：！？、）】》」』])', r'\1', out)
    out = re.sub(r'([（【《「『]) +', r'\1', out)
    out = re.sub(r'\*\*\s*\*\*', '', out)
    out = re.sub(r'` `', ' ', out)
    # justification artifacts: spaces between CJK chars/puncts
    CJKCLS = r'\u3000-\u9fff\uff00-\uffef“”‘’①②③④⑤⑥⑦⑧⑨⑩'
    out = re.sub(rf'(?<=[{CJKCLS}]) +(?=[{CJKCLS}])', '', out)
    out = re.sub(r' {2,}', ' ', out)
    return out.strip()

def merge_code_line(runs, unit):
    out = ''
    prev = None
    for r in runs:
        if not r['text']:
            continue
        if prev is not None:
            gap = r['x'] - (prev['x'] + prev['w'])
            n = round(gap / unit) if gap > 0 else 0
            if n >= 1:
                out += ' ' * n
        out += r['text']
        prev = r
    return out

def diagram_line_md(info, base, unit):
    """Place each char at its pixel-derived column; wide chars render 2 cols.

    Mixed proportional fonts (Times walls vs Courier contents) quantize
    imprecisely, so word spaces are enforced semantically afterwards: the
    tail of the line is shifted right until every space has its own column.
    """
    grid = {}  # col -> (char, target_col_float)

    def place(ch, t):
        col = round(t)
        if col not in grid or grid[col][0] == ' ':
            grid[col] = (ch, t)
            return col
        if grid[col][0] == ch:
            return None  # duplicate merges (dashes etc.)
        # real collision: move existing char left or new char right,
        # whichever introduces less position error
        left_ok = col > 0 and (col - 1 not in grid or grid[col - 1][0] == ' ')
        err_left = abs(grid[col][1] - (col - 1)) if left_ok else 1e9
        k = 1
        while col + k in grid and grid[col + k][0] not in (' ', ch) and k < 3:
            k += 1
        rc = col + k
        right_ok = rc not in grid or grid[rc][0] == ' '
        err_right = abs(t - rc) if right_ok else 1e9
        if err_left < err_right:
            grid[col - 1] = grid[col]
            grid[col] = (ch, t)
            return col
        if right_ok:
            grid[rc] = (ch, t)
            return rc
        return None

    pos = {}        # (run_idx, char_idx) -> placed col
    anchors = []    # (left_key, right_key): a space must fit between them
    last_key, pending_space = None, False
    prev_end = None
    for ri, r in enumerate(info['runs']):
        if prev_end is not None and r['x'] - prev_end > unit * 0.5:
            pending_space = True
        cw = charw(r)
        x = r['x']
        for ci, ch in enumerate(r['text']):
            if ch == ' ':
                if last_key is not None and cw >= unit * 0.5:
                    pending_space = True
            else:
                col = place(ch, (x - base) / unit)
                if col is not None:
                    key = (ri, ci)
                    pos[key] = col
                    if pending_space and last_key is not None:
                        anchors.append((last_key, key))
                        pending_space = False
                    last_key = key
            x += cw * (2 if is_wide(ch) else 1)
        prev_end = max(prev_end or 0, r['x'] + cw * sum(2 if is_wide(c) else 1 for c in r['text']))

    # enforce word separations
    for lk, rk in anchors:
        lc, rc = pos[lk], pos[rk]
        if rc <= lc + 1:
            shift = lc + 2 - rc
            grid = {c + shift if c >= rc else c: v for c, v in grid.items()}
            pos = {k: v + shift if v >= rc else v for k, v in pos.items()}

    if not grid:
        return ''
    out, cur = [], 0
    for col in sorted(grid):
        ch = grid[col][0]
        while cur < col:
            out.append(' '); cur += 1
        out.append(ch)
        cur += 2 if is_wide(ch) else 1
    return ''.join(out).rstrip()

# ---------------- main ----------------

def build(xml_path, out_path):
    fonts, pages = parse(xml_path)
    all_lines = []
    for pg in pages:
        els = [e for e in pg['els'] if fonts[e['font']][2] != '#c0c0c0']
        for ln in group_lines(els):
            info = line_info(ln, fonts)
            if info is None:
                continue
            t = classify(info, fonts)
            if not info['stripped']:
                # whitespace-only: remember code blanks
                t = 'codeblank' if 'Courier' in info['dfam'] and info['dsize'] == 18 else 'blank'
            info['page'] = pg['num']
            info['type'] = t
            all_lines.append(info)

    # drop title-page lines before 前言, drop TOC page 2
    lines = []
    started = False
    for li in all_lines:
        if li['page'] == 1:
            if li['stripped'] == '前言':
                started = True
            if not started:
                continue
        if li['page'] == 2:
            continue
        lines.append(li)
    n = len(lines)

    # diagram adjacency: body16 next to diagram -> diagram
    for i, li in enumerate(lines):
        if li['type'] != 'body16':
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < n and lines[j]['type'] == 'diagram' and lines[j]['page'] == li['page'] \
               and abs(lines[j]['top'] - li['top']) <= 45:
                li['type'] = 'diagram'
                break

    # demote isolated pseudo-code lines that are really wrapped body text
    def nb0(i, step):
        j = i + step
        while 0 <= j < n and lines[j]['type'] in ('blank', 'codeblank', 'footer'):
            j += step
        return j if 0 <= j < n else None
    for i, li in enumerate(lines):
        if li['type'] != 'code':
            continue
        pi, ni = nb0(i, -1), nb0(i, 1)
        if (pi is not None and lines[pi]['type'] == 'code') or \
           (ni is not None and lines[ni]['type'] == 'code'):
            continue  # part of a real code block
        if pi is not None and lines[pi]['type'] == 'body':
            prev_txt = lines[pi]['text'].rstrip()
            if prev_txt and prev_txt[-1] not in SENT_END + '：':
                t = li['text']
                if re.search(r'[\u3000-\u9fff\uff00-\uffef“”‘’]', t) or not re.search(r'[;{}]', t):
                    li['type'] = 'body'

    # code absorption with comment-state tracking
    def nb(i, step):
        j = i + step
        while 0 <= j < n and lines[j]['type'] in ('blank', 'codeblank', 'footer'):
            j += step
        return j if 0 <= j < n else None

    def comment_state(text, state):
        i = 0
        while i < len(text) - 1:
            if not state and text[i:i+2] == '/*':
                state = True; i += 2
            elif state and text[i:i+2] == '*/':
                state = False; i += 2
            else:
                i += 1
        return state

    in_comment = False
    for i, li in enumerate(lines):
        if li['type'] == 'code':
            in_comment = comment_state(li['text'], in_comment)
            continue
        if li['type'] != 'body':
            in_comment = False
            continue
        pi, ni = nb(i, -1), nb(i, 1)
        prev_code = pi is not None and lines[pi]['type'] == 'code'
        next_code = ni is not None and lines[ni]['type'] == 'code'
        t = li['text']
        first = li['runs'][0] if li['runs'] else None
        first_txt = first['text'].strip() if first else ''
        first_courier = (first is not None and 'Courier' in fonts[first['font']][1]
                         and len(first_txt) >= 4 and re.search(r'[;=(){}*&#]', first_txt)
                         and (li['x0'] or 0) >= 150)
        absorb = False
        if in_comment and prev_code:
            absorb = True                       # continuation of an open /* ... */
        elif t.lstrip().startswith('/*') and (prev_code or next_code):
            absorb = True
        elif first_courier and (prev_code or next_code):
            absorb = True                       # code line with trailing CJK comment
        elif t.rstrip().endswith('*/') and prev_code:
            absorb = True
        if absorb:
            li['type'] = 'code'
            in_comment = comment_state(t, in_comment)
        else:
            in_comment = False

    # ---- assemble blocks ----
    blocks = []
    i = 0
    while i < n:
        li = lines[i]
        t = li['type']
        if t in ('blank', 'codeblank', 'footer'):
            i += 1
            continue
        if t in ('h1', 'h2'):
            txt = li['text'].strip()
            j = i + 1
            while j < n and lines[j]['type'] == t and lines[j]['page'] == li['page'] \
                  and lines[j]['top'] - lines[j - 1]['top'] < 90:
                txt = boundary_join(txt, lines[j]['text'].strip())
                j += 1
            blocks.append((t, re.sub(r'\s+', ' ', txt)))
            i = j
        elif t == 'body':
            para = [li]
            j = i + 1
            while j < n:
                cur = lines[j]
                if cur['type'] == 'footer':
                    j += 1
                    continue
                if cur['type'] == 'blank':
                    k = j + 1
                    while k < n and lines[k]['type'] in ('blank', 'footer'):
                        k += 1
                    if k < n and lines[k]['type'] == 'body' and lines[k]['page'] == cur['page']:
                        break  # real same-page paragraph separator
                    j += 1
                    continue
                if cur['type'] != 'body':
                    break
                cross = cur['page'] != para[-1]['page']
                newp = (cur['x0'] or 0) >= 150
                if cross:
                    prev_txt = para[-1]['text'].rstrip()
                    if newp or (prev_txt and prev_txt[-1] in SENT_END):
                        break
                else:
                    gap = cur['top'] - para[-1]['top']
                    prev_blank = lines[j - 1]['type'] == 'blank'
                    if newp or prev_blank or gap > 52:
                        break
                para.append(cur)
                j += 1
            blocks.append(('para', assemble_para(para, fonts, 18)))
            i = j
        elif t == 'body16':
            para = [li]
            j = i + 1
            while j < n and lines[j]['type'] == 'body16':
                cur = lines[j]
                x0 = cur['x0'] or 0
                is_item = bool(re.match(r'[A-D]\d+\s*\.', cur['text'].lstrip()))
                is_bullet = cur['text'].lstrip().startswith(('-', '－'))
                prev = para[-1]
                prev_blank = lines[j - 1]['type'] == 'blank'
                prev_ends = prev['text'].rstrip()[-1:] in SENT_END
                if is_item or is_bullet or (x0 <= 160 and (prev_blank or prev_ends)):
                    break
                para.append(cur)
                j += 1
            blocks.append(('para16', assemble_para(para, fonts, 16)))
            i = j
        elif t == 'code':
            blk = [li]
            j = i + 1
            while j < n:
                tj = lines[j]['type']
                if tj == 'code':
                    blk.append(lines[j]); j += 1
                    continue
                # look past page-edge artifacts (footers / cross-page blanks)
                k = j
                while k < n and lines[k]['type'] in ('blank', 'codeblank', 'footer'):
                    k += 1
                if k < n and lines[k]['type'] == 'code' and \
                   (tj == 'footer' or lines[k]['page'] != lines[j]['page'] or tj == 'codeblank'):
                    j = k
                    continue
                break
            units = [charw(r) for l in blk for r in l['runs']
                     if 'Courier' in fonts[r['font']][1] and len(r['text'].strip()) >= 4]
            unit = statistics.median(units) if units else 10.8
            xs = [l['x0'] for l in blk if l['x0'] is not None]
            base = min(xs) if xs else 0
            out = []
            for l in blk:
                txt = merge_code_line(l['runs'], unit).rstrip()
                if l['x0'] is not None:
                    ind = max(0, round((l['x0'] - base) / unit))
                    txt = ' ' * ind + txt.lstrip()
                out.append(txt.rstrip())
            while out and not out[0].strip(): out.pop(0)
            while out and not out[-1].strip(): out.pop()
            blocks.append(('code', out))
            i = j
        elif t == 'diagram':
            blk = [li]
            j = i + 1
            while j < n and lines[j]['type'] == 'diagram':
                blk.append(lines[j]); j += 1
            units = [charw(r) for l in blk for r in l['runs']
                     if 'Courier' in fonts[r['font']][1] and len(r['text'].strip()) >= 4]
            unit = statistics.median(units) if units else 8.8
            base = min(r['x'] for l in blk for r in l['runs'])
            out = [diagram_line_md(l, base, unit) for l in blk]
            while out and not out[0].strip(): out.pop(0)
            while out and not out[-1].strip(): out.pop()
            blocks.append(('diagram', out))
            i = j
        elif t == 'end':
            blocks.append(('end', li['text'].strip()))
            i += 1
        else:
            i += 1

    # ---- emit ----
    md = ['# 彻底搞定 C 指针（完全版·修订增补版）\n',
          '> 著＝姚云飞　修订＝丁正宇\n', '## 目录\n']
    for kind, payload in blocks:
        if kind == 'h1':
            md.append(f'- [{payload}](#{slug(payload)})')
        elif kind == 'h2':
            md.append(f'  - [{payload}](#{slug(payload)})')
    md.append('\n---\n')
    for kind, payload in blocks:
        if kind == 'h1':
            md.append(f'\n# {payload}\n')
        elif kind == 'h2':
            md.append(f'\n## {payload}\n')
        elif kind == 'para':
            md.append(payload + '\n')
        elif kind == 'para16':
            txt = payload
            txt = re.sub(r'^([-－]+)\s+', r'\1 ', txt)
            md.append(txt + '\n')
        elif kind == 'code':
            lang = 'c' if re.search(r'[;{}#]', '\n'.join(payload)) else ''
            md.append(f'```{lang}')
            md.extend(payload)
            md.append('```\n')
        elif kind == 'diagram':
            md.append('```')
            md.extend(payload)
            md.append('```\n')
        elif kind == 'end':
            md.append('---\n')
            md.append('`' + payload + '`\n')
    text = re.sub(r'\n{3,}', '\n\n', '\n'.join(md))
    open(out_path, 'w', encoding='utf-8').write(text)
    print(f'wrote {out_path}: {len(text)} chars, {len(blocks)} blocks')
    print(Counter(k for k, _ in blocks))

def assemble_para(para, fonts, size):
    """Concatenate line runs into one run list, merging across line boundaries."""
    runs = []
    for idx, li in enumerate(para):
        lruns = [dict(r) for r in li['runs']]
        if not lruns:
            continue
        # strip leading/trailing whitespace of the line
        first = lruns[0]
        lead = len(first['text']) - len(first['text'].lstrip(' '))
        if lead:
            cw = charw(first)
            first['text'] = first['text'].lstrip(' ')
            first['x'] += lead * cw
            first['w'] -= lead * cw
        last = lruns[-1]
        tl = len(last['text'].rstrip(' '))
        if tl < len(last['text']):
            cw = charw(last)
            last['w'] -= (len(last['text']) - tl) * cw
            last['text'] = last['text'].rstrip(' ')
        if not runs:
            runs.extend(lruns)
            continue
        prev = runs[-1]
        cur = lruns[0]
        ps, cs = run_style(prev, fonts), run_style(cur, fonts)
        if ps == cs:
            prev['text'] = boundary_join(prev['text'], cur['text'])
            prev['w'] = cur['x'] + cur['w'] - prev['x']
            runs.extend(lruns[1:])
        else:
            cur['x'] = prev['x'] + prev['w']
            runs.extend(lruns)
    return format_runs(runs, fonts, size)

def slug(t):
    t = t.strip().lower()
    return ''.join('-' if ch == ' ' else ch for ch in t if ch == ' ' or re.match(r'\w', ch, re.UNICODE))

def main():
    pdf = sys.argv[1] if len(sys.argv) > 1 else '彻底搞定 C 指针.pdf'
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(os.path.basename(pdf))[0].replace(' ', '') + '.md'
    with tempfile.TemporaryDirectory() as td:
        prefix = os.path.join(td, 'doc')
        subprocess.run(['pdftohtml', '-xml', '-i', pdf, prefix],
                       check=True, capture_output=True)
        build(prefix + '.xml', out_path)

if __name__ == '__main__':
    main()
