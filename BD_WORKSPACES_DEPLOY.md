# BD workspace — multiple workspaces + custom columns — deploy notes (Oct 2026)

Two additions to Clients -> "BD workspace":

1. **Multiple workspaces.** The switcher at the top of the BD sidebar lists the
   workspaces (e.g. "IT Staffing", "Leadership Hiring") with their company
   counts. Each workspace has its own companies, and the people, deals, tasks
   and notes of those companies. It also has its own views and its own custom
   columns. BD Home shows the open workspace only.
2. **Custom columns, like Airtable.** Click **+** at the right end of the table
   header (or Options -> "+ Add a custom field"). Pick a name and a type: Text,
   Long text, Number, Currency, Percent, Date, Single select (with options),
   Checkbox, Team member, URL, Email or Phone. Custom columns work like the
   built-in ones: inline edit, filter, sort, search, Calculate, Kanban (single
   select), saved views and the side panel ("Custom fields" section). The
   column header menu has "Edit field" (rename, edit options) and
   "Delete field".

## Rules

| Action | Who |
|---|---|
| Create / rename / delete a workspace | Admin |
| Add / edit / delete a custom column | Admin |
| Fill in custom values | Anyone who can use BD (BD stays admin-only for recruiter accounts, as before) |
| Move a company to another workspace (side panel -> Workspace -> Move) | Admin |

- Everything that existed before deploy is in the **Main** workspace. Main
  can't be renamed or deleted.
- A workspace can only be deleted when it has no companies. Its custom
  columns and views are removed with it.
- Company names stay unique across all workspaces (same duplicate check as
  before). Adding a person or deal to a company in another workspace is
  refused.
- A field's type can't be changed later (add a new field instead).
  Removing a select option empties the records that used it. Renaming an
  option keeps them.
- Deleting a field hides it and removes it from every saved view of that
  workspace. The values stay in the database.
- Custom values belong to a workspace. A company moved to another workspace
  shows that workspace's custom columns; its old values stay stored but
  aren't shown.

## Database (additive only)

- New tables: `bd_workspaces`, `bd_custom_fields`, `bd_field_values`.
- New columns: `crm_clients.bd_workspace_id`, `bd_views.workspace_id`
  (default 0 = Main).
- Nothing renamed or removed; no data migration.

Files: `modules/bd_records.py`, `index.html`, new test
`test_bd_workspaces.py` (4 tests).

## API

    GET    /api/bd/workspaces                      list (+ company counts)
    POST   /api/bd/workspaces                      {name, color}
    PATCH  /api/bd/workspaces/<id>                 {name, color, description}
    DELETE /api/bd/workspaces/<id>                 only when empty
    POST   /api/bd/records/companies/<id>/workspace {workspace_id}
    POST   /api/bd/fields?ws=<id>                  {object, label, type, options}
    PATCH  /api/bd/fields/<id>                     {label, options, width}
    DELETE /api/bd/fields/<id>

Every existing `/api/bd/...` call takes `?ws=<id>` (absent = Main). Custom
values are read and written as `cf_<field id>` keys on the normal record API.

## Rollback

Redeploy the previous build. It ignores the new tables and columns, so every
company from every workspace shows up together in one BD list again. No data
is lost.

## Test

    pytest test_bd_workspaces.py -q
