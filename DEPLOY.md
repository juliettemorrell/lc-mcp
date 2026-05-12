# Deploy guide - get a permanent URL

The cloudflared quick-tunnel approach (`start-local.command`) is fine for demos but the URL dies when you close the laptop. For a permanent URL pinned to your MCP, use one of these.

## Render (recommended, free, doesn't sleep aggressively)

1. Push this `mcp-server/` folder to a GitHub repo (any visibility).
2. Sign in to render.com.
3. New + > Web Service > Connect the repo.
4. Settings:
   - Name: `lc-mcp` (anything)
   - Region: closest to you
   - Branch: `main`
   - Root directory: `mcp-server`
   - Runtime: Docker (Render auto-detects `Dockerfile`)
   - Plan: Free
5. Click Create Web Service. First build takes ~3 min.
6. Your URL: `https://lc-mcp-XXXX.onrender.com`
7. Po endpoint: `https://lc-mcp-XXXX.onrender.com/mcp`

To enable auth on Render, add an Environment Variable:
- Key: `MCP_BEARER_TOKEN`
- Value: any strong random string

To make report URLs absolute:
- Key: `PUBLIC_BASE_URL`
- Value: `https://lc-mcp-XXXX.onrender.com`

## Fly.io (also free, doesn't sleep)

```bash
brew install flyctl       # macOS
fly auth signup           # or fly auth login
cd mcp-server
fly launch --no-deploy    # accept defaults; fly detects Dockerfile
fly secrets set MCP_BEARER_TOKEN=your-strong-token PUBLIC_BASE_URL=https://your-app.fly.dev
fly deploy
```

URL: `https://your-app.fly.dev`. The first deploy takes ~3 min.

## Railway (paid)

```bash
brew install railway
railway login
cd mcp-server
railway init
railway up
```

Railway gives you a URL like `https://your-app.up.railway.app`. Add the same env vars in their dashboard.

## Confirming the deploy

After any of the above:

```bash
bash test_harness.sh https://your-permanent-url.example.com
python validate_calculators.py https://your-permanent-url.example.com
```

Expected: 26/26 + 15/15 PASS.

## Register in Po

Once you have the permanent URL:

1. Open Po -> Configuration -> MCP Servers.
2. Either edit one of the existing dead entries (Lab Intelligence, Longevity Copilot MCP, ED-Copilot) and replace the URL, or click + Add MCP Server.
3. Friendly Name: `Longevity Copilot MCP`
4. Endpoint: `https://your-permanent-url.example.com/mcp`
5. Transport: Streamable HTTP
6. Auth: Bearer + paste your `MCP_BEARER_TOKEN` if you set one. Otherwise No Authentication (Open).
7. Continue. Po calls `tools/list` and populates the MCP Tools tab. You should see 22 tools.
8. Open Longevity Copilot orchestrator -> Tools tab. Tick the MCP tools you want exposed to the orchestrator. At minimum: `get_patient_labs`, `get_patient_genomics`, `get_patient_medications`, `generate_clinical_pdf`, `fhir_create_diagnostic_report`, `calc_homa_ir`, `calc_egfr_ckdepi_2021`, `calc_ascvd_10yr`, `calc_reference_ranges`, `chart_lab_trend`, `drug_interaction_matrix`.
9. Save. Live in Po.

## After the demo

The MCP keeps running. Add a custom domain in Render/Fly if you want a vanity URL. For HIPAA, swap `HAPI_FHIR_BASE` from the public sandbox to your tenant's Epic R4 base URL with OAuth 2.0 SMART-on-FHIR.
