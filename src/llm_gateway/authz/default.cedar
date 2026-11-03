// Default tool-call policies for the demo MCP servers (see docs/design/07-cedar-policy.md).
// Cedar denies anything no policy permits, and any forbid beats every permit.
// Point GATEWAY_POLICY_FILE at your own file to replace these.
//
// Path rules use the derived sets (policy/facts.py), so they cover every path a call names,
// whatever the argument is called and however many there are.

// sql server: read-only queries on schema-qualified analytics/public tables, using only
// functions the SQL parser knows (no dblink_exec, pg_read_file, setval…).
@id("sql-select-allowed-schemas")
permit (principal, action == Action::"call_tool", resource)
when {
  resource.server == "sql" && resource.name == "query" &&
  context has sql && context.sql.parsed && context.sql.only_select &&
  context.sql.unknown_functions.isEmpty() &&
  ["analytics", "public"].containsAll(context.sql.schemas)
};

// fs server: look around and read anywhere it is allowed to see.
@id("fs-read")
permit (principal, action == Action::"call_tool", resource)
when {
  resource.server == "fs" &&
  ["read_text_file", "read_multiple_files", "list_directory", "directory_tree",
   "search_files", "get_file_info", "list_allowed_directories"].contains(resource.name)
};

// fs server: write only inside /sandbox (paths are normalized first, so ../ can't escape).
@id("fs-write-sandbox")
permit (principal, action == Action::"call_tool", resource)
when {
  resource.server == "fs" &&
  ["write_file", "edit_file", "create_directory"].contains(resource.name) &&
  context.path_count > 0 && ["/sandbox"].containsAll(context.path_roots)
};

// fs server: moving files is allowed within /sandbox, but a human approves each move.
@id("fs-move-needs-approval")
@decision("require_approval")
permit (principal, action == Action::"call_tool", resource)
when {
  resource.server == "fs" && resource.name == "move_file" &&
  context.path_count == 2 && ["/sandbox"].containsAll(context.path_roots)
};

// Never touch .env files (the gateway's own secrets), whatever else permits it.
@id("fs-never-env-files")
forbid (principal, action == Action::"call_tool", resource)
when { resource.server == "fs" && context.path_extensions.contains(".env") };
