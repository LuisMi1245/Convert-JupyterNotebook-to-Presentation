#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from textwrap import dedent

import nbformat

TODAY = datetime.now().strftime('%Y-%m-%d')

PANDOC_ARGS = [
    'pandoc',
    '-f',
    'markdown+tex_math_dollars+tex_math_double_backslash+smart+fenced_code_blocks+backtick_code_blocks+raw_tex+implicit_figures+multiline_tables+pipe_tables+bracketed_spans+auto_identifiers',
    '-t',
    'latex',
    '--wrap=none',
]

BEAMER_PREAMBLE = r'''
\documentclass[aspectratio=169,10pt]{beamer}
\usetheme{default}
\usecolortheme{default}
\setbeamertemplate{navigation symbols}{}
\setbeamertemplate{footline}{}
\setbeamertemplate{frametitle continuation}{}
\setbeamersize{text margin left=0.60cm,text margin right=0.60cm}

\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage[spanish,es-noquoting]{babel}
\usepackage{lmodern}
\usepackage{microtype}
\usepackage{amsmath,amssymb,mathtools}
\usepackage{graphicx}
\usepackage{xcolor}
\usepackage{booktabs}
\usepackage{array}
\usepackage{ragged2e}
\usepackage{fancyvrb}
\usepackage{listings}
\usepackage{hyperref}
\hypersetup{hidelinks}

\definecolor{codebg}{RGB}{248,248,248}
\definecolor{codeframe}{RGB}{210,210,210}
\definecolor{codeblue}{RGB}{30,60,170}
\definecolor{codegreen}{RGB}{20,110,60}
\definecolor{codeorange}{RGB}{170,90,20}
\definecolor{codegray}{RGB}{120,120,120}

\lstdefinestyle{pycode}{%
  language=Python,
  basicstyle=\ttfamily\scriptsize,
  backgroundcolor=\color{codebg},
  frame=single,
  rulecolor=\color{codeframe},
  xleftmargin=0.25em,
  xrightmargin=0.25em,
  aboveskip=0.20em,
  belowskip=0.20em,
  breaklines=true,
  breakatwhitespace=true,
  columns=fullflexible,
  keepspaces=true,
  showstringspaces=false,
  showtabs=false,
  tabsize=4,
  keywordstyle=\bfseries\color{codeblue},
  commentstyle=\itshape\color{codegreen},
  stringstyle=\color{codeorange},
  numberstyle=\tiny\color{codegray},
  numbers=left,
  numbersep=6pt,
}

\lstdefinestyle{textout}{%
  basicstyle=\ttfamily\tiny,
  frame=single,
  rulecolor=\color{codeframe},
  xleftmargin=0.25em,
  xrightmargin=0.25em,
  aboveskip=0.15em,
  belowskip=0.15em,
  breaklines=true,
  breakatwhitespace=true,
  columns=fullflexible,
  keepspaces=true,
  showstringspaces=false,
}

\setlength{\parindent}{0pt}
\setlength{\parskip}{0.14em}
\setlength{\abovedisplayskip}{2.5pt plus 1pt minus 1pt}
\setlength{\belowdisplayskip}{2.5pt plus 1pt minus 1pt}
\setlength{\abovedisplayshortskip}{2pt plus 1pt minus 1pt}
\setlength{\belowdisplayshortskip}{2pt plus 1pt minus 1pt}
\setlength{\jot}{1.8pt}
\raggedbottom
\providecommand{\tightlist}{\setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}}
\providecommand{\paragraph}[1]{\vspace{0.15em}\textbf{#1}\par}
\providecommand{\subparagraph}[1]{\vspace{0.10em}\textbf{#1}\par}
\allowdisplaybreaks
'''

HEADING_RE = re.compile(r'^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$')
MD_IMAGE_RE = re.compile(r'!\[(?P<alt>[^\]]*)\]\((?P<path>[^)\s]+)(?:\s+"[^"]*")?\)')
FENCE_RE = re.compile(r'^(```+|~~~+)')
MATH_BLOCK_RE = re.compile(r'^\$\$\s*$')
INLINE_CODE_RE = re.compile(r'`[^`\n]+`')
AUTHOR_COMMENT_RE = re.compile(r'<!--\s*(.+?)\s*-->')


# ─── Excepciones personalizadas ───────────────────────────────────────────────

class MarkdownConversionError(Exception):
    """Base para todos los errores de conversión Markdown → LaTeX/Beamer."""


class UnsupportedMarkdownSyntaxError(MarkdownConversionError):
    """
    Se lanza cuando una celda Markdown contiene sintaxis que este pipeline
    no sabe convertir a LaTeX/Beamer.
    """

    def __init__(
        self,
        syntax_description: str,
        *,
        cell_index: int | None = None,
        line_number: int | None = None,
        line_content: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.syntax_description = syntax_description
        self.cell_index = cell_index
        self.line_number = line_number
        self.line_content = line_content
        self.hint = hint

        parts: list[str] = [
            f"Sintaxis Markdown no soportada: {syntax_description}",
        ]
        if cell_index is not None:
            parts.append(f"  → Celda #{cell_index}")
        if line_number is not None:
            preview = (line_content or "").strip()
            if len(preview) > 100:
                preview = preview[:97] + "..."
            parts.append(f"  → Línea {line_number}: {preview!r}")
        if hint:
            parts.append(f"  → Sugerencia: {hint}")
        super().__init__("\n".join(parts))


class PandocConversionError(MarkdownConversionError):
    """
    Se lanza cuando pandoc falla al convertir Markdown a LaTeX.
    Incluye el mensaje de error de pandoc y contexto de la celda.
    """

    def __init__(
        self,
        pandoc_stderr: str,
        *,
        cell_index: int | None = None,
        markdown_excerpt: str | None = None,
    ) -> None:
        self.pandoc_stderr = pandoc_stderr
        self.cell_index = cell_index
        self.markdown_excerpt = markdown_excerpt

        parts: list[str] = ["Error de pandoc al convertir Markdown → LaTeX:"]
        if cell_index is not None:
            parts.append(f"  → Celda #{cell_index}")
        if pandoc_stderr.strip():
            for ln in pandoc_stderr.strip().splitlines():
                parts.append(f"  → Pandoc: {ln}")
        if markdown_excerpt:
            excerpt = markdown_excerpt.strip()
            if len(excerpt) > 300:
                excerpt = excerpt[:297] + "..."
            parts.append(f"  → Fragmento Markdown:\n{textwrap.indent(excerpt, '      ')}")
        super().__init__("\n".join(parts))


# ─── Patrones Markdown no soportados ─────────────────────────────────────────
# Cada entrada: (regex compilado, descripción legible, sugerencia opcional)
# Se evalúan línea a línea, fuera de bloques de código y bloques math.

_UNSUPPORTED_LINE_PATTERNS: list[tuple[re.Pattern[str], str, str | None]] = [
    # Etiquetas HTML en línea o bloques HTML abiertos/cerrados
    (
        re.compile(r'</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?>'),
        "etiqueta HTML (e.g. <div>, </span>, <br/>, <table>)",
        "Las etiquetas HTML no son válidas en LaTeX. Usa formato Markdown puro o "
        "comandos LaTeX directos (envueltos en bloques de código raw_tex).",
    ),
    # Comentarios HTML
    (
        re.compile(r'<!--'),
        "comentario HTML (<!-- ... -->)",
        "Elimina los comentarios HTML; pdflatex no los reconoce.",
    ),
    # Listas de tareas estilo GitHub (- [ ] o - [x])
    # pandoc las convierte con \item[$\square$] que requiere 'wasysym' (no incluido)
    (
        re.compile(r'^\s*[-*+]\s+\[[ xX]\]\s'),
        "lista de tareas estilo GitHub (- [ ] tarea  o  - [x] tarea)",
        "Convierte las listas de tareas a listas ordinarias (- elemento) "
        "o añade \\usepackage{wasysym} al preámbulo.",
    ),
    # Texto tachado ~~texto~~ → \sout{} que requiere 'ulem' (no incluido)
    (
        re.compile(r'(?<!~)~~(?![\s~])[^~\n]+~~'),
        "texto tachado con doble tilde (~~texto~~)",
        "\\sout{} requiere \\usepackage{ulem}. Añádelo al preámbulo "
        "o elimina el tachado.",
    ),
    # Emoji shortcodes estilo Slack/GitHub (:smile:, :rocket:, etc.)
    (
        re.compile(r'(?<![:/\\]):[a-z][a-z0-9_+\-]{1,30}:(?!\d|/)'),
        "emoji shortcode (e.g. :smile:, :rocket:, :warning:)",
        "Sustituye el shortcode por el carácter Unicode directamente "
        "(😊, 🚀) o por texto descriptivo.",
    ),
    # Bloques de directiva/admonición MyST/Sphinx (:::)
    (
        re.compile(r'^:{3,}'),
        "bloque de directiva o admonición estilo MyST/Sphinx (e.g. :::{note})",
        "Este formato es específico de MyST Markdown y no es compatible "
        "con pandoc estándar ni con LaTeX.",
    ),
    # Líneas de bloque (line blocks) con | al inicio — solo si NO es tabla pipe
    # Una tabla pipe tiene al menos dos '|' en la línea; un line block tiene solo uno al inicio
    (
        re.compile(r'^\|\s+(?=[^|\n]*$)'),
        "bloque de línea (line block) con '|' al inicio",
        "Elimina el '|' inicial o usa un entorno LaTeX apropiado.",
    ),
]

# Patrones que se evalúan sobre el texto completo de la celda (no línea a línea)
_UNSUPPORTED_CELL_PATTERNS: list[tuple[re.Pattern[str], str, str | None]] = [
    # Definiciones de notas al pie [^id]: texto
    # En Beamer los \footnote{} dentro de allowframebreaks pueden causar problemas
    (
        re.compile(r'^\s*\[\^[^\]\n]+\]:', re.MULTILINE),
        "definición de nota al pie Markdown ([^id]: texto)",
        "Las notas al pie (\\footnote{}) tienen comportamiento limitado en Beamer. "
        "Considera incorporar el texto directamente en la diapositiva.",
    ),
]


@dataclass
class ImageRewriteResult:
    markdown: str
    used_assets: list[Path]


def run_pandoc(md: str, *, cell_index: int | None = None) -> str:
    """Convierte Markdown a LaTeX usando pandoc.

    Raises
    ------
    PandocConversionError
        Si pandoc falla o emite advertencias de sintaxis no reconocida.
    RuntimeError
        Si el ejecutable 'pandoc' no está instalado.
    """
    md = md.strip()
    if not md:
        return ''
    try:
        proc = subprocess.run(
            PANDOC_ARGS,
            input=md,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "No se encontró el ejecutable 'pandoc'. "
            "Instálalo con: sudo apt install pandoc  o  brew install pandoc"
        ) from None

    if proc.returncode != 0:
        raise PandocConversionError(
            proc.stderr,
            cell_index=cell_index,
            markdown_excerpt=md[:400],
        )

    # Pandoc puede terminar con código 0 pero emitir advertencias sobre
    # sintaxis que no pudo procesar; las re-lanzamos como error.
    stderr = proc.stderr.strip()
    if stderr:
        warning_keywords = (
            'unknown', 'could not', 'skipping', 'ignoring',
            'not supported', 'unrecognized', 'parse error',
        )
        if any(kw in stderr.lower() for kw in warning_keywords):
            raise PandocConversionError(
                stderr,
                cell_index=cell_index,
                markdown_excerpt=md[:400],
            )

    return proc.stdout.strip()


def ascii_fallback(text: str) -> str:
    return unicodedata.normalize('NFKD', text.expandtabs(4)).encode('ascii', 'ignore').decode('ascii')


_LATEX_CHAR_TABLE = {
    '\\': r'\textbackslash{}',
    '&':  r'\&',
    '%':  r'\%',
    '$':  r'\$',
    '#':  r'\#',
    '_':  r'\_',
    '{':  r'\{',
    '}':  r'\}',
    '~':  r'\textasciitilde{}',
    '^':  r'\textasciicircum{}',
}


def _escape_latex_chars(text: str) -> str:
    """Escapa los caracteres especiales de LaTeX *sin* alterar espacios."""
    for old, new in _LATEX_CHAR_TABLE.items():
        text = text.replace(old, new)
    return text


def escape_latex_text(text: str) -> str:
    """Normaliza espacios y escapa los caracteres especiales de LaTeX."""
    return _escape_latex_chars(re.sub(r'\s+', ' ', text).strip())


_TITLE_SPAN_RE = re.compile(
    r'(?P<math>\$\$?[^$\n]+?\$?\$)'   # $...$ o $$...$$ en una sola línea
    r'|(?P<code>`[^`\n]+`)',           # `...`  código inline
)


def title_to_latex(text: str) -> str:
    """Convierte el texto de un titular Markdown a una cadena LaTeX segura.

    Trata cada segmento del titular de forma diferente:

    * **Texto plano** → ``_escape_latex_chars()``  (escapa $, \\, {}, etc.
      sin recortar espacios, que ya se normalizaron a nivel global).
    * **Math inline** ``$...$`` → se pasa *tal cual* (ya es LaTeX válido).
    * **Código inline** `` `...` `` → ``\\texttt{<inner escapado>}``

    De esta forma un título como::

        Ecuación del calor $\\Gamma(x)$  con `scipy.linalg.solve_banded`

    se convierte en::

        Ecuación del calor $\\Gamma(x)$  con \\texttt{scipy.linalg.solve\\_banded}

    preservando el espacio antes de cada span.
    """
    # Normalización global de espacios (una sola vez, antes de segmentar)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return 'Diapositiva'

    parts: list[str] = []
    pos = 0
    for m in _TITLE_SPAN_RE.finditer(text):
        # Texto plano antes del span — escapar chars pero NO hacer strip
        if m.start() > pos:
            parts.append(_escape_latex_chars(text[pos:m.start()]))
        if m.group('math'):
            # Math span: sin tocar — ya es LaTeX válido
            parts.append(m.group('math'))
        else:
            # Código inline: backticks → \texttt{inner escapado}
            inner = m.group('code')[1:-1]          # quitar los backticks
            parts.append(r'\texttt{' + _escape_latex_chars(inner) + '}')
        pos = m.end()

    # Texto plano tras el último span (o todo el texto si no hubo spans)
    if pos < len(text):
        parts.append(_escape_latex_chars(text[pos:]))

    return ''.join(parts)


def sanitize_title(text: str) -> str:
    return title_to_latex(text or 'Diapositiva')


def find_image(path: str, nb_dir: Path) -> Path | None:
    raw = Path(path.strip().strip('"').strip("'"))
    candidates = [
        nb_dir / raw,
        nb_dir / raw.name,
        nb_dir.parent / raw.name,
        Path.cwd() / raw,
        Path.cwd() / raw.name,
        Path('/mnt/data') / raw.name,
    ]
    seen: set[Path] = set()
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except Exception:
            resolved = cand
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    return None


def safe_copy_asset(src: Path, asset_dir: Path) -> Path:
    asset_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r'[^A-Za-z0-9_.-]+', '_', src.stem) or 'image'
    suffix = src.suffix.lower() if src.suffix else '.png'
    dest = asset_dir / f'{stem}{suffix}'
    i = 2
    while dest.exists() and dest.resolve() != src.resolve():
        dest = asset_dir / f'{stem}_{i}{suffix}'
        i += 1
    if dest.resolve() != src.resolve():
        shutil.copy2(src, dest)
    return dest


def latex_placeholder_for_missing_image(alt: str, path: str) -> str:
    alt = escape_latex_text(alt or 'Imagen')
    path = escape_latex_text(Path(path).name)
    return dedent(rf'''
    \begin{{center}}
    \fbox{{%
      \begin{{minipage}}{{0.90\linewidth}}
      \centering
      \textbf{{Imagen no disponible}}\\[0.30em]
      {alt}\\[0.15em]
      \scriptsize\url{{{path}}}
      \end{{minipage}}
    }}
    \end{{center}}
    ''').strip()


def rewrite_markdown_images(md: str, nb_dir: Path, asset_dir: Path) -> ImageRewriteResult:
    used_assets: list[Path] = []

    def repl(match: re.Match[str]) -> str:
        alt = match.group('alt').strip()
        raw_path = match.group('path').strip()
        found = find_image(raw_path, nb_dir)
        if not found:
            return latex_placeholder_for_missing_image(alt, raw_path)
        copied = safe_copy_asset(found, asset_dir)
        used_assets.append(copied)
        rel = copied.relative_to(asset_dir.parent).as_posix()
        return dedent(rf'''
        \begin{{center}}
        \includegraphics[width=0.94\linewidth,height=0.58\textheight,keepaspectratio]{{{rel}}}
        \end{{center}}
        ''').strip()

    return ImageRewriteResult(markdown=MD_IMAGE_RE.sub(repl, md), used_assets=used_assets)


def _iter_content_lines(source: str) -> list[tuple[int, str]]:
    """Devuelve las líneas del source que NO están dentro de bloques cercados
    (``` o ~~~) ni dentro de bloques math ($$).  Cada elemento es
    (número_de_línea_1based, contenido_sin_codigo_inline).
    """
    result: list[tuple[int, str]] = []
    in_fence = False
    in_math = False
    fence_delim = ''

    for lineno, raw in enumerate(source.splitlines(), start=1):
        stripped = raw.strip()

        # Detectar apertura/cierre de bloques cercados
        fm = FENCE_RE.match(stripped)
        if fm:
            delim = fm.group(1)
            if not in_fence:
                in_fence = True
                fence_delim = delim[:3]
            elif stripped.startswith(fence_delim):
                in_fence = False
            continue  # no analizar la línea del delimitador

        # Detectar bloques math $$
        if MATH_BLOCK_RE.match(stripped):
            in_math = not in_math
            continue

        if in_fence or in_math:
            continue

        # Eliminar código inline (`...`) antes de buscar patrones
        line_no_code = INLINE_CODE_RE.sub('', raw)
        result.append((lineno, line_no_code))

    return result


def validate_markdown_cell(source: str, cell_index: int) -> None:
    """Valida una celda Markdown en busca de sintaxis no soportada por este pipeline.

    Analiza el contenido de la celda línea a línea (ignorando bloques de código
    cercados y bloques math) y comprueba el texto completo de la celda contra
    patrones conocidos como problemáticos.

    Parámetros
    ----------
    source:
        Contenido de la celda Markdown.
    cell_index:
        Índice de la celda dentro del notebook (para mensajes de error).

    Raises
    ------
    UnsupportedMarkdownSyntaxError
        En cuanto se detecta la primera sintaxis no soportada, con el número
        de línea y el contenido original de esa línea.
    """
    if not source.strip():
        return

    content_lines = _iter_content_lines(source)
    original_lines = source.splitlines()

    # Comprobaciones línea a línea
    for lineno, line_no_code in content_lines:
        for pattern, description, hint in _UNSUPPORTED_LINE_PATTERNS:
            if pattern.search(line_no_code):
                original_line = original_lines[lineno - 1] if lineno <= len(original_lines) else ''
                raise UnsupportedMarkdownSyntaxError(
                    description,
                    cell_index=cell_index,
                    line_number=lineno,
                    line_content=original_line,
                    hint=hint,
                )

    # Comprobaciones sobre el texto completo de la celda
    # (excluimos bloques de código cercados del texto a analizar)
    source_no_fences = _strip_fenced_blocks(source)
    for pattern, description, hint in _UNSUPPORTED_CELL_PATTERNS:
        m = pattern.search(source_no_fences)
        if m:
            # Encontrar el número de línea aproximado del match
            char_pos = m.start()
            approx_lineno = source_no_fences[:char_pos].count('\n') + 1
            original_line = ''
            if approx_lineno <= len(original_lines):
                original_line = original_lines[approx_lineno - 1]
            raise UnsupportedMarkdownSyntaxError(
                description,
                cell_index=cell_index,
                line_number=approx_lineno,
                line_content=original_line,
                hint=hint,
            )


def _strip_fenced_blocks(source: str) -> str:
    """Devuelve el source con los bloques de código cercados reemplazados por
    líneas vacías (para no hacer match de patrones dentro de ellos)."""
    lines = source.splitlines(keepends=True)
    out: list[str] = []
    in_fence = False
    fence_delim = ''
    for raw in lines:
        stripped = raw.strip()
        fm = FENCE_RE.match(stripped)
        if fm:
            delim = fm.group(1)
            if not in_fence:
                in_fence = True
                fence_delim = delim[:3]
                out.append('\n')
                continue
            elif stripped.startswith(fence_delim):
                in_fence = False
                out.append('\n')
                continue
        out.append('\n' if in_fence else raw)
    return ''.join(out)


def split_markdown_blocks(md: str, max_lines: int = 12, max_chars: int = 1500) -> list[str]:
    md = md.strip()
    if not md:
        return []
    lines = md.splitlines()
    blocks: list[list[str]] = []
    cur: list[str] = []
    in_fence = False
    in_math = False
    fence_delim = ''

    def flush() -> None:
        nonlocal cur
        if cur:
            blocks.append(cur)
            cur = []

    for line in lines:
        stripped = line.strip()
        fm = FENCE_RE.match(stripped)
        if fm:
            delim = fm.group(1)
            if not in_fence:
                in_fence = True
                fence_delim = delim[:3]
            elif stripped.startswith(fence_delim):
                in_fence = False
            cur.append(line)
            continue
        if not in_fence and MATH_BLOCK_RE.match(stripped):
            in_math = not in_math
            cur.append(line)
            continue
        if not in_fence and not in_math and HEADING_RE.match(stripped) and cur:
            flush()
            cur.append(line)
            continue
        cur.append(line)
        if not in_fence and not in_math and len(cur) > max_lines and stripped == '':
            flush()
    flush()

    final: list[str] = []
    for block in blocks:
        text = '\n'.join(block).strip()
        if not text:
            continue
        if len(block) <= max_lines and len(text) <= max_chars:
            final.append(text)
            continue
        paras = re.split(r'\n\s*\n', text)
        buf: list[str] = []
        for para in paras:
            para = para.strip()
            if not para:
                continue
            cand = '\n\n'.join(buf + [para]) if buf else para
            if buf and (len(cand.splitlines()) > max_lines or len(cand) > max_chars):
                final.append('\n\n'.join(buf).strip())
                buf = [para]
            else:
                buf.append(para)
        if buf:
            final.append('\n\n'.join(buf).strip())
    return [b for b in final if b]


def split_code_chunks(code: str, max_lines: int = 38, max_chars: int = 2800) -> list[str]:
    code = code.rstrip()
    if not code:
        return []
    lines = code.splitlines()
    if len(lines) <= max_lines and len(code) <= max_chars:
        return [code]
    chunks: list[str] = []
    cur: list[str] = []
    in_fence = False
    fence_delim = ''

    def flush() -> None:
        nonlocal cur
        if cur:
            chunks.append('\n'.join(cur).rstrip())
            cur = []

    for line in lines:
        stripped = line.strip()
        fm = FENCE_RE.match(stripped)
        if fm:
            delim = fm.group(1)
            if not in_fence:
                in_fence = True
                fence_delim = delim[:3]
            elif stripped.startswith(fence_delim):
                in_fence = False
            cur.append(line)
            continue
        if not in_fence and stripped.startswith('def ') and cur:
            flush()
        cur.append(line)
    flush()

    result: list[str] = []
    for chunk in chunks:
        if len(chunk.splitlines()) <= max_lines and len(chunk) <= max_chars:
            result.append(chunk)
            continue
        paras = re.split(r'\n\s*\n', chunk)
        buf: list[str] = []
        for para in paras:
            para = para.rstrip()
            if not para:
                continue
            cand = '\n\n'.join(buf + [para]) if buf else para
            if buf and (len(cand.splitlines()) > max_lines or len(cand) > max_chars):
                result.append('\n\n'.join(buf).rstrip())
                buf = [para]
            else:
                buf.append(para)
        if buf:
            result.append('\n\n'.join(buf).rstrip())
    return [c for c in result if c.strip()]


def neutralize_markdown_headings(markdown: str, keep_level1: bool = False) -> str:
    out: list[str] = []
    for line in markdown.splitlines():
        m = HEADING_RE.match(line.strip())
        if not m:
            out.append(line)
            continue
        level = len(m.group('level'))
        title = m.group('title').strip()
        if level == 1 and keep_level1:
            out.append(line)
        else:
            out.append(f'**{title}**')
    return '\n'.join(out).strip()


def single_image_only(markdown: str) -> tuple[str, str] | None:
    lines = [ln.strip() for ln in markdown.splitlines() if ln.strip()]
    if len(lines) != 1:
        return None
    m = MD_IMAGE_RE.fullmatch(lines[0])
    if not m:
        return None
    return m.group('alt').strip() or 'Imagen', m.group('path').strip()



def markdown_heading_candidates(markdown: str) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    for line in markdown.splitlines():
        m = HEADING_RE.match(line.strip())
        if m:
            candidates.append((len(m.group('level')), m.group('title').strip()))
    return candidates


def cell_markdown_title(markdown: str, fallback: str = 'Markdown') -> str:
    candidates = markdown_heading_candidates(markdown)
    if not candidates:
        return fallback
    min_level = min(level for level, _ in candidates)
    for level, title in candidates:
        if level == min_level:
            return title
    return fallback


def first_markdown_title_or_fallback(nb: nbformat.NotebookNode, fallback: str) -> str:
    for cell in nb.cells:
        if cell.cell_type != 'markdown':
            continue
        candidates = markdown_heading_candidates(cell.source)
        if not candidates:
            return fallback
        if len(candidates) == 1 and candidates[0][0] == 1:
            return candidates[0][1]
        return fallback
    return fallback


def extract_author_from_first_cell(nb: nbformat.NotebookNode, fallback: str) -> str:
    """Extrae el nombre del autor del comentario HTML <!-- ... --> en la primera celda Markdown."""
    for cell in nb.cells:
        if cell.cell_type != 'markdown':
            continue
        m = AUTHOR_COMMENT_RE.search(cell.source)
        return m.group(1).strip() if m else fallback
    return fallback


def extract_notebook_title(nb: nbformat.NotebookNode, fallback: str) -> str:
    return first_markdown_title_or_fallback(nb, fallback)


def frame(title: str, body: str, fragile: bool = False, allowbreaks: bool = False) -> str:
    opts: list[str] = []
    if fragile:
        opts.append('fragile')
    if allowbreaks:
        opts.append('allowframebreaks')
    opt = f'[{",".join(opts)}]' if opts else ''
    return f'\\begin{{frame}}{opt}{{{sanitize_title(title)}}}\n{body}\n\\end{{frame}}\n'


def make_title_slide(title: str, subtitle: str, author: str = 'Luis Miguel Buend\u00eda') -> str:
    title = sanitize_title(title)
    subtitle = sanitize_title(subtitle)
    author = sanitize_title(author)
    return dedent(rf'''
    \begin{{frame}}[plain]
    \vspace*{{1.0cm}}
    \centering
    {{\Huge\bfseries {title}}}\\[0.45cm]
    {{\large {author}}}\\[0.20cm]
    {{\small {subtitle}}}
    \vfill
    \end{{frame}}
    ''').strip()


def image_body(path: Path, workdir: Path) -> str:
    rel = path.relative_to(workdir).as_posix()
    return dedent(rf'''
    \begin{{center}}
    \includegraphics[width=0.95\linewidth,height=0.78\textheight,keepaspectratio]{{{rel}}}
    \end{{center}}
    ''').strip()


def normalize_display_math(markdown: str) -> str:
    lines = markdown.splitlines()
    out: list[str] = []
    in_math = False
    for line in lines:
        if line.strip() == '$$':
            out.append('$$')
            in_math = not in_math
            continue
        if in_math and not line.strip():
            continue
        out.append(line)
    return '\n'.join(out)


def strip_cell_title_heading(markdown: str, title: str) -> str:
    lines = markdown.splitlines()
    out: list[str] = []
    removed = False
    for i, line in enumerate(lines):
        if not removed:
            m = HEADING_RE.match(line.strip())
            if m and m.group('title').strip() == title:
                removed = True
                continue
        out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    return '\n'.join(out).strip()



def markdown_frame_from_cell(
    cell_source: str,
    nb_dir: Path,
    asset_dir: Path,
    title: str | None = None,
    default_title: str = 'Markdown',
    cell_index: int | None = None,
) -> tuple[str, list[Path]]:
    img_only = single_image_only(cell_source)
    if img_only:
        alt, raw_path = img_only
        found = find_image(raw_path, nb_dir)
        if found:
            copied = safe_copy_asset(found, asset_dir)
            return frame(alt, image_body(copied, asset_dir.parent), allowbreaks=True), [copied]
        return frame(alt, latex_placeholder_for_missing_image(alt, raw_path), allowbreaks=True), []

    rewritten = rewrite_markdown_images(cell_source, nb_dir, asset_dir)
    text = rewritten.markdown.strip()
    if not text:
        return '', rewritten.used_assets

    frame_title = title or cell_markdown_title(text, default_title)
    body_md = strip_cell_title_heading(text, frame_title)
    body_md = normalize_display_math(neutralize_markdown_headings(body_md, keep_level1=False))
    body_md = re.sub(r'\n{3,}', '\n\n', body_md).strip()
    if not body_md:
        return frame(frame_title, r'\centering\textit{Sin contenido adicional}'), rewritten.used_assets

    body_tex = run_pandoc(body_md, cell_index=cell_index)
    if not body_tex:
        return '', rewritten.used_assets

    body = '\n'.join([
        '\\footnotesize',
        '\\justifying',
        body_tex,
    ])
    return frame(frame_title, body, allowbreaks=True), rewritten.used_assets


def save_png_from_b64(data_b64: str, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f'{name}.png'
    path.write_bytes(base64.b64decode(data_b64))
    return path


def save_image_bytes(data_b64: str, out_dir: Path, filename: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_bytes(base64.b64decode(data_b64))
    return path


def render_code_output_to_body(
    out: nbformat.NotebookNode,
    asset_dir: Path,
    workdir: Path,
    cell_idx: int,
    output_idx: int,
) -> tuple[str | None, list[Path]]:
    saved_assets: list[Path] = []

    if out.output_type == 'stream':
        txt = out.get('text', '').rstrip()
        if not txt:
            return None, saved_assets
        txt = ascii_fallback(txt).expandtabs(4).rstrip('\n')
        return '\n'.join([
            '\\begin{Verbatim}[fontsize=\\scriptsize,frame=single,rulecolor=\\color{codeframe},formatcom=\\RaggedRight]',
            txt,
            '\\end{Verbatim}',
        ]), saved_assets

    if out.output_type not in ('display_data', 'execute_result'):
        return None, saved_assets

    data = out.get('data', {})
    if 'image/png' in data:
        img_path = save_png_from_b64(data['image/png'], asset_dir, f'cell{cell_idx}_out{output_idx}')
        saved_assets.append(img_path)
        return image_body(img_path, workdir), saved_assets
    if 'image/jpeg' in data:
        img_path = save_image_bytes(data['image/jpeg'], asset_dir, f'cell{cell_idx}_out{output_idx}.jpg')
        saved_assets.append(img_path)
        return image_body(img_path, workdir), saved_assets
    if 'text/plain' in data:
        txt = ascii_fallback(str(data['text/plain']).rstrip()).expandtabs(4).rstrip('\n')
        return '\n'.join([
            '\\begin{Verbatim}[fontsize=\\scriptsize,frame=single,rulecolor=\\color{codeframe},formatcom=\\RaggedRight]',
            txt,
            '\\end{Verbatim}',
        ]), saved_assets

    return None, saved_assets


DEFAULT_CODE_TITLE   = 'Código Python'
DEFAULT_OUTPUT_TITLE = 'Resultados numéricos'


def build_tex(
    nb_path: Path,
    workdir: Path,
    *,
    code_title: str   = DEFAULT_CODE_TITLE,
    output_title: str = DEFAULT_OUTPUT_TITLE,
) -> tuple[str, list[Path]]:
    nb = nbformat.read(nb_path, as_version=4)
    nb_dir = nb_path.parent.resolve()
    asset_dir = workdir / 'assets'
    asset_dir.mkdir(parents=True, exist_ok=True)

    title_slide = first_markdown_title_or_fallback(nb, 'No se encontró celda de título')
    author = extract_author_from_first_cell(nb, 'Luis Miguel Buendía')
    frames: list[str] = [make_title_slide(title_slide, TODAY, author)]
    saved_assets: list[Path] = []

    first_markdown_consumed = False

    for idx, cell in enumerate(nb.cells, start=1):
        if cell.cell_type == 'markdown':
            if not first_markdown_consumed:
                first_markdown_consumed = True
                continue

            # Validar la celda antes de intentar convertirla
            validate_markdown_cell(cell.source, cell_index=idx)

            cell_title = cell_markdown_title(cell.source, 'Celda sin título')
            ftex, assets = markdown_frame_from_cell(
                cell.source,
                nb_dir,
                asset_dir,
                title=cell_title,
                default_title=cell_title,
                cell_index=idx,
            )
            saved_assets.extend(assets)
            if ftex:
                frames.append(ftex)

        elif cell.cell_type == 'code':
            code_chunks = split_code_chunks(cell.source)
            if not code_chunks:
                code_chunks = ['']

            code_body_parts: list[str] = []
            for chunk in code_chunks:
                code = ascii_fallback(chunk).expandtabs(4).rstrip('\n')
                code_body_parts.append('\n'.join([
                    '\\begin{lstlisting}[style=pycode]',
                    code,
                    '\\end{lstlisting}',
                ]))

            code_body = '\n\n'.join([p for p in code_body_parts if p.strip()])
            if not code_body:
                code_body = r'\centering\textit{Celda vacía}'
            frames.append(frame(code_title, code_body, fragile=True, allowbreaks=True))

            for out_idx, out in enumerate(cell.get('outputs', []), start=1):
                body, assets = render_code_output_to_body(out, asset_dir, workdir, idx, out_idx)
                saved_assets.extend(assets)
                if body:
                    frames.append(frame(output_title, body, fragile=True, allowbreaks=True))

    tex = BEAMER_PREAMBLE + '\n\\begin{document}\n' + '\n'.join(frames) + '\\end{document}\n'
    return tex, saved_assets
def compile_pdf(tex_path: Path) -> None:
    subprocess.run([
        'latexmk',
        '-pdf',
        '-interaction=nonstopmode',
        '-halt-on-error',
        '-file-line-error',
        tex_path.name,
    ], cwd=tex_path.parent, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description='Convert a Jupyter notebook into a Beamer PDF.')
    ap.add_argument('notebook', type=Path, help='Path to the .ipynb notebook')
    ap.add_argument('--out', type=Path, default=None,
                    help='Output folder (default: <notebook_dir>/<notebook_stem>/)')
    ap.add_argument('--keep-workdir', action='store_true', help='Keep temporary build directory')
    ap.add_argument(
        '--code-title',
        default=DEFAULT_CODE_TITLE,
        metavar='TÍTULO',
        help=f'Título para las diapositivas de código Python (por defecto: "{DEFAULT_CODE_TITLE}")',
    )
    ap.add_argument(
        '--output-title',
        default=DEFAULT_OUTPUT_TITLE,
        metavar='TÍTULO',
        help=f'Título para las diapositivas de resultados de ejecución (por defecto: "{DEFAULT_OUTPUT_TITLE}")',
    )
    args = ap.parse_args()

    nb_path = args.notebook.resolve()
    if not nb_path.exists():
        raise FileNotFoundError(f"No se encontró el notebook: {nb_path}")

    stem = nb_path.stem
    out_dir = (args.out or nb_path.parent / stem).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    out_pdf = out_dir / f'{stem}_slides.pdf'
    out_tex = out_dir / f'{stem}_slides.tex'

    workdir = Path(tempfile.mkdtemp(prefix='nb2beamer_', dir=str(out_dir)))
    try:
        tex, _assets = build_tex(nb_path, workdir, code_title=args.code_title, output_title=args.output_title)
        tex_path = workdir / 'output.tex'
        tex_path.write_text(tex, encoding='utf-8')
        compile_pdf(tex_path)
        shutil.copy2(workdir / 'output.pdf', out_pdf)
        shutil.copy2(tex_path, out_tex)
        assets_src = workdir / 'assets'
        if assets_src.exists():
            assets_dst = out_dir / 'assets'
            if assets_dst.exists():
                shutil.rmtree(assets_dst)
            shutil.copytree(assets_src, assets_dst)
        print(out_pdf)
        print(out_tex)

    except UnsupportedMarkdownSyntaxError as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        print(f"\nEl notebook NO fue convertido. Corrige la celda indicada y vuelve a ejecutar.",
              file=sys.stderr)
        sys.exit(2)

    except PandocConversionError as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        print(f"\nEl notebook NO fue convertido. Revisa la celda indicada.",
              file=sys.stderr)
        sys.exit(2)

    except subprocess.CalledProcessError as exc:
        # Error de compilación LaTeX (latexmk)
        print(f"\n[ERROR] Falló la compilación LaTeX (código {exc.returncode}).",
              file=sys.stderr)
        log_path = workdir / 'output.log'
        if log_path.exists():
            # Mostrar las últimas 30 líneas del log para ayudar al diagnóstico
            log_tail = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
            print("\n--- Últimas líneas del log de LaTeX ---", file=sys.stderr)
            for line in log_tail[-30:]:
                print(line, file=sys.stderr)
            print("--- Fin del log ---\n", file=sys.stderr)
        print(f"Directorio de trabajo conservado en: {workdir}", file=sys.stderr)
        sys.exit(3)

    except Exception:
        print(f'\n[ERROR] Fallo inesperado. Directorio de trabajo conservado en: {workdir}',
              file=sys.stderr)
        raise
    finally:
        if not args.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    main()
