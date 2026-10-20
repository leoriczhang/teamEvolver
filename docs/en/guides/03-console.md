# Web Console User Guide

The console and the API are served by the same `api` role process, at `http://127.0.0.1:52010/` by default. The frontend source is in `web-ui/src/`, navigation is defined in `web-ui/src/App.tsx`, and the build output is in `teamEvolver/web/dist/`. When the console is not built, `GET /` returns 503.

## Sign-in and Setup

When the system has no users, the console shows the administrator setup page, which calls `POST /api/auth/bootstrap`. The form is prefilled with username and password `admin`; production deployments must change the password before submitting.

After setup, other people can register on the sign-in page (`POST /api/auth/register`). The registration entry is hidden when `self_registration_enabled=false`. A newly registered account has no tenant membership; a tenant administrator must add it in "Members & Access" before it can enter the tenant.

Console sign-in uses an HttpOnly Session Cookie; the Root Key is never sent to the browser. Roles and permissions are described in [Console RBAC](../api/20-console-rbac.md).

## Tenant Switching and Visibility

The tenant selector at the top of the sidebar lists the tenants the current user can enter. Switching reloads the whole page so no cross-tenant cache survives. Menu visibility is determined by role only:

| Marker | Visible to |
|------|----------|
| None | Users with a tenant membership |
| Tenant administrator | `tenant_admin` of the current tenant |
| Platform administrator | `platform_role=platform_admin` |

A page can be opened directly with `?view=<key>`, for example `/?view=workspace`; tabs of the run overview are selected with `&tab=<key>`.

## Navigation

| Group | Page | `view` | Visibility |
|------|------|--------|--------|
| Skill Mining | Knowledge Base Mining | `mine-knowledge-base` | Tenant administrator, OpenViking required |
| Workbench | Run Overview | `dashboard` | Member |
| Workbench | Experiment Workbench | `workspace` | Member |
| Workbench | Runtime Status | `health` | Tenant administrator |
| Agents & Capabilities | Personal & Team Assets | `agent-assets` | Member |
| Agents & Capabilities | Platform Assets | `platform` | Tenant administrator |
| Agents & Capabilities | Members & Access | `members` | Tenant administrator |
| Knowledge & Methods | Experience Library | `experiences` | Member |
| Evolution & Experiments | Datasets | `datasets` | Member |
| Evolution & Experiments | Evolution Pipeline | `prompt-studio` | Tenant administrator |
| Review & Publish | Review & Publish Entry | `review-center` | Member |
| Review & Publish | Access Audit | `identity-audit` | Platform administrator |
| Review & Publish | Trace Observability | `observability` | Platform administrator |
| Settings & Access | Platform Users | `users` | Platform administrator |
| Settings & Access | Tenant Management | `tenants` | Platform administrator |
| Settings & Access | Model Settings | `model` | Tenant administrator |
| Settings & Access | Data Source Onboarding | `datasource` | Tenant administrator |

## Skill Mining

### Knowledge Base Mining

Knowledge base mining compiles a raw knowledge directory in OpenViking into a structured Wiki. The page has two file trees, "Knowledge Sources" and "Knowledge Base":

- Upload files or folders to the knowledge sources; the service converts them to Markdown
- Browse, edit and delete source and Wiki files, and view backlinks
- Enter mining instructions, choose the incremental compile start, launch OpenViking Compile and follow stage progress
- Generate an HTML knowledge base graph

Without OpenViking configured, the page shows a configuration notice and links to "Runtime Status". The full workflow is in the [Knowledge Base Mining Guide](./07-skill-miner.md).

## Workbench

### Run Overview

The run overview has four tabs:

| Tab | `tab` | Content |
|------|-------|------|
| Overview | `overview` | Service status, Session queue and history, Candidate summary, Skill versions |
| Candidate Review | `candidates` | Candidate details, bundle diff, A/B replay details and publish decision |
| Evolution Audit | `audit` | Sessions consumed by each evolution cycle, Candidates produced and publish results |
| Filter Audit | `filter` | valuable / chitchat classification of Sessions before queuing |

A Candidate first passes the Checklist completion gate, then efficiency is compared by interaction turns, tool calls and token usage; see [Checklist](../concepts/07-checklist.md). Candidate review supports the user marks `reviewed` and `rejected`; publishing (including force publish) marks the Candidate `adopted` automatically.

### Experiment Workbench

The experiment workbench combines Skill bundle editing and True Replay debugging in one view:

- The file tree on the left edits `SKILL.md`, scripts and reference files, with quick open and new file
- Choose a Test Dataset and run parameters, then run True Replay in the debug console
- Candidate trace and file diff are shown at the bottom

Unsaved text edits take part in the run as the Candidate; the formal Skill does not have to be overwritten first. Only tenant administrators can save, and a save creates a new version through SkillHub.

### Runtime Status

The runtime status page aggregates checks for the service, PostgreSQL pool, object storage, OpenViking, model, Replay adapter, service logs and Agent pull, each with a suggested action.

The "OpenViking Deployment" panel selects Volcano Engine OpenViking, local self-hosted, or remote self-hosted (with an Endpoint override), and configures the Account and Root Key. Saving hot-reloads the OpenViking integration without a process restart.

## Agents & Capabilities

### Personal & Team Assets

The assets page browses two OpenViking root spaces:

| Space | Root URI | Write access |
|------|--------|--------|
| Personal memory | `viking://user` | The owner |
| Team resources | `viking://resources` | Tenant administrator |

The file tree supports search, Markdown/JSON/code preview and directory L0/L1 summaries. When the server has the OpenViking CLI installed, the page shows a built-in terminal; regular users can run read-only commands only.

Multi-file editing:

1. Click "Edit" to enter edit mode.
2. Edit several text files; switching files keeps drafts.
3. Click "Finish Editing" to review per-file line diffs.
4. Click "Confirm Save" to submit the batch.

The server compares each file with its content at the start of editing; if any file was changed by someone else, the whole batch returns 409 and the drafts are kept. A batch holds at most 100 files, each ≤2 MiB and ≤16 MiB in total. The API is in [Workspace and Platform Assets API](../api/23-workspace-assets.md).

### Platform Assets

Platform assets show, read-only, the task payloads of the current tenant in the PostgreSQL ObjectStore, grouped as ingest originals, Session stage results and Memory Compile results. The page offers a logical key tree, content preview and per-category counts, and does not depend on OpenViking. These objects are never served to Agents.

### Members & Access

On this page a tenant administrator can:

- Add members by user ID and assign the `tenant_admin` or `member` role
- Rotate the tenant Key; the new Key is shown once
- Set the tenant runtime quota

Agents connect with the tenant Key plus a declared `user_id`; console membership and Agent identity are independent. The default tenant's access credential is configured by a platform administrator.

## Knowledge & Methods

### Experience Library

The experience library shows Skill defect experiences and exemplary practices produced by Session analysis, filterable by Skill and keyword. Experiences come from `used_skills` in Sessions; the Skill does not need to be uploaded. Tenant administrators can manage successful experience sync on the same page; see [Successful Experience Sync](./16-successful-experience-sync.md).

## Evolution & Experiments

### Datasets

The datasets page creates Test Datasets from Sessions, edits the initial query, output Checklist and trajectory Checklist, exports datasets and starts batch replay validation. Batch replay concurrency is 1–2 and the per-case timeout is 30–1800 seconds.

### Evolution Pipeline

The evolution pipeline page has two tabs:

- **Skills Self-Evolution**: edit per-stage prompts, models and process parameters, and test stage output with real input; see the [Prompt Studio Guide](./08-prompt-studio.md)
- **Team Memory Self-Evolution**: choose the OpenViking Account and members, run Team Memory manually in "aggregation only", "maintenance only" or "aggregation + maintenance" mode, and configure the aggregation and maintenance Skills; see [Team Memory Aggregation API](../api/11-team-memory-aggregation.md)

## Review & Publish

"Review & Publish Entry" links to Skill Candidate Review in the run overview. Published Skills and team Memory each retain their maintenance entry.

Platform administrators view platform access, membership and identity change records in "Access Audit", and configure global outbound Langfuse tracing in "Trace Observability", including sample rate and whether model input and output are captured.

## Settings & Access

### Platform Users and Tenant Management

Platform administrators manage sign-in accounts and platform identities in "Platform Users", and create or disable tenants in "Tenant Management". When a tenant is created, the system looks up an OpenViking Account with the same name as `tenant_id`; an existing Account is bound, otherwise one is created with the Root Key.

### Model Settings

Tenant administrators configure the tenant's default model, Base URL, API Key, Max Tokens and Temperature, and can test the connection. Per-stage model overrides are managed in "Evolution Pipeline".

### Data Source Onboarding

The data source onboarding page manages the tenant's Session data source:

- Edit data source connection and filters
- Upload and edit adapter source, save it as an immutable version, and verify conversion with draft tests
- Preview and manually pull Sessions, with business time window and system completion time
- Enable the daily scheduled pull and set the trigger time and time zone

Adapters run with the service process's permissions; upload trusted code only. Steps are in the [Data Source Onboarding Guide](./10-datasource-onboarding.md).

## Related Documents

- [Configuration Reference](./01-configuration.md)
- [Console RBAC](../api/20-console-rbac.md)
- [Storage Spaces and Layout](../concepts/09-storage-layout.md)
