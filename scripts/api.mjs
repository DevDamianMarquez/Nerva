#!/usr/bin/env node
// Runs API tasks inside apps/api using its virtualenv, identically on Windows/macOS/Linux.
//   node scripts/api.mjs <task> [extra args passed to the underlying tool]
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const apiDir = join(root, "apps", "api");
const isWindows = process.platform === "win32";
const venvPython = join(apiDir, ".venv", isWindows ? "Scripts" : "bin", isWindows ? "python.exe" : "python");

/** Each task is a list of steps; every step is an argument list for the venv's Python. */
const tasks = {
  dev: [["-m", "uvicorn", "nerva.main:app", "--reload", "--port", "8000"]],
  test: [["-m", "pytest"]],
  lint: [
    ["-m", "ruff", "check", "."],
    ["-m", "ruff", "format", "--check", "."],
  ],
  format: [
    ["-m", "ruff", "check", "--fix", "."],
    ["-m", "ruff", "format", "."],
  ],
  typecheck: [["-m", "mypy"]],
  migrate: [["-m", "alembic", "upgrade", "head"]],
  revision: [["-m", "alembic", "revision", "--autogenerate"]],
};

function run(command, args) {
  const result = spawnSync(command, args, { cwd: apiDir, stdio: "inherit" });
  if (result.error) {
    console.error(`Could not run ${command}: ${result.error.message}`);
    process.exit(1);
  }
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function setup() {
  if (!existsSync(venvPython)) {
    const candidates = isWindows ? [["py", ["-3"]], ["python", []]] : [["python3", []], ["python", []]];
    const found = candidates.find(([cmd, pre]) => spawnSync(cmd, [...pre, "--version"]).status === 0);
    if (!found) {
      console.error("Python 3.12+ was not found on PATH.");
      process.exit(1);
    }
    run(found[0], [...found[1], "-m", "venv", ".venv"]);
  }
  run(venvPython, ["-m", "pip", "install", "--upgrade", "pip"]);
  run(venvPython, ["-m", "pip", "install", "-e", ".[dev]"]);
}

const [task, ...extra] = process.argv.slice(2);

if (task === "setup") {
  setup();
} else if (task in tasks) {
  if (!existsSync(venvPython)) {
    console.error("The API virtualenv is missing. Run `npm run api:setup` first.");
    process.exit(1);
  }
  const steps = tasks[task];
  steps.forEach((step, index) => {
    const isLast = index === steps.length - 1;
    run(venvPython, isLast ? [...step, ...extra] : step);
  });
} else {
  console.error(`Unknown task "${task}". Available: setup, ${Object.keys(tasks).join(", ")}`);
  process.exit(1);
}
