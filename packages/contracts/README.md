# @inbox/contracts

The single source of truth for everything that crosses a process boundary.

```
src_py/inbox_contracts/   Pydantic v2   ← authored here, ONLY here
        │  scripts/gen.py
        ▼
schema/*.schema.json      JSON Schema   (generated, committed)
        │  scripts/gen-zod.mjs
        ▼
src/generated/*.ts        Zod + TS      (generated, committed)
```

- The backend imports the Python models directly (uv path dependency).
- The cockpit and the executor import the generated Zod package.
- `pnpm run check` regenerates and fails on drift. **Never hand-edit `schema/` or `src/generated/`.**

`extra="forbid"` on the wire models is a security control: it makes "no coordinates, no raw
DOM, no URL" a validation error rather than a convention, and the generated Zod inherits it.
