# About teamEvolver

teamEvolver is an open-source Agent team capability evolution control plane that enables teams' AI Agents to continuously learn and evolve from real work.

## Project Philosophy

The value of AI Agents lies not in the quality of single answers, but in the continuous accumulation of team capabilities. When an Agent completes a complex task, the experience generated should be reused by the entire team, rather than disappearing into conversation history.

teamEvolver's design principles:

1. **Evidence-driven:** Every Skill/Memory change has traceable Evidence sources
2. **Genuine Validation:** Candidates must pass True Replay validation in isolated environments on real Agent Runtimes
3. **White-box Control:** All Prompts, model parameters, and evolution processes are configurable, observable, and intervenable
4. **Security Isolation:** Replay branches run in sandboxes without producing real external side effects
5. **Governance First:** Checklist gates + human review + complete audit chain

## Tech Stack

- **Backend:** Python 3.10+, FastAPI, Pydantic
- **Frontend:** React 18, TypeScript, Vite, shadcn/ui, Tailwind CSS
- **Storage:** OpenViking (required)
- **Observability:** Langfuse (optional)
- **Documentation:** Markdown sources + built-in console reader (full-text search, bilingual zh/en)

## License

MIT License. See [LICENSE](https://github.com/leoriczhang/teamEvolver/blob/main/LICENSE) for details.

## Contributing

Issues and Pull Requests are welcome:

- Issue Tracking: [GitHub Issues](https://github.com/leoriczhang/teamEvolver/issues)
- Code Repository: [leoriczhang/teamEvolver](https://github.com/leoriczhang/teamEvolver)
- Documentation Standards: See [Documentation Maintenance Guide](../api/99-docs-maintenance)

Before submitting code, please ensure:
1. Existing tests pass: `python -m pytest tests/ -v`
2. Code style is followed: `ruff check teamEvolver/`
3. Related documentation is updated
