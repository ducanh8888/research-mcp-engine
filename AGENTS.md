# Repository Guidelines

## Project Structure & Module Organization

This repository is currently a design and research workspace; the engine is not implemented.

- `docs/design.md`: current v3 architecture, implementation contracts, and P0–P4 roadmap.
- `docs/requirements-notes.md`: user requirements and operating constraints.
- `docs/research.md`: research overview and recommendations.
- `docs/research/01-*.md` through `07-*.md`: detailed upstream and provider investigations.

The proposed `src/research_engine/` package, `tests/`, Python manifest, and Docker Compose configuration are described in the design but do not exist yet. No application assets are present.

## Build, Test, and Development Commands

Run these from the repository root:

- `rg --files docs`: list the documentation files.
- `git diff --check`: check changes for whitespace errors.
- `git diff -- docs/ AGENTS.md`: review tracked documentation changes before committing; inspect new, untracked files separately.

There are no build, runtime, or automated test commands yet. Document working commands here when implementation tooling is added.

## Coding Style & Naming Conventions

Use Markdown headings, short paragraphs, tables for comparisons, and fenced code blocks with language labels. Use relative links between repository documents. Follow the numbered, lowercase, hyphenated research filename pattern, such as `08-provider-topic.md`.

For future Python code, use four-space indentation, `snake_case` modules/functions, and `PascalCase` classes. The design targets Python 3.12; no formatter or linter is configured yet.

## Testing & Documentation Validation

No test framework or coverage threshold is configured. Upstream test results recorded in research notes are not this repository's test results. Check changed links and cited paths, run `git diff --check`, and keep related design and research statements consistent.

For implementation, organize tests under `tests/` using `test_*.py` names. Follow the [definition of done](docs/design.md#171-definition-of-done) and relevant roadmap exit criteria, including failover, authentication, identifier deduplication, and job recovery. Record unavailable live checks as pending.

## Commit & Pull Request Guidelines

Recent commits use concise, descriptive subjects, such as `Clarify design v3 implementation contracts`; no mandatory prefix convention is evident. Keep each commit focused.

PRs should summarize changes, identify affected design sections, link related issues or sources, and report validation performed. Label proposals, locally verified findings, and live-tested behavior clearly. Cite upstream commit hashes and paths when documenting copied code.

## Security & Configuration

Keep provider credentials, bearer tokens, OAuth secrets, and encryption keys out of commits and examples. `.gitignore` excludes `.env` and `.env.*` while allowing `.env.example`; use placeholders in any example configuration.
