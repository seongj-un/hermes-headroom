import pytest

from headroom import classify as cls


@pytest.mark.parametrize("command,rule", [
    ("npm install", "js-install"),
    ("npm ci --omit=dev", "js-install"),
    ("pnpm i", "js-install"),
    ("yarn", "js-install"),
    ("npm run build", "js-script"),
    ("npm test", "js-script"),
    ("pnpm run test:unit", "js-script"),
    ("npx jest src", "js-tool"),
    ("npx --yes playwright test", "js-tool"),
    ("./node_modules/.bin/tsc -p .", "js-tool"),
    ("pnpm exec vitest run", "js-tool"),
    ("next build", "js-tool"),
    ("pip install -r requirements.txt", "py-install"),
    ("python3 -m pip install torch", "py-install"),
    ("uv sync", "py-install"),
    ("uv pip install numpy", "py-install"),
    ("pytest -q", "py-test"),
    ("python -m pytest tests/", "py-test"),
    ("uv run pytest -x", "py-test"),
    ("cargo build --release", "rust"),
    ("cargo +nightly test", "rust"),
    ("go test ./...", "go"),
    ("./gradlew assembleDebug", "jvm"),
    ("make -j8", "native"),
    ("cmake --build build", "native"),
    ("docker build -t app .", "container"),
    ("ffmpeg -i in.mp4 out.webm", "media"),
    ("hermes chat -q 'hi'", "agent"),
    ("hermes -p work chat -q 'hi'", "agent"),
])
def test_heavy(command, rule):
    result = cls.classify(command)
    assert result.heavy, command
    assert rule in result.rules


@pytest.mark.parametrize("command", [
    "ls -la",
    "git status",
    'git commit -m "npm install docs"',
    "echo npm run build",
    "cat package.json | grep build",
    "grep -r pytest .",
    "npm --version",
    "npm view react version",
    "pip --version",
    "pip list",
    "cargo --version",
    "go version",
    "make --version",
    "# npm install",
    "docker ps",
    "",
])
def test_light(command):
    assert not cls.classify(command).heavy, command


def test_compound_and_wrappers():
    assert cls.classify("cd web && npm ci && npm run build").rules == ["js-install", "js-script"]
    assert cls.classify("CI=1 NODE_ENV=test npx jest").heavy
    assert cls.classify("time nice -n 10 cargo build").heavy
    assert cls.classify("(cd app; pytest)").heavy
    assert cls.classify("timeout 600 npm test").heavy
    assert cls.classify("echo start; make 2>&1 | tee build.log").heavy


def test_redirection_is_not_background():
    segs = [s.text for s in cls.split_segments("make 2>&1 | tee log &> /dev/null")]
    assert segs[0] == "make 2>&1"


def test_exclude_and_extra():
    assert not cls.classify("npm install", exclude=[r"^npm install$"]).heavy
    custom = cls.classify("python3 train.py --epochs 3", extra=[r"^python3? train\.py"])
    assert custom.heavy and custom.rules == ["custom"]


def test_container_not_isolated():
    assert not cls.classify("docker build .").isolate
    assert cls.classify("docker build . && npm test").isolate


def lighten(command, budget=700, cpus=4, low=True):
    return cls.lighten(command, budget_mb=budget, cpus=cpus, low_memory=low)


def test_jest_run_in_band():
    r = lighten("npx jest src/")
    assert "--runInBand" in r.command
    assert r.command.startswith('NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--max-old-space-size=490" npx jest src/')
    assert lighten("npx jest -i").command.count("--runInBand") == 0
    assert "--runInBand" not in lighten("npx jest", low=False).command


def test_node_heap_prefix_once():
    r = lighten("cd web && npm run build")
    assert r.command == 'cd web && NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--max-old-space-size=490" npm run build'
    assert lighten('NODE_OPTIONS=--max-old-space-size=300 npm run build').command == \
        'NODE_OPTIONS=--max-old-space-size=300 npm run build'


def test_pytest_xdist():
    assert lighten("pytest -n auto tests").command == "pytest -n 0 tests"
    assert lighten("pytest -n 4").command == "pytest -n 0"
    assert lighten("pytest -n 1").command == "pytest -n 1"
    assert lighten("pytest -q").command == "pytest -q"


def test_make_jobs():
    assert lighten("make -j8", budget=700, cpus=4).command == "make -j1"
    assert lighten("make -j", budget=1500, cpus=4).command == "make -j2"
    assert lighten("make -j1").command == "make -j1"
    assert lighten("make").command == "make"


def test_cargo_and_go():
    assert lighten("cargo build --release", budget=700, cpus=4).command == "cargo build -j 1 --release"
    assert lighten("cargo build", budget=700, cpus=1).command == "cargo build"
    assert lighten("cargo build -j 2").command == "cargo build -j 2"
    assert lighten("go test ./...", budget=700, cpus=4).command == "go test -p 1 ./..."


def test_playwright_workers():
    r = lighten("npx playwright test")
    assert r.command.endswith("npx playwright test --workers=1")


def test_light_command_untouched():
    r = lighten("ls -la")
    assert not r.changed and r.command == "ls -la"


def test_needles():
    marks = cls.needles("npm run build && echo 'done here'")
    assert marks and all("'" not in m for m in marks)
    assert "npm run build && echo" in marks[0]
