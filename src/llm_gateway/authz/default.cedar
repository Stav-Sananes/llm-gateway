// Default tool-call policies for the demo MCP servers (see docs/design/07-cedar-policy.md).
// Cedar denies anything no policy permits, and any forbid beats every permit.
// Point GATEWAY_POLICY_FILE at your own file to replace these.

// sql server: read-only queries, and only on the analytics and public schemas.
@id("sql-select-allowed-schemas")
permit (principal, action == Action::"call_tool", resource)
when {
  resource.server == "sql" && resource.name == "query" &&
  context has sql && context.sql.parsed && context.sql.only_select &&
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

// fs server: write only inside /sandbox (the path is normalized first, so ../ can't escape).
@id("fs-write-sandbox")
permit (principal, action == Action::"call_tool", resource)
when {
  resource.server == "fs" && ["write_file", "edit_file", "create_directory"].contains(resource.name) &&
  context.paths has path && context.paths.path like "/sandbox/*"
};

// fs server: moving files is allowed in /sandbox, but a human approves each move.
@id("fs-move-needs-approval")
@decision("require_approval")
permit (principal, action == Action::"call_tool", resource)
when {
  resource.server == "fs" && resource.name == "move_file" &&
  context.paths has source && context.paths.source like "/sandbox/*" &&
  context.paths has destination && context.paths.destination like "/sandbox/*"
};

// Never touch the gateway's own secrets, whatever else permits it.
@id("fs-never-env-files")
forbid (principal, action == Action::"call_tool", resource)
when { resource.server == "fs" && context.paths has path && context.paths.path like "*.env" };
