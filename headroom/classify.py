"""Decide whether a shell command is a heavy job, and how to make it lighter.

The classifier is conservative: it looks at the *command word* of every simple command in a
pipeline/list (after env assignments and wrappers such as ``time``/``nice``/``npx``), so prose
inside quotes (``git commit -m "npm install docs"``) never counts.

Rewrites are limited to flags whose meaning has been stable for years and that only reduce
parallelism or cap a heap. Each one is reported back so the model and the user can see what
actually ran.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- segmentation

_SEPARATORS = ("&&", "||", ";;", ";", "|&", "|", "&", "\n", "(", ")")


@dataclass
class Segment:
    start: int  # offset of the segment text in the original command
    end: int
    text: str


def split_segments(command: str) -> List[Segment]:
    """Split on unquoted list/pipeline operators. Quotes, escapes and ``$(...)`` are respected
    well enough for classification (we never execute the result of this split)."""
    segments: List[Segment] = []
    i, n = 0, len(command)
    seg_start = 0
    quote: Optional[str] = None
    depth = 0  # $( ... ) nesting
    while i < n:
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "#" and (i == 0 or command[i - 1] in " \t\n;&|("):
            # comment to end of line
            nl = command.find("\n", i)
            if nl < 0:
                _push(segments, command, seg_start, i)
                seg_start = n
                i = n
                break
            _push(segments, command, seg_start, i)
            seg_start = nl
            i = nl
            continue
        if command.startswith("$(", i):
            depth += 1
            i += 2
            continue
        if ch == ")" and depth:
            depth -= 1
            i += 1
            continue
        if depth == 0:
            matched = next((op for op in _SEPARATORS if command.startswith(op, i)), None)
            if matched:
                # `2>&1`, `>&2`, `&>` are redirections, not the background operator.
                if matched == "&" and (i > 0 and command[i - 1] in "<>" or command.startswith("&>", i)):
                    i += 1
                    continue
                if matched in ("|", "|&") and i > 0 and command[i - 1] == ">":  # `>|` clobber
                    i += 1
                    continue
                _push(segments, command, seg_start, i)
                i += len(matched)
                seg_start = i
                continue
        i += 1
    _push(segments, command, seg_start, n)
    return segments


def _push(segments: List[Segment], command: str, start: int, end: int) -> None:
    text = command[start:end]
    stripped = text.strip()
    if not stripped:
        return
    lead = len(text) - len(text.lstrip())
    segments.append(Segment(start + lead, start + lead + len(stripped), stripped))


_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"(?:[^\"\\]|\\.)*\"|\S)*\s+")
_WRAPPERS = re.compile(
    r"^(?:(?:env(?:\s+-\S+)*|time(?:\s+-p)?|nice(?:\s+-n\s*-?\d+|\s+-\d+)?|"
    r"ionice(?:\s+-c\s*\d+)?(?:\s+-n\s*\d+)?|nohup|exec|command|builtin|stdbuf(?:\s+-\S+)*|"
    r"timeout(?:\s+-\S+)*\s+\d+[smhd]?|caffeinate|chrt\s+\S+\s+\d+|taskset\s+\S+)\s+)"
)
_SUBSHELL_PREFIX = re.compile(r"^[({]\s*")


def command_words(segment: str) -> Tuple[str, int]:
    """Return the segment with leading env assignments and wrappers removed, plus the offset of
    the command word inside the segment."""
    offset = 0
    text = segment
    while True:
        before = len(text)
        for pattern in (_SUBSHELL_PREFIX, _ENV_ASSIGN, _WRAPPERS):
            m = pattern.match(text)
            if m:
                text = text[m.end():]
                offset += m.end()
        if len(text) == before:
            break
    return text, offset


# ---------------------------------------------------------------- rules


@dataclass(frozen=True)
class Rule:
    name: str
    kind: str  # js | python | rust | go | jvm | native | container | media | browser | agent | custom
    pattern: re.Pattern
    isolate: bool = True  # False when the heavy work runs outside our process tree (docker daemon)


def _r(name: str, kind: str, regex: str, isolate: bool = True) -> Rule:
    return Rule(name, kind, re.compile(regex), isolate)


_JS_RUNNER = r"(?:(?:npx|bunx)(?:\s+--?\S+)*\s+|(?:pnpm|yarn)\s+(?:exec\s+|dlx\s+)?|bun\s+x\s+|bun\s+run\s+|node_modules/\.bin/|\./node_modules/\.bin/)?"
_JS_TOOLS = (r"jest|vitest|playwright|cypress|tsc|vue-tsc|next|nuxt|nuxi|vite|webpack|webpack-cli|rollup|"
             r"esbuild|turbo|nx|eslint|astro|remix|parcel|svelte-kit|svelte-check|ng|react-scripts|"
             r"storybook|gatsby|expo|tsup|rspack|rsbuild|mocha|ava")

RULES: Tuple[Rule, ...] = (
    _r("js-install", "js", r"^(?:npm|pnpm|yarn|bun)\s+(?:install|i|ci|add|update|upgrade)\b|^yarn\s*$"),
    _r("js-script", "js", r"^(?:npm|pnpm|yarn|bun)\s+(?:run(?:-script)?\s+)?"
                          r"(?:build|test|t|e2e|test:\S+|build:\S+|lint|typecheck|type-check|check|compile|bundle|"
                          r"generate|export|storybook|start)\b"),
    _r("js-tool", "js", r"^" + _JS_RUNNER + r"(?:" + _JS_TOOLS + r")(?:\s|$)"),
    _r("py-install", "python", r"^(?:pip3?|python3?(?:\.\d+)?\s+-m\s+pip|uv\s+pip|pipx)\s+install\b|"
                               r"^(?:uv\s+(?:sync|lock|tool\s+install)|poetry\s+(?:install|update|lock)|pdm\s+(?:install|sync)|"
                               r"pipenv\s+(?:install|sync)|conda\s+(?:install|create|update|env)|mamba\s+(?:install|create))\b"),
    _r("py-test", "python", r"^(?:pytest|py\.test|python3?(?:\.\d+)?\s+-m\s+(?:pytest|unittest|mypy)|"
                            r"uv\s+run\s+(?:python3?\s+-m\s+)?(?:pytest|mypy|pyright)|poetry\s+run\s+(?:pytest|mypy)|"
                            r"tox|nox|mypy|pyright|hatch\s+(?:test|run)|pdm\s+run\s+pytest)(?:\s|$)"),
    _r("rust", "rust", r"^cargo\s+(?:\+\S+\s+)?(?:build|b|test|t|check|c|clippy|run|r|install|bench|doc|nextest|tauri|leptos)\b|^rustc\s"),
    _r("go", "go", r"^go\s+(?:build|test|install|vet|run|generate|mod\s+download)\b"),
    _r("jvm", "jvm", r"^(?:\./gradlew|gradlew|gradle|mvn|\./mvnw|mvnw|sbt|lein|bazel|bazelisk|kotlinc|javac)(?:\s|$)"),
    _r("native", "native", r"^(?:make|gmake|ninja|scons|cmake\s+--build|meson\s+(?:compile|test)|"
                           r"gcc|g\+\+|clang|clang\+\+|c\+\+|cc|zig\s+build|swift\s+build|dotnet\s+(?:build|test|publish|restore))(?:\s|$)"),
    _r("container", "container", r"^(?:docker|podman|nerdctl)\s+(?:build|buildx\s+build|compose\s+(?:up|build)|"
                                 r"image\s+build|run)\b|^docker-compose\s+(?:up|build)\b", isolate=False),
    _r("media", "media", r"^(?:ffmpeg|magick|convert|whisper|whisper-cli|whisper\.cpp|llama-cli|llama-server|"
                         r"ollama\s+(?:run|serve|pull|create)|blender|gs|tesseract|ocrmypdf)(?:\s|$)"),
    _r("browser", "browser", r"^(?:chromium|chromium-browser|google-chrome(?:-stable)?|firefox|"
                             r"python3?\s+-m\s+playwright\s+install)(?:\s|$)"),
    _r("agent", "agent", r"^hermes\s+(?:(?:-p|--profile)\s+\S+\s+)?(?:chat|-z|--oneshot|kanban\s+work)\b|"
                         r"^(?:claude|codex|opencode|aider|gemini)(?:\s+-p|\s+exec|\s+run|\s+--print)\b"),
)

_NODE_BASED = re.compile(r"^(?:npm|pnpm|yarn|npx|node|" + _JS_RUNNER.replace("(?:", "(?:", 1) + r"(?:" + _JS_TOOLS + r"))(?:\s|$)")


# `make --version`, `cargo help`, `npm test --help`: asking a tool about itself is never heavy.
_QUERY_ONLY = re.compile(r"^\S+(?:\s+\S+){0,2}\s+(?:--version|-V|--help|-h|help)\s*$")


@dataclass
class Match:
    rule: str
    kind: str
    segment: Segment
    words: str
    words_offset: int
    isolate: bool


@dataclass
class Classification:
    heavy: bool
    matches: List[Match] = field(default_factory=list)

    @property
    def kinds(self) -> List[str]:
        seen: List[str] = []
        for m in self.matches:
            if m.kind not in seen:
                seen.append(m.kind)
        return seen

    @property
    def rules(self) -> List[str]:
        return sorted({m.rule for m in self.matches})

    @property
    def isolate(self) -> bool:
        return any(m.isolate for m in self.matches)

    def summary(self) -> str:
        return ", ".join(self.rules) if self.matches else "light"


def classify(command: str, extra: Sequence[str] = (), exclude: Sequence[str] = ()) -> Classification:
    if not isinstance(command, str) or not command.strip():
        return Classification(False)
    for pattern in exclude:
        try:
            if re.search(pattern, command):
                return Classification(False)
        except re.error:
            continue
    custom = []
    for pattern in extra:
        try:
            custom.append(re.compile(pattern))
        except re.error:
            continue
    matches: List[Match] = []
    for seg in split_segments(command):
        words, offset = command_words(seg.text)
        if not words or _QUERY_ONLY.match(words):
            continue
        for rule in RULES:
            if rule.pattern.search(words):
                matches.append(Match(rule.name, rule.kind, seg, words, offset, rule.isolate))
                break
        else:
            for pattern in custom:
                if pattern.search(words):
                    matches.append(Match("custom", "custom", seg, words, offset, True))
                    break
    return Classification(bool(matches), matches)


# ---------------------------------------------------------------- lighten


@dataclass
class Rewrite:
    command: str
    changes: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def lighten(command: str, *, budget_mb: int, cpus: int, low_memory: bool,
            extra: Sequence[str] = (), exclude: Sequence[str] = ()) -> Rewrite:
    """Return ``command`` with parallelism capped and the Node heap bounded.

    ``budget_mb`` is the memory the job may use; ``low_memory`` says whether that budget is small
    enough that test runners should drop to a single worker.
    """
    result = classify(command, extra, exclude)
    if not result.heavy:
        return Rewrite(command)
    jobs = max(1, min(cpus, budget_mb // 700 if budget_mb > 0 else 1))
    edits: List[Tuple[int, int, str]] = []  # (start, end, replacement) in original coordinates
    changes: List[str] = []
    node_heap = min(budget_mb, max(128, round(budget_mb * 0.7))) if budget_mb > 0 else 0

    for m in result.matches:
        seg = m.segment
        words = m.words
        abs_words = seg.start + m.words_offset
        seg_end = seg.end

        def append(flag: str, why: str) -> None:
            edits.append((seg_end, seg_end, " " + flag))
            changes.append(why)

        # Node heap cap for anything that boots V8 (the cgroup cap would SIGKILL it; a heap
        # limit makes V8 fail with a readable "heap out of memory" instead).
        if node_heap and _NODE_BASED.search(words) and "max-old-space-size" not in seg.text \
                and not re.search(r"(?:^|\s)NODE_OPTIONS=", seg.text):
            prefix = f'NODE_OPTIONS="${{NODE_OPTIONS:+$NODE_OPTIONS }}--max-old-space-size={node_heap}" '
            edits.append((abs_words, abs_words, prefix))
            changes.append(f"capped the Node heap at {node_heap} MB (NODE_OPTIONS)")

        if re.search(r"(?:^|\s|/)jest(?:\s|$)", words) and low_memory and \
                not re.search(r"\s(?:--runInBand|-i|--maxWorkers(?:=|\s)|-w\s?\d|--watch)", words):
            append("--runInBand", "jest: --runInBand (one process instead of a worker pool)")
        elif re.search(r"(?:^|\s|/)playwright\s+test\b", words) and low_memory and \
                not re.search(r"\s(?:--workers|-j)(?:=|\s)", words):
            append("--workers=1", "playwright: --workers=1")
        elif m.rule == "py-test" and low_memory:
            xdist = re.search(r"(\s)(?:-n|--numprocesses)(?:\s+|=)(auto|logical|\d+)\b", seg.text)
            if xdist and xdist.group(2) not in ("0", "1"):
                start = seg.start + xdist.start()
                edits.append((start, seg.start + xdist.end(), xdist.group(1) + "-n 0"))
                changes.append(f"pytest-xdist: -n {xdist.group(2)} -> -n 0 (no worker processes)")
        elif m.rule == "native":
            jflag = re.search(r"(\s)(-j|--jobs)(?:\s*|=)(\d*)(?=\s|$)", seg.text)
            if words.startswith(("make", "gmake")) and jflag:
                wanted = jflag.group(3)
                if not wanted or int(wanted) > jobs:
                    start = seg.start + jflag.start()
                    edits.append((start, seg.start + jflag.end(), f"{jflag.group(1)}-j{jobs}"))
                    changes.append(f"make: {jflag.group(2)}{wanted or ' (unbounded)'} -> -j{jobs}")
            elif words.startswith("ninja") and not jflag and jobs < cpus + 2:
                append(f"-j{jobs}", f"ninja: -j{jobs} (ninja defaults to cpus+2)")
        elif m.rule == "rust" and jobs < cpus and not re.search(r"\s(?:-j|--jobs)(?:\s|=)", words):
            sub = re.match(r"cargo\s+(?:\+\S+\s+)?\S+", words)
            if sub and not re.match(r"cargo\s+(?:\+\S+\s+)?(?:run|r)\b.*\s--\s", words):
                at = abs_words + sub.end()
                edits.append((at, at, f" -j {jobs}"))
                changes.append(f"cargo: -j {jobs}")
        elif m.rule == "go" and jobs < cpus and not re.search(r"\s-p(?:\s|=)\d", words):
            sub = re.match(r"go\s+\S+", words)
            if sub:
                at = abs_words + sub.end()
                edits.append((at, at, f" -p {jobs}"))
                changes.append(f"go: -p {jobs}")
        elif m.rule == "jvm" and re.match(r"(?:\./)?gradlew?\b", words) and \
                "--max-workers" not in words and jobs < cpus:
            append(f"--max-workers={jobs}", f"gradle: --max-workers={jobs}")

    if not edits:
        return Rewrite(command)
    changes = list(dict.fromkeys(changes))  # one line per kind of change
    out = command
    for start, end, replacement in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        out = out[:start] + replacement + out[end:]
    return Rewrite(out, changes)


def needles(command: str, limit: int = 3) -> List[str]:
    """Distinctive substrings of ``command`` that survive any shell quoting Hermes applies
    (no quotes or backslashes), longest first. Used to find the process running it."""
    pieces = re.split(r"['\"\\\n]", command)
    good = sorted({p.strip() for p in pieces if len(p.strip()) >= 4}, key=len, reverse=True)
    return [p[:160] for p in good[:limit]]


def shorten(command: str, width: int = 80) -> str:
    one = " ".join(command.split())
    return one if len(one) <= width else one[: width - 1] + "…"


def iter_rule_names() -> Iterable[str]:
    return (rule.name for rule in RULES)
