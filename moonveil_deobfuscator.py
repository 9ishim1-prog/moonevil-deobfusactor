#!/usr/bin/env python3
"""
MoonVeil (Luau/Lua) static deobfuscator.

Pipeline:
  1. Resolve self-referential identity tables:  x={[2]=1,[1]=x};x[3]=x
     and lookups of the form  t[N][3][t[N][2]]  ->  t[N]
  2. Constant-fold pure numeric expressions  (a+b, a-b, a*b, a/b, a%b, -a, a+-b)
  3. Evaluate  d = <cond> and X or Y  branches when <cond> is statically known
  4. Decode Lua string escapes into readable text
  5. Beautify the flattened state machine into indented, one-statement-per-line code
  6. Emit a report of string-cache calls (t:Q/R/S/T/V/X / t.O[...]) for dynamic follow-up

Usage:
  python3 moonveil_deobfuscator.py input.lua [-o output.lua] [--report report.txt]
"""
import argparse
import re
import sys

# ---------------------------------------------------------------- helpers

NUM = r'-?\d+(?:\.\d+)?'


def fold_arith(expr):
    """Fold a pure numeric expression with Lua-ish semantics. Returns float/int or None."""
    if not re.fullmatch(r'[\d\s\.\+\-\*/%\(\)]+', expr):
        return None
    # Lua: ^ not handled here (rare in flattened code); % is floored modulo
    def repl(m):
        inner = fold_arith(m.group(0)[1:-1])
        return repr(inner) if inner is not None else m.group(0)
    prev = None
    cur = expr
    # resolve innermost parens
    while prev != cur:
        prev = cur
        cur = re.sub(r'\(([^()]+)\)', lambda m: (repr(fold_arith(m.group(1)))
                     if fold_arith(m.group(1)) is not None else m.group(0)), cur)
    if not re.fullmatch(r'[\d\s\.\+\-\*/%]+', cur):
        return None
    # safe eval: numbers and +-*/% only; Python semantics match Lua here for
    # positive operands (integer division differs but obfuscator code uses
    # exact divisors, so results are integral anyway)
    try:
        val = eval(cur, {'__builtins__': {}}, {})
    except Exception:
        return None
    if isinstance(val, float) and val.is_integer():
        return int(val)
    return val


def lua_unescape(s):
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '\\' and i + 1 < len(s):
            n = s[i + 1]
            if n.isdigit():
                j = i + 1
                while j < len(s) and j < i + 4 and s[j].isdigit():
                    j += 1
                out.append(chr(int(s[i + 1:j])))
                i = j
                continue
            m = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', '"': '"', "'": "'", '0': '\0'}
            out.append(m.get(n, n))
            i += 2
        else:
            out.append(ch)
            i += 1
    return ''.join(out)

# ---------------------------------------------------------------- passes


def resolve_identity_tables(src):
    """x={[2]=1,[1]=x};x[3]=x  ==>  (marker that x[i] == x) ; then t[N][3][t[N][2]] -> t[N]."""
    names = set()
    pat = re.compile(r'(\w+)=\{\[2\]=1,\[1\]=\1\};\1\[3\]=\1;?')
    for m in pat.finditer(src):
        names.add(m.group(1))
    src = pat.sub('', src)
    # lookups i[N][3][i[N][2]] -> i[N]
    src = re.sub(r'(\w+)(\[\w+\])\[3\]\[\1\2\[2\]\]', r'\1\2', src)
    return src, names


def fold_constants(src):
    """Fold things like  d=486-d  is not foldable, but  52819/d  where the RHS is pure numbers is."""
    def try_fold(m):
        v = fold_arith(m.group(0))
        return repr(v) if v is not None else m.group(0)
    # repeatedly fold binary ops between literals
    pat = re.compile(NUM + r'\s*[\+\-\*/%]\s*' + r'\+?-?' + NUM.strip('-?') + r'(?:\.\d+)?')
    prev = None
    while prev != src:
        prev = src
        src = re.sub(NUM + r'\+-' + NUM[2:], lambda m: repr(fold_arith(m.group(0))), src)
        src = re.sub(r'(?<![\w\.])(' + NUM + r'\s*[\+\*/%]\s*' + NUM + r')(?![\w\.])',
                     try_fold, src)
    return src


def fold_and_or(src):
    """d = true_const and A or B  ==>  d = A   (when LHS of `and` is a nonzero number)."""
    def repl(m):
        cond, a, b = m.group(1), m.group(2), m.group(3)
        v = fold_arith(cond)
        if v is None:
            return m.group(0)
        return a if v != 0 else b
    return re.sub(r'([\d\.\+\-\*/%\(\)]+)\s+and\s+([^o][^;]*?)\s+or\s+([^;]+?)(?=[;\s]|$)',
                  repl, src)


def decode_strings(src):
    def repl(m):
        body = m.group(1)
        dec = lua_unescape(body)
        if all(32 <= ord(c) < 127 or c in '\n\t' for c in dec):
            return '"%s"' % dec.replace('\\', '\\\\').replace('"', '\\"')
        return m.group(0)
    return re.sub(r'"((?:[^"\\]|\\.)*)"', repl, src)


KEYWORDS = {'then', 'else', 'elseif', 'end', 'do', 'until', 'repeat', 'function',
            'return', 'local', 'while', 'if', 'for', 'in', 'break'}
INDENT_INC = {'then', 'do', 'repeat', 'function'}


def beautify(src):
    # split on statement boundaries
    src = re.sub(r';', ';\n', src)
    src = re.sub(r'\b(then)\b', r' \1\n', src)
    src = re.sub(r'\b(elseif|else)\b', r'\n\1 ', src)
    src = re.sub(r'\b(end)\b', r'\n\1 ', src)
    src = re.sub(r'\b(repeat|do)\b', r' \1\n', src)
    lines = []
    indent = 0
    for raw in src.split('\n'):
        line = raw.strip()
        if not line:
            continue
        first = line.split(' ', 1)[0].rstrip(';')
        if first in ('end', 'elseif', 'else', 'until'):
            indent = max(0, indent - 1)
        lines.append('    ' * indent + line)
        if first in ('then', 'do', 'repeat') or line.startswith('function') or ' function(' in line:
            indent += 1
        if first == 'else' or first == 'elseif':
            indent += 1
    return '\n'.join(lines)


def build_report(src):
    calls = re.findall(r't\.([OU])\[(−?-?\d+)\]|t:([QRSTVX])\(([^)]*)\)', src)
    lines = ['Dynamic-string cache accesses (resolve by running moonveil_dynamic_dump.lua):', '']
    seen = set()
    for m in calls:
        key = m[0] + m[1] + m[2] + m[3]
        if key in seen:
            continue
        seen.add(key)
        if m[0]:
            lines.append(f'  cache table {m[0]}  key {m[1]}')
        else:
            lines.append(f'  decoder t:{m[2]}({m[3]})')
    lines.append('')
    lines.append(f'total unique encoded-string references: {len(seen)}')
    return '\n'.join(lines)

# ---------------------------------------------------------------- main


def deobfuscate(src):
    src = re.sub(r'^--.*$', '', src, flags=re.M)  # drop comment banner
    src, idents = resolve_identity_tables(src)
    src = fold_constants(src)
    src = fold_and_or(src)
    src = decode_strings(src)
    pretty = beautify(src)
    report = build_report(src)
    report = f'identity tables removed: {len(idents)} ({", ".join(sorted(idents))})\n\n' + report
    return pretty, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input')
    ap.add_argument('-o', '--output')
    ap.add_argument('--report')
    a = ap.parse_args()
    src = open(a.input, encoding='utf-8').read()
    pretty, report = deobfuscate(src)
    out = a.output or a.input.rsplit('.', 1)[0] + '.deobf.lua'
    open(out, 'w', encoding='utf-8').write(pretty + '\n')
    rep = a.report or a.input.rsplit('.', 1)[0] + '.report.txt'
    open(rep, 'w', encoding='utf-8').write(report + '\n')
    print(f'wrote {out} and {rep}')


if __name__ == '__main__':
    main()
