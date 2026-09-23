# Hermes integration

Prepare the service URL, tenant credential and a tenant-scoped `user_id`.
Hermes needs neither Agent registration nor an OpenViking Root Key.
Run from the repository root:

```bash
export TEAMEVOLVER_URL="https://teamevolver.example.com"
export TEAMEVOLVER_USER_ID="alice"
# TEAMEVOLVER_TENANT_TOKEN is supplied securely by your operator.

python session_ingestion/push/hermes/install.py   --url "$TEAMEVOLVER_URL" --user-id "$TEAMEVOLVER_USER_ID"   --tenant-token "$TEAMEVOLVER_TENANT_TOKEN"

python teamEvolver/integrations/hermes_skill_sync/install.py   --backend service --url "$TEAMEVOLVER_URL" --user-id "$TEAMEVOLVER_USER_ID"   --tenant-token "$TEAMEVOLVER_TENANT_TOKEN"

hermes hooks list
hermes hooks test pre_llm_call
hermes hooks test on_session_end
```


The feed installer also installs the Context provider; use `--no-context-provider` for Session-only setup.
Identity configuration contains `base_url`, `tenant_token` and `user_id`, saved with mode 0600.
Use `--hermes-home` for another Hermes directory. Only installed hook commands are approved;
`--no-approve` leaves interactive approval to Hermes.

Sessions use v2, and all nine Context routes send user_id.
The local spool's `producer_id` is an idempotency/partition key, never server identity.
Skill pull caches versions and hashes and leaves unchanged files untouched. It refreshes before an
eligible model call with a 15-second minimum interval.

A synthetic end-of-session hook test has no real DB Session, so skipped is expected.
For acceptance, complete a real Session and verify its declared user in the console.
Publish a Skill and verify the new local version before a subsequent eligible model call.
See [v2](./06-protocol-v2.md), [Context](../api/04-context-workspace.md) and [Skill pull](../api/06-skill-sync.md).
