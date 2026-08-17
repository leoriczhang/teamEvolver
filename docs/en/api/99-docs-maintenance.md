# Documentation Maintenance Guide

This document describes writing standards, maintenance processes, and quality check mechanisms for teamEvolver documentation. Docs are maintained as Markdown source files and browsed after login via the built-in console reader (sidebar tree navigation, full-text search, zh/en language switching, Markdown/GFM rendering).

## When to Update Documentation

Documentation must be updated synchronously in the following situations:

| Scenario | Documents Requiring Update |
|---------|---------------------------|
| API interface changes (adding, modifying, deleting endpoints, parameter changes) | Corresponding files in `docs/en/api/`, and `docs/en/agent-integrations/02-protocol-v1.md` |
| New features or capabilities (new capabilities, new integration methods) | Related files in `docs/en/agent-integrations/`, new API documentation when necessary |
| Configuration changes (adding/modifying/deprecating environment variables, config parameters) | Related guide documents and parameter descriptions in API documentation |
| Concept changes (terminology, architecture, process adjustments) | Terminology usage in `docs/en/concepts/` and related documents |
| New Agent integration | Add corresponding integration guide in `docs/en/agent-integrations/` |
| Code path restructuring causing broken references | All documents referencing old paths |
| JSON Schema changes | `docs/schemas/` and documents referencing schemas |

## File Structure Conventions

### Directory Structure

```
docs/
├── zh/                   # Chinese documentation
│   ├── getting-started/  # Getting started (01-03)
│   ├── concepts/         # Core concepts (01-08)
│   ├── guides/           # User guides (01-08)
│   ├── agent-integrations/  # Agent integration docs (01-05)
│   ├── api/              # API reference docs (01-10, 99)
│   ├── faq/              # Frequently asked questions
│   └── about/            # About
├── en/                   # English documentation (mirrors zh/ structure)
├── design/               # Design notes (language-agnostic)
├── schemas/              # JSON Schema definitions
├── assets/               # Image resources
└── scripts/
    └── check-docs-refs.mjs  # Reference validation script
```

### File Naming Conventions

- Use **numeric prefixes** to control ordering: `01-overview.md`, `02-protocol-v1.md`, `99-docs-maintenance.md`
- Numeric prefix and title are connected by a hyphen `-`
- Use lowercase English filenames, words separated by hyphens
- Overview files are uniformly named `01-overview.md`
- Documentation maintenance guides are uniformly named `99-docs-maintenance.md`

### Chinese-English Parallelism

- `docs/zh/` and `docs/en/` maintain identical directory structure and file naming
- When adding Chinese documentation, create the English version synchronously when conditions permit
- The console docs reader automatically scans `docs/zh/`, `docs/en/`, and `docs/design/` directories, sorts by numeric prefix, extracts display names from level 1 headings; new files require no manual registration

### Section Hierarchy

- Document titles use `#` (level 1 heading, title after removing numeric prefix from filename)
- Main sections use `##` (corresponding to major sections in document structure)
- Subsections use `###`
- Deeper levels use `####`; avoid exceeding four levels when possible

## Code Reference Format

Follow these formats when referencing code files and symbols:

```
teamEvolver/<module>/<file>.py:<symbol_name>
```

Examples:

- `teamEvolver/integrations/agent_registry.py:register_agent` -- Module-level function
- `teamEvolver/proxy/agent_context.py:119` -- Specific line number (used when necessary)
- `teamEvolver/integrations/replay_adapters.py:82` -- HttpReplayAdapter class
- `teamEvolver/integrations/agent_protocol.py` -- Entire file (when no specific symbol)

Notes:

- Paths always start with `teamEvolver/`
- Use forward slash `/` as path separator
- Symbol names use class or function names without parentheses
- Reference line numbers only when precise pointing to specific code is needed; avoid frequent line number usage (code changes will cause invalidation)
- File path references use `file:///` absolute path links; the validation script verifies file/directory existence

## API Document Structure

Each API document follows this structure (referencing OpenViking style):

### 1. API Implementation Overview

Explain the interface purpose, core design principles, and related code entry points.

Includes:
- Interface functionality description
- Authentication requirements
- Core design principles (e.g., opaque references, idempotency, etc.)
- Related code file paths

### 2. Interface and Parameter Specification

List all interfaces, methods, and parameters using tables.

- Section by endpoint, including HTTP method and path
- Parameter tables include: field name, type, required/optional, description
- Enumerate all possible values for enums
- Use subtables or indentation for nested objects

### 3. Usage Examples

Provide runnable `bash` code blocks (curl examples):

```bash
curl -X POST "http://localhost:52010/internal/agents/register" \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{...}'
```

JSON response examples use `json` code blocks.

### 4. Response Contract and Error Handling

- List all response fields with their types and descriptions
- Provide success response examples
- List all error codes using tables: HTTP status code, error message, cause
- Explain behavioral conventions such as idempotency, caching, rate limits

## Document Reference Checks

The project provides a document reference check script to validate code reference paths, file links, and images.

```bash
node docs/scripts/check-docs-refs.mjs
```

This script checks:
1. Whether files/directories exist for all `file:///` references
2. Whether files and symbols (class/def/async def) exist for all `teamEvolver/...` code references
3. Whether Markdown relative links between documents point to existing files
4. Whether image references point to existing image files
5. Whether zh/en parallel files are missing (warnings)

Run this check before committing documentation changes to ensure no broken references.

## Adding New Documentation Pages

### Adding Files to Existing Sections

Place Markdown files in the appropriate directory (e.g., `docs/en/guides/`), with filenames starting with appropriate numeric prefixes. The console reader automatically scans and loads the file, sorts by filename number, and extracts the display name from the level 1 heading (`#`).

### Adding New Section Groups

The `section_order` list in backend `DocsMixin._build_docs_tree` maintains section ordering. When adding a new top-level section directory (e.g., `docs/en/plugins/`), add the new section name to this list and add zh/en labels in `_section_label()`.

## Screenshot Update Process

Note the following for screenshots used in documentation:

1. **Anonymize First:** Before taking screenshots, ensure no sensitive information appears in the interface (internal URLs, usernames, API Keys, company names, customer data, etc.).
2. **Full-page Scroll Capture:** Use browser developer tools or screenshot tools to capture complete scrollable areas, avoiding truncated content.
3. **Save to assets:** Save screenshots to the `docs/assets/` directory with descriptive filenames. Reference in docs via `/docs-assets/<filename>`.
4. **English Interface:** English documentation uses English interface screenshots.
5. **Keep Up to Date:** Update corresponding screenshots when there are major UI changes.

## Writing Style Guide

### Terminology Consistency

Keep core terminology consistent, avoid synonyms:

| Term | Correct Usage | Avoid |
|------|--------------|-------|
| Agent | Refers to integrated AI Agent runtimes | "proxy", "intelligent agent" |
| Skill | Refers to team/personal skill packages | "skill" (use Skill in English context), "prompt" |
| Session | Refers to a complete conversation session | "dialog" |
| Context | Refers to context workspace | (use "Context" consistently) |
| Replay | Refers to True Replay validation | "playback" |
| Capability | Refers to Agent-declared capabilities | "function", "feature" |
| integration_id | Integration ID | "agent_id" (note distinction) |
| external_subject | External subject identifier | "user ID", "username" |

### Language Standards

- Use professional technical English
- Code identifiers (variable names, function names, class names, endpoint paths, JSON field names, configuration key names) remain in English, untranslated
- Use active voice, avoid colloquial expressions
- Keep sentences concise and clear; avoid lengthy compound sentences
- Use lists and tables for structured information; avoid large blocks of text

### Markdown Standards

- Use `##` as main section markers
- Do not use emojis (project standard)
- Specify language for code blocks: `` `bash ``, `` `json ``, `` `python ``, `` `yaml ``
- Use standard Markdown table syntax
- Use relative paths for links
- Do not use HTML tags (unless necessary)

## Verification Checklist

Before committing documentation changes, verify each item:

- [ ] All code reference paths actually exist under `/home/zhangpengkun/teamEvolver/`
- [ ] `node docs/scripts/check-docs-refs.mjs` runs without errors
- [ ] New document filenames follow numeric prefix conventions
- [ ] Level 1 headings (`#`) accurately reflect document topics
- [ ] API documents follow four-section structure (implementation overview, parameter specification, usage examples, responses and errors)
- [ ] All code blocks specify correct language identifiers
- [ ] Tables are aligned and readable with complete parameter descriptions
- [ ] Parameters in curl examples match parameter tables
- [ ] No emojis used
- [ ] Screenshots have been anonymized
- [ ] Chinese and English structures remain consistent (when adding files)
- [ ] Terminology usage is consistent
- [ ] All required fields are marked "Yes" in parameter tables
- [ ] Error codes cover all error situations defined in code
