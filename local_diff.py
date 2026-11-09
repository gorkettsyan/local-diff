#!/usr/bin/env python3
"""
local-diff: View your uncommitted changes as a GitHub-style PR diff in the browser.

Usage:
    local-diff                  # diff of all uncommitted changes (staged + unstaged)
    local-diff --staged         # only staged changes
    local-diff --branch main    # diff current branch against main, with its commits (like a PR)
    local-diff --branch main --head feature   # PR-style view of branch feature against main
    local-diff src/ tests/      # only changes under the given directories/files
    local-diff --compare old.py new.py   # any two files or directories, no repository needed
"""

import argparse
import functools
import html
import os
import re
import subprocess
import sys
import tempfile
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Template

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# (foreground, background) per status, using GitHub's dark-theme label colors.
STATUS_COLORS = {
    "added": ("#3fb950", "rgba(46,160,67,0.15)"),
    "deleted": ("#f85149", "rgba(248,81,73,0.15)"),
    "renamed": ("#d29922", "rgba(187,128,9,0.15)"),
    "modified": ("#4493f8", "rgba(56,139,253,0.15)"),
}
STATUS_COLOR_DEFAULT = ("#8b949e", "rgba(110,118,129,0.15)")

HEADER_PREFIXES = {
    "new file": "added",
    "deleted file": "deleted",
    "rename from": "renamed",
}

METADATA_PREFIXES = ("similarity index", "index ", "old mode", "new mode", "rename to")


# --- Data structures ---

@dataclass
class DiffHunk:
    header: str
    lines: list[str] = field(default_factory=list)


@dataclass
class FileDiff:
    header_lines: list[str] = field(default_factory=list)
    old_file: str = ""
    new_file: str = ""
    status: str = "modified"
    hunks: list[DiffHunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def display_name(self) -> str:
        return self.new_file or self.old_file

    @property
    def additions(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.startswith("+"))

    @property
    def deletions(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.startswith("-"))


@dataclass
class Commit:
    sha: str
    short_sha: str
    author: str
    date: datetime
    subject: str
    body: str
    files: list[FileDiff] = field(default_factory=list)


# --- Git helpers ---

def run_git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"git error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def _pathspec(paths: list[str]) -> list[str]:
    """Trailing pathspec args limiting a git command to the given paths (empty when unrestricted)."""
    return ["--", *paths] if paths else []


def check_ref(ref: str) -> None:
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], capture_output=True)
    if result.returncode != 0:
        print(f"Error: unknown branch or commit: {ref}", file=sys.stderr)
        sys.exit(1)


def check_paths(paths: list[str], refs: list[str]) -> None:
    """Fail fast on a pathspec that matches nothing, so a typo doesn't look like 'no changes'.

    A path missing from the working tree is still valid if git knows it (e.g. a deleted directory,
    or one that only exists on one of the branches being compared)."""
    for path in paths:
        if os.path.exists(path):
            continue
        if refs:
            known = "".join(run_git("ls-tree", "-r", "--name-only", ref, "--", path) for ref in refs)
        else:
            known = run_git("ls-files", "--", path)
        if not known.strip():
            print(f"Error: path not found in repository: {path}", file=sys.stderr)
            sys.exit(1)


def get_diff(staged_only: bool, branch: str | None, head: str, paths: list[str]) -> str:
    spec = _pathspec(paths)
    if branch:
        # Three dots: changes on head since it diverged from branch, as a pull request shows them.
        return run_git("diff", f"{branch}...{head}", *spec)
    if staged_only:
        return run_git("diff", "--cached", *spec)
    return run_git("diff", "HEAD", *spec)


def get_untracked_diff(paths: list[str]) -> str:
    """Generate a unified diff for each untracked file, as if added from /dev/null."""
    listing = run_git("ls-files", "--others", "--exclude-standard", *_pathspec(paths))
    untracked = [f for f in listing.splitlines() if f.strip()]
    return "".join(_diff_for_untracked(fp) for fp in untracked)


def _diff_for_untracked(filepath: str) -> str:
    try:
        raw = open(filepath, "rb").read(8192)
    except (OSError, PermissionError):
        return ""

    header = f"diff --git a/{filepath} b/{filepath}\nnew file mode 100644\n--- /dev/null\n+++ b/{filepath}\n"

    if b"\x00" in raw:
        return header + f"Binary files /dev/null and b/{filepath} differ\n"

    try:
        lines = open(filepath, "r", errors="replace").readlines()
    except (OSError, PermissionError):
        return ""

    if not lines:
        return ""

    body = f"@@ -0,0 +1,{len(lines)} @@\n"
    body += "".join(f"+{line.rstrip(chr(10))}\n" for line in lines)
    return header + body


def get_compare_diff(old: str, new: str) -> str:
    """Diff two files or directories directly, whether or not they're in a repository (git diff --no-index)."""
    result = subprocess.run(["git", "diff", "--no-index", "--no-color", "--", old, new], capture_output=True, text=True)
    # Exit status 1 just means the inputs differ.
    if result.returncode not in (0, 1):
        print(f"git error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def strip_compare_roots(files: list[FileDiff], old: str, new: str) -> None:
    """When comparing directories, show file paths relative to the compared directories.

    git prints each side's path as given, minus any leading slash, so strip that root back off."""
    if not (os.path.isdir(old) and os.path.isdir(new)):
        return
    old_root = old.strip("/") + "/"
    new_root = new.strip("/") + "/"
    for f in files:
        # A file only on one side has both paths under that side's root.
        f.old_file = f.old_file.removeprefix(old_root).removeprefix(new_root)
        f.new_file = f.new_file.removeprefix(new_root).removeprefix(old_root)


def get_repo_name() -> str:
    try:
        remote = run_git("remote", "get-url", "origin").strip()
        name = remote.rstrip("/").rsplit("/", 1)[-1]
        return name.removesuffix(".git")
    except SystemExit:
        return os.path.basename(os.getcwd())


def get_branch_name() -> str:
    return run_git("rev-parse", "--abbrev-ref", "HEAD").strip()


def get_short_sha(ref: str = "HEAD") -> str:
    return run_git("rev-parse", "--short", ref).strip()


def get_commits(base: str, head: str, paths: list[str]) -> list[Commit]:
    """Commits on head that aren't on base, oldest first (the commit list of a pull request)."""
    fields = "%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%b%x1e"
    out = run_git("log", "--reverse", f"--format={fields}", f"{base}..{head}", *_pathspec(paths))
    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, short_sha, author, date, subject, body = record.split("\x1f")
        commits.append(Commit(sha, short_sha, author, datetime.fromisoformat(date), subject, body.strip()))
    return commits


def get_commit_diff(sha: str, paths: list[str]) -> str:
    """A single commit's changes; merge commits are shown against their first parent."""
    return run_git("show", "--format=", "--diff-merges=first-parent", sha, *_pathspec(paths))


# --- Diff parsing ---

def parse_diff(raw: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    current_hunk: DiffHunk | None = None

    for line in raw.splitlines():
        if line.startswith("diff --git"):
            current = _flush_and_start(files, current, current_hunk, line)
            current_hunk = None
            continue

        if current is None:
            continue

        current_hunk = _process_line(line, current, current_hunk)

    _flush_file(files, current, current_hunk)
    return files


def _flush_and_start(files: list[FileDiff], current: FileDiff | None, hunk: DiffHunk | None, line: str) -> FileDiff:
    _flush_file(files, current, hunk)
    new_file = FileDiff(header_lines=[line])
    parts = line.split(" b/", 1)
    if len(parts) == 2:
        new_file.new_file = parts[1]
        new_file.old_file = line.split(" a/", 1)[1].split(" b/")[0]
    return new_file


def _flush_file(files: list[FileDiff], current: FileDiff | None, hunk: DiffHunk | None) -> None:
    if current is None:
        return
    if hunk:
        current.hunks.append(hunk)
    files.append(current)


def _process_line(line: str, current: FileDiff, current_hunk: DiffHunk | None) -> DiffHunk | None:
    if line.startswith("--- ") and line[4:] == "/dev/null":
        current.status = "added"
        current.header_lines.append(line)
        return current_hunk

    if line.startswith("+++ ") and line[4:] == "/dev/null":
        current.status = "deleted"
        current.header_lines.append(line)
        return current_hunk

    if line.startswith(("--- ", "+++ ")):
        current.header_lines.append(line)
        return current_hunk

    if line.startswith("Binary files"):
        current.is_binary = True
        current.header_lines.append(line)
        return current_hunk

    if line.startswith("@@"):
        if current_hunk:
            current.hunks.append(current_hunk)
        return DiffHunk(header=line)

    for prefix, status in HEADER_PREFIXES.items():
        if line.startswith(prefix):
            current.status = status
            current.header_lines.append(line)
            return current_hunk

    if line.startswith(METADATA_PREFIXES):
        current.header_lines.append(line)
        return current_hunk

    if current_hunk is not None:
        current_hunk.lines.append(line)

    return current_hunk


# --- Syntax highlighting ---
#
# A small regex tokenizer (standard library only) emitting GitHub's "pl-*" CSS classes:
# pl-k keyword/operator, pl-c comment, pl-c1 constant/number, pl-s string, pl-en function/type name,
# pl-e attribute, pl-ent tag/key, pl-v variable, plus markdown pl-mh / pl-ml / pl-mb / pl-corl.

@dataclass(frozen=True)
class Lang:
    keywords: frozenset[str] = frozenset()
    constants: frozenset[str] = frozenset()
    decl_keywords: frozenset[str] = frozenset()  # the identifier right after one of these is a declared name
    line_comments: tuple[str, ...] = ()  # regexes that start a comment running to end of line
    blocks: tuple[tuple[str, str, str, bool], ...] = ()  # (open, close, class, backslash escapes); may span lines
    quotes: str = ""
    extras: tuple[tuple[str, str], ...] = ()  # (regex, class) tried first; a (?P<v>...) group narrows the colored part
    numbers: bool = True
    operators: bool = False
    call_names: bool = False  # color `name(` and declared names as functions/types
    caps_constants: bool = False  # color ALL_CAPS identifiers as constants
    case_insensitive: bool = False  # keywords are stored lowercase


# Carries an unterminated block comment / multi-line string over to the next line: (close, class, escapes).
HighlightState = tuple[str, str, bool] | None


def _words(s: str) -> frozenset[str]:
    return frozenset(s.split())


_C_BLOCK = ("/*", "*/", "pl-c", False)
_TRIPLE_STRINGS = (('"""', '"""', "pl-s", True), ("'''", "'''", "pl-s", True))
_CODE = dict(operators=True, call_names=True, caps_constants=True)
_ANNOTATION = (r"@[A-Za-z_][\w.]*", "pl-en")

_PYTHON = Lang(
    keywords=_words("and as assert async await break class continue def del elif else except finally for from "
                    "global if import in is lambda nonlocal not or pass raise return try while with yield match case"),
    constants=_words("True False None"),
    decl_keywords=_words("def class"),
    line_comments=("#",), blocks=_TRIPLE_STRINGS, quotes="\"'", extras=(_ANNOTATION,), **_CODE,
)
_JS = Lang(
    keywords=_words("async await break case catch class const continue debugger default delete do else export "
                    "extends finally for from function if import in instanceof let new of return static switch "
                    "throw try typeof var void while with yield as implements interface type enum public private "
                    "protected readonly abstract declare namespace keyof satisfies"),
    constants=_words("true false null undefined this super NaN Infinity string number boolean any unknown never object"),
    decl_keywords=_words("function class interface type enum"),
    line_comments=("//",), blocks=(_C_BLOCK, ("`", "`", "pl-s", True)), quotes="\"'", extras=(_ANNOTATION,), **_CODE,
)
_GO = Lang(
    keywords=_words("break case chan const continue default defer else fallthrough for func go goto if import "
                    "interface map package range return select struct switch type var"),
    constants=_words("true false nil iota"),
    decl_keywords=_words("func type"),
    line_comments=("//",), blocks=(_C_BLOCK, ("`", "`", "pl-s", False)), quotes="\"'", **_CODE,
)
_RUST = Lang(
    keywords=_words("as async await break const continue crate dyn else enum extern fn for if impl in let loop "
                    "match mod move mut pub ref return self Self static struct super trait type unsafe use where while"),
    constants=_words("true false"),
    decl_keywords=_words("fn struct enum trait type impl mod"),
    line_comments=("//",), blocks=(_C_BLOCK,), quotes='"',
    extras=((r"'(?:\\.|[^\\'])'", "pl-s"), (r"[A-Za-z_]\w*!(?=\s*[(\[{])", "pl-en")), **_CODE,
)
_C = Lang(
    keywords=_words("auto break case char const continue default do double else enum extern float for goto if "
                    "inline int long register restrict return short signed sizeof static struct switch typedef union "
                    "unsigned void volatile while bool class namespace template typename public private protected "
                    "virtual override final new delete try catch throw using operator friend explicit constexpr noexcept"),
    constants=_words("true false nullptr NULL this"),
    decl_keywords=_words("struct class enum union namespace"),
    line_comments=("//",), blocks=(_C_BLOCK,), quotes="\"'",
    extras=((r"^\s*#\s*[A-Za-z]+", "pl-k"), (r"(?<=include)\s*(?P<v><[^>]*>)", "pl-s")), **_CODE,
)
_JAVA = Lang(
    keywords=_words("abstract assert boolean break byte case catch char class const continue default do double else "
                    "enum extends final finally float for goto if implements import instanceof int interface long "
                    "native new package private protected public return short static strictfp super switch "
                    "synchronized this throw throws transient try void volatile while var record yield sealed permits "
                    "fun val when is object companion data open override internal lateinit suspend by init typealias"),
    constants=_words("true false null"),
    decl_keywords=_words("class interface enum record fun object"),
    line_comments=("//",), blocks=(_C_BLOCK, _TRIPLE_STRINGS[0]), quotes="\"'", extras=(_ANNOTATION,), **_CODE,
)
_CSHARP = Lang(
    keywords=_words("abstract as base bool break byte case catch char checked class const continue decimal default "
                    "delegate do double else enum event explicit extern finally fixed float for foreach goto if "
                    "implicit in int interface internal is lock long namespace new object operator out override "
                    "params private protected public readonly ref return sbyte sealed short sizeof stackalloc static "
                    "string struct switch this throw try typeof uint ulong unchecked unsafe ushort using virtual void "
                    "volatile while var async await record get set init"),
    constants=_words("true false null"),
    decl_keywords=_words("class struct interface enum record namespace"),
    line_comments=("//",), blocks=(_C_BLOCK,), quotes="\"'", **_CODE,
)
_SWIFT = Lang(
    keywords=_words("associatedtype class deinit enum extension fileprivate func import init inout internal let open "
                    "operator private protocol public rethrows static struct subscript typealias var break case "
                    "continue default defer do else fallthrough for guard if in repeat return switch where while as "
                    "catch is try throw throws async await self Self super some any mutating override final lazy weak"),
    constants=_words("true false nil"),
    decl_keywords=_words("class struct enum protocol extension func"),
    line_comments=("//",), blocks=(_C_BLOCK, _TRIPLE_STRINGS[0]), quotes='"', extras=(_ANNOTATION,), **_CODE,
)
_RUBY = Lang(
    keywords=_words("alias and begin break case class def do else elsif end ensure for if in module next not or redo "
                    "rescue retry return self super then undef unless until when while yield require "
                    "require_relative attr_accessor attr_reader attr_writer private protected public"),
    constants=_words("true false nil"),
    decl_keywords=_words("def class module"),
    line_comments=("#",), quotes="\"'", extras=((r"(?<![\w:]):[A-Za-z_]\w*", "pl-c1"), (r"@@?[A-Za-z_]\w*", "pl-v")),
    **_CODE,
)
_PHP = Lang(
    keywords=_words("abstract and array as break callable case catch class clone const continue declare default do "
                    "echo else elseif empty extends final finally fn for foreach function global goto if implements "
                    "include include_once instanceof interface isset list match namespace new or print private "
                    "protected public readonly require require_once return static switch throw trait try unset use "
                    "var while xor yield"),
    constants=_words("true false null TRUE FALSE NULL"),
    decl_keywords=_words("function class interface trait"),
    line_comments=("//", "#"), blocks=(_C_BLOCK,), quotes="\"'",
    extras=((r"\$[A-Za-z_]\w*", "pl-v"), (r"<\?php|\?>", "pl-k")), **_CODE,
)
_SHELL = Lang(
    keywords=_words("if then else elif fi case esac for while until do done in function select time return exit "
                    "local export readonly declare unset source alias break continue shift trap eval exec set"),
    decl_keywords=_words("function"),
    line_comments=(r"(?<!\S)#",), quotes="\"'", call_names=True,
    extras=((r"\$\{[^}]*\}|\$[A-Za-z_]\w*|\$[0-9@#?$!*-]", "pl-v"), (r"(?<![\w$.-])[A-Za-z_]\w*(?==)", "pl-v")),
)
_SQL = Lang(
    keywords=_words("select from where insert into values update set delete create table alter drop index view join "
                    "inner left right outer full cross on as and or not is in exists between like limit offset order "
                    "by group having union all distinct case when then else end primary key foreign references "
                    "default unique check constraint begin commit rollback transaction returning with if replace "
                    "grant revoke database schema cascade asc desc"),
    constants=_words("true false null"),
    line_comments=("--",), blocks=(_C_BLOCK,), quotes="\"'", operators=True, call_names=True, case_insensitive=True,
)
_LUA = Lang(
    keywords=_words("and break do else elseif end for function goto if in local not or repeat return then until while"),
    constants=_words("true false nil"),
    decl_keywords=_words("function"),
    line_comments=("--",), blocks=(("--[[", "]]", "pl-c", False), ("[[", "]]", "pl-s", False)), quotes="\"'", **_CODE,
)
_CSS = Lang(
    line_comments=("//",), blocks=(_C_BLOCK,), quotes="\"'",
    extras=(
        (r"(?:^|(?<=[{;(]))\s*(?P<v>-{0,2}[A-Za-z][\w-]*)(?=\s*:[^{]*(?:;|\}|$))", "pl-c1"),
        (r"@[\w-]+|!important", "pl-k"),
        (r"#[0-9a-fA-F]{3,8}\b", "pl-c1"),
        (r"\$[\w-]+", "pl-v"),
        (r"[A-Za-z][\w-]*(?=\()", "pl-c1"),
    ),
)
_HTML = Lang(
    blocks=(("<!--", "-->", "pl-c", False),), quotes='"', numbers=False,
    extras=(
        (r"<!(?P<v>(?i:doctype))", "pl-k"),
        (r"</?(?P<v>[A-Za-z][\w:.-]*)", "pl-ent"),
        (r"(?<=\s)[A-Za-z_:@][\w:.-]*(?==)", "pl-e"),
        (r"&#?\w+;", "pl-c1"),
    ),
)
_JSON = Lang(
    constants=_words("true false null"),
    line_comments=("//",), quotes='"',
    extras=((r'"(?:[^"\\]|\\.)*"(?=\s*:)', "pl-ent"),),
)
_YAML = Lang(
    constants=_words("true false null"),
    line_comments=(r"(?<!\S)#",),
    extras=(
        (r"""^[ \t]*(?:-[ \t]+)?(?P<v>[^\s#'"-][^#:]*?|"[^"]*"|'[^']*')(?=[ \t]*:(?:[ \t]|$))""", "pl-ent"),
        (r"""(?<=[:\-\[,{] )(?:"(?:[^"\\]|\\.)*"|'(?:[^']|'')*')""", "pl-s"),
    ),
)
_TOML = Lang(
    constants=_words("true false"),
    line_comments=("#", r"^\s*;"), blocks=_TRIPLE_STRINGS, quotes="\"'",
    extras=((r"^\s*\[\[?[^\]]*\]\]?", "pl-en"), (r'^[ \t]*(?P<v>[\w.\-]+|"[^"]*")(?=[ \t]*=)', "pl-ent")),
)
_MARKDOWN = Lang(
    numbers=False,
    extras=(
        (r"^#{1,6}\s.*", "pl-mh"),
        (r"^\s*```.*", "pl-c1"),
        (r"`[^`]+`", "pl-c1"),
        (r"^\s*(?P<v>[-*+]|\d+\.)(?=\s)", "pl-ml"),
        (r"\*\*[^*]+\*\*|__[^_]+__", "pl-mb"),
        (r"\[[^\]]+\]\([^)]+\)", "pl-corl"),
    ),
)
_DOCKERFILE = Lang(
    line_comments=(r"^\s*#",), quotes="\"'",
    extras=(
        (r"^\s*(?P<v>(?i:FROM|RUN|CMD|LABEL|EXPOSE|ENV|ADD|COPY|ENTRYPOINT|VOLUME|USER|WORKDIR|ARG|ONBUILD|"
         r"STOPSIGNAL|HEALTHCHECK|SHELL|MAINTAINER))\b", "pl-k"),
        (r"\$\{[^}]*\}|\$[A-Za-z_]\w*", "pl-v"),
    ),
)
_MAKEFILE = Lang(
    line_comments=("#",), quotes="\"'", numbers=False,
    extras=(
        (r"^\s*(?P<v>include|ifeq|ifneq|ifdef|ifndef|else|endif|define|endef|export|override)\b", "pl-k"),
        (r"^(?P<v>[\w.-]+)(?=\s*[:?+!]?=)", "pl-v"),
        (r"^(?P<v>[^\s:#=]+(?:[ \t]+[^\s:#=]+)*)(?=[ \t]*::?(?!=))", "pl-en"),
        (r"\$[({][^)}]*[)}]|\$[@<^*?%]", "pl-v"),
    ),
)
# Identifiers only (no coloring), so click-to-definition links still work for unknown file types.
_PLAIN = Lang(numbers=False)

_LANG_BY_EXT = {
    **dict.fromkeys(("py", "pyw"), _PYTHON),
    **dict.fromkeys(("js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts"), _JS),
    "go": _GO,
    "rs": _RUST,
    **dict.fromkeys(("c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "m", "mm"), _C),
    **dict.fromkeys(("java", "kt", "kts"), _JAVA),
    "cs": _CSHARP,
    "swift": _SWIFT,
    **dict.fromkeys(("rb", "rake", "gemspec"), _RUBY),
    "php": _PHP,
    **dict.fromkeys(("sh", "bash", "zsh", "ksh"), _SHELL),
    "sql": _SQL,
    "lua": _LUA,
    **dict.fromkeys(("css", "scss", "sass", "less"), _CSS),
    **dict.fromkeys(("html", "htm", "xhtml", "xml", "svg", "vue", "plist"), _HTML),
    **dict.fromkeys(("json", "jsonc", "geojson"), _JSON),
    **dict.fromkeys(("yaml", "yml"), _YAML),
    **dict.fromkeys(("toml", "ini", "cfg", "conf"), _TOML),
    **dict.fromkeys(("md", "markdown"), _MARKDOWN),
    "mk": _MAKEFILE,
    "dockerfile": _DOCKERFILE,
}

_LANG_BY_FILENAME = {
    "Dockerfile": _DOCKERFILE,
    **dict.fromkeys(("Makefile", "makefile", "GNUmakefile"), _MAKEFILE),
    **dict.fromkeys(("Gemfile", "Rakefile"), _RUBY),
    **dict.fromkeys((".bashrc", ".bash_profile", ".zshrc", ".profile"), _SHELL),
}


def detect_language(path: str) -> Lang | None:
    name = os.path.basename(path)
    if name in _LANG_BY_FILENAME:
        return _LANG_BY_FILENAME[name]
    if name.startswith("Dockerfile."):
        return _DOCKERFILE
    ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
    return _LANG_BY_EXT.get(ext)


_KIND_CLASS = {"comment": "pl-c", "string": "pl-s", "number": "pl-c1", "op": "pl-k"}
_CALL_AHEAD = re.compile(r"\s*\(")


@functools.cache
def _lang_pattern(lang: Lang) -> tuple[re.Pattern[str], dict[str, tuple[str, str, bool]]]:
    """Compile a language's tokenizer: one alternation whose named groups identify the token kind."""
    parts = [f"(?P<x{i}>{rx.replace('(?P<v>', f'(?P<x{i}v>')})" for i, (rx, _) in enumerate(lang.extras)]
    opens = sorted((b[0] for b in lang.blocks), key=len, reverse=True)
    if opens:
        parts.append("(?P<block>" + "|".join(map(re.escape, opens)) + ")")
    if lang.line_comments:
        parts.append("(?P<comment>(?:" + "|".join(lang.line_comments) + ").*)")
    if lang.quotes:
        parts.append("(?P<string>" + "|".join(
            rf"{q}(?:[^{q}\\]|\\.)*(?:{q}|$)" for q in map(re.escape, lang.quotes)
        ) + ")")
    if lang.numbers:
        parts.append(r"(?P<number>\d(?:\w|\.(?=\d))*)")
    parts.append(r"(?P<ident>[A-Za-z_]\w*)")
    if lang.operators:
        starts = [*map(re.escape, opens), *lang.line_comments]
        guard = f"(?!{'|'.join(starts)})" if starts else ""
        parts.append(f"(?P<op>(?:{guard}[-+*/%=<>!&|^~?])+)")
    blocks = {b[0]: (b[1], b[2], b[3]) for b in lang.blocks}
    return re.compile("|".join(parts)), blocks


def _find_close(code: str, pos: int, close: str, escapes: bool) -> int:
    """Index just past the closing delimiter at or after pos, or -1 if the construct continues past this line."""
    while (idx := code.find(close, pos)) >= 0:
        backslashes = 0
        while escapes and idx - backslashes > 0 and code[idx - backslashes - 1] == "\\":
            backslashes += 1
        if backslashes % 2 == 0:
            return idx + len(close)
        pos = idx + 1
    return -1


def _ident_class(lang: Lang, code: str, word: str, end: int, after_decl: bool) -> str:
    key = word.lower() if lang.case_insensitive else word
    if key in lang.keywords:
        return "pl-k"
    if key in lang.constants:
        return "pl-c1"
    if lang.call_names and (after_decl or _CALL_AHEAD.match(code, end)):
        return "pl-en"
    if lang.caps_constants and len(word) > 1 and word.isupper():
        return "pl-c1"
    return ""


def tokenize(code: str, lang: Lang, state: HighlightState) -> tuple[list[tuple[int, int, str]], HighlightState]:
    """Split one line into sorted, non-overlapping (start, end, css_class) spans; the class is "" for plain
    identifiers. `state` continues a block comment or multi-line string from the previous line."""
    spans: list[tuple[int, int, str]] = []
    pos = 0
    if state:
        close, cls, escapes = state
        end = _find_close(code, 0, close, escapes)
        if end < 0:
            return [(0, len(code), cls)], state
        spans.append((0, end, cls))
        pos = end

    pattern, blocks = _lang_pattern(lang)
    prev_word, prev_end = "", 0
    while pos < len(code) and (m := pattern.search(code, pos)):
        kind, start, end = m.lastgroup, m.start(), m.end()
        pos = max(end, start + 1)
        if kind == "block":
            close, cls, escapes = blocks[m.group()]
            close_end = _find_close(code, end, close, escapes)
            if close_end < 0:
                spans.append((start, len(code), cls))
                return spans, (close, cls, escapes)
            spans.append((start, close_end, cls))
            pos = close_end
        elif kind == "ident":
            word = m.group()
            after_decl = prev_word in lang.decl_keywords and code[prev_end:start].isspace()
            spans.append((start, end, _ident_class(lang, code, word, end, after_decl)))
            prev_word, prev_end = word, end
        elif kind.startswith("x"):
            cls = lang.extras[int(kind[1:])][1]
            narrowed = f"{kind}v"
            if narrowed in pattern.groupindex and m.start(narrowed) >= 0:
                start, end = m.span(narrowed)
            spans.append((start, end, cls))
        else:
            spans.append((start, end, _KIND_CLASS[kind]))
    return spans, None


# --- HTML rendering ---

# Heuristic patterns for recognizing function/class definitions across common languages,
# used to power click-to-jump "go to definition" links for identifiers referenced elsewhere in the diff.
_DEF_PATTERNS = [
    re.compile(r"\bdef\s+([A-Za-z_]\w*)\s*\("),                                    # python
    re.compile(r"\bclass\s+([A-Za-z_]\w*)"),                                       # python/js/ts/java/c#
    re.compile(r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("),                  # go
    re.compile(r"\bfn\s+([A-Za-z_]\w*)\s*[<(]"),                                   # rust
    re.compile(r"\bfunction\s*\*?\s+([A-Za-z_]\w*)\s*\("),                         # js/ts function declaration
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_]\w*)\s*=>"),  # js/ts arrow fn
]

_CALL_PATTERN = re.compile(r"\b([A-Za-z_]\w*)\s*\(")

_NOT_CALLS = {
    "if", "for", "while", "switch", "catch", "return", "elif", "else", "except",
    "with", "case", "sizeof", "new", "delete", "typeof", "instanceof", "await",
    "yield", "async", "function", "def", "class", "func", "fn", "const", "let",
    "var", "throw", "do",
}


def status_badge(status: str) -> str:
    fg, bg = STATUS_COLORS.get(status, STATUS_COLOR_DEFAULT)
    return f'<span class="status-badge" style="background:{bg};color:{fg}">{status[0].upper()}</span>'


def _row(num_cls: str, num: str, code_cls: str, content: str, row_id: str = "") -> str:
    id_attr = f' id="{row_id}"' if row_id else ""
    return f'<tr{id_attr}><td class="line-num {num_cls}">{num}</td><td class="code {code_cls}">{content}</td></tr>'


def _match_definition_name(line: str) -> str | None:
    for pattern in _DEF_PATTERNS:
        m = pattern.search(line)
        if m:
            return m.group(1)
    return None


def _find_definitions(hunks: list[DiffHunk]) -> dict[str, tuple[int, int]]:
    """Scan hunk lines (skipping deletions) for definitions, mapping name -> (hunk_index, line_index)."""
    defs: dict[str, tuple[int, int]] = {}
    for h_idx, hunk in enumerate(hunks):
        for l_idx, line in enumerate(hunk.lines):
            if line.startswith("-"):
                continue
            name = _match_definition_name(line)
            if name and name not in defs:
                defs[name] = (h_idx, l_idx)
    return defs


def _def_link(anchor: str, name: str) -> str:
    return f'<a class="def-ref" href="#{anchor}" onclick="jumpToDefinition(event, this)">{html.escape(name)}</a>'


def _call_links(code: str, name_to_anchor: dict[str, str]) -> dict[int, tuple[int, str]]:
    """Map start offset -> (end offset, anchor) for references to known definitions (skipping the definitions themselves)."""
    if not name_to_anchor:
        return {}
    def_spans = [(m.start(1), m.end(1)) for pattern in _DEF_PATTERNS for m in pattern.finditer(code)]
    links = {}
    for m in _CALL_PATTERN.finditer(code):
        start, end, name = m.start(1), m.end(1), m.group(1)
        if name in _NOT_CALLS or any(start < de and end > ds for ds, de in def_spans):
            continue
        if anchor := name_to_anchor.get(name):
            links[start] = (end, anchor)
    return links


_UNLINKABLE_CLASSES = {"pl-s", "pl-c"}


def _line_code_html(
    raw_line: str, name_to_anchor: dict[str, str], lang: Lang | None, state: HighlightState
) -> tuple[str, HighlightState]:
    """Syntax-highlight a diff line (after its +/-/space marker), wrapping recognized function/class
    references as click-to-jump links. Returns the HTML and the multi-line state for the next line."""
    if not raw_line:
        return " ", state

    marker, code = raw_line[0], raw_line[1:]
    spans, state = tokenize(code, lang or _PLAIN, state)
    links = _call_links(code, name_to_anchor)

    pieces = [html.escape(marker)]
    cursor = 0
    for start, end, cls in spans:
        pieces.append(html.escape(code[cursor:start]))
        text = code[start:end]
        link = links.get(start)
        if link and link[0] == end and cls not in _UNLINKABLE_CLASSES:
            inner = _def_link(link[1], text)
        else:
            inner = html.escape(text)
        pieces.append(f'<span class="{cls}">{inner}</span>' if cls else inner)
        cursor = end
    pieces.append(html.escape(code[cursor:]))
    return "".join(pieces), state


class _LineRenderer:
    """Renders the code lines of one side of a split diff, carrying multi-line syntax state
    (block comments, triple-quoted strings) from each line to the next."""

    def __init__(self, lang: Lang | None, name_to_anchor: dict[str, str]):
        self.lang = lang
        self.name_to_anchor = name_to_anchor
        self.state: HighlightState = None

    def render(self, raw_line: str) -> str:
        content, self.state = _line_code_html(raw_line, self.name_to_anchor, self.lang, self.state)
        return content


def _parse_hunk_start(header: str) -> tuple[int, int]:
    try:
        parts = header.split("@@")[1].strip()
        old_part, new_part = parts.split(" +")
        old = int(old_part.lstrip("-").split(",")[0]) - 1
        new = int(new_part.split(",")[0]) - 1
        return old, new
    except (IndexError, ValueError):
        return 0, 0


def _split_hunk_header(header: str) -> tuple[str, str]:
    """Split a hunk header into its '@@ -x,y +a,b @@' part and the trailing function/context git appends, if any."""
    idx = header.find("@@", 2)
    if idx == -1:
        return header, ""
    return header[:idx + 2], header[idx + 2:]


_WORD_PATTERN = re.compile(r"[A-Za-z_]\w*")


def _render_hunk_header(header: str, name_to_anchor: dict[str, str]) -> str:
    """Render a hunk header, turning its function/class context (if any) into a click-to-jump link
    back to that identifier's actual definition, wherever it appears in the diff."""
    mechanical, context = _split_hunk_header(header)
    if not context or not name_to_anchor:
        return html.escape(header)

    pieces = [html.escape(mechanical)]
    cursor = 0
    for m in _WORD_PATTERN.finditer(context):
        anchor = name_to_anchor.get(m.group(0))
        if not anchor:
            continue
        pieces.append(html.escape(context[cursor:m.start()]))
        pieces.append(_def_link(anchor, m.group(0)))
        cursor = m.end()
    pieces.append(html.escape(context[cursor:]))
    return "".join(pieces)


def render_split_rows(file_index: int, hunks: list[DiffHunk], lang: Lang | None = None) -> tuple[str, str]:
    """Render hunks as two separate sets of table rows for left/right split view.

    Lines that look like a function/class definition get a jump anchor of their own. Any later
    reference to that name (a call site, or a hunk header's enclosing-function context) is
    rendered as a click-to-jump link back to it.
    """
    left: list[str] = []
    right: list[str] = []

    definitions = _find_definitions(hunks)
    def_locations = set(definitions.values())
    name_to_anchor = {
        name: f"def-{file_index}-{loc[0]}-{loc[1]}"
        for name, loc in definitions.items()
    }

    for hunk_index, hunk in enumerate(hunks):
        old_num, new_num = _parse_hunk_start(hunk.header)
        header_html = _render_hunk_header(hunk.header, name_to_anchor)
        anchor_id = f"hunk-{file_index}-{hunk_index}"
        # Each hunk is a separate excerpt, so multi-line syntax state starts fresh.
        old_side = _LineRenderer(lang, name_to_anchor)
        new_side = _LineRenderer(lang, name_to_anchor)

        left.append(_row("", "", "hunk-header-content", header_html))
        right.append(_row("", "", "hunk-header-content", header_html))
        # Add hunk-header class (and, on the left row only, a jump anchor id) to the last appended rows
        left[-1] = left[-1].replace("<tr>", f'<tr class="hunk-header" id="{anchor_id}">')
        right[-1] = right[-1].replace("<tr>", '<tr class="hunk-header">')

        i = 0
        lines = hunk.lines
        while i < len(lines):
            line = lines[i]

            # Context line (including empty lines from diff)
            if not line or not line[0] in "+-\\":
                old_num += 1
                new_num += 1
                def_id = f"def-{file_index}-{hunk_index}-{i}" if (hunk_index, i) in def_locations else ""
                left.append(_row("", str(old_num), "context", old_side.render(line)))
                right.append(_row("", str(new_num), "context", new_side.render(line), def_id))
                i += 1
                continue

            # No-newline marker
            if line.startswith("\\"):
                left.append(_row("", "", "no-newline", html.escape(line)))
                right.append(_row("", "", "no-newline", ""))
                i += 1
                continue

            # Collect consecutive deletions then additions and pair them
            if line.startswith("-"):
                del_lines, add_lines, i, old_num, new_num = _collect_change_pair(lines, i, old_num, new_num)
                _append_paired_rows(left, right, del_lines, add_lines, file_index, hunk_index, def_locations, old_side, new_side)
                continue

            # Standalone addition (no preceding deletion)
            new_num += 1
            def_id = f"def-{file_index}-{hunk_index}-{i}" if (hunk_index, i) in def_locations else ""
            left.append(_row("", "", "empty-side", ""))
            right.append(_row("add-num", str(new_num), "addition", new_side.render(line), def_id))
            i += 1

    return "\n".join(left), "\n".join(right)


def _collect_change_pair(
    lines: list[str], i: int, old_num: int, new_num: int
) -> tuple[list[tuple[int, int, str]], list[tuple[int, int, str]], int, int, int]:
    del_lines: list[tuple[int, int, str]] = []
    while i < len(lines) and lines[i].startswith("-"):
        old_num += 1
        del_lines.append((old_num, i, lines[i]))
        i += 1

    add_lines: list[tuple[int, int, str]] = []
    while i < len(lines) and lines[i].startswith("+"):
        new_num += 1
        add_lines.append((new_num, i, lines[i]))
        i += 1

    return del_lines, add_lines, i, old_num, new_num


def _append_paired_rows(
    left: list[str],
    right: list[str],
    del_lines: list[tuple[int, int, str]],
    add_lines: list[tuple[int, int, str]],
    file_index: int,
    hunk_index: int,
    def_locations: set[tuple[int, int]],
    old_side: _LineRenderer,
    new_side: _LineRenderer,
) -> None:
    def def_id(line_index: int) -> str:
        return f"def-{file_index}-{hunk_index}-{line_index}" if (hunk_index, line_index) in def_locations else ""

    for j in range(max(len(del_lines), len(add_lines))):
        if j < len(del_lines):
            num, line_index, raw_line = del_lines[j]
            left.append(_row("del-num", str(num), "deletion", old_side.render(raw_line), def_id(line_index)))
        else:
            left.append(_row("", "", "empty-side", ""))

        if j < len(add_lines):
            num, line_index, raw_line = add_lines[j]
            right.append(_row("add-num", str(num), "addition", new_side.render(raw_line), def_id(line_index)))
        else:
            right.append(_row("", "", "empty-side", ""))


def render_file_list_item(index: int, f: FileDiff) -> str:
    return (
        f'<a class="file-item" href="#file-{index}" title="{html.escape(f.display_name)}">'
        f'{status_badge(f.status)} '
        f'<span class="file-name">{html.escape(f.display_name)}</span>'
        f'<span class="file-stat">'
        f'<span class="stat-add">+{f.additions}</span> '
        f'<span class="stat-del">-{f.deletions}</span>'
        f'</span></a>'
    )


def render_file_diff(index: int, f: FileDiff) -> str:
    body = _render_file_body(index, f)
    return (
        f'<div class="file-diff" id="file-{index}">'
        f'<div class="file-header">'
        f'{status_badge(f.status)} '
        f'<span class="file-path">{html.escape(f.display_name)}</span>'
        f'<span class="file-stat">'
        f'<span class="stat-add">+{f.additions}</span> '
        f'<span class="stat-del">-{f.deletions}</span>'
        f'</span></div>'
        f'{body}</div>'
    )


def _render_file_body(file_index: int, f: FileDiff) -> str:
    if f.is_binary:
        return '<div class="binary-notice">Binary file changed</div>'
    if not f.hunks:
        return '<div class="binary-notice">File metadata changed</div>'

    left_rows, right_rows = render_split_rows(file_index, f.hunks, detect_language(f.display_name))
    return (
        f'<div class="split-container">'
        f'<div class="split-left"><table class="diff-table">{left_rows}</table></div>'
        f'<div class="split-handle" title="Drag to resize"></div>'
        f'<div class="split-right"><table class="diff-table">{right_rows}</table></div>'
        f'</div>'
    )


def render_change_bar(additions: int, deletions: int) -> str:
    total = additions + deletions
    if total == 0:
        return ""
    add_pct = int(additions / total * 100)
    return (
        f'<span class="bar-add" style="width:{add_pct}%"></span>'
        f'<span class="bar-del" style="width:{100 - add_pct}%"></span>'
    )


def load_template() -> Template:
    template_path = TEMPLATE_DIR / "diff.html"
    return Template(template_path.read_text())


def render_hunk_nav(files: list[FileDiff]) -> str:
    """Floating prev/next buttons for jumping between changed hunks (functions) across the whole diff."""
    total_hunks = sum(len(f.hunks) for f in files)
    if total_hunks == 0:
        return ""
    return (
        '<div class="hunk-nav" title="Jump between changes (n / p)">'
        '<button type="button" id="prevHunkBtn" aria-label="Previous change">&uarr;</button>'
        '<span id="hunkCounter" class="hunk-counter"></span>'
        '<button type="button" id="nextHunkBtn" aria-label="Next change">&darr;</button>'
        '</div>'
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def render_files_panel(files: list[FileDiff], first_index: int, empty_message: str) -> str:
    """Summary bar, file sidebar and diff sections for one set of files. Indexes start at first_index
    so element ids stay unique when a page holds several panels (the full diff plus one per commit)."""
    if not files:
        return f'<div class="empty">{html.escape(empty_message)}</div>'

    additions = sum(f.additions for f in files)
    deletions = sum(f.deletions for f in files)
    file_list = "".join(render_file_list_item(i, f) for i, f in enumerate(files, first_index))
    diff_sections = "".join(render_file_diff(i, f) for i, f in enumerate(files, first_index))
    return (
        f'<div class="summary">'
        f'<span class="stat-add">+{additions}</span>'
        f'<span class="stat-del">&minus;{deletions}</span>'
        f'<span>across <strong>{_plural(len(files), "file")}</strong> changed</span>'
        f'<span class="change-bar">{render_change_bar(additions, deletions)}</span>'
        f'</div>'
        f'<div class="layout">'
        f'<aside class="sidebar">'
        f'<div class="file-list">'
        f'<div class="file-list-title">'
        f'<span class="file-list-label">Changed files</span>'
        f'<button type="button" class="sidebar-toggle" aria-expanded="true">&laquo;</button>'
        f'</div>'
        f'<div class="file-list-body">{file_list}</div>'
        f'</div>'
        f'</aside>'
        f'<div class="sidebar-handle" title="Drag to resize (double-click to reset)"></div>'
        f'<div class="main">{diff_sections}</div>'
        f'</div>'
    )


def _format_date(d: datetime) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _avatar(author: str) -> str:
    hue = sum(map(ord, author)) * 37 % 360
    initial = html.escape(author[:1].upper() or "?")
    return f'<span class="avatar" style="background:hsl({hue},45%,38%)" title="{html.escape(author)}">{initial}</span>'


def _committed(c: Commit) -> str:
    """'<author> committed <date>'; the page script rewrites recent dates as relative times."""
    return (
        f'<strong>{html.escape(c.author)}</strong> committed '
        f'<time datetime="{c.date.isoformat()}" title="{c.date:%Y-%m-%d %H:%M %z}">on {_format_date(c.date)}</time>'
    )


def render_commit_list(commits: list[Commit]) -> str:
    """Commits grouped by day, oldest first, each linking to its own diff view."""
    groups: dict[str, list[tuple[int, Commit]]] = {}
    for i, c in enumerate(commits):
        groups.setdefault(_format_date(c.date), []).append((i, c))

    sections = []
    for day, entries in groups.items():
        rows = "".join(
            f'<a class="commit-row" href="#view-commit-{i}">'
            f'{_avatar(c.author)}'
            f'<span class="commit-row-main">'
            f'<span class="commit-subject">{html.escape(c.subject)}</span>'
            f'<span class="commit-row-meta">{_committed(c)}</span>'
            f'</span>'
            f'<span class="file-stat">'
            f'<span class="stat-add">+{sum(f.additions for f in c.files)}</span> '
            f'<span class="stat-del">-{sum(f.deletions for f in c.files)}</span>'
            f'</span>'
            f'<code class="commit-sha">{html.escape(c.short_sha)}</code>'
            f'</a>'
            for i, c in entries
        )
        sections.append(
            f'<div class="commit-group">'
            f'<h3 class="commit-group-title">Commits on {day}</h3>'
            f'<div class="commit-list">{rows}</div>'
            f'</div>'
        )
    return "".join(sections) or '<div class="empty">No commits.</div>'


def render_commit_header(commits: list[Commit], index: int) -> str:
    c = commits[index]

    def nav(target: int, label: str) -> str:
        if 0 <= target < len(commits):
            return f'<a class="btn" href="#view-commit-{target}">{label}</a>'
        return f'<span class="btn disabled">{label}</span>'

    body = f'<pre class="commit-body">{html.escape(c.body)}</pre>' if c.body else ""
    return (
        f'<div class="commit-header">'
        f'<div class="commit-nav">'
        f'<a class="back-link" href="#view-commits">&lsaquo; All commits</a>'
        f'<span class="commit-position">Commit {index + 1} of {len(commits)}</span>'
        f'<span class="btn-group">{nav(index - 1, "&lsaquo; Prev")}{nav(index + 1, "Next &rsaquo;")}</span>'
        f'</div>'
        f'<h2 class="commit-title">{html.escape(c.subject)}</h2>'
        f'{body}'
        f'<div class="commit-meta">{_avatar(c.author)}<span>{_committed(c)}</span>'
        f'<span class="commit-full-sha">commit <code>{html.escape(c.sha)}</code></span></div>'
        f'</div>'
    )


def render_tabs(commit_count: int, file_count: int) -> str:
    return (
        '<nav class="tabs">'
        f'<a class="tab" href="#view-commits" data-view="view-commits">Commits <span class="counter">{commit_count}</span></a>'
        f'<a class="tab selected" href="#view-files" data-view="view-files">Files changed <span class="counter">{file_count}</span></a>'
        '</nav>'
    )


def render_html(files: list[FileDiff], repo: str, meta: str, commits: list[Commit] | None = None) -> str:
    """Render the page. With commits (branch comparisons) it gets PR-style tabs: the combined
    "Files changed" view, a commit list, and a diff view per commit."""
    views = [
        f'<section class="view" id="view-files" data-tab="view-files">'
        f'{render_files_panel(files, 0, "No changes to display.")}</section>'
    ]
    tabs = ""
    all_files = list(files)
    if commits is not None:
        tabs = render_tabs(len(commits), len(files))
        views.append(
            f'<section class="view" id="view-commits" data-tab="view-commits" hidden>'
            f'{render_commit_list(commits)}</section>'
        )
        for i, c in enumerate(commits):
            panel = render_files_panel(c.files, len(all_files), "No file changes in this commit.")
            all_files.extend(c.files)
            views.append(
                f'<section class="view" id="view-commit-{i}" data-tab="view-commits" hidden>'
                f'{render_commit_header(commits, i)}{panel}</section>'
            )

    template = load_template()
    return template.safe_substitute(
        repo=html.escape(repo),
        meta=meta,
        tabs=tabs,
        views="".join(views),
        hunk_nav=render_hunk_nav(all_files),
    )


# --- CLI ---

def get_page_meta(args: argparse.Namespace, files: list[FileDiff], commits: list[Commit] | None) -> str:
    """The HTML subtitle under the page title, e.g. "main ← feature @ abc1234 · 3 commits · 5 files changed"."""
    if args.compare:
        old, new = args.compare
        parts = [f"{html.escape(old)} &rarr; {html.escape(new)}"]
    elif args.branch:
        parts = [f"{html.escape(args.branch)} &larr; {html.escape(args.head)} @ {html.escape(get_short_sha(args.head))}"]
        parts.append(_plural(len(commits or []), "commit"))
    else:
        parts = [f"{html.escape(get_branch_name())} @ {html.escape(get_short_sha())}"]
        parts.append("staged changes" if args.staged else "uncommitted changes")
    if args.paths:
        parts.append("in " + html.escape(", ".join(args.paths)))
    parts.append(f"{_plural(len(files), 'file')} changed")
    return " &middot; ".join(parts)


def write_output(content: str, output_path: str | None) -> str:
    if output_path:
        path = os.path.abspath(output_path)
        with open(path, "w") as f:
            f.write(content)
        return path

    tmp = tempfile.NamedTemporaryFile(suffix=".html", prefix="local-diff-", delete=False)
    tmp.write(content.encode())
    tmp.close()
    return tmp.name


def run_compare(args: argparse.Namespace) -> None:
    old, new = args.compare
    for path in (old, new):
        if not os.path.exists(path):
            print(f"Error: path not found: {path}", file=sys.stderr)
            sys.exit(1)
    if os.path.isdir(old) != os.path.isdir(new):
        print("Error: --compare needs two files or two directories.", file=sys.stderr)
        sys.exit(1)

    raw_diff = get_compare_diff(old, new)
    if not raw_diff.strip():
        print("No differences found.")
        sys.exit(0)

    files = parse_diff(raw_diff)
    strip_compare_roots(files, old, new)
    title = os.path.basename(os.path.abspath(new))
    publish(render_html(files, repo=title, meta=get_page_meta(args, files, None)), args)


def publish(html_content: str, args: argparse.Namespace) -> None:
    out_path = write_output(html_content, args.output)
    print(f"Diff written to: {out_path}")

    if not args.no_open:
        webbrowser.open(f"file://{out_path}")


def main():
    parser = argparse.ArgumentParser(description="View local git diff as a PR-style HTML page")
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="Limit the diff to these directories or files (default: whole repository)",
    )
    parser.add_argument("--staged", action="store_true", help="Show only staged changes")
    parser.add_argument("--branch", type=str, default=None, help="Base branch to compare against, like a PR's target (e.g. main)")
    parser.add_argument("--head", type=str, default=None, help="Branch or commit to review against --branch (default: HEAD)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open the browser")
    parser.add_argument("--no-untracked", action="store_true", help="Exclude untracked files from the diff")
    parser.add_argument(
        "--compare", nargs=2, metavar=("OLD", "NEW"), default=None,
        help="Compare two files or directories directly, inside or outside a git repository",
    )
    parser.add_argument("-o", "--output", type=str, default=None, help="Save HTML to this path instead of a temp file")
    args = parser.parse_args()
    if args.head and not args.branch:
        parser.error("--head requires --branch (the base to compare against)")
    if args.staged and args.branch:
        parser.error("--staged can't be combined with --branch")
    if args.compare and (args.staged or args.branch or args.paths):
        parser.error("--compare can't be combined with --staged, --branch or paths")
    args.head = args.head or "HEAD"

    if args.compare:
        run_compare(args)
        return

    check = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if check.returncode != 0:
        print("Error: not inside a git repository.", file=sys.stderr)
        sys.exit(1)

    refs = [args.branch, args.head] if args.branch else []
    for ref in refs:
        check_ref(ref)
    check_paths(args.paths, refs)

    raw_diff = get_diff(args.staged, args.branch, args.head, args.paths)
    if not args.no_untracked and not args.staged and not args.branch:
        raw_diff += get_untracked_diff(args.paths)

    commits = None
    if args.branch:
        commits = get_commits(args.branch, args.head, args.paths)
        for commit in commits:
            commit.files = parse_diff(get_commit_diff(commit.sha, args.paths))

    if not raw_diff.strip() and not commits:
        scope = f" in {', '.join(args.paths)}" if args.paths else ""
        print(f"No changes found{scope}.")
        sys.exit(0)

    files = parse_diff(raw_diff)
    publish(render_html(files, repo=get_repo_name(), meta=get_page_meta(args, files, commits), commits=commits), args)


if __name__ == "__main__":
    main()
