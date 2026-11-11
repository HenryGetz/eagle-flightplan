# Documentation map

Start with the question you need answered, then open that file.

| document | answers |
| --- | --- |
| [login.md](login.md) | How the Entra ID SAML + TOTP login works, what the saved cookie state contains, and why a session dies. |
| [banner-api.md](banner-api.md) | Which Banner 9 SSB endpoints the client uses, their parameters, response shapes and error behaviour. |
| [audit.md](audit.md) | How the Ellucian Degree Works audit JSON maps to requirement groups, blocks, options and statuses. |
| [planner.md](planner.md) | How requirements become a conflict-free draft and a multi-term roadmap, including ranking and prerequisites. |
| [rmp-api.md](rmp-api.md) | How the Rate My Professors GraphQL search/detail requests work and how names are matched. |
| [mcp.md](mcp.md) | How to register and use the stdio MCP server: tool table, examples, error contract and session recovery. |
| [../README.md](../README.md) | Install, CLI commands, safety model, limitations and repository layout. |

The library entry points are `unw_planner.cli` and `unw_planner.mcp_server`;
both call the same payload functions, so CLI output and MCP tool output agree.
