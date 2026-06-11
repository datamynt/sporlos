# sporlos-mcp

MCP-server som gir AI-assistenter (Claude, ChatGPT m.fl.) read-only tilgang til
Sporløs-tallene dine via [Stats-API-et](https://sporlos.no/utviklere).

Trygt by design: API-et serverer kun aggregater — enkeltpersoner kan ikke slås
opp, fordi rådataene ikke finnes (ingen IP, ingen cookie, daglig-roterende hash).

## Oppsett

1. Lag en API-nøkkel under «API-tilgang» i [dashbordet](https://sporlos.no/app).
2. Installer og registrer:

```bash
pip install -r requirements.txt

# Claude Code:
claude mcp add sporlos -e SPORLOS_API_KEY=sl_... -- python3 /sti/til/server.py
```

Eller i `.mcp.json`:

```json
{
  "mcpServers": {
    "sporlos": {
      "command": "python3",
      "args": ["/sti/til/integrations/mcp/server.py"],
      "env": { "SPORLOS_API_KEY": "sl_..." }
    }
  }
}
```

Self-host? Sett `SPORLOS_API_BASE=https://analytics.dittdomene.no`.

## Verktøy

| Verktøy | Gjør |
|---|---|
| `list_sites` | nettstedene dine (domene + site-ID) |
| `get_stats` | KPI-er + topplister, med forrige periode |
| `get_timeseries` | per dag (per time for period=1) |
| `get_breakdown` | full liste per dimensjon (pages/sources/countries/…) |
| `get_goals` | mål/konverteringer |
| `get_events` | egendefinerte hendelser |
| `get_anchors` | forseglede dags-aggregater (sha256 + txid) |

Prøv: *«Hent siste 30 dager for nettstedet mitt og forklar hva som driver trafikken.»*
