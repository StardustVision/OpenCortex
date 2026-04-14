# Repository Guidelines

## Project Structure & Module Organization
Core backend code lives in `src/opencortex/`. Key areas include `http/` for FastAPI routes, `context/` for session lifecycle, `intent/` for probe/planner/executor retrieval flow, `memory/` and `storage/` for durable records, and `alpha/` / `skill_engine/` / `insights/` for optional higher-level services. Python tests live in `tests/`, benchmark adapters and runners live in `benchmarks/`, design and benchmark notes live in `docs/`, the optional React console lives in `web/`, and the MCP package lives in `plugins/opencortex-memory/`.

## Build, Test, and Development Commands
Install Python dependencies with `uv sync` and run the API locally with `uv run opencortex-server --host 127.0.0.1 --port 8921`. Generate or inspect tokens with `uv run opencortex-token generate` and `uv run opencortex-token list`. Run the main test suite with `uv run --group dev pytest`; target a slice with `uv run --group dev pytest tests/test_context_manager.py -q`. Frontend work uses `cd web && npm install && npm run dev` or `npm run build`. MCP package checks run with `cd plugins/opencortex-memory && npm test`.

## Coding Style & Naming Conventions
Python targets 3.10+ and should follow the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html): 4-space indentation, explicit type hints on public APIs, concise docstrings, and clear exception handling. Prefer simple functions over speculative abstractions. Use `snake_case` for modules, functions, and variables, `PascalCase` for classes, and keep FastAPI/Pydantic request-response models close to their transport layer. Match existing naming such as `SearchResult`, `RetrievalPlan`, and `ContextManager`.

## Testing Guidelines
Use `pytest` and add focused regression coverage with every behavior change. Name files `tests/test_<area>.py` and keep test names descriptive, for example `test_context_end_flushes_buffer()`. Retrieval, storage, and benchmark changes should include at least one targeted unit or adapter test; benchmark-facing changes should also record the command or sample dataset used.

## Commit & Pull Request Guidelines
Follow the existing conventional style from `git log`: `feat(memory): ...`, `fix: ...`, `docs: ...`, `test: ...`. Keep commits scoped to one concern. PRs should state the problem, the affected paths, validation commands, and any benchmark impact. Include screenshots only for `web/` changes, and include sample JSON or trace excerpts when changing retrieval or lifecycle behavior.

## Security & Configuration Tips
Do not commit secrets, JWT tokens, or local runtime artifacts from `data*/` or `logs/`. Use isolated collections for benchmark runs, and prefer environment variables or local config files for API keys and model endpoints.
