# Documentation map

Start with the question you need answered, then open that file.

| document | answers |
| --- | --- |
| [setup.md](setup.md) | First-run setup for a new machine: install, credentials, browser and login, without reading source. |
| [login.md](login.md) | How the Entra ID SAML + TOTP login works, what the saved cookie state contains, and why a session dies. |
| [banner-api.md](banner-api.md) | Which Banner 9 SSB endpoints the client uses, their parameters, response shapes and error behaviour. |
| [audit.md](audit.md) | How the Ellucian Degree Works audit JSON maps to requirement groups, blocks, options and statuses. |
| [planner.md](planner.md) | How requirements become a conflict-free draft and a multi-term roadmap, including ranking and prerequisites. |
| [rmp-api.md](rmp-api.md) | How the Rate My Professors GraphQL search/detail requests work and how names are matched. |
| [registration.md](registration.md) | What is known about registration time-ticket windows and the guarded `snipe` flow, including what remains unverified. |
| [porting.md](porting.md) | Exactly what is UNW-specific — tenant, hosts, term codes, attributes, RMP school id, login selectors — and what to change for another school. |
| [mcp.md](mcp.md) | How to register and use the stdio MCP server: tool table, examples, error contract and session recovery. |
| [../README.md](../README.md) | Install, CLI commands, safety model, limitations and repository layout. |

The entry points are `eagle_flightplan.cli` and `eagle_flightplan.mcp_server`; both sit
on the same domain layer (`auth`, `banner`, `audit`, `catalog`, `planner`, `report`).
