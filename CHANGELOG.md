# CHANGELOG

## v1.8 - 2026-05-12

- **Architectural clarity**: Tools split into three layers via `TOOL_LAYERS` map.
  - **data-plane** (14): the MCP's pure data-normalization job. Available to the orchestrator and every specialist.
  - **agent-helper** (15): action services that BELONG to specific specialists. Report Generator owns the four PDF/FHIR-write tools. Trend Analyst owns chart_lab_trend. Drug-Supplement Check owns drug_interaction_matrix. ED-Copilot owns interpret_vitals. Clinical Educator owns generate_patient_education_pdf. The orchestrator never calls these directly; it routes to the specialist.
  - **ops** (3): discover, audit_tail, list_reports.
- **Po registration walkthrough** (PO_REGISTRATION.md) updated with per-agent Tools-tab assignments so the agent narrative is true: Report Generator is literally the only agent in Po that has the PDF tool ticked.

## v1.7 - 2026-05-12

- **Added** `convert_units` tool. Bidirectional US conventional <-> SI for glucose, cholesterol, triglycerides, creatinine, vitamin D, vitamin B12, testosterone, hemoglobin. 14 conversion factors cited from clinical chemistry references (e.g., glucose 100 mg/dL = 5.55 mmol/L exactly).
- **Added** `normalize_panel` tool. Batch normalize a whole vendor lab panel (vendor + list of codes/values) to canonical LOINC + UCUM in one call. Reports unresolved codes.
- **Added** `fhir_create_medication_statement` tool. Posts a MedicationStatement (RxNorm-coded) back to the configured FHIR server. Live-verified against HAPI (server_id 132050886).
- **Added** `fhir_create_condition` tool. Posts a Condition (diagnosis/working impression) with SNOMED CT + ICD-10-CM coding. Live-verified (server_id 132050887).
- **Total FHIR write-back coverage:** Observation, MedicationStatement, Condition, DiagnosticReport. All four resource types verified posting live to HAPI sandbox in a single session.
- **Tool count:** 32.

## v1.6 - 2026-05-12

- **Expanded** wearable list from 12 to **55** sources. New additions:
  - Rings: Ultrahuman Ring AIR, RingConn, Movano Evie, Circul+
  - Watches/straps: Coros, Wahoo, Suunto, Samsung Health
  - Platforms: Google Fit, Android Health Connect, Strava
  - CGMs: Nutrisense, January AI (Dexcom/Libre/Levels/Veri already covered)
  - BP/cardiac: Omron Connect, QardioArm/QardioCore, iHealth, KardiaMobile (AliveCor)
  - SpO2/overnight: Wellue O2Ring, Masimo SafetyNet, Owlet Dream Sock
  - Smart beds: Sleep Number, Beddit
  - Respiratory: ResMed AirView (CPAP), Spiroo, NuvoAir
  - Fertility: Tempdrop, Mira, Inito, Ava
  - Smart scales: Renpho, Withings Body+
  - Cardiology: Eko CORE, Bittium Faros, Movesense
  - Research/medical-grade: Empatica EmbracePlus, Hexoskin, Biostrap
  - Aggregators: Terra API, Validic, Rook, Human API, Spike API, Healthie
- **Added** `WEARABLE_API_DETAILS` map with per-device auth method, base URL, and supported metrics. 55 entries.
- **Added** `normalize_wearable_metric` tool. Maps vendor metric names (e.g., `oura:hrv_rmssd`) to canonical keys (e.g., `hrv_rmssd_ms_mean`). 28 mappings covered.
- **Expanded** lab vendor list from 12 to **18** (added Mosaic Diagnostics, Great Plains Lab, BioReference, Empire City, Walk-In Lab, Function Health).
- **Expanded** EHR list from 12 to **19** (added InterSystems IRIS, Greenway Intergy, Azalea, Open mHealth, Particle Health, Health Gorilla, 1up.health).
- **Expanded** vendor->LOINC mappings from 107 to **134**. New codes: Dutch urinary hormone metabolites (estrone/estriol/2-OH-E1/4-OH-E1/16-OH-E1, free cortisol/cortisone, 5alpha/5beta-THF, 6-OH-melatonin), Cyrex (gluten IgG/IgA, dairy, WGA), Spectracell (RBC calcium, B6, carnitine, alpha-lipoic), Genova GI Effects (stool bacteria/parasites/chromogranin/elastase), Mosaic Diagnostics (kynurenate, quinolinate, 8-OH-DG), Boston Heart (MPO, OxLDL).
- **Coverage now**: 18 lab vendors, 55 wearables, 19 EHRs, 106 unique biomarkers, 134 vendor codes mapped.

## v1.5 - 2026-05-12

- **Added** `interpret_vitals` tool. Vital signs panel interpreter (systolic/diastolic BP, HR, RR, SpO2, temperature). Critical-value detection with panic thresholds (BP >180/120, HR <40 or >130, SpO2 <88, RR <8 or >30, temp <35 or >39). Calculates MAP. Returns the exact "Critical value flagged for same-day clinician contact" headline the orchestrator prompt requires.
- **Expanded** pytest unit suite from 23 to 37 tests. New coverage: discover, simulate_lab_panel, interpret_vitals (3 cases), fhir_create_observation (build + missing-value), fhir_create_diagnostic_report dry-run, calc_reference_ranges (in_optimal + outside), drug_interaction_matrix (known pair + too-few-drugs).
- **Stress test re-run** at concurrency 30, 300 calls: 0 errors, 902 calls/sec, p99 122 ms.

## v1.4 - 2026-05-12

- **Added** `fhir_create_observation` tool. Writes a single normalized lab Observation back to the configured FHIR server. Verified live against HAPI (server_id 132050840 returned).
- **Added** `discover` tool. Returns full system manifest in one call: tool count, endpoints, coverage stats, sample request payloads. For agents that just connected to the MCP.
- **Added** `simulate_lab_panel` tool. Generates synthetic lab panels by patient profile (healthy / metabolic_syndrome / hashimoto / insulin_resistant). Deterministic via seed. Useful for stress testing and demos without real data.
- **Added** `generate_patient_education_pdf` tool. Plain-language patient-facing PDF (~6th grade reading level), separate from the clinical brief. Used in handoff.
- **Added** `/metrics` Prometheus text-format endpoint exposing per-tool calls, errors, average latency, audit ring size, report + chart counters.
- **Added** per-tool latency + error tracking via `METRICS` and `metrics_record()`.
- **Added** SBOM.json (CycloneDX 1.5) listing pinned deps + licenses + upstream services.
- **Added** DEPLOY.md with Render / Fly.io / Railway one-pager deploy steps and Po MCP registration walkthrough.
- **Added** stress_test.py - concurrent load test. Result: 200 calls @ concurrency 20 -> 0 errors, 856 calls/sec, p99 70ms.

## v1.3 - 2026-05-12

- **Added** `calc_reference_ranges` tool. Given a LOINC code + value, returns standard reference range, functional-medicine optimal range, and a verdict (in optimal / in reference / outside). 20 markers covered with cited cutoffs.
- **Added** `chart_lab_trend` tool. Renders a PNG line chart of a lab marker over time using matplotlib. Overlays the optimal + reference range bands when the LOINC is known. Hosted at `GET /charts/{id}`, returns a markdown image link the agent embeds in Po chat.
- **Added** `drug_interaction_matrix` tool. Takes a list of drugs/supplements, resolves each to RxCUI via NIH RxNav, and returns a pairwise NxN interaction grid. Static KB seeded with notable pairs (Metformin+Berberine, Warfarin+VitK, SSRI+Tramadol, Levothyroxine+Ca/Fe, etc.). Production wires to Lexicomp/DrugBank.
- **Added** matplotlib to requirements.

## v1.2 - 2026-05-12

- **Wrote** `fhir_create_diagnostic_report` MCP tool. Closes the read+write loop: agent can now POST a FHIR R4 DiagnosticReport back to the configured FHIR server. Verified live against HAPI sandbox; server returns Location header with the new resource id.
- **Added** `/dashboard` admin HTML page (auto-refreshes every 10s) showing health probe, audit tail, recent reports, active tools count.
- **Added** `/scorecard/{patient_id}` HTML widget that pulls every patient data tool in one call and renders demographics + labs + meds + genomics + wearables as a single page.
- **Added** offline pytest unit suite (`test_units.py`, 23 tests, no HTTP required).
- **Added** four more clinical calculators: `calc_fib4` (liver fibrosis, Sterling 2006), `calc_findrisc` (diabetes risk, Lindstrom 2003), `calc_bmi_bsa` (BMI + DuBois BSA).
- **Added** persistent audit log (append-only NDJSON at `AUDIT_LOG_PATH`, default `/tmp/lc-audit.ndjson`).
- **Expanded** vendor map from 14 to 107 codes, covering 12 lab vendors (Quest, LabCorp, Boston Heart, Genova, Diagnostic Solutions, Doctor's Data, Vibrant, ZRT, Cleveland HeartLab, Spectracell, Dutch, Cyrex). Includes Lp(a), LDL-P, omega-3 index, TMAO, ADMA, heavy metals, autoimmune markers.
- **Added** `LAB_VENDORS`, `WEARABLE_SOURCES`, `EHR_SOURCES` registries (12 each).
- **Added** `list_supported_sources` tool for breadth discovery.
- **Added** Apache 2.0 LICENSE with clinical disclaimer.
- **Added** GitHub Actions CI yaml.
- **Added** Mermaid architecture diagram to README.
- **Refreshed** sample PDF with richer multi-finding clinical brief.

## v1.1 - 2026-05-12

- **Added** bearer-token auth via `MCP_BEARER_TOKEN` env. Unauthenticated calls get 401 when set.
- **Added** structured audit logging with request_id propagation. Last 200 events held in memory; `audit_tail` tool exposes them.
- **Added** httpx retries with exponential backoff (3 tries, 0.25/0.5/1.0s) on FHIR + RxNav.
- **Added** three real clinical calculators: `calc_homa_ir` (Matthews 1985), `calc_egfr_ckdepi_2021` (NEJM 2021, race-free), `calc_ascvd_10yr` (ACC/AHA 2013, all 4 sex/race buckets).
- **Added** `rxnav_interactions` for NIH RxNav drug-identity lookup.
- **Added** `generate_clinical_pdf` and `list_reports` tools. PDFs hosted at `/reports/{id}` with header/footer/scope. Agent returns clickable markdown_link.
- **Added** `/catalog` browser-friendly test console.
- **Added** `/.well-known/agent.json` A2A v1 discovery card.
- **Added** CORS middleware for browser-based Po extensions.
- **Added** `/healthz` dependency probe (HAPI FHIR latency check).
- **Added** request-ID propagation via `x-request-id` response header.

## v1.0 - 2026-05-12

- Initial release: FastAPI single-file MCP server, Streamable HTTP / JSON-RPC 2.0.
- 7 patient-data tools: get_patient_demographics, get_patient_labs, get_patient_medications, get_patient_genomics, get_wearable_snapshot, normalize_biomarker, fhir_passthrough.
- 14 vendor->LOINC mappings covering basic longevity panel.
- HAPI FHIR R4 sandbox proxy via `HAPI_FHIR_BASE` env.
- Synthetic Tyrone Schiller fixture.
- Dockerfile, Procfile, requirements.txt.
- Test harness (10 assertions).
